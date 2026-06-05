"""
Signal sources for the POC simulation harness  (additive | advisory-only | observe-only)

Pattern-Improvement Loop — Step 1. Lets the harness replay the system's REAL
emitted signals (outputs/latest/watchlist_signals.json) instead of only the
synthetic generator. Read-only: it normalizes rows; it does not write artifacts
and does not touch any protected scoring/decision/allocation logic.

Pattern derivation (decided 2026-06-01): a WatchlistRow's trigger is encoded as
`alert_basis`, a *list* of basis tags (e.g. ['price_move', 'volume_spike']). We
map each tag to a registry *family* label and keep the FULL set under `patterns`
(multi-tag: a composite row credits every bucket it carries) plus a single
representative `pattern` (price action preferred) so the existing per-pattern
breakdown — which assumes one pattern per (ticker, scan_time) — keeps working
unchanged until Steps 1b–4 consume the multi-tag list. Direction (UP vs DOWN) is
deliberately deferred to Step 1b, which has the price series to resolve it; here
`price_move` maps to the family `STRONG_MOVE`.

Note on live signals: rows in outputs/latest are dated *today*, so they have no
forward window yet and will not resolve in a backtest until forward_days elapse.
`load_historical_signal_snapshots` aggregates dated outputs/history snapshots to
build a signal history old enough to evaluate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# alert_basis tag → signal_registry.yaml family label. Unknown tags pass through
# uppercased; an empty basis classifies as UNKNOWN (kept, never silently dropped).
_BASIS_TO_PATTERN: dict[str, str] = {
    "price_move": "STRONG_MOVE",      # → STRONG_MOVE_UP / STRONG_MOVE_DOWN (resolved in Step 1b)
    "volume_spike": "VOLUME_SPIKE",
    "signal_score": "SIGNAL_SCORE",
    "breakout": "BREAKOUT_PROXY",
}

# Priority for selecting the single representative `pattern` from a multi-tag row.
# Price action is the actionable core, then volume, then score-only.
_PATTERN_PRIORITY: list[str] = ["STRONG_MOVE", "VOLUME_SPIKE", "BREAKOUT_PROXY", "SIGNAL_SCORE"]

_UNKNOWN = "UNKNOWN"


def _map_basis(alert_basis: Any) -> list[str]:
    """Map an alert_basis list to deduped registry-family pattern labels,
    preserving first-seen order. Empty / non-list → ['UNKNOWN']."""
    if not isinstance(alert_basis, (list, tuple)) or not alert_basis:
        return [_UNKNOWN]
    out: list[str] = []
    for tag in alert_basis:
        label = _BASIS_TO_PATTERN.get(str(tag).strip().lower(), str(tag).strip().upper())
        if label and label not in out:
            out.append(label)
    return out or [_UNKNOWN]


def _representative_pattern(patterns: list[str]) -> str:
    """Pick one label for the existing single-pattern breakdown path."""
    for preferred in _PATTERN_PRIORITY:
        if preferred in patterns:
            return preferred
    return patterns[0] if patterns else _UNKNOWN


def _normalize_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one WatchlistRow to the harness signal shape. Returns None for
    rows without a ticker (unusable)."""
    ticker = row.get("ticker")
    if not ticker:
        return None
    patterns = _map_basis(row.get("alert_basis"))
    out = {
        "ticker": str(ticker).upper(),
        "scan_time": row.get("scan_time") or row.get("signal_date"),
        "signal_score": row.get("signal_score"),
        "confidence_score": row.get("confidence_score"),
        "pattern": _representative_pattern(patterns),
        "patterns": patterns,
    }
    # Preserve provenance when present (e.g. historical_reconstruction) so downstream
    # consumers (auto_apply's reconstructed-evidence gate) can detect the source.
    if row.get("source"):
        out["source"] = row.get("source")
    return out


def load_signals_from_artifact(
    path: str = "outputs/latest/watchlist_signals.json",
) -> list[dict]:
    """Read {results:[...]} and normalize each row to the harness signal shape
    {ticker, scan_time, signal_score, confidence_score, pattern, patterns}.

    Returns [] if the file is missing, empty, or malformed (degraded) — never
    raises. `pattern`/`patterns` derive from the row's alert_basis (see module
    docstring); rows without a ticker are skipped.
    """
    p = Path(path)
    if not p.exists():
        return []
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return []
    if not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    out: list[dict] = []
    for row in results:
        if not isinstance(row, dict):
            continue
        norm = _normalize_row(row)
        if norm is not None:
            out.append(norm)
    return out


def load_historical_signal_snapshots(snapshot_dir: str = "outputs/history") -> list[dict]:
    """Aggregate dated watchlist_signals snapshots under *snapshot_dir* into one
    signal list, for a longer signal history than the single latest artifact.

    Looks for outputs/history/<date>/watchlist_signals.json (and a flat
    <date>_watchlist_signals.json fallback). Returns [] when the directory is
    absent or holds no readable snapshots (degraded) — never raises.
    """
    base = Path(snapshot_dir)
    if not base.is_dir():
        return []
    out: list[dict] = []
    candidates = sorted(base.glob("*/watchlist_signals.json")) + sorted(
        base.glob("*_watchlist_signals.json")
    )
    for snap in candidates:
        out.extend(load_signals_from_artifact(str(snap)))
    return out
