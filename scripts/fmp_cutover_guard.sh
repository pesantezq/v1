#!/usr/bin/env bash
# FMP-4 fail-closed cutover guard for SCHEDULED entrypoints (daily / monthly).
#
# Sourced by scripts/run_daily_safe.sh and scripts/run_monthly_universe_refresh.sh
# BEFORE any .env credential loading, virtualenv activation, preflight, broker
# sync or outbound call. Needs only bash + /usr/bin/date or /bin/date, run by
# absolute path (FMP4-R2 trusted clock; no Python, no network, no secrets).
# Also directly executable:  bash scripts/fmp_cutover_guard.sh <name>
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

# FMP4-R2 P0: do NOT assign or read environment-inheritable policy globals.
# Bash preserves inherited readonly variables installed by BASH_ENV; a failed
# assignment while sourcing can otherwise turn a post-cutoff HOLD into rc 0.
# Security-critical thresholds and return codes are embedded in expressions.
# HOLD UTC epoch: 1795651200 (2026-11-26T00:00:00Z).
# Clock floor: 1794614400 (2026-11-14T00:00:00Z).
# HOLD return code: 3.

# FMP4-R2 function-attack repair (+ inherited-state collision repair).
# Inherited READ-ONLY functions/variables (BASH_ENV `readonly -f ...`,
# `readonly name=...`) cannot be redefined/assigned, and hostile functions can
# shadow `builtin`, `declare`, `return`, `exit`. Defenses:
#   (1) NO shell variables are assigned anywhere in this file except one
#       `local bins` (test seam, assignment failure => HOLD). Scratch values
#       live in POSITIONAL parameters, which cannot be inherited/read-only, and
#       the direct-run status is carried in "$1" (never `_fmp_rc`/`now`/...).
#   (2) Every status return is `builtin return N; return N` (a fake `builtin`
#       returning 0 falls through to the real `return`).
#   (3) Source-time checks use `\declare` (NOT `builtin declare`, so a `builtin`
#       function cannot blind them): a POSITIVE CANARY (our own function must
#       be reported by `declare -F`) rejects a fake/lying `declare`; a function
#       shadowing `exit` is rejected before any wrapper plain `exit` runs; and
#       if one of THIS file's functions is already READ-ONLY (our definition
#       could not take effect) the guard HOLDs rc 3 and never reaches the
#       continuation. No loop/temp variable is used (nothing to be readonly).
# This cannot contain arbitrary BASH_ENV actions that ran before this file; see
# the LIMIT note below and docs/FMP_CUTOVER_FAILCLOSED.md.
_fmp_canary_fn() { :; }
case "$(\declare -F -- _fmp_canary_fn 2>/dev/null)" in
    _fmp_canary_fn) ;;
    *)
        printf 'FMP_CUTOVER_HOLD entrypoint=guard_source reason=declare_untrusted exit=3 — no FMP calls, no credential load, no broker/trading side effects\n' >&2
        case "${BASH_SOURCE[0]}" in "$0") builtin exit 3; exit 3 ;; esac
        builtin return 3; return 3; builtin exit 3; exit 3 ;;
esac
case "$(\declare -F -- exit 2>/dev/null)" in
    '') ;;
    *)
        printf 'FMP_CUTOVER_HOLD entrypoint=guard_source reason=exit_function_shadow exit=3\n' >&2
        \builtin exit 3; exit 3 ;;
esac
case "$(\declare -pF -- fmp_cutover_guard 2>/dev/null)" in
    declare\ -*r*\ fmp_cutover_guard)
        printf 'FMP_CUTOVER_HOLD entrypoint=guard_source reason=readonly_function_fmp_cutover_guard exit=3 — no FMP calls, no credential load, no broker/trading side effects\n' >&2
        case "${BASH_SOURCE[0]}" in "$0") builtin exit 3; exit 3 ;; esac
        builtin return 3; return 3; builtin exit 3; exit 3 ;;
esac
case "$(\declare -pF -- _fmp_cutover_trusted_epoch 2>/dev/null)" in
    declare\ -*r*\ _fmp_cutover_trusted_epoch)
        printf 'FMP_CUTOVER_HOLD entrypoint=guard_source reason=readonly_function__fmp_cutover_trusted_epoch exit=3 — no FMP calls, no credential load, no broker/trading side effects\n' >&2
        case "${BASH_SOURCE[0]}" in "$0") builtin exit 3; exit 3 ;; esac
        builtin return 3; return 3; builtin exit 3; exit 3 ;;
esac

# FMP4-R2 trusted clock. Prints the epoch on stdout, "untrusted:<why>", or
# nothing (unreadable); it always returns 0 and the caller HOLDs on anything
# but a plain epoch.
#   * Shell FUNCTIONS shadowing the clock path (an exported BASH_FUNC_date%%,
#     or `date() {...}` defined by BASH_ENV) -> untrusted, fail closed.
#   * The binary is run by ABSOLUTE path through `\command`, so PATH, the hash
#     table (hash -p / BASH_CMDS), functions and aliases cannot replace it.
#   * No binary / non-numeric / pre-floor output -> unreadable, fail closed.
# LIMIT (documented, not overclaimed): BASH_ENV and exported functions execute
# arbitrary code before this file is read; a hostile environment that also
# replaces `builtin` AND `declare` (or `[`, `printf`, ...) cannot be fully
# detected from inside bash. Production assumes a trusted service environment:
# BASH_ENV unset and no exported functions (systemd/cron defaults).
_fmp_cutover_trusted_epoch() {
    local bins="/usr/bin/date /bin/date" || {
        printf 'untrusted:bins_not_assignable'; builtin return 0; return 0
    }
    case "$(\declare -F -- _fmp_canary_fn 2>/dev/null)" in
        _fmp_canary_fn) ;;
        *) printf 'untrusted:declare_untrusted'; builtin return 0; return 0 ;;
    esac
    set -- "$(\declare -F -- date command builtin declare test '[' printf type hash \
                unset set shift local /usr/bin/date /bin/date 2>/dev/null)"
    set -- "${1%%$'\n'*}"
    case "$1" in
        '') ;;
        *)
            printf 'untrusted:function_shadows_%s' "${1//[!A-Za-z0-9_]/_}"
            builtin return 0; return 0 ;;
    esac
    set -- $bins
    # $@ = candidate binaries; popped one at a time (no loop variable).
    while [ "$#" -gt 0 ]; do
        if [ -x "$1" ]; then
            set -- "$(\command "$1" -u +%s 2>/dev/null)" "$@"   # $1=out $2=bin
            case "$1" in
                ''|*[!0-9]*) shift ;;
                *)
                    if [ "$1" -ge 1794614400 ]; then
                        printf '%s' "$1"
                    else
                        printf 'untrusted:before_floor'
                    fi
                    builtin return 0; return 0 ;;
            esac
        fi
        shift
    done
    builtin return 0; return 0
}

fmp_cutover_guard() {
    # $1 = entrypoint, $2 = trusted epoch (or untrusted:<why>), $3 = forced epoch
    set -- "${1:-unknown}" "$(_fmp_cutover_trusted_epoch)"
    case "$2" in
        untrusted:*)
            printf 'FMP_CUTOVER_HOLD entrypoint=%s reason=clock_%s exit=%s — no FMP calls, no credential load, no broker/trading side effects\n' \
                "$1" "${2#untrusted:}" 3 >&2
            builtin return 3; return 3; builtin exit 3; exit 3 ;;
        ''|*[!0-9]*) set -- "$1" "" ;;
    esac
    set -- "$1" "$2" "${FMP_CUTOVER_GUARD_NOW_EPOCH:-}"
    case "$3" in
        ''|*[!0-9]*) set -- "$1" "$2" "" ;;
    esac
    if [ -n "$3" ] && [ -n "$2" ] && [ "$3" -gt "$2" ]; then
        set -- "$1" "$3"
    fi
    if [ -z "$2" ]; then
        printf 'FMP_CUTOVER_HOLD entrypoint=%s reason=clock_unreadable exit=%s — no FMP calls, no credential load, no broker/trading side effects\n' \
            "$1" 3 >&2
        builtin return 3; return 3; builtin exit 3; exit 3
    fi
    if [ "$2" -ge 1795651200 ]; then
        printf 'FMP_CUTOVER_HOLD entrypoint=%s reason=post_cutover_rights_unresolved hold_utc=2026-11-26T00:00:00Z now_epoch=%s exit=%s — no FMP calls, no credential load, no broker/trading side effects. See docs/FMP_CUTOVER_FAILCLOSED.md\n' \
            "$1" "$2" 3 >&2
        builtin return 3; return 3; builtin exit 3; exit 3
    fi
    printf 'fmp_cutover_guard: pre-cutover (entrypoint=%s, hold_utc=2026-11-26T00:00:00Z)\n' "$1"
    builtin return 0; return 0; builtin exit 0; exit 0
}

# Direct execution: run the check and exit with its status (carried in "$1";
# no named variable, so an inherited readonly `_fmp_rc` cannot change it).
case "${BASH_SOURCE[0]}" in
    "$0")
        fmp_cutover_guard "${1:-manual}"
        set -- "$?"
        builtin exit "$1"; exit "$1" ;;
esac
