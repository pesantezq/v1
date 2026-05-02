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

`rejected_candidates.json` stores rejected rows under the top-level `candidates` key and uses `total_rejected` instead of `total_candidates`. GUI loaders retain backward compatibility with older fixtures that used `rejected_candidates`.

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

The discovery engine does not read or write the official watchlist. Approval decisions recorded via the GUI approval workflow are sandbox research notes only — they do not trigger any official action. A future `MANUAL_UPDATE` mode step (with `approved=True`) would be required before any official action could be taken.

## GUI Approval Workflow (Sandbox Only)

Implemented in `portfolio_automation/discovery/approval_workflow.py`.

The approval workflow is a **sandbox audit layer only**. It allows the operator to record research review decisions for WATCH candidates visible in the GUI. It does **not**:

- Create buy/sell recommendations
- Update the official watchlist
- Mutate portfolio state
- Trigger any trade

### Allowed Review Decisions

| Decision | Meaning |
|---|---|
| `approve_for_research_review` | Candidate worth tracking in the research lane |
| `keep_watching` | Continue monitoring; not ready for further review |
| `needs_more_evidence` | Corroboration score too low; wait for more data |
| `reject_candidate` | Not worth further research attention |

**Never produced:** buy, sell, actionable, promoted, validated.

### Approval Artifact

Decisions are written append-only to `outputs/sandbox/discovery/approval_decisions.jsonl`. Every line is a JSON object carrying:

| Field | Value |
|---|---|
| `symbol` | Ticker reviewed |
| `decision` | One of the four allowed values |
| `decision_reason` | Optional operator note |
| `corroboration_score` | Score at time of review |
| `corroboration_level` | Level at time of review |
| `candidate_status` | Sandbox status at time of review |
| `observe_only` | Always `true` |
| `sandbox_only` | Always `true` |
| `no_trade` | Always `true` |
| `no_official_promotion` | Always `true` |

Governance flags (`observe_only`, `sandbox_only`, `no_trade`, `no_official_promotion`) are validated before every write. Any attempt to set them to `False` raises `ValueError`.

Read-side loaders are also defensive: malformed JSONL lines are skipped, and syntactically valid but semantically tampered records are ignored when they contain forbidden decisions or missing/false governance flags.

No `approval_summary.json` artifact is produced. Approval summaries are computed in memory from valid JSONL records.

### Approval Entry Points

```python
from portfolio_automation.discovery.approval_workflow import (
    make_approval_decision,
    record_approval_decision,
    load_approval_decisions,
    build_approval_summary,
    ApprovalDecision,
)

# Create a validated decision
dec = make_approval_decision(
    symbol="NVDA",
    decision=ApprovalDecision.KEEP_WATCHING,
    decision_reason="Strong earnings corroboration, watching one more run.",
    corroboration_score=0.72,
    corroboration_level="strong",
)

# Append to sandbox JSONL
path = record_approval_decision(dec, base_dir="outputs")

# Load all decisions
decisions = load_approval_decisions(base_dir="outputs")

# Compute summary
summary = build_approval_summary(decisions)
print(summary["total_decisions"])      # int
print(summary["sandbox_only"])         # True
print(summary["no_official_promotion"]) # True
```

## Daily Memo Discovery Section (Sandbox Only)

Implemented in `watchlist_scanner/daily_memo.py`.

The daily memo now includes a **DISCOVERY RESEARCH [Sandbox Only]** section rendered after the System/Data Health section (if discovery sandbox artifacts exist). This section is:

- **Sandbox-read-only**: reads sandbox artifacts only; never writes to sandbox or produces separate discovery artifacts
- **Non-blocking**: if discovery artifacts are missing or malformed, the memo generates normally with the section absent
- **Sandbox-only**: includes a mandatory disclaimer stating candidates are not buy/sell recommendations and do not update the official watchlist or portfolio

### Section Content

| Subsection | Description |
|---|---|
| Disclaimer | Sandbox-only warning on every render |
| Summary counts | WATCH / DISCOVERED / REJECTED counts and approval decision totals |
| Top WATCH candidates | Up to 5 candidates with score, corroboration, event type, evidence snippet, latest approval decision |
| Monitoring | DISCOVERED candidates listed compactly |
| Persistence | Candidates seen across multiple runs vs. new this run (from `discovery_memory.json`) |
| Recent research decisions | Last 5 operator approval decisions with symbol, decision, reason, timestamp |
| Rejected / Risk summary | Count, risk-flag count, top rejection reasons |

### Safety Constraints

- All approval records are validated via `is_valid_loaded_approval_record()` before rendering
- Records with `decision` = buy/sell/actionable/promoted/validated are silently excluded (defense-in-depth)
- Records with any governance flag set to `False` are silently excluded
- Section never writes to sandbox, never mutates official state, never calls AI/LLM, never calls external APIs

### Artifact Inputs (Read-Only)

| Artifact | Read from |
|---|---|
| `emerging_candidates.json` | `outputs/sandbox/discovery/emerging_candidates.json` |
| `rejected_candidates.json` | `outputs/sandbox/discovery/rejected_candidates.json` |
| `discovery_memory.json` | `outputs/sandbox/discovery/discovery_memory.json` |
| `approval_decisions.jsonl` | `outputs/sandbox/discovery/approval_decisions.jsonl` |

All four are optional — the section gracefully degrades if any are missing, corrupt, or empty.

## Discovery Replay (Sandbox Backtest Evaluation)

Implemented in `portfolio_automation/discovery/discovery_replay.py`.

Evaluates whether sandbox discovery candidates have predictive value over time using injected price/outcome data. No external API calls are made — price data must be supplied by the caller.

**SANDBOX-ONLY**: All outputs go to `outputs/sandbox/discovery/`. No official watchlist, portfolio, or recommendation mutations occur.

### Public Functions

| Function | Description |
|---|---|
| `run_discovery_replay(...)` | Full orchestration pipeline |
| `load_discovery_replay_inputs(...)` | Load and validate sandbox artifacts |
| `evaluate_discovery_candidate_outcomes(...)` | Compute outcome metrics from injected price data |
| `summarize_discovery_replay_results(...)` | Aggregate metrics by status, corroboration, approval decision, risk |
| `write_discovery_replay_report(...)` | Write sandbox artifacts (DISCOVERY or BACKTEST mode only) |

### Output Artifacts

| Artifact | Path | Description |
|---|---|---|
| `replay_results.json` | `outputs/sandbox/discovery/replay_results.json` | Summary with aggregates and governance flags |
| `replay_results.md` | `outputs/sandbox/discovery/replay_results.md` | Human-readable markdown report with disclaimer |
| `replay_candidate_outcomes.jsonl` | `outputs/sandbox/discovery/replay_candidate_outcomes.jsonl` | Per-candidate outcome records (overwritten per run) |

All artifacts carry `observe_only=true`, `sandbox_only=true`, `no_trade=true`, `no_official_promotion=true`.

### Metrics Computed

Per candidate: `status`, `corroboration_score`, `corroboration_level`, `risk_flag`, `event_type`, `insufficient_data`, and for each forward window: `forward_return_pct`, `direction_correct`, `max_drawdown_pct`, `max_runup_pct`.

Aggregates: `status_comparison` (WATCH vs DISCOVERED vs REJECTED), `corroboration_comparison` (high vs low), `approval_decision_comparison` (by decision type), `risk_comparison` (risk-flagged vs non-risk), `rejected_candidate_review`.

### Safety Constraints

- Never produces BUY/SELL/ACTIONABLE/PROMOTED/VALIDATED statuses
- Candidates with forbidden statuses are silently skipped
- Run mode governance enforced: only DISCOVERY and BACKTEST may write
- All approval decisions validated via `is_valid_loaded_approval_record()` before use
- Never writes to `outputs/latest/`, `outputs/policy/`, or `outputs/portfolio/`

### Injected Price Data Format

```python
price_outcomes = {
    "NVDA": {
        "window_1":  {"forward_return_pct": 2.5, "direction_correct": True,
                      "max_drawdown_pct": -0.5, "max_runup_pct": 3.0},
        "window_3":  {...},
        "window_5":  {...},
        "window_10": {...},
        "window_20": {...},
    }
}
```

Pass `{}` or `None` when no price data is available — all candidates are marked `insufficient_data=True`.

## Future Path

| Step | Description |
|---|---|
| ~~Corroboration~~ | ~~Cross-validate discovery candidates across multiple independent sources before promoting to WATCH~~ — **complete** |
| ~~GUI approval workflow~~ | ~~Operator reviews WATCH candidates and records sandbox research decisions~~ — **complete** |
| ~~Historical backtest for discovery~~ | ~~Sandbox replay to evaluate candidate quality over time~~ — **complete** |
| Manual promotion proposal | `MANUAL_UPDATE` + `approved=True` required for any official action (future phase) |
| ~~Daily Memo discovery section~~ | ~~Add a sandbox-only research summary to the daily memo (read-only)~~ — **complete** |

## Known v1 Limitation

`QQQ` is treated as a noise token by default in `news_ticker_discovery.py`.
Adding `QQQ` to `known_universe` does not override that filter because noise filtering runs first.

This is acceptable for conservative v1 behavior, but it should be revisited if ETF discovery
becomes an explicit goal in a future enhancement.
