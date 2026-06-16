# AI Cost Budget Wrapper

## Purpose

The AI Budget Wrapper tracks token usage and estimated cost across all optional
AI/LLM calls, enforces configurable daily and monthly limits, and writes
structured operator artifacts each run.

It is a pure observability and guardrail layer. In default mode (`observe_only=True`)
it never blocks any AI call — it records every call, estimates cost, flags
when limits would be exceeded, and writes artifacts for operator review.

---

## Observe-Only Behavior

The wrapper is additive and non-blocking by default:

- All AI calls are allowed when `observe_only=True` (default).
- Budget warnings appear in event metadata and the summary artifact when limits
  would be exceeded — but no call is blocked.
- Switching to `observe_only=False` enables hard enforcement: calls that would
  exceed the daily or monthly cost limit return `allowed=False` and raise
  `AIBudgetExceeded` when used via the context manager.
- `enabled=False` disables tracking entirely — all calls are always allowed
  with no recording.

**Rule:** AI cannot override scoring or recommendation decisions. The budget
wrapper reports cost but does not influence the decision engine.

---

## Artifacts

| File | Path | Namespace |
|------|------|-----------|
| Usage event log | `outputs/policy/ai_usage_events.jsonl` | POLICY (append-only) |
| Budget summary JSON | `outputs/latest/ai_budget_summary.json` | LATEST |
| Budget summary Markdown | `outputs/latest/ai_budget_summary.md` | LATEST |

### JSON Contract: `ai_budget_summary.json`

```json
{
  "generated_at": "2025-01-01T12:00:00+00:00",
  "observe_only": true,
  "enabled": true,
  "daily_token_total": 12500,
  "daily_cost_total_usd": 0.0125,
  "monthly_cost_total_usd": 0.2500,
  "daily_cost_limit_usd": null,
  "monthly_cost_limit_usd": null,
  "warning": false,
  "blocked": false,
  "warnings": [],
  "summary_line": "$0.0125 USD today (12,500 tokens, 3 event(s))",
  "event_count": 3,
  "events": [ ... ]
}
```

### JSONL Event Format: `ai_usage_events.jsonl`

Each line is a JSON object with:

```json
{
  "timestamp": "2025-01-01T12:00:00+00:00",
  "task_name": "decision_explainer",
  "provider": "anthropic",
  "model": "claude-haiku-4-5-20251001",
  "run_id": "run-abc123",
  "prompt_tokens": 1000,
  "completion_tokens": 200,
  "total_tokens": 1200,
  "estimated_cost_usd": 0.000002,
  "allowed": true,
  "blocked_reason": null,
  "metadata": {}
}
```

---

## Budget Configuration

```python
@dataclass
class AIBudgetConfig:
    enabled: bool = True                          # False → all calls allowed, no tracking
    observe_only: bool = True                     # False → hard enforcement enabled
    daily_token_limit: int | None = None          # Block if daily tokens exceed this
    daily_cost_limit_usd: float | None = None     # Block if daily cost exceeds this
    monthly_cost_limit_usd: float | None = None   # Block if monthly cost exceeds this
    warn_at_daily_cost_pct: float = 0.80          # Warn when 80% of daily limit used
    warn_at_monthly_cost_pct: float = 0.80        # Warn when 80% of monthly limit used
    default_provider: str | None = None
    default_model: str | None = None
```

---

## Pricing Coverage

Static pricing table in `_PRICING_PER_MILLION` (USD per million tokens):

| Provider | Model | Input | Output |
|----------|-------|-------|--------|
| Anthropic | claude-haiku-4-5-20251001 | $1.00 | $5.00 |
| Anthropic | claude-sonnet-4-6 | $3.00 | $15.00 |
| Anthropic | claude-opus-4-7 | $15.00 | $75.00 |
| OpenAI | gpt-4o-mini | $0.15 | $0.60 |
| OpenAI | gpt-4o | $2.50 | $10.00 |
| Local | (all) | $0.00 | $0.00 |

Unknown models return $0.00 with `unknown_pricing: true` in event metadata.

---

## Module API

```python
from portfolio_automation.ai_budget import (
    AIBudgetConfig,
    AIBudgetExceeded,
    AIUsageEvent,
    AIBudgetSummary,
    estimate_ai_cost,
    check_ai_budget,
    record_ai_usage_event,
    load_recent_ai_usage_events,
    write_ai_budget_summary,
    with_ai_budget,
)

# Estimate cost without recording
cost = estimate_ai_cost("anthropic", "claude-haiku-4-5-20251001", 1000, 200)

# Check budget and record event
event = check_ai_budget(
    task_name="my_task",
    provider="anthropic",
    model="claude-haiku-4-5-20251001",
    prompt_tokens=1000,
    completion_tokens=200,
    config=cfg,
)

# Append event to JSONL log
record_ai_usage_event(event)

# Load events for summary
events = load_recent_ai_usage_events()

# Write summary artifacts
summary = write_ai_budget_summary(events, config=cfg)
```

---

## Instrumented AI Call Sites

`portfolio_automation/ai_decision_validator.py` is the only module that makes real
LLM calls in the current stack. `portfolio_automation/decision_explainer.py` is
fully deterministic and records no events.

### How instrumentation works

After each `call_provider()` call in `_try_llm_enhance`, a usage event is recorded
via `_record_validator_event()`:

- **Successful call** — records `task_name="ai_decision_validator"`, provider,
  model, estimated prompt/completion tokens, `status="success"`.
- **Failed call** (any exception from `call_provider`) — records same fields with
  `status="error"` and `error=<message[:200]>`, `completion_tokens=0`.

Token counts are estimated from text length (`len(text) // 4 chars per token`)
because `call_provider()` returns plain text with no response object. Events are
annotated with `metadata.usage_source="estimated_from_length"` to distinguish them
from exact API counts.

### Non-blocking guarantee

`_record_validator_event` wraps all budget/filesystem operations in try/except.
If recording fails, a `WARNING` log is emitted and the original LLM call result
(success or failure) is returned unchanged. The pipeline is never blocked.

### Event fields recorded

```json
{
  "task_name": "ai_decision_validator",
  "provider": "openai",
  "model": "gemma3:4b",
  "prompt_tokens": 116,
  "completion_tokens": 7,
  "total_tokens": 123,
  "estimated_cost_usd": 0.0,
  "allowed": true,
  "metadata": {
    "usage_source": "estimated_from_length",
    "status": "success"
  }
}
```

### LLM call is opt-in

`ai_decision_validator` only calls the LLM when `use_llm=True`, which requires
`AI_VALIDATOR_USE_LLM=1` in the environment. By default no LLM calls are made and
no events are recorded.

**AI Budget is observability/cost-control only. It does not change portfolio
decisions, scoring, allocation, recommendations, or discovery behavior.**

---

## Context Manager

```python
with with_ai_budget(
    "decision_explainer",
    provider="anthropic",
    model="claude-haiku-4-5-20251001",
    estimated_prompt_tokens=1000,
    estimated_completion_tokens=200,
    config=cfg,
) as budget_event:
    if budget_event.allowed:
        result = call_llm(...)
    else:
        result = safe_fallback()
```

- `observe_only=True` (default): `__enter__` never raises; check `budget_event.allowed` manually if you want to skip.
- `observe_only=False`: raises `AIBudgetExceeded` when the budget would be exceeded.

---

## Pipeline Integration

The summary is written each run after all AI calls have completed (Section 5 of
`main.py`). Integration is wrapped in `try/except` so any failure is logged as
a warning and the pipeline continues.

```python
# In main.py — after all AI call sections:
try:
    from portfolio_automation.ai_budget import (
        load_recent_ai_usage_events as _load_ai_events,
        write_ai_budget_summary as _write_ai_budget,
    )
    _ai_events = _load_ai_events()
    if not dry_run:
        _ai_summary = _write_ai_budget(_ai_events)
        logger.info("AI BUDGET: %s", _ai_summary.summary_line)
except Exception as _ab_err:
    logger.warning("AI BUDGET: non-fatal error — %s", _ab_err)
```

---

## Future Use

In a future phase, individual AI call sites (e.g., `decision_explainer.py`,
`ai_decision_validator.py`) may:

- Call `record_ai_usage_event` after each successful LLM call
- Use `with_ai_budget` to gate optional calls when hard enforcement is needed
- Read `ai_budget_summary.json` in the GUI Decision Center's System Health card

**Rule:** Budget configuration changes require an explicit Phase 0 step and must
follow the standard observe-only → validate → wire-in lifecycle.

---

## Module Location

```
portfolio_automation/ai_budget.py
```

Tests:

```bash
python -m pytest -q tests/test_ai_budget.py
```
