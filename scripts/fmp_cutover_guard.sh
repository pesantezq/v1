#!/usr/bin/env bash
# FMP-4 fail-closed cutover guard for SCHEDULED entrypoints (daily / monthly).
#
# Sourced by scripts/run_daily_safe.sh and scripts/run_monthly_universe_refresh.sh
# BEFORE any .env credential loading, virtualenv activation, preflight, broker
# sync or outbound call. Needs only bash + `date` (no Python, no network, no
# secrets). Also directly executable:  bash scripts/fmp_cutover_guard.sh <name>
#
# HOLD instant: 2026-11-26T00:00:00Z (epoch 1795651200) — 24h before the
# 2026-11-27 FMP Starter→Free transition, so it is effective before "Nov 27" in
# ANY civil timezone (earliest is UTC+14 → 2026-11-26T10:00Z). Mirrors
# FMP_CUTOVER_HOLD_EPOCH in portfolio_automation/data_budget/fmp_cutover.py.
#
# NO BYPASS. FMP_CUTOVER_GUARD_NOW_EPOCH (tests) can only move time FORWARD:
# the guard uses max(real_now, injected_now), so it can force a HOLD but never
# lift one. An unreadable/non-numeric clock is a HOLD. Reopening needs a code
# change + scoped rights grant + deployment gate (docs/FMP_CUTOVER_FAILCLOSED.md).
#
# Exit/return status 3 == HOLD (clear, non-zero, distinct from generic failure 1).

FMP_CUTOVER_HOLD_EPOCH=1795651200
FMP_CUTOVER_HOLD_RC=3

fmp_cutover_guard() {
    local entry="${1:-unknown}"
    local now forced
    now="$(date -u +%s 2>/dev/null || true)"
    case "$now" in
        ''|*[!0-9]*) now="" ;;
    esac
    forced="${FMP_CUTOVER_GUARD_NOW_EPOCH:-}"
    case "$forced" in
        ''|*[!0-9]*) forced="" ;;
    esac
    if [ -n "$forced" ] && [ -n "$now" ] && [ "$forced" -gt "$now" ]; then
        now="$forced"
    fi
    if [ -z "$now" ]; then
        printf 'FMP_CUTOVER_HOLD entrypoint=%s reason=clock_unreadable exit=%s — no FMP calls, no credential load, no broker/trading side effects\n' \
            "$entry" "$FMP_CUTOVER_HOLD_RC" >&2
        return "$FMP_CUTOVER_HOLD_RC"
    fi
    if [ "$now" -ge "$FMP_CUTOVER_HOLD_EPOCH" ]; then
        printf 'FMP_CUTOVER_HOLD entrypoint=%s reason=post_cutover_rights_unresolved hold_utc=2026-11-26T00:00:00Z now_epoch=%s exit=%s — no FMP calls, no credential load, no broker/trading side effects. See docs/FMP_CUTOVER_FAILCLOSED.md\n' \
            "$entry" "$now" "$FMP_CUTOVER_HOLD_RC" >&2
        return "$FMP_CUTOVER_HOLD_RC"
    fi
    printf 'fmp_cutover_guard: pre-cutover (entrypoint=%s, hold_utc=2026-11-26T00:00:00Z)\n' "$entry"
    return 0
}

# Direct execution: run the check and exit with its status.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    fmp_cutover_guard "${1:-manual}"
    exit $?
fi
