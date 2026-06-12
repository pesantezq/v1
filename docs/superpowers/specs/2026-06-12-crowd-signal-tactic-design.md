# Design — Crowd-Signal Tactic (Sandbox Sub-Project 2)

> Status: design approved (brainstorm 2026-06-12). Sandbox-only · observe-only ·
> no auto-trading. Sub-project 2 of the portfolio-simulation workstream; depends
> on **sub-project 1** (`docs/superpowers/specs/2026-06-12-portfolio-tactic-backtest-design.md`).

## 1. Goal

Turn the Crowd Radar / Public Knowledge Velocity Layer into a **backtestable +
forward-trackable portfolio tactic**: a capped sleeve that tilts toward the
"useful/early" crowd states and an overlay that avoids the "caution/late" ones,
evaluated against the operator's real portfolio.

## 2. The hard constraint (why this tactic is special)

There is **no faithful historical crowd data** — Crowd Radar started collecting
now, ships disabled, and the Reddit API/ToS preclude reconstructing day-by-day
historical sentiment. A true "what would following the crowd have returned"
backtest would fabricate signals that were never observed. Therefore the tactic
is evaluated two ways:

1. **Forward shadow-tracking (the real track record).** Paper-trade the sleeve
   from today forward; measure realized outcomes as prices resolve. Honest but
   matures over weeks/months.
2. **Proxy historical backtest (illustrative, explicitly labeled).** A
   deterministic volume/momentum stand-in for "crowd attention" run over the 5y
   archive, stamped as a proxy — never trusted as the real signal's record.

## 3. Tactic-interface extension (into sub-project 1)

Sub-project 1's `Tactic` holds a static `target_weights`. Add an optional
resolver so the engine can support time-varying tactics:

```python
def target_weights_asof(date: str, signal_context: dict) -> dict[str, float]
```

Static tactics ignore it (return their constant vector). At each rebalance date
the engine calls `target_weights_asof`; the crowd tactic returns a date-dependent
vector built from crowd states as-of that date (real forward, or proxy historical).

New module: `portfolio_automation/portfolio_sim/crowd_tactic.py`.

## 4. Sleeve construction (at a given date)

Inputs: operator holdings/target (core) + crowd states as-of-date.

- **Core** = holdings/target weights scaled to `1 − sleeve_total`.
- **Sleeve** (`≤15%` total, `≤5%`/idea — reuses config boom-bucket caps):
  names in `emerging_dd / crowd_validation / contrarian_neglect`, **weighted by
  `crowd_research_priority_score`** (the existing capped score), top-N until the
  sleeve fills. Renormalized within the sleeve cap.
- **Avoid-overlay**: names in `hype_acceleration / reflexive_squeeze_risk /
  crowd_exhaustion` are excluded from the sleeve; if such a name is also a
  **core holding**, emit an observe-only underweight flag **and apply a modest
  `×0.8` trim** to that core position in the simulation (gentle, capped), so the
  backtest reflects the avoid signal. Freed weight returns to cash/remaining core.

All caps and the priority/overlay parameters are recorded in the artifact +
Strategy Catalog (sub-project 1 documentation rule).

## 5. Track 1 — Forward shadow-tracking (real evaluation)

Each weekly run:
- Snapshot the current sleeve as a **paper position** → append to
  `outputs/sandbox/discovery/social_signal_history.json` with as-of crowd states,
  entry date, and entry prices.
- On resolution offsets (1d/5d/20d/60d) join forward prices (archive/FMP) →
  realized return vs the operator baseline + vs SPY/QQQ; write resolved records.
- Resolved records flow into the existing **sample-gated**
  `social_signal_backtest.json` (built in the Crowd Radar feature) and fill
  `shadow_portfolios.json`'s `would_have_helped_portfolio`.
- Reuses the existing outcome-resolution machinery
  (`decision_outcome_tracker` / `resolution_due_probe` patterns); no new resolver.

## 6. Track 2 — Proxy historical backtest (illustrative)

Deterministic "crowd attention" proxy from the 5y archive — **price/volume only,
no sentiment/DD** (not recoverable historically):

- `attention = volume z-score vs trailing 20d`; `momentum = trailing return`.
- Pseudo-state map (partial, attention+price only):
  - rising attention + moderate momentum + not-extended → pseudo `emerging_dd`
  - extreme attention spike + high momentum → pseudo `hype_acceleration` (caution)
  - high attention + rolling-over momentum → pseudo `crowd_exhaustion` (caution)
- Run §4 sleeve logic over history using pseudo-states via the same
  `target_weights_asof` path and sub-project 1's engine.
- Output stamped `proxy: true`, `measures: "volume/momentum attention, NOT real
  crowd evidence/sentiment"`, surfaced as illustrative only.

## 7. Outputs

- `social_signal_history.json` (forward ledger; extended) +
  `social_signal_backtest.json` (resolved, sample-gated) — both in
  `outputs/sandbox/discovery/`.
- `outputs/sandbox/crowd_tactic_backtest.json` — the proxy historical run,
  labeled. Observe-only envelope.
- Strategy Lab leaderboard: one tactic row marked **forward-maturing** +
  **proxy** (never presented as a settled track record).
- Strategy Catalog entry: sleeve logic, caps, priority-weighting, overlay trim,
  proxy caveat, and decision rationale (per sub-project 1's documentation rule).

## 8. Governance

Observe-only, sandbox-only, caps from config, no trade verbs, never writes
`decision_plan.json` / `signal_registry.yaml`. Research-priority framing only.
Run-mode discovery/backtest. Non-blocking. Default-inert alongside Crowd Radar
(no crowd data → tactic produces an empty sleeve and degrades cleanly).

## 9. Cadence + health coverage

- Forward snapshot/resolution: weekly (with the backtest engine + Crowd Radar).
- Register `crowd_tactic_backtest.json` in `artifact_registry.yaml` (weekly).
- Extend `monthly-tool-analysis` (quant lens, `portfolio-attribution-analyst`):
  read the forward sleeve's resolved hit-rate / vs-baseline once matured;
  content-liveness: tactic ran but sleeve empty for N weeks (Crowd Radar dark).

## 10. Tests

- sleeve caps respected (≤15% / ≤5%), priority-weighting order, top-N fill.
- avoid-overlay: caution names excluded from sleeve; core holding in caution →
  flag + `×0.8` trim applied in sim.
- proxy pseudo-state mapping (volume-z/momentum thresholds → expected pseudo-state).
- forward ledger append + resolution join (synthetic prices) → resolved record.
- sample-gating: under-sampled forward sleeve labeled `insufficient_data`.
- look-ahead guard: proxy uses only data dated ≤ sim date.
- labeled-proxy flag present on the historical artifact.
- no-mutation invariant (`decision_plan.json` untouched); `observe_only=True`.

## 11. Deferred (YAGNI)

- Sentiment/evidence in the proxy (not in the price archive).
- Auto-sizing beyond the fixed caps.
- Options/short-interest context in the live tactic (blocked until that feed is
  wired for Crowd Radar's `reflexive_squeeze_risk`).

## 12. Risks

- The proxy can be mistaken for the real signal's record — mitigated by the
  hardcoded `proxy: true` label and the Strategy Lab "proxy" marker.
- Forward track has no payoff until enough resolutions accumulate (months) —
  expected and labeled `insufficient_data` until then (same maturity model as
  the Pattern-Loop OOS window).
