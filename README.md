# Portfolio Automation System

A production-ready, rules-based portfolio tracking and rebalancing automation tool with comprehensive finance recommendation scoring.

## Features

- **Market Data Integration**: Alpha Vantage API with retry logic, caching, and rate limiting
- **Portfolio Tracking**: CSV and Excel output with historical tracking
- **Rules-Based Recommendations**: Buy/sell/hold decisions based on configurable rules
- **Finance Scoring System (0-100)**: Multi-factor scoring across 6 core categories
- **401(k) Integration**: Balance-only or holdings CSV import modes
- **Smart Email Digests**: Only sends when thresholds are met (anti-spam)
- **Fully Configurable**: JSON configuration with .env secrets management

## Quick Start

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Copy `.env.template` to `.env` and add your API keys:
   ```bash
   cp .env.template .env
   # Edit .env with your Alpha Vantage API key and email credentials
   ```

3. Update `config.json` with your portfolio holdings

4. Run the system:
   ```bash
   python main.py
   ```

## GUI Launch

Launch the existing Streamlit GUI from the repo root:

```bash
streamlit run gui/app.py
```

The GUI reuses the original single-file Streamlit structure in `gui/app.py`:
- existing sidebar navigation and router
- existing `Dashboard` entry point
- existing output-oriented pages like `Outputs`, `Run History`, and `Watchlist`

The operator dashboard MVP extends that structure instead of creating a parallel app.

### Operator Dashboard Modes

The `Dashboard` page now has two modes:

- `Overview`: a fast operator summary for latest run state, degraded mode, regime, recommendation, warnings, memo excerpt, and top signal rows
- `Advanced`: tabbed operator panels for run status, memo review, signal triage, portfolio construction, strategy recommendation, and health/reliability

### Optional Artifacts

The dashboard reads existing advisory artifacts and degrades gracefully when some are missing.

Primary artifacts:
- `outputs/latest/scraped_intel_run_summary.json`
- `outputs/latest/agent_bundle.json`
- `outputs/latest/agent_llm_metadata.json`
- `outputs/latest/theme_engine_llm_metadata.json`
- `outputs/latest/watchlist_signals.json`
- `outputs/portfolio/portfolio_snapshot.json`
- `outputs/policy/policy_recommendation.json`

Optional artifacts:
- `outputs/policy/recommendation_evaluation.json`
- `outputs/policy/recommendation_outcomes.json`
- latest memo markdown files such as `outputs/latest/monthly_memo.md` or `outputs/latest/decision_memo.md`

If an optional artifact is missing, the GUI shows a non-fatal status message instead of failing.

## Finance Scoring System (0-100)

### Score Components

Each recommendation is scored on four factors:

| Component | Range | Description |
|-----------|-------|-------------|
| **Severity** | 0-40 | How far from target? |
| **Persistence** | 0-25 | Is it getting worse? (streak, trend) |
| **Impact** | 0-25 | What's the downside? |
| **Priority** | 0-10 | User preference (1-5 → 2-10) |

Final score = (Severity + Persistence + Impact + Priority) × (Confidence / 100)

### Action Levels

| Score | Level | Email Behavior |
|-------|-------|----------------|
| 0-24 | FYI | Don't email |
| 25-49 | Monitor | Digest only |
| 50-74 | Recommended | Send email |
| 75-100 | Action Required | Send + highlight |

### Core Finance Categories

1. **Cash Safety** - Emergency fund, cash buffer
2. **Cashflow** - Savings rate, budget variance
3. **Debt** - APR, utilization, payoff timeline
4. **Portfolio Risk** - Drift, concentration, leverage
5. **Taxes** - Tax-inefficient moves, short-term gains
6. **Fraud/Security** - Unexpected transactions

### Recommendation Format

Each recommendation includes:
- **Title**: 10 words max
- **Trigger**: Exact metric + threshold breach
- **What Changed**: Current vs target + delta
- **Why It Matters**: Tie to risk/goal/taxes
- **Action**: Specific action with $ or %
- **Next Check**: Date or "next paycheck"
- **Evidence**: 1-line trend summary

### Email Anti-Spam Rules

- No repeats within 7 days unless score increases 15+
- Related issues deduplicated into root cause
- Max 8 items per email
- Send only if: ≥1 Action Required OR ≥2 Recommended OR digest day

## Configuration

### config.json

The configuration file contains:
- **investor**: Profile information (age, income, risk tolerance)
- **portfolio**: Holdings list with symbols, shares, and target weights
- **rebalance_rules**: Drift thresholds and rebalancing preferences
- **finance_analysis**: Scoring targets and priorities
- **retirement_401k**: 401(k) integration settings
- **market_data**: API configuration (cache TTL, retries)
- **email**: SMTP settings for email reports
- **schedule**: Weekly/annual report schedules
- **output**: File paths for CSV and Excel output

### Environment Variables

Required in `.env`:
- `ALPHA_VANTAGE_API_KEY`: Your Alpha Vantage API key
- `EMAIL_PASSWORD`: Gmail App Password (if email enabled)

## Command Line Options

```
python main.py [OPTIONS]

Options:
  --config, -c PATH    Configuration file path (default: config.json)
  --env, -e PATH       Environment file path (default: .env)
  --debug, -d          Enable debug logging
  --dry-run, -n        Run without side effects
  --force-email        Force email send
  --skip-email         Skip email send
```

## Project Structure

```
portfolio_automation/
├── main.py              # Entry point
├── utils.py             # Configuration and utilities
├── market_data.py       # Alpha Vantage API client
├── portfolio.py         # Portfolio calculations
├── recommendations.py   # Rules-based recommendation engine
├── scoring.py           # Finance scoring system (0-100)
├── finance_analyzer.py  # Integrates scoring with portfolio
├── adjustment.py        # Consolidated portfolio adjustments
├── ml_history.py        # ML training data collection
├── ml_advisor.py        # Pattern recognition advisor
├── email_digest.py      # Smart email digests
├── retirement.py        # 401(k) integration
├── file_output.py       # CSV and Excel generation
├── email_reporter.py    # Legacy email reporting
├── config.json          # Configuration (edit this)
├── .env.template        # Environment template
├── requirements.txt     # Python dependencies
├── test_demo.py         # Demo with mock data
├── data/                # Cache and history files
│   ├── price_cache.json
│   ├── finance_history.json
│   └── ml_history.json
└── output/              # Generated output files
    ├── portfolio_snapshot.csv
    ├── portfolio_tracker.xlsx
    ├── recommendations.csv
    ├── scored_recommendations.csv
    ├── email_view.csv
    ├── ml_advisor_outputs.csv
    └── ml_training_data.csv
```

## Output Files

| File | Description |
|------|-------------|
| `portfolio_snapshot.csv` | Current holdings with prices and drift |
| `portfolio_tracker.xlsx` | Excel workbook with Holdings, Summary, History sheets |
| `recommendations.csv` | Basic buy/sell/hold recommendations |
| `scored_recommendations.csv` | Full scored recommendations with all components |
| `email_view.csv` | Consolidated EmailView for Claude email generation |
| `ml_advisor_outputs.csv` | ML advisor probability estimates |
| `ml_training_data.csv` | Historical records for ML training |
| `email_prompt.txt` | Claude prompt for generating finance emails |
| `ml_analysis_prompt.txt` | Claude prompt for historical pattern analysis |

## Consolidated Portfolio Adjustments

The system normalizes BUY and REBALANCE_ALERT into unified Portfolio Adjustments:

### Adjustment Modes

| Mode | Description |
|------|-------------|
| `CONTRIBUTE_ONLY` | Use future contributions (no selling) |
| `USE_CASH_EXCESS` | Buy with available cash above reserve |
| `TRIM_LEVERAGE_FIRST` | Reduce leveraged positions first |
| `SELL_TO_REBALANCE` | Sell overweight assets (last resort) |

### Decision Priority

1. **Never sell if contributions can fix it** - Prefer directing contributions
2. **Use cash excess before selling** - Only excess above reserve target
3. **Trim leverage before core holdings** - Leveraged positions are riskier
4. **Only sell when necessary** - Band breached AND other options exhausted
5. **Tax-aware selling** - Prefer long-term lots, highest cost basis

### Cash Reserve Logic

The system calculates a reserve target and only recommends using cash above that:

```
Cash Reserve Target = MAX(portfolio × target_cash_pct, monthly_expenses × emergency_months)
Cash Excess = MAX(0, available_cash - reserve_target)
```

For example, with $2,000 cash, $3,000 monthly expenses, and 3-month emergency target:
- Reserve target = MAX($1,000, $9,000) = $9,000
- Cash excess = MAX(0, $2,000 - $9,000) = $0
- Mode: CONTRIBUTE_ONLY (no cash to invest)

With $12,000 cash:
- Cash excess = $12,000 - $9,000 = $3,000
- Mode: USE_CASH_EXCESS (buy with surplus)

## ML Learning System

The system includes a learning-assisted decision support module that improves over time without predicting prices.

### Core Principle

> **Rules decide, ML advises.** The system learns patterns in recommendation behavior and outcomes to improve decision quality.

### What ML Learns (Labels)

| Label | Description |
|-------|-------------|
| **Y1: Persistence** | Did condition persist >= N periods? |
| **Y2: Action Effectiveness** | Did action reduce deviation faster than baseline? |
| **Y3: Resolution Time** | Periods until resolution |
| **Y4: Alert Fatigue** | Did alert resolve without intervention? |

### ML Outputs

For each recommendation, the ML advisor provides:

- **Persistence Probability**: Likelihood condition will persist
- **Expected Resolution Time**: Periods until resolution
- **Action Benefit Probability**: Whether action improves outcomes
- **False Alert Probability**: Risk of unnecessary alert
- **Score Adjustment**: Minor confidence adjustments (-10 to +8)
- **ML Recommendation**: "Act Now", "Wait", "Monitor", or "Suppress"

### Data Collection

Historical records are automatically saved to `data/ml_history.json` with:
- Metric state at creation (drift, deviation, trend)
- Capital context (cash, contributions)
- Market context (volatility regime)
- Outcome labels (resolved naturally vs action taken)

### Training Requirements

- **Minimum for estimates**: 10 resolved records
- **High confidence**: 30+ resolved records
- **Recommended models**: XGBoost, Random Forest, Logistic Regression

### Claude Analysis Prompt

The system exports `ml_analysis_prompt.txt` for using Claude to analyze historical patterns:

```
You are a financial decision-support analyst...
[See output/ml_analysis_prompt.txt]
```

### What ML Does NOT Do

- ❌ Predict market prices
- ❌ Make buy/sell decisions
- ❌ Override rule-based thresholds
- ❌ Replace human judgment

## Default Target Allocation

The default configuration uses the following allocation:
- QQQ: 45% (US Large Cap Tech)
- VFH: 15% (Financial Sector)
- VXUS: 10% (International Equity)
- GLD: 20% (Gold)
- QLD: 5% (2× Leveraged Nasdaq)
- Cash: 5%

## Scheduled Execution

For automated scheduled runs, use cron (Linux/Mac) or Task Scheduler (Windows):

```cron
# Weekly update every Sunday at 9am
0 9 * * 0 cd /path/to/portfolio_automation && python main.py
```

---

## Aggressive Wealth Growth Mode

> **Goal**: Maximize long-term wealth accumulation over a 1–10+ year horizon by
> directing every dollar of new contribution to its highest-impact target,
> expanding drift tolerance, and eliminating panic-driven selling.

Enable by setting `growth_mode.mode` to `"accumulation_aggressive"` in `config.json`.

### How to Run Monthly

```bash
python main.py --config config.json

# Key outputs in growth mode:
#   output/contribution_plan.csv       — where to deploy this month's contribution
#   output/compounding_dashboard.txt   — 10-year projections and milestones
#   output/email_view.csv              — structural violation alerts only
```

### Behavioral Changes vs Default Mode

| Behavior | Default | Aggressive Growth Mode |
|----------|---------|------------------------|
| Drift band | ±7% | ±12% (wider; drift is informational) |
| Primary output | Rebalance alerts | **Contribution Optimization Plan** |
| Selling | When band breached | **Disabled** except structural violations |
| Email alerts | Drift-based | Structural violations only |
| Anti-panic gating | Off | **On** — suppresses sells when drawdown >20% |
| Drawdown awareness | None | Equity tilt in contribution plan |

### Contribution Optimization Engine

Each run computes `output/contribution_plan.csv`:

| Column | Description |
|--------|-------------|
| `Symbol` | Holding ticker |
| `CurrentWeight` | Actual portfolio weight |
| `TargetWeight` | Config target weight |
| `Drift` | Underweight amount (negative = below target) |
| `RecommendedContributionDollars` | How much to invest this month |
| `Reason` | Explanation incl. drawdown tilt if active |

**Allocation rules:**
1. Allocate 100% of contribution to the most underweight *core* (non-leveraged) holding.
2. Split across multiple holdings only when a concentration cap would be exceeded.
3. Leveraged holdings are never targets for new contributions.
4. During drawdowns, equity-class assets get priority.

### Drawdown Regimes

The system tracks a 12-month rolling high and classifies the current regime:

| Regime | Drawdown | Contribution Behavior |
|--------|----------|-----------------------|
| `normal` | < 10% | Allocate by underweight magnitude |
| `modest_dip` | 10–20% | Tilt contributions toward equity holdings |
| `significant_dip` | 20–30% | Aggressive equity tilt; suppress all non-structural sells |
| `severe_dip` | > 30% | Deploy all available cash to equity; maximum tilt |

State is persisted in `data/drawdown_state.json` across runs.

### Structural Violations (Only Cases for Selling)

| Violation | Trigger | Action |
|-----------|---------|--------|
| **Concentration cap** | Any holding > 40% of portfolio | Trim to <40% (long-term lots first) |
| **Leverage cap** | Total leveraged exposure > 15% | Trim — bypasses anti-panic gating |

These appear in `email_view.csv` as `ACTION_REQUIRED` alerts.

### Compounding Dashboard

Written to `output/compounding_dashboard.txt` each run with:
- Current portfolio value and drawdown %
- Expected CAGR (weighted by asset class, config-driven)
- 10-year projected value (with and without contributions)
- Impact of +$200/month scenario
- Milestone estimates: time to $100k, $250k, $500k, $1M

> Expected returns are **config-driven assumptions**, not market predictions.

### Growth Mode Configuration

```json
"growth_mode": {
  "mode": "accumulation_aggressive",
  "concentration_cap": 0.40,
  "leverage_cap": 0.15,
  "target_cagr": 0.09,
  "drawdown_thresholds": {
    "modest_equity_tilt": 0.10,
    "aggressive_equity_tilt": 0.20,
    "deploy_all_cash": 0.30
  },
  "expected_returns": {
    "us_equity": 0.10,
    "us_equity_sector": 0.09,
    "international_equity": 0.08,
    "commodity": 0.04,
    "us_equity_leveraged": 0.14,
    "bonds": 0.04,
    "cash": 0.04
  }
}
```

Set `mode` to anything other than `"accumulation_aggressive"` to disable growth mode
and revert to default behaviour.

### New Output Files (Growth Mode)

| File | Description |
|------|-------------|
| `output/contribution_plan.csv` | Where to deploy this month's contribution |
| `output/compounding_dashboard.txt` | 10-year projections and milestone estimates |
| `data/drawdown_state.json` | Persistent peak and drawdown state |

### Running Unit Tests

```bash
python -m unittest tests.test_growth_mode -v
python -m unittest tests.test_run_mode -v
```

---

## Running Locally with Windows Task Scheduler

The system supports three run modes designed for automated scheduling:

| Mode | When to schedule | Email behaviour |
|------|-----------------|-----------------|
| `daily` | Every weekday morning | Silent unless ACTION\_REQUIRED items exist |
| `weekly` | Every Sunday | Always sends full portfolio digest |
| `monthly` | 1st of each month | Always sends Capital Deployment Memo |

### Run-Mode Overview

```
--run-mode daily    Fetches prices, updates state, emails ONLY if structural
                    violations or ACTION_REQUIRED items are found. Otherwise
                    completely silent — no noise on normal days.

--run-mode weekly   Always sends the full Finance Digest (portfolio value,
                    drift, top recommendations). Capped at 8 items.

--run-mode monthly  Sends the Capital Deployment Memo:
                      • Portfolio value & drawdown %
                      • Expected weighted CAGR
                      • 10-year projection (with & without contributions)
                      • Impact of +$200/month scenario
                      • Time to $100k / $250k / $500k / $1M
                      • This month's contribution plan
```

### Output Directories

Every run writes to two locations:

```
outputs/
  latest/                   ← Always overwritten with most-recent run
  history/
    2026-03-01/             ← Archived once per day (never duplicated)
    2026-03-02/
    ...
logs/
  2026-03-01.log            ← One log file per day
  2026-03-02.log
```

### PowerShell Commands for Task Scheduler

Create three scheduled tasks from PowerShell (run as Administrator).
Replace `C:\PersonalWork\stock_bot\v1` with your actual project path and
`C:\Python311\python.exe` with your Python interpreter path.

```powershell
$python = "C:\Python311\python.exe"
$script = "C:\PersonalWork\stock_bot\v1\main.py"
$workdir = "C:\PersonalWork\stock_bot\v1"

# ── Daily task (weekdays at 07:30) ──────────────────────────────────────────
$dailyAction = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "$script --run-mode daily" `
    -WorkingDirectory $workdir

$dailyTrigger = New-ScheduledTaskTrigger `
    -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "07:30"

Register-ScheduledTask `
    -TaskName "StockBot-Daily" `
    -Action $dailyAction `
    -Trigger $dailyTrigger `
    -RunLevel Highest `
    -Force

# ── Weekly task (Sunday at 08:00) ───────────────────────────────────────────
$weeklyAction = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "$script --run-mode weekly" `
    -WorkingDirectory $workdir

$weeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "08:00"

Register-ScheduledTask `
    -TaskName "StockBot-Weekly" `
    -Action $weeklyAction `
    -Trigger $weeklyTrigger `
    -RunLevel Highest `
    -Force

# ── Monthly task (1st of each month at 08:30) ───────────────────────────────
# Note: New-ScheduledTaskTrigger has no -Monthly parameter; use schtasks.exe instead.
schtasks /create `
    /tn "StockBot-Monthly" `
    /tr "`"$python`" `"$script`" --run-mode monthly" `
    /sc monthly /d 1 /st 08:30 `
    /rl highest /f
# Workaround to set the working directory on the monthly task:
$task = Get-ScheduledTask -TaskName "StockBot-Monthly"
$task.Actions[0].WorkingDirectory = $workdir
Set-ScheduledTask -TaskName "StockBot-Monthly" -Action $task.Actions
```

### Manual One-Off Runs

```powershell
# From the project directory:
python main.py --run-mode daily          # Quiet check
python main.py --run-mode weekly         # Force digest
python main.py --run-mode monthly        # Force capital memo
python main.py --run-mode daily --debug  # Verbose logging
python main.py --dry-run                 # No files written, no emails
```

### Overlap Protection

A file lock (`data/run.lock`) prevents two instances from running at the same
time. If Task Scheduler fires a second run while the first is still in progress,
the second exits immediately with code 0. Locks older than 30 minutes are
treated as stale (e.g. after a crash) and removed automatically.

### Network Resilience

- If the Alpha Vantage API is unavailable, the system falls back to cached
  prices (from `data/price_cache.json`). The cache TTL is configured by
  `market_data.cache_ttl_seconds` in `config.json` (default: 3600 seconds).
- Email failures are logged as warnings and do **not** abort the run.
- All errors return exit code 1; success returns exit code 0 — making it easy
  for Task Scheduler to detect and alert on failures.

---

## Claude Code MCP Server

The repo ships a local [MCP](https://modelcontextprotocol.io) server that lets
Claude Code run the portfolio tool safely — without exposing secrets or allowing
arbitrary shell commands.

### Setup

**Step 1 — Install the MCP package** (one-time):
```powershell
pip install mcp
```

**Step 2 — Set your API keys in `.env`** (never in `.mcp.json`):
```
# .env
ALPHA_VANTAGE_API_KEY=your_key_here
FMP_API_KEY=your_key_here        # required if scanner.enabled = true
EMAIL_PASSWORD=your_app_password  # optional — only if email enabled
```

**Step 3 — `.mcp.json` is already at the repo root:**
```json
{
  "mcpServers": {
    "stockbot-mcp": {
      "command": "py",
      "args": ["-m", "stockbot_mcp_server"]
    }
  }
}
```
Claude Code reads this file automatically when you open the repo folder.
The server starts on demand via `py -m stockbot_mcp_server`.

**Step 4 — Verify** (in Claude Code):
```
/mcp
```
You should see `stockbot-mcp` listed with four tools.

### Available Tools

| Tool | Description |
|------|-------------|
| `doctor()` | Checks dirs, config.json, Python imports, env vars (`FMP_API_KEY`, `ALPHA_VANTAGE_API_KEY`) — never prints values. Returns a JSON status object. |
| `run(...)` | Runs `py main.py`; captures stdout/stderr to `logs/<mode>_run_<ts>.log`; returns `{exit_code, skipped, ran_command, log_file, latest_outputs_files}` |
| `latest_summary()` | Reads `outputs/latest/` and summarises: value, drawdown %, top-5 drifts, sleeve %, top-5 scanner candidates |
| `tail_log(lines=80)` | Returns last N lines of the newest log file (redacted) |

### Example Usage

**Health check:**
```
Run the stockbot-mcp doctor tool.
```

**Dry-run preview (no files written, no email):**
```
Run the portfolio tool in monthly mode as a dry run.
```
→ `run(mode="monthly", dry_run=True, no_email=True)`

**Live monthly run (writes output files, no email):**
```
Do a live monthly run — skip email but write the output files.
```
→ `run(mode="monthly", dry_run=False, no_email=True)`

**Review latest output:**
```
Show me the latest portfolio summary.
```
→ `latest_summary()`

**Check the log for errors:**
```
Show me the last 60 lines of the most recent log.
```
→ `tail_log(lines=60)`

### Safety

- **No secrets in `.mcp.json`** — API keys stay in `.env` only
- **Redaction**: any `apikey=`, `password=`, `token=`, or `secret=` value in
  any output is replaced with `<REDACTED>` before being returned
- **Run lock**: Windows named mutex (`Global\StockBotMCPRun`) + `asyncio.Lock`
  prevent overlapping `run()` calls; if locked, returns `{skipped: true}`
- **Subprocess only**: only `py main.py` is ever invoked — no `eval`, `exec`,
  or arbitrary shell access

---

## AI Agent Layer (Hybrid 3)

The AI agent runs **after** the deterministic engine and converts raw outputs into
human-readable memos, escalation packets, and (optionally) maintainer patches.

- **Provider-aware** — set `STOCKBOT_LLM_PROVIDER=ollama|anthropic|openai`
- **Ollama-ready** — local models use Ollama's OpenAI-compatible `/v1` endpoints
- **Fallback-safe** — the agent falls back before dropping to the offline stub
- **Offline mode** — writes deterministic templated memos with no LLM at all

### Quick Start

```powershell
# 1. Install optional deps (Claude needs anthropic SDK)
pip install anthropic          # only needed for monthly / maintainer

# 2. Configure the preferred provider
# .env
STOCKBOT_LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=gemma3:4b
OLLAMA_API_KEY=ollama

# 3. Optional: set Anthropic for fallback / monthly / maintainer
# .env
ANTHROPIC_API_KEY=sk-ant-...

# 4. Smoke-test the provider first
python -m tools.llm_smoke_test --provider ollama

# 5. Run the engine first, then the agent
python main.py --run-mode daily --llm-provider ollama
py -m agent --mode daily --provider ollama
```

### Windows PowerShell Setup For Ollama

```powershell
# 1. Install Ollama
winget install Ollama.Ollama

# 2. Start the local Ollama service
ollama serve

# 3. Pull the model StockBot should use
ollama pull gemma3:4b

# 4. Set provider vars for the current shell
$env:STOCKBOT_LLM_PROVIDER = "ollama"
$env:OLLAMA_BASE_URL = "http://localhost:11434/v1"
$env:OLLAMA_MODEL = "gemma3:4b"
$env:OLLAMA_API_KEY = "ollama"

# 5. Smoke test
python -m tools.llm_smoke_test --provider ollama

# 6. Run the repo entry points
py -m theme_engine --mode daily --dry-run
python main.py --run-mode daily --llm-provider ollama --dry-run
py -m agent --mode daily --provider ollama
```

If the smoke test says the model is missing, run:

```powershell
ollama pull gemma3:4b
```

To switch back later:

```powershell
$env:STOCKBOT_LLM_PROVIDER = "anthropic"
# or clear the override entirely
Remove-Item Env:\STOCKBOT_LLM_PROVIDER
```

### CLI

```
py -m agent --mode daily|weekly|monthly [OPTIONS]

Options:
  --no-network          Offline mode — write templated memos, no LLM calls
  --ollama-model NAME   Override Ollama model (default: gemma3:4b)
  --claude-model NAME   Override Claude model (default: claude-haiku-4-5-20251001)
  --config PATH         Config file or config/ directory (default: config.json)
  --profile NAME        Optional structured config profile
  --provider NAME       Provider override for this run

Environment trigger for offline mode:
  STOCKBOT_TESTING=1    Same as --no-network (for unit tests / CI)
```

### Required Environment Variables

| Variable | Required for | Where to set |
|----------|-------------|--------------|
| `ALPHA_VANTAGE_API_KEY` | Engine (price fetch) | `.env` |
| `FMP_API_KEY` | Engine scanner | `.env` |
| `STOCKBOT_LLM_PROVIDER` | Preferred provider for `theme_engine` + `agent` | `.env` or shell |
| `OLLAMA_BASE_URL` | Ollama OpenAI-compatible base URL | `.env` or shell |
| `OLLAMA_API_KEY` | Placeholder API key for Ollama-compatible clients | `.env` or shell |
| `OLLAMA_MODEL` | Ollama model for theme engine + agent | `.env` or shell |
| `ANTHROPIC_API_KEY` | Agent monthly + maintainer | `.env` |
| `ANTHROPIC_MODEL` | Agent Claude model (optional override) | `.env` or shell |
| `OPENAI_API_KEY` | Optional when `STOCKBOT_LLM_PROVIDER=openai` | `.env` |
| `OPENAI_MODEL` | Optional when `STOCKBOT_LLM_PROVIDER=openai` | `.env` or shell |
| `OPENAI_BASE_URL` | Optional OpenAI-compatible override | `.env` or shell |

### Task-Specific Provider Preferences

Use config for workflow-specific routing and keep `STOCKBOT_LLM_PROVIDER` as the global override:

```json
{
  "theme_engine": {
    "task_providers": {
      "daily": "ollama"
    }
  },
  "agent": {
    "task_providers": {
      "standalone": "ollama",
      "weekly": "ollama",
      "monthly": "anthropic",
      "maintainer": "anthropic"
    }
  }
}
```

Resolution order:

1. CLI arg such as `python main.py --llm-provider openai`, `py -m theme_engine --provider ollama`, or `py -m agent --provider anthropic`
2. `STOCKBOT_LLM_PROVIDER`
3. Task-specific config such as `theme_engine.task_providers.daily` or `agent.task_providers.monthly`
4. Existing built-in fallback order

### Agent Output Files

All written to `outputs/latest/`:

| File | When written | Generator |
|------|-------------|-----------|
| `agent_bundle.json` | Every run | `bundle_builder.py` |
| `decision_memo.md` | daily / weekly | preferred provider → fallbacks → offline stub |
| `escalation_packet.md` | daily / weekly (if violations) | rule-based |
| `monthly_memo.md` | monthly | preferred provider → fallbacks → offline stub |
| `email_draft.md` | monthly | provider output / `NO_EMAIL` if email disabled |
| `maintainer_patch.diff` | any mode, if `approved_actions.json` exists | preferred provider → fallbacks |
| `maintainer_plan.md` | any mode, if `approved_actions.json` exists | preferred provider → fallbacks |

### LLM Routing

```
If a CLI provider or STOCKBOT_LLM_PROVIDER is set:
  1. Try the CLI provider or STOCKBOT_LLM_PROVIDER first
  2. Then use the normal fallback chain

If no CLI provider and STOCKBOT_LLM_PROVIDER are unset:
  daily / weekly:
    1. Try Ollama
    2. Fall back to Anthropic
    3. Use the offline stub if neither is available

  monthly / maintainer:
    1. Try Anthropic
    2. Fall back to Ollama
    3. Use the offline stub / blocked note if neither is available

Task-specific config can change the first provider when the global override is absent:
  - theme_engine.task_providers.daily=ollama keeps daily theme detection local
  - agent.task_providers.monthly=anthropic keeps monthly memo generation on Claude
  - agent.task_providers.maintainer=openai routes maintainer work to an OpenAI-compatible endpoint
```

### Recommended Task Scheduler Chaining

Run the engine at 07:10, agent at 07:15 (5-minute gap ensures engine outputs are ready):

Scheduler-safe wrapper example:

```powershell
$workdir = "C:\PersonalWork\stock_bot\v1"
Set-Location $workdir

python -m tools.llm_smoke_test --provider ollama
if ($LASTEXITCODE -ne 0) {
    Write-Error "Ollama smoke test failed. Aborting scheduled run."
    exit 1
}

python main.py --run-mode daily --llm-provider ollama
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

py -m agent --mode daily --provider ollama
exit $LASTEXITCODE
```

```powershell
$python   = "C:\Python311\python.exe"
$workdir  = "C:\PersonalWork\stock_bot\v1"

# Engine — daily at 07:10
$engineAction = New-ScheduledTaskAction `
    -Execute $python -Argument "main.py --run-mode daily" -WorkingDirectory $workdir
Register-ScheduledTask -TaskName "StockBot-Engine-Daily" `
    -Action $engineAction `
    -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Mon,Tue,Wed,Thu,Fri -At "07:10") `
    -RunLevel Highest -Force

# Agent — daily at 07:15
$agentAction = New-ScheduledTaskAction `
    -Execute $python -Argument "-m agent --mode daily" -WorkingDirectory $workdir
Register-ScheduledTask -TaskName "StockBot-Agent-Daily" `
    -Action $agentAction `
    -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Mon,Tue,Wed,Thu,Fri -At "07:15") `
    -RunLevel Highest -Force
```

### PowerShell Examples

Daily run on Ollama:

```powershell
Set-Location C:\PersonalWork\stock_bot\v1

Remove-Item Env:\STOCKBOT_LLM_PROVIDER -ErrorAction SilentlyContinue
$env:OLLAMA_BASE_URL = "http://localhost:11434/v1"
$env:OLLAMA_MODEL = "gemma3:4b"
$env:OLLAMA_API_KEY = "ollama"

python -m tools.llm_smoke_test --provider ollama
if ($LASTEXITCODE -ne 0) { exit 1 }

python main.py --run-mode daily --llm-provider ollama
py -m agent --mode daily --provider ollama
```

Monthly run on Anthropic:

```powershell
Set-Location C:\PersonalWork\stock_bot\v1

Remove-Item Env:\STOCKBOT_LLM_PROVIDER -ErrorAction SilentlyContinue
$env:ANTHROPIC_API_KEY = "your_anthropic_key_here"
$env:ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

python main.py --run-mode monthly
py -m agent --mode monthly --provider anthropic
```

Monthly run on OpenAI:

```powershell
Set-Location C:\PersonalWork\stock_bot\v1

Remove-Item Env:\STOCKBOT_LLM_PROVIDER -ErrorAction SilentlyContinue
$env:OPENAI_API_KEY = "your_openai_key_here"
$env:OPENAI_MODEL = "gpt-4o-mini"

python main.py --run-mode monthly --llm-provider openai
py -m agent --mode monthly --provider openai
```

### Provider Evaluation Workflow

Use the evaluation helper to run the same task across providers, reuse the existing LLM metadata sidecars, and save copied output artifacts side by side under `outputs/evals/`.

Daily memo comparison:

```powershell
python -m tools.provider_eval --task agent_daily --providers ollama anthropic openai
```

Daily memo comparison without fallback:

```powershell
python -m tools.provider_eval --task agent_daily --providers ollama anthropic openai --disable-fallback
```

Monthly memo comparison:

```powershell
python -m tools.provider_eval --task agent_monthly --providers anthropic openai ollama
```

Daily theme comparison:

```powershell
python -m tools.provider_eval --task theme_daily --providers ollama anthropic openai
```

Each run writes:

- `outputs/evals/<timestamp>_<task>/provider_eval.csv`
- `outputs/evals/<timestamp>_<task>/provider_eval_summary.md`
- `outputs/evals/<timestamp>_<task>/artifacts/*`

Artifact naming is review-oriented:

- same-provider success: `agent_daily__requested-ollama.md`
- fallback case: `agent_daily__requested-ollama__actual-anthropic.md`

The CSV includes automatic telemetry such as provider, model, latency, success, fallback, and copied output path, plus blank manual review columns:

- `manual_score_relevance`
- `manual_score_clarity`
- `manual_score_structure`
- `manual_score_actionability`
- `manual_score_hallucination_risk`
- `notes`

Recommended review loop:

1. Run the eval command for the task you care about.
2. Open `provider_eval_summary.md` for the quick comparison table.
3. Open the copied artifacts in `outputs/evals/<timestamp>_<task>/artifacts/`.
4. Compare clarity, structure, actionability, and hallucination risk side by side.
5. Fill in the manual scoring columns in `provider_eval.csv`.
6. Use `--disable-fallback` when you want a pure provider measurement instead of “best available output”.
7. Repeat for daily and monthly tasks before changing your default provider routing.

### Maintainer Patch Workflow

1. Create `approved_actions.json` in the repo root:
   ```json
   {
     "actions": [
       {
         "id": "fix-001",
         "file": "finance_analyzer.py",
         "line_range": [100, 110],
         "description": "Add date deduplication to FinanceHistoryStore.add()"
       }
     ]
   }
   ```
2. Run the agent: `py -m agent --mode daily`
3. Review `outputs/latest/maintainer_patch.diff` and `maintainer_plan.md`
4. Apply with `git apply outputs/latest/maintainer_patch.diff`
5. Delete `approved_actions.json` to re-arm the gate

### Agent Module Structure

```
agent/
  __init__.py          # Package marker
  __main__.py          # py -m agent entry point
  agent_runner.py      # Main pipeline: bundle → LLM → write files
  bundle_builder.py    # Synthesise agent_bundle.json from CSV/JSON/SQLite
  llm_adapters.py      # call_ollama() + call_claude()
  prompts.py           # 3 prompt templates (daily/weekly, monthly, maintainer)
  io_utils.py          # redact(), write_markdown_atomic(), tail_latest_log()
  repo_tree.py         # get_repo_tree() — compact tree for maintainer prompt
  mcp_agent_tools.py   # MCP server for testing the agent layer from Claude Code
```

### Running Agent Tests

```bash
python -m unittest tests.test_agent_bundle_builder -v    # 17 tests
python -m unittest tests.test_agent_runner_offline_mode -v  # 14 tests
```

All agent tests are fully offline — no Ollama, no Claude API, no network.

---

## Disclaimer

This tool is for informational and educational purposes only. It is NOT:
- A trading bot
- A prediction engine
- Financial advice

Always consult a qualified financial advisor before making investment decisions.
