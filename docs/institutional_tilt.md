# Institutional Tilt (13F Tactic)

## Purpose

`portfolio_automation/portfolio_sim/institutional_tilt.py` is the institutional
(SEC 13F) **tilt primitive** and its Strategy Lab tactic + variants. It applies a
pure, deterministic, point-in-time-safe, long-only, bounded, and normalized tilt
on top of a core weight vector, driven by independence-adjusted institutional
consensus signals. It never adds a symbol merely because one famous manager
initiated a position — a qualifying signal must clear both a minimum consensus
confidence and a minimum effective-independent-manager count.

This is a **focused tactic doc**. The authoritative, full-subsystem picture
(signal derivation, PIT/anti-look-ahead rules, options handling, health, and
artifacts) lives in [`docs/INSTITUTIONAL_INTELLIGENCE.md`](INSTITUTIONAL_INTELLIGENCE.md).
The generated per-variant metrics and rationale live in
[`docs/STRATEGY_CATALOG.md`](STRATEGY_CATALOG.md).

---

## Observe-Only Behavior

This tactic is sandbox-only and additive. Every emitted tactic carries hardcoded
metadata `observe_only: true`, `sandbox_only: true`, and
`feeds_decision_engine: false`. It never writes `outputs/latest/decision_plan.json`,
never changes any of the six protected scores, and never mutates production
allocations or watchlist state. When no qualifying signal exists for the evaluated
date, the tilt is a no-op and the core anchor is returned unchanged (normalized).

---

## Caps + Thresholds

Defaults mirror `config/base.json:institutional_intelligence.strategy` and are
empirically tested by the Strategy Lab, not assumed.

| Knob | Default | Meaning |
|------|---------|---------|
| `max_total_sleeve` | `0.10` | Total institutional sleeve budget (sum of absolute tilts) |
| `max_new_position` | `0.02` | Per-position cap for a newly added (non-core) symbol |
| `max_existing_tilt` | `0.02` | Per-position cap when tilting an existing core holding |
| `max_distribution_trim` | `0.02` | Per-position cap for trimming on distribution |
| `min_confidence` | `0.55` | Minimum consensus confidence to fund any tilt |
| `min_effective_managers` | `1.5` | Minimum effective-independent managers to fund any tilt |

Signals below either gate (or absent/stale) produce **no tilt**. Directional
magnitude scales with `abs(consensus_score) * cap * confidence` (times an optional
`strategy_fit`), and distribution is long-only — a trim floors the weight at 0 and
never goes short. The total sleeve budget is enforced across the deterministic
pass (strongest absolute signal first, then symbol), truncating the last tilt if
the budget would be exceeded.

---

## Module API

- `InstitutionalCaps` — frozen dataclass holding the six knobs above.
- `apply_institutional_tilt(core_weights, signals, caps=None, *, use_strategy_fit=False, crowding_aware=False, contrarian=False) -> dict[str, float]`
  — pure primitive returning long-only, normalized weights that sum to 1.
- `InstitutionalTactic(TimeVaryingTactic)` — PIT-safe tactic; `target_weights_asof`
  uses the nearest signal snapshot with `date <= evaluated date` (no look-ahead).
- `institutional_variants(core_weights, signals_by_date, *, caps=None)` — the five
  Strategy Lab variants over identical inputs: `institutional_single_manager`
  (diagnostic), `institutional_consensus`, `institutional_consensus_strategy_fit`,
  `institutional_consensus_crowding_aware`, and
  `institutional_contrarian_crowding_diagnostic`.

The `crowding_aware` variant dampens crowded adds by `(1 - crowding_score)`; the
`contrarian` variant treats a crowded accumulation as a caution and trims instead.

---

## Cross-References

- Subsystem doc (authoritative): [`docs/INSTITUTIONAL_INTELLIGENCE.md`](INSTITUTIONAL_INTELLIGENCE.md)
- Generated tactic metrics + rationale: [`docs/STRATEGY_CATALOG.md`](STRATEGY_CATALOG.md)
- Decision log: `docs/CHANGELOG_DECISIONS.md` (Institutional Intelligence entries)
