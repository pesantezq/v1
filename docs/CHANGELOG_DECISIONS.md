# Changelog Decisions

Use this file to record high-impact changes that affect meaning, not just implementation.

## How To Use This File

Add an entry whenever a change affects:

- scoring semantics
- ranking weights
- conviction or sizing behavior
- allocation caps
- output contracts
- SQLite schema
- architecture boundaries

## Required Entry Format

### Date

`YYYY-MM-DD`

### Area

One of:

- scoring
- allocation
- alerts
- state
- output_contract
- architecture
- evaluation

### Files / Functions

Name the exact files and functions changed.

### Decision

Describe what changed in plain language.

### Why

State the problem being solved.

### Invariants Preserved

Explicitly state what did not change.

### Downstream Impact

List affected artifacts, tests, and GUI surfaces.

---

## Baseline

### Date

2026-04-28

### Area

architecture

### Files / Functions

- `main.py:run_portfolio_update`
- `watchlist_scanner/__main__.py:run`
- `watchlist_scanner/scanner.py`
- `watchlist_scanner/confidence.py:compute_confidence`
- `watchlist_scanner/conviction.py:apply_conviction_layer`
- `watchlist_scanner/portfolio_construction.py:apply_portfolio_construction_layer`
- `allocation_engine.py:suggest_allocation`
- `policy_evaluator/*`
- `state_store.py`

### Decision

Documented the current baseline architecture, output contracts, scoring meanings, and state schema without changing application behavior.

### Why

AI agents and maintainers need a stable source of truth before making behavior changes.

### Invariants Preserved

- advisory-only operation
- separate `signal_score` and `confidence_score`
- output file paths and top-level contract shapes
- existing SQLite schema

### Downstream Impact

- documentation consumers
- AI coding agents
- future regression review
