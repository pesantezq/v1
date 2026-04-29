#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/opt/stockbot"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE_NAME="stockbot-daily"
SERVICE_SRC="/opt/stockbot/deploy/stockbot-daily.service"
TIMER_SRC="/opt/stockbot/deploy/stockbot-daily.timer"
SERVICE_DEST="/etc/systemd/system/stockbot-daily.service"
TIMER_DEST="/etc/systemd/system/stockbot-daily.timer"
LOG_DIR="/opt/stockbot/logs"

[ -f "$SERVICE_SRC" ] || {
    echo "ERROR: Missing service file: $SERVICE_SRC" >&2
    exit 1
}

[ -f "$TIMER_SRC" ] || {
    echo "ERROR: Missing timer file: $TIMER_SRC" >&2
    exit 1
}

mkdir -p "$LOG_DIR"

install -m 0644 "$SERVICE_SRC" "$SERVICE_DEST"
install -m 0644 "$TIMER_SRC" "$TIMER_DEST"

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.timer"

echo "Installed ${SERVICE_NAME}.service and ${SERVICE_NAME}.timer"
echo "Working directory: $REPO_DIR"
echo "Logs directory: $LOG_DIR"
echo "Check timer status with: systemctl status ${SERVICE_NAME}.timer"
