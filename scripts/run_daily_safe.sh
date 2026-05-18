#!/usr/bin/env bash
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
        printf '\nDAILY RUN PASSED\n'
    else
        printf '\nDAILY RUN FAILED\n' >&2
    fi
    exit "$exit_code"
}

trap finish EXIT

REPO_ROOT="$(resolve_repo_root)" || {
    printf 'DAILY RUN FAILED\n' >&2
    exit 1
}
cd "$REPO_ROOT"

LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/daily_safe_$(date '+%Y-%m-%d').log"

exec > >(tee -a "$LOG_FILE") 2>&1

section "Daily Safe Wrapper"
printf 'Repo root: %s\n' "$REPO_ROOT"
printf 'Log file: %s\n' "$LOG_FILE"

section "Preflight"
"$REPO_ROOT/scripts/preflight.sh"
printf 'Preflight passed. Continuing to daily run.\n'

section "Runtime Environment"
if [ -f "$REPO_ROOT/.venv/bin/activate" ]; then
    # shellcheck source=/dev/null
    source "$REPO_ROOT/.venv/bin/activate"
elif [ -f "$REPO_ROOT/.venv/Scripts/activate" ]; then
    # shellcheck source=/dev/null
    source "$REPO_ROOT/.venv/Scripts/activate"
else
    printf 'FAIL: Could not locate a virtualenv activation script.\n' >&2
    exit 1
fi

if [ -f "$REPO_ROOT/.env" ]; then
    load_dotenv_file "$REPO_ROOT/.env"
fi

section "Daily Pipeline"
run_cmd=(python main.py --run-mode daily)
if [ "${DRY_RUN_MODE:-0}" = "1" ]; then
    run_cmd+=(--dry-run)
    printf 'DRY_RUN_MODE=1 detected. Running advisory daily pipeline in --dry-run mode.\n'
fi

printf 'Command: %s\n' "${run_cmd[*]}"
"${run_cmd[@]}"

# Non-blocking advisory stages. Sandbox-only writes; failures must not
# abort the chain because the official decision plan has already landed.
run_aux_stage() {
    local label="$1"; shift
    section "$label"
    if "$@"; then
        printf '%s: OK\n' "$label"
    else
        printf '%s: WARN (non-blocking; exit %d)\n' "$label" "$?" >&2
    fi
}

# Stage 2 — Discovery news integration (sandbox research lane).
run_aux_stage "Discovery news integration" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.discovery.news_integration import run_discovery_news_integration; print(run_discovery_news_integration(run_mode='discovery'))"

# Stage 3 — Automatic promotion governance (sandbox research lane).
run_aux_stage "Automatic promotion governance" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.discovery.automatic_promotion_governance import run_automatic_promotion_governance; print(run_automatic_promotion_governance(run_mode='discovery', write_files=True))"

# Stage 4 — Daily investment memo (also triggers email if MEMO_EMAIL_ENABLED=1).
run_aux_stage "Daily memo + email" \
    python -c "import os; os.chdir('${REPO_ROOT}'); import runpy; runpy.run_module('watchlist_scanner.daily_memo', run_name='__main__')"
