#!/usr/bin/env bash
# Daily Portfolio-Sim Wrapper — runs ONLY the sandbox simulation stages
# (backtest + forward projection + research-backed strategy lab) once a day,
# AFTER the daily pipeline has refreshed the price archive + holdings.
#
# Why a separate script (not run_weekly_safe.sh): the weekly script also rebuilds
# the top-100 watchlist with many FMP calls — we do NOT want that daily. The sims
# are observe-only, sandbox-only, run-mode=discovery; they read the cached 5y
# archive and never write decision_plan / config / signal registry.
#
# Cadence rationale: sim inputs (price archive, holdings, OOS windows) change at
# most once per day, so daily is the most frequent cadence that yields fresh
# output. Non-fatal per stage; never aborts.

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/stockbot}"
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/sims_daily_$(date -u +%Y-%m-%d).log"

cd "$REPO_ROOT"

# Activate venv.
if [ -f "$REPO_ROOT/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    . "$REPO_ROOT/.venv/bin/activate"
else
    printf '%s sims_daily: venv missing at %s/.venv — aborting\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$REPO_ROOT" >> "$LOG_FILE"
    exit 1
fi

# Load .env so a cache-miss can fall back to FMP (free); harmless if absent.
if [ -f "$REPO_ROOT/.env" ]; then
    set -a; . "$REPO_ROOT/.env"; set +a
fi

{
    printf '%s sims_daily: start\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    printf '\n-- Portfolio simulation: backtest (sandbox, observe-only) --\n'
    python -m portfolio_automation.portfolio_sim.run_portfolio_backtest --root "$REPO_ROOT" --run-mode discovery \
        || printf 'portfolio_sim backtest non-fatal failure\n'

    printf '\n-- Portfolio simulation: forward projection (sandbox, observe-only) --\n'
    python -m portfolio_automation.portfolio_sim.run_portfolio_projection --root "$REPO_ROOT" --run-mode discovery \
        || printf 'portfolio_sim projection non-fatal failure\n'

    printf '\n-- Research-backed strategy lab (sandbox, observe-only) --\n'
    python -m portfolio_automation.portfolio_sim.run_strategy_lab --root "$REPO_ROOT" --run-mode discovery \
        || printf 'strategy_lab non-fatal failure\n'

    printf '\n%s sims_daily: done\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >> "$LOG_FILE" 2>&1
