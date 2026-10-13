#!/usr/bin/env python3
"""Send a command to the already running dictation UI."""
import os
import socket
import subprocess
import sys

COMMAND = sys.argv[1] if len(sys.argv) > 1 else "toggle"
PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "live-dictation.sock")
try:
    if COMMAND == 'toggle':
        subprocess.run(['gdbus', 'call', '--session',
                        '--dest', 'org.gnome.Shell.Extensions.LiveDictation',
                        '--object-path', '/org/gnome/Shell/Extensions/LiveDictation',
                        '--method', 'org.gnome.Shell.Extensions.LiveDictation.RememberTarget'],
                       capture_output=True, timeout=1)
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(2)
        connection.connect(PATH)
        connection.sendall((COMMAND + "\n").encode())
        if COMMAND == 'status':
            print(connection.recv(4096).decode())
except OSError as error:
    print(f"Live dictation is unavailable: {error}", file=sys.stderr)
    sys.exit(1)
