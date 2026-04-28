#!/usr/bin/env bash
# =============================================================================
# install_cron.sh — Install the daily StockBot pipeline cron job
# Preserves existing crontab entries; safe to re-run (no duplicates added).
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIPELINE_SCRIPT="$REPO_DIR/scripts/server_run_pipeline.sh"
CRON_LOG="$REPO_DIR/logs/cron.log"
CRON_ENTRY="0 9 * * * $PIPELINE_SCRIPT >> $CRON_LOG 2>&1"

if [ ! -f "$PIPELINE_SCRIPT" ]; then
    echo "ERROR: Pipeline script not found: $PIPELINE_SCRIPT" >&2
    exit 1
fi

chmod +x "$PIPELINE_SCRIPT"

# Capture current crontab (empty is OK)
CURRENT_CRONTAB="$(crontab -l 2>/dev/null || true)"

if echo "$CURRENT_CRONTAB" | grep -qF "$PIPELINE_SCRIPT"; then
    echo "==> Cron entry already exists, skipping."
else
    echo "==> Installing cron entry..."
    (
        echo "$CURRENT_CRONTAB"
        echo "$CRON_ENTRY"
    ) | crontab -
    echo "==> Cron entry installed."
fi

echo ""
echo "==> Current crontab:"
crontab -l
