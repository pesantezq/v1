# Task template — Production hardening loop

Paste the block below into Claude Code from the repo root. It plans first, waits
for your approval, then works the audit's prioritized order one item at a time.
Source of truth: `docs/TECH_DEBT_AUDIT.md`.

---

```
You are working in this advisory-only repo. Obey CLAUDE.md and AGENTS.md exactly.

Read first:
- CLAUDE.md and AGENTS.md
- docs/TECH_DEBT_AUDIT.md           ← the prioritized work list
- docs/PRODUCTION_READINESS_PLAN.md (sections 3 and 6)
- docs/REGRESSION_CHECKLIST.md
- .agent/project_state.yaml  (and run: python scripts/agent_context_check.py)

Objective: work the "Prioritized remediation order" table in
docs/TECH_DEBT_AUDIT.md to move the system toward production-ready.

Hard rules:
- Additive and backward-compatible. NO changes to decision_engine.py, scoring.py,
  allocation_engine.py, the six protected scores, or any output artifact schema
  without my explicit written approval.
- New layers are observe-only (observe_only: true) and wrapped in try/except so
  they can't break the pipeline. Follow the OutputNamespace rules.
- Get a green test safety net BEFORE any refactor of large files
  (gui/app.py, main.py); never refactor protected modules.

Workflow:
1. Begin in PLAN MODE. Re-verify the audit's priority order against the current
   code (counts may have changed), then present a sequenced plan: per item give
   scope, risk, the smallest safe patch, and the test plan. WAIT for my approval.
2. Execute ONE item at a time, in the audit's order:
   (1) verify empty-DB producers by running the pipeline once and re-checking —
       report whether it's wiring or dead schema;
   (2) add logging / narrow the silent `except Exception` sites in
       gui_operator_data.py;
   (3) finish v2-data-governance: route the 3 flagged writers through
       data_governance.safe_write_json;
   (4) add characterization tests for the top untested modules (start with
       gui_operator_data.py);
   then continue down the table.
   For each item: tests first where feasible, `python -m py_compile`, targeted
   tests, then the relevant suite. End with the repo's Final Report, update
   .agent state, and PAUSE for my OK before the next item.
3. If you're on the laptop, return VPS validation commands as a copyable block;
   do not claim VPS results.

Start with item (1): verify the empty-DB producers.
```
