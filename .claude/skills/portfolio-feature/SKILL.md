---
name: portfolio-feature
description: Implement a scoped, additive observe-only feature for the Portfolio Automation System with tests, docs, non-blocking pipeline integration, and a structured final report. Use when adding a new advisor/producer module, a new artifact writer, or a new non-blocking pipeline stage that respects the protected-semantics + observe-only invariants.
---

# Skill: portfolio-feature

## Purpose

Implement a scoped feature for the Portfolio Automation System with tests,
docs, non-blocking pipeline integration, and a structured final report.

## When to Use

- Implementing a new observability v2 producer (concentration, leverage, VaR,
  budget telemetry, run-status, attribution, etc.)
- Adding a new additive non-blocking stage to `scripts/run_daily_safe.sh`
- Implementing a step from `.agent/project_state.yaml:next_official_step`
- Building a new artifact writer + summary function under `outputs/latest/`
- Adding a new memo section that reads existing artifacts (no scoring change)

## When NOT to Use

- Changing scoring, allocation, or recommendation behavior — requires explicit
  user approval with scope. The post-2026-05-18 gauge values are the new
  baseline; do not re-tune them without approval.
- Adding broker integration or auto-trading.
- Building GUI pages — use the GUI v2 patterns under `gui_v2/templates/`.
- Running VPS deployment — the user does this manually.
- Modifying the protected scores: `signal_score`, `confidence_score`,
  `effective_score`, `conviction_score`, `final_rank_score`,
  `recommendation_score`.

## Pre-flight: Read Current State

```bash
python scripts/agent_context_check.py
cat .agent/project_state.yaml | head -50
```

Current `next_official_step` is `observe_and_iterate` — the data-maturation
phase. Most new features should be *observability* additions that surface
existing data more usefully, not new decision/scoring logic.

## Step-by-Step Process

1. **Confirm scope.**
   - Is this an additive observability layer? Proceed.
   - Does it touch scoring/allocation/recommendation? Stop. Ask for explicit
     scoped approval first.

2. **Read the v2 producer reference implementations.**
   - `portfolio_automation/risk_delta_advisor.py` — single-snapshot reader,
     three computed sections, status badging.
   - `portfolio_automation/retune_impact_tracker.py` — append-only history
     ledger pattern + outcome attribution join.
   - `portfolio_automation/fmp_budget_telemetry.py` — multi-source read +
     history JSONL pattern.
   - `portfolio_automation/daily_run_status.py` — log-scan pattern.
   - `portfolio_automation/resolution_due_probe.py` — CSV scan + group-by.

3. **Implement the module.**
   - Create `portfolio_automation/<module>.py`.
   - Use `safe_write_json` / `safe_write_text` from `data_governance` for all
     writes; never bypass namespace routing.
   - Hardcode `observe_only: True`, `schema_version: "1"`, `source: "<module>"`
     in artifact payloads.
   - Standard function names: `build_<name>(...)`, `render_<name>_md(...)`,
     `run_<name>(...)`.
   - Wrap the top-level `run_*` in `try/except` and return a degraded-state
     dict (`{"status": "error", "error": ...}`) on any unhandled exception.

4. **Write tests** at `tests/test_<module>.py`.
   - Pure computation: synthetic dict in → expected output.
   - Degraded mode: missing artifacts → `{"available": False, "reason": ...}`.
   - End-to-end: temp dir + minimal inputs + `run_*` → both `.json` and `.md`
     artifacts exist + `observe_only=True` asserted.
   - No-mutation invariant: `decision_plan.json` and `portfolio_snapshot.json`
     are untouched after the run.
   - For any FMP-fallback path, use `unittest.mock.MagicMock` — do not depend
     on a live FMP key.

5. **Document.**
   - Create `docs/<MODULE_NAME>.md` from the template in
     `.claude/agents/portfolio-doc-writer.md`.
   - Add the JSON + MD contract to `docs/OUTPUT_ARTIFACT_CONTRACTS.md`.
   - Append an entry to `docs/CHANGELOG_DECISIONS.md` if the feature changes
     a contract, flag, or data flow.

6. **Wire into the pipeline.**
   - Add a new `run_aux_stage` call in `scripts/run_daily_safe.sh` between
     existing stages. Non-blocking by definition (Stage 1 is the only
     fail-fast stage).
   - Add the module to `scripts/preflight.sh` compile-check and
     advisor-smoke-import lists.

7. **Surface in the memo (optional).**
   - If the output is glanceable, add a one-line summary to the Advisor
     Stack or Portfolio Pulse section of `watchlist_scanner/daily_memo.py`.
   - Match the existing pattern: `_safe_load` the artifact, format conditionally
     on `available`.

8. **Compile + test.**

   ```bash
   python -m py_compile portfolio_automation/<module>.py
   python -m pytest -q tests/test_<module>.py
   python -m pytest -q \
     --ignore=tests/test_gui_api_health.py \
     --ignore=tests/test_gui_insight_cards.py
   ```

9. **Return final report.**
   Use `.agent/task_templates/final_report_template.md`.
   Include VPS validation commands. Do not claim VPS tests passed.

## Required Final Output

- New module file: `portfolio_automation/<module>.py`
- New test file: `tests/test_<module>.py`
- Updated or new docs: `docs/<MODULE_NAME>.md`
- Updated `docs/OUTPUT_ARTIFACT_CONTRACTS.md`
- Updated `scripts/run_daily_safe.sh` + `scripts/preflight.sh`
- Optional: memo section in `watchlist_scanner/daily_memo.py`
- Optional: CHANGELOG entry in `docs/CHANGELOG_DECISIONS.md`
- Final report using the template, including VPS commands
