#!/usr/bin/env bash
# Local dev loop for ShivaGPT.
#
# Runs the FastAPI server on YOUR MAC, proxying to Ollama on kailash.
# That gives you an instant edit/refresh cycle: change index.html, hit
# Cmd-Shift-R in Safari, see the change. No deploy needed.
#
# Usage:
#   ./dev.sh                        # OLLAMA_URL=http://kailash:11434, port 8000
#   PORT=8001 ./dev.sh
#   OLLAMA_URL=http://otherbox:11434 ./dev.sh
#
# Requires: python3, ssh access to kailash (only used to verify reachability).

set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
export OLLAMA_URL="${OLLAMA_URL:-http://kailash:11434}"

# Quick reachability hint
if command -v curl >/dev/null 2>&1; then
  if ! curl -fsS -m 3 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
    echo "WARNING: cannot reach $OLLAMA_URL"
    echo "  Make sure ollama on the remote is bound to 0.0.0.0:11434"
    echo "  (e.g. OLLAMA_HOST=0.0.0.0:11434 ollama serve)"
    echo "  or set OLLAMA_URL to a reachable host."
    echo
  fi
fi

if [ ! -d .venv ]; then
  echo "==> First run: creating .venv on your Mac"
  python3 -m venv .venv
  .venv/bin/pip install -q -U pip
  .venv/bin/pip install -q -r requirements.txt
fi

echo
echo "==> Dev server: http://localhost:$PORT"
echo "    Proxying  -> $OLLAMA_URL"
echo "    Edit frontend/index.html and just refresh Safari (Cmd-Shift-R)."
echo

# --reload restarts on server.py changes; the static index.html is read
# fresh on every request so no reload needed for frontend edits.
exec .venv/bin/python -m uvicorn server:app \
  --host 127.0.0.1 --port "$PORT" --reload
