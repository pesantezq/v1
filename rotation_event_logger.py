"""
Rotation Event Logger
=====================
Append-only JSONL log of rotation-evaluation events from evaluate_exit().

Records every exit suggestion that had a rotation_detail evaluated — both
triggered and non-triggered rotations — enabling offline quality analysis
via profit_attribution.rotation_attribution.

Output: outputs/policy/rotation_events.jsonl
  One JSON object per line; one record per exit evaluation with a non-empty
  rotation_detail per run.

Public API:
  append_rotation_events(exit_results, *, run_id, ...)  -> int
  load_rotation_events(path)                            -> list[dict]
  iter_rotation_events(path)                            -> Iterator[dict]
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, List, Optional

logger = logging.getLogger("rotation_event_logger")

DEFAULT_PATH = Path("outputs/policy/rotation_events.jsonl")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RotationEventRecord:
    """
    One rotation evaluation event.

    Populated whenever evaluate_exit() receives a stronger_opportunity argument,
    regardless of whether rotation was triggered.  Forward outcome fields
    remain None until a separate enrichment step resolves them.
    """
    event_id: str               # "{symbol}_{run_id}"
    timestamp: str
    run_id: str

    # Position context
    symbol: str
    strategy_type: str          # "momentum" | "compounder"

    # Rotation decision detail (from ExitSuggestion.rotation_detail)
    incumbent_score: float      # 0–100
    challenger_score: float     # 0–100
    actual_margin: float        # challenger_score − incumbent_score
    required_margin: float      # threshold from config
    rotation_triggered: bool
    score_basis: str            # "composite_0_to_100"

    # Challenger metadata
    challenger_symbol: Optional[str]
    challenger_is_breakout: bool    # True if challenger has BREAKOUT_PROXY event

    # Regime context
    degraded_mode: bool
    drawdown_regime: str

    # Forward outcome — populated by enrichment; None until resolved
    forward_return_5d: Optional[float] = None
    outcome_resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def append_rotation_events(
    exit_results: List[Any],
    *,
    run_id: str,
    drawdown_regime: str = "normal",
    degraded_mode: bool = False,
    timestamp: Optional[str] = None,
    history_path: Optional[Path] = None,
    dry_run: bool = False,
) -> int:
    """
    Append rotation event records for all exit suggestions with a non-empty
    rotation_detail.

    Only records events where a challenger was evaluated (rotation_detail != {}).
    Records both triggered and non-triggered rotation evaluations.

    Non-fatal: serialization or IO failures are logged as warnings and
    return 0 without raising.

    Args:
        exit_results:    List of ExitSuggestion.to_dict() dicts or ExitSuggestion
                         objects returned from evaluate_exit().
        run_id:          Unique run identifier.
        drawdown_regime: Market regime label at evaluation time.
        degraded_mode:   Whether data quality is degraded.
        timestamp:       ISO datetime string; defaults to now.
        history_path:    Override output file path.
        dry_run:         If True, build records but skip writing.

    Returns:
        Number of records written (or would-be-written in dry_run mode).
    """
    if not exit_results:
        return 0

    ts = timestamp or datetime.now().isoformat()
    records: list[dict[str, Any]] = []

    for result in exit_results:
        detail = _get_rotation_detail(result)
        if not detail:
            continue
        try:
            record = _build_record(
                result=result,
                detail=detail,
                run_id=run_id,
                timestamp=ts,
                drawdown_regime=drawdown_regime,
                degraded_mode=degraded_mode,
            )
            records.append(record.to_dict())
        except Exception as exc:
            logger.warning(
                "rotation_event_logger: failed to serialize result for %s — %s",
                _get_str(result, "symbol", "UNKNOWN"), exc,
            )

    if not records:
        return 0

    if dry_run:
        logger.debug(
            "rotation_event_logger: dry_run=True — skipping write (%d records)", len(records)
        )
        return len(records)

    out_path = Path(history_path) if history_path else DEFAULT_PATH
    try:
        written = _append_jsonl(records, out_path)
        logger.info("rotation_event_logger: appended %d events to %s", written, out_path)
        return written
    except Exception as exc:
        logger.warning("rotation_event_logger: write failed (non-fatal) — %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Reader helpers
# ---------------------------------------------------------------------------

def load_rotation_events(
    path: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """
    Load all rotation events from JSONL into a list of dicts.

    Returns empty list if file absent or unreadable.
    """
    return list(iter_rotation_events(path))


def iter_rotation_events(
    path: Optional[Path] = None,
) -> Iterator[dict[str, Any]]:
    """
    Iterate over rotation events from JSONL one dict at a time.

    Skips malformed lines with a warning rather than raising.
    """
    src = Path(path) if path else DEFAULT_PATH
    if not src.exists():
        return

    with src.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "rotation_event_logger: malformed line %d in %s — %s",
                    lineno, src, exc,
                )
                continue
            yield record


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_rotation_detail(result: Any) -> dict[str, Any]:
    """Extract rotation_detail from a dict or ExitSuggestion object."""
    if isinstance(result, dict):
        return result.get("rotation_detail") or {}
    return getattr(result, "rotation_detail", None) or {}


def _get_str(result: Any, key: str, default: str) -> str:
    if isinstance(result, dict):
        return str(result.get(key) or default)
    return str(getattr(result, key, default) or default)


def _build_record(
    result: Any,
    detail: dict[str, Any],
    *,
    run_id: str,
    timestamp: str,
    drawdown_regime: str,
    degraded_mode: bool,
) -> RotationEventRecord:
    symbol = _get_str(result, "symbol", "UNKNOWN").upper()
    strategy_type = _get_str(result, "strategy_type", "unknown")
    event_id = f"{symbol}_{run_id}"

    incumbent_score = float(detail.get("incumbent_score") or 0.0)
    challenger_score = float(detail.get("challenger_score") or 0.0)
    actual_margin = float(detail.get("actual_margin") or 0.0)
    required_margin = float(detail.get("required_margin") or 0.0)
    rotation_triggered = bool(detail.get("rotation_triggered", False))
    score_basis = str(detail.get("score_basis") or "composite_0_to_100")

    challenger_symbol: Optional[str]
    if isinstance(result, dict):
        challenger_symbol = result.get("challenger_symbol") or None
        challenger_events: list = list(result.get("challenger_events") or [])
    else:
        challenger_symbol = getattr(result, "challenger_symbol", None) or None
        challenger_events = list(getattr(result, "challenger_events", None) or [])

    challenger_is_breakout = "BREAKOUT_PROXY" in challenger_events

    return RotationEventRecord(
        event_id=event_id,
        timestamp=timestamp,
        run_id=run_id,
        symbol=symbol,
        strategy_type=strategy_type,
        incumbent_score=incumbent_score,
        challenger_score=challenger_score,
        actual_margin=actual_margin,
        required_margin=required_margin,
        rotation_triggered=rotation_triggered,
        score_basis=score_basis,
        challenger_symbol=challenger_symbol,
        challenger_is_breakout=challenger_is_breakout,
        degraded_mode=bool(degraded_mode),
        drawdown_regime=str(drawdown_regime or "normal"),
    )


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _append_jsonl(records: list[dict[str, Any]], path: Path) -> int:
    """Atomic batch append — single write + fsync, no torn records."""
    if not records:
        return 0
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd: Optional[int] = None
    try:
        fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY)
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
        return len(records)
    finally:
        if fd is not None:
            os.close(fd)
