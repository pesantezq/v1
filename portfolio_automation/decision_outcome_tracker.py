from __future__ import annotations

import json
import logging
import os
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Callable

from portfolio_automation.env import get_secret

logger = logging.getLogger("stockbot.portfolio_automation.decision_outcome_tracker")

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

DECISION_PLAN_RELATIVE_PATH = ("outputs", "latest", "decision_plan.json")
AI_VALIDATION_RELATIVE_PATH = ("outputs", "latest", "ai_decision_validation.json")
WATCHLIST_SIGNALS_RELATIVE_PATH = ("outputs", "latest", "watchlist_signals.json")
OUTCOMES_JSONL_RELATIVE_PATH = ("outputs", "policy", "decision_outcomes.jsonl")
SUMMARY_JSON_RELATIVE_PATH = ("outputs", "policy", "decision_outcome_summary.json")
SUMMARY_MD_RELATIVE_PATH = ("outputs", "policy", "decision_outcome_summary.md")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WAIT_CORRECT_THRESHOLD = 0.03   # abs(return) < 3 % = WAIT was correct
DEFAULT_LOOKBACK_DAYS = (1, 3, 7)
MAX_SNAPSHOT_DECISIONS = 10     # max decisions captured per daily run
MAX_HISTORY_ROWS = 500          # cap JSONL file size

_DECISIONS_THAT_WANT_DOWN = frozenset({"SELL", "AVOID"})
_DECISIONS_THAT_WANT_UP = frozenset({"BUY", "SCALE"})
_DECISIONS_NEUTRAL = frozenset({"HOLD"})

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _safe_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _safe_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        result = float(v)
        return result if result == result else None  # exclude NaN
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# JSONL I/O
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except json.JSONDecodeError:
                pass
    except OSError:
        pass
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, default=str) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def _safe_json_load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------


def _extract_price_map(watchlist_signals: dict[str, Any]) -> dict[str, float]:
    """Build {symbol: price} from watchlist_signals.json results."""
    price_map: dict[str, float] = {}
    for row in _safe_list(watchlist_signals.get("results")):
        sym = _safe_str(row.get("symbol") or row.get("ticker"))
        if not sym:
            continue
        price = _safe_float(
            row.get("price")
            or row.get("last_price")
            or row.get("current_price")
            or row.get("fmp_price")
        )
        if price is not None and price > 0:
            price_map[sym.upper()] = price
    return price_map


def _try_build_price_fetcher() -> Callable[[list[str]], dict[str, float]] | None:
    """
    Try to build an FMP-backed price fetcher from environment variables.
    Returns None if FMP_API_KEY is absent or the client cannot be created.
    """
    if not get_secret("FMP_API_KEY"):
        return None
    try:
        from fmp_client import FMPClient

        client = FMPClient()

        def _fetcher(symbols: list[str]) -> dict[str, float]:
            if not symbols:
                return {}
            quotes = client.get_batch_quotes(symbols, ttl_hours=1) or {}
            result: dict[str, float] = {}
            for sym, q in quotes.items():
                p = _safe_float((q or {}).get("price"))
                if p is not None and p > 0:
                    result[sym.upper()] = p
            return result

        return _fetcher
    except Exception as exc:
        logger.debug("Could not build FMP price fetcher: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Correctness logic
# ---------------------------------------------------------------------------


def _is_direction_correct(
    decision: str,
    return_pct: float,
    *,
    wait_threshold: float = WAIT_CORRECT_THRESHOLD,
) -> bool | None:
    """
    Return True/False for directional decisions, None for neutral (HOLD).
    SELL/AVOID: correct when price drops.
    BUY/SCALE:  correct when price rises.
    WAIT:       correct when abs(move) < threshold.
    HOLD:       neutral — not counted.
    """
    d = decision.upper()
    if d in _DECISIONS_THAT_WANT_DOWN:
        return return_pct < 0
    if d in _DECISIONS_THAT_WANT_UP:
        return return_pct > 0
    if d == "WAIT":
        return abs(return_pct) < wait_threshold
    if d in _DECISIONS_NEUTRAL:
        return None
    return None


# ---------------------------------------------------------------------------
# Validation status lookup
# ---------------------------------------------------------------------------


def _get_validation_status(
    symbol: str, decision: str, ai_validation: dict[str, Any]
) -> str:
    sym_up = symbol.upper()
    dec_up = decision.upper()
    for v in _safe_list(ai_validation.get("validations")):
        if (
            _safe_str(v.get("symbol")).upper() == sym_up
            and _safe_str(v.get("decision")).upper() == dec_up
        ):
            return _safe_str(v.get("validation_status")) or "unknown"
    return "unknown"


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _make_snapshot_row(
    run_id: str,
    date_str: str,
    decision_row: dict[str, Any],
    validation_status: str,
    price_at_decision: float | None,
) -> dict[str, Any]:
    structured = _safe_dict(decision_row.get("decision_reason_structured"))
    return {
        "run_id": run_id,
        "date": date_str,
        "symbol": _safe_str(decision_row.get("symbol") or "UNKNOWN"),
        "decision": _safe_str(decision_row.get("decision") or "UNKNOWN").upper(),
        "priority": _safe_float(decision_row.get("priority")),
        "source": _safe_str(decision_row.get("source") or "unknown"),
        "strategy": _safe_str(structured.get("strategy") or ""),
        "band": _safe_str(structured.get("band") or ""),
        "confidence": _safe_float(decision_row.get("confidence")),
        "validation_status": validation_status,
        "price_at_decision": price_at_decision,
        "timestamp": datetime.now().isoformat(),
        "resolved": False,
        "resolved_at": None,
        "days_elapsed": None,
        "price_at_resolution": None,
        "return_pct": None,
        "direction_correct": None,
    }


# ---------------------------------------------------------------------------
# Step 1 — Snapshot decisions
# ---------------------------------------------------------------------------


def snapshot_decisions(
    root: Path,
    decision_plan: dict[str, Any],
    ai_validation: dict[str, Any],
    *,
    price_snapshot: dict[str, float] | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Append today's decisions to decision_outcomes.jsonl.

    Idempotent: if a row with the same run_id already exists, the run is
    skipped and the existing rows are returned unchanged.
    """
    today = date.today()
    effective_run_id = run_id or f"{today.isoformat()}_daily"
    date_str = today.isoformat()

    decisions = _safe_list(decision_plan.get("decisions"))
    if not decisions:
        logger.debug("OUTCOME TRACKER: no decisions in plan — snapshot skipped")
        return []

    jsonl_path = root.joinpath(*OUTCOMES_JSONL_RELATIVE_PATH)
    existing = _load_jsonl(jsonl_path)

    # Idempotency: skip if already snapshotted for this run_id
    if any(r.get("run_id") == effective_run_id for r in existing):
        logger.debug("OUTCOME TRACKER: run_id %s already snapshotted", effective_run_id)
        return []

    prices = price_snapshot or {}
    new_rows: list[dict[str, Any]] = []

    for row in decisions[:MAX_SNAPSHOT_DECISIONS]:
        symbol = _safe_str(row.get("symbol") or "UNKNOWN").upper()
        decision = _safe_str(row.get("decision") or "UNKNOWN").upper()
        validation_status = _get_validation_status(symbol, decision, ai_validation)
        price_at_decision = prices.get(symbol)
        new_rows.append(
            _make_snapshot_row(
                effective_run_id, date_str, row, validation_status, price_at_decision
            )
        )

    combined = (existing + new_rows)[-MAX_HISTORY_ROWS:]
    _write_jsonl(jsonl_path, combined)

    logger.debug(
        "OUTCOME TRACKER: snapshotted %d decisions for run_id=%s",
        len(new_rows),
        effective_run_id,
    )
    return new_rows


# ---------------------------------------------------------------------------
# Step 2 — Resolve outcomes
# ---------------------------------------------------------------------------


def resolve_outcomes(
    root: Path,
    *,
    price_fetcher: Callable[[list[str]], dict[str, float]] | None = None,
    lookback_days: tuple[int, ...] = DEFAULT_LOOKBACK_DAYS,
    wait_threshold: float = WAIT_CORRECT_THRESHOLD,
) -> list[dict[str, Any]]:
    """
    Update unresolved rows with current prices and compute return metrics.

    Only resolves rows where:
    - resolved is False
    - price_at_decision is not None
    - at least min(lookback_days) calendar days have elapsed

    Returns the full updated row list.
    """
    if price_fetcher is None:
        logger.debug("OUTCOME TRACKER: no price fetcher — outcome resolution skipped")
        jsonl_path = root.joinpath(*OUTCOMES_JSONL_RELATIVE_PATH)
        return _load_jsonl(jsonl_path)

    jsonl_path = root.joinpath(*OUTCOMES_JSONL_RELATIVE_PATH)
    rows = _load_jsonl(jsonl_path)

    today = date.today()
    min_days = min(lookback_days) if lookback_days else 1

    # Collect symbols that need resolution
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if row.get("resolved"):
            continue
        if row.get("price_at_decision") is None:
            continue
        try:
            decision_date = date.fromisoformat(str(row["date"]))
        except (KeyError, ValueError):
            continue
        days_elapsed = (today - decision_date).days
        if days_elapsed < min_days:
            continue
        row["_days_elapsed"] = days_elapsed
        candidates.append(row)

    if not candidates:
        return rows

    symbols = list({r["symbol"].upper() for r in candidates})
    try:
        current_prices = price_fetcher(symbols)
    except Exception as exc:
        logger.warning("OUTCOME TRACKER: price fetch failed — %s", exc)
        return rows

    now_iso = datetime.now().isoformat()
    changed = 0

    for row in candidates:
        sym = row["symbol"].upper()
        current_price = current_prices.get(sym)
        if current_price is None:
            continue

        days_elapsed = row.pop("_days_elapsed")
        price_at = _safe_float(row["price_at_decision"])
        if price_at is None or price_at <= 0:
            continue

        return_pct = (current_price - price_at) / price_at
        direction_correct = _is_direction_correct(
            row["decision"], return_pct, wait_threshold=wait_threshold
        )

        row["resolved"] = True
        row["resolved_at"] = now_iso
        row["days_elapsed"] = days_elapsed
        row["price_at_resolution"] = current_price
        row["return_pct"] = return_pct
        row["direction_correct"] = direction_correct
        changed += 1

    if changed:
        _write_jsonl(jsonl_path, rows)
        logger.debug("OUTCOME TRACKER: resolved %d rows", changed)

    return rows


# ---------------------------------------------------------------------------
# Step 3 — Aggregate metrics
# ---------------------------------------------------------------------------


def _group_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute count/resolved/hit_rate/avg_return for a slice of rows."""
    resolved = [r for r in rows if r.get("resolved")]
    judgeable = [r for r in resolved if r.get("direction_correct") is not None]
    correct = [r for r in judgeable if r.get("direction_correct")]
    returns = [r["return_pct"] for r in resolved if r.get("return_pct") is not None]
    return {
        "count": len(rows),
        "resolved": len(resolved),
        "correct": len(correct),
        "hit_rate": len(correct) / len(judgeable) if judgeable else None,
        "avg_return_pct": sum(returns) / len(returns) if returns else None,
    }


def aggregate_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute summary statistics from all history rows."""
    overall = _group_stats(rows)

    # Group by decision type
    by_decision: dict[str, list] = {}
    for row in rows:
        key = _safe_str(row.get("decision") or "UNKNOWN").upper()
        by_decision.setdefault(key, []).append(row)

    # Group by validation_status
    by_validation: dict[str, list] = {}
    for row in rows:
        key = _safe_str(row.get("validation_status") or "unknown")
        by_validation.setdefault(key, []).append(row)

    resolved_rows = [r for r in rows if r.get("resolved")]
    last_10 = sorted(
        resolved_rows,
        key=lambda r: r.get("resolved_at") or r.get("date") or "",
        reverse=True,
    )[:10]

    rows_with_return = [r for r in resolved_rows if r.get("return_pct") is not None]
    best = (
        max(rows_with_return, key=lambda r: r["return_pct"])
        if rows_with_return
        else None
    )
    worst = (
        min(rows_with_return, key=lambda r: r["return_pct"])
        if rows_with_return
        else None
    )

    return {
        "generated_at": datetime.now().isoformat(),
        "total_decisions": overall["count"],
        "resolved": overall["resolved"],
        "unresolved": overall["count"] - overall["resolved"],
        "hit_rate": overall["hit_rate"],
        "avg_return_pct": overall["avg_return_pct"],
        "by_decision": {k: _group_stats(v) for k, v in by_decision.items()},
        "by_validation_status": {k: _group_stats(v) for k, v in by_validation.items()},
        "last_10_resolved": last_10,
        "best_decision": best,
        "worst_decision": worst,
    }


# ---------------------------------------------------------------------------
# Step 4 — Markdown report
# ---------------------------------------------------------------------------


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:+.1%}"


def _fmt_rate(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:.0%}"


def render_summary_md(summary: dict[str, Any]) -> str:
    total = summary.get("total_decisions", 0)
    resolved = summary.get("resolved", 0)
    hit_rate = summary.get("hit_rate")
    avg_return = summary.get("avg_return_pct")

    lines = [
        "# Decision Outcome Summary",
        "",
        "Observe-only. No trades are executed.",
        "",
        f"Generated: {summary.get('generated_at', '-')}",
        "",
        "## Overview",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total decisions | {total} |",
        f"| Resolved | {resolved} |",
        f"| Unresolved | {summary.get('unresolved', 0)} |",
        f"| Hit rate | {_fmt_rate(hit_rate)} |",
        f"| Avg return | {_fmt_pct(avg_return)} |",
        "",
    ]

    by_decision = summary.get("by_decision") or {}
    if by_decision:
        lines += [
            "## By Decision Type",
            "",
            "| Decision | Count | Resolved | Hit Rate | Avg Return |",
            "|----------|-------|----------|----------|------------|",
        ]
        for dec, stats in sorted(by_decision.items()):
            lines.append(
                f"| {dec} | {stats.get('count', 0)} | {stats.get('resolved', 0)}"
                f" | {_fmt_rate(stats.get('hit_rate'))}"
                f" | {_fmt_pct(stats.get('avg_return_pct'))} |"
            )
        lines.append("")

    by_validation = summary.get("by_validation_status") or {}
    if by_validation:
        lines += [
            "## By Validation Status",
            "",
            "| Status | Count | Resolved | Hit Rate | Avg Return |",
            "|--------|-------|----------|----------|------------|",
        ]
        for status, stats in sorted(by_validation.items()):
            lines += [
                f"| {status} | {stats.get('count', 0)} | {stats.get('resolved', 0)}"
                f" | {_fmt_rate(stats.get('hit_rate'))}"
                f" | {_fmt_pct(stats.get('avg_return_pct'))} |"
            ]
        lines.append("")

    best = summary.get("best_decision")
    worst = summary.get("worst_decision")
    if best or worst:
        lines.append("## Notable Decisions")
        lines.append("")
        if best:
            lines.append(
                f"Best: {best.get('decision')} {best.get('symbol')}"
                f" on {best.get('date')} — return {_fmt_pct(best.get('return_pct'))}"
            )
        if worst:
            lines.append(
                f"Worst: {worst.get('decision')} {worst.get('symbol')}"
                f" on {worst.get('date')} — return {_fmt_pct(worst.get('return_pct'))}"
            )
        lines.append("")

    lines.append("## Insights")
    lines.append("")
    if not resolved:
        lines.append("No resolved outcomes yet. Outcomes accumulate over time.")
    elif hit_rate is not None and hit_rate >= 0.65:
        lines.append(f"Hit rate of {_fmt_rate(hit_rate)} is above the 65 % threshold.")
    elif hit_rate is not None and hit_rate < 0.50:
        lines.append(
            f"Hit rate of {_fmt_rate(hit_rate)} is below 50 %. "
            "Review decision confidence thresholds."
        )
    else:
        lines.append(f"Hit rate is {_fmt_rate(hit_rate)}. Continue accumulating data.")
    lines.append("")

    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_outcome_tracker(
    root: Path | str | None = None,
    *,
    write_files: bool = True,
    price_fetcher: Callable[[list[str]], dict[str, float]] | None = None,
    lookback_days: tuple[int, ...] = DEFAULT_LOOKBACK_DAYS,
) -> tuple[dict[str, Any], str]:
    """
    Orchestrate: snapshot → resolve → aggregate → write.

    Non-fatal: never raises; returns empty summary on failure.
    """
    root_path = Path(root) if root is not None else Path(".")

    # Load source artifacts
    decision_plan = _safe_json_load(root_path.joinpath(*DECISION_PLAN_RELATIVE_PATH))
    ai_validation = _safe_json_load(root_path.joinpath(*AI_VALIDATION_RELATIVE_PATH))
    watchlist_signals = _safe_json_load(root_path.joinpath(*WATCHLIST_SIGNALS_RELATIVE_PATH))
    price_snapshot = _extract_price_map(watchlist_signals)

    # Step 1: snapshot
    if decision_plan:
        try:
            snapshot_decisions(
                root_path,
                decision_plan,
                ai_validation,
                price_snapshot=price_snapshot,
            )
        except Exception as exc:
            logger.warning("OUTCOME TRACKER: snapshot failed (non-fatal): %s", exc)

    # Step 2: resolve
    effective_fetcher = price_fetcher if price_fetcher is not None else _try_build_price_fetcher()
    try:
        rows = resolve_outcomes(
            root_path,
            price_fetcher=effective_fetcher,
            lookback_days=lookback_days,
        )
    except Exception as exc:
        logger.warning("OUTCOME TRACKER: resolve failed (non-fatal): %s", exc)
        rows = _load_jsonl(root_path.joinpath(*OUTCOMES_JSONL_RELATIVE_PATH))

    # Step 3: aggregate
    summary = aggregate_metrics(rows)

    # Step 4: write
    if write_files:
        try:
            json_path = root_path.joinpath(*SUMMARY_JSON_RELATIVE_PATH)
            md_path = root_path.joinpath(*SUMMARY_MD_RELATIVE_PATH)
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
            md = render_summary_md(summary)
            md_path.write_text(md, encoding="utf-8")
        except Exception as exc:
            logger.warning("OUTCOME TRACKER: summary write failed (non-fatal): %s", exc)

    markdown = render_summary_md(summary)
    return summary, markdown


if __name__ == "__main__":
    run_outcome_tracker()
