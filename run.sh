#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Bundled Tk runtime (used when the system Python lacks libtk8.6.so).
if [[ -f "$PWD/.tk/libtk8.6.so" ]]; then
    export LD_LIBRARY_PATH="$PWD/.tk${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export TK_LIBRARY="$PWD/.tk/tk8.6"
fi

# Create the local Python environment on first run if it is missing.
if [[ ! -x .venv/bin/python ]]; then
    printf '\n==> Creating local .venv (pymobiledevice3 + websocket-client)\n'
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip >/dev/null
    .venv/bin/pip install pymobiledevice3 websocket-client
fi

export PATH="$PWD/.venv/bin:$PATH"
exec .venv/bin/python bookloader.py "$@"
