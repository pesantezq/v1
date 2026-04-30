"""
AI Cost Budget Wrapper — observe-only guardrail for optional AI/LLM features.

Tracks token usage and estimated cost across AI calls, enforces optional
daily/monthly limits, and writes structured artifacts for operator review.

Default behavior (observe_only=True):
  - Never blocks any AI call.
  - Records every call, estimates cost, and flags when limits would be exceeded.
  - Writes outputs/policy/ai_usage_events.jsonl (append-only).
  - Writes outputs/latest/ai_budget_summary.json + .md each run.

Rule: AI cannot override scoring or recommendation decisions.
Rule: Budget guardrails fail closed only when observe_only=False.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

logger = logging.getLogger("stockbot.portfolio_automation.ai_budget")

# ---------------------------------------------------------------------------
# Pricing table
# Prices per million tokens (USD) — (input_cost, output_cost).
# Conservative static estimates; may lag provider pricing changes.
# ---------------------------------------------------------------------------

_PRICING_PER_MILLION: dict[str, tuple[float, float]] = {
    # Anthropic Claude
    "claude-haiku-4-5-20251001":  (1.00,   5.00),
    "claude-haiku-4-5":           (1.00,   5.00),
    "claude-3-5-haiku-20241022":  (1.00,   5.00),
    "claude-3-haiku-20240307":    (0.25,   1.25),
    "claude-sonnet-4-6":          (3.00,  15.00),
    "claude-3-5-sonnet-20241022": (3.00,  15.00),
    "claude-3-5-sonnet-20240620": (3.00,  15.00),
    "claude-3-sonnet-20240229":   (3.00,  15.00),
    "claude-opus-4-7":            (15.00, 75.00),
    "claude-3-opus-20240229":     (15.00, 75.00),
    # OpenAI
    "gpt-4o-mini":                (0.15,   0.60),
    "gpt-4o":                     (2.50,  10.00),
    "gpt-4o-2024-11-20":          (2.50,  10.00),
    "gpt-4-turbo":                (10.00, 30.00),
    "gpt-3.5-turbo":              (0.50,   1.50),
    # Ollama / local models (free)
    "gemma3:4b":                  (0.00,   0.00),
    "llama3":                     (0.00,   0.00),
    "mistral":                    (0.00,   0.00),
}

# Providers where all models are always free
_FREE_PROVIDERS: frozenset[str] = frozenset({"ollama", "local"})

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AIBudgetExceeded(Exception):
    """Raised by with_ai_budget when budget is exceeded and observe_only=False."""


# ---------------------------------------------------------------------------
# Config and data model
# ---------------------------------------------------------------------------


@dataclass
class AIBudgetConfig:
    enabled: bool = True
    observe_only: bool = True
    daily_token_limit: int | None = None
    daily_cost_limit_usd: float | None = None
    monthly_cost_limit_usd: float | None = None
    warn_at_daily_cost_pct: float = 0.80
    warn_at_monthly_cost_pct: float = 0.80
    default_provider: str | None = None
    default_model: str | None = None


@dataclass
class AIUsageEvent:
    timestamp: str
    task_name: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    allowed: bool
    run_id: str | None = None
    provider: str | None = None
    model: str | None = None
    blocked_reason: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class AIBudgetSummary:
    generated_at: str
    observe_only: bool
    enabled: bool
    daily_token_total: int
    daily_cost_total_usd: float
    monthly_cost_total_usd: float
    daily_cost_limit_usd: float | None
    monthly_cost_limit_usd: float | None
    warning: bool
    blocked: bool
    warnings: list[str]
    events: list[AIUsageEvent]
    summary_line: str = ""


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def estimate_ai_cost(
    provider: str | None,
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """
    Estimate AI call cost in USD using a static pricing table.

    Returns 0.0 for unknown models or free providers.
    Does not make any API calls.
    """
    if provider and provider.lower() in _FREE_PROVIDERS:
        return 0.0

    if model:
        pricing = _PRICING_PER_MILLION.get(model.strip())
        if pricing:
            input_price, output_price = pricing
            cost = (
                input_price * max(0, prompt_tokens) / 1_000_000
                + output_price * max(0, completion_tokens) / 1_000_000
            )
            return round(cost, 8)

    return 0.0


def _pricing_known(provider: str | None, model: str | None) -> bool:
    """Return True if we have pricing data for this provider/model pair."""
    if provider and provider.lower() in _FREE_PROVIDERS:
        return True
    if model and model.strip() in _PRICING_PER_MILLION:
        return True
    return False


# ---------------------------------------------------------------------------
# Budget check
# ---------------------------------------------------------------------------


def check_ai_budget(
    task_name: str,
    provider: str | None = None,
    model: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    run_id: str | None = None,
    config: AIBudgetConfig | None = None,
    metadata: dict | None = None,
    *,
    _current_daily_cost_usd: float = 0.0,
    _current_monthly_cost_usd: float = 0.0,
    _current_daily_tokens: int = 0,
) -> AIUsageEvent:
    """
    Check AI budget constraints and return an AIUsageEvent.

    Behavior:
      - observe_only=True  (default): always returns allowed=True; records
        warnings when limits would be exceeded.
      - observe_only=False: returns allowed=False and sets blocked_reason when
        daily or monthly cost limit is exceeded.
      - enabled=False: always returns allowed=True regardless of limits.

    The _current_* parameters carry accumulated totals for the current period.
    Pass these when enforcing budget across multiple calls in the same run
    (e.g., from load_recent_ai_usage_events). Default is 0.0 (no history).

    Never raises for missing or unknown provider/model.
    """
    cfg = config or AIBudgetConfig()
    ts = datetime.now(timezone.utc).isoformat()
    prompt_tokens = max(0, prompt_tokens)
    completion_tokens = max(0, completion_tokens)
    total_tokens = prompt_tokens + completion_tokens

    cost = estimate_ai_cost(provider, model, prompt_tokens, completion_tokens)
    unknown_pricing = not _pricing_known(provider, model)

    event_metadata: dict[str, Any] = dict(metadata or {})
    if unknown_pricing and total_tokens > 0:
        event_metadata["unknown_pricing"] = True
        event_metadata["pricing_note"] = (
            f"No pricing data for provider={provider!r} model={model!r}. "
            "Cost estimated as $0.00."
        )

    warnings: list[str] = []
    blocked = False
    blocked_reason: str | None = None

    if not cfg.enabled:
        return AIUsageEvent(
            timestamp=ts,
            task_name=task_name,
            provider=provider,
            model=model,
            run_id=run_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=cost,
            allowed=True,
            blocked_reason=None,
            metadata=event_metadata,
        )

    new_daily_cost = _current_daily_cost_usd + cost
    new_monthly_cost = _current_monthly_cost_usd + cost
    new_daily_tokens = _current_daily_tokens + total_tokens

    # ── Daily cost limit ────────────────────────────────────────────────────
    if cfg.daily_cost_limit_usd is not None:
        if new_daily_cost > cfg.daily_cost_limit_usd:
            blocked = True
            blocked_reason = (
                f"Daily cost limit ${cfg.daily_cost_limit_usd:.4f} USD would be exceeded "
                f"(current ${_current_daily_cost_usd:.4f} + ${cost:.6f} = "
                f"${new_daily_cost:.4f})"
            )
            warnings.append(blocked_reason)
        elif new_daily_cost >= cfg.daily_cost_limit_usd * cfg.warn_at_daily_cost_pct:
            warn_msg = (
                f"Daily cost at {new_daily_cost / cfg.daily_cost_limit_usd:.0%} of limit "
                f"(${new_daily_cost:.4f} / ${cfg.daily_cost_limit_usd:.4f})"
            )
            warnings.append(warn_msg)

    # ── Monthly cost limit ──────────────────────────────────────────────────
    if cfg.monthly_cost_limit_usd is not None:
        if new_monthly_cost > cfg.monthly_cost_limit_usd:
            blocked = True
            blocked_reason = blocked_reason or (
                f"Monthly cost limit ${cfg.monthly_cost_limit_usd:.4f} USD would be exceeded "
                f"(current ${_current_monthly_cost_usd:.4f} + ${cost:.6f} = "
                f"${new_monthly_cost:.4f})"
            )
            warnings.append(blocked_reason)
        elif new_monthly_cost >= cfg.monthly_cost_limit_usd * cfg.warn_at_monthly_cost_pct:
            warn_msg = (
                f"Monthly cost at {new_monthly_cost / cfg.monthly_cost_limit_usd:.0%} of limit "
                f"(${new_monthly_cost:.4f} / ${cfg.monthly_cost_limit_usd:.4f})"
            )
            if warn_msg not in warnings:
                warnings.append(warn_msg)

    # ── Daily token limit ───────────────────────────────────────────────────
    if cfg.daily_token_limit is not None and new_daily_tokens > cfg.daily_token_limit:
        blocked = True
        tok_msg = (
            f"Daily token limit {cfg.daily_token_limit:,} would be exceeded "
            f"({new_daily_tokens:,} tokens)"
        )
        blocked_reason = blocked_reason or tok_msg
        warnings.append(tok_msg)

    if warnings:
        event_metadata["budget_warnings"] = warnings

    allowed = True
    if blocked:
        if cfg.observe_only:
            allowed = True  # observe_only never blocks
        else:
            allowed = False

    return AIUsageEvent(
        timestamp=ts,
        task_name=task_name,
        provider=provider,
        model=model,
        run_id=run_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=cost,
        allowed=allowed,
        blocked_reason=blocked_reason if not allowed else None,
        metadata=event_metadata,
    )


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class with_ai_budget:
    """
    Context manager that gates optional AI calls against the budget policy.

    Usage::

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

    When observe_only=True (default): never raises; budget_event.allowed
    reflects whether the call would exceed limits.

    When observe_only=False and budget exceeded: raises AIBudgetExceeded.
    """

    def __init__(
        self,
        task_name: str,
        provider: str | None = None,
        model: str | None = None,
        estimated_prompt_tokens: int = 0,
        estimated_completion_tokens: int = 0,
        config: AIBudgetConfig | None = None,
        *,
        _current_daily_cost_usd: float = 0.0,
        _current_monthly_cost_usd: float = 0.0,
    ) -> None:
        self.task_name = task_name
        self.provider = provider
        self.model = model
        self.estimated_prompt_tokens = estimated_prompt_tokens
        self.estimated_completion_tokens = estimated_completion_tokens
        self.config = config or AIBudgetConfig()
        self._current_daily_cost_usd = _current_daily_cost_usd
        self._current_monthly_cost_usd = _current_monthly_cost_usd
        self._event: AIUsageEvent | None = None

    def __enter__(self) -> AIUsageEvent:
        self._event = check_ai_budget(
            task_name=self.task_name,
            provider=self.provider,
            model=self.model,
            prompt_tokens=self.estimated_prompt_tokens,
            completion_tokens=self.estimated_completion_tokens,
            config=self.config,
            _current_daily_cost_usd=self._current_daily_cost_usd,
            _current_monthly_cost_usd=self._current_monthly_cost_usd,
        )
        if not self._event.allowed and not self.config.observe_only:
            raise AIBudgetExceeded(
                self._event.blocked_reason or "AI budget limit exceeded"
            )
        return self._event

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        return False  # do not suppress exceptions


# ---------------------------------------------------------------------------
# Event persistence
# ---------------------------------------------------------------------------


def _event_to_dict(event: AIUsageEvent) -> dict:
    return {
        "timestamp": event.timestamp,
        "task_name": event.task_name,
        "provider": event.provider,
        "model": event.model,
        "run_id": event.run_id,
        "prompt_tokens": event.prompt_tokens,
        "completion_tokens": event.completion_tokens,
        "total_tokens": event.total_tokens,
        "estimated_cost_usd": event.estimated_cost_usd,
        "allowed": event.allowed,
        "blocked_reason": event.blocked_reason,
        "metadata": event.metadata,
    }


def _event_from_dict(d: dict) -> AIUsageEvent:
    return AIUsageEvent(
        timestamp=str(d.get("timestamp", "")),
        task_name=str(d.get("task_name", "")),
        provider=d.get("provider"),
        model=d.get("model"),
        run_id=d.get("run_id"),
        prompt_tokens=int(d.get("prompt_tokens", 0)),
        completion_tokens=int(d.get("completion_tokens", 0)),
        total_tokens=int(d.get("total_tokens", 0)),
        estimated_cost_usd=float(d.get("estimated_cost_usd", 0.0)),
        allowed=bool(d.get("allowed", True)),
        blocked_reason=d.get("blocked_reason"),
        metadata=dict(d.get("metadata") or {}),
    )


def record_ai_usage_event(
    event: AIUsageEvent,
    base_dir: str | Path = "outputs",
) -> Path:
    """
    Append an AIUsageEvent to the JSONL event log in OutputNamespace.POLICY.

    Writes to outputs/policy/ai_usage_events.jsonl.
    Creates the file and parent directory if they don't exist.
    """
    from portfolio_automation.data_governance import OutputNamespace, get_output_path, ensure_output_dir

    ensure_output_dir(OutputNamespace.POLICY, base_dir=str(base_dir))
    out_path = get_output_path(
        OutputNamespace.POLICY, "ai_usage_events.jsonl", base_dir=str(base_dir)
    )
    with out_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_event_to_dict(event), default=str) + "\n")
    return out_path


def load_recent_ai_usage_events(
    path: str | Path = "outputs/policy/ai_usage_events.jsonl",
    max_events: int = 500,
) -> list[AIUsageEvent]:
    """
    Load recent AI usage events from the JSONL event log.

    Tolerates:
      - missing file (returns empty list)
      - malformed JSON lines (skips with a debug log)
      - missing fields in individual records
    """
    p = Path(path)
    if not p.exists():
        return []

    events: list[AIUsageEvent] = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("ai_budget: could not read %s: %s", p, exc)
        return []

    for line_no, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            events.append(_event_from_dict(d))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.debug("ai_budget: skipping malformed line %d in %s: %s", line_no, p, exc)

    # Return the most recent events up to max_events
    return events[-max_events:]


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp tolerantly."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _compute_summary_line(summary: AIBudgetSummary) -> str:
    if not summary.enabled:
        return "AI budget tracking disabled"
    if summary.blocked:
        return (
            f"AI BUDGET EXCEEDED — ${summary.daily_cost_total_usd:.4f} USD today, "
            f"${summary.monthly_cost_total_usd:.4f} USD this month"
        )
    if summary.warning:
        return (
            f"AI budget warning — ${summary.daily_cost_total_usd:.4f} USD today "
            f"({len(summary.events)} event(s))"
        )
    if not summary.events:
        return "No AI calls logged today"
    return (
        f"${summary.daily_cost_total_usd:.4f} USD today "
        f"({summary.daily_token_total:,} tokens, {len(summary.events)} event(s))"
    )


def write_ai_budget_summary(
    events: list[AIUsageEvent],
    config: AIBudgetConfig | None = None,
    base_dir: str | Path = "outputs",
) -> AIBudgetSummary:
    """
    Compute an AIBudgetSummary from a list of usage events and write
    JSON + Markdown artifacts to OutputNamespace.LATEST.

    Returns the AIBudgetSummary. Always succeeds even with an empty events list.
    """
    from portfolio_automation.data_governance import OutputNamespace, safe_write_json, safe_write_text

    cfg = config or AIBudgetConfig()
    ts = datetime.now(timezone.utc).isoformat()
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    daily_events = [e for e in events if _parse_ts(e.timestamp) >= today_start]
    monthly_events = [e for e in events if _parse_ts(e.timestamp) >= month_start]

    daily_token_total = sum(e.total_tokens for e in daily_events)
    daily_cost_total = sum(e.estimated_cost_usd for e in daily_events)
    monthly_cost_total = sum(e.estimated_cost_usd for e in monthly_events)

    warnings: list[str] = []
    blocked = any(not e.allowed for e in events)
    warning = False

    if cfg.daily_cost_limit_usd is not None and daily_cost_total > 0:
        pct = daily_cost_total / cfg.daily_cost_limit_usd
        if pct >= 1.0:
            warnings.append(
                f"Daily cost limit EXCEEDED: ${daily_cost_total:.4f} / ${cfg.daily_cost_limit_usd:.4f}"
            )
        elif pct >= cfg.warn_at_daily_cost_pct:
            warnings.append(
                f"Daily cost at {pct:.0%} of limit: ${daily_cost_total:.4f} / ${cfg.daily_cost_limit_usd:.4f}"
            )
            warning = True

    if cfg.monthly_cost_limit_usd is not None and monthly_cost_total > 0:
        pct = monthly_cost_total / cfg.monthly_cost_limit_usd
        if pct >= 1.0:
            warnings.append(
                f"Monthly cost limit EXCEEDED: ${monthly_cost_total:.4f} / ${cfg.monthly_cost_limit_usd:.4f}"
            )
        elif pct >= cfg.warn_at_monthly_cost_pct:
            warnings.append(
                f"Monthly cost at {pct:.0%} of limit: ${monthly_cost_total:.4f} / ${cfg.monthly_cost_limit_usd:.4f}"
            )
            warning = True

    summary = AIBudgetSummary(
        generated_at=ts,
        observe_only=cfg.observe_only,
        enabled=cfg.enabled,
        daily_token_total=daily_token_total,
        daily_cost_total_usd=round(daily_cost_total, 6),
        monthly_cost_total_usd=round(monthly_cost_total, 6),
        daily_cost_limit_usd=cfg.daily_cost_limit_usd,
        monthly_cost_limit_usd=cfg.monthly_cost_limit_usd,
        warning=warning or bool(warnings),
        blocked=blocked,
        warnings=warnings,
        events=daily_events,
    )
    summary.summary_line = _compute_summary_line(summary)

    payload = _summary_to_dict(summary)
    safe_write_json(
        OutputNamespace.LATEST,
        "ai_budget_summary.json",
        payload,
        base_dir=str(base_dir),
    )
    safe_write_text(
        OutputNamespace.LATEST,
        "ai_budget_summary.md",
        _build_summary_markdown(summary),
        base_dir=str(base_dir),
    )
    logger.info("AI BUDGET: summary written — %s", summary.summary_line)
    return summary


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _summary_to_dict(summary: AIBudgetSummary) -> dict:
    return {
        "generated_at": summary.generated_at,
        "observe_only": summary.observe_only,
        "enabled": summary.enabled,
        "daily_token_total": summary.daily_token_total,
        "daily_cost_total_usd": summary.daily_cost_total_usd,
        "monthly_cost_total_usd": summary.monthly_cost_total_usd,
        "daily_cost_limit_usd": summary.daily_cost_limit_usd,
        "monthly_cost_limit_usd": summary.monthly_cost_limit_usd,
        "warning": summary.warning,
        "blocked": summary.blocked,
        "warnings": summary.warnings,
        "summary_line": summary.summary_line,
        "event_count": len(summary.events),
        "events": [_event_to_dict(e) for e in summary.events],
    }


def _build_summary_markdown(summary: AIBudgetSummary) -> str:
    lines: list[str] = []
    lines.append("# AI Budget Summary")
    lines.append("")
    lines.append(f"**Generated:** {summary.generated_at}  ")
    lines.append(f"**Mode:** {'observe-only' if summary.observe_only else 'enforced'}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"**{summary.summary_line}**")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Daily tokens | {summary.daily_token_total:,} |")
    lines.append(f"| Daily cost (USD) | ${summary.daily_cost_total_usd:.6f} |")
    lines.append(f"| Monthly cost (USD) | ${summary.monthly_cost_total_usd:.6f} |")
    daily_limit = f"${summary.daily_cost_limit_usd:.4f}" if summary.daily_cost_limit_usd else "—"
    monthly_limit = f"${summary.monthly_cost_limit_usd:.4f}" if summary.monthly_cost_limit_usd else "—"
    lines.append(f"| Daily limit | {daily_limit} |")
    lines.append(f"| Monthly limit | {monthly_limit} |")
    lines.append(f"| Warning | {'Yes ⚠️' if summary.warning else 'No'} |")
    lines.append(f"| Blocked | {'Yes 🚫' if summary.blocked else 'No'} |")
    lines.append("")

    if summary.warnings:
        lines.append("### Warnings")
        lines.append("")
        for w in summary.warnings:
            lines.append(f"- {w}")
        lines.append("")

    if summary.events:
        lines.append(f"## Today's Events ({len(summary.events)})")
        lines.append("")
        lines.append("| Task | Provider | Model | Tokens | Cost (USD) | Allowed |")
        lines.append("|------|----------|-------|--------|------------|---------|")
        for ev in summary.events[:20]:
            allowed = "✓" if ev.allowed else "✗"
            lines.append(
                f"| {ev.task_name} | {ev.provider or '—'} | {ev.model or '—'} "
                f"| {ev.total_tokens:,} | ${ev.estimated_cost_usd:.6f} | {allowed} |"
            )
        if len(summary.events) > 20:
            lines.append(f"| … | … | … | … | … | {len(summary.events) - 20} more |")
        lines.append("")

    lines.append("---")
    lines.append(
        "*AI Budget Monitor is observe-only by default. "
        "Budget guardrails fail closed only when observe_only=False.*"
    )
    return "\n".join(lines)
