"""
Coverage Tracker
================
Records promoted market-coverage candidates to a JSONL history file so their
price performance can be evaluated on subsequent runs.

Storage: ``outputs/policy/coverage_history.jsonl`` (one JSON line per record)

Each record captures the state of a promoted candidate at the time it was
promoted:

  run_id          — "{YYYY-MM-DD}_{mode}" e.g. "2026-04-16_daily"
  date            — ISO-8601 calendar date derived from run_id
  recorded_at     — UTC ISO-8601 wall-clock timestamp of the write
  symbol          — ticker symbol
  label           — "compounder" | "momentum" | "watchlist"
  score           — composite opportunity score (0–100)
  rank            — rank within the promoted list for this run
  events          — list of EventType string values that fired
  price           — price at time of promotion (from scan_by_symbol)
  pct_change_1d   — same-day % change (from scan_by_symbol)
  rel_volume      — relative volume at promotion time
  drawdown_regime — portfolio drawdown regime at promotion time
  portfolio_context — action_bucket / action_hint from build_portfolio_review()

On each subsequent run where the same symbol is promoted, a new record is
appended.  The evaluator uses the first appearance of a symbol (or the first
appearance after a configurable gap) as the "entry" and all later appearances
as price observations.

Usage::

    from coverage_tracker import append_coverage_run, load_coverage_history

    written = append_coverage_run(
        run_id="2026-04-16_daily",
        promoted=promoted_candidates,       # List[PromotedCandidate]
        scan_by_symbol=scan_result_dict,    # Dict[str, ScanResult] or List[ScanResult]
        drawdown_regime="normal",
    )
    history = load_coverage_history()       # List[dict]
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union

from decision_support import normalize_score, normalize_symbol, read_value

logger = logging.getLogger("portfolio_automation.coverage_tracker")

_DEFAULT_HISTORY_PATH = Path("outputs/policy/coverage_history.jsonl")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def append_coverage_run(
    run_id: str,
    promoted: list,
    scan_by_symbol: Union[Dict, List, None] = None,
    drawdown_regime: str = "normal",
    history_path: Optional[Path] = None,
) -> int:
    """
    Append one record per promoted candidate to coverage_history.jsonl.

    Args:
        run_id:         Run identifier, format "{YYYY-MM-DD}_{mode}".
        promoted:       List of PromotedCandidate objects.
        scan_by_symbol: Either a ``Dict[str, ScanResult]`` keyed by symbol,
                        or a ``List[ScanResult]``.  Used to look up entry
                        price and related shallow fields.  Pass None or empty
                        if scan data is unavailable.
        drawdown_regime: Portfolio drawdown regime at the time of this run.
        history_path:   Override default output path.

    Returns:
        Number of records successfully written.
    """
    path = Path(history_path) if history_path else _DEFAULT_HISTORY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    scan_map = _to_scan_map(scan_by_symbol)
    run_date = _parse_date_from_run_id(run_id)
    date_str = (
        run_date.isoformat()
        if run_date is not None
        else datetime.now(timezone.utc).date().isoformat()
    )
    now_iso = datetime.now(timezone.utc).isoformat()

    records = []
    for cand in promoted:
        sr = scan_map.get(normalize_symbol(_get_attr(cand, "symbol"), default=""))
        rec = _build_record(
            cand=cand,
            run_id=run_id,
            date_str=date_str,
            now_iso=now_iso,
            scan_result=sr,
            drawdown_regime=drawdown_regime,
        )
        records.append(rec)

    if not records:
        logger.debug(
            "coverage_tracker: no promoted candidates to record for %s", run_id
        )
        return 0

    written = 0
    try:
        with open(path, "a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
                written += 1
        logger.info(
            "coverage_tracker: appended %d records for %s → %s",
            written, run_id, path,
        )
    except OSError as exc:
        logger.warning("coverage_tracker: write failed (non-fatal): %s", exc)
        return 0

    return written


def load_coverage_history(
    history_path: Optional[Path] = None,
) -> List[dict]:
    """
    Load all records from coverage_history.jsonl.

    Returns:
        List of dicts (one per record), oldest first.
        Returns empty list if the file does not exist or is unreadable.
    """
    path = Path(history_path) if history_path else _DEFAULT_HISTORY_PATH
    if not path.exists():
        logger.debug("coverage_tracker: no history file at %s", path)
        return []

    records: List[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    records.append(json.loads(stripped))
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "coverage_tracker: skipping malformed line %d: %s", lineno, exc
                    )
    except OSError as exc:
        logger.warning("coverage_tracker: read failed: %s", exc)
        return []

    logger.debug(
        "coverage_tracker: loaded %d records from %s", len(records), path
    )
    return records


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_record(
    cand: object,
    run_id: str,
    date_str: str,
    now_iso: str,
    scan_result: object,
    drawdown_regime: str,
) -> dict:
    """Build a single history record dict from a PromotedCandidate."""
    price = _get_attr(scan_result, "price")
    pct_change = _get_attr(scan_result, "pct_change_1d")
    rel_volume = _get_attr(scan_result, "rel_volume")

    pc = _get_attr(cand, "portfolio_context") or {}

    return {
        "run_id": run_id,
        "date": date_str,
        "recorded_at": now_iso,
        "symbol": normalize_symbol(_get_attr(cand, "symbol"), default=""),
        "label": str(_get_attr(cand, "label") or "watchlist"),
        "score": _score_value(cand),
        "rank": _safe_int(_get_attr(cand, "rank")),
        "events": list(_get_attr(cand, "events") or []),
        "price": _safe_float(price),
        "pct_change_1d": _safe_float(pct_change),
        "rel_volume": _safe_float(rel_volume),
        "drawdown_regime": str(drawdown_regime or "normal"),
        "action_bucket": str(pc.get("action_bucket", "")),
        "action_hint": str(pc.get("action_hint", "")),
    }


def _to_scan_map(scan_by_symbol: Union[Dict, List, None]) -> dict:
    """Normalise scan_by_symbol to a {symbol: ScanResult} dict."""
    if scan_by_symbol is None:
        return {}
    if isinstance(scan_by_symbol, dict):
        if all(normalize_symbol(sym, default="") == str(sym) for sym in scan_by_symbol):
            return scan_by_symbol
        result: dict = {}
        for sym, payload in scan_by_symbol.items():
            normalized = normalize_symbol(sym, default="")
            if normalized:
                result[normalized] = payload
        return result
    # Assume iterable of ScanResult-like objects
    result: dict = {}
    for sr in scan_by_symbol:
        sym = _get_attr(sr, "symbol", None)
        if sym:
            result[normalize_symbol(sym)] = sr
    return result


def _parse_date_from_run_id(run_id: str) -> Optional[date]:
    """Extract calendar date from a run_id like '2026-04-16_daily'."""
    try:
        return date.fromisoformat(str(run_id).split("_")[0])
    except (ValueError, AttributeError, IndexError):
        return None


def _get_attr(obj: object, name: str, default=None):
    """Safe getattr for use with arbitrary objects or None."""
    if obj is None:
        return default
    return read_value(obj, name, default)


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return None if (f != f or f == float("inf") or f == float("-inf")) else f
    except (TypeError, ValueError):
        return None


def _safe_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _score_value(cand: object) -> Optional[float]:
    score = _get_attr(cand, "score", None)
    if score is None:
        score = _get_attr(cand, "total_score", None)
    if score is None:
        return None
    normalized = normalize_score(score, default=0.0)
    return _safe_float(normalized)
