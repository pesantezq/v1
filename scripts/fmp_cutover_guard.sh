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

# FMP4-R2 function-attack repair: inherited READ-ONLY functions (BASH_ENV
# `readonly -f fmp_cutover_guard` / `readonly -f return`) cannot be redefined
# and, with a bare `return`, could force rc 0. Defenses, all without a bare
# `return`/`exit` (hostile functions can shadow those): (1) every status
# return below is `builtin return N`; (2) at source time, if one of THIS
# file's own functions is already defined READ-ONLY (so our definition could
# not take effect), HOLD rc 3 explicitly and never reach the continuation;
# (3) any function shadowing `exit` is rejected BEFORE calling a wrapper's
# own plain `exit` (which could otherwise turn HOLD rc=3 into rc=0).
# This cannot contain arbitrary BASH_ENV actions that ran before this file.
if \builtin declare -F -- exit >/dev/null 2>&1; then
    \builtin printf 'FMP_CUTOVER_HOLD entrypoint=guard_source reason=exit_function_shadow exit=3\n' >&2
    \builtin exit 3
fi
for _fmp_n in fmp_cutover_guard _fmp_cutover_trusted_epoch; do
    _fmp_d="$(\builtin declare -pF -- "$_fmp_n" 2>/dev/null)" || _fmp_d=""
    _fmp_f="${_fmp_d#declare }"
    _fmp_f="${_fmp_f%% *}"
    case "$_fmp_d:$_fmp_f" in
        declare\ -*:*r*)
            printf 'FMP_CUTOVER_HOLD entrypoint=guard_source reason=readonly_function_%s exit=3 — no FMP calls, no credential load, no broker/trading side effects\n' \
                "$_fmp_n" >&2
            if [ "${BASH_SOURCE[0]}" = "$0" ]; then builtin exit 3; fi
            builtin return 3; return 3; builtin exit 3; exit 3 ;;
    esac
done
unset _fmp_n _fmp_d _fmp_f

# FMP4-R2 trusted clock. Prints the epoch on stdout, or "untrusted:<why>" /
# nothing; non-zero status means "cannot prove pre-cutover" (caller HOLDs).
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
    local name bin out
    local bins="/usr/bin/date /bin/date"
    for name in date command builtin declare test '[' printf type hash unset \
                /usr/bin/date /bin/date; do
        if \declare -F -- "$name" >/dev/null 2>&1 \
           || \builtin \declare -F -- "$name" >/dev/null 2>&1; then
            printf 'untrusted:function_shadows_%s' "${name//[!A-Za-z0-9_]/_}"
            builtin return 1; return 1; builtin exit 1; exit 1
        fi
    done
    for bin in $bins; do
        if [ -x "$bin" ]; then
            out="$(\command "$bin" -u +%s 2>/dev/null)" || out=""
            case "$out" in
                ''|*[!0-9]*) out="" ;;
            esac
            if [ -n "$out" ] && [ "$out" -ge 1794614400 ]; then
                printf '%s' "$out"
                builtin return 0; return 0; builtin exit 0; exit 0
            fi
            [ -n "$out" ] && { printf 'untrusted:before_floor'; builtin return 1; return 1; builtin exit 1; exit 1; }
        fi
    done
    builtin return 1; return 1; builtin exit 1; exit 1
}

fmp_cutover_guard() {
    local entry="${1:-unknown}"
    local now forced
    now="$(_fmp_cutover_trusted_epoch)" || {
        case "$now" in
            untrusted:*)
                printf 'FMP_CUTOVER_HOLD entrypoint=%s reason=clock_%s exit=%s — no FMP calls, no credential load, no broker/trading side effects\n' \
                    "$entry" "${now#untrusted:}" 3 >&2
                builtin return 3; return 3; builtin exit 3; exit 3 ;;
        esac
        now=""
    }
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
            "$entry" 3 >&2
        builtin return 3; return 3; builtin exit 3; exit 3
    fi
    if [ "$now" -ge 1795651200 ]; then
        printf 'FMP_CUTOVER_HOLD entrypoint=%s reason=post_cutover_rights_unresolved hold_utc=2026-11-26T00:00:00Z now_epoch=%s exit=%s — no FMP calls, no credential load, no broker/trading side effects. See docs/FMP_CUTOVER_FAILCLOSED.md\n' \
            "$entry" "$now" 3 >&2
        builtin return 3; return 3; builtin exit 3; exit 3
    fi
    printf 'fmp_cutover_guard: pre-cutover (entrypoint=%s, hold_utc=2026-11-26T00:00:00Z)\n' "$entry"
    builtin return 0; return 0; builtin exit 0; exit 0
}

# Direct execution: run the check and exit with its status.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    fmp_cutover_guard "${1:-manual}"
    _fmp_rc=$?
    builtin exit "$_fmp_rc"; exit "$_fmp_rc"
fi
