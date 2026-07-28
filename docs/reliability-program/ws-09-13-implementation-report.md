# WS9 + WS13 Implementation Report

Branch: `fix/ws9-13-silentzero` (off `main`, though a shared-checkout timing
quirk means its recorded base commit is `92176881` — see Notes below).
Commits:
- `b013b24c` — fix(sim-gov): wire `experiment_watchlist_discovery_adds` to its real input key (WS13)
- `f30433b1` — fix(universe): detect + surface degenerate `top100_daily` ranking (WS9)

---

## Fix 1 (WS13) — container-key mismatch in `_enrich_baseline`

**Verified independently before changing anything.** Read the live
`outputs/sandbox/discovery/automatic_promotion_candidates.json`: its real
top-level container key is `"decisions"` (today: `decision_count: 0`,
`"decisions": []`). Confirmed the per-row field names
(`PromotionDecision` dataclass, `automatic_promotion_governance.py:273-294`):
`ticker`, `proposed_status`, `corroboration_score`, `evidence_score`,
`catalyst_flags`, `risk_flags` — all compatible with what `_enrich_baseline`
already expected, confirming the audit's claim that only the outer key was
wrong.

**What changed** (`portfolio_automation/sim_governance/daily_governance_run.py`):
- `_enrich_baseline` now reads `promo.get("decisions", [])` instead of
  `.get("candidates", [])`, tolerant of a missing/malformed artifact
  (degrades to `[]`, never raises).
- Added a filter: only `proposed_status == "MONITOR"` decisions become
  watchlist-add candidates. `REJECTED`/`EXPIRED`/`NEEDS_REVIEW`/hold-status
  decisions are never surfaced — feeding a rejected ticker into "add to
  watchlist" would be wrong regardless of the key fix.
- New explicit, default-OFF config gate: `config.json →
  sim_governance.experiments.watchlist_discovery_adds_enabled` (default
  `false`). Kill-switch (wins even when `enabled: true`): file
  `config/experiment_watchlist_discovery_adds.DISABLED` or env
  `STOCKBOT_SIM_GOV_DISCOVERY_ADDS_DISABLED=1`.
- When disabled, `_enrich_baseline` reproduces the historical (buggy,
  always-empty) behavior byte-for-byte — verified directly (see below).
- `experiment_watchlist_rerank` has no producer anywhere in the codebase for
  `watchlist_ranked` — not invented. It is now explicitly classified
  `INERT_NO_PRODUCER` in the new diagnostics (see next item), distinguishing
  "no admissible input, structurally, forever" from "ran and found nothing."

**Per-experiment input accounting** (`simulation_lane.py`):
`run_simulation_lane` now tracks each experiment's own candidate list and any
exception it raised, and attaches `experiment_diagnostics: list[dict]` to its
result (and to `daily_governance_status.json →
stages.simulation_lane.experiment_diagnostics`). Each entry carries
`expected_input_key`, `actual_input_count`, `candidate_count`,
`zero_expected`, `classification` (`OPERATIONAL` / `INERT_GATED_OFF` /
`INERT_NO_PRODUCER` / `BROKEN`), and `reason`. This directly fixes the audit's
finding that the only prior signal (`candidate_count`, an aggregate) could
never expose a permanently-dead experiment because other experiments kept it
non-zero.

### Verification (read-only, no artifacts written)

Ran `_enrich_baseline` + `run_simulation_lane` against the real repo state:

```
gate default enabled flag: False
discovery_candidates (disabled): []
discovery_adds candidate_count (disabled): 0
total candidate_count (disabled): 66

discovery_candidates (enabled, real data today): []
discovery_adds candidate_count (enabled, real data today): 0
total candidate_count (enabled, real data today): 66

real decisions today total: 0   MONITOR: 0
```

**Candidate count today: 0 with the gate on, 0 with the gate off** — because
`automatic_promotion_candidates.json` currently has `decision_count: 0`
(no promotion decisions of any kind exist today, matching the audit's own
observation). Disabled reproduces today's behavior **exactly** (both are `[]`
/ `0`, and the total lane `candidate_count` of 66 — from the two already-live
experiments, crowd-context (46) + flock (20) — is unaffected either way).

Because today's live data can't demonstrate the fix producing a *non-zero*
result, ran a second, synthetic read-only check against a fabricated
`automatic_promotion_candidates.json` (4 decisions: 2 `MONITOR`, 1
`REJECTED`, 1 `DISCOVERED`/hold-status) to prove the wiring functions
end-to-end:

```
disabled -> discovery_candidates: []
enabled  -> discovery_candidates: [ABCD (score 0.82), WXYZ (score 0.70)]
watchlist_add candidates emitted (enabled): 2  ['ABCD', 'WXYZ']
```

Confirms: (1) the real `"decisions"` key is read correctly, (2) only
`MONITOR` decisions pass the filter (REJECTED/DISCOVERED excluded), (3) the
candidates flow all the way through `run_simulation_lane` into real
`watchlist_add` `SimulationCandidate` objects.

`experiment_diagnostics` for today's real run:

```json
[
  {"experiment": "experiment_watchlist_discovery_adds", "expected_input_key": "discovery_candidates",
   "actual_input_count": 0, "candidate_count": 0, "zero_expected": true,
   "classification": "OPERATIONAL", "reason": "No admissible input this run (expected input is empty upstream)."},
  {"experiment": "experiment_watchlist_rerank", "expected_input_key": "watchlist_ranked",
   "actual_input_count": 0, "candidate_count": 0, "zero_expected": true,
   "classification": "INERT_NO_PRODUCER", "reason": "No producer anywhere in this codebase populates 'watchlist_ranked' ..."},
  {"experiment": "experiment_advisory_crowd_context", "expected_input_key": "advisory,crowd",
   "actual_input_count": 182, "candidate_count": 46, "zero_expected": false, "classification": "OPERATIONAL"},
  {"experiment": "experiment_flock_intelligence", "expected_input_key": "flock",
   "actual_input_count": 3, "candidate_count": 20, "zero_expected": false, "classification": "OPERATIONAL"}
]
```

(Note: with the gate enabled but today's real `decisions` empty,
`discovery_adds` reads `OPERATIONAL`/`zero_expected=true` — "no admissible
input", not gated. With the gate disabled it would instead read
`INERT_GATED_OFF`.)

### Tests added

`tests/test_ws13_discovery_adds_gate.py` — 14 tests: default-off gate,
disabled-reproduces-prior-behavior, real-key read + score/tags mapping,
MONITOR-only filtering, end-to-end candidate emission, missing/malformed
artifact tolerance, env + file kill-switch precedence over `enabled=true`,
and 5 tests of `experiment_diagnostics` classification (`INERT_NO_PRODUCER`,
`INERT_GATED_OFF`, `OPERATIONAL` with a real zero vs. a healthy non-zero,
`BROKEN` on an exception).

### Docs

`docs/SIM_GOVERNANCE.md` — new section documenting the fix, the gate, the
kill-switch, the `INERT_NO_PRODUCER` classification, and the
`experiment_diagnostics` shape. `.claude/commands/daily-tool-analysis.md` —
added a per-experiment diagnostics bullet (item under the sim-gov section)
and a `sim_gov_experiment_broken` AMBER dispatch rule
(`classification == "BROKEN"` → `portfolio-learning-loop-health`).

---

## Fix 2 (WS9) — degenerate-ranking diagnostic for `top100_daily`

**No change to the ranking algorithm, weights, or `lookback_days`.**
Verified byte-identical output: re-ran `build_top100_daily('.')` against the
live repo and diffed every candidate's `symbol`/`score`/`rank`/`sources`/
`theme_confidence_max` against the pre-change artifact on disk — **0
mismatches** across all 31 rows.

**What changed:**
- `portfolio_automation/universe_sanitation.py`: new `_diagnose_ranking`
  function, called from `_build_payload` and attached as
  `ranking_diagnostics` on every `top100_{daily,weekly,monthly}.json`.
  Detects: zero-variance scores, the largest tie bucket (by the full
  effective sort key: score, distinct-source count, theme confidence — the
  same key `_rank_candidates` sorts on ahead of the symbol tiebreak) as a
  fraction of candidates, any weighted term (`sources_presence`,
  `theme_confidence`, `recent_hit_rate`, `fmp_top100_presence`) that
  contributed no information this run, and a small-universe flag
  (`candidate_count < 10`).
- `render_top100_md` now emits a "Ranking quality" section — a plain-language
  warning when degenerate, or a one-line discriminative-ranking confirmation
  otherwise — so a human reading `top100_daily.md` sees the same finding.
- `portfolio_automation/daily_run_status.py`: `_check_top100_daily` (the
  existing content_liveness assessor — no new tier invented) now also warns
  when `ranking_diagnostics.degenerate_ranking` is true, in addition to its
  original zero-candidates check. A "warn" here already flows through the
  existing generic content_liveness → AMBER rule in `daily-tool-analysis.md`
  (`overall_status == "ok_with_warnings" AND only content_liveness warns`).
- Added item 30 to `.claude/commands/daily-tool-analysis.md` documenting the
  check and its AMBER condition.

### Verification (real `top100_daily.json`, read-only)

```
same length: True  (31, 31)
mismatches (ranking output vs. pre-change artifact): 0

ranking_diagnostics:
{
  "candidate_count": 31,
  "distinct_score_count": 8,
  "zero_variance": false,
  "largest_tie_group_size": 17,
  "largest_tie_fraction": 0.5484,
  "largest_tie_score": 0.16,
  "alphabetical_tiebreak_detected": true,
  "zero_information_terms": ["recent_hit_rate"],
  "small_universe": false,
  "degenerate_ranking": true,
  "warning": "Ranking is degenerate this run — 17/31 candidates (55%) tie
    exactly at score 0.16 and fall back to alphabetical symbol order within
    that group (AAPL…XLK); weighted term(s) recent_hit_rate contributed no
    information this run (identical value for every candidate). Rank order
    within the affected tie group does NOT reflect a genuine signal
    difference. Diagnostic only; ranking output itself is unchanged."
}
```

This exactly reproduces the audit's numbers (17/31 = 55%, score 0.16, span
AAPL…XLK, zero-information `recent_hit_rate`). `_check_top100_daily` on this
payload returns `("warn", 31)` — confirmed directly.

### Tests added

- `tests/test_universe_sanitation.py`: `TestRankingDiagnostics` (6 tests:
  empty input, discriminative/non-degenerate, zero-variance, majority tie
  bucket with the real WS9 shape, zero-information term detection
  independent of tie majority, small-universe flag);
  `TestRealisticResolutionTiming` (4 tests, item 4's requirement — writes
  signals 12h old with **no** `outcome_return_1d` populated, reflecting real
  production resolution lag, unlike the pre-existing fixture that
  unrealistically pre-resolved a 12h-old signal; asserts `recent_hit_rate_1d`
  is `None`/zero-information at `lookback_days=1` and contrasts with a
  genuinely-resolved older signal at `lookback_days=30`);
  `TestRankingOutputUnchangedByDiagnostics` (regression guard: diagnostics
  never mutate `_rank_candidates`' output).
- `tests/test_daily_run_status.py`: `TestTop100DailyRankingQuality` (4 tests:
  non-degenerate → ok, degenerate-with-nonzero-candidates → warn,
  zero-candidates → warn as before, missing `ranking_diagnostics` key on an
  old-shaped artifact → ok, for backward compatibility).

---

## Test commands run

```
.venv/bin/python -m py_compile portfolio_automation/sim_governance/daily_governance_run.py \
  portfolio_automation/sim_governance/simulation_lane.py portfolio_automation/universe_sanitation.py \
  portfolio_automation/daily_run_status.py tests/test_ws13_discovery_adds_gate.py \
  tests/test_universe_sanitation.py tests/test_daily_run_status.py

.venv/bin/python -m pytest -q tests/test_ws13_discovery_adds_gate.py tests/test_universe_sanitation.py \
  tests/test_daily_run_status.py tests/test_sim_governance.py tests/test_sim_governance_pipeline.py \
  tests/test_flock_sim_governance.py tests/crowd_intelligence/test_unified_sim_governance.py \
  tests/test_approval_packet_pipeline.py tests/test_governance_digest_wiring.py
```

Result: **141 passed**, 0 failed (re-confirmed against the final committed
tree). Full suite was intentionally NOT run per task constraints.

## Notes / environment

This is a shared, non-isolated working directory: other concurrent agent
sessions checked out and committed to their own branches (`ws-01a`,
`feat/ws1-score-decomposition`, `feat/ws2-oos-states-health`, etc.) in this
same checkout while this task ran, at one point switching `HEAD` away from
`fix/ws9-13-silentzero` mid-task (visible in `git reflog`) and causing one
transient, non-reproducing test-suite flake (immediately confirmed as a
one-off by rerunning the identical command — all tests passed). The two
commits above were staged with explicit file paths (never `git add -A`) to
avoid picking up unrelated concurrent changes; `git diff` was used to confirm
each staged file's diff contained only this task's intended changes before
committing. `portfolio_automation/sim_governance/production_application.py`
and several other pre-existing dirty-tree files (present before this task
started, per the session's initial `git status`) were deliberately left
uncommitted and untouched.
