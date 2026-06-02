# Task template — Continuous-improvement rhythm

Stand up the steady-state loop: outcomes → calibration/attribution → tuning
*proposals*. Proposes only — never applies weights. Paste the block below into
Claude Code from the repo root.

---

```
You are working in this advisory-only repo. Obey CLAUDE.md and AGENTS.md exactly.

Read first:
- CLAUDE.md and AGENTS.md
- docs/PRODUCTION_READINESS_PLAN.md and docs/PATTERN_LOOP_IMPLEMENTATION_SPEC.md
  (Step 4 = proposals; Step 5 = apply, PROTECTED)
- docs/EVALUATION_AND_LEARNING_LOOP.md, docs/CONFIDENCE_CALIBRATION.md
- watchlist_scanner/weight_tuning.py, portfolio_automation/confidence_calibration.py,
  portfolio_automation/decision_outcome_tracker.py, profit_attribution/

Objective: assemble the recurring improvement cycle from EXISTING signals and
emit weight-tuning PROPOSALS for my review. This is observe-only. Do NOT apply
weight changes, and do NOT modify decision_engine.py, scoring.py,
allocation_engine.py, or the six protected scores. Step 5 (apply) is out of scope
here and requires my explicit written approval in a separate task.

Begin in PLAN MODE. Present the plan and WAIT for approval. Then, one step at a time:

1. Aggregate the feedback already produced: decision-outcome resolutions
   (1/3/7-day), confidence calibration buckets/slope, profit attribution, and the
   per-signal calibration in watchlist_signals.json (signal_results). Write a
   concise "improvement digest" to outputs/policy/ with observe_only: true.
2. Detect drift worth acting on: calibration slope decay, patterns whose
   out-of-sample edge faded, persistent over/under-confidence per signal_id.
   Gate every conclusion on minimum sample size + a confidence interval.
3. Produce tuning PROPOSALS by extending weight_tuning.py's existing
   weight_tuning_suggestions.json contract (align schemas; mark proposed_only:
   true). Each proposal: signal_id, bounded delta, rationale, n, OOS hit rate +
   CI, and the noise-control comparison. The registry file stays byte-identical.
4. Summarize what you'd recommend applying and why — for my decision. Do not edit
   config/signal_registry.yaml.

For each step: add healthy + degraded tests, `python -m py_compile`, targeted
then relevant suite. End with the repo's Final Report (and confirm
config/signal_registry.yaml is unchanged). PAUSE for my approval.
If on the laptop, return VPS validation commands as a copyable block.
```
