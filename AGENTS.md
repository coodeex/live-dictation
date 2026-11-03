# Live Dictation development workflow

The checkout used by `live-dictation.service` is production. Inspect the user
service's `ExecStart` to locate it. Do not edit its tracked files during development.

Use a separate sibling Git worktree named `live-dictation-dev` for changes.
If it does not exist, create a detached worktree from the current `main` commit.
The dev instance uses `LIVE_DICTATION_INSTANCE=dev`, which gives it its own socket,
history, draft, and settings. It has no F4 shortcut and does not call the production
GNOME focus helper. Launch it with `scripts/dev-app`; control it with
`scripts/dev-client`. Test Caret delivery only in a scratch field you own.

Before release, smoke-test the dev instance, run syntax and relevant checks, and
commit only intended source and documentation changes. Never commit keys,
recordings, transcripts, or logs. From the dev checkout, run
`python3 scripts/release.py --tested`. This pushes the dev commit to GitHub `main`,
then waits until production has no recording, finalization, microphone capture, or
visible popup for three continuous seconds before fast-forwarding and restarting
the user service. The script rechecks after checkout and before restart. If it
cannot safely deploy, leave production running and report the reason.

Do not restart, stop, or send F4 to production to make a release window. Do not
test development changes through the production socket or write to production's
history and state. Preserve the user's clipboard and focused app during tests.
