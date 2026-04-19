"""
Execution / Action Layer

Converts portfolio_decision_engine outputs into clean, actionable summaries.
Read-only downstream layer — does not modify any decision logic.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, dataclass, field
from typing import Any


# ─── Data Model ───────────────────────────────────────────────────────────────

@dataclass
class ExecutionAction:
    symbol: str
    action: str              # BUY | SELL | TRIM | HOLD | WATCH
    priority: str            # HIGH | MEDIUM | LOW
    group: str               # immediate | watchlist | conditional
    strategy: str | None
    allocation: float | None       # 0–1 fraction of portfolio
    allocation_amount: float | None  # dollar amount
    reason: str              # 1–2 sentence rationale
    score: float | None
    confidence: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionSummary:
    immediate: list[ExecutionAction] = field(default_factory=list)
    watchlist: list[ExecutionAction] = field(default_factory=list)
    conditional: list[ExecutionAction] = field(default_factory=list)
    summary_line: str = ""

    @property
    def all_actions(self) -> list[ExecutionAction]:
        return self.immediate + self.watchlist + self.conditional

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary_line": self.summary_line,
            "immediate": [a.to_dict() for a in self.immediate],
            "watchlist": [a.to_dict() for a in self.watchlist],
            "conditional": [a.to_dict() for a in self.conditional],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def to_csv(self) -> str:
        output = io.StringIO()
        fields = [
            "group", "priority", "symbol", "action", "strategy",
            "allocation", "allocation_amount", "score", "confidence", "reason",
        ]
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for action in self.all_actions:
            writer.writerow(action.to_dict())
        return output.getvalue()


# ─── Priority Logic ───────────────────────────────────────────────────────────

_EXIT_ACTIONS = {"SELL", "TRIM"}
_HIGH_SCORE = 72.0
_HIGH_CONF = 0.75
_MED_SCORE = 55.0
_MED_CONF = 0.60


def _assign_priority(
    action: str,
    score: float | None,
    confidence: float | None,
    strategy: str | None,  # noqa: ARG001 — reserved for future strategy-based rules
) -> str:
    if action in _EXIT_ACTIONS:
        return "HIGH"

    s = score or 0.0
    c = confidence or 0.0

    if action in ("BUY", "PROMOTE_TO_PORTFOLIO"):
        if s >= _HIGH_SCORE and c >= _HIGH_CONF:
            return "HIGH"
        if s >= _MED_SCORE and c >= _MED_CONF:
            return "MEDIUM"
        return "LOW"

    return "LOW"


# ─── Action Normalisation ─────────────────────────────────────────────────────

_ACTION_MAP: dict[str, str] = {
    "BUY": "BUY",
    "PROMOTE_TO_PORTFOLIO": "BUY",
    "SELL": "SELL",
    "TRIM": "TRIM",
    "HOLD": "HOLD",
    "ADD_TO_WATCHLIST": "WATCH",
}


def _normalise_action(raw: str) -> str:
    return _ACTION_MAP.get(raw.upper(), raw.upper())


# ─── Grouping ─────────────────────────────────────────────────────────────────

def _assign_group(action: str, priority: str) -> str:
    if action in ("SELL", "TRIM"):
        return "immediate"
    if action == "BUY" and priority in ("HIGH", "MEDIUM"):
        return "immediate"
    if action == "WATCH":
        return "watchlist"
    return "conditional"


# ─── Reason Builder ───────────────────────────────────────────────────────────

def _build_reason(raw: dict[str, Any]) -> str:
    rationale: list[str] = raw.get("rationale") or []
    if rationale:
        return ". ".join(str(r) for r in rationale[:2])
    # Fall back to action string so reason is never empty
    return str(raw.get("action", "No details available"))


# ─── Core Builder ─────────────────────────────────────────────────────────────

def _build_execution_action(raw: dict[str, Any]) -> ExecutionAction:
    raw_action_str = str(raw.get("action", "HOLD"))
    action = _normalise_action(raw_action_str)
    score = raw.get("score")
    confidence = raw.get("confidence")
    strategy = raw.get("strategy_type")

    priority = _assign_priority(raw_action_str, score, confidence, strategy)
    group = _assign_group(action, priority)

    return ExecutionAction(
        symbol=str(raw.get("symbol", "UNKNOWN")),
        action=action,
        priority=priority,
        group=group,
        strategy=strategy,
        allocation=raw.get("suggested_allocation_pct"),
        allocation_amount=raw.get("suggested_allocation_amount"),
        reason=_build_reason(raw),
        score=score,
        confidence=confidence,
    )


# ─── Public API ───────────────────────────────────────────────────────────────

def build_execution_summary(portfolio_output: dict[str, Any]) -> ExecutionSummary:
    """
    Convert generate_portfolio_actions() output into a clean ExecutionSummary.

    Usage:
        from portfolio_decision_engine import generate_portfolio_actions
        from execution_layer import build_execution_summary, print_execution_summary

        result = generate_portfolio_actions(...)
        summary = build_execution_summary(result)
        print_execution_summary(summary)
    """
    summary = ExecutionSummary(summary_line=portfolio_output.get("summary_line", ""))

    for raw in portfolio_output.get("actions", []):
        ea = _build_execution_action(raw)
        if ea.group == "immediate":
            summary.immediate.append(ea)
        elif ea.group == "watchlist":
            summary.watchlist.append(ea)
        else:
            summary.conditional.append(ea)

    _priority_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    _action_rank = {"SELL": 0, "TRIM": 1, "BUY": 2, "HOLD": 3, "WATCH": 4}

    def _sort_key(ea: ExecutionAction) -> tuple:
        return (
            _action_rank.get(ea.action, 9),
            _priority_rank.get(ea.priority, 9),
            -(ea.score or 0.0),
        )

    summary.immediate.sort(key=_sort_key)
    summary.watchlist.sort(key=_sort_key)
    summary.conditional.sort(key=_sort_key)

    return summary


# ─── Console Printer ──────────────────────────────────────────────────────────

_PRIORITY_ICON = {"HIGH": "[!]", "MEDIUM": "[~]", "LOW": "[ ]"}
_WIDTH = 72


def print_execution_summary(summary: ExecutionSummary) -> None:
    """Print a human-readable execution summary to stdout."""
    div = "─" * _WIDTH

    print()
    print("=" * _WIDTH)
    print("  EXECUTION SUMMARY  —  What should I do right now?")
    print("=" * _WIDTH)
    if summary.summary_line:
        print(f"  {summary.summary_line}")
    print()

    def _print_group(title: str, actions: list[ExecutionAction]) -> None:
        if not actions:
            return
        print(f"  {title}")
        print(f"  {div[:len(title) + 2]}")
        for ea in actions:
            icon = _PRIORITY_ICON.get(ea.priority, "[ ]")
            strat = f" [{ea.strategy}]" if ea.strategy else ""
            alloc = ""
            if ea.allocation is not None:
                alloc = f"  alloc={ea.allocation * 100:.1f}%"
            elif ea.allocation_amount is not None:
                alloc = f"  alloc=${ea.allocation_amount:,.0f}"
            conf = f"  conf={ea.confidence:.2f}" if ea.confidence is not None else ""
            score = f"  score={ea.score:.0f}" if ea.score is not None else ""

            print(f"  {icon} {ea.symbol:<8} {ea.action:<5}{strat:<13}{alloc}{conf}{score}")
            print(f"       {ea.reason[:110]}")
            print()

    _print_group("IMMEDIATE ACTIONS  (execute now)", summary.immediate)
    _print_group("WATCHLIST / MONITOR", summary.watchlist)
    _print_group("CONDITIONAL / WAITING", summary.conditional)

    total = len(summary.all_actions)
    imm = len(summary.immediate)
    print(f"  {div}")
    print(f"  {imm} immediate  •  {total} total decisions")
    print("=" * _WIDTH)
    print()
