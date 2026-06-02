# Task template — Analysis cadence (daily/monthly/yearly agents)

Close the two cadence gaps from the roadmap: wire `portfolio-test-reviewer` into
a cadence, and create + dispatch a `portfolio-backtest-health` agent for the
learning loop. Paste the block below into Claude Code from the repo root.

---

```
You are working in this advisory-only repo. Obey CLAUDE.md and AGENTS.md exactly.

Read first:
- CLAUDE.md (the four-lens model + "Analysis + Health Coverage Requirement")
- docs/BUILD_ROADMAP.md (section 3 — operating cadence + the two gaps)
- .claude/commands/daily-tool-analysis.md, monthly-tool-analysis.md,
  yearly-tool-analysis.md
- .claude/agents/portfolio-test-reviewer.md and one wired agent for the pattern
  (e.g. .claude/agents/portfolio-learning-loop-health.md)

Objective: improve the daily/monthly/yearly analysis coverage. Observe-only —
agents and analysis commands never modify scoring/decision/allocation logic.

Begin in PLAN MODE. Present the plan and WAIT for approval. Then, one item at a time:

1. Wire `portfolio-test-reviewer` (Developer lens) into a cadence — it currently
   isn't dispatched by any. Add it to daily (or at least monthly): extend the
   command's artifacts-read + dispatch + body-grammar sections following the
   existing pattern. Add/extend a test asserting it produces healthy vs degraded
   status on fixtures.
2. Create `.claude/agents/portfolio-backtest-health.md` (Quant lens) — the
   analysis-health pairing CLAUDE.md requires for the POC simulation harness. It
   reads outputs/backtest/ and outputs/policy/signal_weight_proposals.json and
   flags: stale results, degenerate output (e.g., all-"unknown" regimes), n below
   threshold, calibration slope sign-flips, and "looks-fresh-but-empty" artifacts.
   Dispatch it from yearly-tool-analysis (backtest cadence). Add its test.
3. Verify the CLAUDE.md corollary: every outputs/latest/*.json is consumed by at
   least one cadence check; report any orphans.

NOTE (CLAUDE.md): newly created agents are snapshotted at session start — after
writing portfolio-backtest-health.md, commit it and tell me a session restart is
needed before it can dispatch; smoke-test only the refreshed/edited agents.

For each item: `python -m py_compile` touched Python, targeted then relevant
suite (`python -m pytest -q`). End with the repo's Final Report and PAUSE.
If on the laptop, return VPS validation commands as a copyable block.
```
