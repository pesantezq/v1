"""
POC Simulation Harness  (additive | advisory-only | observe-only)

Demonstrates evaluating signal & pattern efficacy on top of the existing
backtesting/fmp_backtester.py, adding a per-signal Sharpe-like ratio, an
edge-vs-random-baseline comparison, and a per-pattern breakdown.

Observe-only: it does NOT modify or override any protected scoring/decision/
allocation logic. It reads the public FMPBacktester API, computes extra summary
stats, and writes to the governed HISTORICAL namespace (outputs/backtest/) via
the data-governance safe writers. It never writes to the live (latest) namespace.

Offline by default: uses a deterministic synthetic price provider duck-typed to
FMPClient (no network, no API keys). Pass --live to use the real FMPClient. The
synthetic data embeds a mild, documented edge so the metrics are non-degenerate;
use --edge 0.0 for a pure-noise control. In synthetic mode the numbers describe
generated data, not the live strategy.

Usage:
    python -m backtesting.poc_simulation_harness
    python -m backtesting.poc_simulation_harness --signals 250 --seed 7
    python -m backtesting.poc_simulation_harness --edge 0.0      # noise control
    python -m backtesting.poc_simulation_harness --live          # real FMPClient
    python -m backtesting.poc_simulation_harness --no-write
"""

from __future__ import annotations

import argparse
import math
import random
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from backtesting.fmp_backtester import FMPBacktester

# Labels mirror config/signal_registry.yaml (display only; no scoring touched).
_PATTERNS = ["STRONG_MOVE_UP", "STRONG_MOVE_DOWN", "VOLUME_SPIKE", "BREAKOUT_PROXY"]
_OBSERVE_ONLY = True  # hardcoded per repo observe-only policy (CLAUDE.md)


class SyntheticPriceProvider:
    """Deterministic offline stand-in for FMPClient.

    Exposes get_historical_prices(symbol, years=...) returning FMP-shaped bars
    (newest-first). Each symbol gets a stable, seed-derived drift and vol.
    """

    def __init__(self, seed: int = 42, end: Optional[date] = None) -> None:
        self._seed = seed
        self._end = end or date.today()
        self._params: dict[str, tuple[float, float]] = {}

    def drift_vol(self, symbol: str) -> tuple[float, float]:
        """Stable (annual_drift, annual_vol) for a symbol, from the seed."""
        if symbol not in self._params:
            rng = random.Random(f"{self._seed}:{symbol}")
            mu = rng.uniform(-0.35, 0.55)    # wide drift -> demo-detectable
            sigma = rng.uniform(0.10, 0.22)  # modest vol -> drift not swamped
            self._params[symbol] = (mu, sigma)
        return self._params[symbol]

    def get_historical_prices(self, symbol: str, years: int = 5) -> list[dict]:
        mu, sigma = self.drift_vol(symbol)
        rng = random.Random(f"{self._seed}:path:{symbol}")
        days = max(int(years * 365) + 5, 90)
        start = self._end - timedelta(days=days - 1)
        d_mu, d_sigma = mu / 252.0, sigma / math.sqrt(252.0)
        rows: list[dict] = []
        price = 100.0
        for i in range(days):
            d = start + timedelta(days=i)
            price *= math.exp((d_mu - 0.5 * d_sigma ** 2) + d_sigma * rng.gauss(0.0, 1.0))
            close = round(price, 4)
            rows.append({
                "date": d.isoformat(), "open": round(close * 0.997, 4),
                "high": round(close * 1.008, 4), "low": round(close * 0.992, 4),
                "close": close, "adjClose": close, "volume": 1_000_000,
            })
        rows.reverse()  # FMP returns newest-first
        return rows


def _make_universe(n_symbols: int) -> list[str]:
    return [f"SYM{i:02d}" for i in range(n_symbols)]


def generate_signals(provider: SyntheticPriceProvider, n_signals: int, n_symbols: int,
                     seed: int, forward_days_long: int, edge: float = 0.7) -> list[dict]:
    """Deterministic synthetic signals.

    Two documented couplings make the metrics non-degenerate: higher-drift
    symbols are surfaced more often (weighted selection), and confidence is tied
    to symbol drift with strength `edge`. Use edge=0.0 for a pure-noise control.
    """
    rng = random.Random(f"{seed}:signals")
    universe = _make_universe(n_symbols)
    end = provider._end
    latest = end - timedelta(days=forward_days_long + 7)
    earliest = end - timedelta(days=int(2 * 365))
    span = max((latest - earliest).days, 30)
    drifts = {s: provider.drift_vol(s)[0] for s in universe}
    lo, hi = min(drifts.values()), max(drifts.values())
    rng_span = (hi - lo) or 1.0
    weights = [(drifts[s] - lo) / rng_span + 0.10 for s in universe]

    signals: list[dict] = []
    for _ in range(n_signals):
        sym = rng.choices(universe, weights=weights, k=1)[0]
        sig_date = earliest + timedelta(days=rng.randint(0, span))
        drift_norm = (drifts[sym] - lo) / rng_span
        confidence = max(0.0, min(1.0, (1.0 - edge) * rng.random() + edge * drift_norm))
        signals.append({
            "ticker": sym, "scan_time": sig_date.isoformat(),
            "signal_score": round(max(0.0, min(1.0, rng.random())), 4),
            "confidence_score": round(confidence, 4),
            "pattern": rng.choice(_PATTERNS),
        })
    return signals


def _baseline_signals(n: int, provider: SyntheticPriceProvider, n_symbols: int, seed: int) -> list[dict]:
    """Uniform-random symbol + random entry date -- a 'dart-throw' control."""
    rng = random.Random(f"{seed}:baseline")
    universe = _make_universe(n_symbols)
    end = provider._end
    earliest = end - timedelta(days=int(2 * 365))
    span = max((end - timedelta(days=45) - earliest).days, 30)
    return [{"ticker": rng.choice(universe),
             "scan_time": (earliest + timedelta(days=rng.randint(0, span))).isoformat()}
            for _ in range(n)]


def _sharpe_like(returns_pct: list[float], forward_days: int) -> dict[str, Any]:
    """Per-signal Sharpe-like ratio = mean/stdev of forward returns, plus a naive
    annualized estimate (sqrt(252/forward_days)). Illustrative proxy, not a
    portfolio Sharpe ratio."""
    clean = [r for r in returns_pct if r is not None]
    if len(clean) < 2:
        return {"per_signal": 0.0, "annualized_estimate": 0.0, "n": len(clean)}
    sd = statistics.stdev(clean)
    per = round(statistics.mean(clean) / sd, 4) if sd else 0.0
    ann = round(per * math.sqrt(252.0 / max(forward_days, 1)), 4) if sd else 0.0
    return {"per_signal": per, "annualized_estimate": ann, "n": len(clean)}


def _per_pattern_breakdown(results: list[dict], signals: list[dict], forward_days: int) -> list[dict]:
    """Group evaluated results by pattern label and summarize efficacy."""
    by_key = {(str(s.get("ticker", "")).upper(), str(s.get("scan_time", ""))[:10]):
              s.get("pattern", "UNKNOWN") for s in signals}
    groups: dict[str, list[float]] = {}
    for r in results:
        key = (str(r.get("ticker", "")).upper(), str(r.get("signal_date", ""))[:10])
        ret = r.get(f"return_{forward_days}d")
        if ret is not None:
            groups.setdefault(by_key.get(key, "UNKNOWN"), []).append(ret)
    out = []
    for pattern, rets in sorted(groups.items()):
        wins = [x for x in rets if x > 0]
        out.append({"pattern": pattern, "count": len(rets),
                    "hit_rate": round(len(wins) / len(rets) * 100, 2) if rets else 0.0,
                    "avg_return": round(statistics.mean(rets), 4) if rets else 0.0})
    return out


def run_poc(*, n_signals: int = 200, n_symbols: int = 12, seed: int = 42, years: int = 3,
            forward_days: int = 10, forward_days_long: int = 30, edge: float = 0.7,
            live: bool = False, write: bool = True, base_dir: str = "outputs") -> dict[str, Any]:
    """Run the POC simulation; optionally write artifacts to HISTORICAL. Returns payload."""
    if live:
        from fmp_client import FMPClient  # lazy: offline default needs no deps/keys
        provider: Any = FMPClient()
        mode = "live_fmp"
        syn = SyntheticPriceProvider(seed=seed)
        signals = generate_signals(syn, n_signals, n_symbols, seed, forward_days_long, edge=edge)
        baseline = _baseline_signals(n_signals, syn, n_symbols, seed)
        real = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "JPM", "XOM",
                "JNJ", "PG", "KO", "DIS"][:max(n_symbols, 1)]
        remap = {f"SYM{i:02d}": real[i % len(real)] for i in range(n_symbols)}
        for s in signals:
            s["ticker"] = remap.get(s["ticker"], s["ticker"])
        for b in baseline:
            b["ticker"] = remap.get(b["ticker"], b["ticker"])
    else:
        provider = SyntheticPriceProvider(seed=seed)
        mode = "synthetic_offline"
        signals = generate_signals(provider, n_signals, n_symbols, seed, forward_days_long, edge=edge)
        baseline = _baseline_signals(n_signals, provider, n_symbols, seed)

    bt = FMPBacktester(provider, years_default=years)
    perf = bt.simulate_signal_performance(signals, forward_days=forward_days,
                                          forward_days_long=forward_days_long)
    calib = bt.evaluate_confidence_calibration(signals, forward_days=forward_days)

    returns = [r for r in (x.get(f"return_{forward_days}d") for x in perf.get("results", []))
               if r is not None]
    sharpe = _sharpe_like(returns, forward_days)
    base_perf = bt.simulate_signal_performance(baseline, forward_days=forward_days)
    edge_vs_baseline = round(perf.get("avg_return", 0.0) - base_perf.get("avg_return", 0.0), 4)

    payload: dict[str, Any] = {
        "observe_only": _OBSERVE_ONLY,
        "advisory_only": True,
        "generated_by": "backtesting.poc_simulation_harness",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "disclaimer": ("Proof-of-concept. In synthetic mode these numbers describe "
                       "generated data, not the live strategy. No trades implied."),
        "params": {"n_signals": n_signals, "n_symbols": n_symbols, "seed": seed,
                   "years": years, "forward_days": forward_days,
                   "forward_days_long": forward_days_long, "edge": edge},
        "performance": perf,
        "calibration": calib,
        "added_metrics": {
            "sharpe_like": sharpe,
            "edge_vs_random_baseline_pct": edge_vs_baseline,
            "baseline_avg_return_pct": base_perf.get("avg_return", 0.0),
            "per_pattern": _per_pattern_breakdown(perf.get("results", []), signals, forward_days),
        },
    }
    if write:
        _write_artifacts(payload, base_dir=base_dir)
    return payload


def _markdown_summary(p: dict[str, Any]) -> str:
    perf, cal, am = p["performance"], p["calibration"], p["added_metrics"]
    cstate = "well-calibrated" if cal.get("well_calibrated") else "not calibrated"
    L = [f"# POC Simulation Results", "", f"> {p['disclaimer']}", "",
         f"- Mode: {p['mode']}  |  Generated: {p['generated_at']}",
         f"- Params: {p['params']}", "", "## Headline metrics", "",
         "| Metric | Value |", "|---|---|",
         f"| Signals evaluated | {perf.get('evaluated')} / {perf.get('total_signals')} |",
         f"| Hit rate | {perf.get('hit_rate')}% |",
         f"| Avg forward return | {perf.get('avg_return')}% |",
         f"| Win/loss ratio | {perf.get('win_loss_ratio')} |",
         f"| Sharpe-like (per-signal) | {am['sharpe_like']['per_signal']} |",
         f"| Sharpe-like (annualized est.) | {am['sharpe_like']['annualized_estimate']} |",
         f"| Edge vs. random baseline | {am['edge_vs_random_baseline_pct']}% |",
         f"| Calibration slope | {cal.get('calibration_slope')} ({cstate}) |",
         "", "## Confidence calibration buckets", "",
         "| Band | Count | Hit rate | Avg return |", "|---|---|---|---|"]
    for b in cal.get("buckets", []):
        L.append(f"| {b['label']} | {b['count']} | {b['hit_rate']}% | {b['avg_return']}% |")
    L += ["", "## Per-pattern efficacy", "", "| Pattern | Count | Hit rate | Avg return |",
          "|---|---|---|---|"]
    for r in am["per_pattern"]:
        L.append(f"| {r['pattern']} | {r['count']} | {r['hit_rate']}% | {r['avg_return']}% |")
    return "\n".join(L) + "\n"


def _write_artifacts(payload: dict[str, Any], base_dir: str = "outputs") -> None:
    """Write JSON + Markdown to the HISTORICAL namespace (outputs/backtest/)."""
    from portfolio_automation.data_governance import (
        OutputNamespace, safe_write_json, safe_write_text,
    )
    safe_write_json(OutputNamespace.HISTORICAL, "poc_simulation_results.json",
                    payload, base_dir=base_dir)
    safe_write_text(OutputNamespace.HISTORICAL, "poc_simulation_results.md",
                    _markdown_summary(payload), base_dir=base_dir)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="poc_simulation_harness",
                                 description="Observe-only POC backtest/simulation harness.")
    ap.add_argument("--signals", type=int, default=200)
    ap.add_argument("--symbols", type=int, default=12)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--forward-days", type=int, default=10)
    ap.add_argument("--forward-days-long", type=int, default=30)
    ap.add_argument("--edge", type=float, default=0.7,
                    help="synthetic confidence->drift coupling (0.0 = pure-noise control)")
    ap.add_argument("--live", action="store_true", help="use real FMPClient (needs FMP_API_KEY)")
    ap.add_argument("--no-write", action="store_true")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    a = _build_parser().parse_args(argv)
    p = run_poc(n_signals=a.signals, n_symbols=a.symbols, seed=a.seed, years=a.years,
                forward_days=a.forward_days, forward_days_long=a.forward_days_long,
                edge=a.edge, live=a.live, write=not a.no_write)
    perf, am = p["performance"], p["added_metrics"]
    print("[poc] mode={m} evaluated={e}/{t} hit_rate={h}% avg_return={a}% sharpe={s} "
          "edge_vs_baseline={ed}% calib_slope={c}".format(
              m=p["mode"], e=perf["evaluated"], t=perf["total_signals"], h=perf["hit_rate"],
              a=perf["avg_return"], s=am["sharpe_like"]["per_signal"],
              ed=am["edge_vs_random_baseline_pct"], c=p["calibration"].get("calibration_slope")))
    if not a.no_write:
        print("[poc] wrote outputs/backtest/poc_simulation_results.{json,md} (HISTORICAL)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
