# Documentation Audit System — Design Spec

**Date:** 2026-06-01
**Status:** Approved (brainstorming) — pending implementation plan
**Owner:** operator (Enrique Pesantez)

## Problem

The repo has a documentation *writer* (`portfolio-doc-writer` agent + `portfolio-docs`
skill) that reactively syncs docs for one just-shipped change. It has **no
documentation *auditor***: nothing sweeps the whole corpus (51 docs, ~14.3k lines)
to catch docs that contradict the code, numbers that have drifted, changes that
shipped with no doc update, redundant/bloated prose, or cross-doc contradictions.
The four-lens tool-analysis system audits *runtime health* only.

The operator edits the repo mainly from the production VPS but also from other
workstations, so any solution must be portable (no machine-local state) and
runnable both on a schedule (VPS) and on demand (anywhere).

## Goals

- A read-only audit that scans docs against current code + recent git history and
  reports findings across four dimensions: factual drift/staleness, coverage gaps,
  clarity/conciseness, cross-doc consistency.
- High-confidence **factual drift** is auto-fixed under guardrails; everything else
  is report-only (drafted by the existing `portfolio-doc-writer`).
- Runs as a portable on-demand skill **and** an automated VPS cron.
- Two cadence tiers: a fast **weekly** deterministic+auto-fix pass, and a heavier
  **monthly** judgment retrospective.

## Non-Goals

- No changes to runtime behavior, scoring/decision/allocation semantics, or output
  schemas (the auditor documents and reports; it never recomputes decisions).
- No new prose-generation engine — judgment findings are produced by the
  `portfolio-doc-auditor` agent and fixed by `portfolio-doc-writer`.
- Not a replacement for `portfolio-doc-writer` — the auditor is the *observe* half;
  the writer remains the *mutate* half for judgment-call edits.

## Architecture

Follows the repo's established pattern: observe-only producer → read-only agent →
orchestrator skill → cron, with git as the cross-workstation state store.

```
            git  (.agent/doc_audit_state.yaml = last_audited_sha)
                         │ git diff <sha>..HEAD
                         ▼
WEEKLY   /doc-audit ──▶ doc_audit.py (deterministic) ──▶ outputs/latest/doc_audit_status.json + .md
(skill + VPS cron)        │
                          ├─ high-confidence drift ─▶ AUTO-FIX (guardrails, audit log, git-revertible)
                          └─ everything else ───────▶ dispatch portfolio-doc-writer (draft, operator approves)

MONTHLY  /doc-audit-monthly ──▶ portfolio-doc-auditor (judgment) ──▶ monthly report (report-only)
(skill + VPS cron)              clarity / conciseness / redundancy / large-doc decomposition
```

### Components (five units, one job each)

1. **`portfolio_automation/doc_audit.py`** — observe-only producer. Pure functions,
   `observe_only: true` hardcoded, degrades to a status dict on any failure. Runs the
   deterministic checks only. Writes `outputs/latest/doc_audit_status.json` + `.md`
   via `OutputNamespace.LATEST`.
2. **`portfolio-doc-auditor`** (new `.claude/agents/` agent, read-only:
   Read/Grep/Glob/Bash) — the documentation lens. Handles judgment dimensions
   (clarity, conciseness, redundancy, "doc grew too large"). Reads the producer JSON +
   flagged docs, returns ranked findings. Never edits.
3. **`/doc-audit`** (new skill) — weekly orchestrator: run producer → auto-fix safe
   drift under guardrails → dispatch `portfolio-doc-writer` for the rest → write state.
4. **`/doc-audit-monthly`** (new skill) — monthly orchestrator: dispatch
   `portfolio-doc-auditor` for the corpus-wide judgment retrospective; report-only.
   A one-line dispatch hook is added to `monthly-tool-analysis` so the documentation
   verdict surfaces in the monthly heartbeat.
5. **`portfolio-doc-writer`** (existing, unchanged) — the only unit that makes
   judgment-call edits, gated by operator approval.

## Deterministic checks (producer)

Four families. Only family 1 is auto-fixable.

### 1. Factual drift — anchor registry (auto-fixable)

A registry of `(anchor_name, doc_pattern, source_of_truth)` bindings, seeded by a
**full upfront sweep** of documented constants across all docs, growable thereafter.
Seed examples:

| Anchor | Documented in | Source of truth |
|---|---|---|
| pipeline stage count | `PIPELINE_RUNBOOK.md`, `ARCHITECTURE.md`, `CRON_AND_PREFLIGHT_RUNBOOK.md` | `daily_run_status.json:stage_summary.total` |
| concentration_cap / leverage_cap / sector_cap | `ALLOCATION_POLICY.md`, `CHANGELOG_DECISIONS.md` | `retune_impact.json:current_snapshot` |
| fmp_daily_calls_budget | `AI_BUDGET.md`, runbooks | config / `fmp_budget_status.json:budget` |
| AI monthly cost cap | `AI_BUDGET.md` | `ai_budget_summary.json:monthly_cost_limit_usd` |
| discovery pulse caps (fmp/openai/runs) | `DISCOVERY_ENGINE.md`, pulse docs | `discovery_pulse_status.json:caps` |
| retune auto-apply guardrails (drift cap, min_n, max deltas) | learning-loop docs | `gate_retune_suggestions.json:guardrails` |

A doc value is **auto-fix eligible only if**: it is a registered anchor, its source
is currently resolvable, `doc_value != source_value`, and the substitution is a pure
in-place value replace (no structural edit). Anything else → report-only.

### 2. Dead references (report-only)

Scan docs for `path/file.py` and `symbol()` mentions; flag any that no longer resolve
via Glob/Grep. Not auto-fixed — the producer cannot infer the new name.

### 3. Cross-doc consistency (report-only, may become auto-fixable)

When the same anchor appears in ≥2 docs, flag disagreement. Once the anchor's source
is resolved, these are fixed in the same auto-fix pass as family 1.

### 4. Coverage gaps (report-only)

From `git diff <last_audited_sha>..HEAD`:
- new `portfolio_automation/*.py` / `watchlist_scanner/*.py` with no `docs/<MODULE>.md`;
- commits touching protected-semantics files with no `CHANGELOG_DECISIONS.md` entry in range;
- new `outputs/latest/*.json` artifacts absent from `OUTPUT_ARTIFACT_CONTRACTS.md`.

## Output artifact

`outputs/latest/doc_audit_status.json` (`OutputNamespace.LATEST`):

```json
{
  "generated_at": "ISO",
  "observe_only": true,
  "schema_version": "1",
  "source": "doc_audit",
  "last_audited_sha": "…",
  "current_sha": "…",
  "overall_status": "ok | ok_with_warnings | drift | coverage_gap",
  "findings": [
    {"dimension": "drift|dead_ref|consistency|coverage",
     "severity": "low|med|high",
     "doc": "docs/…", "detail": "…",
     "auto_fixable": true, "anchor": "stage_count",
     "current": "17", "expected": "24"}
  ],
  "auto_fix_candidates": [ … subset of findings with auto_fixable=true … ],
  "coverage_gaps": [ … ],
  "auto_fixes_applied": [ … populated by the skill after it applies … ],
  "disclaimer": "Observe-only documentation audit. Reads docs + code + git; …"
}
```

Plus a compact `doc_audit_status.md` summary.

## Guardrails, state, rollback (modeled on `retune_auto_apply`)

- **Auto-fix guardrails:** registered anchor only; source resolvable; pure value
  substitution; per-run cap (default ≤ 10 anchors/run, configurable); `apply_enabled` flag
  (default `true`) to pause auto-fix without disabling the audit.
- **State (committed, portable):** `.agent/doc_audit_state.yaml` holds
  `last_audited_sha`, `last_run_at`, `apply_enabled`, per-run fix counts. Coverage is
  derived from `git diff <last_audited_sha>..HEAD`. No `data/`-local state (that dir
  is gitignored and would not travel between workstations).
- **Audit trail = git history.** Auto-fixes land in a dedicated commit
  (`docs(auto): doc-audit drift fixes YYYY-MM-DD`). Rollback = `git revert`. A
  committed `.md` summary records what each run changed.

## Cadence

- Weekly `/doc-audit` — VPS cron Mondays 09:45 UTC; runs identically on demand on any
  workstation. Deterministic checks + guardrailed auto-fix.
- Monthly `/doc-audit-monthly` — VPS cron 1st of month 09:45 UTC. Judgment
  retrospective, report-only. Surfaced via a hook in `monthly-tool-analysis`.

## Testing & coverage pairing (CLAUDE.md requirement)

- `tests/test_doc_audit.py` — anchor-drift detection; coverage-gap detection from a
  fixture git range; auto-fix eligibility logic; degraded-state (unresolvable source
  → report-only, never auto-fix). Healthy + degraded fixtures.
- **Consumer pairing:** `daily-tool-analysis` gains a line that reads the latest
  `doc_audit_status.json` and flags AMBER on any high-severity coverage gap or unfixed
  drift; the monthly tier owns the deep clarity review. (Satisfies the "every
  artifact has a consumer" corollary.)
- **Agent-loading note:** the new `portfolio-doc-auditor` agent is committed but will
  not dispatch until the next session (per CLAUDE.md agent-snapshot rule); the skill
  degrades to producer-only findings if the agent is not yet live.

## Build sequence (high level — detailed in the implementation plan)

1. Seed the anchor registry via a full upfront sweep of documented constants.
2. `doc_audit.py` producer + `tests/test_doc_audit.py` (deterministic checks).
3. `/doc-audit` skill: orchestration + guardrailed auto-fix + state write.
4. `portfolio-doc-auditor` agent (judgment lens).
5. `/doc-audit-monthly` skill + `monthly-tool-analysis` hook.
6. `daily-tool-analysis` consumer line.
7. VPS cron entries (weekly + monthly).
8. Docs sync (this system documents itself: a `docs/DOC_AUDIT.md`).

## Open risks

- Anchor registry false positives (a doc number that legitimately differs from the
  artifact, e.g. an illustrative example). Mitigation: anchors are explicit opt-in
  bindings, not a blanket numeric scan; ambiguous matches stay report-only.
- Auto-fix on an anchor whose source artifact is transiently stale/degraded.
  Mitigation: source must be resolvable AND fresh; degraded → report-only.
