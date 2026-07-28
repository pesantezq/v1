# Reliability Audit — Workstreams 13, 15, 16
Universe quality-screen bypasses · dead simulation experiments · quant-watch ledger taxonomy

Audited 2026-07-28, read-only, no files/DBs mutated. All findings below are **confirmed**
by reading source + running read-only snippets against real artifacts, except where marked
**inferred**.

---

## WS15 — Universe admission paths and quality-screen bypasses

### Path inventory

| # | Path | Admission checks applied | Bypassed checks |
|---|---|---|---|
| 1 | Static config list — `config.json:watchlist_scanner.watchlist` (22 symbols), default fallback `watchlist_scanner/config.py:DEFAULT_WATCHLIST` (20 symbols, `config.py:10-14`) | **None.** Config loading only runs `config/schema.py:41 normalize_symbol_list()` — uppercases, dedupes, and type-checks that each entry is a string. No market cap / revenue growth / PE / trend check. | mkt_cap, rev_growth, PE, FCF, trend (all of `candidate_scanner.py:_passes_hard_filters`) |
| 2 | FMP discovery / candidate scanner — `scanner/candidate_scanner.py`, instantiated `main.py:888` | Full hard-filter set in `_passes_hard_filters` (`candidate_scanner.py:240-289`): `mkt_cap >= min_mkt_cap` (5B), `rev_growth >= min_rev_growth` (15%, only if present), `pe <= 50` (only if present), `fcf_yield >= 0` (only if present), `price > priceAvg200` if `trend_filter_200dma` | None — this is the actual quality screen. Only scans `sp500_symbols` (an S&P 500 universe), so non-index tickers/ETFs are structurally never candidates here regardless of fundamentals. |
| 3 | `extended_watchlist` promotions — `watchlist_scanner/extended_watchlist.py` | Theme confidence >= 0.80, reinforcement (multi-theme OR "direct" source OR 3-day persistence), capacity `max_symbols=3`, TTL 7 days (`evaluate_candidates`, lines 175-291). `promote_operator_approved` (line 293) explicitly documents it "bypasses the multi-day-persistence / multi-theme gate ... but still respects every other gate." | No fundamental screen (mkt cap / rev growth / trend) at all — admission criterion is theme-confidence + persistence, a completely different quality bar than path 2. |
| 4 | Simulation-lane / durable overlay — `portfolio_automation/sim_governance/` production watchlist overlay, gated by `config.json:sim_governance.production_application.apply_watchlist_overlay` (default `false`); applied in `watchlist_scanner/__main__.py:219-233` via `production_overlays.load_production_watchlist` | Human approval (`promotion_approvals`/`record_approval`) + upstream evidence gates: `ready_for_production_review` thresholds keyed on `corroboration_score`/confidence (e.g. `simulation_lane.py:301` `score >= 0.80`), never mkt cap/rev growth/PE/trend. `promotion_proposals.py` has no market-cap-style gate (grepped — none found). | Fundamental screen entirely absent; gate is evidence-corroboration, not quality/liquidity. Ships default-OFF (`apply_watchlist_overlay=false`), so currently inert in production. |
| 5 | Manual/operator path — `ExtendedWatchlist.promote_operator_approved` (`extended_watchlist.py:293-327`) | Not-in-static, not-already-active, capacity cap (`max_symbols`) | Explicitly, by design, bypasses the persistence/multi-theme reinforcement gate. Never runs the fundamental screen either (no code path connects operator approval to `candidate_scanner`). |
| 6 | `scanner/fallback_watchlist.py` — activates only when FMP unavailable / no watchlist on disk (module docstring, lines 8-13) | None — `_DEFAULT_SYMBOLS` (20 mega-caps, lines 37-42) or `config.json:scanner.fallback_watchlist_symbols` inserted unconditionally; theme candidates blended in (`build()`, lines 116-195) with zero fundamental filtering. | Same as path 1 — fully bypasses `_passes_hard_filters`. Output is tagged `watchlist_source: "fallback"` so it's at least distinguishable (`fallback_watchlist.py:63,183,208`). |

### The known case — verified

`data/fmp_cache/profile_stable_MARA.json` (stored 2026-07-26): `marketCap: 4,620,998,496` (~$4.62B).
`config.json:scanner.min_mkt_cap = 5,000,000,000`. **MARA fails the market-cap screen** (4.62B < 5B).

Rechecked against the freshest cache, `data/fmp_cache/quote_stable_MARA.json` (stored 2026-07-28):
`marketCap: 4,487,553,820` (~$4.49B, further below the bar) **and** `price: 11.77 < priceAvg200: 12.0183`
— **MARA also fails the trend (200-DMA) filter today.** MARA is a static-watchlist double-failure, not
borderline.

### Every other static symbol checked against the live screen (2026-07-26/28 FMP cache)

| Symbol | Market cap | vs $5B floor | Note |
|---|---|---|---|
| AAPL | $4,891B | pass | |
| MSFT | $2,835B | pass | |
| NVDA | $5,010B | pass | |
| AMD | $851B | pass | |
| META | $1,511B | pass | |
| GOOGL | $3,870B | pass | |
| TSLA | $1,236B | pass | |
| AMZN | $2,497B | pass | |
| SMCI | $19.5B | pass | |
| AVGO | $1,817B | pass | |
| PLTR | $282B | pass | |
| COIN | $41.7B | pass | |
| **MARA** | **$4.62B → $4.49B** | **FAIL** | also fails trend filter (price < 200dma) |
| RIOT | $8.52B | pass mkt-cap | — flagged by an existing **manual** quant-watch probe (see WS16) as the actual driver of Financial-Services sector drag (1d hit-rate 20%, mean -13.1%); not re-checked here against rev-growth/PE/trend since fin_growth cache isn't populated for it (non-S&P-500 crypto miner, never scanned by path 2) |
| SPY/QQQ/XLE/XLF/XLK/IWM | ETFs | n/a | `isEtf: true` — the scanner's hard filters (mkt cap, rev growth, PE, FCF) are equity-oriented and the scanner only iterates `sp500_symbols`; ETFs are never members of that universe, so they structurally never encounter the screen (not a bypass so much as an out-of-scope asset class for path 2) |
| NASA/CHAT | ETFs (thematic, small AUM: $34.8M / $1.07B) | n/a | same as above; note neither has a `fin_growth_stable_*` cache file at all, confirming they've never been run through the fundamentals-scan pipeline |

**Verdict: MARA is the only static equity that would fail today's live discovery screen**, and it fails
on two independent filters (market cap and trend), not one.

### Provenance — does anything record which path added a symbol / which filters ran?

Partial, and the part that exists answers a different question than the one that matters:

- `watchlist_source` is a real, wired field (`watchlist_scanner/__main__.py:261-276`, `models.py:144`,
  consumed by `performance_feedback.py:42/87`, `outcome_reporting.py:90`, `postprocess.py:24`,
  rendered in `output_writers.py:269,380`). It tags each scan result as `"static"`,
  `"extended_theme"`, `"discovery:<theme_name>"`, `"fmp"`/`"fmp_cached"`, or `"fallback"`/`"fallback+themes"`.
- **What it does NOT do:** it never records *which admission checks ran* or *whether they passed*
  for a given symbol. `"static"` is stamped unconditionally at `__main__.py:276` (`else: r["watchlist_source"]
  = "static"`) with no reference to `_passes_hard_filters` at all — the two code paths never intersect.
  Even the `extended_watchlist` DB schema (`extended_watchlist.py:_DDL`, lines 35-51) stores promotion
  metadata (`theme_confidence`, `mention_count`, `drop_reason`) but nothing about mkt-cap/rev-growth/PE
  at time of promotion.
  `candidate_scanner.py` writes debug rows with `failed_filters` (line 105) *only* for symbols that
  went through `full_scan`/`weekly_refresh` — static/fallback/extended entries never generate a debug
  row, so there is no artifact anywhere that says "MARA was never screened" or "MARA would fail if
  it were."
- **This absence is itself the finding**: there is no artifact, DB column, or log line in the repo
  that answers "was this static symbol ever screened, and against what thresholds" — an operator
  auditing the watchlist today has no way to distinguish "passed the screen once" from "never
  screened, ever" other than manually doing what this audit did (cross-referencing `fmp_cache/profile_stable_*`
  against `config.json:scanner` by hand).

---

## WS13 — Dead simulation experiments

### 1. Read-only snippet run against real artifacts

```
BASELINE KEYS (portfolio_automation.sim_governance.simulation_lane.load_production_baseline('.')):
  watchlist: list, len=0
  advisory: list, len=48
  crowd: dict, keys=[...129 symbols...]
  flock: dict, keys=['report', 'watchlist_candidates', 'advisory_context']

EXPERIMENT CANDIDATE COUNTS (DEFAULT_EXPERIMENTS run against that baseline):
  experiment_watchlist_discovery_adds: 0 candidates
  experiment_watchlist_rerank: 0 candidates
  experiment_advisory_crowd_context: 46 candidates
  experiment_flock_intelligence: 20 candidates
```

Confirms exactly what was suspected: `load_production_baseline` (`simulation_lane.py:111-160`) never
sets `discovery_candidates` or `watchlist_ranked` — those keys are **absent from the dict entirely**
(not empty lists), so both experiments' `baseline.get(..., []) or []` (lines 282, 312) silently
resolve to `[]`. `watchlist` itself is also empty — it's sourced from `config.json:portfolio.watchlist`
(`load_production_baseline` line 129), which is `None` in the live config (the real static watchlist
lives at `config.json:watchlist_scanner.watchlist`, a different key `load_production_baseline` doesn't
read).

### 2/3. The wiring is more specific than "unwired" — it's a **container-key mismatch bug**, live since inception

There is a *second*, separate baseline-construction path that production actually runs:
`portfolio_automation/sim_governance/daily_governance_run.py:_enrich_baseline` (lines 80-109), invoked
at `daily_governance_run.py:158`:

```python
baseline = _enrich_baseline(root, simulation_lane.load_production_baseline(root))
```

`_enrich_baseline` DOES attempt to populate `discovery_candidates` from
`outputs/sandbox/discovery/automatic_promotion_candidates.json` (line 91-106):

```python
promo = _read(root / "outputs" / "sandbox" / "discovery" / "automatic_promotion_candidates.json")
cands: list[dict] = []
rows = (promo or {}).get("candidates", []) if isinstance(promo, dict) else []   # line 93 — BUG
```

**Verified real structure of `automatic_promotion_candidates.json`** (read live, 2026-07-28 run):
top-level keys are `generated_at, run_mode, run_id, observe_only, no_trade, ..., decision_count,
monitor_count, needs_review_count, rejected_count, expired_count, decisions, prohibited_actions_detected,
safety_disclaimer`. **There is no `candidates` key at all** — the confirmed audit note is correct. The
real container key is `decisions`, produced by `portfolio_automation/discovery/automatic_promotion_governance.py`
(`_report_to_dict`, line 1282: `"decisions": [_decision_to_dict(d) for d in report.decisions]`).

Field-level mapping is otherwise **compatible** — `PromotionDecision` (`automatic_promotion_governance.py:273-294`)
carries `ticker`, `evidence_score`, `corroboration_score`, `catalyst_flags`, `risk_flags`, which line up
exactly with what `_enrich_baseline` expects (`r.get("ticker")`, `r.get("corroboration_score",
r.get("evidence_score", 0.0))`, `r.get("catalyst_flags", [])`, `r.get("risk_flags")`). **The single
bug is the outer key**: `.get("candidates", [])` should be `.get("decisions", [])`. Because of this,
`baseline["discovery_candidates"]` is `[]` on every run regardless of whether the promotion-governance
producer emitted 0 or N decisions (today's run happened to have `decision_count: 0` anyway, so the
bug is currently unobservable from output alone — but the key mismatch means it would stay `[]` even
on a day with real decisions).

`watchlist_ranked`: `_enrich_baseline` line 108 just does `baseline.setdefault("watchlist_ranked", [])`
— **no producer is wired for it at all**, unconditionally empty. No file in the repo populates a
`watchlist_ranked`-shaped artifact for consumption here; the only place the *name* is written is
`outputs/sandbox/sim_governance/simulation_candidates.json:4461` and
`outputs/simulation/daily_simulation_bundle.json:3278` — both of which are **downstream outputs of this
same experiment**, i.e., they're empty because the experiment is empty, not upstream sources. No
"ranked watchlist" producer (a resolver that ranks the current watchlist by some signal) exists
anywhere in the codebase — confirmed by repo-wide grep, only the four hits above.

### 4. Does any health check report these as having run successfully despite zero admissible input?

**Yes — confirmed by direct quote.** `.claude/commands/daily-tool-analysis.md:164`:

```
`sim_gov_candidates` = `bundle` stage candidate_count
```

and the dispatch line at `daily-tool-analysis.md:382` renders `"Sim-gov: lane {active|disabled} ·
{sim_gov_candidates} cand. · ..."`. This is the **aggregate** `candidate_count` from
`simulation_lane.py:489` (`len(candidates)` across all 4 experiments in `DEFAULT_EXPERIMENTS`), which
in the live run is `46 (crowd_context) + 20 (flock) + 0 (discovery_adds) + 0 (rerank) = 66`. The health
check only ever sees `66`, a healthy-looking non-zero number contributed entirely by the two *working*
experiments — it has no per-experiment breakdown, so `experiment_watchlist_discovery_adds` and
`experiment_watchlist_rerank` being permanently zero is **structurally invisible** to the health check
and always will be as long as the other two experiments keep firing. `daily_governance_run.py:161-165`
similarly reports `status["stages"]["simulation_lane"] = {"ok": True, "candidate_count": ...}` — `ok`
is `True` whenever the stage doesn't raise, independent of whether any given experiment yielded
candidates.

Test coverage exists but doesn't catch this either: `tests/test_sim_governance.py`,
`tests/test_flock_sim_governance.py`, and `tests/crowd_intelligence/test_unified_sim_governance.py` all
call `experiment_watchlist_discovery_adds`/`experiment_watchlist_rerank` directly against **hand-built
baseline fixtures** that inject `discovery_candidates`/`watchlist_ranked` manually — none of them
exercise `_enrich_baseline` or `load_production_baseline` against a real/representative
`automatic_promotion_candidates.json`, so the container-key bug has no test that would catch it.

### 5. Git history — did this ever work?

No. `git blame` on the offending line:

```
66218b393 (Enrique Pesantez 2026-06-16 01:49:04 +0000) rows = (promo or {}).get("candidates", []) ...
```

`_enrich_baseline` was introduced whole in commit `66218b39` ("feat(sim-governance): two-lane
active-simulation + human-gated production promotion", 2026-06-16) and has not been touched since
(no later commit in `daily_governance_run.py`'s history — `07ca6c8f`, `aac7fed0`, `3d27cef4`,
`38fc0422` — touches this function). The producer it's supposed to read,
`automatic_promotion_governance.py`, already existed and already used the `"decisions"` container key
since its own first commit, `ae8c4105` (2026-05-11 — over a month **before** `_enrich_baseline` was
written). So this was never a "used to work, drifted" case — **it was wired to the wrong key from the
day it was authored**, and has silently returned zero for both watchlist experiments for its entire
existence (2026-06-16 → today, 2026-07-28, ~6 weeks).

---

## WS16 — Quant-watch ledger taxonomy

Source: `portfolio_automation/quant_watch_probes.py`, live ledger `data/quant_watch_ledger.json`
(schema_version 1, 2 active, 9 archived at audit time).

### 1. Detectors + current fields

Three automated detectors + one manual class:
- `DETECTOR_PRIOR_GAUGE = "prior_gauge_underperformance"` (`quant_watch_probes.py:31`, detect
  fn `detect_prior_gauge_underperformance` lines 141-189)
- `DETECTOR_NEG_RETURN = "negative_mean_return_persistence"` (line 32, lines 229-255)
- `DETECTOR_SECTOR_DRAG = "sector_drag"` (line 33, lines 316-347)
- `DETECTOR_MANUAL = "manual"` (line 34) — no detector function; registered by hand, no evaluator
  (`_EVALUATORS` dict, lines 376-380, has no `"manual"` key)

Fields an active probe carries today (from live ledger + `_active`/probe-builder shapes):
`id, detector, lens, scope_key, created_at, created_run, severity, concern, trigger_snapshot,
resolve_hint, last_evaluated_at, observations[]` (each observation: `run`/`at`, plus detector-specific
metric snapshot, and free-text `note` on manual probes). One manual probe in the live ledger
(`manual:regime_classifier_neutral_collapse`, archived) additionally carries an ad hoc `owner` field
("regime-classifier owner (market_regime.py)") — proving the schema is a loose dict (nothing enforces
required keys), so extra fields can be hand-added but aren't consistently present across probes.
Archived probes add: `resolved_at, resolved_run, resolution, resolution_detail, lifetime_days`.

### 2. Current escalation rule

**Escalation to RED exists ONLY for `DETECTOR_PRIOR_GAUGE`.** `_eval_prior_gauge` (lines 192-224)
escalates when `delta_pre <= -PRETRACKER_RED_GATE_PP` (10pp) **and** `resolved >= MIN_RESOLVED_1D`
(30) — i.e., persistence (n>=30) + impact magnitude (>=10pp) does drive escalation, but *only* for this
one detector. `_eval_neg_return` and `_eval_sector_drag` (lines 258-278, 350-371) have **no escalation
branch at all** — they can only ever transition `active → active` or `active → resolved`, never
`active → escalated`, no matter how severe or persistent (`overall_status`, lines 458-463, only
returns RED if *any* transition is `ESCALATED`). `MAX_PROBE_AGE_DAYS = 60` (line 41) applies uniformly
to all three detector types and auto-**resolves** (not escalates) any probe past that age via
`"ttl_expired"` (lines 214-215, 269-270, 368-369) — this is a pure age-based silent close, independent
of whether the underlying concern is fixed. So today's actual rule is: **persistence+impact escalation
only for D1; TTL-based auto-resolution (age alone, no verification the issue resolved) for all three;
no explicit "trust-boundary severity" dimension anywhere** (all three detectors hardcode
`severity: AMBER` — there's no severity tier above AMBER short of the binary ESCALATED transition).

### 3. Fields needed for the richer taxonomy — what exists vs. missing

| Proposed field | Status | Note |
|---|---|---|
| Stable concern ID | **Partial** | `id = f"{detector}:{scope_key}"` (or fingerprint-suffixed for D1) is stable *within* a gauge era, but D1's id embeds `current_fingerprint` — a gauge rotation mints a brand-new id and the old one resolves via `"scope_changed"`, so identity does not survive the event the program most wants tracked across (regime/gauge changes). |
| Concern class | **Exists** (`detector` + `lens`), but only 3 automated classes + one catch-all `manual` | |
| First/last observed | **Exists** — `created_at` / `last_evaluated_at` | |
| Consecutive observations | **Partial** — `observations[]` trail exists (capped at `MAX_OBSERVATIONS=14`, line 42) but there is no explicit counter field; a caller must count trail entries, and the trail is a FIFO cap so long-lived probes lose the true total. | |
| Evidence artifact | **Partial** — `trigger_snapshot` inlines raw metric values but there is no field pointing at the *source artifact path* (e.g. `outputs/latest/retune_impact.json`) the way manual probes' free-text sometimes does informally. | |
| Affected component | **Missing** — `scope_key` is a fingerprint or sector/regime label, not a named system component/producer. Only the one hand-authored manual probe (`regime_classifier_neutral_collapse`) names a component, in free text within `owner`, not a structured field. | |
| Escalation threshold | **Missing as a stored field** — thresholds are hardcoded module constants (`PRIOR_GAUGE_FIRE_PP`, `PRETRACKER_RED_GATE_PP`, `SECTOR_MIN_N`, etc., lines 38-43), never recorded on the probe itself, so a probe doesn't self-document what would have escalated it. | |
| Owner | **Missing as a schema field** — present ad hoc on exactly one manual probe, absent from all detector-generated probes and the dataclass/dict shape. | |
| Remediation status | **Missing** — only a binary `active`/archived (`resolved`/`escalated`) state; no in-progress/queued/wontfix taxonomy. | |
| Closure evidence | **Partial** — `resolution` + `resolution_detail` capture the closing rationale as free text at archive time, but there's no structured "evidence artifact + snapshot value at closure" field distinct from that prose. | |
| Regression-test reference | **Missing entirely** — no field, no convention, nothing in the module links a resolved probe to a test that would catch recurrence. | |

### 4. Are manual probes ever auto-resolved?

**No — confirmed in code.** `evaluate()` (lines 397-410): `ev = _EVALUATORS.get(probe.get("detector"))`;
for `"manual"` this is `None` (the `_EVALUATORS` dict only maps `DETECTOR_PRIOR_GAUGE`,
`DETECTOR_NEG_RETURN`, `DETECTOR_SECTOR_DRAG` — line 376-380), so the fallback fires: `out.append(_active(probe,
"manual — operator clears", now_iso, None))`. A manual probe always re-enters `active` on every run,
regardless of age (the `MAX_PROBE_AGE_DAYS` TTL check lives inside the three detector-specific
evaluators, which manual probes never reach) — it can only leave the ledger via an operator manually
editing `data/quant_watch_ledger.json` to move it to archive. This matches the documented behavior
(module docstring: "auto-archives it on resolution / scope-change / escalation" — manual is the
explicit exception) and the two manual probes currently in `active` (`financial_services_sector_drag_5687885c`,
created 2026-07-15, last evaluated today; `regime_coverage_gap_5687885c`, created 2026-07-27) confirm
this in the live ledger — both are >13 days and >1 day old respectively with `last_evaluated_at` bumped
to today, i.e., neither TTL nor auto-resolution has touched them.

---

## Summary of confirmed-vs-inferred

All WS15 market-cap/trend numbers, the WS13 baseline/experiment counts, the WS13 container-key bug and
its git-blame dating, and the WS16 ledger contents/escalation logic are **confirmed** by direct
read/execution against the live repo on 2026-07-28. The only **inferred** point: whether
`experiment_watchlist_discovery_adds` would in fact receive nonzero rows on a day `decision_count > 0`
in `automatic_promotion_candidates.json` — not observed directly (today's file has `decision_count: 0`),
but the container-key mismatch (`"candidates"` vs `"decisions"`) guarantees zero regardless, so this is
a structural conclusion from the code, not a live-data observation.
