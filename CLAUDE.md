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

---

## Agent Orchestration Context

This repo uses a repo-native orchestration layer in `.agent/`. Read these before starting any feature:

```bash
python scripts/agent_context_check.py   # prints current phase, step, next steps
cat .agent/project_state.yaml           # full machine-readable project state
cat .agent/phase_status.yaml            # per-step roadmap status
```

## Roadmap Discipline

- Implement only the step explicitly requested by the user.
- Do not recommend Discovery Engine as the next step if a named roadmap step (e.g., Confidence Calibration, GUI panels) is still pending.
- The authoritative next step is `next_official_step` in `.agent/project_state.yaml`.
- If you are unsure whether a step is in scope, ask before implementing.

## Observe-Only Default

- All new observability layers must set `observe_only: true` as a hardcoded field in output artifacts.
- Do not remove or make `observe_only` conditional unless explicitly approved.
- Non-blocking pipeline integration: wrap all new calls in `try/except`.

## Output Namespace Rules

- Use `OutputNamespace` from `portfolio_automation/data_governance.py` for all file writes.
- Live pipeline artifacts → `OutputNamespace.LATEST` (`outputs/latest/`)
- Budget/governance/audit artifacts → `OutputNamespace.POLICY` (`outputs/policy/`)
- Replay artifacts → `OutputNamespace.HISTORICAL` (`outputs/backtest/`) — never from live pipeline
- Never write to namespaces outside the module's declared purpose.

## VPS Warning

Claude runs locally. Claude does not have access to the production VPS.

- Do not claim tests passed on VPS — they ran locally only.
- Return VPS validation commands for the user to run manually.
- Format VPS commands clearly as a copyable block at the end of the final report.
- Use `.agent/task_templates/vps_validation_prompt.md` as the template for VPS commands.

## Test Requirements

- Add tests for every new module in `tests/`.
- Run targeted tests before the full suite.
- Full suite ignores known GUI health tests:
  ```
  python -m pytest -q --ignore=tests/test_gui_api_health.py --ignore=tests/test_gui_insight_cards.py
  ```

## Final Report Format

End every implementation task with this report:

```
## Final Report

Files created: [list]
Files modified: [list]
Behavior implemented: [description]
Artifacts written: [paths + namespaces]
Tests added: [file + count]
Test commands run: [commands]
Test results: [pass/fail summary]
Assumptions: [list]
Risks: [list or none]
VPS validation commands: [copyable block]
Recommended next step: [from .agent/project_state.yaml:next_official_step]
```
