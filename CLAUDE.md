# Claude Code Instructions

This repo is an advisory-only portfolio automation system. It produces analysis, recommendations, and operator artifacts; it does not execute trades.

## Read First
- `docs/ARCHITECTURE.md`
- `docs/decision_engine.md`
- `docs/gui_decision_center.md`
- `docs/daily_memo.md`

## Hard Boundaries
- `outputs/latest/decision_plan.json` is the decision source of truth.
- GUI, memo, and explanation layers are artifact consumers only.
- Do not recompute decisions outside core decision layers.
- Do not introduce broker integration, execution logic, or auto-trading behavior.
- Keep all new features additive and backward compatible.

## Protected Semantics
- Do not change `signal_score`, `confidence_score`, `effective_score`, `conviction_score`, `final_rank_score`, or `recommendation_score` semantics without explicit user approval.
- Do not modify `decision_engine.py`, scoring logic, or recommendation logic unless the user explicitly approves that scope.
- Do not bypass FMP registry/compliance rules for endpoint work.

## Working Style
- Trace the exact source-to-artifact path before editing.
- Name exact files and functions before changing behavior.
- Prefer the smallest patch that preserves explainability and contracts.
- Lower certainty when data is stale or degraded; do not invent conviction.

## Output Contracts
- Decision artifacts: `outputs/latest/decision_plan.json`, `outputs/latest/decision_plan.md`
- Memo: compact brief only
  - max 5 decisions
  - max 3 risk items
  - max 3 changes
- GUI Decision Center: same compact contract, full detail below

## Validation
- Targeted tests first, then broader suites when scope expands.
- Compile touched Python files:
  - `python -m py_compile <files>`
- Run repo tests:
  - `pytest -q`
- For production-run changes, respect:
  - `bash scripts/preflight.sh`
  - `bash scripts/run_daily_safe.sh`

## Reference Docs
- `docs/OUTPUT_ARTIFACT_CONTRACTS.md`
- `docs/PIPELINE_RUNBOOK.md`
- `docs/REGRESSION_CHECKLIST.md`
- `docs/CLAUDE_AGENT_RULES.md`
