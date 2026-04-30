# Signal Registry

## Purpose

The Signal Registry is a config-driven catalog of known signal types used
throughout the system — event detection, portfolio risk, historical replay, and
future discovery layers. It provides a single source of truth for signal
metadata: what a signal means, whether it can drive action, whether it requires
corroboration from other signals before being promoted, and which pipeline layer
owns it.

Before the registry existed, signal identifiers were hardcoded as string
literals scattered across event detection, decision engine, scanner, and replay
modules. The registry does not change any of those string values. It adds a
lookup layer on top so that governance rules (actionable vs. discover-only,
corroboration requirements, default weights) are expressed once in a YAML file
rather than re-implemented per module.

---

## Config File

```
config/signal_registry.yaml
```

Loaded by `portfolio_automation/signal_registry.py` via `load_signal_registry()`.
Edit the YAML to add or disable signals; no code changes required.

---

## Key Concepts

### `actionable`

A signal marked `actionable: true` is permitted to drive portfolio decisions
directly through the decision engine. Only signals with a clear directional
interpretation and sufficient historical validation should be marked actionable.

Current actionable signals: `STRONG_MOVE_UP`, `STRONG_MOVE_DOWN`,
`BREAKOUT_PROXY`, `LEVERAGE_VIOLATION`, `CONCENTRATION_VIOLATION`,
`DRIFT_VIOLATION`, `PORTFOLIO_DRIFT`.

### `discovery_only`

A signal marked `discovery_only: true` surfaces as an observation or alert but
cannot directly promote a ticker to actionable status. These signals are inputs
to the corroboration layer, not outputs to the decision layer.

**Rule:** A discovery-only signal must always have `requires_corroboration: true`.
The registry validates this at load time and raises `SignalRegistryError` if the
constraint is violated.

Current discovery-only signals: `VOLATILITY_EXPANSION`,
`HISTORICAL_MOMENTUM_PROXY`.

### `requires_corroboration`

When `requires_corroboration: true`, this signal should be combined with at
least one other signal before being treated as a basis for action. Examples:
`VOLUME_SPIKE` (volume alone is ambiguous without price direction) and
`VOLATILITY_EXPANSION` (direction unclear from range alone).

Unknown signals always have `requires_corroboration: True` by design.

### `actionable` and `discovery_only` are mutually exclusive

The registry rejects any signal definition where both fields are `true`. A
discovery-only signal cannot directly drive portfolio actions; it exists to feed
corroboration logic only.

---

## Corroboration Rules

| Signal | Actionable | Discovery Only | Requires Corroboration |
|--------|-----------|---------------|------------------------|
| STRONG_MOVE_UP | ✓ | — | — |
| STRONG_MOVE_DOWN | ✓ | — | — |
| BREAKOUT_PROXY | ✓ | — | — |
| VOLUME_SPIKE | — | — | ✓ |
| VOLATILITY_EXPANSION | — | ✓ | ✓ |
| LEVERAGE_VIOLATION | ✓ | — | — |
| CONCENTRATION_VIOLATION | ✓ | — | — |
| DRIFT_VIOLATION | ✓ | — | — |
| PORTFOLIO_DRIFT | ✓ | — | — |
| HISTORICAL_MOMENTUM_PROXY | — | ✓ | ✓ |
| *unknown* | — | ✓ | ✓ |

---

## Module API

```python
from portfolio_automation.signal_registry import load_signal_registry, SignalRegistryError

registry = load_signal_registry()                    # uses config/signal_registry.yaml
registry = load_signal_registry("path/to/file.yaml") # explicit path

# Lookup
d = registry.get("STRONG_MOVE_UP")       # → SignalDefinition or None
d = registry.require("STRONG_MOVE_UP")   # → SignalDefinition or raises SignalRegistryError

# Enumeration
registry.all()                           # all definitions including disabled
registry.enabled()                       # only enabled=True definitions
registry.by_category("price_action")     # filter by category
registry.by_source_domain("scanner")     # filter by source domain

# Predicates (safe for unknown signal_ids)
registry.is_actionable("STRONG_MOVE_UP")         # True
registry.is_actionable("UNKNOWN_SIG")            # False — unknown is never actionable
registry.is_discovery_only("VOLATILITY_EXPANSION") # True
registry.requires_corroboration("VOLUME_SPIKE")   # True
registry.validate_signal_id("BREAKOUT_PROXY")     # True

# Annotation
ann = registry.annotate_signal("STRONG_MOVE_UP")
# → {"signal_id": ..., "known": True, "actionable": True, ...}

ann = registry.annotate_signal("UNKNOWN_SIG")
# → {"signal_id": ..., "known": False, "actionable": False,
#    "discovery_only": True, "requires_corroboration": True, ...}
```

---

## How to Add a New Signal

1. Open `config/signal_registry.yaml`.
2. Append a new entry under `signals:` following the existing pattern.
3. Choose `actionable: true` only if the signal has a clear direction and has
   been validated against outcome data. Default to `actionable: false` for new
   signals.
4. If the signal is directionally ambiguous or experimental, set
   `discovery_only: true` and `requires_corroboration: true`.
5. Run `python -m pytest -q tests/test_signal_registry.py` to verify the
   registry loads cleanly.
6. Add the new signal_id to `_EXPECTED_SIGNAL_IDS` in
   `tests/test_signal_registry.py` once it is part of the permanent catalog.

---

## How This Prepares for the Discovery Engine

The Discovery Engine (future Phase 0 Step 4) will scan for emerging themes and
surface candidate tickers. All candidate signals it emits will need registry
metadata before they can be passed downstream:

- New discovery signals start as `discovery_only: true`, `actionable: false`.
- They are promoted to `actionable: true` only after outcome validation confirms
  directional reliability.
- The `requires_corroboration` flag controls whether a discovery output needs
  confirmation from an independent signal source before reaching the decision
  engine.

The registry enforces this lifecycle: unknown signals are unconditionally
non-actionable, so a new signal emitted by the discovery engine cannot
accidentally influence the decision plan until it has been registered with
explicit governance metadata.

---

## Enforcement Summary

| Rule | Enforced by |
|------|-------------|
| `actionable` and `discovery_only` cannot both be `true` | `_validate_definition()` at load |
| `discovery_only: true` requires `requires_corroboration: true` | `_validate_definition()` at load |
| `default_weight` must be in `[0.0, 1.0]` | `_validate_definition()` at load |
| Duplicate `signal_id` rejected | `SignalRegistry.__init__()` at construction |
| Unknown signals are non-actionable | `is_actionable()`, `annotate_signal()` |
| Unknown signals require corroboration | `requires_corroboration()`, `annotate_signal()` |

---

## Module Location

```
portfolio_automation/signal_registry.py
```

Importable from any module in the repo:

```python
from portfolio_automation.signal_registry import (
    SignalDefinition,
    SignalRegistry,
    SignalRegistryError,
    load_signal_registry,
    VALID_CATEGORIES,
    VALID_SOURCE_DOMAINS,
)
```
