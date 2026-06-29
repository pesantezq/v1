"""
Observe-only rank-aware allocation preview.

Simulates how final_rank_score would influence position sizing without touching
live allocation logic, recommendations, or any output used in real decisions.

Reads:
  outputs/latest/watchlist_signals.json     (scanner results)
  outputs/portfolio/portfolio_snapshot.json  (config + existing sector exposure)

Writes:
  outputs/performance/allocation_policy_preview.json

Run standalone:
  python -m watchlist_scanner.allocation_preview
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("watchlist_scanner.allocation_preview")

# ---------------------------------------------------------------------------
# Relative paths
# ---------------------------------------------------------------------------

_SIGNALS_REL = ("outputs", "latest", "watchlist_signals.json")
_PORTFOLIO_REL = ("outputs", "portfolio", "portfolio_snapshot.json")
_OUTPUT_REL = ("outputs", "performance", "allocation_policy_preview.json")

# ---------------------------------------------------------------------------
# Rank multiplier thresholds (mirrors portfolio_fit.py label thresholds)
# ---------------------------------------------------------------------------

RANK_STRONG: float = 0.75
RANK_GOOD: float = 0.55
RANK_NEUTRAL: float = 0.35

MULTIPLIER_STRONG: float = 1.25
MULTIPLIER_GOOD: float = 1.10
MULTIPLIER_NEUTRAL: float = 1.00
MULTIPLIER_POOR: float = 0.75

# Fallback sizing config when portfolio snapshot config is absent.
# Mirrors allocation_engine DEFAULT_CONFIG after the 2026-05-18 retune,
# adjusted by the 2026-06-26 targeted partial revert (sector_cap 0.35->0.25,
# max_position_cap 0.15->0.12). Keep in lock-step with allocation_engine.
_DEFAULT_BASELINE_PCT: float = 0.02
_DEFAULT_MAX_TICKER_PCT: float = 0.12
_DEFAULT_MAX_SECTOR_PCT: float = 0.25
_DEFAULT_MAX_TOTAL_PCT: float = 0.40
_DEFAULT_CONFIDENCE_THRESHOLD: float = 0.50


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _rank_multiplier(score: float) -> tuple[float, str]:
    """Return (multiplier, label) for a final_rank_score."""
    if score >= RANK_STRONG:
        return MULTIPLIER_STRONG, "strong"
    if score >= RANK_GOOD:
        return MULTIPLIER_GOOD, "good"
    if score >= RANK_NEUTRAL:
        return MULTIPLIER_NEUTRAL, "neutral"
    return MULTIPLIER_POOR, "poor"


def _sector_from_signal(signal: dict[str, Any]) -> str:
    """Extract sector name from a signal dict (handles nested fundamentals)."""
    fund = signal.get("fundamentals") or {}
    raw = str(fund.get("sector") or signal.get("sector") or "Unknown")
    return raw.strip().upper() or "Unknown"


def _build_reason(
    signal: dict[str, Any],
    rank_label: str,
    multiplier: float,
    capped_by: list[str],
) -> str:
    parts: list[str] = []
    score = float(signal.get("final_rank_score") or 0.0)
    parts.append(f"{rank_label.title()} rank score ({score:.3f})")
    fit_label = str(signal.get("portfolio_fit_label") or "unknown")
    parts.append(f"portfolio fit: {fit_label}")
    themes = list(signal.get("themes") or [])
    if themes:
        parts.append(f"themes: {', '.join(str(t) for t in themes[:2])}")
    if multiplier != 1.0:
        parts.append(f"size multiplied ×{multiplier:.2f}")
    if capped_by:
        parts.append(f"capped by {', '.join(capped_by)}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------

def build_allocation_preview(
    signals: list[dict[str, Any]],
    portfolio_snapshot: dict[str, Any],
    *,
    confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD,
) -> dict[str, Any]:
    """
    Build an observe-only rank-aware allocation preview.

    Does NOT mutate portfolio_snapshot or any signal dict.

    Returns a preview dict with:
      - observe_only: True
      - not_applied: True
      - opportunities: list sorted by final_rank_score descending

    Sizing caps (in order of precedence):
      1. max_ticker_allocation — single-position ceiling
      2. sector_cap — sector headroom above current portfolio exposure
      3. total_cap — remaining headroom under max_total_allocation

    Sector exposure starts from portfolio_snapshot["allocation_by_sector"] and
    accumulates across preview opportunities so each candidate reduces the
    remaining sector headroom for subsequent candidates.
    """
    snap_cfg = dict(portfolio_snapshot.get("config") or {})
    baseline_pct = float(snap_cfg.get("baseline_position_pct", _DEFAULT_BASELINE_PCT))
    max_ticker_pct = float(snap_cfg.get("max_ticker_allocation", _DEFAULT_MAX_TICKER_PCT))
    max_sector_pct = float(snap_cfg.get("max_sector_allocation", _DEFAULT_MAX_SECTOR_PCT))
    max_total_pct = float(snap_cfg.get("max_total_allocation", _DEFAULT_MAX_TOTAL_PCT))

    # Working copy of sector exposure — never modifies the original snapshot
    sector_exp: dict[str, float] = {
        k: float(v)
        for k, v in (portfolio_snapshot.get("allocation_by_sector") or {}).items()
    }

    # Filter eligible signals
    eligible = [
        s for s in signals
        if bool(s.get("filter_allowed"))
        and float(s.get("confidence_score") or 0.0) >= confidence_threshold
    ]
    eligible.sort(key=lambda s: float(s.get("final_rank_score") or 0.0), reverse=True)

    opportunities: list[dict[str, Any]] = []
    running_total_pct: float = 0.0

    for sig in eligible:
        ticker = str(sig.get("ticker") or "UNKNOWN").upper()
        rank_score = float(sig.get("final_rank_score") or 0.0)
        confidence = float(sig.get("confidence_score") or 0.0)
        sector = _sector_from_signal(sig)
        fit_label = str(sig.get("portfolio_fit_label") or "unknown")

        multiplier, rank_label = _rank_multiplier(rank_score)
        raw_pct = round(baseline_pct * multiplier, 4)

        # Cap 1: max single-position size
        capped_pct = raw_pct
        capped_by: list[str] = []
        if capped_pct > max_ticker_pct:
            capped_pct = max_ticker_pct
            capped_by.append("max_position_cap")

        # Cap 2: sector headroom
        current_sector = sector_exp.get(sector, 0.0)
        sector_headroom = max(0.0, max_sector_pct - current_sector)
        if capped_pct > sector_headroom:
            capped_pct = max(0.0, sector_headroom)
            if "sector_cap" not in capped_by:
                capped_by.append("sector_cap")

        # Cap 3: remaining total allocation headroom
        total_headroom = max(0.0, max_total_pct - running_total_pct)
        if capped_pct > total_headroom:
            capped_pct = max(0.0, total_headroom)
            if "total_cap" not in capped_by:
                capped_by.append("total_cap")

        capped_pct = round(capped_pct, 4)

        # Accumulate running totals (local copies only — no snapshot mutation)
        running_total_pct = round(running_total_pct + capped_pct, 4)
        sector_exp[sector] = round(sector_exp.get(sector, 0.0) + capped_pct, 4)

        opportunities.append({
            "ticker": ticker,
            "final_rank_score": round(rank_score, 4),
            "rank_label": rank_label,
            "rank_multiplier": multiplier,
            "baseline_size": round(baseline_pct, 4),
            "preview_size": capped_pct,
            "capped_by": capped_by,
            "sector": sector,
            "confidence_score": round(confidence, 4),
            "portfolio_fit_label": fit_label,
            "reason": _build_reason(sig, rank_label, multiplier, capped_by),
        })

    return {
        "generated_at": datetime.now().isoformat(),
        "observe_only": True,
        "not_applied": True,
        "confidence_threshold": confidence_threshold,
        "candidate_count": len(opportunities),
        "total_baseline_pct": round(sum(o["baseline_size"] for o in opportunities), 4),
        "total_preview_pct": round(running_total_pct, 4),
        "opportunities": opportunities,
    }


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def _load_signals(signals_path: Path) -> list[dict[str, Any]]:
    """
    Load signal list from watchlist_signals.json.

    The file contains a full scan_result dict; signals are under the "results" key.
    Returns [] when the file is absent, malformed, or has no results.
    """
    if not signals_path.exists():
        logger.info("allocation_preview: signals file not found at %s", signals_path)
        return []
    try:
        data = json.loads(signals_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("allocation_preview: could not read signals — %s", exc)
        return []
    if not isinstance(data, dict):
        return []
    results = data.get("results")
    if not isinstance(results, list):
        return []
    return results


def _load_portfolio_snapshot(snapshot_path: Path) -> dict[str, Any]:
    """Load portfolio_snapshot.json. Returns {} on any failure."""
    if not snapshot_path.exists():
        return {}
    try:
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("allocation_preview: could not read portfolio snapshot — %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Report generator
# ---------------------------------------------------------------------------

def generate_allocation_preview_report(
    *,
    root: Path | str | None = None,
    output_dir: Path | str | None = None,
    confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD,
) -> dict[str, Any]:
    """
    Load signals + portfolio snapshot, build preview, write output file.

    Returns the preview dict.
    Does not raise on missing inputs — falls back to empty signals or empty snapshot.
    """
    root_path = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    out_dir = Path(output_dir) if output_dir is not None else root_path.joinpath(*_OUTPUT_REL).parent

    signals_path = root_path.joinpath(*_SIGNALS_REL)
    snapshot_path = root_path.joinpath(*_PORTFOLIO_REL)

    signals = _load_signals(signals_path)
    snapshot = _load_portfolio_snapshot(snapshot_path)

    preview = build_allocation_preview(
        signals,
        snapshot,
        confidence_threshold=confidence_threshold,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "allocation_policy_preview.json"
    out_path.write_text(json.dumps(preview, indent=2), encoding="utf-8")
    logger.info(
        "allocation_preview: wrote %d opportunities to %s",
        len(preview["opportunities"]),
        out_path,
    )
    return preview


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m watchlist_scanner.allocation_preview",
        description=(
            "Build an observe-only rank-aware allocation preview. "
            "Does NOT modify live allocation outputs."
        ),
    )
    parser.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="Project root (default: two levels above this module)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        metavar="DIR",
        help="Output directory (default: outputs/performance/ relative to root)",
    )
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=_DEFAULT_CONFIDENCE_THRESHOLD,
        metavar="FLOAT",
        help=f"Min confidence_score to include a signal (default: {_DEFAULT_CONFIDENCE_THRESHOLD})",
    )
    args = parser.parse_args()

    preview = generate_allocation_preview_report(
        root=args.root,
        output_dir=args.output_dir,
        confidence_threshold=args.confidence_threshold,
    )
    print(json.dumps(preview, indent=2))


if __name__ == "__main__":
    _main()
