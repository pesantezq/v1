"""
Earnings Calendar Gate — observe-only risk-window flagger.

Surfaces positions within N calendar days of earnings so the operator can
manually decide whether to trim, hedge, or hold through the event. Buying
into earnings is a known asymmetric-risk pattern; this advisor names which
positions are in that window.

Inputs (read-only):
  - config.json portfolio.holdings (symbols)
  - An earnings_lookup callable (optionally injected). Default is None,
    in which case every position reports status="no_earnings_source".

Why injection rather than a direct FMP call:
  FMP compliance is governed by a registry (docs/FMP_COMPLIANCE.md).
  Adding a new endpoint requires a separate compliance review. The gate
  is designed to ship today as a framework + tests; once an
  fmp_client.get_earnings_calendar() method exists, the caller injects it
  via the earnings_lookup parameter.

Outputs (LATEST namespace):
  - outputs/latest/earnings_gate.json
  - outputs/latest/earnings_gate.md

Hard guarantees:
  - observe_only=True hardcoded.
  - Never raises into the pipeline (caller wraps in try/except per pattern).
  - Never recommends a SELL or any structural action — only "review" flags.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger("stockbot.portfolio_automation.earnings_gate")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CRITICAL_WINDOW_DAYS = 5      # within this many days → "review_before_earnings"
_WARNING_WINDOW_DAYS = 15      # within this → "earnings_approaching"
_RECENT_WINDOW_DAYS = 3        # earnings in the past N days → "post_earnings_review"


_GATE_HOLD = "HOLD"
_GATE_REVIEW = "REVIEW_BEFORE_EARNINGS"
_GATE_APPROACHING = "EARNINGS_APPROACHING"
_GATE_POST = "POST_EARNINGS_REVIEW"
_ALL_GATES = (_GATE_HOLD, _GATE_REVIEW, _GATE_APPROACHING, _GATE_POST)

# Earnings lookup callable signature:
#     lookup(symbol: str) -> dict | None
# Expected dict shape:
#     {"symbol": str, "earnings_date": "YYYY-MM-DD", "time": "amc"|"bmo"|None}
EarningsLookup = Callable[[str], dict[str, Any] | None]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


def _parse_date(raw: Any) -> date | None:
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, str):
        s = raw.strip()
        if len(s) >= 10:
            try:
                return datetime.strptime(s[:10], "%Y-%m-%d").date()
            except ValueError:
                pass
    return None


def _load_json_safe(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------


def classify_window(days_until: int) -> str:
    """
    Map days-until-earnings (negative = past) to a gate label.

    Examples:
        25 days  → HOLD
        12 days  → EARNINGS_APPROACHING
         3 days  → REVIEW_BEFORE_EARNINGS
         0 days  → REVIEW_BEFORE_EARNINGS (day of)
        -2 days  → POST_EARNINGS_REVIEW
        -7 days  → HOLD
    """
    if days_until < 0:
        return _GATE_POST if abs(days_until) <= _RECENT_WINDOW_DAYS else _GATE_HOLD
    if days_until <= _CRITICAL_WINDOW_DAYS:
        return _GATE_REVIEW
    if days_until <= _WARNING_WINDOW_DAYS:
        return _GATE_APPROACHING
    return _GATE_HOLD


def evaluate_position(
    *,
    symbol: str,
    earnings_data: dict[str, Any] | None,
    today: date | None = None,
) -> dict[str, Any]:
    """
    Return a row describing this position's earnings-window status.

    When *earnings_data* is None the row reports status="no_earnings_source"
    and gate=HOLD — never a REVIEW recommendation on missing data.
    """
    today = today or date.today()
    if not earnings_data:
        return {
            "symbol": symbol,
            "gate": _GATE_HOLD,
            "status": "no_earnings_source",
            "earnings_date": None,
            "days_until": None,
            "reasons": [],
        }

    earnings_date = _parse_date(earnings_data.get("earnings_date"))
    if earnings_date is None:
        return {
            "symbol": symbol,
            "gate": _GATE_HOLD,
            "status": "unparseable_date",
            "earnings_date": _safe_str(earnings_data.get("earnings_date")),
            "days_until": None,
            "reasons": ["earnings_date could not be parsed"],
        }

    days_until = (earnings_date - today).days
    gate = classify_window(days_until)
    reasons: list[str] = []
    if gate == _GATE_REVIEW:
        reasons.append(
            f"earnings in {days_until} day(s) — review position size before close"
        )
    elif gate == _GATE_APPROACHING:
        reasons.append(
            f"earnings in {days_until} day(s) — plan trim/hedge if applicable"
        )
    elif gate == _GATE_POST:
        reasons.append(
            f"earnings {abs(days_until)} day(s) ago — review thesis and reaction"
        )

    return {
        "symbol": symbol,
        "gate": gate,
        "status": "ok",
        "earnings_date": earnings_date.isoformat(),
        "earnings_time": _safe_str(earnings_data.get("time")) or None,
        "days_until": days_until,
        "reasons": reasons,
    }


def build_plan(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {g: 0 for g in _ALL_GATES}
    for r in rows:
        g = r.get("gate", _GATE_HOLD)
        counts[g] = counts.get(g, 0) + 1
    summary_line = (
        f"Earnings gate: {counts.get(_GATE_REVIEW, 0)} review, "
        f"{counts.get(_GATE_APPROACHING, 0)} approaching, "
        f"{counts.get(_GATE_POST, 0)} post-earnings, "
        f"{counts.get(_GATE_HOLD, 0)} hold"
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "schema_version": "1",
        "counts": counts,
        "summary_line": summary_line,
        "positions": rows,
        "thresholds": {
            "critical_window_days": _CRITICAL_WINDOW_DAYS,
            "warning_window_days": _WARNING_WINDOW_DAYS,
            "recent_window_days": _RECENT_WINDOW_DAYS,
        },
    }


def _render_markdown(plan: dict[str, Any]) -> str:
    lines: list[str] = [
        "# Earnings Gate",
        "",
        f"_Generated: {plan.get('generated_at')}_",
        "",
        "Observe-only. No trades are executed.",
        "",
        plan.get("summary_line", ""),
        "",
        "## Positions",
        "",
        "| Symbol | Gate | Earnings date | Days until | Note |",
        "|---|---|---|---|---|",
    ]
    for r in plan.get("positions", []):
        lines.append("| {sym} | {gate} | {ed} | {du} | {note} |".format(
            sym=r.get("symbol", "?"),
            gate=r.get("gate", "?"),
            ed=r.get("earnings_date") or "—",
            du=(str(r.get("days_until")) if r.get("days_until") is not None else "—"),
            note=(r.get("reasons") or [""])[0],
        ))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


def _holdings_symbols(repo_root: Path) -> list[str]:
    cfg = _load_json_safe(repo_root / "config.json")
    portfolio = cfg.get("portfolio") or {}
    symbols: list[str] = []
    for h in portfolio.get("holdings") or []:
        if not isinstance(h, dict):
            continue
        sym = _safe_str(h.get("symbol")).upper()
        shares = _safe_float(h.get("shares"))
        if sym and shares is not None and shares > 0:
            symbols.append(sym)
    return symbols


def run_earnings_gate(
    repo_root: Path | str,
    *,
    earnings_lookup: EarningsLookup | None = None,
    base_dir: Path | str = "outputs",
    today: date | None = None,
) -> dict[str, Any]:
    """
    Evaluate earnings windows for every active holding.

    Pass *earnings_lookup* with signature ``lookup(symbol) -> dict | None`` to
    enable real evaluation. Without it every position reports
    status="no_earnings_source" and gate=HOLD — safe and informative
    fallback.
    """
    repo_root = Path(repo_root)
    base_dir = Path(base_dir)
    today = today or date.today()

    symbols = _holdings_symbols(repo_root)
    rows: list[dict[str, Any]] = []
    for sym in symbols:
        data = None
        if earnings_lookup is not None:
            try:
                data = earnings_lookup(sym)
            except Exception as exc:
                logger.debug(
                    "earnings_gate: lookup failed for %s (non-fatal): %s",
                    sym, exc,
                )
                data = None
        rows.append(evaluate_position(symbol=sym, earnings_data=data, today=today))

    plan = build_plan(rows)
    try:
        safe_write_json(OutputNamespace.LATEST, "earnings_gate.json", plan, base_dir=base_dir)
        safe_write_text(
            OutputNamespace.LATEST, "earnings_gate.md",
            _render_markdown(plan), base_dir=base_dir,
        )
    except Exception as exc:
        logger.warning("earnings_gate: failed to write artifacts (non-fatal): %s", exc)
    return plan
