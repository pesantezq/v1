---
name: portfolio-automation-guardrails
description: Use for any code, test, or documentation work in this repo. Enforces advisory-only architecture, decision-plan truth, additive contracts, compact memo/GUI rules, and validation discipline.
---

# Portfolio Automation Guardrails

Use this skill for work in this repository.

## Read First
- `docs/ARCHITECTURE.md`
- `docs/decision_engine.md`
- `docs/gui_decision_center.md`
- `docs/daily_memo.md`

## Hard Rules
- The system is advisory-only. No trade execution, broker integration, or auto-trading behavior.
- `outputs/latest/decision_plan.json` is the decision source of truth.
- GUI, memo, and explanation layers must read artifacts only; no decision recomputation there.
- All changes must be additive and backward compatible unless the user explicitly approves a breaking change.
- Do not change scoring, ranking, recommendation, or decision semantics without explicit user approval.

## Protected Areas
- `portfolio_automation/decision_engine.py`
- `scoring.py`
- `recommendations.py`
- contract consumers such as `watchlist_scanner/daily_memo.py`, `gui_operator_data.py`, and `gui/app.py`

For protected consumers:
- preserve compact contracts
- preserve read-only behavior
- preserve existing artifact schemas

## Compact Contract
- Memo summary:
  - max 5 decisions
  - max 3 risk items
  - max 3 changes
- GUI Decision Center summary:
  - same compact limits
  - full detail remains below in tables / expanders

## Workflow
1. Trace the exact source-to-output path.
2. Name the exact files and functions involved.
3. Prefer the smallest diff-friendly patch.
4. Preserve explainability, degraded-mode behavior, and artifact stability.
5. Validate with targeted tests and compile checks.

## Validation
- `python -m py_compile <files>`
- `pytest -q`
- For FMP or production-run work also use:
  - `bash scripts/preflight.sh`
  - `python -m fmp_endpoint_compliance`

## Reference Docs
- `docs/OUTPUT_ARTIFACT_CONTRACTS.md`
- `docs/PIPELINE_RUNBOOK.md`
- `docs/REGRESSION_CHECKLIST.md`
- `docs/CLAUDE_AGENT_RULES.md`
