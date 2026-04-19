"""
Alert / Notification Layer

Converts ExecutionSummary outputs into structured alerts with severity,
human-readable messages, and grouped delivery. Read-only downstream layer.

Supports:
  - console print
  - structured JSON
  - webhook / email-ready flat payload
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from execution_layer import ExecutionAction, ExecutionSummary


# ─── Data Model ───────────────────────────────────────────────────────────────

@dataclass
class Alert:
    alert_id: str          # unique per session — stable key for dedup / tracking
    symbol: str
    alert_type: str        # BUY | SELL | TRIM | WATCH | HOLD | REPLACEMENT
    severity: str          # HIGH | MEDIUM | LOW
    group: str             # immediate | monitor | informational
    headline: str          # one-line human-readable summary
    detail: str            # 1-2 sentence context
    strategy: str | None
    allocation: float | None        # 0-1 fraction
    allocation_amount: float | None # dollars
    score: float | None
    confidence: float | None
    timestamp: str                  # ISO-8601 UTC
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AlertBundle:
    immediate: list[Alert] = field(default_factory=list)
    monitor: list[Alert] = field(default_factory=list)
    informational: list[Alert] = field(default_factory=list)
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def all_alerts(self) -> list[Alert]:
        return self.immediate + self.monitor + self.informational

    @property
    def has_urgent(self) -> bool:
        return any(a.severity == "HIGH" for a in self.immediate)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "has_urgent": self.has_urgent,
            "counts": {
                "immediate": len(self.immediate),
                "monitor": len(self.monitor),
                "informational": len(self.informational),
                "total": len(self.all_alerts),
            },
            "immediate": [a.to_dict() for a in self.immediate],
            "monitor": [a.to_dict() for a in self.monitor],
            "informational": [a.to_dict() for a in self.informational],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def to_webhook_payload(self) -> dict[str, Any]:
        """Minimal flat structure for webhooks, email triggers, or API calls."""
        urgent = [a for a in self.immediate if a.severity == "HIGH"]
        return {
            "generated_at": self.generated_at,
            "has_urgent": self.has_urgent,
            "urgent_count": len(urgent),
            "total_alerts": len(self.all_alerts),
            "headlines": [a.headline for a in self.all_alerts],
            "urgent_alerts": [a.to_dict() for a in urgent],
        }


# ─── Severity & Group Resolution ──────────────────────────────────────────────

_PRIORITY_TO_SEVERITY: dict[str, str] = {
    "HIGH": "HIGH",
    "MEDIUM": "MEDIUM",
    "LOW": "LOW",
}

_SEVERITY_TO_GROUP: dict[str, str] = {
    "HIGH": "immediate",
    "MEDIUM": "monitor",
    "LOW": "informational",
}

_SEV_RANK: dict[str, int] = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def _resolve_severity(action: str, priority: str) -> str:
    if action in ("SELL", "TRIM"):
        return "HIGH"
    return _PRIORITY_TO_SEVERITY.get(priority, "LOW")


# ─── Headline Templates ───────────────────────────────────────────────────────

def _format_headline(ea: ExecutionAction) -> str:
    strat = f" [{ea.strategy}]" if ea.strategy else ""
    alloc = ""
    if ea.allocation is not None:
        alloc = f"  alloc={ea.allocation * 100:.1f}%"
    elif ea.allocation_amount is not None:
        alloc = f"  alloc=${ea.allocation_amount:,.0f}"
    score = f"  score={ea.score:.0f}" if ea.score is not None else ""
    conf = f"  conf={ea.confidence:.2f}" if ea.confidence is not None else ""

    templates: dict[str, str] = {
        "BUY":   f"BUY {ea.symbol}{strat}{alloc}{score}{conf}",
        "SELL":  f"SELL {ea.symbol}{strat}  —  exit signal triggered",
        "TRIM":  f"TRIM {ea.symbol}{strat}  —  partial exit",
        "WATCH": f"WATCH {ea.symbol}{strat}  —  monitor for entry",
        "HOLD":  f"HOLD {ea.symbol}{strat}  —  no action needed",
    }
    return templates.get(ea.action, f"{ea.action} {ea.symbol}{strat}")


def _format_replacement_headline(new_sym: str, old_sym: str, strategy: str | None) -> str:
    strat = f" [{strategy}]" if strategy else ""
    return f"REPLACEMENT{strat}: {new_sym} may replace {old_sym}"


# ─── Detail Builder ───────────────────────────────────────────────────────────

def _build_detail(ea: ExecutionAction) -> str:
    return ea.reason if ea.reason else f"{ea.action} signal detected for {ea.symbol}."


# ─── Alert ID ─────────────────────────────────────────────────────────────────

def _make_alert_id(symbol: str, alert_type: str) -> str:
    return f"{symbol.upper()}_{alert_type}_{uuid.uuid4().hex[:8]}"


# ─── Deduplication ────────────────────────────────────────────────────────────

def _deduplicate(alerts: list[Alert]) -> list[Alert]:
    """
    For any (symbol, alert_type) pair that appears more than once,
    keep the highest-severity alert.
    """
    seen: dict[tuple[str, str], Alert] = {}
    for alert in alerts:
        key = (alert.symbol, alert.alert_type)
        prior = seen.get(key)
        if prior is None or _SEV_RANK[alert.severity] < _SEV_RANK[prior.severity]:
            seen[key] = alert
    return list(seen.values())


# ─── Builders ─────────────────────────────────────────────────────────────────

def _alert_from_execution_action(ea: ExecutionAction, ts: str) -> Alert:
    severity = _resolve_severity(ea.action, ea.priority)
    return Alert(
        alert_id=_make_alert_id(ea.symbol, ea.action),
        symbol=ea.symbol,
        alert_type=ea.action,
        severity=severity,
        group=_SEVERITY_TO_GROUP[severity],
        headline=_format_headline(ea),
        detail=_build_detail(ea),
        strategy=ea.strategy,
        allocation=ea.allocation,
        allocation_amount=ea.allocation_amount,
        score=ea.score,
        confidence=ea.confidence,
        timestamp=ts,
        metadata={},
    )


def _replacement_alert_from_raw(raw: dict[str, Any], ts: str) -> Alert | None:
    """
    Emit a REPLACEMENT alert when a raw PortfolioAction names a related_symbol,
    indicating this opportunity could rotate out an existing holding.
    """
    related = raw.get("related_symbol")
    symbol = str(raw.get("symbol", "")).strip()
    if not related or not symbol:
        return None

    strategy = raw.get("strategy_type")
    rationale: list[str] = raw.get("rationale") or []
    detail = rationale[0] if rationale else (
        f"{symbol} is a stronger setup and may replace {related} in the portfolio."
    )

    return Alert(
        alert_id=_make_alert_id(symbol, "REPLACEMENT"),
        symbol=symbol,
        alert_type="REPLACEMENT",
        severity="MEDIUM",
        group="monitor",
        headline=_format_replacement_headline(symbol, related, strategy),
        detail=detail,
        strategy=strategy,
        allocation=raw.get("suggested_allocation_pct"),
        allocation_amount=raw.get("suggested_allocation_amount"),
        score=raw.get("score"),
        confidence=raw.get("confidence"),
        timestamp=ts,
        metadata={"replaces": related},
    )


# ─── Public API ───────────────────────────────────────────────────────────────

def build_alert_bundle(
    summary: ExecutionSummary,
    raw_actions: list[dict[str, Any]] | None = None,
    *,
    deduplicate: bool = True,
) -> AlertBundle:
    """
    Convert an ExecutionSummary into a grouped AlertBundle.

    Args:
        summary:      Output of execution_layer.build_execution_summary().
        raw_actions:  Optional list of raw PortfolioAction dicts from
                      generate_portfolio_actions()["actions"]. Enables
                      REPLACEMENT alert detection via the related_symbol field.
        deduplicate:  When True (default), keep only the highest-severity alert
                      per (symbol, alert_type) pair.

    Usage:
        from execution_layer import build_execution_summary
        from alert_layer import build_alert_bundle, print_alert_bundle

        exec_summary = build_execution_summary(portfolio_output)
        bundle = build_alert_bundle(
            exec_summary,
            raw_actions=portfolio_output["actions"],
        )
        print_alert_bundle(bundle)
        bundle.to_json()
        bundle.to_webhook_payload()
    """
    ts = datetime.now(timezone.utc).isoformat()

    # Index raw actions by symbol for O(1) enrichment lookup
    raw_by_symbol: dict[str, dict[str, Any]] = {}
    for raw in (raw_actions or []):
        sym = str(raw.get("symbol", "")).strip()
        if sym:
            raw_by_symbol[sym] = raw

    alerts: list[Alert] = []

    for ea in summary.all_actions:
        alerts.append(_alert_from_execution_action(ea, ts))

        raw = raw_by_symbol.get(ea.symbol)
        if raw:
            replacement = _replacement_alert_from_raw(raw, ts)
            if replacement:
                alerts.append(replacement)

    if deduplicate:
        alerts = _deduplicate(alerts)

    bundle = AlertBundle(generated_at=ts)

    _type_rank: dict[str, int] = {
        "SELL": 0, "TRIM": 1, "BUY": 2, "REPLACEMENT": 3, "WATCH": 4, "HOLD": 5,
    }

    def _sort_key(a: Alert) -> tuple:
        return (_type_rank.get(a.alert_type, 9), _SEV_RANK.get(a.severity, 9), -(a.score or 0.0))

    for alert in alerts:
        if alert.group == "immediate":
            bundle.immediate.append(alert)
        elif alert.group == "monitor":
            bundle.monitor.append(alert)
        else:
            bundle.informational.append(alert)

    bundle.immediate.sort(key=_sort_key)
    bundle.monitor.sort(key=_sort_key)
    bundle.informational.sort(key=_sort_key)

    return bundle


# ─── Console Printer ──────────────────────────────────────────────────────────

_SEV_ICON: dict[str, str] = {"HIGH": "[!!!]", "MEDIUM": "[!]  ", "LOW": "[i]  "}
_WIDTH = 72


def print_alert_bundle(bundle: AlertBundle) -> None:
    """Print a human-readable alert summary to stdout."""
    div = "─" * _WIDTH

    print()
    print("=" * _WIDTH)
    print("  ALERTS")
    print("=" * _WIDTH)
    print(f"  Generated : {bundle.generated_at}")
    urgent = sum(1 for a in bundle.all_alerts if a.severity == "HIGH")
    print(f"  Urgent    : {urgent}   Total: {len(bundle.all_alerts)}")
    print()

    def _print_group(title: str, alerts: list[Alert]) -> None:
        if not alerts:
            return
        print(f"  {title}")
        print(f"  {div[:len(title) + 2]}")
        for a in alerts:
            icon = _SEV_ICON.get(a.severity, "[i]  ")
            print(f"  {icon} {a.headline}")
            print(f"         {a.detail[:108]}")
            print()

    _print_group("IMMEDIATE", bundle.immediate)
    _print_group("MONITOR", bundle.monitor)
    _print_group("INFORMATIONAL", bundle.informational)

    print(div)
    print(f"  {urgent} urgent  •  {len(bundle.all_alerts)} total alerts")
    print("=" * _WIDTH)
    print()
