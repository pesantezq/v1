#!/usr/bin/env bash
# Discovery Pulse cron entrypoint.
#
# Runs portfolio_automation.discovery_pulse, which triggers theme_engine +
# scraped_intel pipelines on off-hours when the daily cron is not running.
# Monthly bandwidth caps ($10 OpenAI, 5000 FMP calls) enforced in Python.
#
# Lock-file gating: non-blocking flock prevents overlap with other pulses
# or with the daily cron (if that wrapper also acquires the same lock).
# Lock file lives in /var/lock when available, falls back to /tmp.

set -euo pipefail

# FMP4-R1 fail-closed cutover HOLD — FIRST action, self-located, before logs,
# the shared flock, .env and any FMP call. Exit 3 when held or guard missing.
_FMP_GUARD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fmp_cutover_guard.sh"
# shellcheck source=/dev/null
if [ -r "$_FMP_GUARD" ] && . "$_FMP_GUARD"; then
    fmp_cutover_guard discovery_pulse || exit $?
else
    printf 'FMP_CUTOVER_HOLD entrypoint=discovery_pulse reason=guard_missing exit=3\n' >&2
    exit 3
fi

REPO_ROOT="${REPO_ROOT:-/opt/stockbot}"
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/discovery_pulse_$(date -u +%Y-%m-%d).log"

# Lock file location: prefer /var/lock (persistent perms); fall back to /tmp
if [ -d /var/lock ] && [ -w /var/lock ]; then
    LOCK_FILE="/var/lock/stockbot-discovery-pulse.lock"
else
    LOCK_FILE="/tmp/stockbot-discovery-pulse.lock"
fi

# Acquire non-blocking exclusive lock; exit 0 if held by another process
# (don't error the cron — overlap-avoidance is the design, not a failure).
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    printf '%s discovery_pulse: lock held by another process — skipping\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG_FILE"
    exit 0
fi

cd "$REPO_ROOT"

# Note: .env is loaded by the Python module itself (utils.load_env) rather
# than bash-sourced here, because Gmail app passwords and other secrets
# often contain spaces that bash sourcing would parse as commands.

if [ ! -x "$REPO_ROOT/.venv/bin/python" ]; then
    printf '%s discovery_pulse: venv missing at %s/.venv — aborting\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$REPO_ROOT" >> "$LOG_FILE"
    exit 1
fi

{
    printf '\n=== discovery_pulse run @ %s ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    "$REPO_ROOT/.venv/bin/python" -m portfolio_automation.discovery_pulse
    printf 'discovery_pulse exit code: %s\n' "$?"
} >> "$LOG_FILE" 2>&1
