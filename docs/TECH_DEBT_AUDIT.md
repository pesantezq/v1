# Technical Debt Audit

**Date:** 2026-06-01 · **Scope:** full repository snapshot · **Method:**
automated scans (`grep`/`wc`/`find`) plus manual inspection of flagged sites.

> **How to read this.** Counts marked *(scanned)* come from repository-wide
> pattern scans and are accurate to the scan but coarse (a raw `except Exception`
> count says nothing about whether each one is justified). Items marked
> *(verified)* were opened and confirmed by hand. Nothing here changes protected
> scoring/decision logic; this is an assessment, not a patch.

---

## Executive summary

The codebase is large and disciplined: ~188K lines across **257 production
modules** and **170 test files**, with strong governance scaffolding (namespaces,
two-lane separation, observe-only defaults) and **clean secret hygiene**. The
debt that exists is the normal kind for a system that grew fast: a few unfinished
migrations marked in-code, very broad exception handling, several oversized
modules that are hard to test, a half-finished GUI migration, and a database
whose tables are currently empty. None of it is alarming; most is bounded and
addressable in priority order.

The five highest-value items, in order: (1) confirm the empty-DB producers are
actually wired, (2) tighten the broadest exception handlers in the operator-data
path, (3) finish the two marked migrations, (4) add tests around the largest
untested modules, (5) complete the `gui_v2` migration so two GUIs aren't
maintained at once.

---

## 1. In-code migration markers — `TODO(...)` *(verified)*

There are only **five genuine TODO markers in production code** (a repo scan also
matches `$X,XXX` dollar placeholders in `agent/prompts.py` and the detector regex
in `tools/repo_overview.py` — those are false positives, not debt). They fall
into two named, deliberate migrations:

**`v2-data-governance` — three writers still write files directly** instead of
going through `data_governance.safe_write_json()`:

| Location | What it is |
|---|---|
| `coverage_report_writer.py:28` | Coverage report writer bypasses governed namespace |
| `policy_evaluator/outcome_writer.py:23` | Policy outcome writer bypasses governed namespace |
| `profit_attribution/report_writer.py:31` | Profit-attribution report writer bypasses governed namespace |

*Why it's debt:* until these route through the safe writer, namespace isolation
isn't uniformly enforced, which is a prerequisite for clean multi-tenant /
sandbox separation. **Severity: Medium · Effort: M.** (The migration target API
already exists and is stdlib-only, so each is a small, low-risk change.)

**`v2-user-scope` — two aggregate SQL queries lack a `user_id` filter:**

| Location | What it is |
|---|---|
| `state_store.py:444` | Snapshot aggregation query is not user-scoped |
| `policy_evaluator/outcome_attributor.py:360` | Outcome attribution aggregation is not user-scoped |

*Why it's debt:* fine for single-user today, but a blocker (and a data-crosstalk
risk) the day multi-user is enabled. **Severity: Medium · Effort: M.**

> `portfolio_automation/data_governance.py:20` also mentions the
> `v2-data-governance` migration in its module docstring — that's a pointer, not a
> debt site.

---

## 2. Broad exception handling *(scanned + spot-verified)*

- **562** occurrences of `except Exception` across the codebase.
- **0** bare `except:` clauses — good; there are no untyped catch-alls.

A broad catch isn't automatically wrong (the daily pipeline deliberately wraps
non-critical stages so they can't crash the core run — see the observe-only
policy in `CLAUDE.md`). The debt is the subset that **swallows silently** —
returns an empty value or `pass` without logging *why* — because those turn real
failures into invisible "looks fine but empty" states.

Highest-density site to review first: **`gui_operator_data.py`** (the operator-UI
data aggregator), which concentrates many broad catches that return empty
dicts/None on failure; combined with its size and lack of tests (below) it's the
most likely place for a silent data-fetch failure to reach the operator unnoticed.

**Recommendation:** classify the 562 into (a) intentional pipeline-stage guards
(keep, but ensure each logs a reason), and (b) silent swallows (add logging or
narrow the exception). **Severity: Medium–High · Effort: M** (mechanical but
broad; do it module-by-module starting with `gui_operator_data.py`, `main.py`,
and the `agent/` modules).

---

## 3. Test-coverage gaps *(heuristic — confirm with a coverage run)*

170 test files against 257 production modules is healthy breadth, and **no tests
are disabled** (`pytest.mark.skip`/`xfail` not found). The gap is *absence* of
tests around a few large, high-traffic modules rather than broken tests. By
file-name heuristic, the notable candidates with no obvious dedicated test are
the largest modules in §4 — especially **`gui_operator_data.py` (2,184 LOC)**,
which pairs "no tests" with "many silent catches."

**Recommendation:** run `pytest --cov` to get real per-module coverage, then add
characterization tests for the top untested modules, `gui_operator_data.py`
first. **Severity: Medium–High (for the operator-data path) · Effort: M–L.**

---

## 4. Oversized modules (complexity hotspots) *(verified)*

| LOC | File | Note |
|---:|---|---|
| 7,181 | `gui/app.py` | Monolithic Streamlit app; hard to unit-test as one file |
| 3,180 | `main.py` | Pipeline entry point + orchestration in one module |
| 2,429 | `watchlist_scanner/daily_memo.py` | Memo synthesis |
| 2,184 | `gui_operator_data.py` | Operator-data aggregator (untested; see §2, §3) |
| 1,720 | `portfolio_automation/decision_engine.py` | **Protected** — restructure only with approval |
| 1,683 | `watchlist_scanner/scanner.py` | Scan orchestration |
| 1,599 | `agent/agent_runner.py` | LLM agent pipeline |
| 1,491 | `portfolio_automation/discovery/automatic_promotion_governance.py` | Promotion rules |
| 1,367 | `state_store.py` | SQLite wrapper (13-table schema; see §5) |
| 1,289 | `portfolio_automation/market_narratives.py` | Narrative synthesis |

*Why it's debt:* large files raise the cost of every change and review and make
targeted testing hard. *Caveat:* `decision_engine.py` is protected — don't split
it without explicit owner approval. **Severity: Medium · Effort: L** (refactor
`gui/app.py` and `main.py` into feature/orchestration submodules first; biggest
maintainability win).

---

## 5. Empty database tables *(verified — but read the nuance)*

`data/portfolio.db` defines **13 tables, all currently with 0 rows**:
`alert_events`, `cash_ledger`, `email_history`, `extended_watchlist`,
`portfolio_peaks`, `run_history`, `snapshots`, `structural_violations`,
`subsystem_health`, `theme_signals`, `watchlist_alert_outcomes`,
`watchlist_signal_feedback` (plus `sqlite_sequence`).

**Important nuance:** in this checkout there are also **no live artifacts** in
`outputs/latest/` and the FMP cache is empty — i.e., the pipeline has not been
run here. So empty tables most likely mean "no run yet in this clone," **not**
dead code. The audit action is therefore *verification*, not deletion:

1. Run the pipeline once (`bash scripts/run_daily_safe.sh` or a dry run) in a
   working environment.
2. Re-inspect the tables. Any that stay empty after a real run are genuine
   producer-wiring gaps or dead schema — *then* decide to wire or remove them.

**Severity: Medium · Effort: S** (mostly a verification task).

---

## 6. Parallel GUI implementations *(verified)*

Two dashboards coexist: legacy **`gui/`** (Streamlit, `app.py` at 7,181 LOC) and
new **`gui_v2/`** (FastAPI + HTMX + Jinja2, small and read-only). Maintaining both
duplicates effort and risks divergence in what each shows.

**Recommendation:** treat `gui_v2` completion as the path that *also* retires the
biggest file in the repo (§4). Track it against the roadmap; see
`docs/STREAMLIT_RETIREMENT.md`. **Severity: Medium · Effort: M.**

---

## 7. Documentation drift in `README.md` *(verified)*

The current `README.md` leads with Alpha-Vantage-first framing and a project tree
that predates the decision-engine / governance / `gui_v2` era, and several
PowerShell examples hardcode an **old path, `C:\PersonalWork\stock_bot\v1`**, while
the repo actually lives at `C:\PersonalWork\v1`. *Why it's debt:* a new reader is
oriented to a version of the system that no longer matches the code.

*Status:* **being addressed now** — the README is being refreshed into an
onboarding entry point as part of this same work, including correcting the stale
paths. **Severity: Low · Effort: S.**

---

## 8. Console output / observability hygiene *(scanned)*

**422** `print(` calls remain in the codebase. Many are intentional console
output for CLI/operator display, but a structured-logging discipline (logger
levels instead of prints in library code) would make production runs easier to
filter and monitor. **Severity: Low · Effort: M** (low priority; do opportunistically).

---

## 9. What is *not* a problem (verified clean)

- **Secrets:** no hardcoded API keys found; keys come from env (`FMP_API_KEY`,
  `ALPHA_VANTAGE_API_KEY`, `ANTHROPIC_API_KEY`), and logs are redacted.
- **No disabled tests**, no bare `except:`.
- **Governance scaffolding** (namespaces, two-lane, observe-only) is in place and
  consistently referenced.

---

## Prioritized remediation order

| # | Item | Severity | Effort | Why this order |
|---|---|---|---|---|
| 1 | Verify empty-DB producers (run once, re-check) | Med | S | Cheap; tells you if §5 is "no run yet" or real dead code |
| 2 | Add logging/narrowing to silent catches in `gui_operator_data.py` | Med–High | M | Removes the most likely "silent failure" path to the operator |
| 3 | Finish `v2-data-governance` (3 writers) | Med | M | Small, well-scoped; completes namespace enforcement |
| 4 | Tests for top untested modules (`gui_operator_data.py` first) | Med–High | M–L | Safety net before any refactor |
| 5 | Complete `gui_v2`, retire `gui/app.py` | Med | M | Ends dual maintenance; deletes the 7.1K-LOC monolith |
| 6 | `v2-user-scope` `user_id` filtering (2 queries) | Med | M | Do before enabling multi-user |
| 7 | Refresh `README.md` (in progress) | Low | S | Onboarding accuracy |
| 8 | Split `main.py` orchestration; logging over `print` | Low–Med | L | Long-term maintainability |

> All items are additive or non-behavioral except any future restructuring of
> `portfolio_automation/decision_engine.py`, which is **protected** and must not
> be touched without explicit owner approval (`CLAUDE.md`).
