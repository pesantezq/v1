# Discovery Engine

## Purpose

The Discovery Engine identifies and tracks research candidates from news/event text records without affecting official portfolio decisions. It operates exclusively in the research/sandbox lane.

**Discovery candidates are NOT buy/sell recommendations.**
**Discovery candidates are NOT official portfolio actions.**

## What Discovery v1 Does

- Extracts ticker candidates from news/event records (cashtag, parenthetical, source-provided)
- Classifies the event type driving each record (earnings, merger, legal risk, etc.)
- Scores candidates by mention count, source diversity, event confidence, and risk signals
- Computes deterministic corroboration score (source diversity 35%, mention 20%, event strength 25%, persistence 20%, risk penalty −0.20)
- Assigns research-lane statuses: DISCOVERED, WATCH, REJECTED — **WATCH requires corroboration_met=True**
- Maintains sandbox memory across runs (`discovery_memory.json`); persistence data feeds corroboration scoring
- Writes sandbox-only artifacts to `outputs/sandbox/discovery/`

## What Discovery v1 Does NOT Do

- Does NOT produce buy/sell recommendations
- Does NOT mutate the official watchlist
- Does NOT update official portfolio state, allocations, or risk limits
- Does NOT write to `outputs/latest/`, `outputs/policy/`, `outputs/portfolio/`, or `outputs/users/`
- Does NOT make network calls
- Does NOT call AI/LLM APIs
- Does NOT produce PROMOTED, VALIDATED, ACTIONABLE, BUY, or SELL statuses
- Does NOT execute trades (no mode does — this system is advisory-only)

## Two-Lane Operating Model

All discovery work runs in the **research/sandbox lane** governed by `RunMode.DISCOVERY`. The official lane (`DAILY`, `MANUAL_UPDATE`, `WEEKLY_REVIEW`) cannot write discovery sandbox artifacts.

| Lane | Modes | May Write |
|------|-------|-----------|
| Official | DAILY, MANUAL_UPDATE, WEEKLY_REVIEW | `outputs/latest/`, `outputs/policy/`, `outputs/portfolio/` |
| Research | DISCOVERY, BACKTEST, HISTORICAL_REPLAY | sandbox and/or backtest namespaces depending on mode |

## Sandbox-Only Behavior

Every artifact written by the discovery engine:
- Includes `"discovery_only": true`
- Includes `"sandbox_only": true`
- Includes `"observe_only": true`
- Includes the disclaimer: *"Discovery candidates are not buy/sell recommendations."*
- States: *"Official watchlist and recommendations were not modified."*

## Event Types

| Event Type | Description |
|---|---|
| `earnings` | Earnings results, revenue beats/misses |
| `guidance` | Forward guidance raises/lowering |
| `analyst_action` | Upgrades, downgrades, price target changes |
| `product_launch` | New product announcements or releases |
| `partnership` | Strategic alliances, joint ventures, deals |
| `regulatory` | FDA approvals, SEC filings, compliance actions |
| `macro_theme` | Fed policy, inflation, interest rates |
| `merger_acquisition` | Acquisitions, mergers, buyouts |
| `legal_risk` | Lawsuits, litigation, securities fraud |
| `financing` | IPO, equity/debt offerings, capital raises |
| `management_change` | CEO/CFO changes, leadership succession |
| `unknown` | No keyword match — insufficient signal |

`risk_flag=True` for `legal_risk` events and for `regulatory` events that contain negative-signal keywords (investigation, probe, penalty, fine, enforcement).

## Candidate Statuses (v1 only)

| Status | Meaning |
|---|---|
| `WATCH` | Score meets threshold AND `corroboration_met=True` — worth monitoring in the research lane |
| `DISCOVERED` | Extracted and scored but below WATCH threshold, or score meets threshold but corroboration not met |
| `REJECTED` | Risk flag with low event confidence, or below minimum threshold |

**Never produced:** PROMOTED, VALIDATED, ACTIONABLE, BUY, SELL.

Every candidate carries corroboration fields:
```json
{
  "corroboration_required": true,
  "corroboration_met": false,
  "corroboration_score": 0.0,
  "corroboration_level": "none",
  "corroboration_sources": []
}
```

`corroboration_met=True` requires `corroboration_score >= 0.65` (level `"strong"`).

## Corroboration Scoring

Implemented in `portfolio_automation/discovery/corroboration.py`.

| Component | Weight | Normalization |
|---|---|---|
| `source_diversity` | 35% | `min(unique_sources / 4, 1.0)` |
| `mention` | 20% | `min(log2(mentions+1) / 3.0, 1.0)` |
| `event_strength` | 25% | `event_confidence` (direct, 0.0–1.0) |
| `persistence` | 20% | `min(seen_runs / 3, 1.0)` |
| `risk_penalty` | −0.20 | Applied when `risk_flag=True` |

Levels:

| Level | Score Range | `corroboration_met` |
|---|---|---|
| `none` | [0.00, 0.30) | `False` |
| `weak` | [0.30, 0.50) | `False` |
| `moderate` | [0.50, 0.65) | `False` |
| `strong` | [0.65, 1.00] | `True` |

`seen_runs` comes from `DiscoveryMemory` (prior runs only — not the current run). A first-run candidate with strong evidence from 4+ sources and high confidence can still meet corroboration. Most WATCH candidates will have persistence from multiple runs.

No candidate may become an official watchlist entry or recommendation without explicit operator action in `MANUAL_UPDATE` mode with `approved=True`.

## Artifact Contract

All artifacts written to `outputs/sandbox/discovery/`:

| File | Contents |
|---|---|
| `emerging_candidates.json` | WATCH + DISCOVERED candidates with scores, event types, evidence |
| `rejected_candidates.json` | REJECTED candidates with rejection reasons |
| `discovery_memory.json` | Persistent candidate history across runs |
| `discovery_memo_section.md` | Human-readable research memo with disclaimer |

`emerging_candidates.json` and `rejected_candidates.json` carry these operator-facing top-level fields:

| Field | Value |
|---|---|
| `observe_only` | `true` |
| `discovery_only` | `true` |
| `sandbox_only` | `true` |
| `disclaimer` | Warning string |

`discovery_memory.json` is internal sandbox memory. It carries `discovery_only` and `sandbox_only` but does **not** include `observe_only` or `disclaimer` — those fields are for operator-facing report artifacts only.

## Run Mode Governance

Enforced via `portfolio_automation/run_mode_governance.py`.

```python
from portfolio_automation.run_mode_governance import (
    assert_can_write_namespace, RunMode, RunModeViolation
)

# DISCOVERY mode: allowed
assert_can_write_namespace(RunMode.DISCOVERY, "sandbox")   # passes

# BACKTEST mode: also allowed for offline sandbox evaluation
assert_can_write_namespace(RunMode.BACKTEST, "sandbox")    # passes

# DAILY mode: blocked
assert_can_write_namespace(RunMode.DAILY, "sandbox")       # raises RunModeViolation
```

`write_discovery_reports` calls `assert_can_write_namespace` before any file I/O.
`DISCOVERY` and `BACKTEST` may write sandbox discovery artifacts.
`DAILY`, `MANUAL_UPDATE`, `WEEKLY_REVIEW`, and `HISTORICAL_REPLAY` raise `RunModeViolation`.

## Modules

| Module | Role |
|---|---|
| `news_ticker_discovery.py` | Deterministic ticker extraction (cashtag, parenthetical, source-provided) |
| `event_classifier.py` | Keyword-based event type classification |
| `corroboration.py` | Deterministic corroboration scoring (`compute_corroboration`, `CorroborationResult`) |
| `candidate_promotion_engine.py` | Scoring, status assignment, corroboration integration |
| `discovery_memory.py` | Persistent sandbox memory (load/update/serialize) |
| `discovery_reports.py` | Sandbox artifact writer + `run_discovery_engine` orchestrator |
| `__init__.py` | Public API re-exports |

## Entry Point

```python
from portfolio_automation.discovery import run_discovery_engine

summary = run_discovery_engine(
    records=[
        {"title": "$NVDA beats earnings quarterly results", "source": "reuters"},
        {"title": "NVIDIA (NVDA) raises guidance outlook", "source": "bloomberg"},
    ],
    run_mode="discovery",
    run_id="2026-05-01_discovery",
    base_dir="outputs",
    write_files=True,
)

print(summary["watch_count"])          # integer
print(summary["discovery_only"])       # True
print(summary["can_execute_trades"])   # False
```

## No Buy/Sell Rule

The discovery engine never produces a buy or sell signal. The `CandidateStatus` enum contains only: `DISCOVERED`, `WATCH`, `REJECTED`. Governance flags are hard-coded, not configurable:

```python
candidate.discovery_only = True       # always
candidate.sandbox_only = True         # always
candidate.corroboration_required = True  # always
# corroboration_met is computed — True only when corroboration_score >= 0.65
```

## No Official Watchlist Mutation Rule

The discovery engine does not read or write the official watchlist. Candidates must go through a future GUI approval workflow and `MANUAL_UPDATE` mode (with `approved=True`) before any official action can be taken.

## Future Path

| Step | Description |
|---|---|
| ~~Corroboration~~ | ~~Cross-validate discovery candidates across multiple independent sources before promoting to WATCH~~ — **complete** |
| GUI approval workflow | Operator reviews WATCH candidates and approves promotion proposals |
| Manual promotion proposal | `MANUAL_UPDATE` + `approved=True` required for any official action |
| Historical backtest for discovery | Run discovery against historical data to calibrate scoring thresholds |
| Daily Memo discovery section | Add a sandbox-only research summary to the daily memo (read-only) |

## Known v1 Limitation

`QQQ` is treated as a noise token by default in `news_ticker_discovery.py`.
Adding `QQQ` to `known_universe` does not override that filter because noise filtering runs first.

This is acceptable for conservative v1 behavior, but it should be revisited if ETF discovery
becomes an explicit goal in a future enhancement.
