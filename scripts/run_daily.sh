#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/opt/stockbot"
PYTHON_BIN="/opt/stockbot/.venv/bin/python3"
MAIN_PY="/opt/stockbot/main.py"
LOG_DIR="/opt/stockbot/logs"
LOG_FILE="/opt/stockbot/logs/daily.log"

timestamp() {
    date '+%Y-%m-%d %H:%M:%S'
}

log() {
    printf '[%s] %s\n' "$(timestamp)" "$*" | tee -a "$LOG_FILE"
}

mkdir -p "$LOG_DIR"

[ -x "$PYTHON_BIN" ] || {
    log "ERROR: Python interpreter not found or not executable: $PYTHON_BIN"
    exit 1
}

[ -f "$MAIN_PY" ] || {
    log "ERROR: Entry point not found: $MAIN_PY"
    exit 1
}

cd "$REPO_DIR"

log "Starting daily portfolio run"
log "Command: $PYTHON_BIN $MAIN_PY --run-mode daily"

set +e
"$PYTHON_BIN" "$MAIN_PY" --run-mode daily 2>&1 \
    | awk '{ print strftime("[%Y-%m-%d %H:%M:%S]"), $0; fflush(); }' \
    | tee -a "$LOG_FILE"
pipeline_status=${PIPESTATUS[0]}
set -e

if [ "$pipeline_status" -ne 0 ]; then
    log "Daily portfolio run failed with exit code $pipeline_status"
    exit "$pipeline_status"
fi

log "Daily portfolio run completed successfully"
