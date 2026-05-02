# Repo Cleanup Audit

Last updated: 2026-05-02

## 1. Summary

This cleanup pass reviewed the repository structure, documentation, artifact contracts, tests, and agent state before continuing to `daily_memo_discovery_section`.

The core architecture is workable and still advisory-only. The main cleanup value is consolidation: keep contracts precise, document suspected legacy files, and avoid deleting ambiguous files until ownership is confirmed.

No scoring, allocation, recommendation, discovery promotion, run-mode permission, broker/API, AI/LLM, or auto-trading behavior was changed.

## 2. What Was Inspected

- Agent state: `.agent/project_state.yaml`, `.agent/phase_status.yaml`, `AGENTS.md`, `CLAUDE.md`
- Discovery runtime: `portfolio_automation/discovery/`
- GUI discovery loaders and view: `gui_operator_data.py`, `gui/app.py`
- Artifact governance: `portfolio_automation/data_governance.py`, `portfolio_automation/run_mode_governance.py`
- Artifact contracts and docs: `docs/OUTPUT_ARTIFACT_CONTRACTS.md`, `docs/DISCOVERY_ENGINE.md`, `docs/RUN_MODE_GOVERNANCE.md`, `docs/DATA_GOVERNANCE.md`, `docs/AI_BUDGET.md`, `docs/CONFIDENCE_CALIBRATION.md`, `docs/roadmap.md`
- Test hygiene: `tests/discovery/`, GUI approval tests, run-mode/data-governance/system-health tests
- Repo inventory: top-level modules, docs, generated outputs, scripts, tests, deployment files

## 3. Files Safe/Active

The following areas are active and should not be deleted during cleanup:

- `main.py`, `run_daily_pipeline.py`
- `portfolio_automation/`
- `watchlist_scanner/`
- `gui/app.py`, `gui_operator_data.py`
- `agent/`
- `policy_evaluator/`
- `profit_attribution/`
- `theme_engine/`, `theme_discovery/`
- `scraped_intel/`
- `tests/`
- `docs/`
- `.agent/`
- `deploy/`, `scripts/`

Some top-level modules overlap with package modules, but they are still referenced by tests, scripts, or legacy entry points. They should be consolidated only after import tracing and owner approval.

## 4. Files Suspected Legacy/Dead

Not deleted in this pass:

- `demo_email_view.csv`, `demo_scored_recommendations.csv`, `demo_tracker.xlsx` - likely demo artifacts.
- `output/` - old generated-output directory separate from governed `outputs/`.
- `stockbot.txt`, `stockbot.txt.pub` - key-like files; review for removal/rotation outside normal code cleanup.
- `DEPLOYMENT.md` and `docs/deployment.md` - overlapping deployment docs that should be reconciled.
- `decision_memo.md`, `email_draft.md`, `email_prompt.txt`, `ml_analysis_prompt.txt`, `monthly_memo.md`, `escalation_packet.md` - likely generated or prompt artifacts; confirm whether still operator-maintained.
- `test_demo.py` - standalone demo test; confirm whether it still has value.
- `backtesting/fmp_backtester.py`, `policy_evaluator/__main__.py`, `tools/policy_recommender.py`, `tools/policy_simulator.py`, `watchlist_scanner/optimize_config.py`, `universe/fmp_universe.py` - possible CLI/manual tooling; keep until usage is verified.
- `__pycache__/`, `*.pyc`, `.pytest_cache/` - generated cache files. Safe to clean locally, but not part of this repo-edit pass.

## 5. Files Modified

- `portfolio_automation/discovery/approval_workflow.py`
- `gui_operator_data.py`
- `docs/OUTPUT_ARTIFACT_CONTRACTS.md`
- `docs/DISCOVERY_ENGINE.md`
- `docs/roadmap.md`
- `docs/REPO_CLEANUP_AUDIT.md`

## 6. Files Deleted

None.

## 7. Docs-To-Runtime Mismatches Found

- `rejected_candidates.json` runtime uses top-level `candidates`; docs were too vague and some historical tests used `rejected_candidates`.
- Approval summary is computed in memory; stale constants/docs implied a separate `approval_summary.json` artifact.
- Approval JSONL loaders now defensively skip tampered-but-valid records, so docs needed to mention read-side filtering.

## 8. Artifact Contract Fixes

- Documented required top-level fields for `outputs/sandbox/discovery/rejected_candidates.json`.
- Clarified runtime `candidates` key and backward-compatible GUI fallback for old `rejected_candidates` fixtures.
- Clarified `approval_decisions.jsonl` is the only approval artifact and that summaries are in-memory only.
- Clarified malformed/tampered approval JSONL records are ignored by loaders.

## 9. Agent State Fixes

`.agent/project_state.yaml` already points to `daily_memo_discovery_section` as the current and next official primary step.

`.agent/phase_status.yaml` already lists completed discovery corroboration, GUI approval workflow, AI call-site instrumentation, GUI panels, and related hardening notes. No agent state change was required in this pass.

## 10. Test Hygiene Findings

- Discovery approval tests cover allowed decisions, forbidden values, append-only writes, malformed JSONL tolerance, tampered-record filtering, and summary compatibility.
- GUI approval tests cover approval loaders, summary display data, rejected candidate key compatibility, and approval-only availability.
- No stale expected counts were changed in code; roadmap wording was changed from brittle counts to coverage descriptions where cleanup hardening adds additional tests.

## 11. Remaining Cleanup Backlog

- Decide whether to remove or archive demo/generated files listed above.
- Reconcile `DEPLOYMENT.md` with `docs/deployment.md` and the systemd/VPS runbooks.
- Audit top-level duplicate-sounding modules against package modules before any deletion.
- Consider moving generated prompt/memo samples into a documented `examples/` or `docs/examples/` directory if they are intentionally retained.
- Clean local generated caches (`__pycache__/`, `*.pyc`, `.pytest_cache/`) outside source-control cleanup.

## 12. Safety Confirmation

Confirmed for this pass:

- No scoring behavior changed.
- No allocation behavior changed.
- No recommendation behavior changed.
- No official watchlist mutation was added.
- No discovery promotion behavior was added.
- No BUY/SELL/ACTIONABLE/PROMOTED/VALIDATED discovery status was added.
- No broker/API execution or auto-trading was added.
- No AI/LLM or external API calls were added.
- Discovery remains sandbox-only and research-only.
- Approval decisions remain audit/research notes only.

## 13. Recommended Next Step

Proceed to `daily_memo_discovery_section`, the primary `next_official_step` in `.agent/project_state.yaml`.
