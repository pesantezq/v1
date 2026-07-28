# Watchlist-Removal Producer — Design

**Date:** 2026-07-28
**Status:** approved (operator, 2026-07-28)
**Scope:** simulation-lane producer + baseline fix + attribution guard + health pairing

---

## 1. Problem

The quant-watch probe `manual:financial_services_sector_drag_5687885c` (opened
2026-07-15) and the 2026-07-28 attribution analysis independently concluded that
the "Financial Services drag" under gauge `5687885c` is not sectoral but
idiosyncratic crypto-miner decay:

| ticker | n_1d | hit_1d | mean_1d | worst_1d |
|---|---|---|---|---|
| RIOT | 33 | 33.3% | -0.27 | -15.01 |
| MARA | 33 | 42.4% | -0.37 | -13.02 |
| COIN | 33 | 57.6% | +0.96 | -8.35 |
| XLF | 33 | 72.7% | +0.49 | -2.08 |

RIOT and MARA are hardcoded entries in `config.json:watchlist_scanner.watchlist`
(22 symbols), seeded in the initial commit and never re-litigated. Static seats
bypass every quality screen permanently:

- The real screen (`scanner/candidate_scanner.py:255,265,281` — `min_mkt_cap`
  $5B, `min_rev_growth`, `trend_filter_200dma`) governs **only** the FMP
  top-100 candidate path.
- `extended_watchlist` demotion (`expire_stale`, `demote_vetoed`) operates on DB
  rows only; static symbols never enter the DB.
- MARA's market cap is $4.62B — **below the system's own $5B floor**. It would
  be rejected today if it had to earn its seat.

`universe_sanitation` has already reached the same conclusion and nothing
consumes it. In `outputs/latest/top100_monthly.json` (30-day lookback,
generated 2026-07-27):

| symbol | rank | score | recent_hit_rate_1d | recent_resolved_1d |
|---|---|---|---|---|
| MARA | 25 | 0.250 | 0.4516 | 31 |
| RIOT | 29 | 0.231 | 0.3548 | 31 |
| SMCI | 30 | 0.218 | 0.2903 | 31 |
| LLY | 31 | 0.180 | `null` | 0 |

**Two different sample counts appear in this spec and are not in conflict.** The
`n = 33` figures in the table above come from the *gauge window*
(2026-06-28 to 2026-07-28, 881 resolved rows in `signal_outcomes.csv`), which the
attribution analysis uses. The `recent_resolved_1d = 31` figures come from
`universe_sanitation`'s rolling **30-day** lookback, which the gate reads. The
gate is defined against the latter.

Note LLY's `recent_hit_rate_1d` is `null` (not `0.0`) — the gate must tolerate a
missing value, not just a zero count.

### The governance gap

`PROPOSAL_WATCHLIST_REMOVE` is fully built and switched on, but has **no
producer**:

| Component | Location | State |
|---|---|---|
| Type constant | `sim_governance/schemas.py:33` | exists, in valid-type sets (`:51`, `:77`) |
| Rollback text | `sim_governance/promotion_proposals.py:41` | exists |
| Applier | `sim_governance/production_overlays.py:76` | exists |
| Simulated-view handling | `simulation_lane.py:413` (`op == "remove"`) | exists |
| Live wiring | `watchlist_scanner/__main__.py:224` | **enabled** (`apply_watchlist_overlay: true`) |
| **Producer** | — | **none** |

### The deeper defect (found during design)

`load_production_baseline` (`simulation_lane.py:111`) reads
`config.json:portfolio.watchlist` — **a key that does not exist**. The real list
is `watchlist_scanner.watchlist`. Verified at runtime, `baseline["watchlist"]`
is `[]`. The loader also never populates `discovery_candidates` or
`watchlist_ranked`. Measured against today's baseline:

```
experiment_watchlist_discovery_adds  -> 0 candidates   DEAD (no discovery_candidates)
experiment_watchlist_rerank          -> 0 candidates   DEAD (no watchlist_ranked)
experiment_advisory_crowd_context    -> 46 candidates  live
experiment_flock_intelligence        -> 20 candidates  live
```

So the sim-governance **watchlist workflow has never produced a proposal** — it
is advisory-only, not add-only. Every proposal in
`outputs/promotion_approvals/approved_proposals.json` is
`flock_advisory_context_logic`. Consequence for this work: the removal producer
will be the first functioning watchlist experiment, and the removal branch of
`apply_approved_watchlist` has never executed against a real approved proposal —
only unit tests. The end-to-end test is therefore load-bearing, not a formality.

---

## 2. Goals / non-goals

**Goals**

1. Emit `watchlist_remove` candidates for decayed static universe members, via
   the existing human-gated promotion workflow.
2. Fix `load_production_baseline` to reflect effective production state.
3. Record the universe-composition break so gauge attribution stays honest.
4. Pair the feature with a health check (CLAUDE.md Analysis + Health
   Coverage Requirement).

**Non-goals**

- Editing `config.json`'s watchlist by hand. The overlay performs removal;
  config remains the historical baseline definition.
- Reviving `experiment_watchlist_discovery_adds` / `experiment_watchlist_rerank`
  (they need separate input wiring — out of scope, recorded as follow-up).
- Making removals auto-approvable. See invariant below.
- Changing `_TRACKED_KNOBS`, score semantics, `decision_engine.py`, or
  allocation logic.

---

## 3. Authority invariant (must hold)

`PROPOSAL_WATCHLIST_REMOVE` is **absent** from
`auto_approval._WATCHLIST_ELIGIBLE_TYPES` (`auto_approval.py:62-65`, verified
`False`). A removal can therefore never be GPT-auto-approved; it is always
human-gated. **This design must not add it to that set.** A regression test pins
the invariant.

---

## 4. Architecture

```
universe_sanitation (monthly) -> outputs/latest/top100_monthly.json
                                   (rank, recent_hit_rate_1d, recent_resolved_1d)
                                          |
                                          v
load_production_baseline  ->  baseline["watchlist"] = EFFECTIVE runtime list
   (FIX: watchlist_scanner.watchlist + extended rows + approved overlays)
                                          |
                                          v
experiment_watchlist_decay_removals(baseline)                        [NEW]
   gate: recent_resolved_1d >= 30
     AND recent_hit_rate_1d < 0.40
     AND symbol present in effective baseline
                                          |
                                          v
daily_simulation_bundle (workflow=watchlist bucket)
   -> daily AI review -> generate_proposals -> pending_proposals.json
                                          |
                                          v
                    HUMAN approval only (promotion_approvals.record_approval)
                                          |
                                          v
     production_overlays.apply_approved_watchlist -> live scan drops the symbol
                                          |
                                          v
              quant-watch probe: universe composition break recorded
```

### Gate rationale

`recent_resolved_1d >= 30 AND recent_hit_rate_1d < 0.40`.

- Today's output: **RIOT** (0.3548) and **SMCI** (0.2903).
- MARA (0.4516) does **not** qualify. Its real disqualifier is the $4.62B market
  cap, which belongs to a separate market-cap screen (follow-up), not a
  hit-rate rule. Removing it for the wrong reason would be incoherent.
- The `n >= 30` guard is load-bearing: without it LLY (rank 31, **0 resolved**,
  `recent_hit_rate_1d = null`) is swept purely for lacking history.
- Absolute rather than rank-relative: a bottom-decile rule always removes
  someone, even when the whole universe is healthy.

### Idempotence

The baseline is the **effective post-overlay** watchlist, so once a removal is
approved and applied the symbol is no longer in the baseline, the gate's
membership condition fails, and the rule stops proposing it. Self-suppressing,
with no second source of truth. This matters because `pending_proposals.json` is
overwritten each run and `make_proposal_id(cid, now)` mints a new id daily — a
config-reading baseline would re-propose the same removal forever.

---

## 5. Components

| Change | File / symbol | Nature |
|---|---|---|
| Baseline reads effective runtime watchlist | `sim_governance/simulation_lane.py:111` `load_production_baseline` | bug fix |
| New producer | `simulation_lane.py` `experiment_watchlist_decay_removals` | new |
| Register experiment | `simulation_lane.py:383` `DEFAULT_EXPERIMENTS` | one line |
| Composition-break probe | `portfolio_automation/quant_watch_probes.py` | new detector |
| Health check | `.claude/commands/daily-tool-analysis.md` §6n + `portfolio-discovery-health` | pairing |

No new module: `simulation_lane.py` is 516 lines with an established experiment
registry, and a fifth sibling experiment does not justify a new file or a second
wiring path.

### Blast radius of the baseline fix

Precisely zero effect on the other three experiments:

- `experiment_watchlist_discovery_adds` reads `discovery_candidates` (still
  unpopulated) — stays dead; its dedup guard `sym in prod_wl` remains moot until
  that input is wired.
- `experiment_watchlist_rerank` reads `watchlist_ranked` — stays dead.
- `experiment_advisory_crowd_context` and `experiment_flock_intelligence` do not
  read `baseline["watchlist"]`.

The bundle's `added` / `removed` diffs (`daily_simulation_bundle.py:98-99`) will
become meaningful for the first time, since `prod_wl` is currently empty.

---

## 6. Attribution guard

The watchlist is **not** part of the gauge fingerprint (`_TRACKED_KNOBS` covers
`allocation_engine`, `portfolio_construction`, `structural_caps`,
`feature_flags`, `api_limits` — verified). Removing RIOT therefore does not mint
a new fingerprint: pre- and post-removal samples pool under the same
`5687885c`, and its `hit_rate_1d` / `mean_return_1d` drift upward purely from
composition change — indistinguishable from genuine gauge improvement. Measured
over the current gauge window (881 resolved rows):

| removed | n | share of resolved |
|---|---|---|
| RIOT | 33 | 3.75% |
| SMCI | 33 | 3.75% |
| RIOT + SMCI | 66 | **7.49%** |

Mitigation: on a successful removal apply, auto-register a quant-watch probe
recording the break (symbol, sample share, date, fingerprint) so any later
attribution read of that fingerprint self-warns and splits the window.

Rejected alternative: adding the watchlist to the fingerprint. Extended-watchlist
promotion runs ~2-3 symbols/week, so this would fragment attribution into
unusably small cohorts and would change protected attribution semantics.

Operational note (recommended, not enforced): timing the approval to land just
after the next retune puts the composition break on a fingerprint boundary,
which is cleaner than annotating a mid-window break.

---

## 7. Error handling

The simulation lane is non-fatal per experiment. Specifically:

- Missing / malformed / empty `top100_monthly.json` -> return `[]`, never raise.
- Rows lacking `recent_resolved_1d`, or whose `recent_hit_rate_1d` is `null` /
  non-numeric, -> skipped (never coerced to `0.0`, which would read as a
  worst-case hit rate and trigger removal).
- Symbol absent from the effective baseline -> skipped (cannot propose removing
  something already gone).
- Baseline loader: each source (config, extended rows, overlay) degrades
  independently to empty, preserving today's tolerant behavior.

---

## 8. Testing

Gate behavior
- boundaries: `n = 29` vs `30`; `hit_rate = 0.399` / `0.400` / `0.401`
- today's real `top100_monthly.json` -> proposes exactly `{RIOT, SMCI}`
- MARA excluded (hit_rate above threshold); LLY excluded (`n = 0`)
- **`recent_hit_rate_1d = null` is skipped, never coerced to `0.0`** — a
  null-to-zero coercion would read as the worst possible hit rate and remove
  every no-history symbol. Asserted with `n >= 30` *and* a null hit rate, so the
  test fails if the code relies on the `n` guard alone.

Baseline fix
- `baseline["watchlist"]` is non-empty and contains `RIOT`
- reflects approved overlays, not raw config
- each missing source degrades to empty without raising

Idempotence
- after an applied removal for `X`, the rule no longer proposes `X`

Authority invariant
- `PROPOSAL_WATCHLIST_REMOVE not in auto_approval._WATCHLIST_ELIGIBLE_TYPES`
- a removal candidate reaches `pending`, never `auto_approved`

End-to-end (load-bearing — this path has never run in production)
- candidate -> bundle -> proposal -> human approval -> overlay -> symbol absent
  from the scanned watchlist

Attribution guard
- a successful removal apply registers the composition-break probe once
  (idempotent across reruns)

Health check
- healthy and degraded fixture states produce the expected status

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| Removal path never exercised in production | load-bearing end-to-end test before enabling |
| Composition break inflates gauge hit-rate | quant-watch probe + after-retune timing guidance |
| Rule sweeps a symbol for lacking data | `n >= 30` guard, tested at the boundary |
| SMCI removal is broader than the original probe | surfaced for human approval; never auto-applied |
| Baseline fix changes lane behavior | verified zero effect on the other three experiments |

---

## 10. Roadmap position

`next_official_step` in `.agent/project_state.yaml` is `observe_and_iterate`, so
this is not a named roadmap step. It is justified as (a) fixing a confirmed
defect (`load_production_baseline` reading a nonexistent key) and (b) closing
the governance asymmetry whereby the universe can grow but never shrink.
Recorded here so the scope decision is visible rather than implicit.

## 11. Follow-ups (out of scope)

1. Wire `discovery_candidates` to revive `experiment_watchlist_discovery_adds`.
2. Wire `watchlist_ranked` to revive `experiment_watchlist_rerank`.
3. Advisory report: static seats that would fail the live candidate screen
   (`min_mkt_cap` $5B) — gives MARA a principled exit.
4. `build_top100_daily(lookback_days=1)` zeroes the `_W_RECENT_HITRATE` term, so
   all tickers tie at 0.16 and the daily artifact cannot discriminate.
5. Modernize the stale fixture in `tests/test_gui_dashboard_memo.py:83,87`.
