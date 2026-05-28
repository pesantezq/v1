#!/usr/bin/env bash
# Weekly Safe Wrapper — refreshes data/fmp_cache/top100_watchlist.json via
# FMP scoring. Designed to run at the start of the workweek (Monday morning
# UTC) before the daily cron so the daily run inherits a fresh universe.
#
# Differences from run_daily_safe.sh:
#   - Invokes main.py --run-mode weekly (vs daily)
#   - Skips the heavy advisor / memo stages — weekly is a data-prep run
#   - Uses a separate log file (logs/weekly_safe_<date>.log)
#   - Acquires the same discovery-pulse lock to prevent overlap with pulses

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/stockbot}"
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/weekly_safe_$(date -u +%Y-%m-%d).log"

# Shared lock with discovery_pulse — weekly is a heavier FMP-consuming run
# and we want pulses to skip while it's underway.
if [ -d /var/lock ] && [ -w /var/lock ]; then
    LOCK_FILE="/var/lock/stockbot-discovery-pulse.lock"
else
    LOCK_FILE="/tmp/stockbot-discovery-pulse.lock"
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    printf '%s weekly_safe: lock held — skipping (another pulse or weekly run is active)\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG_FILE"
    exit 0
fi

cd "$REPO_ROOT"

if [ ! -x "$REPO_ROOT/.venv/bin/activate" ] && [ ! -f "$REPO_ROOT/.venv/bin/activate" ]; then
    printf '%s weekly_safe: venv missing at %s/.venv — aborting\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$REPO_ROOT" >> "$LOG_FILE"
    exit 1
fi

# shellcheck disable=SC1091
source "$REPO_ROOT/.venv/bin/activate"

{
    printf '\n=== weekly_safe run @ %s ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'Repo root: %s\n' "$REPO_ROOT"

    printf '\n-- Preflight --\n'
    if "$REPO_ROOT/scripts/preflight.sh"; then
        printf 'Preflight: OK\n'
    else
        printf 'Preflight: FAILED — aborting weekly run\n'
        exit 1
    fi

    printf '\n-- Weekly pipeline --\n'
    if python main.py --run-mode weekly; then
        printf 'Weekly pipeline: OK\n'
    else
        rc=$?
        printf 'Weekly pipeline: FAILED (exit %s)\n' "$rc"
        exit "$rc"
    fi

    printf '\n-- Verify watchlist freshness --\n'
    python -c "
import json
from datetime import datetime, timezone
from pathlib import Path
p = Path('data/fmp_cache/top100_watchlist.json')
d = json.loads(p.read_text())
src = d.get('watchlist_source','?')
cands = d.get('candidates') or []
non_fb = [c for c in cands if c.get('watchlist_source') != 'fallback']
print(f'watchlist_source={src}  candidates={len(cands)}  non-fallback={len(non_fb)}')
if src == 'fallback' or len(non_fb) == 0:
    print('WARN: top100_watchlist is still fallback content')
    import sys; sys.exit(2)
"

    printf '\n-- Universe sanitation (weekly) --\n'
    python -m portfolio_automation.universe_sanitation weekly

    printf '\n-- Universe sanitation (monthly rolling 30d) --\n'
    python -m portfolio_automation.universe_sanitation monthly

    printf '\n-- Pattern learning (weekly) --\n'
    python -m portfolio_automation.pattern_learning weekly

    printf '\n-- Pattern learning (monthly rolling 30d) --\n'
    python -m portfolio_automation.pattern_learning monthly

    printf '\n-- Pattern learning (yearly, partitioned by gauge x regime) --\n'
    python -m portfolio_automation.pattern_learning yearly

    printf '\n-- Retune suggestions --\n'
    python -m portfolio_automation.retune_suggestions

    printf '\n-- Retune auto-apply (gated by guardrails) --\n'
    python -m portfolio_automation.retune_auto_apply --apply

    printf '\nDAILY RUN PASSED\n'
} >> "$LOG_FILE" 2>&1
