#!/usr/bin/env bash
# Historical Backfill cron entrypoint.
#
# Step 2 of the FMP-capacity roadmap sequence. Runs Sat/Sun mornings
# (markets closed) to pull 5y daily price history for the active
# watchlist universe and persist a HISTORICAL-namespace archive that
# the historical_replay loader can consume.
#
# Cron entry is intentionally NOT installed yet — per
# .agent/project_state.yaml, this step blocks on "one week of attribution
# data on raised budget" before activation. The operator installs the
# entry once that observation window has passed.
#
# Lock-file shared with discovery_pulse so a backfill cannot overlap
# with a pulse (both consume FMP budget).

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/stockbot}"
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/historical_backfill_$(date -u +%Y-%m-%d).log"

# Shared lock with discovery_pulse + weekly_safe — they all consume FMP budget
if [ -d /var/lock ] && [ -w /var/lock ]; then
    LOCK_FILE="/var/lock/stockbot-discovery-pulse.lock"
else
    LOCK_FILE="/tmp/stockbot-discovery-pulse.lock"
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    printf '%s historical_backfill: lock held — skipping\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG_FILE"
    exit 0
fi

cd "$REPO_ROOT"

if [ ! -x "$REPO_ROOT/.venv/bin/python" ]; then
    printf '%s historical_backfill: venv missing — aborting\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG_FILE"
    exit 1
fi

{
    printf '\n=== historical_backfill run @ %s ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    "$REPO_ROOT/.venv/bin/python" -m portfolio_automation.historical_backfill
    printf 'historical_backfill exit code: %s\n' "$?"
} >> "$LOG_FILE" 2>&1
