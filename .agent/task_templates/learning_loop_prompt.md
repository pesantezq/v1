# Task template — Learning loop (pattern-improvement)

Paste the block below into Claude Code from the repo root. It plans first, waits
for your approval, then executes one step at a time and stops at the protected
weight-apply gate. Source of truth: `docs/PATTERN_LOOP_IMPLEMENTATION_SPEC.md`.

---

```
You are working in this advisory-only repo. Obey CLAUDE.md and AGENTS.md exactly.

Read first, in this order:
- CLAUDE.md and AGENTS.md
- docs/PATTERN_LOOP_IMPLEMENTATION_SPEC.md   ← the source of truth for this work
- docs/PRODUCTION_READINESS_PLAN.md
- .agent/project_state.yaml  (and run: python scripts/agent_context_check.py)
- backtesting/poc_simulation_harness.py and tests/test_poc_simulation_harness.py
- config/signal_registry.yaml, watchlist_scanner/weight_tuning.py,
  portfolio_automation/retune_auto_apply.py

Objective: implement the pattern-improvement loop exactly as specified in
docs/PATTERN_LOOP_IMPLEMENTATION_SPEC.md, extending existing modules (don't
reinvent weight_tuning.py / retune_auto_apply.py / the signal_results calibration).

Hard rules:
- Steps 0–4 and Step 6 are observe-only and additive. Build only those.
- STOP before Step 5. It edits signal_registry.yaml weights / scoring and is
  PROTECTED — do not start it, and do not touch decision_engine.py, scoring.py,
  allocation_engine.py, or the six protected scores, without my explicit written
  approval.
- All new artifacts use the OutputNamespace safe writers: backtests →
  HISTORICAL (outputs/backtest/), proposals → POLICY (outputs/policy/), each with
  observe_only: true. Never write to outputs/latest/ from a backtest path.
- Respect FMP compliance/budget; keep everything deterministic and seeded.

Workflow:
1. Begin in PLAN MODE. Present a step-by-step plan mapped to the spec's Steps
   0–4 (+6): files you'll create, function signatures, and the test plan for
   each. Note any conflict with .agent next_official_step and proceed since I'm
   explicitly authorizing this scope. WAIT for my approval before editing.
2. Then execute ONE step at a time. For each step: add healthy + degraded tests,
   run `python -m py_compile` on touched files, run the targeted test, then the
   relevant suite. End every step with the repo's Final Report format, update
   .agent state, and PAUSE for my OK before the next step.
3. Use the repo's own skills/agents where they apply (e.g., portfolio-feature,
   portfolio-test-reviewer).
4. If you're on the laptop, return VPS validation commands as a copyable block;
   do not claim VPS results.

Start with Step 0 (baseline run) and Step 1 (real-signal ingestion).
```
