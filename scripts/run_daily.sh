#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/opt/stockbot"
PYTHON_BIN="/opt/stockbot/.venv/bin/python3"
MAIN_PY="/opt/stockbot/main.py"
LOG_DIR="/opt/stockbot/logs"
LOG_FILE="/opt/stockbot/logs/daily.log"

timestamp() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[%s] %s\n' "$(timestamp)" "$*" | tee -a "$LOG_FILE"; }

run_stage_nonblocking() {
    local stage_name="$1"; shift
    log "------------------------------------------------------------"
    log "STAGE: $stage_name"
    set +e
    (cd "$REPO_DIR" && "$@") 2>&1 \
        | awk '{ print strftime("[%Y-%m-%d %H:%M:%S]"), $0; fflush(); }' \
        | tee -a "$LOG_FILE"
    local rc=${PIPESTATUS[0]}
    set -e
    if [ "$rc" -ne 0 ]; then
        log "STAGE WARNING: $stage_name exited with code $rc (continuing chain)"
    else
        log "STAGE OK: $stage_name"
    fi
    return 0
}

mkdir -p "$LOG_DIR"
[ -x "$PYTHON_BIN" ] || { log "ERROR: Python interpreter not found: $PYTHON_BIN"; exit 1; }
[ -f "$MAIN_PY" ]    || { log "ERROR: Entry point not found: $MAIN_PY"; exit 1; }

cd "$REPO_DIR"

log "============================================================"
log "STOCKBOT DAILY CHAIN START  (cwd=$(pwd))"
log "============================================================"

# ----- Stage 1: Daily portfolio pipeline (FAIL-FAST) -----
log "STAGE: Daily portfolio pipeline (run mode = daily)"
set +e
(cd "$REPO_DIR" && "$PYTHON_BIN" "$MAIN_PY" --run-mode daily) 2>&1 \
    | awk '{ print strftime("[%Y-%m-%d %H:%M:%S]"), $0; fflush(); }' \
    | tee -a "$LOG_FILE"
pipeline_status=${PIPESTATUS[0]}
set -e
if [ "$pipeline_status" -ne 0 ]; then
    log "STAGE FAIL: Daily portfolio pipeline (exit $pipeline_status) -- aborting chain"
    exit "$pipeline_status"
fi
log "STAGE OK: Daily portfolio pipeline"

# ----- Stage 2: Discovery news integration -----
# Force cwd inside the python subprocess so relative paths land under /opt/stockbot/outputs/
run_stage_nonblocking "Discovery news integration" \
    "$PYTHON_BIN" -c "import os; os.chdir('/opt/stockbot'); from portfolio_automation.discovery.news_integration import run_discovery_news_integration; print(run_discovery_news_integration(run_mode='discovery'))"

# ----- Stage 3: Automatic promotion governance -----
run_stage_nonblocking "Automatic promotion governance" \
    "$PYTHON_BIN" -c "import os; os.chdir('/opt/stockbot'); from portfolio_automation.discovery.automatic_promotion_governance import run_automatic_promotion_governance; print(run_automatic_promotion_governance(run_mode='discovery', write_files=True))"

# ----- Stage 4: Daily memo (also triggers email if MEMO_EMAIL_ENABLED=1) -----
# Use runpy to invoke the -m entrypoint after forcing cwd
run_stage_nonblocking "Daily memo + email" \
    "$PYTHON_BIN" -c "import os; os.chdir('/opt/stockbot'); import runpy; runpy.run_module('watchlist_scanner.daily_memo', run_name='__main__')"

log "============================================================"
log "STOCKBOT DAILY CHAIN COMPLETE"
log "============================================================"
