# doc_audit — Documentation Auditor

## Purpose

`portfolio_automation/doc_audit.py` is the **observe-only** counterpart to the
portfolio-doc-writer.  The doc-writer mutates docs; the auditor reads them and
reports drift, gaps, dead references, and cross-document inconsistency.  It
never touches scoring, decisions, allocation, or portfolio state.

---

## Four Check Families

| Family | What it checks | Auto-fixable? |
|---|---|---|
| **Factual drift** | Documented value disagrees with source-of-truth JSON artifact | Yes (anchor-bound only) |
| **Coverage gaps** | New source module in `portfolio_automation/`, `watchlist_scanner/`, or `scanner/` with no `docs/<stem>.md` | No |
| **Dead refs** | Backtick `path/to/file.py` reference in a doc points to a file that no longer exists | No |
| **Cross-doc consistency** | The same anchor value is documented differently across two docs | No |

---

## The Anchor Registry

An `Anchor` is a typed binding between a machine-readable source and a
prose-documentation pattern:

```python
@dataclass(frozen=True)
class Anchor:
    name: str              # unique key, e.g. "concentration_cap"
    source_artifact: str   # repo-relative path to a JSON artifact
    source_json_path: str  # dot-separated key path inside that JSON
    doc_globs: tuple[str, ...]  # which docs this anchor is authoritative for
    pattern: str           # regex with exactly ONE capture group = the value
    fmt: str = "int"       # how to format the source value: int/float2/pct1/usd0
```

`ANCHOR_REGISTRY` is the module-level list of all registered anchors.  The
drift-check extracts the documented value with `pattern`, fetches the live value
via `resolve_source(anchor, root)`, formats it with `fmt`, and emits a Finding
when they differ.

### Currently registered (calibrated 2026-06-01)

Three structural-cap anchors, all in `docs/ALLOCATION_POLICY.md`, documented as
decimals in canonical bullet lines:

| Anchor | Source (`retune_impact.json`) | Pattern |
|---|---|---|
| `concentration_cap` | `current_snapshot.structural_caps.concentration_cap` | `` ^- `concentration_cap = (\d+\.\d+)` `` |
| `leverage_cap` | `current_snapshot.structural_caps.leverage_cap` | `` ^- `leverage_cap = (\d+\.\d+)` `` |
| `sector_cap` | `current_snapshot.allocation_engine.sector_cap` | `` ^- `sector_cap = (\d+\.\d+)` `` |

The `` ^- `name = `` bullet anchor is deliberate: it excludes the nearby
`Pre-retune baseline: ... concentration_cap = 0.40` lines so the auditor can
never rewrite the historical record.

**Deliberately NOT registered** (current-vs-reference ambiguities — auto-fixing
would replace a correct statement with a wrong one): pipeline stage count
("13-stage" pipeline vs "17-stage" wrapper vs `daily_run_status.total=24` —
three different measures); FMP daily budget (`DATA_AND_FMP_ENDPOINTS.md`
documents the client *default* 230, while 500 is the configured *live* value);
AI monthly cap (`AI_BUDGET.md` shows `monthly_cost_limit_usd: null` as an
example). These need manual operator reconciliation, not auto-fix.

### How to add an anchor

1. Identify the source JSON artifact and the key path inside it.
2. Identify the doc(s) where this value is documented, and read the ACTUAL line —
   match the format as it really appears (decimal vs percent, code-span vs prose).
3. Anchor the regex to the canonical line (e.g. a Markdown bullet `^- \`name = ...\``)
   so it does NOT also match baseline/example/historical mentions of the same key.
   The captured group must render identically to `resolve_source`'s `fmt` output
   when the doc is in sync (verify by running `find_drift` against the real repo).
4. Append an `Anchor(...)` to `ANCHOR_REGISTRY`.

A new anchor is auto-fix-eligible automatically — the fixer substitutes the
captured group in-place the next time the audit runs with `apply_enabled`. Only
register a value as an anchor if its source is the live truth (not a code default
or illustrative example); otherwise it belongs in a report-only check, not the
auto-fix registry.

---

## Auto-fix Guardrails

Auto-fix applies only to factual-drift findings.  The following guardrails are
always active, regardless of configuration:

- **Anchor-only**: only findings produced by `find_drift` (bound to a registered
  anchor) are eligible.
- **Cap 10/run**: at most 10 fixes are applied in a single audit run.
- **`apply_enabled` flag**: if `apply_enabled: false` in
  `.agent/doc_audit_state.yaml`, no files are touched (see *Pause Auto-fix*
  below).
- **Pure captured-value substitution**: only the regex capture group is replaced;
  surrounding prose is untouched.
- **Path-containment guard**: refuses to write to any path outside the repo tree.
- **Staleness guard**: if the target line has changed since the audit was
  computed, the fix is skipped rather than applied to the wrong position.
- **Rollback**: the audit trail is git history; any unwanted fix is undone with
  `git revert`.

---

## Git-as-State Model

Cross-workstation state lives in `.agent/doc_audit_state.yaml` (a tracked,
committed file):

| Key | Meaning |
|---|---|
| `last_audited_sha` | The git SHA through which the last audit ran |
| `last_run_at` | ISO-8601 timestamp of the last audit |
| `apply_enabled` | Whether auto-fix is permitted this run |
| `fixes_last_run` | Count of fixes applied on the last run |

The coverage-gap check derives "changed files" from `git diff <last_audited_sha>..HEAD`.
Because `.agent/` is tracked, any workstation that pulls the branch sees the
same last-audited baseline and produces the same coverage report.

---

## Two Cadence Tiers

| Tier | Skill | Cadence | Mode |
|---|---|---|---|
| Weekly | `/doc-audit` | Mon 09:45 UTC | Producer + guardrailed auto-fix + state advance |
| Monthly | `/doc-audit-monthly` | 1st of month 09:15 UTC | Producer + read-only judgment (clarity / conciseness / redundancy / decomposition) via `portfolio-doc-auditor` agent |

The weekly skill applies eligible auto-fixes and advances `last_audited_sha`.
The monthly skill is report-only; it dispatches the auditor agent but never
modifies docs.

---

## Pause Auto-fix

Set `apply_enabled: false` in `.agent/doc_audit_state.yaml`:

```yaml
apply_enabled: false
```

The audit will still run and report findings; it will just skip the substitution
step.  Re-enable by setting the flag back to `true`.

---

## Output Artifacts

- `outputs/latest/doc_audit_status.json` — machine-readable findings list
  (`observe_only: true` hardcoded)
- `outputs/latest/doc_audit_status.md` — human-readable summary

Both are consumed by `daily-tool-analysis` (AMBER on `coverage_gap` or unfixed
drift) and by the monthly `portfolio-doc-auditor` judgment agent.

---

## Related

- `portfolio_automation/doc_audit_state.py` — state file I/O
- `.agent/doc_audit_state.yaml` — committed state
- `docs/doc_audit_state.md` — state-module doc
