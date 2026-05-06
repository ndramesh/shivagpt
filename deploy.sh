#!/usr/bin/env bash
# Deploy ShivaGPT to your DGX (or any host accessible via ssh).
#
# Usage:
#   ./deploy.sh                                # rsync + venv setup
#   ./deploy.sh --service                      # ALSO install/restart systemd service
#                                              # (requires sudo on the remote)
#   HOST=mybox DIR=~/apps/shivagpt ./deploy.sh
#
# Once installed as a service, future ./deploy.sh --service runs will
# rsync new code and `systemctl restart shivagpt` so changes go live.

set -euo pipefail

HOST="${HOST:-kailash}"
DIR="${DIR:-shivagpt}"   # relative to remote $HOME
PORT="${PORT:-8000}"

INSTALL_SERVICE=0
for arg in "$@"; do
  case "$arg" in
    --service|-s) INSTALL_SERVICE=1 ;;
    *) echo "Unknown arg: $arg"; exit 2 ;;
  esac
done

here="$(cd "$(dirname "$0")" && pwd)"

echo "==> Deploying ShivaGPT to ${HOST}:${DIR}"

# 1. Make sure remote dir exists.
ssh "$HOST" "mkdir -p '$DIR'"

# 2. Sync source. Prefers rsync; falls back to scp+tar.
if command -v rsync >/dev/null 2>&1; then
  rsync -avz --delete \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.git' \
    --exclude '.DS_Store' \
    --exclude 'server.log' \
    "$here/" "$HOST:$DIR/"
else
  echo "rsync not found; falling back to tar+ssh"
  ( cd "$here" && tar --exclude='.venv' --exclude='__pycache__' --exclude='.git' -cz . ) \
    | ssh "$HOST" "cd '$DIR' && tar -xz"
fi

# 3. Set up venv + deps on the remote.
ssh "$HOST" "bash -se" <<EOF
set -euo pipefail
cd "$DIR"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q -r requirements.txt
chmod +x run.sh
echo "Remote setup complete in \$(pwd)"
EOF

# 4. Optional: install / restart the systemd service.
if [ "$INSTALL_SERVICE" = "1" ]; then
  echo
  echo "==> (Re)installing systemd unit on $HOST (will sudo)"
  # Always re-run the installer so changes to the unit (Environment lines,
  # ExecStart args, hardening directives) propagate every deploy.
  # install-service.sh is idempotent — overwrites the unit, daemon-reload,
  # then restart.
  ssh -t "$HOST" "cd '$DIR' && sudo PORT=$PORT ./install-service.sh"

  echo
  echo "==> Health check from your Mac"
  set +e
  if curl -fsS "http://$HOST:$PORT/healthz"; then
    echo
    echo "==> Open http://$HOST:$PORT in Safari."
  else
    echo "Server didn't respond yet. Try:"
    echo "  ssh $HOST 'sudo journalctl -u shivagpt -n 50 --no-pager'"
  fi
  set -e
else
  cat <<EOF

==> Deployed (no service installed).

Install as a systemd service (recommended — survives reboots):
  ./deploy.sh --service

Or start manually for one session:
  ssh $HOST 'cd $DIR && ./run.sh'                                       # foreground
  ssh $HOST 'cd $DIR && nohup ./run.sh > server.log 2>&1 & disown'      # background

Then open in Safari:
  http://$HOST:$PORT

Health check:
  curl http://$HOST:$PORT/healthz
EOF
fi
