---
description: Run the full MONTHLY cadence suite of the Portfolio Automation System in one shot — invokes monthly-tool-analysis, doc-audit-monthly, and pattern-loop-analysis in sequence, then emits a combined roll-up (worst-status lead line + one heartbeat per member) followed by each member's detailed output. Pure orchestration; observe-only. Run on demand or via the monthly (1st-of-month) cron.
---

# Run All — Monthly Suite

Orchestrator skill. Runs every monthly-cadence skill in order and rolls the results
up into one report. Adds **no logic of its own** — it only sequences the members and
formats the roll-up. Each member keeps its own boundaries.

## Members (run in THIS order)
1. `monthly-tool-analysis` — 30-day retrospective across the four lenses (pattern-efficacy trends, retune-apply audit, AI+FMP spend trajectory, memo-vs-outcome accuracy, discovery yield).
2. `doc-audit-monthly` — monthly documentation retrospective (dispatches the read-only portfolio-doc-auditor for the judgment dimensions; report-only).
3. `pattern-loop-analysis` — Pattern-Improvement Loop operational + health check; its recompute cadence is monthly, so this is its primary home.

## How to run
Invoke each member via the Skill tool, in the order above, one at a time, letting each
complete fully before starting the next. Capture each member's lead line.

**Failure tolerance:** if a member errors or a Skill invocation fails, record it as
`ERROR — <reason>` for that member and CONTINUE to the next. Never abort the suite.

## Output contract
1. **Roll-up lead line** (always first):
   `[GREEN|AMBER|RED] run-all-monthly YYYY-MM-DD: {n}/{total} skills run · worst {STATUS}{ · {k} errored}`
   Suite status = WORST member status (RED > AMBER > GREEN); an errored member counts
   as AMBER and is named.
2. **Per-member heartbeat block** — one line each, in run order:
   `- monthly-tool-analysis: {its lead line}`
   `- doc-audit-monthly: {its lead line}`
   `- pattern-loop-analysis: {its lead line}`
   (Errored member: `- <skill>: ERROR — <reason>`.)
3. **Detailed sections** — each member's full output under a `### <skill>` header, in
   run order.

## Record the run (always, after the members complete)
Stamp the monthly cadence (observe-only state in `.agent/suite_run_state.json`):
```bash
.venv/bin/python -c "from portfolio_automation.suite_run_state import stamp; stamp('monthly')"
```

## Boundaries
Observe-only orchestration. Does not modify the decision plan, scoring, allocation,
config, or holdings. Members retain their own report-only / guardrailed behaviour.
