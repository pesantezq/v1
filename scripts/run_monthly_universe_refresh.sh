#!/usr/bin/env bash
# Monthly Universe-Membership Refresh — forces a full S&P 500 constituent
# re-resolution + full_scan so watchlist MEMBERSHIP stays current.
#
# Why this exists, and why it is NOT `--run-mode monthly`:
#   Recovery no longer needs a monthly run: the weekly path self-heals whenever
#   the cached watchlist falls below the trust floor (MIN_TRUSTED_DATASET_SIZE).
#   What the weekly path does NOT do is discover NEW members while the cache is
#   healthy — weekly_refresh() only re-filters what is already cached, so
#   membership slowly goes stale even though the dataset stays sufficient.
#
#   `main.py --run-mode monthly` would rebuild, but it does strictly more than a
#   membership refresh: it applies theme boosts to scanner candidates and routes
#   email through send_monthly_memo instead of the daily memo path. So this
#   wrapper uses the NARROWEST reusable mechanism instead —
#   `--run-mode weekly --force-universe-refresh`, which takes the existing full
#   rebuild branch (fresh constituent resolution + full_scan + screening
#   assessment + save_watchlist) and nothing else.
#
# FMP call volume (measured live 2026-08-03, per full scan):
#   ~503 stable/profile  +  ~503 stable/key-metrics (+financial-growth where
#   key-metrics lacks revenueGrowth)  +  ~503 stable/quote  ≈ 3,700 calls.
#   Profiles cache 7d and key-metrics 30d, so a monthly cadence largely re-pays
#   the metrics cost each run. The `daily` run-mode budget is uncapped
#   (config.json data_budget.run_modes.daily.call_budget == 0 means UNCAPPED),
#   and FMP access is a flat subscription rather than per-call billing, so this
#   is safe monthly. It would NOT be safe daily — do not shorten the cadence.
#
# Advisory-only: no broker path, no trade execution, no approval mutation. The
# speculative sleeve stays gated by the same scanner-quality guards as any run.
#
# Schedule (add via crontab; deliberately off-market and clear of the Monday
# 08:00 weekly / 08:30 ETF / 09:00 daily cluster and of discovery pulses):
#   30 6 1 * *  /opt/stockbot/scripts/run_monthly_universe_refresh.sh
# The 1st at 06:30 UTC is pre-market for US equities and, being date-based, will
# only ever collide with the weekday cluster when the 1st is a Monday — and even
# then the shared flock below serializes them.

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/stockbot}"
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/monthly_universe_refresh_$(date -u +%Y-%m-%d).log"

# Same lock the weekly run and discovery pulses use, so a monthly refresh can
# never overlap either of them (both are heavy FMP consumers).
if [ -d /var/lock ] && [ -w /var/lock ]; then
    LOCK_FILE="/var/lock/stockbot-discovery-pulse.lock"
else
    LOCK_FILE="/tmp/stockbot-discovery-pulse.lock"
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    printf '%s monthly_universe_refresh: lock held — skipping (weekly run or pulse active)\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG_FILE"
    exit 0
fi

cd "$REPO_ROOT"

# shellcheck disable=SC1091
if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    . "$REPO_ROOT/.env"
    set +a
fi

PY="${PY:-$REPO_ROOT/.venv/bin/python}"

{
    printf '\n===== MONTHLY UNIVERSE REFRESH %s =====\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    printf '\n-- Forced full universe rebuild (weekly mode + --force-universe-refresh) --\n'
    if "$PY" main.py --run-mode weekly --force-universe-refresh; then
        printf 'Universe refresh: OK\n'
    else
        rc=$?
        printf 'Universe refresh: FAILED (exit %s)\n' "$rc"
        exit "$rc"
    fi

    printf '\n-- Scanner-quality acceptance canary --\n'
    "$PY" -c "
import os; os.chdir('${REPO_ROOT}')
from portfolio_automation.scanner_canary import run_scanner_canary, render_canary_text
print(render_canary_text(run_scanner_canary('.')))
" || printf 'scanner_canary non-fatal failure\n'

    printf '\nMONTHLY UNIVERSE REFRESH PASSED\n'
} >> "$LOG_FILE" 2>&1
