# Alert Lifecycle

Last verified against `watchlist_scanner/scanner.py`, `watchlist_scanner/alert_filter.py`, `watchlist_scanner/postprocess.py`, `watchlist_scanner/state.py`, and `state_store.py`.

## Scope

This document covers the watchlist alert lifecycle, not legacy finance email recommendations.

## Lifecycle Stages

```text
scan result
    -> alert basis detection
    -> routing priority decision
    -> emission filter
    -> cooldown check
    -> action suppression check
    -> surfaced alert record
    -> notification metadata
    -> outcome resolution
```

## 1. Alert Creation Rules

Primary logic:
`watchlist_scanner/scanner.py:_evaluate_alert_decision`

An alert basis can come from:

- `price_move`
- `volume_spike`
- `signal_score`
- `sentiment`

Possible routed priorities:

- `high`
- `normal`
- `watch`
- `None` for suppression

High-level routing behavior:

- High-confidence observable moves with structural confirmation can route to `high`.
- Medium-confidence signals need more confirmation and often route to `normal` or `watch`.
- Low-confidence results are usually suppressed unless an observable or exceptional move is present.

## 2. Evidence Thresholds

Evidence breadth is tracked independently from raw signal strength.

Key fields:

- `confirmation_count`
- `evidence_categories`
- `evidence_breadth`
- `alert_quality_tier`

Important thresholds:

- `signals.min_evidence_count`
  Default `2` for medium-confidence emission.
- `alert_quality_tier`
  Common states: `broad`, `confirmed`, `thin`, `none`

## 3. Emission Filter

Primary logic:
`watchlist_scanner/alert_filter.py:should_emit_alert`

Default gates:

- `min_signal_score = 0.50`
- `min_confidence_score = 0.50`
- `min_evidence_count = 2`

Tier behavior:

- `high` tier
  Allowed immediately once routed.
- `medium` tier
  Requires evidence count threshold.
- `low` tier
  Suppressed before emission.

## 4. Cooldowns

Primary logic:
`watchlist_scanner/alert_filter.py:cooldown_decision`

Default tier cooldowns:

- `high -> 6h`
- `medium -> 24h`
- `low -> 72h`

State table:
`alert_events`

Fingerprint identity:

- `ticker`
- `watchlist_source`
- primary trigger type

State hash:

- bucketed `signal_score`
- bucketed `confidence_score`
- `confidence_band`
- `data_quality`
- `alert_priority`

## 5. Cooldown Bypass Rules

Cooldown may be bypassed when:

- tier upgrade
- priority upgrade
- material state-hash change
- optional high-confidence strong-signal bypass
- effective-score jump above configured reset delta

This is why repeated alerts can legitimately re-surface without waiting for the full base cooldown.

## 6. Action Suppression

Primary logic:
`watchlist_scanner/postprocess.py:_confidence_action_decision`

This layer is separate from alert existence.

A row may:

- remain an alert
- be recorded in outputs
- still be marked non-actionable because confidence is too weak for action

Important behavior:

- fallback opportunities are non-actionable by design
- degraded mode raises the actionable confidence bar
- cooldown-suppressed rows are non-actionable

## 7. Outcome Tracking

State table:
`watchlist_alert_outcomes`

A surfaced alert records:

- fingerprint and state hash
- `surfaced_at`
- `baseline_price`
- `baseline_signal_score`
- `baseline_confidence_score`
- alert quality and portfolio overlay metadata
- evaluation window
- outcome status fields

Default evaluation window:
`1d,3d,5d,10d`

Current persisted resolution fields include:

- `evaluation_price`
- `return_pct`
- `evaluated_at`
- `outcome_label`
- `outcome_status`
- `outcome_pending`
- `resolved_at`

## 8. Signal Feedback Loop

Separate from alert outcomes, every result row can be tracked in:
`watchlist_signal_feedback`

That table stores:

- `signal_score`
- `confidence_score`
- `effective_score`
- `conviction_score`
- `normalized_allocation`
- regime/theme/portfolio-fit context
- forward 1d/3d/7d outcome fields

This is the learning loop for score calibration and future weighting, not a live execution engine.

## 9. Invariants

- Cooldown suppression must never erase the underlying result row.
- Alert state persistence must not change base ranking semantics.
- Outcome tracking must remain explainable and keyed to persisted baseline fields.
- Observe-only output is allowed to become richer, but not to mutate into auto-execution behavior.
