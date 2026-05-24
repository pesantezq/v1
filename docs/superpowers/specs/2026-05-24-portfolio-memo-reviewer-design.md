# Design — `portfolio-memo-reviewer` Agent

Date: 2026-05-24
Status: design (pending user approval before implementation)

## Problem

The daily Portfolio Automation pipeline emails an operator-facing memo each
morning. Today, no automated check inspects the produced memo for
clarity, factual accuracy against source artifacts, or contract
violations. `portfolio-render-reviewer` covers the renderer **code** when
that code is edited, but not the **rendered output** for runs where the
code is unchanged. Numeric drift between source JSONs and memo prose,
internal cross-section contradictions, stale carryover claims, and
compact-contract overruns can therefore survive into the inbox unflagged.

## Goal

Introduce a read-only review agent that audits operator-facing memo
artifacts after generation, returns a structured finding list, and
participates in the daily-portfolio-check loop.

## Non-Goals

- Editing memos, regenerating them, or modifying renderer code.
- Recomputing decisions, scores, or any field flagged by CLAUDE.md
  protected-semantics.
- Critiquing the advisory content itself (whether the verdict is "right"
  is the operator's call). The agent reviews mechanical correctness only.
- Replacing `portfolio-render-reviewer`. The two are complementary —
  render-reviewer scopes the source, memo-reviewer scopes the output.

## Scope of Artifacts Reviewed

The agent reviews this fixed set of operator-facing memos and their
paired source JSONs:

| Memo | Primary source JSON(s) |
|---|---|
| `outputs/latest/daily_memo.md` (+ `.txt`) | `outputs/latest/system_decision_summary.json`, `outputs/latest/decision_plan.json`, `outputs/latest/risk_delta.json`, `outputs/latest/retune_impact.json`, `outputs/latest/fmp_budget_status.json` |
| `outputs/latest/system_decision_summary.md` | `outputs/latest/system_decision_summary.json` |
| `outputs/latest/retune_impact.md` | `outputs/latest/retune_impact.json` |
| `outputs/latest/risk_delta.md` | `outputs/latest/risk_delta.json` |
| `outputs/portfolio/portfolio_summary.md` | `outputs/portfolio/portfolio_snapshot.json` |
| `outputs/regime/regime_performance.md` | `outputs/regime/regime_performance.json` |

Other `outputs/latest/*.md` and `outputs/policy/*.md` files are out of
scope. They are reference dumps rather than narrative memos and would
inflate runtime without proportionate benefit.

## Historical-Mode Support

The agent accepts an optional date argument. When invoked as
`portfolio-memo-reviewer 2026-05-20` (or with a date in the prompt body),
it reviews `outputs/history/2026-05-20/*.md` instead of `outputs/latest/`.
This unblocks postmortem review of an emailed memo whose oddity was
noticed days later. When no date is provided, it reviews
`outputs/latest/`.

## Findings Categories

Each invocation returns findings grouped into four buckets. Severity is
implicit in ordering (accuracy issues first; cosmetic clarity last).

### 1. Accuracy

Cross-references each numeric claim in the memo against its source
artifact. The unit-convention table from `portfolio-render-reviewer`
applies — hit_rate as decimal, mean_return as percent units, weight as
decimal, etc. Detects 100× scale drift, stale carryover from previous
runs, math errors (e.g., capital action subtotals disagreeing with the
"total recommended capital" line), and percent-vs-pp confusions.

Targeted, not exhaustive: the agent spot-checks the numbers that appear
in the memo prose, not every field in every source JSON. Exhaustive
coverage was rejected to avoid bloated runtime and false-positive rate.

### 2. Internal Consistency

Detects same-fact disagreement across memo sections. Example: Risk Delta
section reports `near_cap` on QQQ while Portfolio Pulse section shows the
same position as `ok`. Both should derive from `risk_delta.json`; a
mismatch indicates a renderer wired up to a stale or wrong source.

### 3. Clarity

Checks operator readability: empty parens, dangling commas, half-rendered
placeholders (`{symbol}`, `None`, `nan%`, `0e+00%`), verdict line missing
or contradictory, action items unparseable. Does not critique writing
style.

### 4. Completeness — Including Compact-Contract Enforcement

Verifies that sections required by `docs/daily_memo.md` are present and
non-empty. Verifies the compact-contract limits hold:

- ≤5 entries in Top Decisions
- ≤3 entries in Risk Focus
- ≤3 entries in What Changed

A violation is a finding even if the prose otherwise reads cleanly,
because the contract is the agreed memo design.

## Response Format

```
## Memo Review — YYYY-MM-DD

Artifacts reviewed: [paths]
Source JSONs cross-referenced: [paths]

Accuracy:
- [memo line N: "<quoted claim>" → source <file>.<field> = <value>] [OK | DRIFT: <explanation>]
- ...

Internal consistency:
- [<cross-section assertion>] [OK | CONFLICT: <explanation>]
- ...

Clarity:
- [section name: <issue>] OR "none"

Completeness:
- daily_memo.md required sections: [all present | missing: <list>]
- Compact contract: Top Decisions [N/5 OK | violated], Risk Focus [N/3 OK | violated], What Changed [N/3 OK | violated]

Overall: clean | N issue(s) — <highest-severity one-line summary>
Priority fixes: [ordered list; include a one-line remediation hint only when the fix is obvious]
```

Findings are terse and mechanical, mirroring `portfolio-render-reviewer`.
Remediation hints appear only when the fix is unambiguous from the
finding alone (e.g., "renderer rounded to 1dp; widen to .2f"). When the
fix requires judgment, the agent lists the issue and stops.

## Tooling

`tools: Read, Grep, Glob, Bash` — read-only. No `Edit`, `Write`, or
`NotebookEdit`. Consistent with `portfolio-render-reviewer`.

## Invocation

### From `daily-portfolio-check` (auto-dispatch)

Patched into Step 3 of `/opt/stockbot/.claude/commands/daily-portfolio-check.md`
as a fourth always-fire dispatch (no threshold gate). The skill's body
output (Step 4) gains one line:

```
memo-reviewer: clean | N issue(s) [— <highest-severity summary>]
```

If issues are found, the priority-fix list flows through to the body
under the existing "Agent dispatch results" subsection.

Cost: one extra agent dispatch per daily check (~+20k tokens at the
attribution-analyst's measured rate, similar order). Acceptable for a
once-per-day check whose primary purpose is operator-facing quality.

### On-demand

`Agent` tool with `subagent_type: portfolio-memo-reviewer`. Optional
date argument in the prompt body. Useful for postmortems and for
re-checking a memo after a renderer fix.

## Boundaries (Hard)

- Does not call FMP or any external API.
- Does not write to `outputs/latest/` or any other namespace.
- Does not modify code, tests, or configuration.
- Does not touch `decision_engine.py`, scoring logic, or protected-semantics fields.
- Treats missing source JSONs gracefully — flags the missing input under
  Accuracy and proceeds with the remaining cross-checks.

## Files Touched by Implementation

| Path | Change |
|---|---|
| `.claude/agents/portfolio-memo-reviewer.md` | New file. Agent definition. |
| `.claude/commands/daily-portfolio-check.md` | Patch Step 3 dispatch + Step 4 body line. |

No production code, no test files, no schema changes.

## Test Coverage

Smoke-testing the new agent requires a session restart, because
`.claude/agents/*.md` are snapshotted at session start (per CLAUDE.md
Agent + Skill Loading Behavior). The agent file will be reviewed by Read
in the same session to verify YAML frontmatter and response-format
template render correctly. End-to-end dispatch verification is deferred
to the next session and noted explicitly in the final report.

No new pytest cases are required (this is an agent definition, not
runtime code).

## Open Questions

None remaining after brainstorming. All scope, trigger, and tone
decisions confirmed with the user.

## Risks

- **Token overhead** — one extra agent dispatch per daily check. Mitigated
  by the targeted (not exhaustive) cross-reference policy.
- **False positives on advisory prose** — risk that the agent flags
  legitimate operator-facing phrasing as "vague." Mitigated by scoping
  Clarity to mechanical issues (empty parens, placeholders, contradictions),
  not subjective writing-quality calls.
- **Schema drift** — if `daily_memo.md` adds new required sections, the
  completeness check needs updating. Mitigated by referencing
  `docs/daily_memo.md` as the single source of truth in the agent prompt,
  so the check is description-driven rather than hardcoded.
