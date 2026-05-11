# Market Narratives Layer

## Overview

The Market Narratives layer (`portfolio_automation/market_narratives.py`) turns existing structured artifacts into daily, weekly, and monthly operator-readable market narratives.

Narratives explain:
- What changed (daily)
- What themes persisted (weekly)
- What the big-picture regime context is (monthly)
- How current news and discovery context relates to the portfolio

**This layer is observe-only. It does not create recommendations, change official decision logic, or mutate any official portfolio/watchlist/allocation/scoring state.**

Safety invariants (hardcoded):
- `observe_only: true`
- `no_trade: true`
- `not_recommendation: true`
- No BUY/SELL/HOLD/PROMOTED/VALIDATED/ACTIONABLE language in generated text
- Writes only to `OutputNamespace.LATEST`
- No writes to POLICY, PORTFOLIO, or SANDBOX namespaces
- All input artifacts are read-only
- No LLM/AI calls — deterministic rules only (AI support deferred)

## Module Location

```
portfolio_automation/
  market_narratives.py
```

## Public API

```python
from portfolio_automation.market_narratives import run_market_narratives

result = run_market_narratives(
    base_dir="outputs",
    periods=["daily", "weekly", "monthly"],
    write_files=True,
)
```

### Individual functions

| Function | Purpose |
|---|---|
| `load_all_inputs(base_dir)` | Load all input artifacts safely; degrades on missing/malformed |
| `validate_narrative_safety(text)` | Check text for prohibited instruction patterns |
| `build_market_narrative_report(period, inputs, base_dir)` | Build structured `MarketNarrativeReport` |
| `render_market_narrative_markdown(report)` | Render report as Markdown string |
| `write_market_narrative_report(period, report, base_dir)` | Write JSON + MD to LATEST namespace |
| `run_market_narratives(base_dir, periods, write_files)` | Top-level orchestrator |

## Data Types

| Type | Purpose |
|---|---|
| `NarrativeInputSummary` | Records which input artifacts were available |
| `NarrativeTheme` | A market theme identified across multiple inputs |
| `NarrativeRisk` | A risk flag surfaced from news/discovery evidence |
| `NarrativeCatalyst` | A positive catalyst flag from news/discovery evidence |
| `NarrativeDiscoveryContext` | Sandbox-only discovery research context |
| `MarketNarrativeReport` | Full structured narrative for one period |

## Input Artifacts

All inputs degrade gracefully when missing, malformed, or non-object JSON.

| Artifact | Source | Used for |
|---|---|---|
| `outputs/latest/news_intelligence.json` | News intelligence layer | Themes, risks, catalysts |
| `outputs/latest/decision_plan.json` | Decision engine | Portfolio context |
| `outputs/latest/system_decision_summary.json` | Pipeline summary | System health context |
| `outputs/latest/data_quality_report.json` | Data quality monitor | Data quality notes |
| `outputs/latest/confidence_calibration.json` | Confidence calibration | Calibration notes |
| `outputs/latest/ai_budget_summary.json` | AI budget tracker | Budget context |
| `outputs/latest/decision_explanations.json` | Decision explainer | Decision context |
| `outputs/sandbox/discovery/news_enriched_candidates.json` | Discovery news integration | Discovery research context |
| `outputs/sandbox/discovery/emerging_candidates.json` | Discovery engine | Candidate counts |
| `outputs/sandbox/discovery/rejected_candidates.json` | Discovery engine | Rejected candidate context |
| `outputs/sandbox/discovery/replay_results.json` | Discovery replay | Backtest context |
| `outputs/sandbox/discovery/approval_decisions.jsonl` | Approval workflow | Decision audit context |

## Output Artifacts

All written to `OutputNamespace.LATEST`:

| Artifact | Period | Description |
|---|---|---|
| `outputs/latest/market_narrative_daily.json` | Daily | Structured daily narrative |
| `outputs/latest/market_narrative_daily.md` | Daily | Human-readable daily narrative |
| `outputs/latest/market_narrative_weekly.json` | Weekly | Structured weekly narrative |
| `outputs/latest/market_narrative_weekly.md` | Weekly | Human-readable weekly narrative |
| `outputs/latest/market_narrative_monthly.json` | Monthly | Structured monthly narrative |
| `outputs/latest/market_narrative_monthly.md` | Monthly | Human-readable monthly narrative |

## Narrative Periods

### Daily
Focuses on:
- What changed today
- Top news themes
- Affected holdings/watchlist/discovery candidates
- Major risk/catalyst flags
- Data quality warnings
- What to watch next

### Weekly
Focuses on:
- Persistent themes
- Repeated news catalysts
- Discovery candidates gaining/losing news support
- Risk themes that persisted
- Confidence calibration/replay context
- Operator review queue

### Monthly
Focuses on:
- Big-picture regime/context
- Sector/theme rotation
- Portfolio-level narrative
- Discovery/replay lessons
- System health summary
- Suggested review areas (not recommendations)

## Safety Validator

`validate_narrative_safety(text)` checks for prohibited instruction patterns including:
- `buy now`, `sell now`, `hold now`
- `add shares`, `reduce shares`, `rebalance now`
- `execute trade`, `execute order`
- `recommend buying`, `recommend selling`
- `promote candidate`, `official recommendation`

Returns a list of detected violations (empty = clean). Used internally to guard all generated text.

## Discovery Research Context

Discovery context is always sandbox-only:
- Candidates labeled as: `news-supported research candidate`, `risk-heavy research candidate`, `needs more corroboration`, `news-only signal — needs corroboration`
- No `PROMOTED`, `VALIDATED`, `ACTIONABLE`, `BUY`, `SELL` statuses
- Disclaimer always present: "Discovery research is sandbox-only. No candidates are promoted or recommended."

## AI Support

AI narrative support is **deferred**. All generation is deterministic keyword/rules-based.

When/if AI support is added later:
- Must be disabled by default (`AI_NARRATIVES_ENABLED=0`)
- Must use existing AI budget/cost controls from `ai_budget.py`
- Must degrade safely to deterministic output on any AI failure
- Must not override deterministic safety guards
- Must not generate trading commands or official recommendations

## Tests

File: `tests/test_market_narratives.py`
Count: 79 tests across 9 test classes

Coverage: input loading, safety validator, daily/weekly/monthly narrative, markdown rendering, artifact writing, orchestrator, discovery boundary, namespace compliance, determinism.
