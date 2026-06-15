#!/usr/bin/env bash
set -euo pipefail

section() {
    printf '\n== %s ==\n' "$1"
}

pass() {
    printf 'PASS: %s\n' "$1"
}

fail() {
    printf 'FAIL: %s\n' "$1" >&2
    exit 1
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

REPO_ROOT="$(resolve_repo_root)" || fail "Could not detect repository root from the current directory or script location."
cd "$REPO_ROOT"

section "Repo Root"
printf 'Repo root: %s\n' "$REPO_ROOT"
pass "Repository root detected"

section "Virtual Environment"
[ -d "$REPO_ROOT/.venv" ] || fail ".venv directory is missing at $REPO_ROOT/.venv"

if [ -f "$REPO_ROOT/.venv/bin/activate" ]; then
    VENV_ACTIVATE="$REPO_ROOT/.venv/bin/activate"
elif [ -f "$REPO_ROOT/.venv/Scripts/activate" ]; then
    VENV_ACTIVATE="$REPO_ROOT/.venv/Scripts/activate"
else
    fail "Could not find a bash-compatible activation script under .venv"
fi

# shellcheck source=/dev/null
source "$VENV_ACTIVATE"
pass "Activated virtual environment via $VENV_ACTIVATE"

PYTHON_EXEC="$(python -c "import sys; print(sys.executable)")"
PYTHON_VERSION="$(python -c "import sys; print(sys.version.splitlines()[0])")"

if [[ "$PYTHON_EXEC" != *".venv"* ]]; then
    fail "Active python is not from .venv: $PYTHON_EXEC"
fi

printf 'Python executable: %s\n' "$PYTHON_EXEC"
printf 'Python version: %s\n' "$PYTHON_VERSION"
pass "Using virtualenv python: $PYTHON_EXEC"

section "Required Files"
required_files=(
    "fmp_endpoint_registry.py"
    "fmp_endpoint_compliance.py"
    "requirements.txt"
    "main.py"
)
for required_file in "${required_files[@]}"; do
    [ -f "$required_file" ] || fail "Missing required file: $required_file"
done
pass "Required files exist"

section "Environment"
if [ -f "$REPO_ROOT/.env" ]; then
    load_dotenv_file "$REPO_ROOT/.env"
    pass "Loaded environment variables from .env"
else
    printf 'INFO: .env not found; relying on process environment only.\n'
fi

[ -n "${FMP_API_KEY:-}" ] || fail "FMP_API_KEY is missing from both the environment and .env"
pass "FMP_API_KEY is available"

section "Env Var Registry"
# portfolio_automation.env.check_state — declared env-var inventory + redaction.
# --strict exits non-zero when any registered required var is missing.
# Secret values are never printed. Existing call sites continue to read
# os.environ directly; this is the new validation surface only.
if python -m portfolio_automation.env --check --strict; then
    pass "All registered required env vars are set"
else
    fail "One or more registered required env vars are missing (see above)"
fi

section "Artifact Shape Smoke Test"
# tools.smoke_test — registry-driven, read-only shape validation of every
# artifact in portfolio_automation.artifacts_registry. --strict exits 1 when
# any required artifact is missing or malformed (invalid JSON/JSONL, missing
# observe_only flag, empty markdown/text, etc.). Optional and append-only
# artifacts that are absent are reported as INFO and never fail preflight.
# Read-only; never writes. Mirrors the env-registry strict gate above.
if python -m tools.smoke_test --strict; then
    pass "All registered required artifacts have the expected shape"
else
    fail "One or more registered required artifacts are missing or malformed (see above)"
fi

section "FMP Compliance"
compliance_output="$(mktemp)"
pytest_output=""
trap 'rm -f "${compliance_output:-}" "${pytest_output:-}"' EXIT
if python -m fmp_endpoint_compliance | tee "$compliance_output"; then
    if grep -q "RESULT: COMPLIANT" "$compliance_output"; then
        pass "FMP endpoint compliance reports RESULT: COMPLIANT"
    else
        fail "FMP endpoint compliance did not emit RESULT: COMPLIANT"
    fi
else
    fail "python -m fmp_endpoint_compliance failed"
fi

section "FMP Tests"
pytest_output="$(mktemp)"
# GUI test exclusions removed 2026-05-28 after datetime tz fix in
# gui_operator_data.py — these tests collect cleanly now.
if python -m pytest tests/ -k fmp -v \
    | tee "$pytest_output"; then
    pass "FMP-focused pytest suite passed"
else
    fail "python -m pytest tests/ -k fmp -v failed"
fi

section "Compile Check"
python -m py_compile \
    main.py fmp_client.py fmp_endpoint_registry.py fmp_endpoint_compliance.py \
    portfolio_automation/risk_delta_advisor.py \
    portfolio_automation/retune_impact_tracker.py \
    portfolio_automation/fmp_budget_telemetry.py \
    portfolio_automation/daily_run_status.py \
    portfolio_automation/resolution_due_probe.py \
    portfolio_automation/news/run_news_intelligence.py \
    portfolio_automation/social_intelligence/public_knowledge_velocity.py \
    portfolio_automation/social_intelligence/crowd_state_classifier.py \
    portfolio_automation/social_intelligence/ticker_extractor.py \
    portfolio_automation/social_intelligence/context_join.py \
    portfolio_automation/social_intelligence/activation_check.py \
    portfolio_automation/social_intelligence/multi_source_crowd_aggregator.py \
    portfolio_automation/social_sources/apewisdom_connector.py \
    portfolio_automation/social_sources/fmp_social_sentiment_connector.py \
    portfolio_automation/social_sources/finnhub_social_probe.py \
    portfolio_automation/social_sources/stocktwits_probe.py \
    portfolio_automation/social_sources/quiver_probe.py \
    portfolio_automation/social_sources/source_health.py \
    portfolio_automation/social_sources/dev_doc_audit.py \
    portfolio_automation/social_sources/run_multi_source_crowd.py \
    portfolio_automation/portfolio_sim/run_portfolio_backtest.py \
    portfolio_automation/portfolio_sim/backtest_engine.py \
    portfolio_automation/portfolio_sim/tactics.py \
    portfolio_automation/portfolio_sim/run_strategy_lab.py \
    portfolio_automation/portfolio_sim/research_library.py \
    portfolio_automation/portfolio_sim/strategy_lab_health.py
pass "Targeted py_compile check passed"

section "Wrapper Syntax Check"
bash -n "${REPO_ROOT}/scripts/run_daily_safe.sh"
pass "scripts/run_daily_safe.sh parses cleanly"

section "Advisor Smoke Imports"
# Import-check the four new observability modules so a typo can't slip
# past the wrapper's non-blocking stages and silently break in cron.
python -c "
import importlib
modules = [
    'portfolio_automation.risk_delta_advisor',
    'portfolio_automation.retune_impact_tracker',
    'portfolio_automation.fmp_budget_telemetry',
    'portfolio_automation.daily_run_status',
    'portfolio_automation.resolution_due_probe',
    'portfolio_automation.news.run_news_intelligence',
    'portfolio_automation.social_intelligence.public_knowledge_velocity',
    'portfolio_automation.social_intelligence.activation_check',
    'portfolio_automation.social_sources.run_multi_source_crowd',
    'portfolio_automation.portfolio_sim.run_portfolio_backtest',
    'portfolio_automation.portfolio_sim.run_portfolio_projection',
    'portfolio_automation.portfolio_sim.run_strategy_lab',
]
for m in modules:
    importlib.import_module(m)
print('imported:', len(modules), 'observability modules')
"
pass "Observability advisor imports clean"

section "Summary"
pass "Preflight completed successfully"
