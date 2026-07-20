# Design: Memo Datasets Separation

**Date:** 2026-07-20
**Status:** Approved (brainstorming) — pending spec review
**Author:** Claude Code (with operator)

## Problem

The daily investment memo is a single monolithic artifact (`outputs/latest/daily_memo.txt` / `.md`, ~18 sections) that mixes distinct domains — portfolio/capital, crowd/watchlist, institutional (13F), risk, and system/ops — into one long read. An operator who cares about only one domain must scan the whole thing, and no consumer (GUI, email, analysis agents) can take a single domain's data independently. The operator wants the memo separated into **domain datasets** (crowd/watchlist vs portfolio vs institutional vs risk, etc.).

## Goal

One structured per-domain **dataset** as the source of truth, feeding multiple surfaces:
- per-domain rendered **briefs** (Markdown/text),
- per-domain **GUI sub-tabs**,
- the existing **combined memo** kept as an email roll-up.

"One dataset → many surfaces." (Operator choice, 2026-07-20.)

## Non-goals / constraints

- **Observe-only, additive.** No recompute of any decision/score/allocation; `feeds_decision_engine=false`; never writes `decision_plan.json`; never mutates production/simulation state.
- **No rewrite of `daily_memo.py`** (3,500 lines, ~50 passing tests, protected renderer). The combined `daily_memo.md` is unchanged.
- **No new data.** The datasets are a *pure reassembly* of fields already emitted by canonical producers — so a dataset can never drift from its source producer.
- No unrelated refactoring.

## Approach (A — additive dataset producer)

Chosen over (B) refactoring `daily_memo` into per-domain modules — too large/risky for the same user-visible result — and (C) GUI-only tabs — produces no machine-consumable datasets. Approach A leaves a clean path to later migrate `daily_memo` to consume the datasets (optional follow-up, not in scope).

A new observe-only producer reads the existing memo/producer artifacts and reorganizes them into domain-keyed datasets; renderers and GUI derive from that dataset. Mirrors the established `capital_plan_view` / `institutional_memo` pattern.

## Architecture

### Unit 1 — `portfolio_automation/memo_datasets.py` (new)

Placed alongside its closest analog `capital_plan_view.py` (an observe-only,
read-only reassembly module), not in `watchlist_scanner/` (which holds the
`daily_memo.py` renderer being left untouched).

Pure functions + a non-raising runner:

- `build_memo_datasets(sources: dict) -> dict` — pure. Takes already-loaded source artifacts, returns the domain-keyed dataset. No I/O, no recompute.
- `render_domain_brief(dataset: dict, domain: str, *, markdown: bool) -> list[str]` — pure. Renders one domain's brief lines.
- `run_memo_datasets(root=".", *, write=True) -> dict` — loads source artifacts, builds the dataset, optionally writes `memo_datasets.json` + per-domain briefs. Wrapped in try/except → degraded dict; never raises into the pipeline.

**What it does / how you use it / what it depends on:**
- *Does:* reorganize existing memo-producer fields into 5 domain datasets + render per-domain briefs.
- *Use:* call `run_memo_datasets(root)` (pipeline) or `build_memo_datasets(sources)` (tests).
- *Depends on:* existing artifacts only — `system_decision_summary.json`, `decision_plan.json`, `memo_coherence.json`, `daily_capital_plan.json`, `risk_delta.json`, `correlation_risk_advisor.json`, `unified_crowd_intelligence_status.json`, `watch_candidates.json`, `institutional_intelligence.json`, `daily_run_status.json`. Read via `_safe_load`; every one is optional (missing → that section degrades honestly, never crashes).

### Artifact — `outputs/latest/memo_datasets.json`

Observe-only envelope + domain map:
```json
{
  "schema_version": "1",
  "source": "memo_datasets",
  "generated_at": "…",
  "observe_only": true,
  "no_trade": true,
  "feeds_decision_engine": false,
  "domains": {
    "portfolio":       {"headline": "…", "status": "ok|degraded|unavailable",
                         "sections": [{"title": "…", "lines": ["…"], "severity": "info"}],
                         "source_artifacts": ["…"], "warnings": []},
    "crowd_watchlist": {"…"},
    "institutional":   {"…"},
    "risk":            {"…"},
    "system":          {"…"}
  }
}
```
A `section` is `{title, lines[], severity}`. `domains.<d>.status` is `unavailable` when its source artifacts are absent (e.g. institutional inert).

### Domain mapping (deterministic, config-tunable via `domains` list)

| Domain | Source artifacts | Sections |
|---|---|---|
| `portfolio` | daily_capital_plan, decision_plan, portfolio_snapshot, system_decision_summary | Verdict, Today's Capital Plan, What To Do Today, Funded/Deferred/Sell, Bottom Line, Portfolio Pulse, Growth |
| `crowd_watchlist` | system_decision_summary (top_theme), unified_crowd_intelligence_status, watch_candidates | Top Insight/themes, Unified crowd (retail + market context), Watchlist candidates, Top Movers |
| `institutional` | institutional_intelligence(.json) | Consensus by symbol, filing age, effective managers, crowding (inert-aware: `unavailable` until activated) |
| `risk` | risk_delta, correlation_risk_advisor | Risk Delta, concentration/leverage, correlation, VaR, Risk Focus |
| `system` | daily_run_status, retune_impact, advisor artifacts | What Changed, Advisor Stack, System/Data Health, Operator appendix |

### Unit 2 — Surfaces

1. **Briefs** → `outputs/latest/memo/<domain>_brief.md` (+ `.txt`) via `render_domain_brief`. Each independently scannable; each carries a one-line "observe-only / no funded-action override where applicable" footer.
2. **GUI** → `/dashboard/memo` gains a domain sub-tab bar (Portfolio / Crowd & Watchlist / Institutional / Risk / System). A new loader `gui_v2/data/dash_memo_datasets.py::collect_memo_datasets_view(root)` reads `memo_datasets.json`; the existing memo route/behavior is preserved (additive tab bar). Null-tolerant.
3. **Combined roll-up** → `daily_memo.md` unchanged (email path via Stage 10).

### Data flow

```
canonical producers (decision_plan, memo_coherence, capital_plan_view,
  risk_delta, unified_crowd, institutional_intelligence, daily_run_status …)
        │  (read-only)
        ▼
memo_datasets.build_memo_datasets  →  memo_datasets.json (source of truth)
        ├─ render_domain_brief → outputs/latest/memo/<domain>_brief.md
        ├─ GUI /dashboard/memo domain sub-tabs
        └─ (daily_memo.md combined roll-up — unchanged)
```

### Pipeline wiring

New non-blocking **Stage 10c** in `scripts/run_daily_safe.sh`, AFTER Stage 10 (Daily memo) so the memo-producer artifacts are fresh. Uses `run_aux_stage` (WARN-not-abort). Added to `scripts/preflight.sh` compile + import lists.

### Registry / health

- Register `memo_datasets.json` (+ the per-domain brief artifacts, or the dir) in `artifact_registry.yaml` (lens `market_discovery`/`decision_core`, role `narrative`, cadence `daily`, consumers `[gui_operator_data, daily-tool-analysis]`, `required: false`, severity `info`).
- Health: the always-on `portfolio-memo-reviewer` reviews the briefs; add a content-liveness entry (`memo_datasets` fresh-but-empty: `generated_at` today but all domains `unavailable` while their sources exist → warn).
- daily-tool-analysis: add a one-line consumer heartbeat reading `memo_datasets.json` domain statuses (read-only; no mutator involvement).

## Error handling

- `run_memo_datasets` never raises; on any failure returns `{"status": "error", …}` and writes a degraded artifact with all domains `unavailable`.
- Each domain builds independently; one domain's missing source degrades only that domain (`status: unavailable` + a warning), never the others.
- GUI loader falls back to the existing single-memo view if `memo_datasets.json` is absent.

## Testing

`tests/test_memo_datasets.py` (mirror `test_capital_plan_view.py`):
1. build produces all 5 domains from synthetic sources.
2. missing source artifact → that domain `unavailable`, others intact.
3. institutional inert → `institutional` domain `unavailable` (not error).
4. `feeds_decision_engine=false` + `observe_only` in the envelope.
5. no-mutation: inputs unchanged after build; `decision_plan.json` never written.
6. deterministic + idempotent build.
7. render_domain_brief produces non-empty lines for a populated domain; `[]` for `unavailable`.
8. config `domains` list filters which datasets are emitted.
9. per-domain brief files written to `outputs/latest/memo/`.
10. GUI loader shapes domains for display; null-tolerant when artifact absent.
Plus regression: existing `tests/test_daily_memo.py`, `tests/gui_v2/` (memo route unchanged).

## Config (`config/base.json:memo_datasets`)

```json
{ "memo_datasets": {
    "enabled": true,
    "domains": ["portfolio","crowd_watchlist","institutional","risk","system"],
    "write_briefs": true } }
```
Conservative defaults; absent key → all defaults (safe).

## Rollout

Additive and observe-only — safe to ship enabled. The GUI sub-tabs appear after a dashboard restart. The combined memo and all existing consumers are unaffected. Optional future follow-up (out of scope): migrate `daily_memo.py` to render its sections from `memo_datasets.json` (single rendering path), which would retire the parallel derivation.

## Governance statement

No change to `decision_engine.py`, the six protected scores, allocations, brokerage, or production/simulation state. `feeds_decision_engine=false`. `decision_plan.json` never written. Read-only reassembly of existing artifacts.
