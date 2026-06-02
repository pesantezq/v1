# Task template — Upkeep / housekeeping pass

Routine maintenance to prevent slow rot. Observe-first, additive fixes only.
Paste the block below into Claude Code from the repo root.

---

```
You are working in this advisory-only repo. Obey CLAUDE.md and AGENTS.md exactly.

Read first:
- CLAUDE.md and AGENTS.md
- docs/BUILD_ROADMAP.md (section 5) and docs/TECH_DEBT_AUDIT.md
- .agent/project_state.yaml  (and run: python scripts/agent_context_check.py)

Objective: a routine upkeep pass. Report findings first; apply only additive,
low-risk fixes. NEVER modify decision_engine.py, scoring.py, allocation_engine.py,
the six protected scores, or any output artifact schema.

Begin in PLAN MODE. Inventory, then present findings + a fix plan, and WAIT for
my approval before changing anything. Checklist:

1. Health snapshot: run `python -m tools.status` and `python -m tools.smoke_test`
   and `python -m portfolio_automation.env --check`; summarize any red.
2. Empty-DB producers: run the pipeline once (scripts/run_daily_safe.sh or a dry
   run), then re-check the 13 SQLite tables; report which populate vs stay empty
   (wiring gap vs dead schema).
3. Dead/orphan outputs: `python -m tools.cleanup_orphan_outputs` (review first).
   Confirm every outputs/latest/*.json has at least one consumer (CLAUDE.md
   corollary); flag producers without consumers.
4. Dependency drift: diff requirements.txt against what's installed; flag
   outdated/pinned-but-vulnerable packages. Propose, don't auto-upgrade.
5. Dead code / noise: find unused modules, and library-code `print()` that should
   be logging (don't touch CLI display output).
6. Namespace hygiene: confirm no module writes outside its declared namespace.
7. Docs freshness: run the doc auditor (see doc_cleanup_prompt.md) and surface
   drift/dead-refs.
8. Test health: `python -m pytest -q`; report failures/slowest tests; note any
   modules with no test.

For each fix you apply: smallest patch, add/adjust tests, `python -m py_compile`,
targeted then relevant suite. End with the repo's Final Report and PAUSE.
If on the laptop, return VPS validation commands as a copyable block.
```
