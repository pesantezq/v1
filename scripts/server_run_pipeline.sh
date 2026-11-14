#!/usr/bin/env bash
# =============================================================================
# server_run_pipeline.sh — Run the daily investment pipeline on the VPS
# Called by cron or manually; logs are appended to logs/server_pipeline.log
# =============================================================================
set -euo pipefail

# FMP4-R1 fail-closed cutover HOLD — FIRST action, self-located, before venv,
# .env, logs and the pipeline. Exit 3 when held or guard missing. No bypass.
_FMP_GUARD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fmp_cutover_guard.sh"
# shellcheck source=/dev/null
if [ -r "$_FMP_GUARD" ] && . "$_FMP_GUARD"; then
    fmp_cutover_guard server_run_pipeline || exit $?
else
    printf 'FMP_CUTOVER_HOLD entrypoint=server_run_pipeline reason=guard_missing exit=3\n' >&2
    exit 3
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="$REPO_DIR/logs/server_pipeline.log"
VENV_ACTIVATE="$REPO_DIR/.venv/bin/activate"

cd "$REPO_DIR"

# Activate virtual environment
if [ ! -f "$VENV_ACTIVATE" ]; then
    echo "ERROR: Virtual environment not found at $REPO_DIR/.venv" >&2
    echo "       Run scripts/server_setup.sh first." >&2
    exit 1
fi
# shellcheck source=/dev/null
source "$VENV_ACTIVATE"

# Load .env safely (export only KEY=VALUE lines; ignore comments and blanks)
if [ -f "$REPO_DIR/.env" ]; then
    set -o allexport
    # shellcheck source=/dev/null
    source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$REPO_DIR/.env")
    set +o allexport
fi

mkdir -p "$REPO_DIR/logs"

echo "==> [$(date '+%Y-%m-%d %H:%M:%S')] Starting daily pipeline..." | tee -a "$LOG_FILE"

python run_daily_pipeline.py --send-email 2>&1 | tee -a "$LOG_FILE"
EXIT_CODE=${PIPESTATUS[0]}

if [ "$EXIT_CODE" -eq 0 ]; then
    echo "==> [$(date '+%Y-%m-%d %H:%M:%S')] Pipeline finished successfully." | tee -a "$LOG_FILE"
else
    echo "==> [$(date '+%Y-%m-%d %H:%M:%S')] Pipeline exited with code $EXIT_CODE." | tee -a "$LOG_FILE"
fi

exit "$EXIT_CODE"
