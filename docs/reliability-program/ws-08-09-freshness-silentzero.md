# Reliability Audit — WS8 (Freshness Contract) + WS9 (Silent-Tie Ranking)

Read-only audit. No files under `outputs/`, `data/`, or elsewhere were modified.
Date of audit: 2026-07-28. Repo: `/opt/stockbot`, branch `main`.

All findings below are labeled **CONFIRMED** (verified by reading code and/or
running read-only Python snippets against real artifacts) or **INFERRED**
(reasoned from code but not directly executed/observed).

---

## WS8 — Freshness contract

### F1. `generated_at`/`created_at` coverage is good; a real "source-data-through" or "market-session" field essentially does not exist — CONFIRMED

Scanned all 148 JSON files under `outputs/latest/`, `outputs/sandbox/`,
`outputs/policy/`, `outputs/promotion_review/`:

- 135/148 (91%) carry `generated_at` or `created_at`.
- 11 files carry neither (`outputs/latest/decision_holdings_source.json`,
  `market_opportunities.json`, `scraped_intel_run_summary.json`,
  `outputs/sandbox/discovery/social_sentiment_simulation_adjustment.json`,
  `social_sentiment_status.json`, `outputs/sandbox/regime_collapse_validation.json`,
  `outputs/policy/active_strategy_selection.json`,
  `calibration_correction_proposal.json`, `run_manifest.json` (has `data_as_of`
  instead), `signal_tagging_proposal.json`, and a `portfolio_backups/config.*`
  snapshot).
- Only **5 files** carry anything resembling a "data-through" concept, all
  using the key `data_as_of`: `outputs/latest/institutional_consensus.json`,
  `institutional_intelligence.json`, `institutional_intelligence_status.json`,
  `outputs/sandbox/daily_input_snapshot.json`, `outputs/policy/run_manifest.json`.
- **Zero** files use any of `last_completed_session`, `market_session`,
  `trading_session`, `session_date`, `last_trading_day`.

Critically, `data_as_of` is **not** a market-session concept — it is a
wall-clock capture timestamp. In `run_manifest.json` and
`daily_input_snapshot.json`, `data_as_of` = `"2026-07-28T09:02:36.99Z"` — the
moment the run started reading inputs, not "the last close this data reflects."
`decision_plan.json:lineage.data_as_of` follows the same convention
(`"2026-07-28T09:02:36.996762+00:00"`, matching the manifest exactly).
`institutional_consensus.json:data_as_of = "2026-07-28"` is closer to a
session-date but is scoped to that one 13F subsystem only, not a repo-wide
convention.

**Conclusion:** there is no field anywhere in the artifact corpus that answers
"what is the last completed trading session this data reflects" as distinct
from "when did the process that wrote this file run." A pipeline that runs at
09:00 UTC (pre-market, ~4:00am ET) and a pipeline that runs post-close both
stamp `generated_at`/`data_as_of` as "now" with no way to tell, from the
artifact alone, whether the underlying prices are today's close, yesterday's
close, or a stale weekend/holiday snapshot.

### F2. `decision_plan.json:lineage.freshness` is wall-clock-threshold-based, not calendar-aware — CONFIRMED

`portfolio_automation/decision_plan.json` (verified via
`outputs/latest/decision_plan.json`) carries a `lineage` block:
```
{"run_id": "...", "data_as_of": "...", "producer": "decision_engine",
 "source_commit": "...", "config_hash": "...", "upstream_refs": ["run_manifest.json"],
 "quality": "ok", "freshness": "fresh"}
```
Tracing `freshness` to its source, `portfolio_automation/daily_input_snapshot.py:127-153`
(`_evaluate_source`) computes it as:
```python
age_hours = (now_dt - obs_dt).total_seconds() / 3600.0
...
elif age_hours is not None and age_hours > src.stale_after_hours:
    quality = freshness = "stale"
else:
    quality, freshness = "ok", "fresh"
```
`src.stale_after_hours` defaults to **26.0 hours** (`daily_input_snapshot.py:50`)
for most daily inputs (holdings, portfolio_snapshot, decision_baseline,
decision_holdings_source, news, crowd_unified, source_health), with per-source
overrides up to 8760h (config, active_overlays) for genuinely slow-moving
inputs. This is a flat wall-clock window with **no NYSE-holiday or weekend
awareness at all** in this module — it happens not to false-positive across a
normal weekend only because `run_daily_safe.sh`'s cron (`0 9 * * *`, no
day-of-week restriction — confirmed in `crontab -l`) re-runs and re-stamps
every one of these artifacts every single day, including weekends and
holidays. If any daily producer in this list were ever skipped on a weekend
(the way `historical_backfill` and `discovery_pulse`'s weekend cadence
already are, by design), the flat 26h threshold would misclassify a
Friday-stamped artifact as "stale" by Saturday afternoon — a false positive —
or, in the opposite direction, would call a truly-stale holiday snapshot
"fresh" simply because the process happened to re-run and re-stamp it that
morning even though the underlying prices didn't move. Neither direction is
guarded against by session awareness; only by the accident that the daily
cron runs unconditionally 7 days/week.

### F3. The only calendar-aware code in the repo is private to one module and is never reused — CONFIRMED

Grepped the full repo (excluding `.venv`) for `pandas_market_calendars`,
`market_calendar`, `trading_calendar`, `is_market_holiday`, `is_trading_day`,
`last_trading_day`, `next_trading_day`, and holiday-list patterns. The **only**
hit is `portfolio_automation/resolution_due_probe.py:90-121`
(`_NYSE_HOLIDAYS: frozenset[date]`), a hardcoded set of NYSE closures from
2025-01-01 through **2027-12-24 only**, used exclusively inside
`_trading_days_elapsed()` (`resolution_due_probe.py:129-149`) to compute
trading-day age for the "decisions due for resolution" probe. Grepped for
importers of `_NYSE_HOLIDAYS` or any function from this module elsewhere in
the repo (excluding tests) — **zero** hits. It is private (`_`-prefixed),
unexported, and not reused by `daily_run_status.py`, `artifact_registry.py`,
or `daily_input_snapshot.py`, all three of which independently reimplement
their own flat wall-clock freshness windows (30h/100h/192h/768h/9000h in
`artifact_registry.py`; 26h/720h/2160h/8760h in `daily_input_snapshot.py`;
an ad-hoc 840-minute tolerance for the discovery-pulse check in
`daily_run_status.py:104-124`, whose own docstring explains it was hand-tuned
to "the overnight window ... observed as ~13.25h at the 09:15 daily check").

**Finding, stated plainly per the audit brief's instruction:** there is no
shared market-calendar/trading-session helper in this codebase. The one
holiday-aware routine that exists is scoped to a single probe, hardcoded
through end-2027 (a landmine: after 2027-12-24 it silently reverts to
weekday-only counting with no holidays, no warning, no test guarding the
degradation), and not designed or exposed for reuse as the freshness
primitive the rest of the system would need.

### F4. A designed cross-artifact coherence check exists, is tested, and is never called — CONFIRMED

`portfolio_automation/run_manifest.py:210-220` defines
`coherent_run_ids(expected_run_id, artifacts) -> bool`, which returns `True`
only if every artifact in a list carries `run_id == expected_run_id` (an
artifact missing `run_id` is treated as non-coherent by design — see its
docstring: "an unstamped input is either legacy or from a different run and
must not be silently combined with a fresh production run"). This is exactly
the mechanism WS8 needs — a daily consumer silently combining today's
`decision_plan.json` with a stale/mixed-run `top100_monthly.json` is precisely
the "mixed-run" case this function was written to catch.

Grepped the whole repo for `coherent_run_ids`: it is exported in
`run_manifest.__all__`, unit-tested in `tests/test_run_manifest.py:119-126`
(`test_coherent_run_ids_accepts_matching_and_rejects_mixed`), and **called
from nowhere else in the codebase** — not `daily_run_status.py`, not
`artifact_registry.py`, not `main.py`. `daily_run_status.py:543-549` reads
`read_manifest`/`is_complete` (whether *the* run finished), but never calls
`coherent_run_ids` to check whether the artifacts it's reporting on actually
belong to that run. This is dead code implementing a real safety property
that nothing in production exercises.

### F5. `artifact_registry_status.json` / its validator: what it checks and what it explicitly does not — CONFIRMED

`portfolio_automation/artifact_registry.py` classifies every artifact in
`artifact_registry.yaml` (141 rows) into present/stale/idle/missing/invalid
using **file mtime only** (`is_stale(row, age_hours)` at `:73-75`, driven by
`CADENCE_MAX_AGE_HOURS = {"daily": 30, "weekend": 100, "weekly": 192,
"monthly": 768, "yearly": 9000, "on_demand": None}`, `:58-61`). It does:
- Compare `now - mtime` against a per-cadence threshold (each artifact's own
  declared cadence).
- Track `idle_ok` rows (append-only event logs) so a quiet day isn't
  misclassified as a broken producer (`:78-90`).
- Roll up `unjustified_debt` / `by_consumer_status` for the doc/artifact
  governance program.

It does **not**:
- Check content for freshness relative to a market session — mtime is the
  only signal; a file rewritten with byte-identical stale content still
  counts as fresh.
- Know that a "weekly"-cadence artifact might be read by a daily-cadence
  consumer expecting daily-fresh data — cadence is a property of the
  *producer/artifact*, never of the *reader*. Nothing in the registry or its
  validator models "who reads this and how fresh do they need it."
- Detect the WS9 zero-variance/tie condition (see below) — `_check_top100_daily`
  (`daily_run_status.py:134-141`) only checks `len(candidates) > 0`.

`daily_run_status.py`'s `content_liveness` section (`scan_content_liveness`,
`:475-509`) is the deeper of the two checks: it runs a per-artifact predicate
(`_CONTENT_LIVENESS_CHECKS`) that inspects payload *content* (e.g.
`_check_top100_daily` counts candidates; `_check_scraped_intel_degraded`
checks a degraded flag). This is real content inspection, not mtime — but
every existing predicate checks "is there *something* non-empty here," never
"is the *distribution* of values informative" (no check anywhere asks "are
all scores identical / all zero / zero-variance"). See F-WS9-4 below — this
absence is itself a finding.

### F6. Producer/consumer cadence-mismatch inventory (cross-referenced crontab vs. producers)

Crontab (`crontab -l`, confirmed):
```
0 9 * * *          run_daily_safe.sh        (daily, 7 days/week, no calendar gate)
15 9 * * 1-5       daily_check.sh           (weekdays only)
0 11,15,19,23 * * 1-5 / 0 12,20 * * 6,0  discovery_pulse.sh  (4/day weekday, 2/day weekend)
0 8 * * 1          run_weekly_safe.sh       (Monday only — rebuilds top100_watchlist + top100_weekly/monthly)
30 9 1 * *         monthly_check.sh
0 10 1 1 *         yearly_check.sh
15 10 1 * *        pattern_loop_check.sh
50 9 1 * *         pattern_loop_reconstruct.sh
0 7 * * 1          run_doc_audit.sh
45 9 * * *         run_sims_daily.sh
30 8 * * 1         run_weekly_etf_bundles.sh
```

- **The one case named in the audit brief (`top100_monthly.json` produced
  weekly, feared read by a daily gate) does NOT currently exist as a live
  code path** — CONFIRMED. `artifact_registry.yaml` lists its sole consumer
  as `monthly-tool-analysis` (a monthly-cadence skill — matching cadence
  correctly), and a repo-wide grep for `top100_monthly` turns up no daily
  script or daily-cadence Python module reading it directly today. The
  registry's own `consumers:` list is self-declared, not code-verified,
  but the independent grep for the literal string across `*.py`/`*.sh`
  confirms no daily consumer exists in current code.

- **However, a same-day design doc (`docs/superpowers/specs/2026-07-28-watchlist-remove-producer-design.md`,
  status: "approved," not yet implemented — no corresponding test files
  exist yet on disk) is about to *introduce* exactly this pattern
  deliberately and knowingly**: it wires `sim_governance/simulation_lane.py`'s
  new `experiment_watchlist_decay_removals` (part of the **daily** sim-gov
  lane, Stage 10e — this producer runs as part of the daily cron chain) to
  read `outputs/latest/top100_monthly.json`, which is refreshed only on
  Mondays. The design doc explicitly acknowledges the mismatch in its own
  comment (`load_production_baseline`, planned addition): *"The MONTHLY
  cadence is required: build_top100_daily uses lookback_days=1, which zeroes
  the recent-hit-rate weight, so the daily artifact cannot discriminate."*
  This is a **deliberate, documented cadence mismatch** (daily consumer,
  weekly-refreshed evidence, 6 days out of 7) chosen as a workaround for the
  WS9 defect rather than a fix to it. It is a reasonable engineering choice
  given the constraint, but it is exactly the WS8 risk pattern the audit
  brief describes, now about to be added by design rather than by accident,
  and it has no freshness guard of its own (nothing checks "is
  `top100_monthly.json` more than 7 days old" before the daily gate reads it
  — if `run_weekly_safe.sh` fails silently for a week, the gate keeps reading
  progressively stale evidence with no escalation).

- **`historical_backfill` (weekend-only producer) is the one cadence
  mismatch that IS handled correctly** — CONFIRMED. `daily_run_status.py`'s
  `_check_historical_backfill` (`:144-159`, not fully quoted above but
  present) treats "no recent run" as expected on weekdays specifically
  because the producer is weekend-only by design; it does not misfire.
  This is the template the rest of the system doesn't otherwise follow.

- **`daily_check.sh` (weekday-only, 09:15 UTC) vs. `run_daily_safe.sh`
  (every day, 09:00 UTC)** — INFERRED risk: on Saturday/Sunday, the health
  check that would normally catch stage failures in the daily pipeline does
  not run, even though the daily pipeline itself does run those two days.
  A silent weekend producer failure would not be surfaced by
  `daily_check.sh` until Monday's 09:15 run, by which point up to 2 days of
  degraded/missing artifacts could have accumulated. Not independently
  verified against a real failure; flagged as a plausible gap given the
  crontab's own asymmetry (7-day pipeline, 5-day health check).

### F7. Boundary behavior summary

| Boundary | Behavior observed | Confirmed / Inferred |
|---|---|---|
| Weekends | `run_daily_safe.sh` still fires (no day-of-week filter); `daily_check.sh` does not (weekdays only, `1-5`) | CONFIRMED (crontab) |
| Holidays | No holiday-awareness anywhere in the cron trigger or `daily_input_snapshot.py`/`artifact_registry.py` freshness logic. Only `resolution_due_probe.py`'s private `_NYSE_HOLIDAYS` list knows about holidays, and only for a different purpose (trading-day-age counting for outcome resolution, not evidence freshness) | CONFIRMED |
| Pre-market runs | `run_daily_safe.sh` fires at 09:00 UTC (~04:00–05:00 ET depending on DST), well before the 09:30 ET open — the "daily" decision plan is built entirely from the prior session's close. No artifact states this explicitly; a reader has to know the cron schedule to infer it | CONFIRMED (crontab time) / INFERRED (no artifact documents "this reflects yesterday's close") |
| Post-market runs | Not separately scheduled; same 09:00 UTC run is the only daily pipeline invocation | CONFIRMED |
| Delayed prices | No detection: `freshness`/`stale` logic in `daily_input_snapshot.py` only measures age of the artifact file, never compares the embedded price/quote timestamp against the current session. If FMP serves delayed or previous-close data during a live run, nothing here would notice as long as the artifact was written "on time" | INFERRED (no counter-evidence found; no code path compares quote timestamp to session) |
| Failed producer runs | `run_manifest.py` tracks `status: running/complete/failed` per run and `daily_run_status.py` surfaces `is_complete()`; `run_stage_nonblocking` in `run_daily.sh`/`run_daily_safe.sh` continues past a failed stage (logs a WARNING, does not abort the chain) except Stage 1 (main pipeline), which is fail-fast. So a downstream stage's failure leaves its artifact at its last-successful mtime while upstream artifacts advance — a genuine cross-artifact incoherence that `coherent_run_ids` (F4) was built to catch but is never invoked to catch | CONFIRMED (script logic + dead-code cross-reference) |
| Partially written artifacts | Guarded at the single-file level: `data_governance.safe_write_text`/`safe_write_json` (`portfolio_automation/data_governance.py:226-271`) write to a temp file in the same directory and `os.replace()` into place, with cleanup on failure — an interrupted write cannot leave a torn/partial file. This does **not** extend to cross-artifact atomicity: a crash between writing artifact A and artifact B in the same run leaves a real (each-individually-valid) but mutually inconsistent set of "today's" artifacts, which again is exactly what `coherent_run_ids` exists to flag and doesn't get used for | CONFIRMED |

---

## WS9 — `build_top100_daily` silent tie

### F-WS9-1. The lookback=1 recent-hit-rate term is structurally dead, not merely quiet today — CONFIRMED (ran read-only snippets against real artifacts)

`portfolio_automation/universe_sanitation.py:506-507`:
```python
def build_top100_daily(root: str | Path = ".", *, lookback_days: int = 1) -> dict[str, Any]:
    return _build_payload(Path(root).resolve(), cadence="daily", lookback_days=lookback_days)
```
`_score` (`:289-306`) weights: `_W_SOURCES=0.40`, `_W_THEME_CONF=0.30`,
`_W_RECENT_HITRATE=0.20`, `_W_FMP_TOP100=0.10`. The `recent_hit_rate`
component reads `hits_1d / resolved_1d` from `_load_recent_signals(root,
lookback_days)` (`:145-176`), which filters `signal_outcomes.csv` rows to
`signal_time >= now - lookback_days`.

Ran against the real `outputs/performance/signal_outcomes.csv` (2,373 rows):
```
rows within lookback_days=1 cutoff:                 27
of those, resolved_1d (outcome_return_1d populated): 0
```
This is not a today-specific anomaly — it is structural: `outcome_return_1d`
is only populated once a full trading day has elapsed and the outcome
resolver has run (see `resolution_due_probe.py`'s own model of this same
process), so a signal whose `signal_time` falls inside a 1-day lookback
window can never have had time to resolve yet. **The 20%-weighted
`recent_hit_rate` term of `_score()` is permanently zero-information at
`lookback_days=1`, by construction, every single day** — not a data-quality
problem, a design property of pairing a 1-day evidence window with a
1-day-minimum outcome-maturation lag.

### F-WS9-2. The real `top100_daily.json` does NOT tie every candidate — the claim needs correcting, but a majority-group tie with an alphabetical tiebreak is real and worse in one respect — CONFIRMED (ran read-only snippet)

Quoted directly from `outputs/latest/top100_daily.json`
(`generated_at: 2026-07-28T19:00:22Z`, 31 candidates):
```
unique scores: [0.16, 0.18, 0.32, 0.43, 0.45, 0.465, 0.51, 0.645]
score counts:  {0.16: 17, 0.18: 1, 0.32: 3, 0.43: 3, 0.45: 1, 0.465: 2, 0.51: 2, 0.645: 2}
```
So it is **not** true that "every candidate genuinely ties" — 8 distinct
score buckets exist among 31 rows, driven by genuine differences in
`sources` count, `theme_confidence_max`, and FMP-top100 presence (all of
which are lookback-independent). The audit brief's premise is only partially
right. What **is** exactly true: **17 of 31 (55%)** of today's daily
candidates are tied at the identical score `0.16`. Inspecting that group:
```
AAPL AVGO CHAT COIN IWM MARA META NASA PLTR QQQ RIOT SMCI SPY TSLA XLE XLF XLK
```
— all 17 share `sources=['recent_signal','static']` (2 of 5 known sources →
`presence=0.4` → `0.40*0.4=0.16`), `theme_confidence_max=0.0`, and (per F-WS9-1)
`recent_resolved_1d=0` for every one of them, so `recent_hit_rate` contributes
`0` for all 17 regardless of any real underlying difference in signal
quality between, say, `AAPL` and `MARA`.

`_rank_candidates`'s sort key (`:452-458`):
```python
rows.sort(key=lambda r: (-r["score"], -len(r["sources"]),
                          -(r["theme_confidence_max"] or 0.0), r["symbol"]))
```
For this 17-row group, `score` ties, `len(sources)` ties (all 2), and
`theme_confidence_max` ties (all 0.0) — so the tiebreak collapses entirely to
`r["symbol"]`, i.e., **pure alphabetical order**. `AAPL` outranks `AVGO`
outranks `CHAT` ... outranks `XLK`, every single day, for a reason that has
nothing to do with any signal, theme, or hit-rate difference among them —
purely because "A" sorts before "X." This is a silent, systematic,
content-free ranking bias affecting the majority of the daily universe.

For contrast, weekly (`lookback_days=7`) and monthly (`lookback_days=30`)
discriminate meaningfully better, quoted directly from real artifacts:
```
weekly (31 candidates):  12 distinct scores, largest tie group = 8/31 (26%)
monthly (29 candidates): 21 distinct scores, largest tie group = 2/29 (7%)
```
The severity of the tie strictly decreases as lookback grows, which is fully
consistent with F-WS9-1: longer lookback windows contain signals old enough
to have actually resolved, so `recent_hit_rate` starts contributing real
information; at `lookback_days=1` it never can.

### F-WS9-3. Not an accident — a deliberate design that nobody has revisited, and the team already knows about it — CONFIRMED

Git history: `universe_sanitation.py` has exactly 2 commits total —
`7435c7d3` (introduced the whole module, including `lookback_days: int = 1`
as the daily default) and `14af7d83` (unrelated ETF sector-normalization
fix). The `lookback_days=1` default has never been touched or reconsidered
since introduction. The module's own docstring (`:35`) documents it as
intentional API surface (`build_top100_daily(root, *, lookback_days=1)`), and
`tests/test_universe_sanitation.py:217-222`
(`test_daily_lookback_is_1`) pins the value of `1` as a hard requirement —
so it reads as a deliberate cadence choice (daily cadence → 1-day lookback,
by naming symmetry), not a copy-paste accident. But the interaction between
"1-day lookback" and "resolution requires ≥1 day to mature" was very likely
not modeled when the default was chosen, and it has now been independently
rediscovered and documented **today** by a same-day design effort:
`docs/superpowers/specs/2026-07-28-watchlist-remove-producer-design.md`
and its companion plan `docs/superpowers/plans/2026-07-28-watchlist-remove-producer.md`
both list, verbatim, as an explicit out-of-scope follow-up:
> "`build_top100_daily(lookback_days=1)` zeroes the `_W_RECENT_HITRATE` term,
> so all tickers tie at 0.16 and the daily artifact cannot discriminate."

That design deliberately routes its new daily-cadence gate to
`top100_monthly.json` instead of `top100_daily.json` *specifically to avoid*
this defect (see WS8 F6) — i.e., the team has already identified this exact
condition today, independently of this audit, and worked around it rather
than fixed it. This audit's finding is therefore corroborating, not novel,
but it independently confirms the defect by execution (F-WS9-1/2) rather
than by inspection alone, and extends it slightly (the 55% tie + alphabetical
tiebreak mechanics were not spelled out in the design doc).

### F-WS9-4. No test covers the tie under realistic production timing; no health check exists for zero-variance/all-equal-score rankings — CONFIRMED

`tests/test_universe_sanitation.py` has 19 test methods, including
`test_daily_lookback_is_1` (asserts the field value, nothing about score
distribution) and `TestSignalOutcomesLookback.test_old_signals_excluded`
(`:305-319` in the file), which writes a fixture where a "recent" (12-hours-old)
signal already carries a populated `outcome_return_1d`:
```python
_write_signal_outcomes(root, [
    {"ticker": "OLD", "signal_time": old_ts, ...},
    {"ticker": "NEW", "signal_time": recent_ts,
     "outcome_return_1d": "0.02", "direction_correct_1d": "1"},
])
```
This fixture is **not representative of production timing** — in the real
system, per F-WS9-1, a signal only 12 hours old cannot yet have a populated
`outcome_return_1d` (confirmed empirically: 0/27 real 1-day-window rows do).
The test suite's fixture therefore masks the exact defect this audit
confirmed against real data; no test in the repo exercises
`build_top100_daily` under realistic resolution timing, and none asserts
anything about score dispersion, tie size, or tiebreak fairness.

Separately, searched `daily_run_status.py`, `artifact_registry.py`, and the
full test corpus for any check of "zero variance," "all equal scores,"
"zeroed weight," or similar: **none exists**. `_check_top100_daily`
(`daily_run_status.py:134-141`) only checks `len(candidates) > 0`; it would
report "ok" on a payload where all 100 candidates share an identical score of
`0.0`. This absence is itself a finding per the audit brief's framing.

### F-WS9-5. Consumer impact — mixed, mostly low-stakes today but not zero

Grepped all consumers of `top100_daily.json`:

- `daily_run_status.py:_check_top100_daily` — only checks candidate count,
  unaffected by the tie (F5 above).
- `watchlist_scanner/daily_memo.py:_pattern_confirmed_candidates`
  (`:2204-2243`) — reads `top100_daily.json`, but **re-derives its own
  ranking** from `pattern_efficacy` winning-tag overlap first, using
  `top100_daily`'s `score` field only as a secondary tiebreak
  (`ranked.sort(key=lambda kv: (-kv[0], -float(kv[1].get("score") or 0.0), kv[1].get("symbol", "")))`).
  This section only activates when `winning_tags` is non-empty, and even
  then, `score` only matters as a tiebreak among candidates that already
  share the same tag-overlap count — so the alphabetical bias from F-WS9-2
  can still leak through this secondary sort when two candidates share both
  `win_count` and `score`, but it is not the primary driver of what memo
  surfaces here.
- `portfolio_automation/pattern_learning.py:_load_snapshot` (`:104-116`) —
  loads the full `candidates` list unfiltered by rank/order for its
  outcome-join; rank/score ordering does not affect which tickers are
  included, only cosmetic order.
- `portfolio_automation/social_intelligence/public_knowledge_velocity.py:_known_universe`
  (`:120-129`) reads `outputs/sandbox/top100_daily.json` (note: **sandbox**
  path, distinct from `outputs/latest/top100_daily.json`) looking for keys
  `symbols`/`tickers`/`watchlist` — but the actual daily payload schema uses
  `candidates` (a list of dicts), not any of those three keys. This lookup
  will silently return an empty set every time regardless of the tie issue —
  a separate, minor dead-consumption bug noted in passing (not part of this
  audit's core scope, flagged for completeness).
- The `rank` field itself (assigned post-sort, `:457`) is not consumed by any
  code outside `universe_sanitation.py`'s own rendering
  (`render_top100_md`) — grepped for `.get('rank')`/`["rank"]` reads
  elsewhere and found none outside the module and unrelated `sim_governance`
  modules that have their own, unrelated `rank` field (`final_rank_score`
  aliasing, protected-semantics territory, not this artifact).

**Net effect today:** the alphabetical mis-ordering is real and would mislead
a human operator glancing at `top100_daily.md`'s ranked list expecting rank
to reflect signal quality (it currently reads as "AAPL is measurably better
than XLK" when the two are indistinguishable by every signal the scorer has),
but no *automated* downstream decision currently keys off `top100_daily`'s
rank order in a way that silently corrupts a decision. The blast radius is
presentation/trust, not (yet) decision-correctness — with the caveat that
the same defect (F-WS9-1) is precisely why the same-day design doc chose to
route a new **decision-adjacent** gate (`experiment_watchlist_decay_removals`,
which proposes production watchlist removals) to the monthly artifact instead
of the daily one. Had that design used `top100_daily.json`'s
`recent_hit_rate_1d`/`score` naively, the defect would have propagated into
an actual human-reviewed production-watchlist-removal proposal, not just a
memo section.

---

## Summary table

| # | Finding | Confirmed/Inferred | Test coverage | Blast radius |
|---|---|---|---|---|
| WS8-F1 | No repo-wide "source-data-through"/market-session field; `data_as_of` is capture-time, not session-time | Confirmed | None | All 148 scanned artifacts; decision-adjacent (`decision_plan.json`) included |
| WS8-F2 | `decision_plan.json` freshness = flat 26h wall-clock window, no calendar awareness | Confirmed | Untested for weekend/holiday edge cases | Core decision artifact |
| WS8-F3 | No shared market-calendar helper; only private, hardcoded-through-2027 `_NYSE_HOLIDAYS` in one probe | Confirmed | `resolution_due_probe` has tests; not reused elsewhere | Repo-wide gap; 2027-12-24 cliff |
| WS8-F4 | `coherent_run_ids` (cross-artifact run coherence) exists, tested, never called in production | Confirmed | Unit-tested, zero production callers | The one built safeguard for WS8's exact scenario is inert |
| WS8-F5 | Registry validator = mtime-only staleness per cadence; no reader-side freshness modeling | Confirmed | `test_artifact_registry.py` exists for the mechanism as designed | Any daily reader of a weekly/monthly artifact is invisible to this validator |
| WS8-F6 | Named `top100_monthly` daily-read case doesn't exist yet in code, but a same-day, not-yet-implemented design is about to introduce exactly that pattern deliberately, with no freshness guard | Confirmed (current state) / plan not yet implemented | New producer's plan includes gate boundary tests, but no "is top100_monthly stale" guard | Would be decision-adjacent (human-gated watchlist removal) once implemented |
| WS8-F7 | Boundary behavior: weekends/holidays/pre-market/failed-run/partial-write | Mixed (see table) | Partial (atomic single-file writes tested; cross-artifact coherence not) | System-wide |
| WS9-F1 | `recent_hit_rate` term is structurally zero-information at `lookback_days=1` (0/27 real rows resolved) | Confirmed by execution | Existing fixture is unrealistic and masks this | `top100_daily.json` scoring |
| WS9-F2 | 17/31 (55%) of real daily candidates tie exactly; tiebreak collapses to alphabetical symbol order | Confirmed by execution | None | Presentation/trust in `top100_daily.md`/memo section |
| WS9-F3 | Deliberate (not accidental) design, already independently flagged today by an unrelated in-flight design doc as an explicit out-of-scope follow-up | Confirmed | N/A | N/A |
| WS9-F4 | No test exercises realistic resolution timing; no health check for zero-variance/all-equal-score rankings anywhere in the repo | Confirmed | Confirmed absent | Any future consumer that trusts daily rank/score naively |
| WS9-F5 | Current consumers mostly re-derive their own ranking or ignore rank; blast radius today is presentation, not decision-correctness — but a same-day in-flight design was steered around this defect specifically to avoid it leaking into a production-adjacent gate | Confirmed | N/A | Would escalate to decision-adjacent if a future daily consumer used `top100_daily.json`'s score/rank directly |
