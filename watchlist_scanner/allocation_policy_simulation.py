"""
Observe-only allocation policy simulation.

Compares flat (baseline) allocation vs rank-aware allocation on resolved
historical signals to evaluate whether rank score weighting improves capital
deployment outcomes.

Simulation ONLY:
  - No live allocation changes
  - No mutations to portfolio or signal dicts
  - No changes to recommendations

Reads:
  data/portfolio.db                                   (resolved signal feedback)
  outputs/performance/allocation_policy_preview.json  (optional: current preview)

Writes:
  outputs/performance/allocation_policy_simulation.json

Run standalone:
  python -m watchlist_scanner.allocation_policy_simulation
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from watchlist_scanner.allocation_preview import (
    _DEFAULT_BASELINE_PCT,
    _DEFAULT_MAX_TICKER_PCT,
    _rank_multiplier,
)

logger = logging.getLogger("watchlist_scanner.allocation_policy_simulation")

PRIMARY_WINDOW_DAYS: int = 3

_OUTPUT_REL = ("outputs", "performance", "allocation_policy_simulation.json")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _compute_preview_size(
    baseline_size: float,
    final_rank_score: float,
    max_ticker_pct: float,
) -> tuple[float, str, float]:
    """Return (capped_preview_size, rank_label, multiplier) for a resolved row."""
    multiplier, label = _rank_multiplier(final_rank_score)
    raw = round(baseline_size * multiplier, 4)
    capped = round(min(raw, max_ticker_pct), 4)
    return capped, label, multiplier


def _is_win(outcome_return: float) -> bool:
    return outcome_return > 0.0


# ---------------------------------------------------------------------------
# Core builder (pure — no I/O)
# ---------------------------------------------------------------------------

def build_allocation_policy_simulation(
    resolved_rows: list[dict[str, Any]],
    preview_opportunities: dict[str, dict[str, Any]],
    *,
    primary_window_days: int = PRIMARY_WINDOW_DAYS,
    max_ticker_pct: float = _DEFAULT_MAX_TICKER_PCT,
    fallback_baseline_pct: float = _DEFAULT_BASELINE_PCT,
) -> dict[str, Any]:
    """
    Compare baseline vs rank-aware allocation on resolved historical signals.

    Does NOT mutate resolved_rows, preview_opportunities, or any nested dicts.

    Args:
        resolved_rows: All signal feedback rows (resolved + unresolved).
            Unresolved rows (missing outcome_return_{window}d) are ignored.
        preview_opportunities: Ticker → opportunity dict from
            allocation_policy_preview.json. Used for preview_size when
            available; otherwise recomputed from final_rank_score.
        primary_window_days: Return window used as outcome (1, 3, or 7).
        max_ticker_pct: Single-position ceiling for rank-aware preview_size.
        fallback_baseline_pct: Used when normalized_allocation is None.

    Returns a simulation dict with observe_only=True, not_applied=True.
    """
    return_col = f"outcome_return_{primary_window_days}d"

    resolved = [
        row for row in resolved_rows
        if row.get(return_col) is not None
    ]

    _empty_side: dict[str, Any] = {
        "total_return": 0.0,
        "avg_return_per_trade": 0.0,
        "capital_efficiency": 0.0,
        "total_allocated_pct": 0.0,
        "win_capital_pct": 0.0,
        "loss_capital_pct": 0.0,
    }

    if not resolved:
        return {
            "generated_at": datetime.now().isoformat(),
            "observe_only": True,
            "not_applied": True,
            "primary_window_days": primary_window_days,
            "sample_size": 0,
            "baseline": dict(_empty_side),
            "rank_aware": dict(_empty_side),
            "delta": {"total_return_delta": 0.0, "efficiency_delta": 0.0, "win_capital_delta": 0.0},
            "details": [],
        }

    details: list[dict[str, Any]] = []

    for row in resolved:
        ticker = str(row.get("ticker") or "UNKNOWN").upper()
        outcome_return = float(row[return_col])
        rank_score = float(row.get("final_rank_score") or 0.0)

        norm_alloc = row.get("normalized_allocation")
        baseline_size = (
            float(norm_alloc) if norm_alloc is not None else fallback_baseline_pct
        )
        baseline_size = round(max(0.0, baseline_size), 4)

        if ticker in preview_opportunities:
            opp = preview_opportunities[ticker]
            preview_size = float(opp.get("preview_size") or 0.0)
            rank_label = str(opp.get("rank_label") or "unknown")
            rank_mult = float(opp.get("rank_multiplier") or 1.0)
        else:
            preview_size, rank_label, rank_mult = _compute_preview_size(
                baseline_size, rank_score, max_ticker_pct
            )

        details.append({
            "ticker": ticker,
            "outcome_return": round(outcome_return, 4),
            "baseline_size": baseline_size,
            "preview_size": preview_size,
            "rank_score": round(rank_score, 4),
            "rank_label": rank_label,
            "rank_multiplier": round(rank_mult, 4),
            "baseline_contribution": round(outcome_return * baseline_size, 4),
            "preview_contribution": round(outcome_return * preview_size, 4),
            "win": _is_win(outcome_return),
        })

    sample_size = len(details)

    def _side_metrics(size_key: str, contrib_key: str) -> dict[str, Any]:
        total_alloc = round(sum(d[size_key] for d in details), 4)
        total_ret = round(sum(d[contrib_key] for d in details), 4)
        avg_ret = round(total_ret / sample_size, 4)
        efficiency = round(total_ret / total_alloc, 4) if total_alloc > 0 else 0.0
        win_cap = round(sum(d[size_key] for d in details if d["win"]), 4)
        loss_cap = round(sum(d[size_key] for d in details if not d["win"]), 4)
        win_cap_pct = round(win_cap / total_alloc, 4) if total_alloc > 0 else 0.0
        loss_cap_pct = round(loss_cap / total_alloc, 4) if total_alloc > 0 else 0.0
        return {
            "total_return": total_ret,
            "avg_return_per_trade": avg_ret,
            "capital_efficiency": efficiency,
            "total_allocated_pct": total_alloc,
            "win_capital_pct": win_cap_pct,
            "loss_capital_pct": loss_cap_pct,
        }

    baseline_side = _side_metrics("baseline_size", "baseline_contribution")
    rank_aware_side = _side_metrics("preview_size", "preview_contribution")

    return {
        "generated_at": datetime.now().isoformat(),
        "observe_only": True,
        "not_applied": True,
        "primary_window_days": primary_window_days,
        "sample_size": sample_size,
        "baseline": baseline_side,
        "rank_aware": rank_aware_side,
        "delta": {
            "total_return_delta": round(
                rank_aware_side["total_return"] - baseline_side["total_return"], 4
            ),
            "efficiency_delta": round(
                rank_aware_side["capital_efficiency"] - baseline_side["capital_efficiency"], 4
            ),
            "win_capital_delta": round(
                rank_aware_side["win_capital_pct"] - baseline_side["win_capital_pct"], 4
            ),
        },
        "details": details,
    }


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def _load_resolved_signals(
    db_path: Path,
    *,
    primary_window_days: int = PRIMARY_WINDOW_DAYS,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """
    Load signal feedback from SQLite and return rows resolved for the given window.

    Returns [] when the DB is absent or unreadable.
    """
    if not db_path.exists():
        logger.info("allocation_policy_simulation: DB not found at %s", db_path)
        return []
    try:
        from watchlist_scanner.state import WatchlistStateStore  # noqa: PLC0415
        store = WatchlistStateStore(db_path)
        rows = store.list_signal_feedback(limit=limit)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "allocation_policy_simulation: could not load signal feedback — %s", exc
        )
        return []
    return_col = f"outcome_return_{primary_window_days}d"
    return [row for row in rows if row.get(return_col) is not None]


def _load_preview_opportunities(preview_path: Path) -> dict[str, dict[str, Any]]:
    """
    Load allocation_policy_preview.json and return a ticker → opportunity map.

    Returns {} when the file is absent or malformed.
    """
    if not preview_path.exists():
        return {}
    try:
        data = json.loads(preview_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "allocation_policy_simulation: could not read preview — %s", exc
        )
        return {}
    if not isinstance(data, dict):
        return {}
    opps = data.get("opportunities")
    if not isinstance(opps, list):
        return {}
    return {
        str(o.get("ticker") or "").upper(): o
        for o in opps
        if o.get("ticker")
    }


# ---------------------------------------------------------------------------
# Report generator
# ---------------------------------------------------------------------------

def generate_allocation_policy_simulation_report(
    *,
    root: Path | str | None = None,
    db_path: str | Path | None = None,
    output_dir: Path | str | None = None,
    primary_window_days: int = PRIMARY_WINDOW_DAYS,
    limit: int = 5000,
) -> dict[str, Any]:
    """
    Load resolved signals + preview, build simulation, write output file.

    Returns the simulation dict.
    Gracefully handles missing DB or preview file.
    """
    root_path = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    _db = Path(db_path) if db_path is not None else root_path / "data" / "portfolio.db"
    out_dir = (
        Path(output_dir) if output_dir is not None
        else root_path.joinpath(*_OUTPUT_REL).parent
    )

    resolved_rows = _load_resolved_signals(
        _db,
        primary_window_days=primary_window_days,
        limit=limit,
    )
    preview_path = root_path / "outputs" / "performance" / "allocation_policy_preview.json"
    preview_opp_by_ticker = _load_preview_opportunities(preview_path)

    sim = build_allocation_policy_simulation(
        resolved_rows,
        preview_opp_by_ticker,
        primary_window_days=primary_window_days,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "allocation_policy_simulation.json"
    out_path.write_text(json.dumps(sim, indent=2), encoding="utf-8")
    logger.info(
        "allocation_policy_simulation: wrote %d-signal simulation to %s",
        sim["sample_size"],
        out_path,
    )
    return sim


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m watchlist_scanner.allocation_policy_simulation",
        description=(
            "Compare baseline vs rank-aware allocation on resolved historical signals. "
            "Simulation ONLY — does NOT modify live allocation outputs."
        ),
    )
    parser.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="Project root (default: two levels above this module)",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        metavar="PATH",
        help="Path to portfolio.db (default: <root>/data/portfolio.db)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        metavar="DIR",
        help="Output directory (default: outputs/performance/ relative to root)",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=PRIMARY_WINDOW_DAYS,
        metavar="DAYS",
        help=f"Primary return window in days (default: {PRIMARY_WINDOW_DAYS})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5000,
        metavar="N",
        help="Max signal feedback rows to load (default: 5000)",
    )
    args = parser.parse_args()

    sim = generate_allocation_policy_simulation_report(
        root=args.root,
        db_path=args.db_path,
        output_dir=args.output_dir,
        primary_window_days=args.window,
        limit=args.limit,
    )
    print(json.dumps(sim, indent=2))


if __name__ == "__main__":
    _main()
