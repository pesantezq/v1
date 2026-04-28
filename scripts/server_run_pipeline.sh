#!/usr/bin/env bash
# =============================================================================
# server_run_pipeline.sh — Run the daily investment pipeline on the VPS
# Called by cron or manually; logs are appended to logs/server_pipeline.log
# =============================================================================
set -euo pipefail

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
