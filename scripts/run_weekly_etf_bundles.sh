#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_weekly_etf_bundles.sh — standalone weekly ETF bundle watchlist job.
#
# Fully isolated from the daily pipeline AND from run_weekly_safe.sh: it has its
# OWN flock lock and its OWN log, so it can never affect the daily run's exit
# code or the watchlist rebuild. Observe-only; ships INERT (email gated OFF).
#
# Suggested operator crontab (VPS), after the weekly watchlist rebuild:
#   30 8 * * 1  /opt/stockbot/scripts/run_weekly_etf_bundles.sh
#
# Modes are passed through to the runner, e.g.:
#   scripts/run_weekly_etf_bundles.sh --email-dry-run
#   scripts/run_weekly_etf_bundles.sh --send-email      (requires WEEKLY_ETF_BUNDLES_EMAIL_ENABLED=1)
# ---------------------------------------------------------------------------
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/stockbot}"
cd "$REPO_ROOT"

# Load .env early so the master kill-switch below can be read from it.
if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$REPO_ROOT/.env"
    set +a
fi

# Master kill-switch — ships INERT. The cron path does nothing until the operator
# sets WEEKLY_ETF_BUNDLES_ENABLED=1 (in .env). Direct `python -m ...run` is a
# deliberate manual/backfill invocation and is intentionally NOT gated here.
_ENABLED="${WEEKLY_ETF_BUNDLES_ENABLED:-0}"
if [ "$_ENABLED" != "1" ] && [ "$_ENABLED" != "true" ] && [ "$_ENABLED" != "yes" ]; then
    echo "$(date -u +%FT%TZ) weekly_etf_bundles: disabled (set WEEKLY_ETF_BUNDLES_ENABLED=1 to enable) — skipping"
    exit 0
fi

LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/weekly_etf_bundles_$(date -u +%Y-%m-%d).log"

# Dedicated lock — NOT shared with the daily run or the discovery pulse.
if [ -d /var/lock ] && [ -w /var/lock ]; then
    LOCK_FILE="/var/lock/stockbot-weekly-etf-bundles.lock"
else
    LOCK_FILE="$REPO_ROOT/.weekly_etf_bundles.lock"
fi
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$(date -u +%FT%TZ) weekly_etf_bundles: another run holds the lock — skipping" >>"$LOG_FILE"
    exit 0
fi

{
    echo "=================================================================="
    echo "weekly_etf_bundles run @ $(date -u +%FT%TZ)"
    echo "=================================================================="

    # .env was already loaded at the top (for the kill-switch); transport +
    # email gates are therefore already in the environment here.
    if [ -d "$REPO_ROOT/.venv" ]; then
        # shellcheck disable=SC1091
        source "$REPO_ROOT/.venv/bin/activate"
    fi

    # Default mode is email-dry-run; pass --send-email explicitly to deliver.
    ARGS=("$@")
    if [ ${#ARGS[@]} -eq 0 ]; then
        ARGS=(--email-dry-run)
    fi

    python -m portfolio_automation.weekly_etf_bundles.run --root "$REPO_ROOT" "${ARGS[@]}" \
        || printf 'weekly_etf_bundles: non-fatal failure (observe-only)\n'

    echo "weekly_etf_bundles: DONE @ $(date -u +%FT%TZ)"
} >>"$LOG_FILE" 2>&1
