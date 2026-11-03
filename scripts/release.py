#!/usr/bin/env python3
"""Push a tested dev commit, then update production at a safe idle moment."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time


DEV = Path(__file__).resolve().parents[1]
IDLE_SECONDS = 3.0
POLL_SECONDS = 0.5


def run(*args, cwd=None, check=True):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f'{" ".join(args)} failed: {result.stderr.strip()}')
    return result


def git(path, *args):
    return run('git', '-C', str(path), *args).stdout.strip()


def production_checkout():
    override = os.environ.get('LIVE_DICTATION_PRODUCTION')
    if override:
        return Path(override).expanduser().resolve()
    output = git(DEV, 'worktree', 'list', '--porcelain')
    for section in output.split('\n\n'):
        lines = section.splitlines()
        if 'branch refs/heads/main' in lines:
            return Path(lines[0].removeprefix('worktree ')).resolve()
    raise RuntimeError('Cannot find the production worktree on main')


def status(production):
    env = os.environ.copy()
    env.pop('LIVE_DICTATION_INSTANCE', None)
    result = subprocess.run([sys.executable, str(production / 'client.py'), 'status'],
                            env=env, text=True, capture_output=True, timeout=3)
    if result.returncode:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def idle(production):
    state = status(production)
    return (state is not None and not any(state.get(key) for key in
            ('recording', 'finalizing', 'microphone_running', 'popup_visible')))


def wait_for_idle(production, timeout):
    started = time.monotonic()
    quiet_since = None
    last_notice = 0
    while time.monotonic() - started < timeout:
        now = time.monotonic()
        if idle(production):
            if quiet_since is None:
                quiet_since = now
            if now - quiet_since >= IDLE_SECONDS:
                return
        else:
            quiet_since = None
            if now - last_notice >= 15:
                print('Waiting for production recording to stop and its popup to close...',
                      flush=True)
                last_notice = now
        time.sleep(POLL_SECONDS)
    raise RuntimeError('Production never stayed idle with its popup closed; no restart made')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tested', action='store_true',
                        help='Confirm the separate dev app has been smoke-tested')
    parser.add_argument('--dry-run', action='store_true',
                        help='Check release prerequisites without pushing or restarting')
    parser.add_argument('--timeout', type=int, default=1800,
                        help='Seconds to wait for the production idle window')
    args = parser.parse_args()
    if not args.dry_run and not args.tested:
        parser.error('smoke-test the dev app first, then pass --tested')

    runtime = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
    with (runtime / 'live-dictation-release.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        production = production_checkout()
        if DEV == production:
            raise RuntimeError('Release must run from a separate dev worktree')
        if git(production, 'branch', '--show-current') != 'main':
            raise RuntimeError('Production must be on main')
        for path in (DEV, production):
            if git(path, 'status', '--porcelain'):
                raise RuntimeError(f'Uncommitted changes in {path}; release stopped')
        run(sys.executable, '-m', 'py_compile', str(DEV / 'app.py'),
            str(DEV / 'client.py'))
        git(DEV, 'fetch', 'origin', 'main')
        if run('git', '-C', str(DEV), 'merge-base', '--is-ancestor',
               'origin/main', 'HEAD', check=False).returncode:
            raise RuntimeError('Dev commit does not contain current origin/main')
        new = git(DEV, 'rev-parse', 'HEAD')
        old = git(production, 'rev-parse', 'HEAD')
        print(f'Dev: {new[:12]}  Production: {old[:12]}', flush=True)
        if args.dry_run:
            print('Dry run passed; GitHub and production unchanged')
            return
        git(DEV, 'push', 'origin', 'HEAD:main')
        print('Pushed to GitHub; waiting for production idle window', flush=True)
        wait_for_idle(production, args.timeout)
        git(production, 'fetch', 'origin', 'main')
        if git(production, 'status', '--porcelain'):
            raise RuntimeError('Production gained local changes; release stopped')
        if run('git', '-C', str(production), 'merge-base', '--is-ancestor',
               'HEAD', 'origin/main', check=False).returncode:
            raise RuntimeError('Production cannot fast-forward to origin/main')
        # Recheck after the fetch and immediately before touching the checkout.
        wait_for_idle(production, args.timeout)
        git(production, 'merge', '--ff-only', 'origin/main')
        # The old process keeps running until this check. If the user started
        # recording during the checkout, defer the restart until they finish.
        wait_for_idle(production, args.timeout)
        run('systemctl', '--user', 'restart', 'live-dictation.service')
        for _ in range(20):
            if (run('systemctl', '--user', 'is-active', 'live-dictation.service',
                    check=False).stdout.strip() == 'active' and status(production)):
                print(f'Production active at {new[:12]}', flush=True)
                return
            time.sleep(POLL_SECONDS)
        raise RuntimeError(f'Restart did not become healthy; previous commit was {old}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f'Release stopped: {error}', file=sys.stderr)
        sys.exit(1)
