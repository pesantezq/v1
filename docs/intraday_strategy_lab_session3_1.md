# Intraday Strategy Lab — Session 3.1D Preregistration

Status target: `HYPOTHESIS_PREREGISTRATION_READY` / `SESSION_3_2_GO`.

This session freezes scientific claims only. It does **not** evaluate returns and
does not define orders, fills, costs, sizing, P&L, Sharpe, optimization, or
winner selection.

## Authority

Session 3.1 consumes the durable Session 3.0 gate directly:

```
session3/graduation/pointer.json
  -> immutable Session 3 population v2 evidence
  -> source/child verification
  -> population_audit.session3_0_status()
  -> immutable Session 3.1 preregistration set
  -> session3/preregistration/pointer.json
  -> strategy_definitions.session3_1_status()
```

`session3/irregular_session_population.json` is a rendered report and has no
authority. Changing or deleting it cannot grant or revoke `SESSION_3_2_GO`.

## Generation 1

### `SHORT_HORIZON_MEAN_REVERSION_V1`

Claim: a material 15-minute displacement partially reverses over the following
15 minutes.

Registered observation:

```
displacement = close_t / close_(t-3) - 1
SHORT prediction when displacement >= +0.005
LONG  prediction when displacement <= -0.005
otherwise NO_SIGNAL
```

Parameters:

| parameter | value | unit | rationale |
|---|---:|---|---|
| lookback_bars | 3 | 5-minute intervals | shortest registered multi-bar 15-minute displacement |
| displacement_threshold | 0.005 | return fraction | round ex-ante 0.5% material-move threshold; not tuned |
| evaluation_horizon_bars | 3 | 5-minute bars | symmetric 15-minute future evaluation |

A halt/gap resets the contiguous segment. Four post-boundary close endpoints are
required before the signal can become available again.

### `OPENING_RANGE_BREAKOUT_CONTINUATION_V1`

Claim: after an uninterrupted first-30-minute range is established, a strict
close outside the range contains same-direction continuation information.

```
range_high = max(high) over opening bars 1..6
range_low  = min(low)  over opening bars 1..6

LONG  when post-range close > range_high
SHORT when post-range close < range_low
otherwise NO_SIGNAL
```

`break_threshold = 0.0` is explicit. A buffered breakout is therefore a new
parameter set/version rather than an unrecorded degree of freedom.

The future evaluation horizon is six 5-minute bars. Session 3.1 declares that
outcome; it does not measure it.

### `EARLY_TO_LATE_INTRADAY_MOMENTUM_V1`

Claim: direction from certified session open through the first 30 minutes
contains same-direction information about the final 30 minutes.

```
early_return = close(opening bar 6) / open(opening bar 1) - 1

LONG  when early_return > 0
SHORT when early_return < 0
otherwise NO_SIGNAL
```

The prediction becomes knowable only after all six opening bars are knowable.
The evaluation window is the final six certified bars, anchored to session
close so early-close sessions retain the same semantic contract.

V1 deliberately does **not** use previous close or overnight return because the
frozen foundation does not certify dividend/corporate-action semantics needed
for that research.

## Halt-boundary compatibility

Strategy definitions bind:

- `IR.policy_fingerprint()`;
- `IR.HALT_BOUNDARY_POLICY_VERSION`;
- the content fingerprint of `IR.halt_boundary_policy()`;
- required Session 2 feature versions;
- custom primitive semantic versions;
- parameter-set fingerprints;
- semantic rule versions.

Therefore a foundation-policy or primitive-semantics change mints a different
strategy identity.

A required opening observation/range window that intersects an authoritative
halt is `FEATURE_UNAVAILABLE`. The evaluator consumes the foundation's
halt-boundary compatibility table mechanically. In particular,
`opening_range_construction` is blocked if a partial halt-boundary bar lies in
the required window; bars are never compressed into a fake uninterrupted
opening range.

## Immutable hypothesis registrations

Each `HypothesisRegistration` freezes:

- claim;
- strategy fingerprint;
- parameter-set fingerprint;
- formula-bound observation contract;
- prediction-known timing;
- future evaluation window;
- primary outcome;
- invalidation conditions;
- foundation binding;
- `optimization_performed = false`.

Result-informed changes cannot mutate V1. They require a new registration and
explicit amendment/supersession lineage.

## Pre-foundation prototype artifacts

Prototype files already present under the historical Session 3 output directory
are never deleted or overwritten. When the authoritative preregistration set is
minted, those files are recorded as:

```
status    = DRAFT_PRE_FOUNDATION
authority = NON_AUTHORITATIVE
relation  = superseded_by_this_authoritative_preregistration_set
```

Recognized old strategy IDs are mapped explicitly to their final registration:

- `OPENING_MOMENTUM_CONTINUATION_V1`
  -> `EARLY_TO_LATE_INTRADAY_MOMENTUM_V1`
- `OPENING_RANGE_BEHAVIOR_V1`
  -> `OPENING_RANGE_BREAKOUT_CONTINUATION_V1`
- `SHORT_HORIZON_MEAN_REVERSION_V1`
  -> `SHORT_HORIZON_MEAN_REVERSION_V1`

The old bytes remain preserved — and that preservation is **re-verified, not
asserted**. Every superseded artifact named by the immutable preregistration is
re-read from the exact path the evidence records, and its fingerprint is
recomputed on each verification. A deleted, modified, unreadable, or
out-of-tree artifact fails the gate closed.

The fingerprint contract hashes **meaning, not formatting**: an artifact that
parses as JSON is hashed from its parsed payload, so reindenting or reordering
keys is not corruption; anything unparseable falls back to its raw bytes.
Discovery and verification share one helper so the two can never drift into
disagreeing about what "unchanged" means.

Paths recorded in preregistration evidence are relative to the Intraday root and
must resolve inside it. Verification refuses absolute paths and any path that
escapes the tree, so tampered evidence cannot turn a gate check into an
arbitrary file read.

## Research burden

Frozen before outcome observation:

```json
{
  "strategy_families": 3,
  "registered_hypotheses": 3,
  "parameter_sets": 3,
  "directional_subhypotheses": 6,
  "optimization_trials": 0,
  "post_result_amendments": 0,
  "optimization_performed": false
}
```

Negative and abandoned research must remain part of the later testing burden;
Session 4 may not count only winners.

## Gate

`SESSION_3_2_GO` requires all of the following:

1. durable Session 3.0 evidence still verifies and remains `SESSION_3_1_GO`;
2. a content-addressed Session 3.1 preregistration set exists;
3. the mutable pointer selects that exact immutable object;
4. strategy and hypothesis bytes recompute from current code;
5. population and halt policy fingerprints still match;
6. parameter/feature/primitive versions still match;
7. the frozen research-burden record matches;
8. every superseded legacy artifact still exists, still resolves inside the
   Intraday root, and still reproduces its recorded fingerprint;
9. `strategy_validation_allowed` remains false.

A pointer is selection, not authority.

## Certification

Minting is a single narrow entrypoint that composes the steps in their only safe
order. It owns no authority: the gate it reports is whatever `session3_1_status`
derives from durable evidence.

```python
from portfolio_automation.intraday_lab.strategy_definitions import (
    freeze_session3_1_preregistration,
)

result = freeze_session3_1_preregistration(root=".")
```

It fails closed. A pointer is installed only after the persisted object has
verified, so a refused certification leaves no selection behind that could be
mistaken for a successful one. It raises rather than returning a degraded
success when Session 3.0 is not ready or the persisted set does not verify.

## Next session

Session 3.2 may map predictions to a deterministic hypothetical execution model.
It must preserve the frozen temporal rule that an input cannot be acted upon
before its `known_at`. Nothing in Session 3.1 implies an entry, fill, or trade.
