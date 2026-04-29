#!/usr/bin/env bash
# rerun_today_safe.sh — safely re-run the daily pipeline for today's run_id.
#
# Use this when outputs/latest artifacts are stale after a code deployment
# and the idempotency guard in run_history is blocking a fresh run.
#
# What this script does:
#   1. Detects repo root and activates .venv
#   2. Shows the current run_history row for today's run_id
#   3. Requires you to type 'rerun' to confirm
#   4. Resets only that run_id to status='failed' so main.py will re-execute it
#   5. Runs preflight.sh
#   6. Runs python main.py --run-mode daily
#   7. Verifies outputs/latest/decision_plan.json exists and is structurally valid
#
# What this script NEVER does:
#   - Delete the database
#   - Delete outputs/ or outputs/history/
#   - Modify any run_id other than today's _daily entry
#   - Skip preflight
set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers (same pattern as run_daily_safe.sh / preflight.sh)
# ---------------------------------------------------------------------------

section() { printf '\n== %s ==\n' "$1"; }
pass()    { printf 'PASS: %s\n' "$1"; }
fail()    { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
info()    { printf 'INFO: %s\n' "$1"; }

find_repo_root() {
    local start="$1"
    while [ -n "$start" ]; do
        if [ -f "$start/main.py" ] && [ -f "$start/requirements.txt" ] && [ -d "$start/scripts" ]; then
            printf '%s\n' "$start"
            return 0
        fi
        local parent
        parent="$(dirname "$start")"
        [ "$parent" != "$start" ] || break
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
    [ -n "$candidate" ] && { printf '%s\n' "$candidate"; return 0; }
    candidate="$(find_repo_root "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || true)"
    [ -n "$candidate" ] && { printf '%s\n' "$candidate"; return 0; }
    return 1
}

load_dotenv_file() {
    local env_file="$1"
    local line trimmed key value
    [ -f "$env_file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        trimmed="${line#"${line%%[![:space:]]*}"}"
        [ -z "$trimmed" ] || [ "${trimmed:0:1}" = "#" ] && continue
        trimmed="${trimmed#export }"
        [[ "$trimmed" == *=* ]] || continue
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
        printf '\nREROUN COMPLETE\n'
    else
        printf '\nREROUN FAILED (exit %d)\n' "$exit_code" >&2
    fi
    exit "$exit_code"
}

trap finish EXIT

# ---------------------------------------------------------------------------
# Repo root + venv
# ---------------------------------------------------------------------------

section "Repo Root"
REPO_ROOT="$(resolve_repo_root)" || fail "Could not detect repository root."
cd "$REPO_ROOT"
pass "Repo root: $REPO_ROOT"

LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/rerun_$(date '+%Y-%m-%d_%H%M%S').log"
exec > >(tee -a "$LOG_FILE") 2>&1
info "Logging to $LOG_FILE"

section "Virtual Environment"
if [ -f "$REPO_ROOT/.venv/bin/activate" ]; then
    VENV_ACTIVATE="$REPO_ROOT/.venv/bin/activate"
elif [ -f "$REPO_ROOT/.venv/Scripts/activate" ]; then
    VENV_ACTIVATE="$REPO_ROOT/.venv/Scripts/activate"
else
    fail "Could not find .venv activation script under $REPO_ROOT/.venv"
fi
# shellcheck source=/dev/null
source "$VENV_ACTIVATE"
PYTHON_EXEC="$(python -c "import sys; print(sys.executable)")"
[[ "$PYTHON_EXEC" == *".venv"* ]] || fail "Active python is not from .venv: $PYTHON_EXEC"
pass "Activated .venv: $PYTHON_EXEC"

if [ -f "$REPO_ROOT/.env" ]; then
    load_dotenv_file "$REPO_ROOT/.env"
    info "Loaded .env"
fi

# ---------------------------------------------------------------------------
# Compute today's run_id
# ---------------------------------------------------------------------------

section "Run ID"
TODAY="$(date '+%Y-%m-%d')"
RUN_ID="${TODAY}_daily"
DB="$REPO_ROOT/data/portfolio.db"

printf 'Target run_id: %s\n' "$RUN_ID"
[ -f "$DB" ] || fail "Database not found: $DB"
pass "Database exists: $DB"

# ---------------------------------------------------------------------------
# Show current run_history row
# ---------------------------------------------------------------------------

section "Current run_history Row"
printf 'Querying: SELECT run_id, status, started_at, completed_at FROM run_history WHERE run_id = '"'"'%s'"'"'\n' "$RUN_ID"
printf '\n'

ROW="$(sqlite3 "$DB" \
    "SELECT run_id, status, started_at, completed_at FROM run_history WHERE run_id='${RUN_ID}';" \
    2>/dev/null || true)"

if [ -z "$ROW" ]; then
    info "No run_history row found for $RUN_ID — the pipeline has not run today yet."
    info "You do not need this script. Run: bash scripts/run_daily_safe.sh"
    exit 0
fi

printf '%s\n' "$ROW"
printf '\n'

# ---------------------------------------------------------------------------
# Confirmation gate
# ---------------------------------------------------------------------------

section "Confirmation Required"
printf 'This will reset run_id "%s" to status=failed so main.py will re-execute it.\n' "$RUN_ID"
printf 'The database, outputs/, and outputs/history/ will NOT be modified beyond that row.\n'
printf '\n'
printf 'Type exactly  rerun  to proceed, or anything else to abort: '
read -r CONFIRM

if [ "$CONFIRM" != "rerun" ]; then
    printf 'Aborted — no changes made.\n'
    exit 0
fi

# ---------------------------------------------------------------------------
# Reset run_history row (narrowly scoped)
# ---------------------------------------------------------------------------

section "Resetting run_history"
sqlite3 "$DB" \
    "UPDATE run_history SET status='failed', completed_at=NULL WHERE run_id='${RUN_ID}';"

UPDATED="$(sqlite3 "$DB" \
    "SELECT run_id, status, started_at, completed_at FROM run_history WHERE run_id='${RUN_ID}';")"
printf 'Row after update:\n%s\n' "$UPDATED"
pass "run_history row reset to status=failed"

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

section "Preflight"
"$REPO_ROOT/scripts/preflight.sh"
pass "Preflight passed"

# ---------------------------------------------------------------------------
# Daily pipeline
# ---------------------------------------------------------------------------

section "Daily Pipeline"
printf 'Running: python main.py --run-mode daily\n'
python main.py --run-mode daily

# ---------------------------------------------------------------------------
# Verify decision_plan.json
# ---------------------------------------------------------------------------

section "Output Verification"
DECISION_PLAN="$REPO_ROOT/outputs/latest/decision_plan.json"

[ -f "$DECISION_PLAN" ] || fail "outputs/latest/decision_plan.json not found after run"
pass "outputs/latest/decision_plan.json exists"

python - "$DECISION_PLAN" <<'PYEOF'
import json, sys

path = sys.argv[1]
with open(path) as f:
    raw = json.load(f)

# Resolve the list of decision rows regardless of top-level shape
if isinstance(raw, list):
    rows = raw
elif isinstance(raw, dict):
    for key in ("decisions", "rows", "results"):
        if isinstance(raw.get(key), list):
            rows = raw[key]
            break
    else:
        rows = [raw]
else:
    print(f"FAIL: unexpected top-level type in decision_plan.json: {type(raw)}", file=sys.stderr)
    sys.exit(1)

if not rows:
    print("FAIL: decision_plan.json contains no decision rows", file=sys.stderr)
    sys.exit(1)

first = rows[0]
required = ("decision_reason", "decision_reason_structured")
missing = [k for k in required if k not in first]
if missing:
    print(f"FAIL: first decision row is missing fields: {missing}", file=sys.stderr)
    sys.exit(1)

print(f"PASS: first decision row has decision_reason and decision_reason_structured")
print(f"INFO: {len(rows)} decision row(s) found")
PYEOF

pass "decision_plan.json structure verified"

section "Post-Run run_history"
FINAL="$(sqlite3 "$DB" \
    "SELECT run_id, status, started_at, completed_at FROM run_history WHERE run_id='${RUN_ID}';")"
printf '%s\n' "$FINAL"
