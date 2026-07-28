# Workstream 1a — strategy_score decomposition persistence — implementation report

Branch: `feat/ws1-score-decomposition` (off `main`)

## What changed (artifact-only, additive)

- `portfolio_automation/portfolio_sim/strategy_score.py`:
  - `score()` now also returns a `score_decomposition` block alongside the unchanged
    `strategy_score`/`flags`/`components`. Per component it records: `raw` value,
    `normalized`/contributing value, `weight`, `direction` (`higher_better` /
    `lower_better`), `contribution` (signed, sums to the composite), `missing` bool,
    and `missing_reason` (never a silent `0.0` substitution for an unmeasured value —
    `raw`/`normalized` are `null` when missing; the pre-existing `overfit=None -> 0.0`
    fallback still drives `contribution`/`strategy_score` unchanged).
  - Also records `weight_set` (`version` + `source`: `"default"` vs `"config_override"`
    + the actual merged weights applied) and `normalization` (`method: "none"` — no
    cross-sectional normalization occurs anywhere in the formula today, recorded
    explicitly rather than omitted).
  - Includes `stored_composite`, `recomputed_composite`, `residual`, `reproducible`
    (self-consistency check that the persisted contributions re-sum to the score).
  - New module-level pure helper `recompute_composite_from_decomposition(decomposition)`
    — the one shared re-derivation of "parts sum to total," for tests and future
    consumers.
  - The protected composite math (`total = ...`) is byte-for-byte unchanged; the
    decomposition is computed from the same intermediate values purely for recording.
- `portfolio_automation/portfolio_sim/run_strategy_lab.py`: `_score_tactic` now
  persists `sc["score_decomposition"]` into every leaderboard row (including the
  sentiment diagnostic row, which reuses the same function).
- `tests/portfolio_sim/test_strategy_score.py`: 4 new tests (see below).

No changes to `decision_engine.py`, `_TRACKED_KNOBS`, or any protected score
semantics. No broker/execution code touched. Strategy Lab remains sandbox-only.

## Tests added

In `tests/portfolio_sim/test_strategy_score.py`, against a 5-tactic fixture set
spanning: a top performer with `overfit` unmeasured, a mid-pack tactic, the one
genuinely walk-forward-validated tactic (`overfit=2.009588`), a baseline with
`overfit` measured as exactly `0.0`, and a high-turnover crowd-style tactic:

1. `test_decomposition_reproducible_for_fixture_tactics` — recomputed composite from
   the decomposition equals stored `strategy_score` within `1e-6` for every fixture.
2. `test_decomposition_ordering_parity` — sorting fixtures by score reconstructed
   purely from decompositions matches `rank()`'s stored order.
3. `test_decomposition_missing_data_honesty` — `overfit` unmeasured records
   `missing=True`, `raw=None`, `normalized=None` (asserts `raw != 0.0`, i.e. fails if
   `0.0` were substituted); a genuinely-measured `overfit=0.0` case is asserted
   `missing=False` with `raw=0.0`, so the two are distinguishable.
4. `test_decomposition_no_change_guard` — an independent re-implementation of the
   pre-existing formula (not calling `score()`'s internals) produces identical
   `strategy_score` values and identical `rank()` ordering to the current code.

## Verification run

Ran the real Strategy Lab entry point directly (`run_strategy_lab(root=".", run_mode="discovery")`)
— sandbox-only, observe-only, writes only to `outputs/sandbox/` (gitignored, not
committed), no network calls (local price archives only). Confirmed against the
pre-change artifact (saved copy before regenerating):

- 26/26 tactics scored, `status: ok`, `coverage_complete: True`.
- **Ranking identical**: `tactic_id` order in the regenerated leaderboard matches the
  pre-existing `outputs/sandbox/strategy_leaderboard.json` order exactly.
- **Scores identical**: every `strategy_score` value matches the pre-existing artifact
  (zero diffs) — confirms the decomposition addition changed no score/rank value.
- **Reconstruction residual**: max `0.0`, mean `0.0` across all 26 tactics (recomputed
  composite from `score_decomposition["components"][*]["contribution"]` sums exactly
  to the stored `strategy_score`) — this replaces the pre-change residual of
  `0.15–0.51` per tactic reported in the audit (§2), which was purely a persistence
  gap (turnover/concentration/leverage were computed but discarded, never a math bug).
- **Missing-component count**: 25 of 26 tactics have at least one missing component —
  in every case the missing component is `overfit` (walk-forward is only run for
  `research_momentum_rotation`; the other 25 tactics have `overfit: null`/never
  measured). No other component (`turnover`, `tax_drag`, `concentration`, `leverage`,
  etc.) is ever missing — they are always computed, just via crude proxies (a
  separate, already-documented finding, not touched by this change).

## Test commands run

```
.venv/bin/python -m py_compile portfolio_automation/portfolio_sim/strategy_score.py portfolio_automation/portfolio_sim/run_strategy_lab.py
.venv/bin/python -m pytest -q tests/portfolio_sim/test_strategy_score.py tests/portfolio_sim/test_strategy_lab_e2e.py tests/portfolio_sim/test_strategy_lab_health.py
.venv/bin/python -m pytest -q tests/portfolio_sim/            # full portfolio_sim subtree
```

Results: 29/29 targeted tests passed; full `tests/portfolio_sim/` subtree 126/126
passed. Full repo suite (`pytest -q`) intentionally NOT run per task constraint
(known ~4-5 min runtime; also known to mutate the tracked `config/signal_registry.yaml`
per prior session notes — unrelated to this change).

## Assumptions

- "Weight-set identity/version" is satisfied by a static `WEIGHT_SET_VERSION` module
  constant plus a `source` field (`default`/`config_override`) and the actual merged
  weight values — since `config.json portfolio_sim.strategy_lab.scoring` has never
  been populated in production (confirmed by audit §1), `source` is `"default"` for
  every live row today.
- "Normalization population + date if any normalization occurs" — none occurs
  anywhere in this formula (confirmed audit §3), so the block honestly records
  `method: "none"` with `population`/`population_date` as `null` rather than
  fabricating a population that doesn't exist.
- Missing-data marking is generic (any component whose key is absent or explicitly
  `None`) rather than special-cased only for `overfit`, so the pattern is
  future-proof if another component ever becomes optionally-missing; today only
  `overfit` triggers it in practice, matching the audit's finding.

## Risks

None identified. Change is purely additive to the returned/persisted dict shape;
no existing consumer field was removed, renamed, or changed in value. Verified
via full before/after score+ranking diff against the real 26-tactic artifact.

## Recommended next step

Per `.agent/project_state.yaml:next_official_step` — not consulted for this scoped
artifact-only fix; workstream 1a is one item in a larger reliability program (audit
`ws-01-strategy-score.md` §1/3/6 identify follow-on items — double-counted
`consistency`/`prob_beat_spy`, constant `tax_drag=0.0`, binary `turnover` proxy,
`overfit` scale mismatch, and the absence of any sensitivity/perturbation analysis —
each explicitly out of scope here and gated behind separate operator approval since
they change score *values*, not just persistence).
