---
description: Run the full WEEKLY cadence suite of the Portfolio Automation System in one shot — invokes doc-audit, strategy-lab-analysis, and strategy-catalog in sequence, then emits a combined roll-up (worst-status lead line + one heartbeat per member) followed by each member's detailed output. Pure orchestration; observe-only (doc-audit's guardrailed drift-fix is the only member that may mutate docs, unchanged here). Run on demand or via the weekly cron.
---

# Run All — Weekly Suite

Orchestrator skill. Runs every weekly-cadence skill in order and rolls the results
up into one report. Adds **no logic of its own** — it only sequences the members and
formats the roll-up. Each member keeps its own boundaries.

## Members (run in THIS order)
1. `doc-audit` — weekly documentation audit; auto-fixes high-confidence factual drift under its own guardrails (cap 10/run, apply_enabled flag) and dispatches portfolio-doc-writer for the rest. This is the ONLY member that may mutate docs, and only within its existing guardrails — the suite does not change that.
2. `strategy-lab-analysis` — health + status review of the Research-Backed Strategy Lab.
3. `strategy-catalog` — regenerate + review the Strategy Catalog (Strategy Documentation Requirement); flags any undocumented tactic.

## How to run
Invoke each member via the Skill tool, in the order above, one at a time, letting each
complete fully before starting the next. Capture each member's lead line.

**Failure tolerance:** if a member errors or a Skill invocation fails, record it as
`ERROR — <reason>` for that member and CONTINUE to the next. Never abort the suite.

## Output contract
1. **Roll-up lead line** (always first):
   `[GREEN|AMBER|RED] run-all-weekly YYYY-MM-DD: {n}/{total} skills run · worst {STATUS}{ · {k} errored}`
   Suite status = WORST member status (RED > AMBER > GREEN); an errored member counts
   as AMBER and is named.
2. **Per-member heartbeat block** — one line each, in run order:
   `- doc-audit: {its lead line}`
   `- strategy-lab-analysis: {its lead line}`
   `- strategy-catalog: {its one-line summary}`
   (Errored member: `- <skill>: ERROR — <reason>`.)
3. **Detailed sections** — each member's full output under a `### <skill>` header, in
   run order.

## Record the run (always, after the members complete)
Stamp the weekly cadence so `run-all-daily`'s due-check resets — this applies
whether the suite was run standalone OR auto-chained from `run-all-daily`:
```bash
.venv/bin/python -c "from portfolio_automation.suite_run_state import stamp; stamp('weekly')"
```
This writes `.agent/suite_run_state.json:last_weekly_run_at` (observe-only state).

## Boundaries
Observe-only orchestration. Does not modify the decision plan, scoring, allocation,
config, or holdings. doc-audit's guardrailed doc-drift auto-fix is the sole
doc-mutating behaviour and is entirely owned by that member skill.
