#!/usr/bin/env bash
# Install ShivaGPT as a systemd service on the local machine (run on kailash).
#
# Run from your Mac:
#   ssh -t kailash 'cd shivagpt && sudo ./install-service.sh'
#
# Or interactively:
#   ssh kailash
#   cd ~/shivagpt
#   sudo ./install-service.sh
#
# What it does:
#   * writes /etc/systemd/system/shivagpt.service
#   * systemctl daemon-reload && enable --now
#   * shows status + tails the journal briefly so you see it come up
#
# Safe to re-run; it overwrites the unit file and restarts the service.

set -euo pipefail

PORT="${PORT:-8000}"
HOST_BIND="${HOST_BIND:-0.0.0.0}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
SERVICE_NAME="${SERVICE_NAME:-shivagpt}"

if [ "$EUID" -ne 0 ]; then
  echo "This script needs root (it writes /etc/systemd/system/...)."
  echo "Re-run with: sudo $0"
  exit 1
fi

# Resolve the directory this script lives in (the install dir)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The user who actually owns the install (NOT root)
RUN_USER="${SUDO_USER:-$(stat -c '%U' "$SCRIPT_DIR")}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

VENV_PY="$SCRIPT_DIR/.venv/bin/python"
SERVER_PY="$SCRIPT_DIR/server.py"

if [ ! -x "$VENV_PY" ]; then
  echo "No venv found at $VENV_PY"
  echo "Run ./deploy.sh from your Mac first (it creates the venv), then re-run this."
  exit 1
fi
if [ ! -f "$SERVER_PY" ]; then
  echo "server.py not found at $SERVER_PY"
  exit 1
fi

UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

echo "==> Writing $UNIT_PATH"
echo "    user=$RUN_USER  dir=$SCRIPT_DIR  port=$PORT  ollama=$OLLAMA_URL"

cat > "$UNIT_PATH" <<EOF
[Unit]
Description=ShivaGPT (Ollama chat UI)
After=network-online.target
Wants=network-online.target
# If Ollama runs as a systemd service on this host, wait for it.
After=ollama.service

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$SCRIPT_DIR
Environment=OLLAMA_URL=$OLLAMA_URL
Environment=PYTHONUNBUFFERED=1
# Verbose logging is ON by default. To turn it off later:
#   sudo systemctl edit shivagpt
#   [Service]
#   Environment=SHIVAGPT_DEBUG=0
#   sudo systemctl restart shivagpt
Environment=SHIVAGPT_DEBUG=1
ExecStart=$VENV_PY $SERVER_PY --host $HOST_BIND --port $PORT
Restart=on-failure
RestartSec=3
# Reasonable hardening (loosen if you need more access)
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=$SCRIPT_DIR

[Install]
WantedBy=multi-user.target
EOF

echo "==> systemctl daemon-reload"
systemctl daemon-reload

echo "==> Enabling and (re)starting ${SERVICE_NAME}.service"
systemctl enable "${SERVICE_NAME}.service"
# Always restart, not just `--now`, so a re-install actually picks up
# any changes to Environment lines or ExecStart args (otherwise systemd
# leaves the running process alone if it's already up).
systemctl restart "${SERVICE_NAME}.service"

# Give it a beat to come up before checking
sleep 1.5

echo
echo "==> systemctl status ${SERVICE_NAME} --no-pager"
systemctl --no-pager --full status "${SERVICE_NAME}.service" || true

echo
echo "==> Last log lines (journalctl -u ${SERVICE_NAME} -n 25)"
journalctl --no-pager -u "${SERVICE_NAME}.service" -n 25 || true

echo
echo "==> Health check"
if command -v curl >/dev/null 2>&1; then
  set +e
  curl -fsS "http://127.0.0.1:${PORT}/healthz" && echo
  set -e
else
  echo "(curl not installed — skip)"
fi

cat <<EOF

==> Done.

Service:    ${SERVICE_NAME}.service
URL:        http://$(hostname):${PORT}
Logs:       sudo journalctl -u ${SERVICE_NAME} -f
Restart:    sudo systemctl restart ${SERVICE_NAME}
Stop:       sudo systemctl stop ${SERVICE_NAME}
Disable:    sudo systemctl disable --now ${SERVICE_NAME}
Edit unit:  sudo \$EDITOR $UNIT_PATH && sudo systemctl daemon-reload && sudo systemctl restart ${SERVICE_NAME}
EOF
