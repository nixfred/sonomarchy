#!/bin/bash
# Run the unit tests with the plugin's own Python environment (created by
# sonomarchy-backend on first start). Falls back to any python that can import
# pa_dlna.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

VENV="${XDG_DATA_HOME:-${HOME}/.local/share}/io.github.nixfred.sonomarchy/venv"
if [[ -x "$VENV/bin/python" ]]; then
  PY="$VENV/bin/python"
elif python3 -c 'import pa_dlna' 2>/dev/null; then
  PY=python3
else
  echo "No environment with pa_dlna found. Start the plugin once (it builds $VENV), or: python3 -m venv $VENV && $VENV/bin/pip install --require-hashes -r requirements.lock" >&2
  exit 1
fi

exec "$PY" -m unittest discover -s tests -v
