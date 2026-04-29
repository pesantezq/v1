#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/opt/stockbot"
DECISION_JSON="/opt/stockbot/outputs/latest/decision_plan.json"
DECISION_MD="/opt/stockbot/outputs/latest/decision_plan.md"
LAST_SUCCESS="/opt/stockbot/data/last_success.json"
LOG_FILE="/opt/stockbot/logs/daily.log"

timestamp() {
    date '+%Y-%m-%d %H:%M:%S'
}

log() {
    printf '[%s] %s\n' "$(timestamp)" "$*"
}

check_file() {
    local path="$1"
    if [ -f "$path" ]; then
        log "PASS: Found $path"
    else
        log "FAIL: Missing $path"
        return 1
    fi
}

cd "$REPO_DIR"

check_file "$DECISION_JSON"
check_file "$DECISION_MD"
check_file "$LAST_SUCCESS"

if [ -f "$LOG_FILE" ]; then
    log "Last 20 log lines from $LOG_FILE"
    tail -n 20 "$LOG_FILE"
else
    log "FAIL: Missing $LOG_FILE"
    exit 1
fi
