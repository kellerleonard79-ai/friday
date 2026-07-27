#!/bin/zsh
# Launch Friday's core. Everything resolves from this script's own location so
# the file is identical on every machine — no user's home path baked in.
#
# Interpreter resolution order (first hit wins):
#   $FRIDAY_PYTHON       explicit override (set by the installer / LaunchAgent)
#   ./.venv/bin/python3  a venv sitting next to friday.py
#   python3 on PATH
set -e

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

if [ -n "$FRIDAY_PYTHON" ] && [ -x "$FRIDAY_PYTHON" ]; then
    PY="$FRIDAY_PYTHON"
elif [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then
    PY="$SCRIPT_DIR/.venv/bin/python3"
else
    PY="$(command -v python3 || true)"
fi

if [ -z "$PY" ]; then
    echo "No python3 found. Set FRIDAY_PYTHON to your interpreter." >&2
    exit 1
fi

exec "$PY" "$SCRIPT_DIR/friday.py" "$@"
