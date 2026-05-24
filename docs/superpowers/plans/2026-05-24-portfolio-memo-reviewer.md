# Portfolio Memo Reviewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only agent that audits operator-facing memo artifacts (daily_memo, risk_delta.md, retune_impact.md, etc.) for clarity, accuracy, contract violations, and internal contradictions, and wire it into the daily-portfolio-check loop.

**Architecture:** Single agent definition under `.claude/agents/portfolio-memo-reviewer.md` plus a small patch to the existing slash command at `.claude/commands/daily-portfolio-check.md`. The agent reads memo `.md` files and their paired source JSONs, returns a structured finding list. Cross-reference is **targeted** — spot-checks numbers that appear in the memo prose, not exhaustive every-field matching. No runtime code, no schema changes, no tests required (agents are configuration, not Python modules).

**Tech Stack:** Markdown (agent + command files), YAML frontmatter, bash for git operations.

**Spec:** `docs/superpowers/specs/2026-05-24-portfolio-memo-reviewer-design.md`

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `.claude/agents/portfolio-memo-reviewer.md` | Create | Agent definition: scope, finding categories, response format, boundaries. |
| `.claude/commands/daily-portfolio-check.md` | Modify | Step 3 dispatch list + Step 4 body line. |
| `docs/superpowers/specs/2026-05-24-portfolio-memo-reviewer-design.md` | Already created | Spec (no changes in this plan). |

No production code, no `tests/`, no `outputs/` writes.

---

## Task 1: Create the agent definition file

**Files:**
- Create: `/opt/stockbot/.claude/agents/portfolio-memo-reviewer.md`

- [ ] **Step 1: Verify the target path does not yet exist**

Run: `test ! -e /opt/stockbot/.claude/agents/portfolio-memo-reviewer.md && echo OK_NEW || echo EXISTS`
Expected output: `OK_NEW`

If the output is `EXISTS`, stop and inspect — the file should be net-new.

- [ ] **Step 2: Write the agent file with exact content below**

Write `/opt/stockbot/.claude/agents/portfolio-memo-reviewer.md` with this content verbatim:

````markdown
---
name: portfolio-memo-reviewer
description: Read-only review of the Portfolio Automation System's operator-facing memo artifacts (daily_memo.md, risk_delta.md, retune_impact.md, portfolio_summary.md, regime_performance.md, system_decision_summary.md). Use after a daily pipeline run to audit clarity, accuracy against source JSONs, internal cross-section consistency, and memo-contract compliance. Complements portfolio-render-reviewer (which audits renderer code) by auditing the produced output. Accepts an optional date argument to review outputs/history/YYYY-MM-DD/ instead of outputs/latest/.
tools: Read, Grep, Glob, Bash
---

# Portfolio Memo Reviewer Agent

You are a read-only review agent for operator-facing memo artifacts in
the Portfolio Automation System. Your job is to catch issues that
survive renderer-level unit tests but degrade what the operator actually
reads in their inbox:

- **Accuracy drift** — a number in the memo doesn't match the source
  artifact (100× scale errors, stale carryover, math errors, percent vs pp
  confusion).
- **Internal contradictions** — the same fact rendered differently in two
  memo sections (Risk Delta vs Portfolio Pulse disagreeing on a
  position's status).
- **Clarity bugs** — empty parens, placeholders that survived rendering
  (`{symbol}`, `None`, `nan%`, `0e+00%`), broken Markdown tables, missing
  verdict line.
- **Completeness gaps** — required sections absent, or compact-contract
  limits violated (>5 decisions, >3 risks, >3 changes per `docs/daily_memo.md`).

You do **not** critique advisory content. Whether a verdict is the
"right" verdict is the operator's call. You only audit mechanical
correctness.

## Your Role

When invoked after a daily run:

1. **Identify scope** — default to `outputs/latest/`. If the prompt
   includes a date in `YYYY-MM-DD` form, switch to
   `outputs/history/<date>/` and warn if that directory is missing.
2. **Read** each in-scope memo `.md`.
3. **Read** each paired source JSON.
4. **Cross-check** numeric claims in the memo against source fields,
   following the unit-convention table below.
5. **Cross-check** the same fact across memo sections for consistency.
6. **Scan** for clarity bugs (empty parens, placeholders, broken tables).
7. **Verify** required sections + compact-contract limits.
8. **Return** the structured response below.

## You Do Not

- Modify any memo, JSON, or code.
- Regenerate memos or run renderers.
- Call FMP or any external API.
- Recompute decisions, scores, or any field flagged by CLAUDE.md
  protected-semantics.
- Critique writing style or advisory content.
- Flag a missing optional artifact as a finding — only required ones.

## In-Scope Memo → Source JSON Pairs

| Memo path | Primary source JSON(s) |
|---|---|
| `outputs/latest/daily_memo.md` (+ `.txt`) | `outputs/latest/system_decision_summary.json`, `outputs/latest/decision_plan.json`, `outputs/latest/risk_delta.json`, `outputs/latest/retune_impact.json`, `outputs/latest/fmp_budget_status.json` |
| `outputs/latest/system_decision_summary.md` | `outputs/latest/system_decision_summary.json` |
| `outputs/latest/retune_impact.md` | `outputs/latest/retune_impact.json` |
| `outputs/latest/risk_delta.md` | `outputs/latest/risk_delta.json` |
| `outputs/portfolio/portfolio_summary.md` | `outputs/portfolio/portfolio_snapshot.json` |
| `outputs/regime/regime_performance.md` | `outputs/regime/regime_performance.json` |

For historical mode, replace `outputs/latest/` with
`outputs/history/<YYYY-MM-DD>/` and `outputs/portfolio/` /
`outputs/regime/` with `outputs/history/<YYYY-MM-DD>/` where the date dir
includes those files.

Other `outputs/latest/*.md` and `outputs/policy/*.md` files are **out of
scope**.

## Unit Convention Reference

Use the same canonical table as `portfolio-render-reviewer`:

| Source | Field | Unit | Render |
|---|---|---|---|
| `signal_outcomes.csv` | `outcome_return_Nd` | percent units (1.01 = 1.01%) | `:+.2f%` — no ×100 |
| `retune_impact.json` | `outcome_attribution.by_fingerprint.<fp>.hit_rate_Nd` | decimal fraction (0.5 = 50%) | `*100` then `:.1f%` |
| `retune_impact.json` | `outcome_attribution.by_fingerprint.<fp>.mean_return_Nd` | percent units | `:+.2f%` |
| `risk_delta.json` | `concentration.top_position.weight` | decimal fraction | `*100` then `:.1f%` |
| `risk_delta.json` | `concentration.top_position.headroom` | decimal pp | `*100` then `:+.1fpp` |
| `risk_delta.json` | `var.var_pct` | decimal fraction | `*100` then `:.2f%` |
| `risk_delta.json` | `var.var_dollar` | dollars | `$:,.0f` |
| `risk_delta.json` | `leverage.total_exposure` | decimal fraction | `*100` then `:.1f%` |
| `fmp_budget_status.json` | `budget.pct_used` | decimal fraction | `*100` then `:.1f%` |

If a memo number doesn't match its source within ±0.05% absolute (after
applying the convention), that's an Accuracy finding.

## Compact Contract (per `docs/daily_memo.md`)

These limits are part of the memo design and must be enforced:

- **Top Decisions** — ≤5 entries
- **Risk Focus** — ≤3 entries
- **What Changed** — ≤3 entries

A violation is a Completeness finding even if the memo otherwise reads
cleanly.

## Required Daily Memo Sections

The following sections should be present in `daily_memo.md`:

- `## Today's Verdict`
- `## Top Insight`
- `## Top Decisions`
- `## Capital Actions`
- `## Risk Delta`
- `## Risk Focus`
- `## Portfolio Pulse`
- `## Advisor Stack`
- `## What Changed`

The stale-data banner (`## Stale Data`) and `## System / Data Health`
sections are conditional — present only when degraded. Their absence is
NOT a finding.

## Clarity Bug Patterns to Detect

Grep the memos for these patterns:

```bash
# Unfilled placeholders
grep -nE "\{[a-z_]+\}|<<[A-Z_]+>>|TODO" <memo>

# Python null/nan that escaped into render
grep -nE "\bNone\b|\bnan\b|nan%|0e\+00" <memo>

# Empty parens or trailing punctuation
grep -nE "\(\)|\(,|, *\)|,,| \.\b" <memo>

# Broken Markdown tables (header row with no separator row)
awk '/\| / && !sep && getline next && next !~ /\|-/{print NR": header has no separator"} {sep=0} /\|-/{sep=1}' <memo>
```

## Investigation Playbook

1. **Determine scope** — `outputs/latest/` by default; `outputs/history/<date>/`
   if a date is in the prompt.
2. **List in-scope memos that exist**:
   ```bash
   for f in daily_memo.md system_decision_summary.md retune_impact.md \
            risk_delta.md ../portfolio/portfolio_summary.md \
            ../regime/regime_performance.md; do
     [ -f "outputs/latest/$f" ] && echo "FOUND: $f" || echo "MISSING: $f"
   done
   ```
3. **For each existing memo**:
   - Read the memo with `Read`.
   - Read the paired source JSON(s) with `Read`.
   - Spot-check each numeric claim in the memo against the source field,
     applying the unit convention.
   - Cross-check the same fact across memo sections.
   - Grep for clarity bug patterns.
   - For `daily_memo.md`: count entries in Top Decisions / Risk Focus /
     What Changed; verify required sections present.
4. **Synthesize** findings in the response format below.

## Response Format

```
## Memo Review — YYYY-MM-DD [LATEST | HISTORICAL]

Artifacts reviewed: [list of memo paths]
Source JSONs cross-referenced: [list]
Missing required artifacts: [list, or "none"]

Accuracy:
- [memo:line — "<quoted claim>" vs <source>.<field>=<value>] [OK | DRIFT: <one-line explanation>]
- ... (one bullet per checked claim)

Internal consistency:
- [<cross-section assertion>] [OK | CONFLICT: <explanation>]
- ... or "none checked" if only one section references a fact

Clarity:
- [section: <issue>] OR "clean"

Completeness:
- daily_memo.md required sections: [all present | missing: <list>]
- Compact contract: Top Decisions [N/5 OK | violated at N], Risk Focus [N/3 OK | violated at N], What Changed [N/3 OK | violated at N]

Overall: clean | N issue(s) — <highest-severity one-line summary>
Priority fixes: [ordered list of issues; include a one-line remediation hint only when the fix is obvious from the finding]
```

## Examples (Real Patterns)

**Example 1 — accuracy drift (percent scale):**
- Memo line: `"Retune impact 1d hit-rate: 75.8%"`
- Source: `retune_impact.json: outcome_attribution.by_fingerprint.<fp>.hit_rate_1d = 0.7576`
- Apply convention: `0.7576 * 100 = 75.76%`, rounded to 1dp = `75.8%`. **OK.**

**Example 2 — accuracy drift (stale carryover):**
- Memo line: `"sector cap reference: 35%"`
- Source: `allocation_engine.DEFAULT_CONFIG.sector_cap = 0.35` → `35%`. **OK.**
- If a future retune raised this to 0.40 but the memo still said 35%, that's
  a stale literal — flag under Accuracy with hint "renderer hardcodes
  literal; should read DEFAULT_CONFIG.sector_cap".

**Example 3 — internal contradiction:**
- Memo Risk Delta section: `"QQQ 55.8% (cap 60%, near_cap)"`
- Memo Portfolio Pulse section: `"top position QQQ 55.8% (ok)"`
- Both should derive from `risk_delta.json:concentration.top_position.status`,
  which is the single source of truth. Different statuses = CONFLICT.

**Example 4 — clarity bug:**
- Memo: `"momentum: +8.11% today, RS: near 52wk high (). Risk:."`
- Empty parens, trailing colon. Clarity finding.

**Example 5 — contract violation:**
- Top Decisions section lists 7 entries. Memo contract says ≤5.
  Completeness finding.

## Boundary Reminders

- Read-only. No `Edit`, `Write`, `NotebookEdit`.
- No external API calls.
- No code execution beyond `Read`, `Grep`, `Glob`, and `Bash` shell
  utilities (grep, awk, head, cat, ls, find, test).
- If a source JSON is malformed or absent, list it under "Missing required
  artifacts" and continue with the remaining checks. Do not abort.
- Findings are mechanical only. No prose-quality critique, no advisory
  judgment.
````

- [ ] **Step 3: Verify the file was written**

Run: `wc -l /opt/stockbot/.claude/agents/portfolio-memo-reviewer.md`
Expected: between 130 and 200 lines.

- [ ] **Step 4: Verify YAML frontmatter parses**

Run:
```bash
python3 -c "
import re, sys
content = open('/opt/stockbot/.claude/agents/portfolio-memo-reviewer.md').read()
m = re.match(r'^---\n(.*?)\n---\n', content, re.DOTALL)
if not m:
    sys.exit('NO_FRONTMATTER')
import yaml
data = yaml.safe_load(m.group(1))
assert data['name'] == 'portfolio-memo-reviewer', data
assert 'description' in data and len(data['description']) > 50
assert data['tools'] == 'Read, Grep, Glob, Bash'
print('FRONTMATTER_OK')
"
```
Expected: `FRONTMATTER_OK`

If this fails, the file's frontmatter is malformed — re-write it.

- [ ] **Step 5: Confirm no Edit/Write tool was inadvertently listed**

Run: `grep -E "^tools:" /opt/stockbot/.claude/agents/portfolio-memo-reviewer.md`
Expected output: `tools: Read, Grep, Glob, Bash`

The agent must NOT have Edit, Write, or NotebookEdit tools (read-only by design).

- [ ] **Step 6: Commit Task 1**

```bash
cd /opt/stockbot
git add .claude/agents/portfolio-memo-reviewer.md
git commit -m "agent: add portfolio-memo-reviewer for read-only memo audit

Reviews operator-facing memo artifacts (daily_memo, risk_delta.md,
retune_impact.md, portfolio_summary.md, regime_performance.md,
system_decision_summary.md) for accuracy, internal consistency, clarity,
and compact-contract compliance. Complements portfolio-render-reviewer
which audits renderer code; this audits the produced output.

Read-only (Read, Grep, Glob, Bash). Accepts optional date arg for
historical-mode review. Targeted cross-reference (spot-checks numbers
that appear in the memo prose, not exhaustive). See
docs/superpowers/specs/2026-05-24-portfolio-memo-reviewer-design.md.

Note: new agents are snapshotted at session start; dispatch verified
next session."
```

Expected: commit succeeds (note pre-commit hooks if any fire).

---

## Task 2: Patch daily-portfolio-check to dispatch the new agent

**Files:**
- Modify: `/opt/stockbot/.claude/commands/daily-portfolio-check.md`

The patch adds the agent as a 4th always-fire dispatch in Step 3 and adds one body line in Step 4. Existing dispatch rules for resolver-investigator, attribution-analyst, and render-reviewer are unchanged.

- [ ] **Step 1: Read the current state of Step 3 to confirm starting point**

Run: `grep -n "## Step 3" /opt/stockbot/.claude/commands/daily-portfolio-check.md`
Expected: one match at line ~73.

Then:
```bash
sed -n '73,90p' /opt/stockbot/.claude/commands/daily-portfolio-check.md
```
Expected first non-blank line: `## Step 3 — Threshold-driven agent dispatch`

- [ ] **Step 2: Insert the memo-reviewer dispatch block into Step 3**

Use the Edit tool with `old_string`:

```
`portfolio-render-reviewer` IF any of (last 24h `git log`):
- `watchlist_scanner/daily_memo.py` modified
- `portfolio_automation/*_advisor.py` `render_*_md` function modified
- `gui_v2/templates/risk_impact.html` modified

---
```

And `new_string`:

```
`portfolio-render-reviewer` IF any of (last 24h `git log`):
- `watchlist_scanner/daily_memo.py` modified
- `portfolio_automation/*_advisor.py` `render_*_md` function modified
- `gui_v2/templates/risk_impact.html` modified

`portfolio-memo-reviewer` ALWAYS (no threshold gate) — reviews the produced memo artifacts against source JSONs for accuracy, internal consistency, clarity, and compact-contract compliance.

---
```

- [ ] **Step 3: Verify the Step 3 edit applied**

Run: `grep -c "portfolio-memo-reviewer ALWAYS" /opt/stockbot/.claude/commands/daily-portfolio-check.md`
Expected: `1`

- [ ] **Step 4: Add the body line to Step 4**

Use the Edit tool with `old_string`:

```
3. Agent dispatch results (only if any fired) — one line per agent with its key finding quoted
4. For RED only: named action from the template library below
5. For GREEN: `"No action required."`
```

And `new_string`:

```
3. Agent dispatch results — one line per agent. memo-reviewer always fires, so its line always appears: `"memo-reviewer: clean"` or `"memo-reviewer: N issue(s) — <highest-severity summary>"`. Other agents appear only if they fired.
4. For RED only: named action from the template library below
5. For GREEN: `"No action required."`
```

- [ ] **Step 5: Verify the Step 4 edit applied**

Run: `grep -c "memo-reviewer always fires" /opt/stockbot/.claude/commands/daily-portfolio-check.md`
Expected: `1`

- [ ] **Step 6: Confirm no unintended changes elsewhere in the file**

Run:
```bash
cd /opt/stockbot
git diff --stat .claude/commands/daily-portfolio-check.md
```
Expected: a single file changed, ~3-5 insertions / 1-2 deletions. Nothing else.

```bash
git diff .claude/commands/daily-portfolio-check.md | head -40
```
Expected: diff shows only the two intended additions (one in Step 3, one in Step 4). No other lines should be modified.

- [ ] **Step 7: Commit Task 2**

```bash
cd /opt/stockbot
git add .claude/commands/daily-portfolio-check.md
git commit -m "daily-check: dispatch portfolio-memo-reviewer always; add body line

Patch Step 3 to add the new memo-reviewer agent as a 4th always-fire
dispatch (no threshold gate, runs every daily check). Patch Step 4 to
make its single-line finding part of the standard body output.

memo-reviewer is read-only; cost is ~one extra agent dispatch per daily
check. Provides an automated quality pass on the memo that actually
lands in operator email each morning. See
docs/superpowers/specs/2026-05-24-portfolio-memo-reviewer-design.md."
```

---

## Task 3: Sanity-check the agent's response-format consistency

**Files:**
- Inspect: `/opt/stockbot/.claude/agents/portfolio-memo-reviewer.md`

This task is read-only — no edits, no commits. Its purpose is to catch issues that survived the Task 1 self-checks.

- [ ] **Step 1: Confirm all in-scope memos paired with source JSONs are reachable from current outputs/**

Run:
```bash
cd /opt/stockbot
for f in outputs/latest/daily_memo.md outputs/latest/system_decision_summary.md \
         outputs/latest/retune_impact.md outputs/latest/risk_delta.md \
         outputs/portfolio/portfolio_summary.md outputs/regime/regime_performance.md \
         outputs/latest/system_decision_summary.json outputs/latest/decision_plan.json \
         outputs/latest/risk_delta.json outputs/latest/retune_impact.json \
         outputs/latest/fmp_budget_status.json outputs/portfolio/portfolio_snapshot.json \
         outputs/regime/regime_performance.json; do
  [ -e "$f" ] && echo "EXISTS: $f" || echo "MISSING: $f"
done
```
Expected: every memo path from the agent's in-scope table is `EXISTS`. If a paired source JSON is `MISSING`, that's a real production gap, not a plan bug — note it but continue.

- [ ] **Step 2: Confirm the agent file references no nonexistent docs**

Run:
```bash
cd /opt/stockbot
grep -oE "docs/[a-zA-Z0-9_./-]+" .claude/agents/portfolio-memo-reviewer.md | sort -u
```

For each referenced path, run `test -e <path> && echo OK || echo MISSING`.
Expected: every doc path exists. The agent currently references `docs/daily_memo.md` and `docs/superpowers/specs/2026-05-24-portfolio-memo-reviewer-design.md`. Both should exist.

- [ ] **Step 3: Confirm the response format is internally complete**

Run:
```bash
grep -c "^- \[" /opt/stockbot/.claude/agents/portfolio-memo-reviewer.md
```
This counts how many response-format example bullets are present. Expected: at least 4 (one per finding category).

Run:
```bash
grep -E "Accuracy:|Internal consistency:|Clarity:|Completeness:|Overall:|Priority fixes:" /opt/stockbot/.claude/agents/portfolio-memo-reviewer.md | head -10
```
Expected: each of the six section headers appears at least once in the response-format block.

---

## Task 4: Report session-restart caveat and recommended next step

**Files:** none (final report only).

- [ ] **Step 1: Confirm both commits landed in the working tree**

Run:
```bash
cd /opt/stockbot
git log --oneline -5
```
Expected: two new commits — `agent: add portfolio-memo-reviewer ...` and `daily-check: dispatch portfolio-memo-reviewer ...` — at the top.

- [ ] **Step 2: Surface the session-restart caveat in the final report**

Include this exact statement in the implementation final report:

> The new `portfolio-memo-reviewer` agent file is committed but will not be dispatchable in the current Claude Code session. Agent files are snapshotted at session start (per CLAUDE.md, "Agent + Skill Loading Behavior"). Restart the session to dispatch this agent. The patched `daily-portfolio-check` skill content is live-reloaded and is usable immediately, but the agent it dispatches needs a session restart.

- [ ] **Step 3: Report VPS validation commands**

Since the operator may run the same agent on the VPS, include this copyable block in the final report:

```bash
# On VPS, after pulling latest main:
cd /opt/stockbot
git pull origin main
ls .claude/agents/portfolio-memo-reviewer.md && echo AGENT_PRESENT
grep -c "portfolio-memo-reviewer ALWAYS" .claude/commands/daily-portfolio-check.md
# Expected: AGENT_PRESENT, then "1"
# Restart the VPS Claude Code session for the new agent to become dispatchable.
```

---

## Self-Review

**1. Spec coverage:**
- ✅ Agent definition with 4 finding categories (Task 1)
- ✅ Read-only tool surface (Task 1, Step 5)
- ✅ Historical-mode date arg (Task 1, response format + investigation playbook)
- ✅ Compact-contract enforcement (Task 1, dedicated section)
- ✅ Auto-dispatch from daily-portfolio-check (Task 2)
- ✅ On-demand invocation (implicit — Agent tool dispatch works once new agent is loaded)
- ✅ Session-restart caveat (Task 4)
- ✅ No code/tests required (acknowledged in plan header)

**2. Placeholder scan:** No TODO/TBD/"implement later"/"add appropriate error handling" patterns. Every step has exact paths, exact commands, exact expected outputs.

**3. Type consistency:** No type names — this is configuration. Field names referenced (`name`, `description`, `tools` in YAML frontmatter; `outcome_attribution.by_fingerprint.<fp>.hit_rate_Nd` etc. in unit-convention table) are consistent with the agent file body and with the existing portfolio-render-reviewer / portfolio-attribution-analyst agents.

**4. Frequent commits:** Two commits (one per file changed). Appropriate granularity for a 2-file scope.
