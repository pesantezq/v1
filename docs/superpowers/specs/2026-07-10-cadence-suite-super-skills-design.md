# Design — Cadence "suite" super-skills (run-all-daily / -weekly / -monthly)

Date: 2026-07-10
Status: approved (operator, via /daily-system-improvement follow-up)

## Problem
The system has grown a family of cadence-scoped analysis/maintenance skills
(daily-tool-analysis, daily-system-improvement, quant-watch-analysis,
pattern-loop-analysis, doc-audit, strategy-lab-analysis, strategy-catalog,
monthly-tool-analysis, doc-audit-monthly). Running a full cadence today means
invoking each skill by hand and mentally stitching the results. The operator wants
one command per cadence that runs the whole tier and rolls the results up.

## Goal
Three thin **orchestrator** skills — one per cadence — that invoke their member
skills in sequence and emit a combined roll-up. Pure orchestration: observe-only, no
new decision/scoring/allocation logic, no change to any member skill's behaviour or
boundaries.

## How a super-skill works
A skill is a markdown instruction file; invoking it loads its instructions into the
main context. A super-skill's body instructs the assistant to invoke each member
skill via the Skill tool, in order. Execution is inherently **sequential** (skills
share the single main context), though each member still fans its own subagents out
internally (e.g. daily-tool-analysis dispatching the attribution/memo agents). If a
member errors, the orchestrator records the failure and continues to the next member —
it never aborts the roll-up.

## Membership (deduped, in run order)
| Suite | Members |
|---|---|
| `run-all-daily` | daily-tool-analysis → quant-watch-analysis → pattern-loop-analysis → daily-system-improvement |
| `run-all-weekly` | doc-audit → strategy-lab-analysis → strategy-catalog |
| `run-all-monthly` | monthly-tool-analysis → doc-audit-monthly → pattern-loop-analysis |

Notes:
- daily-tool-analysis already folds the *Step-1 backbones* of quant-watch and
  pattern-loop into its one-line heartbeats. The daily suite ALSO runs those two
  standalone (operator's explicit choice) for their fuller triage + manual-judgment
  layer. The re-run is idempotent (quant-watch re-evaluates the same ledger; pattern-
  loop is read-mostly), so double-inclusion is safe — documented as intentional.
- pattern-loop-analysis lives in the monthly suite because its recompute cadence is
  monthly; it appears in daily only as the standalone tripwire above.
- yearly-tool-analysis is out of scope (operator asked for daily/weekly/monthly only).

## Output contract (each suite)
1. A lead roll-up line: `[GREEN|AMBER|RED] run-all-<cadence> YYYY-MM-DD: <n> skills run, <worst-status> worst` where the suite status is the worst member status (RED > AMBER > GREEN; a member that errored counts as AMBER for the suite and is named).
2. One heartbeat line per member: `- <skill>: <that skill's lead line, or "ERROR — <reason>">`.
3. Then each member's full detailed output, in order, under a `### <skill>` header.

## Files
- `.claude/commands/run-all-daily.md`
- `.claude/commands/run-all-weekly.md`
- `.claude/commands/run-all-monthly.md`
Each: `description:` frontmatter + a short body listing members, run order, the
failure-tolerance rule, and the roll-up output contract.

## Scheduling (operator opted in)
Add a crontab block that invokes each suite on its cadence (daily; weekly Mon;
monthly 1st), staggered after the production cron. The exact block is presented to
the operator for approval BEFORE installation — nothing is scheduled without sign-off.
The suites remain invocable on demand regardless.

## Auto-chain: daily → weekly when due (added 2026-07-10)
Because the suites are on-demand (no suite crons), `run-all-daily` doubles as the
weekly scheduler. After its four daily members complete, it checks
`suite_run_state.is_due("weekly")` (true if ≥7 days since the weekly suite last ran,
or never) and, if due, auto-invokes `/run-all-weekly` and folds its roll-up into the
daily report under an `## ⟳ Auto-chained: run-all-weekly` header. Each suite stamps
its own cadence (`suite_run_state.stamp("<cadence>")`) when it runs, so the weekly
clock resets whether weekly ran standalone or was auto-chained — it will not
re-trigger the next day.

- State: `.agent/suite_run_state.json` (`last_{daily,weekly,monthly}_run_at`), managed
  by the pure helper `portfolio_automation/suite_run_state.py` (load / stamp /
  days_since / is_due), mirroring `doc_audit_state`. Observe-only.
- Threshold: 7 days for weekly (`DUE_THRESHOLD_DAYS`). "Never run" counts as due.
- Scope: daily→weekly AND weekly→monthly are both wired (2026-07-10). The chain
  cascades: a fully-lapsed `run-all-daily` runs daily → (weekly due) → run-all-weekly
  → (monthly due) → run-all-monthly in one pass, catching every tier up. Thresholds:
  weekly 7d, monthly 30d. Each tier stamps its own cadence so it won't re-fire early.
- A chained suite's status does not absorb into the parent lead-line; each tier reports
  its own worst-status inside its folded `## ⟳ Auto-chained: run-all-<cadence>` section.

## Boundaries / non-goals
- Observe-only. No member skill is modified. No decision/score/allocation change.
- No new producers or artifacts (the members write their own artifacts as today).
- The orchestrator adds no logic of its own beyond sequencing + roll-up formatting.

## Testing / validation
Skill files are markdown instructions, not code, so there is no unit test. Validation
is a live smoke run: invoke `/run-all-daily` and confirm each member fires and the
roll-up renders with correct worst-status aggregation. (This mirrors how the existing
analysis skills are validated — by running them.)
