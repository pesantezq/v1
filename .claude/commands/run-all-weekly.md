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

## Auto-chain the monthly suite when due
AFTER the three weekly members complete (and before emitting the roll-up), check
whether the monthly suite is due — it is due once ≥ 30 days have elapsed since it
last ran, or if it has never run:
```bash
.venv/bin/python -c "from portfolio_automation.suite_run_state import is_due, days_since; import json; print(json.dumps({'due': is_due('monthly'), 'days_since': days_since('monthly')}))"
```
- If `due` is **true**: announce `"Monthly suite due ({days_since}d since last / never) — auto-chaining /run-all-monthly"`, then invoke the `/run-all-monthly` skill and let it complete. It stamps its own run, so the clock resets and it will not re-trigger next week. Fold its roll-up into this run's output (see contract below).
- If `due` is **false**: note `"Monthly suite not due (~{days_since:.1f}/30d) — skipped"` and do nothing further.
- This mirrors the daily→weekly chain: a fully-lapsed `run-all-daily` can therefore cascade daily → weekly → monthly in one pass, catching every tier up.
- Failure tolerance applies: if the due-check or the chained monthly errors, record it and still emit the weekly roll-up.

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
   Append a monthly-chain note line: `- monthly-suite: auto-chained (due {days_since}d)` OR `- monthly-suite: not due (~{days_since:.1f}/30d), skipped`.
3. **Detailed sections** — each member's full output under a `### <skill>` header, in
   run order.
4. **Auto-chained monthly** (only if it ran): the full `/run-all-monthly` roll-up +
   detailed sections under a `## ⟳ Auto-chained: run-all-monthly` header. The weekly
   lead-line status does NOT absorb the monthly status — the monthly reports its own
   worst-status within its section.

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
