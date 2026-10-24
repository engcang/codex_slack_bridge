#!/bin/sh
set -eu
umask 077

bridge_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
bridge_python=${PYTHON:-python3}
bridge_venv="$HOME/.local/share/codex-slack-bridge/venv"

if [ "$(id -u)" = 0 ]; then
    printf '%s\n' 'Run setup as the same Ubuntu user who runs VS Code, without sudo.' >&2
    exit 1
fi
"$bridge_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11+ is required; use PYTHON=python3.11 sh setup.sh")'
if [ ! -x "$bridge_venv/bin/python" ]; then
    "$bridge_python" -m venv "$bridge_venv" || {
        printf '%s\n' 'Install the venv package for your Python (for example: sudo apt install python3-venv), then retry.' >&2
        exit 1
    }
fi
"$bridge_venv/bin/python" -m pip install "$bridge_root[router]"
exec "$bridge_venv/bin/codex-slack" setup "$@"
