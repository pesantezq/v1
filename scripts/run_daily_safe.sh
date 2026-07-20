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

# Stage 00 — Run context (Phase 1): write the immutable run manifest
# (status=running) BEFORE any artifact is produced, so every artifact of this
# run is traceable to one coherent run_id and the is_complete / coherent_run_ids
# guards can reject incomplete or mixed-run inputs. A hard mid-run abort leaves
# the manifest at status=running, which is_complete() correctly rejects.
# Observe-only; never blocks the pipeline.
run_aux_stage "Run context (manifest begin)" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from datetime import datetime, timezone; from portfolio_automation.run_manifest import begin_run; m = begin_run('.', pipeline_mode='daily', started_at=datetime.now(timezone.utc).isoformat(), config_path='config.json'); print('run_id:', m['run_id'], 'commit:', m['source_commit'], 'cfg:', m['config_hash'][:8])"

# Stage 0 — News intelligence (run BEFORE the daily pipeline so it gets
# first claim on the FMP budget; one batched call populates the news cache
# for the rest of the run). Uses portfolio holdings + yesterday's watchlist
# artifact as the seed universe; degrades gracefully when artifacts are
# absent on first install.
run_aux_stage "News intelligence (pre-pipeline)" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.news.run_news_intelligence import run; s = run(root='.'); print('articles:', s.get('articles_fetched', 0), 'packets:', s.get('evidence_packet_count', 0))"

# Stage 0b — Schwab read-only broker sync (observe-only). MOVED here ahead of the
# decision run (operator-approved 2026-06-16) to fix the 24h-boundary holdings flap:
# main.py's §1a broker overlay reads schwab_positions.json /
# schwab_portfolio_snapshot.json via holdings_resolver, and those are written ONLY
# by schwab_sync. Running the sync first means the overlay consumes a same-run-fresh
# snapshot instead of yesterday's, so holdings_resolver's exactly-24h stale gate no
# longer flips broker-vs-config holdings near the cron boundary. READ-ONLY: no trade
# path exists (AST-enforced in brokers/). Fail-closed: absent SCHWAB_* creds / OAuth
# token it writes status=unconfigured and no-ops the rest. Wrapped non-blocking so a
# Schwab API / token failure degrades to error/AMBER and never aborts the pipeline —
# when it fails the prior snapshot stays on disk and the overlay falls back to config
# exactly as before. Still runs before Stage 11 so daily_run_status + the registry +
# the wiring probe count broker_sync_status fresh. Proposal stays operator-applied.
run_aux_stage "Schwab broker sync" \
    python -m portfolio_automation.brokers.schwab_sync --sync --reconcile

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

# Stage 7b2 — Scenario risk (Phase 11): deterministic stress illustrations
# (broad/nasdaq/semis/vol/rate/gold/liquidity) on the current weights — NOT
# forecasts. Reads risk_delta weights; ETF look-through is not fabricated.
# Observe-only.
run_aux_stage "Scenario risk" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.scenario_risk import build_scenario_risk; r = build_scenario_risk('.'); print('degraded:', r.get('degraded'), 'positions:', r.get('n_positions'), 'worst:', r.get('worst_case_scenario'))"

# Stage 7c — Retune impact tracker (gauge fingerprint vs baseline).
run_aux_stage "Retune impact tracker" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.retune_impact_tracker import run_retune_impact_tracker; r = run_retune_impact_tracker(root='.'); print('fingerprint:', r.get('fingerprint'), 'changes:', r.get('changes_count'), 'appended:', r.get('history_row_appended'))"

# Stage 7d — FMP / news budget telemetry (per-day call usage + news outcome).
run_aux_stage "FMP budget telemetry" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.fmp_budget_telemetry import run_fmp_budget_telemetry; r = run_fmp_budget_telemetry(root='.'); print('overall:', r.get('overall_status'), 'memo_line:', r.get('memo_line'))"

# Stage 7d2 — Data budget status (governor usage ledger -> 3 observe-only
# artifacts: fmp_usage_status / fmp_cache_status / data_budget_status). Non-blocking.
run_aux_stage "Data budget status" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.data_budget.run_status import run_data_budget_status; run_data_budget_status(root='.'); import json; b=json.load(open('outputs/latest/data_budget_status.json')); print('overall:', b.get('overall_status'), 'bw_pct:', b.get('monthly_bandwidth_pct'))"

# Stage 7d3 — Crowd intelligence (observe-only context for holdings). Non-blocking:
# run() swallows all errors and returns a status dict, so a failure WARNs and never
# blocks the run. Reads only AVAILABLE FMP endpoints via the governor; never feeds
# decision_plan / allocations / advisory selection.
run_aux_stage "Crowd intelligence" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.crowd_intelligence.artifact_writer import run; s=run('.'); print('overall:', s.get('overall_status'), 'symbols:', s.get('symbols_count'))"

# Stage 7e — Resolution-due probe: surface any signal whose 1d/3d/7d
# outcome window has elapsed but whose outcome_return_Nd is null.
run_aux_stage "Resolution-due probe" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.resolution_due_probe import run_resolution_due_probe; r = run_resolution_due_probe(root='.'); print('status:', r.get('status'), 'stuck:', r.get('stuck_count'), '/', r.get('total_signals'))"

# Stage 7f — Quant-watch probe ledger: auto-register sub-RED quant concerns,
# re-check open probes, auto-archive resolved ones. Consumes retune_impact.json
# (Stage 7c above) + pattern_efficacy_monthly.json, so it runs after the impact
# tracker. Wired here deterministically so the ledger refreshes every cron run
# rather than depending on the /quant-watch-analysis LLM skill being invoked.
# run_quant_watch never raises (degrades to an empty-but-valid status); the
# created_run tag marks cron-sourced registrations distinctly from skill runs.
run_aux_stage "Quant-watch probe ledger" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.quant_watch_probes import run_quant_watch; r = run_quant_watch(root='.', created_run='run_daily_safe'); print('overall:', r.get('overall_status'), 'active:', r.get('active_count'), 'registered:', len(r.get('registered_today') or []), 'escalated:', len(r.get('escalated_today') or []))"

# Stage 7g — Daily input snapshot (Phase 2): freeze ONE point-in-time view of
# every decision-time input (references + content hashes, not copies) so the
# production decision and every daily simulation evaluate the SAME data and no
# sim can read later information. Runs AFTER the decision pipeline + advisors so
# the production baseline + holdings/risk/crowd inputs all exist; the snapshot
# (+ its snapshot_hash) is what Phase 3 sims will read. Future-dated inputs are
# rejected. Inherits run_id/data_as_of from the run manifest. Observe-only.
run_aux_stage "Daily input snapshot" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.daily_input_snapshot import run_daily_input_snapshot; s = run_daily_input_snapshot('.'); print('run_id:', s.get('run_id'), 'hash:', (s.get('snapshot_hash') or '')[:12], 'valid:', s.get('valid_count'), 'stale:', s.get('stale_count'), 'missing:', s.get('missing_count'), 'future_rejected:', s.get('future_rejected_count'))"

# Stage 7h — Decision-time context capture (Phase 4): record each production
# decision's IMMUTABLE at-decision context (regime/crowd/factor/confidence/
# data-quality + horizons + the frozen snapshot hash) to an append-only log, so
# later outcome maturation attributes results to the conditions that produced
# them. Observe-only; never mutates the protected stored win-rate. Runs after
# the snapshot (7g) so it binds to the frozen input identity.
run_aux_stage "Decision-time context capture" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.decision_context_capture import run_decision_context_capture; r = run_decision_context_capture('.'); print('run_id:', r.get('run_id'), 'captured:', r.get('captured'), 'snapshot:', (r.get('snapshot_hash') or '')[:12])"

# Stage 7i — Quant feedback attribution (Phase 5): join the decision-time
# context log (7h) with matured outcomes and attribute performance by regime /
# crowd-state / strategy / action using the standardized taxonomy + honest
# denominators + sample sufficiency. Evidence only (never changes confidence/
# weights/production). Insufficient evidence is reported distinctly.
run_aux_stage "Quant feedback attribution" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.quant_feedback import run_quant_feedback; r = run_quant_feedback('.'); print('evidence:', r.get('evidence_status'), 'ctx:', r.get('n_context_records'), 'resolved:', r.get('n_resolved_outcomes'), 'fallback_rate:', r.get('fallback_rate'))"

# Stage 8 — News intelligence refresh (re-run now that the decision plan
# and watchlist have landed; cached calls cost no budget so this is cheap
# and broadens the captured universe).
run_aux_stage "News intelligence (post-pipeline refresh)" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.news.run_news_intelligence import run; s = run(root='.'); print('articles:', s.get('articles_fetched', 0), 'packets:', s.get('evidence_packet_count', 0))"

# Stage 8a — Market narratives (observe-only synthesis of news intelligence +
# decision artifacts into daily/weekly/monthly narrative summaries). Runs after
# Stage 8 (consumes news_intelligence.json) and BEFORE the news-evidence layer,
# promotion governance (Stage 9), and the memo (Stage 10), all of which consume
# its market_narrative_*.json output. Pure read of local artifacts; non-blocking.
run_aux_stage "Market narratives" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.market_narratives import run_market_narratives; r = run_market_narratives(base_dir='outputs'); d = r.get('daily') or {}; print('themes:', d.get('themes_found', 0), 'risks:', d.get('risks_found', 0), 'catalysts:', d.get('catalysts_found', 0))"

# Stage 8a2 — News evidence layer (observe-only evidence bundle keyed to the
# decision plan; consumes news_intelligence.json + market_narrative_*.json, so
# it runs right after Stage 8a). Consumed downstream by promotion governance
# (Stage 9) and the daily memo's memo_enrichment (Stage 10). Non-blocking;
# writes only its own observe-only artifacts.
run_aux_stage "News evidence layer" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.news_evidence_layer import run_news_evidence_layer; r = run_news_evidence_layer(base_dir='outputs'); print('data_available:', r.get('data_available'), 'ticker_ctx:', r.get('ticker_context_count', 0), 'decision_ctx:', r.get('decision_context_count', 0))"

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

# Stage 9c — Crowd Radar / Public Knowledge Velocity Layer (sandbox research lane).
# Observe-only, sandbox-only, DEFAULT-DISABLED (config.json crowd_radar.enabled=false).
# Classifies the state of public knowledge around tickers from API-compliant public
# discussion (Reddit-first). Runs in discovery run-mode so it MAY write
# outputs/sandbox/discovery/, but it can NEVER write the official decision plan.
# Fail-safe: missing REDDIT_* creds / disabled flag / kill-switch write a degraded
# artifact and no-op the network. Runs before the memo (Stage 10) so the memo's
# Crowd Radar section reads the fresh artifact. Non-blocking; never aborts the run.
run_aux_stage "Crowd Radar (public knowledge velocity)" \
    python -m portfolio_automation.social_intelligence.public_knowledge_velocity --root "${REPO_ROOT}" --run-mode discovery

# Stage 9c1 — Multi-source Crowd Radar (no-extra-cost, API-first, observe-only).
# Runs the dev-doc-audited source connectors (ApeWisdom active; FMP/Finnhub
# entitlement probes; Stocktwits/Quiver blocked by no-extra-cost policy), the
# multi-source aggregator, and writes crowd_source_dev_doc_audit / crowd_source_health
# / crowd_multi_source_velocity + summary under outputs/sandbox/discovery/. Probes
# only hit the network when a source is configured with credentials. Runs BEFORE
# the activation check (9c2) so the health/entitlement artifact is fresh for it.
# Non-blocking; never aborts the run.
run_aux_stage "Crowd Radar multi-source" \
    python -m portfolio_automation.social_sources.run_multi_source_crowd --root "${REPO_ROOT}" --run-mode discovery

# Stage 9c2 — Crowd Radar activation checklist (observe-only readiness probe).
# Pure (no network): reports whether Crowd Radar is safe + ready to collect
# (flag, creds, source-terms, rate-limit, storage/AI policy, sandbox + decision
# invariants, last smoke test). Writes outputs/sandbox/discovery/
# crowd_radar_activation_check.json. Non-blocking; never aborts the run.
run_aux_stage "Crowd Radar activation check" \
    python -m portfolio_automation.social_intelligence.activation_check --root "${REPO_ROOT}" --run-mode discovery

# Stage 9c3 — Unified Crowd Intelligence Bus (observe-only; joins Lane A + Lane B).
# Runs AFTER both crowd lanes have written: Lane B = crowd_intelligence
# (Stage 7d3, artifact_writer.run) + Lane A = multi-source crowd (Stage 9c1,
# run_multi_source_crowd). Joins ApeWisdom retail attention with FMP institutional
# context into a single per-ticker view (outputs/latest/unified_crowd_intelligence
# .json + _status.json + .md). run() is non-blocking: it swallows all errors and
# returns a status dict, so a failure WARNs and never blocks the run. Never feeds
# decision_plan / allocations / advisory selection.
run_aux_stage "Unified Crowd Intelligence Bus" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.crowd_intelligence.unified_writer import run; s=run('.'); print('unified_crowd:', s.get('overall_status'), 'tickers:', s.get('total_tickers'))"

# Stage 9c4 — Social Sentiment Pipeline (free text connectors + FinBERT scoring).
# Reads the top-N tickers from crowd_multi_source_velocity.json (Stage 9c1), fetches
# text posts from Mastodon/Lemmy/Bluesky, scores with FinBERT, aggregates cross-source,
# and writes social_sentiment_status.json + social_sentiment_simulation_adjustment.json
# under outputs/sandbox/discovery/. Simulation-active / production-gated: adjustments
# are sandbox-scoped and never touch decision_plan.json. Non-blocking; never aborts the run.
run_aux_stage "Social Sentiment Pipeline" \
    python -m portfolio_automation.social_sentiment.run_sentiment_pipeline --root "${REPO_ROOT}" --run-mode discovery

# Stage 9e — Memo decision-coherence reconciliation (observe-only, advisory).
# Reads the decision/portfolio/risk/crowd artifacts and writes memo_coherence.json
# (funded vs unfunded, reconciled posture, contradictions). Runs before Stage 10 so
# the memo's investor core can consume it. Non-blocking; never feeds decision_plan.
run_aux_stage "Memo coherence reconciliation" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.memo_coherence import run_memo_coherence; r = run_memo_coherence('.'); print('status:', r.get('coherence_status'), 'funded:', (r.get('funding') or {}).get('funded_count'), 'deferred:', (r.get('funding') or {}).get('blocked_count'), 'unresolved:', (r.get('reconciliation') or {}).get('unresolved_count'))"

# Stage 9e2 — Today's Capital Plan view model (observe-only, read-only).
# Normalizes the coherence funding split + cash envelope + decision-plan sell
# detail into outputs/latest/daily_capital_plan.json (the audit copy of the
# decision-ready memo block). The memo (Stage 10) renders the same view; this
# stage persists it. Runs after coherence, before the memo.
run_aux_stage "Today's Capital Plan view" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.capital_plan_view import run_capital_plan_view; v = run_capital_plan_view('.'); cs = v.get('capital_summary') or {}; print('available:', v.get('available'), 'funded:', cs.get('funded_count'), 'deferred:', cs.get('deferred_count'), 'recon:', v.get('reconciliation_status'), 'warnings:', len(v.get('funding_warnings') or []))"

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

# Stage 10b2 — Simulation Charts (observe-only). Pure read+aggregate of the existing
# sandbox simulation artifacts (strategy_comparison.json [daily], portfolio_backtest.json
# + portfolio_projection.json [weekly]) into a single normalized, human-readable
# outputs/latest/simulation_charts.json that the Strategy Lab dashboard renders as
# plain-English charts. Runs AFTER Stage 10b (which writes strategy_comparison.json) and
# before Stage 11 so daily_run_status + the registry count it fresh. No network/LLM; never
# writes decision_plan.json; never trades. Charts without source data degrade honestly.
run_aux_stage "Simulation charts" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.simulation_charts import run_simulation_charts; r = run_simulation_charts('.'); print('sources present:', r.get('source_files_present'))"

# Stage 10c — Schwab read-only broker sync MOVED to Stage 0b (above, ahead of the
# decision run) on 2026-06-16 to fix the 24h-boundary holdings flap. See Stage 0b.

# Stage 10d — Schwab re-auth email heads-up (observe-only). Reads the
# broker_sync_status.json written by Stage 0b (Schwab sync, now pre-pipeline);
# when the 7-day refresh token
# is due_soon/expired it sends ONE email per expiry window via the shared
# memo_email_sender SMTP transport. Default-INERT (SCHWAB_REAUTH_EMAIL_ENABLED=0) —
# no-ops silently until the operator opts in. Non-blocking; never aborts the run.
run_aux_stage "Schwab re-auth notifier" \
    python -m portfolio_automation.brokers.schwab_reauth_notifier --send

# Stage 10e — Simulation-governance daily lane. Runs AFTER the production
# baseline artifacts (decision_plan, watchlist, discovery, crowd) already exist:
# active simulation lane → daily simulation bundle → consolidated AI/product
# review packet → ONE gated AI review (<= $0.50/day, else deferred) → pending
# production proposals → apply already human-approved proposals to the production
# overlays. Simulation lane is ACTIVE (may change SANDBOX/SIMULATION outputs);
# production overlays are human-gated and default-OFF. Every step non-blocking;
# never writes decision_plan or scoring. Runs before Stage 11 so daily_run_status
# + the registry + the wiring probe count its artifacts fresh.
run_aux_stage "Simulation-governance daily lane" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.sim_governance.daily_governance_run import run_daily_governance; r = run_daily_governance('.'); print('enabled:', r.get('enabled'), 'stages:', list(r.get('stages', {}).keys()), 'pending:', r.get('pending_proposal_count'), 'approved:', r.get('approved_proposal_count'))"

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

# Stage 13 — Pipeline wiring probe (root-cause layer over the registry). Runs
# AFTER registry governance so it sees the full fresh corpus. Crosses artifact
# freshness with static caller-grep to explain WHY any producer is stale
# (unwired / cadence_mismatch / silently_skipped) rather than just flagging the
# symptom. Observe-only, AMBER-max, never blocks the decision core.
run_aux_stage "Pipeline wiring probe" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.pipeline_wiring_probe import run_pipeline_wiring_probe; r = run_pipeline_wiring_probe(root='.'); s = r.get('summary') or {}; print('overall:', r.get('overall_status'), 'audited:', s.get('total_audited'), 'unwired:', s.get('unwired'), 'mismatch:', s.get('cadence_mismatch'), 'skipped:', s.get('silently_skipped'), 'empty:', s.get('fresh_but_empty'))"

# Stage 13b — Semantic-liveness meta-monitor (Phase 6): detect degenerate
# (constant/default/zero-variance/class-disappeared) outputs with min-sample +
# documented-exception guards, so a technically-green-but-broken pipeline can't
# stay silent. Observe-only, AMBER-max; sub-RED findings route to quant-watch.
run_aux_stage "Semantic-liveness probes" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.semantic_liveness import run_semantic_liveness; r = run_semantic_liveness('.'); print('status:', r.get('overall_status'), 'findings:', r.get('finding_count'))"

# Stage 14 — Run context (Phase 1): stamp the manifest complete. Runs LAST so
# completion means every prior stage finished; is_complete() flips True only
# here, so a consumer reading outputs/policy/run_manifest.json knows the run is
# whole (a run that aborts earlier stays status=running -> not complete).
# Observe-only.
run_aux_stage "Run context (manifest complete)" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from datetime import datetime, timezone; from portfolio_automation.run_manifest import complete_run; m = complete_run('.', completed_at=datetime.now(timezone.utc).isoformat(), status='complete'); print('run_id:', m.get('run_id'), 'status:', m.get('status'))"
