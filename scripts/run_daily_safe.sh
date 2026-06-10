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

# Helper for non-blocking advisory stages. Sandbox + read-only writes;
# failures here must not abort the chain because either (a) the official
# decision plan has already landed, or (b) the stage is a pre-pipeline
# observability layer that should never block the main pipeline.
run_aux_stage() {
    local label="$1"; shift
    section "$label"
    if "$@"; then
        printf '%s: OK\n' "$label"
    else
        printf '%s: WARN (non-blocking; exit %d)\n' "$label" "$?" >&2
    fi
}

# Stage 0 — News intelligence (run BEFORE the daily pipeline so it gets
# first claim on the FMP budget; one batched call populates the news cache
# for the rest of the run). Uses portfolio holdings + yesterday's watchlist
# artifact as the seed universe; degrades gracefully when artifacts are
# absent on first install.
run_aux_stage "News intelligence (pre-pipeline)" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.news.run_news_intelligence import run; s = run(root='.'); print('articles:', s.get('articles_fetched', 0), 'packets:', s.get('evidence_packet_count', 0))"

section "Daily Pipeline"
run_cmd=(python main.py --run-mode daily)
if [ "${DRY_RUN_MODE:-0}" = "1" ]; then
    run_cmd+=(--dry-run)
    printf 'DRY_RUN_MODE=1 detected. Running advisory daily pipeline in --dry-run mode.\n'
fi

printf 'Command: %s\n' "${run_cmd[*]}"
"${run_cmd[@]}"

# Stage 2 — Weight tuning report (performance review of signal weights).
run_aux_stage "Weight tuning" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from pathlib import Path; from watchlist_scanner.weight_tuning import generate_weight_tuning_report; r = generate_weight_tuning_report(db_path=Path('data/portfolio.db'), output_dir=Path('outputs/performance')); print('recommended:', (r.get('suggestions') or {}).get('recommended_candidate') or 'current')"

# Stage 3 — Policy evaluator (historical decision policy evaluation).
run_aux_stage "Policy evaluator" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from policy_evaluator.evaluator import evaluate_history; from policy_evaluator.report_writer import write_evaluation_reports; r = evaluate_history(history_path=None); write_evaluation_reports(r, policy_dir=None); print('records:', getattr(r, 'total_records', 0), 'runs:', getattr(r, 'total_runs', 0))"

# Stage 4 — Allocation preview (writes outputs/latest/allocation_preview.json).
run_aux_stage "Allocation preview" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from pathlib import Path; from watchlist_scanner.allocation_preview import generate_allocation_preview_report; p = generate_allocation_preview_report(root=Path('.')); print('candidates:', int(p.get('candidate_count') or len(p.get('opportunities') or [])))"

# Stage 5 — Allocation policy simulation (rank-aware policy efficiency).
run_aux_stage "Allocation policy simulation" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from pathlib import Path; from watchlist_scanner.allocation_policy_simulation import generate_allocation_policy_simulation_report; s = generate_allocation_policy_simulation_report(root=Path('.')); print('sample:', s.get('sample_size', 0))"

# Stage 6 — Allocation policy activation (writes approved_*.json gate artifacts).
run_aux_stage "Allocation policy activation" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from pathlib import Path; from watchlist_scanner.allocation_policy_activation import run_activation_check; r = run_activation_check(root=Path('.'), approve=False); print('all_rules_passed:', r.get('all_rules_passed', False))"

# Stage 7 — System decision summary (writes outputs/latest/system_decision_summary.json).
# Memo reads its generated_at from this file, so this must run before Stage 10.
run_aux_stage "System decision summary" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from pathlib import Path; from watchlist_scanner.system_summary import generate_system_decision_summary; s = generate_system_decision_summary(root=Path('.'), write_files=True); print('top_theme:', (s.get('top_theme') or {}).get('name') or '-', 'top_opp:', (s.get('top_opportunity') or {}).get('ticker') or '-')"

# Stage 7b — Risk delta panel (concentration / leverage / VaR vs caps).
# Runs after system_summary so portfolio_value and benchmark sigma are fresh.
run_aux_stage "Risk delta panel" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.risk_delta_advisor import run_risk_delta_advisor; r = run_risk_delta_advisor(root='.'); print('status:', r.get('status'), 'overall:', r.get('overall_status'), 'top_pos:', (r.get('concentration_top') or {}).get('symbol'), 'lev:', r.get('leverage_exposure'), 'var_pct:', r.get('var_pct'))"

# Stage 7c — Retune impact tracker (gauge fingerprint vs baseline).
run_aux_stage "Retune impact tracker" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.retune_impact_tracker import run_retune_impact_tracker; r = run_retune_impact_tracker(root='.'); print('fingerprint:', r.get('fingerprint'), 'changes:', r.get('changes_count'), 'appended:', r.get('history_row_appended'))"

# Stage 7d — FMP / news budget telemetry (per-day call usage + news outcome).
run_aux_stage "FMP budget telemetry" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.fmp_budget_telemetry import run_fmp_budget_telemetry; r = run_fmp_budget_telemetry(root='.'); print('overall:', r.get('overall_status'), 'memo_line:', r.get('memo_line'))"

# Stage 7e — Resolution-due probe: surface any signal whose 1d/3d/7d
# outcome window has elapsed but whose outcome_return_Nd is null.
run_aux_stage "Resolution-due probe" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.resolution_due_probe import run_resolution_due_probe; r = run_resolution_due_probe(root='.'); print('status:', r.get('status'), 'stuck:', r.get('stuck_count'), '/', r.get('total_signals'))"

# Stage 8 — News intelligence refresh (re-run now that the decision plan
# and watchlist have landed; cached calls cost no budget so this is cheap
# and broadens the captured universe).
run_aux_stage "News intelligence (post-pipeline refresh)" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.news.run_news_intelligence import run; s = run(root='.'); print('articles:', s.get('articles_fetched', 0), 'packets:', s.get('evidence_packet_count', 0))"

# Stage 8b — Discovery news integration (sandbox research lane).
run_aux_stage "Discovery news integration" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.discovery.news_integration import run_discovery_news_integration; print(run_discovery_news_integration(run_mode='discovery'))"

# Stage 9 — Automatic promotion governance (sandbox research lane).
run_aux_stage "Automatic promotion governance" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.discovery.automatic_promotion_governance import run_automatic_promotion_governance; print(run_automatic_promotion_governance(run_mode='discovery', write_files=True))"

# Stage 9b — Sandbox lane status (writes outputs/sandbox/discovery/
# sandbox_run_status.json + .md). The two underlying discovery steps run
# again here via tools.daily_sandbox_run, but they hit the cache from
# Stages 8b/9 so the cost is negligible. The point is to refresh the
# sandbox lane's own run-status artifact so operators can see it ran.
run_aux_stage "Sandbox lane status" \
    python -m tools.daily_sandbox_run

# Stage 10 — Daily investment memo (also triggers email if MEMO_EMAIL_ENABLED=1).
run_aux_stage "Daily memo + email" \
    python -c "import os; os.chdir('${REPO_ROOT}'); import runpy; runpy.run_module('watchlist_scanner.daily_memo', run_name='__main__')"

# Stage 10b — Next-stage research/strategy lane (Phases 1-15). Standalone
# observe-only orchestrator: system-improvement → universe scan + radar →
# shadow tracking → market-opportunity prompts → strategy comparison →
# approval queues → broker-aware side-panel. Pure (no LLM/FMP/network; reads
# local artifacts only), every producer non-fatal, never writes decision_plan.
# Runs before Stages 11-12 so daily_run_status + the registry validator count
# its artifacts as freshly present rather than missing.
run_aux_stage "Next-stage research/strategy lane" \
    python -m portfolio_automation.next_stage.run_next_stage --root "${REPO_ROOT}"

# Stage 11 — Daily run status (reads its own log; runs last so it captures
# all preceding stages). Provides operator-glanceable ok/partial/failed.
run_aux_stage "Daily run status" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.daily_run_status import run_daily_run_status; r = run_daily_run_status(root='.'); print('overall:', r.get('overall_status'), 'missing_required:', r.get('required_missing_count'))"

# Stage 12 — Artifact registry governance (corpus-integrity gate). Runs LAST,
# after every other stage has written its artifact, so its presence/staleness
# scan sees the fresh corpus (including daily_run_status above). Observe-only:
# reads the registry + artifact mtimes, writes only its own status artifact.
# This is the governance gate /daily-tool-analysis reads first to gate
# confidence in everything below it.
run_aux_stage "Artifact registry governance" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.artifact_registry import run_artifact_registry; r = run_artifact_registry(root='.'); c = r.get('counts') or {}; print('overall:', r.get('overall_status'), 'present:', c.get('present'), '/', c.get('total'), 'missing:', c.get('missing'), '(required', str(c.get('missing_required')) + ')', 'stale:', c.get('stale'), 'debt:', c.get('unjustified_debt'))"
