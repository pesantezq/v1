---
description: Run the full DAILY cadence suite of the Portfolio Automation System in one shot — invokes daily-tool-analysis, quant-watch-analysis, pattern-loop-analysis, and daily-system-improvement in sequence, then emits a combined roll-up (worst-status lead line + one heartbeat per member) followed by each member's detailed output. Pure orchestration; observe-only; changes no member behaviour. Run on demand or via the daily cron.
---

# Run All — Daily Suite

Orchestrator skill. Runs every daily-cadence skill in order and rolls the results
up into one report. This skill adds **no analysis logic of its own** — it only
sequences the members and formats the roll-up. Each member keeps its own hard
boundaries (observe-only, no decision/score/allocation changes).

## Members (run in THIS order)
1. `daily-tool-analysis` — system-wide health triage across the four lenses (dispatches its own agents; folds the Step-1 backbones of quant-watch + pattern-loop as one-liners).
2. `quant-watch-analysis` — full sub-RED quant-concern ledger triage + manual-judgment layer.
3. `pattern-loop-analysis` — Pattern-Improvement Loop operational + health check.
4. `daily-system-improvement` — Type-C engineering/product/ops improvement backlog.

**Why 2 and 3 run standalone even though daily-tool-analysis already folds their
backbones:** the standalone skills produce the fuller triage + manual-judgment output
that the one-line fold omits. The re-run is idempotent (quant-watch re-evaluates the
same ledger for the day; pattern-loop is read-mostly), so this is safe and intended —
do NOT skip them.

## How to run
Invoke each member via the Skill tool, in the order above, one at a time, letting each
complete fully (including its own subagent dispatches and any state writes) before
starting the next. Capture each member's lead line.

**Failure tolerance:** if a member errors or a Skill invocation fails, record it as
`ERROR — <reason>` for that member and CONTINUE to the next. Never abort the suite
because one member failed.

## Output contract
Emit, in this structure:

1. **Roll-up lead line** (always first):
   `[GREEN|AMBER|RED] run-all-daily YYYY-MM-DD: {n}/{total} skills run · worst {STATUS}{ · {k} errored}`
   The suite status is the WORST member status (RED > AMBER > GREEN). A member that
   errored counts as AMBER for the suite and is named in the errored count.
2. **Per-member heartbeat block** — one line each, in run order:
   `- daily-tool-analysis: {its lead line}`
   `- quant-watch-analysis: {its lead line}`
   `- pattern-loop-analysis: {its lead line}`
   `- daily-system-improvement: {its one-line summary}`
   (For an errored member: `- <skill>: ERROR — <reason>`.)
3. **Detailed sections** — each member's full output under a `### <skill>` header, in
   run order, so nothing from the individual skills is lost.

## Boundaries
Observe-only orchestration. Does not modify any member skill, the decision plan,
scoring, allocation, config, or holdings. Any operator decisions surfaced by
`daily-system-improvement` are still routed through that skill's own artifact-based
approval flow (this suite executes nothing on their behalf).
