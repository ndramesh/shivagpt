#!/usr/bin/env bash
# Start the ShivaGPT server. Run on the DGX after deploy.
#
#   ./run.sh                       # defaults: 0.0.0.0:8000, ollama at localhost:11434
#   PORT=9000 ./run.sh             # change port
#   OLLAMA_URL=http://other:11434 ./run.sh

set -euo pipefail

cd "$(dirname "$0")"

HOST="${HOST_BIND:-0.0.0.0}"
PORT="${PORT:-8000}"
export OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"

if [ ! -d .venv ]; then
  echo "No .venv found — creating one"
  python3 -m venv .venv
  .venv/bin/pip install -q -U pip
  .venv/bin/pip install -q -r requirements.txt
fi

echo "Starting ShivaGPT on http://$HOST:$PORT  (proxying $OLLAMA_URL)"
exec .venv/bin/python server.py --host "$HOST" --port "$PORT"
