# Run Mode Governance

## Purpose

Centralizes run-mode declarations and enforces two-lane operating boundaries so every pipeline run clearly states what it is allowed to read, write, and mutate. Prevents discovery or backtest runs from producing official portfolio artifacts.

## Two-Lane Operating Model

### Official Lane

Produces authoritative portfolio artifacts consumed by the GUI, memo, and recommendation stack.

| Mode | Purpose |
|------|---------|
| `daily` | Official daily portfolio intelligence run |
| `manual_update` | User-approved official state changes |
| `weekly_review` | Digest/review reports (maps from legacy `weekly` and `monthly` CLI args) |

### Discovery / Research Lane

Autonomous research and candidate-generation. Never alters official portfolio decisions, allocations, risk limits, watchlists, or trades. Outputs are sandbox/research-only.

| Mode | Purpose |
|------|---------|
| `discovery` | AI/signal research, candidate discovery |
| `backtest` | Simulation; writes `outputs/backtest/` only |
| `historical_replay` | Offline replay; writes `outputs/backtest/` only |

## No Auto-Trading

`can_execute_trades` is `False` for every mode. This system is advisory-only and must never place trades or invoke broker APIs.

## Run Mode Table

| Mode | Official Lane | Research Lane |
|------|:---:|:---:|
| `daily` | ✓ | |
| `manual_update` | ✓ | |
| `weekly_review` | ✓ | |
| `discovery` | | ✓ |
| `backtest` | | ✓ |
| `historical_replay` | | ✓ |

## Permissions Table

| Permission | daily | manual_update | discovery | weekly_review | backtest | historical_replay |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| `can_write_latest` | ✓ | ✓ | | ✓ | | |
| `can_write_policy` | ✓ | ✓ | | | | |
| `can_write_portfolio` | ✓ | ✓ | | ✓ | | |
| `can_write_user_state` | | ✓ | | | | |
| `can_write_historical` | | | | | ✓ | ✓ |
| `can_write_sandbox` | | | ✓ | | ✓ | |
| `can_write_discovery` | | | ✓ | | | |
| `can_update_official_watchlist` | | ✓ | | | | |
| `can_change_allocations` | | ✓ | | | | |
| `can_change_risk_limits` | | ✓ | | | | |
| `can_emit_recommendations` | ✓ | ✓ | | ✓ | | |
| `can_execute_trades` | | | | | | |
| `requires_manual_approval` | | ✓ | | | | |

## Approval Rules

`manual_update` is the only mode that may mutate official portfolio state (allocations, risk limits, watchlist). All such mutations require `approved=True` to be passed explicitly. Without approval, `RunModeViolation` is raised.

```python
# Blocked — no approval
assert_can_update_portfolio_state(RunMode.MANUAL_UPDATE, approved=False)  # raises

# Permitted — explicit approval
assert_can_update_portfolio_state(RunMode.MANUAL_UPDATE, approved=True)   # passes
```

## Output Namespace Rules

| OutputNamespace | Allowed modes |
|---|---|
| `outputs/latest/` | `daily`, `manual_update`, `weekly_review` |
| `outputs/policy/` | `daily`, `manual_update` |
| `outputs/portfolio/` | `daily`, `manual_update`, `weekly_review` |
| `outputs/users/` | `manual_update` only |
| `outputs/backtest/` | `backtest`, `historical_replay` |
| `outputs/sandbox/` | `discovery`, `backtest` |

## Legacy CLI Aliases

The existing `--run-mode` CLI argument accepts `daily`, `weekly`, and `monthly`. These map to governance modes as follows:

| CLI value | RunMode |
|---|---|
| `daily` | `DAILY` |
| `weekly` | `WEEKLY_REVIEW` |
| `monthly` | `WEEKLY_REVIEW` |

## API

```python
from portfolio_automation.run_mode_governance import (
    RunMode,
    RunModeViolation,
    normalize_run_mode,
    get_run_mode_policy,
    create_run_mode_context,
    validate_output_write,
    assert_can_write_namespace,
    assert_can_update_portfolio_state,
    assert_can_update_watchlist,
    assert_can_emit_recommendation,
    is_official_mode,
    is_research_only_mode,
)

# Normalize from string (handles legacy aliases)
mode = normalize_run_mode("weekly")        # → RunMode.WEEKLY_REVIEW
mode = normalize_run_mode("daily")         # → RunMode.DAILY

# Create context
ctx = create_run_mode_context("daily")
print(ctx.mode)                            # RunMode.DAILY
print(ctx.policy.can_write_latest)         # True
print(ctx.policy.can_execute_trades)       # False (always)

# Soft check — returns bool
allowed = validate_output_write(RunMode.DISCOVERY, "latest")   # False
allowed = validate_output_write(RunMode.DISCOVERY, "sandbox")  # True

# Hard assertion — raises RunModeViolation
assert_can_write_namespace(RunMode.DAILY, "latest")   # passes
assert_can_write_namespace(RunMode.DAILY, "sandbox")  # raises

# Portfolio state changes (requires approval in MANUAL_UPDATE)
assert_can_update_portfolio_state(RunMode.MANUAL_UPDATE, approved=True)   # passes
assert_can_update_portfolio_state(RunMode.MANUAL_UPDATE, approved=False)  # raises

# Recommendations
assert_can_emit_recommendation(RunMode.DAILY)      # passes
assert_can_emit_recommendation(RunMode.DISCOVERY)  # raises

# Lane detection
is_official_mode(RunMode.DAILY)             # True
is_research_only_mode(RunMode.DISCOVERY)    # True
```

## Integration with Discovery Engine

The Discovery Engine Foundation is now implemented.

For normal research runs, discovery should use `RunMode.DISCOVERY`. The governance layer enforces that:
- Discovery candidates are written to `outputs/sandbox/` only
- Discovery runs cannot write `outputs/latest/` or emit official recommendations
- Candidates require corroboration before promotion to the official lane

For offline evaluation, `RunMode.BACKTEST` may also write sandbox discovery artifacts.
`RunMode.HISTORICAL_REPLAY` may not.

## Module

`portfolio_automation/run_mode_governance.py`

No I/O, no file writes, no side effects. Pure in-memory governance layer.
