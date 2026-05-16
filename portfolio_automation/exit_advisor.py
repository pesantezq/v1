"""
Exit Advisor — observe-only trailing-stop / time-stop / signal-decay layer.

Reads current holdings + recent price history + (optionally) prior decision
plan and produces a per-position advisory recommendation:

    EXIT_FULL     — drawdown from peak or hard time-stop breach
    EXIT_HALF     — moderate drawdown + signal decay
    TIGHTEN_STOP  — gains worth protecting; peak >= entry by margin
    HOLD          — within normal envelope

Output:
    outputs/latest/exit_advisor.json
    outputs/latest/exit_advisor.md

Hard guarantees:
    - observe_only=True hardcoded in every artifact.
    - Never mutates decision_plan, decision_outcomes, or any score.
    - Never writes outside LATEST namespace.
    - Robust to missing price history; degrades to status="insufficient_data".
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger("stockbot.portfolio_automation.exit_advisor")

# ---------------------------------------------------------------------------
# Constants — tuned for advisory advisory; conservative defaults
# ---------------------------------------------------------------------------

# Strategy-specific trailing-stop thresholds (drawdown from trailing peak)
_COMPOUNDER_DD_SOFT = 0.10   # 10% off peak → TIGHTEN_STOP
_COMPOUNDER_DD_HARD = 0.18   # 18% off peak → EXIT_HALF
_COMPOUNDER_DD_FULL = 0.28   # 28% off peak → EXIT_FULL

_MOMENTUM_DD_SOFT = 0.05     # 5% off peak → TIGHTEN_STOP
_MOMENTUM_DD_HARD = 0.10     # 10% off peak → EXIT_HALF
_MOMENTUM_DD_FULL = 0.18     # 18% off peak → EXIT_FULL

# Profit-protect thresholds (gain from entry that earns a TIGHTEN_STOP)
_PROFIT_PROTECT_COMPOUNDER = 0.25
_PROFIT_PROTECT_MOMENTUM = 0.12

# Time-stop: momentum positions stale > N days without a new high
_MOMENTUM_TIME_STOP_DAYS = 120

# Signal-decay: if current signal_score is below entry signal_score by this
# margin AND drawdown is non-trivial, downgrade.
_SIGNAL_DECAY_DELTA = 0.20

_DECISION_HOLD = "HOLD"
_DECISION_TIGHTEN = "TIGHTEN_STOP"
_DECISION_EXIT_HALF = "EXIT_HALF"
_DECISION_EXIT_FULL = "EXIT_FULL"

_ALL_DECISIONS = (
    _DECISION_HOLD,
    _DECISION_TIGHTEN,
    _DECISION_EXIT_HALF,
    _DECISION_EXIT_FULL,
)

# ---------------------------------------------------------------------------
# Safe helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        result = float(v)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


# ---------------------------------------------------------------------------
# Pure analysis functions
# ---------------------------------------------------------------------------


def classify_strategy(holding: dict[str, Any]) -> str:
    """
    Return "momentum" or "compounder" for a holding row.

    Leverage proxies and high-beta tech tend to act like momentum trades;
    sector ETFs and broad indices act like compounders. The default is
    compounder when in doubt — yields the gentler thresholds.
    """
    if bool(holding.get("is_leveraged")):
        return "momentum"
    asset_class = _safe_str(holding.get("asset_class")).lower()
    if "leveraged" in asset_class:
        return "momentum"
    explicit = _safe_str(holding.get("strategy_type")).lower()
    if explicit in ("momentum", "compounder"):
        return explicit
    return "compounder"


def _strategy_thresholds(strategy: str) -> dict[str, float]:
    if strategy == "momentum":
        return {
            "dd_soft": _MOMENTUM_DD_SOFT,
            "dd_hard": _MOMENTUM_DD_HARD,
            "dd_full": _MOMENTUM_DD_FULL,
            "profit_protect": _PROFIT_PROTECT_MOMENTUM,
            "time_stop_days": _MOMENTUM_TIME_STOP_DAYS,
        }
    return {
        "dd_soft": _COMPOUNDER_DD_SOFT,
        "dd_hard": _COMPOUNDER_DD_HARD,
        "dd_full": _COMPOUNDER_DD_FULL,
        "profit_protect": _PROFIT_PROTECT_COMPOUNDER,
        "time_stop_days": None,  # compounders have no hard time-stop
    }


def evaluate_position(
    *,
    symbol: str,
    strategy: str,
    current_price: float | None,
    peak_price: float | None,
    entry_price: float | None = None,
    days_held: int | None = None,
    entry_signal_score: float | None = None,
    current_signal_score: float | None = None,
) -> dict[str, Any]:
    """
    Produce a single exit-advice row.

    All inputs are nullable so the caller can pass whatever it actually has.
    When inputs are missing the row reports status="insufficient_data" and
    decision="HOLD" — never an EXIT decision on incomplete data.
    """
    th = _strategy_thresholds(strategy)
    triggers: list[str] = []
    reasons: list[str] = []
    decision = _DECISION_HOLD
    status = "ok"

    if current_price is None or peak_price is None or peak_price <= 0:
        return {
            "symbol": symbol,
            "strategy": strategy,
            "decision": _DECISION_HOLD,
            "status": "insufficient_data",
            "triggers": [],
            "reasons": ["price history unavailable"],
            "drawdown_from_peak": None,
            "gain_from_entry": None,
            "days_held": days_held,
            "thresholds": th,
        }

    drawdown_from_peak = round((peak_price - current_price) / peak_price, 4)
    gain_from_entry = None
    if entry_price is not None and entry_price > 0:
        gain_from_entry = round((current_price - entry_price) / entry_price, 4)

    # --- Drawdown ladder ---------------------------------------------------
    if drawdown_from_peak >= th["dd_full"]:
        decision = _DECISION_EXIT_FULL
        triggers.append("drawdown_full")
        reasons.append(
            f"drawdown {drawdown_from_peak:.1%} >= full threshold {th['dd_full']:.0%}"
        )
    elif drawdown_from_peak >= th["dd_hard"]:
        decision = _DECISION_EXIT_HALF
        triggers.append("drawdown_hard")
        reasons.append(
            f"drawdown {drawdown_from_peak:.1%} >= half-exit threshold {th['dd_hard']:.0%}"
        )
    elif drawdown_from_peak >= th["dd_soft"]:
        decision = _DECISION_TIGHTEN
        triggers.append("drawdown_soft")
        reasons.append(
            f"drawdown {drawdown_from_peak:.1%} >= tighten threshold {th['dd_soft']:.0%}"
        )

    # --- Profit-protect: gains worth defending earn at minimum a TIGHTEN ---
    if (
        gain_from_entry is not None
        and gain_from_entry >= th["profit_protect"]
        and decision == _DECISION_HOLD
    ):
        decision = _DECISION_TIGHTEN
        triggers.append("profit_protect")
        reasons.append(
            f"gain {gain_from_entry:.1%} >= profit-protect threshold "
            f"{th['profit_protect']:.0%}"
        )

    # --- Time-stop for momentum -------------------------------------------
    if (
        th["time_stop_days"] is not None
        and days_held is not None
        and days_held > th["time_stop_days"]
        and decision in (_DECISION_HOLD, _DECISION_TIGHTEN)
    ):
        triggers.append("time_stop")
        reasons.append(
            f"momentum position held {days_held}d > {th['time_stop_days']}d cap"
        )
        if decision == _DECISION_HOLD:
            decision = _DECISION_TIGHTEN

    # --- Signal decay ------------------------------------------------------
    if (
        entry_signal_score is not None
        and current_signal_score is not None
    ):
        delta = entry_signal_score - current_signal_score
        if delta >= _SIGNAL_DECAY_DELTA and drawdown_from_peak >= th["dd_soft"]:
            triggers.append("signal_decay")
            reasons.append(
                f"signal_score dropped {delta:.2f} from entry while in drawdown"
            )
            if decision == _DECISION_TIGHTEN:
                decision = _DECISION_EXIT_HALF

    return {
        "symbol": symbol,
        "strategy": strategy,
        "decision": decision,
        "status": status,
        "triggers": triggers,
        "reasons": reasons,
        "drawdown_from_peak": drawdown_from_peak,
        "gain_from_entry": gain_from_entry,
        "days_held": days_held,
        "thresholds": th,
    }


# ---------------------------------------------------------------------------
# Plan-level summary
# ---------------------------------------------------------------------------


def build_plan(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap evaluation rows in the canonical plan envelope."""
    counts = {d: 0 for d in _ALL_DECISIONS}
    for row in rows:
        d = row.get("decision", _DECISION_HOLD)
        counts[d] = counts.get(d, 0) + 1
    summary_line = (
        f"Exit advisor: {counts.get(_DECISION_EXIT_FULL, 0)} full-exit, "
        f"{counts.get(_DECISION_EXIT_HALF, 0)} half-exit, "
        f"{counts.get(_DECISION_TIGHTEN, 0)} tighten-stop, "
        f"{counts.get(_DECISION_HOLD, 0)} hold"
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "schema_version": "1",
        "summary_line": summary_line,
        "counts": counts,
        "positions": rows,
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_markdown(plan: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Exit Advisor")
    lines.append("")
    lines.append(f"_Generated: {plan.get('generated_at')}_")
    lines.append("")
    lines.append("Observe-only. No trades are executed.")
    lines.append("")
    lines.append(plan.get("summary_line", ""))
    lines.append("")
    lines.append("## Positions")
    lines.append("")
    lines.append("| Symbol | Strategy | Decision | DD from peak | Gain from entry | Triggers |")
    lines.append("|---|---|---|---|---|---|")
    for row in plan.get("positions", []):
        dd = row.get("drawdown_from_peak")
        gn = row.get("gain_from_entry")
        lines.append(
            "| {sym} | {strat} | {dec} | {dd} | {gn} | {trg} |".format(
                sym=row.get("symbol", "?"),
                strat=row.get("strategy", "?"),
                dec=row.get("decision", "?"),
                dd=f"{dd:.1%}" if isinstance(dd, (int, float)) else "—",
                gn=f"{gn:+.1%}" if isinstance(gn, (int, float)) else "—",
                trg=", ".join(row.get("triggers") or []) or "—",
            )
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


def _load_json_safe(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return {}


def _load_holdings_from_config(repo_root: Path) -> list[dict[str, Any]]:
    cfg_path = repo_root / "config.json"
    cfg = _load_json_safe(cfg_path)
    portfolio = cfg.get("portfolio") or {}
    raw = portfolio.get("holdings") or []
    holdings = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        symbol = _safe_str(row.get("symbol")).upper()
        shares = _safe_float(row.get("shares"))
        if not symbol or shares is None or shares <= 0:
            continue
        holdings.append(row)
    return holdings


def _peak_and_current_from_history(rows: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    """
    Given a list of FMP historical-price rows (newest-first), return
    (peak_close_in_window, latest_close).
    """
    if not rows:
        return None, None
    closes: list[float] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        c = _safe_float(r.get("adjClose")) or _safe_float(r.get("close"))
        if c is not None and c > 0:
            closes.append(c)
    if not closes:
        return None, None
    latest = closes[0]  # FMP returns newest-first
    peak = max(closes)
    return peak, latest


def run_exit_advisor(
    repo_root: Path | str,
    *,
    fmp_client: Any | None = None,
    lookback_days: int = 252,
    base_dir: Path | str = "outputs",
) -> dict[str, Any]:
    """
    Read holdings from config.json, fetch (or load cached) historical prices
    via the provided FMPClient, evaluate every position, write artifacts.

    When *fmp_client* is None, the function still runs but every position
    will report status="insufficient_data". This is intentional so the
    layer is safe to integrate before all data hooks are wired.
    """
    repo_root = Path(repo_root)
    base_dir = Path(base_dir)

    holdings = _load_holdings_from_config(repo_root)

    rows: list[dict[str, Any]] = []
    for h in holdings:
        symbol = _safe_str(h.get("symbol")).upper()
        strategy = classify_strategy(h)

        peak_price: float | None = None
        current_price: float | None = None

        if fmp_client is not None:
            try:
                # Use existing cached endpoint; no new endpoint, no compliance
                # change. The TTL defaults already protect against budget burn.
                hist = fmp_client.get_historical_prices(
                    symbol, years=max(1, lookback_days // 252), ttl_days=1
                )
                if hist:
                    peak_price, current_price = _peak_and_current_from_history(
                        hist[:lookback_days]
                    )
            except Exception as exc:
                logger.debug(
                    "exit_advisor: price fetch failed for %s (non-fatal): %s",
                    symbol, exc,
                )

        row = evaluate_position(
            symbol=symbol,
            strategy=strategy,
            current_price=current_price,
            peak_price=peak_price,
        )
        rows.append(row)

    plan = build_plan(rows)

    try:
        safe_write_json(OutputNamespace.LATEST, "exit_advisor.json", plan, base_dir=base_dir)
        safe_write_text(
            OutputNamespace.LATEST,
            "exit_advisor.md",
            _render_markdown(plan),
            base_dir=base_dir,
        )
    except Exception as exc:
        logger.warning("exit_advisor: failed to write artifacts (non-fatal): %s", exc)

    return plan
