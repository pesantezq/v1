# Repo Overview: v1

> Generated 2026-04-13T18:54:27Z by `tools/repo_overview.py`

---

## 1. High-Level Purpose

**Summary:** Portfolio automation and decision-support system. Rules-based rebalancing, scoring (0-100), AI-assisted narrative generation, and watchlist scanning. Analysis-only — no broker API, no automated trades.

**Runtime:** CLI (Python 3.12), Windows Task Scheduler, optional Streamlit GUI

**Action-taking:** No — Produces recommendations and emails only. No trades are placed. Emails require explicit SMTP config.

**Major workflows:**
- Daily: fetch prices → score → guardrails → recommendations → ML → email (if ACTION_REQUIRED)
- Weekly: same as daily + always send digest
- Monthly: same + contribution plan + Claude AI memo + CAGR projections
- Watchlist scan: standalone Alpha Vantage scan with fundamental/technical/theme scoring
- Theme engine: RSS ingestion → Ollama theme detection → candidate boosts

## 2. Entry Points & Execution Flow

- **[agent/__main__.py](agent/__main__.py)** (package `__main__`) — run modes: `n/a`
- **[agent/agent_runner.py](agent/agent_runner.py)** (script) — run modes: `n/a`
- **[agent/mcp_agent_tools.py](agent/mcp_agent_tools.py)** (script) — run modes: `n/a`
- **[main.py](main.py)** (script) — run modes: `daily, weekly, monthly`
- **[stockbot_mcp_server.py](stockbot_mcp_server.py)** (script) — run modes: `daily`
- **[test_demo.py](test_demo.py)** (script) — run modes: `n/a`
- **[tests/test_agent_bundle_builder.py](tests/test_agent_bundle_builder.py)** (script) — run modes: `n/a`
- **[tests/test_agent_runner_offline_mode.py](tests/test_agent_runner_offline_mode.py)** (script) — run modes: `n/a`
- **[tests/test_growth_mode.py](tests/test_growth_mode.py)** (script) — run modes: `n/a`
- **[tests/test_guardrails.py](tests/test_guardrails.py)** (script) — run modes: `n/a`
- **[tests/test_run_mode.py](tests/test_run_mode.py)** (script) — run modes: `daily`
- **[tests/test_scanner.py](tests/test_scanner.py)** (script) — run modes: `n/a`
- **[tests/test_sleeve.py](tests/test_sleeve.py)** (script) — run modes: `n/a`
- **[tests/test_state_store.py](tests/test_state_store.py)** (script) — run modes: `n/a`
- **[tests/test_theme_engine.py](tests/test_theme_engine.py)** (script) — run modes: `n/a`
- **[theme_engine/__main__.py](theme_engine/__main__.py)** (package `__main__`) — run modes: `daily, weekly, monthly`
- **[tools/build_prompt.py](tools/build_prompt.py)** (script) — run modes: `n/a`
- **[tools/daily_packet.py](tools/daily_packet.py)** (script) — run modes: `n/a`
- **[tools/repo_overview.py](tools/repo_overview.py)** (script) — run modes: `n/a`
- **[tools/review_packet.py](tools/review_packet.py)** (script) — run modes: `n/a`
- **[tools/task_ranker.py](tools/task_ranker.py)** (script) — run modes: `n/a`
- **[watchlist_scanner/__main__.py](watchlist_scanner/__main__.py)** (package `__main__`) — run modes: `n/a`

**Main execution flow** (inferred from `main.py`):

1. Parse args → load `.env` → load `config.json` → acquire run lock
2. Idempotency check (SQLite `run_history`)
3. Fetch market prices (Alpha Vantage)
4. Run guardrail checks
5. Score holdings (0-100)
6. Generate adjustments & recommendations
7. Run ML advisor
8. *Monthly only:* contribution engine + CAGR projections + scanner + theme boosts
9. Write output files (CSV, Excel, markdown memos)
10. Send email digest (if conditions met)
11. Update SQLite state (snapshots, peaks, email history)
12. Release run lock

## 3. Important Files & Modules

| File | Tags | Purpose |
|------|------|---------|
| [agent/__main__.py](agent/__main__.py) | `orchestration` `agent` | agent/__main__.py — Package entry point. |
| [theme_engine/__main__.py](theme_engine/__main__.py) | `entry_point` | Theme Engine CLI. |
| [watchlist_scanner/__main__.py](watchlist_scanner/__main__.py) | `entry_point` | Watchlist Scanner CLI. |
| [main.py](main.py) | `orchestration` `entry_point` | Portfolio Automation System - Main Entry Point |
| [tests/test_agent_runner_offline_mode.py](tests/test_agent_runner_offline_mode.py) | `orchestration` `agent` | tests/test_agent_runner_offline_mode.py |
| [agent/agent_runner.py](agent/agent_runner.py) | `agent` `orchestration` | agent/agent_runner.py — AI Agent runner for the portfolio automation system. |
| [agent/io_utils.py](agent/io_utils.py) | `agent` `utility` | agent/io_utils.py — Safe I/O helpers for the AI agent layer. |
| [email_digest.py](email_digest.py) | `output` `integration` | Finance Email Digest Module |
| [email_reporter.py](email_reporter.py) | `output` `integration` | Email reporting module. |
| [theme_engine/theme_store.py](theme_engine/theme_store.py) | `theme` `state` | Theme Store — persists theme signals to SQLite and writes JSON output files. |
| [watchlist_scanner/alpha_vantage_client.py](watchlist_scanner/alpha_vantage_client.py) | `integration` | Alpha Vantage client for the watchlist scanner. |
| [watchlist_scanner/cache_manager.py](watchlist_scanner/cache_manager.py) | `state` | Cache manager for the watchlist scanner. |
| [watchlist_scanner/config.py](watchlist_scanner/config.py) | `scanner` `config` | Watchlist Scanner — static defaults and constants. |
| [watchlist_scanner/theme_engine.py](watchlist_scanner/theme_engine.py) | `scanner` `theme` | Keyword-based theme classifier for financial news headlines. |
| [tests/test_agent_bundle_builder.py](tests/test_agent_bundle_builder.py) | `agent` | tests/test_agent_bundle_builder.py |
| [tests/test_guardrails.py](tests/test_guardrails.py) | `core_logic` | Unit tests for guardrails.py pre-flight structural checks. |
| [tests/test_scanner.py](tests/test_scanner.py) | `scanner` | Unit tests for scanner/candidate_scanner.py. |
| [tests/test_sleeve.py](tests/test_sleeve.py) | `scanner` | Unit tests for sleeve/spec_sleeve_allocator.py. |
| [tests/test_state_store.py](tests/test_state_store.py) | `state` | Unit tests for PortfolioStateStore (state_store.py). |
| [tests/test_theme_engine.py](tests/test_theme_engine.py) | `theme` | Offline tests for the theme engine. |
| [adjustment.py](adjustment.py) | `core_logic` | Portfolio Adjustment Module |
| [agent/__init__.py](agent/__init__.py) | `agent` | — |
| [agent/bundle_builder.py](agent/bundle_builder.py) | `agent` `output` | agent/bundle_builder.py — Build agent_bundle.json from existing engine outputs. |
| [agent/llm_adapters.py](agent/llm_adapters.py) | `agent` `integration` | agent/llm_adapters.py — LLM abstraction layer for the AI agent. |
| [agent/mcp_agent_tools.py](agent/mcp_agent_tools.py) | `integration` | StockBot — AI Agent MCP Testing Tools |
| [agent/prompts.py](agent/prompts.py) | `agent` | agent/prompts.py — LLM prompt templates for the AI agent layer. |
| [agent/repo_tree.py](agent/repo_tree.py) | `utility` | agent/repo_tree.py — Compact repo tree for maintainer prompts. |
| [api_budget.py](api_budget.py) | `utility` | Shared Alpha Vantage daily call budget. |
| [contribution_engine.py](contribution_engine.py) | `core_logic` | Contribution Optimization Engine |
| [digest_builder.py](digest_builder.py) | `output` | Digest Builder Module |

## 4. Module Relationships

- **`main.py`** → `utils.py`, `market_data.py`, `portfolio.py`, `recommendations.py`, `file_output.py`, `email_reporter.py`, `scoring.py`, `finance_analyzer.py`
- **`watchlist_scanner/__main__.py`** → `watchlist_scanner/fundamentals_engine.py`, `watchlist_scanner/cache_manager.py`, `watchlist_scanner/alpha_vantage_client.py`, `watchlist_scanner/scanner.py`, `watchlist_scanner/config.py`, `api_budget.py`, `watchlist_scanner/extended_watchlist.py`
- **`theme_engine/__main__.py`** → `theme_engine/rss_collector.py`, `theme_engine/theme_detector.py`, `theme_engine/theme_mapper.py`, `theme_engine/theme_store.py`, `utils.py`
- **`agent/agent_runner.py`** → `agent/bundle_builder.py`, `agent/io_utils.py`, `agent/llm_adapters.py`, `agent/prompts.py`, `agent/repo_tree.py`
- **`tests/test_theme_engine.py`** → `theme_engine/rss_collector.py`, `theme_engine/theme_detector.py`, `theme_engine/theme_mapper.py`, `theme_engine/theme_store.py`, `scanner/candidate_scanner.py`
- **`watchlist_scanner/scanner.py`** → `watchlist_scanner/alpha_vantage_client.py`, `watchlist_scanner/cache_manager.py`, `watchlist_scanner/fundamentals_engine.py`, `watchlist_scanner/confidence.py`, `watchlist_scanner/config.py`
- **`finance_analyzer.py`** → `utils.py`, `portfolio.py`, `scoring.py`, `email_digest.py`
- **`test_demo.py`** → `utils.py`, `portfolio.py`, `recommendations.py`, `file_output.py`
- **`tests/test_growth_mode.py`** → `drawdown.py`, `contribution_engine.py`, `projections.py`, `adjustment.py`
- **`tests/test_run_mode.py`** → `contribution_engine.py`, `drawdown.py`, `scoring.py`, `adjustment.py`
- **`email_digest.py`** → `utils.py`, `scoring.py`, `digest_builder.py`
- **`email_reporter.py`** → `utils.py`, `portfolio.py`, `recommendations.py`
- **`watchlist_scanner/alpha_vantage_client.py`** → `watchlist_scanner/cache_manager.py`, `watchlist_scanner/config.py`, `api_budget.py`
- **`file_output.py`** → `utils.py`, `portfolio.py`, `recommendations.py`
- **`digest_builder.py`** → `projections.py`, `utils.py`

## 5. Data Models

- **`CashAnalysis`** (dataclass) in `adjustment.py` — fields: `available_cash`, `cash_reserve_target`, `cash_excess`, `monthly_contribution`, `months_to_fix_via_contributions`
- **`TaxContext`** (dataclass) in `adjustment.py` — fields: `is_taxable_account`, `holding_period_days`, `is_long_term`, `unrealized_gain`, `cost_basis`
- **`PortfolioAdjustment`** (dataclass) in `adjustment.py` — fields: `rec_key`, `recommendation_type`, `adjustment_mode`, `action_level`, `symbol`, `final_score`, `title`, `what`
- **`ContributionAllocation`** (dataclass) in `contribution_engine.py` — fields: `symbol`, `asset_class`, `current_weight`, `target_weight`, `drift`, `recommended_dollars`, `reason`
- **`DigestContext`** (dataclass) in `digest_builder.py` — fields: `total_value`, `cash_available`, `max_drift`, `drawdown_pct`, `drawdown_regime`, `monthly_contribution`, `expected_cagr`, `prior_snapshot`
- **`DrawdownState`** (dataclass) in `drawdown.py` — fields: `all_time_high`, `rolling_12m_high`, `rolling_12m_high_date`, `last_update_date`, `current_value`
- **`FinanceSnapshot`** (dataclass) in `finance_analyzer.py` — fields: `date`, `portfolio_value`, `cash_available`, `emergency_fund_months`, `savings_rate`, `max_drift`, `max_drift_symbol`, `drifts_by_symbol`
- **`FinanceConfig`** (dataclass) in `finance_analyzer.py` — fields: `target_savings_rate`, `target_emergency_months`, `drift_band`, `monthly_income`, `monthly_expenses`, `priority_savings`, `priority_emergency`, `priority_drift`
- **`GuardrailViolation`** (dataclass) in `guardrails.py` — fields: `symbol`, `violation_type`, `current_pct`, `cap_pct`, `required_action`
- **`GuardrailResult`** (dataclass) in `guardrails.py` — fields: `status`, `violations`, `summary`
- **`PriceCache`** (dataclass) in `market_data.py` — fields: `cache_dir`, `ttl_seconds`, `_cache`
- **`PersistenceEstimate`** (dataclass) in `ml_advisor.py` — fields: `probability`, `confidence`, `expected_periods`, `sample_size`, `explanation`
- **`ActionEffectivenessEstimate`** (dataclass) in `ml_advisor.py` — fields: `action_benefit_probability`, `expected_time_with_action`, `expected_time_without`, `confidence`, `sample_size`, `explanation`
- **`AlertFatigueEstimate`** (dataclass) in `ml_advisor.py` — fields: `false_alert_probability`, `repeat_alert_count`, `should_suppress`, `confidence`, `explanation`
- **`MLAdvisorOutput`** (dataclass) in `ml_advisor.py` — fields: `rec_key`, `symbol`, `persistence`, `effectiveness`, `alert_fatigue`, `original_score`, `adjusted_score`, `score_adjustment_reason`
- **`RecommendationRecord`** (dataclass) in `ml_history.py` — fields: `record_id`, `rec_key`, `symbol`, `created_date`, `resolved_date`, `metric_type`, `drift_percent`, `absolute_deviation`
- **`PortfolioSummary`** (dataclass) in `portfolio.py` — fields: `total_holdings_value`, `cash_value`, `total_portfolio_value`, `retirement_401k_value`, `total_net_worth`, `cash_weight`, `max_drift`, `max_drift_symbol`
- **`HoldingAnalysis`** (dataclass) in `portfolio.py` — fields: `symbol`, `shares`, `current_price`, `market_value`, `target_weight`, `actual_weight`, `drift`, `drift_direction`
- **`CompoundingDashboard`** (dataclass) in `projections.py` — fields: `current_portfolio_value`, `drawdown_pct`, `expected_cagr`, `monthly_contribution`, `projected_value_10yr`, `projected_value_10yr_no_contrib`, `projected_value_10yr_extra_200`, `extra_200_impact`
- **`Recommendation`** (dataclass) in `recommendations.py` — fields: `action_type`, `symbol`, `shares`, `amount`, `reason`, `priority`, `is_urgent`
- **`RecommendationReport`** (dataclass) in `recommendations.py` — fields: `recommendations`, `summary_message`, `has_actions`, `has_urgent_actions`, `notes`
- **`RetirementHolding`** (dataclass) in `retirement.py` — fields: `symbol`, `name`, `shares`, `price`, `market_value`, `percentage`
- **`RetirementSummary`** (dataclass) in `retirement.py` — fields: `total_balance`, `holdings`, `mode`, `last_updated`
- **`TrendData`** (dataclass) in `scoring.py` — fields: `current_value`, `previous_values`, `periods_below_threshold`, `periods_above_threshold`, `threshold`, `is_increasing_bad`
- **`ScoringComponents`** (dataclass) in `scoring.py` — fields: `severity`, `persistence`, `impact`, `priority`, `confidence`
- **`FinanceRecommendation`** (dataclass) in `scoring.py` — fields: `id`, `impact_area`, `components`, `title`, `trigger`, `what_changed`, `why_it_matters`, `action`
- **`SleeveRecommendation`** (dataclass) in `sleeve/spec_sleeve_allocator.py` — fields: `symbol`, `score`, `sector`, `max_add_dollars`, `is_new_position`, `current_position_dollars`, `reason`
- **`_Holding`** (dataclass) in `tests/test_growth_mode.py` — fields: `symbol`, `shares`, `target_weight`, `asset_class`, `is_leveraged`, `leverage_factor`, `current_price`, `market_value`
- **`_Analysis`** (dataclass) in `tests/test_growth_mode.py` — fields: `symbol`, `drift`, `actual_weight`, `target_weight`, `is_breached`, `drift_direction`
- **`_H`** (dataclass) in `tests/test_growth_mode.py` — fields: `asset_class`, `actual_weight`
- **`Holding`** (dataclass) in `utils.py` — fields: `symbol`, `shares`, `target_weight`, `asset_class`, `is_leveraged`, `leverage_factor`, `current_price`, `market_value`
- **`Retirement401k`** (dataclass) in `utils.py` — fields: `enabled`, `mode`, `balance`, `holdings_csv_path`, `include_in_net_worth`, `holdings`
- **`InvestorProfile`** (dataclass) in `utils.py` — fields: `name`, `age`, `birthdate`, `annual_income`, `monthly_expenses`, `investment_horizon_years`, `risk_tolerance`, `strategy`
- **`RebalanceRules`** (dataclass) in `utils.py` — fields: `band_threshold`, `use_cash_before_selling`, `direct_contributions_first`, `trim_leverage_before_core`, `avoid_taxable_sales`, `panic_sell_protection`
- **`Config`** (dataclass) in `utils.py` — fields: `investor`, `holdings`, `cash_available`, `target_cash_weight`, `rebalance_rules`, `retirement_401k`, `market_data`, `email`

## 6. State & Storage

**SQLite file(s):** data/portfolio.db, portfolio.db, test.db

**Tables:**
- `dataclasses`
- `datetime`
- `enum`
- `typing`
- `utils`
- `portfolio`
- `overweight`
- `agent`
- `pathlib`
- `engine`
- `files`
- `existing`
- `drawdown`
- `prices`
- `first`
- `snapshots`
- `any`
- `exc`
- `config`
- `mcp`
- `this`
- `dotenv`
- `data`
- `sqlite_master`
- `available`
- `the`
- `bundle`
- `listing`
- `collectively`
- `api_budget`
- `__future__`
- `new`
- `most`
- `projections`
- `pre`
- `user`
- `peak`
- `all`
- `12m`
- `email`
- `scoring`
- `digest_builder`
- `recommendations`
- `openpyxl`
- `disk`
- `historical`
- `main`
- `email_digest`
- `fmp_client`
- `live`
- `adjustment`
- `workflow`
- `completed`
- `failed`
- `result`
- `market_data`
- `retirement`
- `file_output`
- `email_reporter`
- `finance_analyzer`
- `ml_history`
- `ml_advisor`
- `contribution_engine`
- `run_lock`
- `state_store`
- `guardrails`
- `ledger`
- `loaded`
- `universe`
- `scanner`
- `sleeve`
- `theme_engine`
- `watchlist_scanner`
- `history`
- `holdings`
- `cache`
- `an`
- `resolution`
- `streak`
- `today`
- `compute_portfolio_cagr`
- `underweight`
- `price`
- `bulk`
- `stored`
- `min_rev_growth`
- `raw`
- `full_scan`
- `baseline`
- `target`
- `spec`
- `run_history`
- `email_history`
- `portfolio_peaks`
- `theme_signals`
- `alert_events`
- `subsystem_health`
- `structural_violations`
- `cash_ledger`
- `extended_watchlist`
- `a`
- `contextlib`
- `pipeline`
- `unittest`
- `tests`
- `sent_at`
- `headlines`
- `approved`
- `mapper`
- `proposed_task`
- `repo`
- `untracked`
- `names`
- `docstring`
- `key`
- `coupling`
- `memory`
- `code`
- `import`
- `scratch`
- `backlog`
- `decimal`
- `log_dir`
- `third`
- `dictionary`
- `technicals`
- `data_quality`
- `overview`
- `_compute_technicals`
- `parse_overview`
- `theme`
- `company`
- `fundamental_context_score`
- `fundamentals_engine`
- `stale`
- `ticker`
- `signal_score`

**JSON state files:**
- `approved_actions.json`
- `data/drawdown_state.json`
- `data/finance_history.json`
- `data/last_success.json`
- `drawdown_state.json`
- `finance_history.json`
- `last_success.json`
- `call_counter.json`
- `data/ml_history.json`
- `data/fmp_cache/top100_watchlist.json`
- `rss_seen.json`
- `data/rss_seen.json`
- `watchlist_signals.json`

## 7. Config Map

**Config files:** .mcp.json, config.json

**Environment variables (from code):**
- `STOCKBOT_TESTING`
- `OLLAMA_MODEL`
- `ANTHROPIC_MODEL`
- `ANTHROPIC_API_KEY`
- `FMP_API_KEY`
- `STOCKBOT_ENABLE_OLLAMA_TEST`
- `ALPHA_VANTAGE_API_KEY`

**`.env` keys:**
- `ALPHA_VANTAGE_API_KEY`
- `EMAIL_PASSWORD`
- `EMAIL_SENDER`
- `EMAIL_RECIPIENT`
- `FMP_API_KEY`
- `DEBUG`
- `ANTHROPIC_API_KEY`
- `OLLAMA_MODEL`

**Feature flags (from config.json):**
- `retirement_401k.enabled = False`
- `ml_advisor.enabled = False`
- `email.enabled = True`
- `speculative_sleeve.enabled = True`
- `scanner.enabled = True`
- `watchlist_scanner.enabled = True`
- `theme_engine.enabled = True`
- `extended_watchlist.enabled = True`

## 8. External Integrations

### Alpha Vantage API (required)
- **Files:** market_data.py, watchlist_scanner/alpha_vantage_client.py
- **Purpose:** Market data (prices, news sentiment, company overview)
- **Auth:** `ALPHA_VANTAGE_API_KEY env var`

### Financial Modeling Prep (FMP) API (optional)
- **Files:** fmp_client.py
- **Purpose:** S&P 500 universe, bulk profiles, metrics
- **Auth:** `FMP_API_KEY env var`

### Ollama (local LLM) (optional)
- **Files:** agent/llm_adapters.py, theme_engine/theme_detector.py
- **Purpose:** Daily/weekly AI narrative generation and theme detection
- **Auth:** `None (local)`

### Anthropic Claude API (optional)
- **Files:** agent/llm_adapters.py
- **Purpose:** Monthly AI memos; maintainer patch generation
- **Auth:** `ANTHROPIC_API_KEY env var`

### SMTP Email (optional)
- **Files:** email_reporter.py, email_digest.py
- **Purpose:** Sends portfolio digests and alerts
- **Auth:** `EMAIL_USER / EMAIL_PASSWORD / SMTP_HOST env vars`

### MCP Server (Claude Code integration) (optional)
- **Files:** stockbot_mcp_server.py, agent/mcp_agent_tools.py
- **Purpose:** Exposes tools to Claude Code IDE sessions
- **Auth:** `None (local)`

## 9. Output / Reporting Paths

| Directory | Contents |
|-----------|----------|
| `outputs/latest/` | Always-overwritten: CSV, Excel, markdown memos |
| `outputs/history/YYYY-MM-DD/` | Daily archive (no duplicates) |
| `logs/YYYY-MM-DD.log` | One log file per day (14-day retention) |
| `data/` | Persistent state: SQLite, JSON caches, run lock |

**Key output files:**
- `outputs/latest/portfolio_snapshot.csv`
- `outputs/latest/recommendations.csv`
- `outputs/latest/contribution_plan.csv`
- `outputs/latest/compounding_dashboard.txt`
- `outputs/latest/decision_memo.md`
- `outputs/latest/monthly_memo.md`
- `outputs/latest/watchlist_summary.md`
- `outputs/latest/candidates_top20.csv`
- `outputs/latest/agent_bundle.json`

## 10. Run Cadence

**Modes detected:** weekly, monthly, daily, batch

| Mode | Trigger | Key behaviors |
|------|---------|---------------|
| `daily` | Weekday mornings | Silent unless ACTION_REQUIRED; idempotent |
| `weekly` | Sundays | Always sends full digest |
| `monthly` | 1st of month | Contribution plan + Claude memo + scanner run |

**Task Scheduler references:** digest_builder.py, main.py, state_store.py

## 11. Known Issues / Technical Debt

- No TODO/FIXME markers found.

## 12. Safe vs Risky Edit Zones

> *Advisory only — inferred from import coupling and module tags.*

### Safer for additive edits
- **[watchlist_scanner/theme_engine.py](watchlist_scanner/theme_engine.py)** — tags: theme; low coupling
- **[agent/__init__.py](agent/__init__.py)** — tags: agent; low coupling
- **[agent/prompts.py](agent/prompts.py)** — tags: agent; low coupling
- **[agent/repo_tree.py](agent/repo_tree.py)** — tags: utility; low coupling
- **[digest_builder.py](digest_builder.py)** — tags: output; low coupling
- **[theme_engine/__init__.py](theme_engine/__init__.py)** — tags: theme; low coupling
- **[tools/build_prompt.py](tools/build_prompt.py)** — tags: agent; low coupling

### Higher risk — inspect carefully before editing
- **[agent/__main__.py](agent/__main__.py)** — tags: orchestration
- **[main.py](main.py)** — tags: orchestration
- **[agent/agent_runner.py](agent/agent_runner.py)** — tags: orchestration
- **[email_digest.py](email_digest.py)** — tags: integration
- **[email_reporter.py](email_reporter.py)** — tags: integration
- **[theme_engine/theme_store.py](theme_engine/theme_store.py)** — tags: state
- **[watchlist_scanner/alpha_vantage_client.py](watchlist_scanner/alpha_vantage_client.py)** — tags: integration
- **[watchlist_scanner/cache_manager.py](watchlist_scanner/cache_manager.py)** — tags: state
- **[watchlist_scanner/config.py](watchlist_scanner/config.py)** — tags: config
- **[adjustment.py](adjustment.py)** — tags: core_logic
- **[agent/llm_adapters.py](agent/llm_adapters.py)** — tags: integration
- **[agent/mcp_agent_tools.py](agent/mcp_agent_tools.py)** — tags: integration
- **[contribution_engine.py](contribution_engine.py)** — tags: core_logic
- **[drawdown.py](drawdown.py)** — tags: core_logic
- **[finance_analyzer.py](finance_analyzer.py)** — tags: core_logic
- **[fmp_client.py](fmp_client.py)** — tags: integration
- **[guardrails.py](guardrails.py)** — tags: core_logic
- **[market_data.py](market_data.py)** — tags: integration
- **[ml_history.py](ml_history.py)** — tags: state
- **[portfolio.py](portfolio.py)** — imported by 6 modules; tags: core_logic
- **[projections.py](projections.py)** — tags: core_logic
- **[recommendations.py](recommendations.py)** — imported by 4 modules; tags: core_logic
- **[scanner/candidate_scanner.py](scanner/candidate_scanner.py)** — tags: core_logic
- **[scoring.py](scoring.py)** — imported by 4 modules; tags: core_logic
- **[sleeve/spec_sleeve_allocator.py](sleeve/spec_sleeve_allocator.py)** — tags: core_logic
- **[state_store.py](state_store.py)** — tags: state
- **[theme_engine/rss_collector.py](theme_engine/rss_collector.py)** — tags: integration
- **[theme_engine/theme_detector.py](theme_engine/theme_detector.py)** — tags: integration
- **[theme_engine/theme_mapper.py](theme_engine/theme_mapper.py)** — tags: core_logic
- **[utils.py](utils.py)** — imported by 14 modules; tags: config
- **[watchlist_scanner/fundamentals_engine.py](watchlist_scanner/fundamentals_engine.py)** — tags: core_logic
- **[watchlist_scanner/scanner.py](watchlist_scanner/scanner.py)** — tags: core_logic

## 13. Tests

**Framework:** unittest
**Test directory:** `tests/`
**Total tests:** ~185

**Smoke test:**
```bash
python -m unittest discover tests/ -v
```

**Test files:**
- `tests/test_agent_bundle_builder.py` (19 tests)
- `tests/test_agent_runner_offline_mode.py` (18 tests)
- `tests/test_growth_mode.py` (30 tests)
- `tests/test_guardrails.py` (9 tests)
- `tests/test_run_mode.py` (18 tests)
- `tests/test_scanner.py` (16 tests)
- `tests/test_sleeve.py` (9 tests)
- `tests/test_state_store.py` (19 tests)
- `tests/test_theme_engine.py` (47 tests)

## 14. Prompt Helper — Where to Look First

Use these pointers when writing future AI-assisted edit prompts.

### Changing email digest content or UX
**Inspect first:** [`email_digest.py`](email_digest.py), [`digest_builder.py`](digest_builder.py), [`email_reporter.py`](email_reporter.py)
**Notes:** Dedup is SHA-256 hash in state_store.py:email_history. Anti-spam gating in email_digest.py.

### Changing state persistence or schema
**Inspect first:** [`state_store.py`](state_store.py), [`guardrails.py`](guardrails.py), [`ml_history.py`](ml_history.py)
**Notes:** SQLite DDL is in state_store.py. Tables: run_history, snapshots, email_history, portfolio_peaks, theme_signals. Any schema change needs migration or a fresh db.

### Changing scoring or rebalancing logic
**Inspect first:** [`scoring.py`](scoring.py), [`adjustment.py`](adjustment.py), [`finance_analyzer.py`](finance_analyzer.py), [`recommendations.py`](recommendations.py)
**Notes:** Scores are 0-100. Growth mode changes scoring weights — check config.json growth_mode.mode. Structural violations in guardrails.py gate actions.

### Changing scanner / API budgeting
**Inspect first:** [`fmp_client.py`](fmp_client.py), [`scanner/candidate_scanner.py`](scanner/candidate_scanner.py), [`watchlist_scanner/alpha_vantage_client.py`](watchlist_scanner/alpha_vantage_client.py), [`api_budget.py`](api_budget.py), [`watchlist_scanner/cache_manager.py`](watchlist_scanner/cache_manager.py)
**Notes:** FMP budget guard: 230 calls/day. AV budget: 20 calls/day. Cache TTLs in watchlist_scanner/config.py. Daily call counter persisted in data/watchlist_cache/call_counter.json.

### Changing scheduler or run cadence
**Inspect first:** [`main.py`](main.py), [`run_lock.py`](run_lock.py), [`state_store.py`](state_store.py)
**Notes:** Run modes: daily|weekly|monthly via --run-mode flag. Idempotency anchor: run_history table (run_id = YYYY-MM-DD_mode). Lock file: data/run.lock (30-min stale threshold). Task Scheduler setup in README.md.

### Changing AI agent narrative / prompts
**Inspect first:** [`agent/prompts.py`](agent/prompts.py), [`agent/agent_runner.py`](agent/agent_runner.py), [`agent/llm_adapters.py`](agent/llm_adapters.py), [`agent/bundle_builder.py`](agent/bundle_builder.py)
**Notes:** LLM routing: daily/weekly → Ollama → Claude fallback. monthly → Claude. Offline stub active when STOCKBOT_TESTING=1. Bundle JSON is in outputs/latest/agent_bundle.json.

### Changing theme engine or RSS collection
**Inspect first:** [`theme_engine/theme_detector.py`](theme_engine/theme_detector.py), [`theme_engine/rss_collector.py`](theme_engine/rss_collector.py), [`theme_engine/theme_mapper.py`](theme_engine/theme_mapper.py), [`data/themes_catalog.json`](data/themes_catalog.json)
**Notes:** Theme detection uses Ollama. testing_mode=True or STOCKBOT_TESTING=1 returns MOCK_THEMES. Theme boosts are applied in scanner/candidate_scanner.py:apply_theme_boosts().

### Changing watchlist scanner behavior
**Inspect first:** [`watchlist_scanner/__main__.py`](watchlist_scanner/__main__.py), [`watchlist_scanner/scanner.py`](watchlist_scanner/scanner.py), [`watchlist_scanner/fundamentals_engine.py`](watchlist_scanner/fundamentals_engine.py)
**Notes:** 3-component score: theme_news×0.45 + technical×0.30 + fundamentals×0.25. Free-tier AV uses TIME_SERIES_DAILY (no adjusted close). ETFs return empty OVERVIEW — handled gracefully.

### Changing output file formats or paths
**Inspect first:** [`file_output.py`](file_output.py), [`agent/io_utils.py`](agent/io_utils.py), [`main.py`](main.py)
**Notes:** outputs/latest/ is always overwritten. outputs/history/YYYY-MM-DD/ is archived once per day. Atomic writes use temp-then-rename pattern in agent/io_utils.py.

### Adding a new config section or feature flag
**Inspect first:** [`utils.py`](utils.py), [`config.json`](config.json)
**Notes:** Config is a dataclass hierarchy in utils.py (Config → sub-configs). Feature flags follow pattern: config.section.enabled (bool). Always add defaults so old configs remain valid.

---
*End of report — 2026-04-13T18:54:27Z*
