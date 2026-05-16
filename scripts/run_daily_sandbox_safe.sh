#!/usr/bin/env bash
#
# Daily Sandbox Safe Wrapper
# ==========================
#
# Runs the sandbox/research lane orchestrator
# (tools.daily_sandbox_run) under a virtualenv with structured logging.
#
# Safety:
#   - Observe-only — never trades, never calls brokers
#   - Writes only to outputs/sandbox/discovery/
#   - Does not block, restart, or modify the official daily pipeline
#   - Non-zero step failures are recorded in the status artifact but the
#     wrapper still exits 0 so a systemd timer reports success
#
# Usage:
#   bash scripts/run_daily_sandbox_safe.sh
#   DRY_RUN_MODE=1 bash scripts/run_daily_sandbox_safe.sh
set -euo pipefail

section() {
    printf '\n== %s ==\n' "$1"
}

find_repo_root() {
    local start="$1"
    while [ -n "$start" ]; do
        if [ -f "$start/main.py" ] && [ -f "$start/requirements.txt" ] && [ -d "$start/scripts" ]; then
            printf '%s\n' "$start"
            return 0
        fi
        local parent
        parent="$(dirname "$start")"
        if [ "$parent" = "$start" ]; then
            break
        fi
        start="$parent"
    done
    return 1
}

resolve_repo_root() {
    local candidate=""

    if [ -n "${REPO_ROOT:-}" ] && [ -f "${REPO_ROOT}/main.py" ]; then
        printf '%s\n' "$REPO_ROOT"
        return 0
    fi

    candidate="$(find_repo_root "$PWD" || true)"
    if [ -n "$candidate" ]; then
        printf '%s\n' "$candidate"
        return 0
    fi

    candidate="$(find_repo_root "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || true)"
    if [ -n "$candidate" ]; then
        printf '%s\n' "$candidate"
        return 0
    fi

    return 1
}

load_dotenv_file() {
    local env_file="$1"
    local line trimmed key value
    [ -f "$env_file" ] || return 0

    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        trimmed="${line#"${line%%[![:space:]]*}"}"
        if [ -z "$trimmed" ] || [ "${trimmed:0:1}" = "#" ]; then
            continue
        fi
        trimmed="${trimmed#export }"
        if [[ "$trimmed" != *=* ]]; then
            continue
        fi
        key="${trimmed%%=*}"
        value="${trimmed#*=}"
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        export "$key=$value"
    done < "$env_file"
}

finish() {
    local exit_code=$?
    trap - EXIT
    if [ "$exit_code" -eq 0 ]; then
        printf '\nDAILY SANDBOX RUN COMPLETE (observe-only)\n'
    else
        printf '\nDAILY SANDBOX RUN WRAPPER ERROR\n' >&2
    fi
    exit "$exit_code"
}

trap finish EXIT

REPO_ROOT="$(resolve_repo_root)" || {
    printf 'DAILY SANDBOX RUN WRAPPER ERROR\n' >&2
    exit 1
}
cd "$REPO_ROOT"

LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/daily_sandbox_$(date '+%Y-%m-%d').log"

exec > >(tee -a "$LOG_FILE") 2>&1

section "Daily Sandbox Safe Wrapper"
printf 'Repo root: %s\n' "$REPO_ROOT"
printf 'Log file:  %s\n' "$LOG_FILE"
printf 'Mode:      observe-only sandbox lane (no trades, no broker calls)\n'

section "Runtime Environment"
if [ -f "$REPO_ROOT/.venv/bin/activate" ]; then
    # shellcheck source=/dev/null
    source "$REPO_ROOT/.venv/bin/activate"
elif [ -f "$REPO_ROOT/.venv/Scripts/activate" ]; then
    # shellcheck source=/dev/null
    source "$REPO_ROOT/.venv/Scripts/activate"
else
    printf 'WARNING: Could not locate a virtualenv activation script; falling back to system python.\n' >&2
fi

if [ -f "$REPO_ROOT/.env" ]; then
    load_dotenv_file "$REPO_ROOT/.env"
fi

section "Sandbox Lane"
run_cmd=(python -m tools.daily_sandbox_run --base-dir "$REPO_ROOT" -v)
if [ "${DRY_RUN_MODE:-0}" = "1" ]; then
    run_cmd+=(--dry-run)
    printf 'DRY_RUN_MODE=1 — running sandbox runner in --dry-run mode.\n'
fi

printf 'Command: %s\n' "${run_cmd[*]}"
# Always exit 0 from the wrapper so a systemd timer does not flap.  The
# status artifact records per-step success/failure.
"${run_cmd[@]}" || printf 'WARNING: sandbox runner returned non-zero; see status artifact.\n' >&2
