# discovery (memo renderer)

Last verified against `watchlist_scanner/memo/discovery.py` (split out of
`watchlist_scanner/daily_memo.py` in `b93e977c`, 2026-08-07). Last updated
2026-08-08.

> **Disambiguation.** This doc covers `watchlist_scanner/memo/discovery.py` —
> the two **memo renderers** for the Discovery Research section. It is not about
> the discovery *engine*: the candidate producers live under
> `portfolio_automation/discovery/` (`news_integration.py`,
> `automatic_promotion_governance.py`, `approval_workflow.py`,
> `discovery_reports.py`) and are documented separately.

## Purpose

Render the **Discovery Research** section of the daily memo, in both the
plain-text and Markdown flavours, from already-produced sandbox discovery
artifacts.

The module exists because `daily_memo.py` had grown to 3,749 lines and hosted
four of the eleven rendering defects found by the 2026-08-07 memo review. These
two builders were identified as the cleanest seam in that module: 326 lines
depending on exactly one shared helper and two constants, all of which either
travel with them or live in `watchlist_scanner/memo/_shared.py`. Both functions
are re-exported from `watchlist_scanner.daily_memo`, so **every existing import
path is unchanged**.

See `watchlist_scanner/memo/__init__.py` for the wider extraction policy: groups
move one cohesive set at a time, only when their coupling is genuinely low.

## Governance posture

**Sandbox-only, render-only.** Stated in the module docstring and enforced by
what the code does:

- Nothing here is a buy/sell recommendation.
- It does not update the official watchlist, the portfolio, or
  `decision_plan.json`.
- It performs **no I/O**: both functions are pure string builders over a dict
  the caller supplies. They read no file and write no artifact, so they declare
  no `OutputNamespace` of their own. (`_safe_load` is imported from `_shared`
  but is currently unused in this module.)
- Neither function sets `observe_only` — that field belongs to artifact
  *producers*. These are renderers; their governance statement is the
  `_DISCOVERY_DISCLAIMER` string and the `[Research lane — sandbox only. No
  official action taken.]` footer, both emitted into the rendered output on the
  populated path.

### Forbidden-decision defense-in-depth

Both builders filter the approval list through
`_shared._FORBIDDEN_MEMO_DECISIONS` (`buy`, `sell`, `actionable`, `promoted`,
`validated`) before counting or rendering anything. This is intentionally
redundant with upstream validation: the memo is advisory-only and the discovery
lane is sandbox-only, so a rendered `buy`/`sell` would misrepresent the system's
authority regardless of which layer let it through. Filtered records are dropped
from the counts as well as the text, so the header cannot advertise a decision
the body refuses to show.

## Pipeline integration

| Layer | Detail |
|---|---|
| Wrapper | `scripts/run_daily_safe.sh` **Stage 10** — "Daily memo + email", `runpy.run_module('watchlist_scanner.daily_memo')` |
| Cadence | Daily, via the 09:00 UTC `run_daily_safe.sh` cron |
| Direct callers | `daily_memo.build_daily_memo` (line 2381, plain text) and `daily_memo.build_daily_memo_md` (line 2593, Markdown) |
| Guard | Both call sites are conditional on `discovery_data is not None` and wrapped in `try`/`except`; a raise is logged at WARNING and replaced with a one-line fallback stub |

The section is emitted after the tax/strategy line and before the simulation
review section, in both flavours.

## Input contract

The caller (`daily_memo._load_discovery_sandbox_data`) assembles the `data` dict
from four sandbox artifacts. If all four are empty it passes `None` and the
section is skipped entirely.

| `data` key | Source artifact | Namespace | Loaded by |
|---|---|---|---|
| `emerging` | `outputs/sandbox/discovery/emerging_candidates.json` | `SANDBOX` | `_shared._safe_load` (degrades to `{}`) |
| `rejected` | `outputs/sandbox/discovery/rejected_candidates.json` | `SANDBOX` | `_shared._safe_load` |
| `memory` | `outputs/sandbox/discovery/discovery_memory.json` | `SANDBOX` | `_shared._safe_load` |
| `approvals` | `outputs/sandbox/discovery/approval_decisions.jsonl` | `SANDBOX` | `daily_memo._load_discovery_approval_decisions`, which validates each line via `portfolio_automation/discovery/approval_workflow.py::is_valid_loaded_approval_record` and silently skips invalid/tampered records |

Fields actually consumed:

- **Candidates** (`emerging.candidates[]`, `rejected.candidates[]`): `status`
  (`watch` / `discovered`, case-insensitive), `ticker`, `score`,
  `corroboration_score`, `corroboration_level`, `event_type`, `risk_flag`,
  `evidence_snippets[]`, `rejection_reason`.
- **Memory** (`memory.entries[]`): `ticker`, `seen_runs`.
- **Approvals**: `decision`, `decision_reason`, `symbol`, `generated_at`.

Non-dict candidate entries are filtered out before any field access.

## Rendered output

Both builders produce the same information in two formats.

| Block | Condition | Content |
|---|---|---|
| Header + disclaimer | Populated path | Section title, the sandbox disclaimer, and a `WATCH / DISCOVERED / REJECTED` count line |
| Approval summary | `valid_approvals` non-empty | Total plus counts of `approve_for_research_review` and `needs_more_evidence` |
| Research candidates (WATCH) | Any WATCH | **Top 5 only**, with score, corroboration level + score, event type, a risk-flag marker, the **first** evidence snippet truncated to 120 chars, and the most recent matching approval decision (reason truncated to 80 chars, date to 10 chars) |
| Monitoring | Any DISCOVERED | **First 8 tickers**, then "…and N more." |
| Persistence | `memory.entries` non-empty | Up to 6 tickers each for `seen_runs > 1` (persistent) and `seen_runs == 1` (new this run), sorted |
| Operator research decisions | `valid_approvals` non-empty | **Last 5** decisions |
| Rejected / risk summary | Any rejected or risk-flagged | Rejected count, risk-flag count, and up to **3 unique** rejection reasons in first-seen order |
| Footer | Populated path | The research-lane disclaimer |

Approval-to-candidate matching iterates `reversed(valid_approvals)` and keeps the
first hit per uppercased symbol — i.e. **the most recent decision wins**.

### Collapsed path

When there are no WATCH, no DISCOVERED, no rejected candidates, no approvals
**and** no memory entries, the section collapses to a single line rather than
rendering an empty scaffold with a disclaimer that carries no signal:

- plain text: a bracketed header plus `No sandbox research candidates today.`
- Markdown: `## Discovery Research — Sandbox Only` plus
  `_No sandbox research candidates today._`

## Module API

```python
_build_discovery_section(data: dict[str, Any]) -> str      # plain text
_build_discovery_section_md(data: dict[str, Any]) -> str   # Markdown
```

Both are underscore-private by name but are **public by re-export** from
`watchlist_scanner.daily_memo`, which is how `daily_memo` and
`tests/test_daily_memo.py` reach them.

Contract for both: pure, total over any dict shape (every lookup is defensive —
`data.get(...) or {}`, `isinstance` filtering, `_flt` coercion with a `0.0`
default), returns a string, performs no I/O, and never mutates its input.

`_DISCOVERY_DISCLAIMER` is module-local; `_LINE`, `_flt`,
`_FORBIDDEN_MEMO_DECISIONS` (and the currently unused `_safe_load`) come from
`watchlist_scanner/memo/_shared.py` — they live there rather than in
`daily_memo` so extracted modules can use them without importing their former
home, which would be circular.

## Failure / degraded behavior

- **Missing or malformed sections of `data`** — absorbed. Missing keys become
  `{}` / `[]`, non-dict candidates are filtered, non-numeric scores become
  `0.0`, and missing tickers render `-` or `?`. The section narrows what it can
  say; it never raises.
- **Unreadable source artifacts** — handled one layer up: `_safe_load` returns
  `{}` with a WARNING, and a fully empty set yields `discovery_data is None`, so
  the section is omitted rather than rendered blank.
- **Invalid approval records** — dropped by
  `is_valid_loaded_approval_record`; a JSON-decode failure on one line skips
  that line only.
- **A raise inside either builder** — caught at the `daily_memo` call site,
  logged (`daily_memo: discovery section failed — …` /
  `… (md) failed — …`), and replaced by a stub reading "Discovery data
  unavailable (loading error)."

## Known limitations

- **Fixed, unconfigurable truncation.** Top-5 WATCH, 8 monitored tickers, 6
  persistence tickers, last-5 decisions, 3 rejection reasons, 120-char evidence,
  80-char reason. All are hardcoded; there is no "full detail" flavour from this
  module. That matches the memo's compact contract in `CLAUDE.md`, but it means
  the memo is not a complete view of the discovery lane — the GUI and
  `portfolio_automation/discovery/discovery_reports.py` are.
- **Only the first evidence snippet is shown** per candidate; the rest are
  loaded and discarded.
- **The two flavours are parallel implementations,** not one renderer with two
  formatters. The selection, filtering, and truncation logic is duplicated
  between `_build_discovery_section` and `_build_discovery_section_md`, so a
  behaviour change must be made twice. The 31 tests below cover both to keep
  them aligned.
- **Markdown heading level differs on the degraded path.** The normal Markdown
  path emits `## Discovery Research — Sandbox Only`; the exception fallback in
  `daily_memo.py` emits `### Discovery Research — Sandbox Only`. Memo heading
  depth is load-bearing for downstream section parsing, so this asymmetry is
  worth knowing about when reading a memo produced during a failure.
- **`_safe_load` is imported but unused** in this module — a leftover of the
  extraction, harmless but noted so a future reader does not infer that this
  module does file I/O.

## Tests

```
.venv/bin/python -m pytest -q "tests/test_daily_memo.py::TestDiscoverySectionPlainText" \
                              "tests/test_daily_memo.py::TestDiscoverySectionMarkdown"
# 31 passed
```

The tests import both builders from `watchlist_scanner.daily_memo` (the
re-export path), which is itself the regression guard that the split did not
break any caller. They cover the collapsed path, the count line, WATCH
truncation, evidence rendering, risk flags, approval matching and counting, the
forbidden-decision filter, memory persistence buckets, and the rejected/risk
summary — in both flavours.

## See also

- `docs/daily_memo.md` — the memo layer this section belongs to; lists
  `Discovery Research` among the memo's sections.
- `docs/OUTPUT_ARTIFACT_CONTRACTS.md` — the sandbox discovery artifacts.
- `CLAUDE.md` → "Output Contracts" — the compact-brief limits the memo honours.
