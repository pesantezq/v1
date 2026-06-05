"""
Historical signal reconstruction  (additive | advisory-only | observe-only)

Pattern-Improvement Loop — sub-project F. Recomputes pattern-family signals
(STRONG_MOVE_UP/DOWN, VOLUME_SPIKE) point-in-time from archived OHLCV so the
walk-forward OOS window can mature without waiting for live signal history.

Look-ahead-safe BY CONSTRUCTION: each emitted date uses only rows at or before it
(price move from the prior close; volume vs the trailing window; an optional
`today` hard-stop). signal_score/confidence are deferred (emitted None). Outcomes
(forward returns) are computed downstream by the backtester and are future by
definition — that is the label, not leakage. `assert_no_lookahead` proves the
no-leakage property via a truncation-equality audit. Pure/total; never raises.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from backtesting.signal_sources import _map_basis, _representative_pattern

_OBSERVE_ONLY = True
_SOURCE = "historical_reconstruction"


def _sorted_series(rows: list[dict]) -> list[dict]:
    """Chronological (ascending-date) series, dropping undatable / closeless rows.
    FMP archives are newest-first, so this normalizes order."""
    clean = [r for r in rows if r.get("date") and r.get("close") is not None]
    return sorted(clean, key=lambda r: str(r["date"])[:10])


def reconstruct_signals(
    ticker: str,
    rows: list[dict],
    *,
    strong_move_pct: float = 3.0,
    volume_spike_factor: float = 2.0,
    vol_window: int = 20,
    today: str | None = None,
) -> list[dict]:
    """Reconstruct pattern-family signals for one ticker from its OHLCV rows
    (any order). Each emitted signal for date D uses only rows <= D. Returns a
    list of harness signal dicts; never raises."""
    series = _sorted_series(rows)
    out: list[dict] = []
    for i in range(1, len(series)):
        d = str(series[i]["date"])[:10]
        if today is not None and d > today:
            break
        try:
            prev_close = float(series[i - 1]["close"])
            close = float(series[i]["close"])
        except (TypeError, ValueError):
            continue
        if prev_close <= 0:
            continue
        ret_pct = (close - prev_close) / prev_close * 100.0

        basis: list[str] = []
        direction = "up" if ret_pct >= 0 else "down"
        if abs(ret_pct) >= strong_move_pct:
            basis.append("price_move")

        window = series[max(0, i - vol_window):i]
        vols = [float(r["volume"]) for r in window if r.get("volume") not in (None, "")]
        avg_vol = sum(vols) / len(vols) if vols else 0.0
        try:
            vol = float(series[i].get("volume") or 0.0)
        except (TypeError, ValueError):
            vol = 0.0
        if avg_vol > 0 and vol / avg_vol >= volume_spike_factor:
            basis.append("volume_spike")

        if not basis:
            continue
        patterns = _map_basis(basis)
        out.append({
            "ticker": str(ticker).upper(),
            "scan_time": d,
            "alert_basis": basis,
            "pattern": _representative_pattern(patterns),
            "patterns": patterns,
            "direction": direction,
            "signal_score": None,
            "confidence_score": None,
            "price_change_pct": round(ret_pct, 4),
            "source": _SOURCE,
        })
    return out


def reconstruct_universe(
    archive_dir: str,
    recon_dir: str,
    *,
    strong_move_pct: float = 3.0,
    volume_spike_factor: float = 2.0,
    vol_window: int = 20,
    today: str | None = None,
) -> dict[str, Any]:
    """Reconstruct signals for every <TICKER>_5y.json in archive_dir and write
    snapshot-compatible recon_dir/<date>/watchlist_signals.json files (the shape
    signal_sources.load_historical_signal_snapshots reads). Never raises."""
    adir = Path(archive_dir)
    archives = sorted(adir.glob("*_5y.json")) if adir.is_dir() else []
    if not archives:
        return {"observe_only": _OBSERVE_ONLY, "status": "no_prices",
                "tickers": 0, "signals_total": 0, "archive_dir": archive_dir}

    by_date: dict[str, list[dict]] = {}
    tickers = 0
    for arc in archives:
        try:
            payload = json.loads(arc.read_text(encoding="utf-8"))
            ticker = str(payload.get("symbol") or arc.stem.split("_")[0])
            rows = payload.get("rows") or []
            sigs = reconstruct_signals(
                ticker, rows, strong_move_pct=strong_move_pct,
                volume_spike_factor=volume_spike_factor, vol_window=vol_window, today=today)
        except Exception:
            continue
        tickers += 1
        for s in sigs:
            by_date.setdefault(s["scan_time"], []).append(s)

    rdir = Path(recon_dir)
    signals_total = 0
    for d, sigs in by_date.items():
        out_dir = rdir / d
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "watchlist_signals.json").write_text(
            json.dumps({"results": sigs, "source": _SOURCE}), encoding="utf-8")
        signals_total += len(sigs)

    dates = sorted(by_date)
    span = 0
    if dates:
        from datetime import date as _date
        span = _date.fromisoformat(dates[-1]).toordinal() - _date.fromisoformat(dates[0]).toordinal()
    return {"observe_only": _OBSERVE_ONLY, "status": "ok", "tickers": tickers,
            "signals_total": signals_total, "dates": len(dates), "span_days": span,
            "recon_dir": recon_dir}


def _signals_for_date(sigs: list[dict], d: str) -> list[dict]:
    """The load-bearing fields of the signals emitted for date *d*, normalized for
    order so two reconstructions can be compared for equality."""
    keyed = [{k: s.get(k) for k in ("ticker", "scan_time", "alert_basis", "pattern",
                                    "patterns", "direction")}
             for s in sigs if s.get("scan_time") == d]
    return sorted(keyed, key=lambda s: (str(s["ticker"]), str(s["pattern"])))


def assert_no_lookahead(
    series_by_ticker: dict[str, list[dict]],
    *,
    reconstructor: Callable[..., list[dict]] = reconstruct_signals,
    sample: int = 10,
    **recon_kw: Any,
) -> dict[str, Any]:
    """Prove the reconstructor uses only data <= D: for a sample of dates D, the
    signals it emits for D from the FULL series must equal those from the series
    TRUNCATED at D. Any mismatch ⇒ look-ahead leakage. Never raises."""
    mismatches: list[dict] = []
    dates_checked = 0
    for ticker, rows in series_by_ticker.items():
        series = _sorted_series(rows)
        dates = [str(r["date"])[:10] for r in series]
        if len(dates) < 2:
            continue
        step = max(1, len(dates) // max(sample, 1))
        idxs = sorted(set(range(1, len(dates), step)))
        full = reconstructor(ticker, rows, **recon_kw)
        for i in idxs:
            d = dates[i]
            truncated = reconstructor(ticker, series[: i + 1], **recon_kw)
            dates_checked += 1
            if _signals_for_date(full, d) != _signals_for_date(truncated, d):
                mismatches.append({"ticker": ticker, "date": d})
    return {"observe_only": _OBSERVE_ONLY,
            "look_ahead_clean": not mismatches,
            "dates_checked": dates_checked,
            "mismatches": mismatches}


def write_reconstruction_audit(report: dict, base_dir: str = "outputs") -> str:
    """Persist the look-ahead audit to the HISTORICAL namespace."""
    from portfolio_automation.data_governance import OutputNamespace, safe_write_json
    return str(safe_write_json(OutputNamespace.HISTORICAL, "reconstruction_audit.json",
                               report, base_dir=base_dir))
