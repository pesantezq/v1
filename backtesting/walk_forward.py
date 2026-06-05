"""
Walk-forward / out-of-sample engine for the POC backtest  (additive | advisory-only | observe-only)

Pattern-Improvement Loop — Step 2. Stops reporting in-sample numbers: signals are
split by scan_time into rolling train/test folds and only the TEST (out-of-sample)
slice of each fold is evaluated, with sample-size gating and a Wilson 95% CI on
every reported hit rate.

The "train" window is a look-back / burn-in gate, not model fitting — this harness
replays already-emitted signals, so the train span exists only to guarantee each
evaluated signal sits strictly forward of a prior window. Signals living only in
the very first train window are never scored, which is what keeps the aggregate
genuinely out-of-sample.

Observe-only: it reads the public `FMPBacktester.simulate_signal_performance` API
and computes summary stats. It does not write artifacts and does not touch any
protected scoring/decision/allocation logic. Direction-aware win/loss labelling
(Step 1b) is orthogonal and slots into the underlying backtester later; this
engine reports whatever outcome the injected `bt` produces.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

_OBSERVE_ONLY = True  # hardcoded per repo observe-only policy (CLAUDE.md)
_Z95 = 1.959963984540054  # standard normal quantile for a two-sided 95% interval


def wilson_interval(wins: int, n: int, z: float = _Z95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (wins / n), as a
    (low, high) pair of proportions in [0, 1]. Dependency-free (no scipy).

    Returns (0.0, 0.0) for n == 0 (no data) so callers never divide by zero.
    Bounds are clamped to [0, 1] — at 0 or all wins the interval touches the
    edge rather than spilling outside it (the reason Wilson is preferred over
    the normal approximation for hit rates).
    """
    if n <= 0:
        return (0.0, 0.0)
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = (z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)) / denom
    low = max(0.0, center - half)
    high = min(1.0, center + half)
    return (low, high)


def _parse_date(value: Any) -> date | None:
    """Parse an ISO date or datetime string to a date; None if unparseable."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _evaluate_fold(window_signals: list[dict], bt: Any, forward_days: int) -> dict[str, Any] | None:
    """Evaluate one set of signals via the injected backtester and summarize
    the out-of-sample outcome. Returns None when nothing was evaluable."""
    perf = bt.simulate_signal_performance(window_signals, forward_days=forward_days)
    ret_key = f"return_{forward_days}d"
    rets = [r.get(ret_key) for r in perf.get("results", [])]
    rets = [r for r in rets if r is not None]
    n_eval = len(rets)
    if n_eval == 0:
        return None
    wins = sum(1 for r in rets if r > 0)
    low, high = wilson_interval(wins, n_eval)
    return {
        "n": n_eval,
        "wins": wins,
        "hit_rate": round(wins / n_eval * 100.0, 2),
        "avg_return": perf.get("avg_return", 0.0),
        "hit_rate_ci95": [round(low * 100.0, 2), round(high * 100.0, 2)],
    }


def walk_forward(
    signals: list[dict],
    bt: Any,
    *,
    train_days: int = 252,
    test_days: int = 63,
    step_days: int = 63,
    forward_days: int = 10,
    min_signals_per_fold: int = 30,
) -> dict[str, Any]:
    """Split *signals* by scan_time into rolling train/test folds; evaluate each
    TEST fold via ``bt.simulate_signal_performance`` and return per-fold plus
    aggregated OUT-OF-SAMPLE metrics, each annotated with n and a Wilson 95% CI
    on the hit rate.

    Folds whose test window holds fewer than ``min_signals_per_fold`` signals are
    reported with status 'insufficient' (no metrics). Empty or undatable input
    yields no folds and an 'insufficient' aggregate — never raises.
    """
    params = {
        "train_days": train_days, "test_days": test_days, "step_days": step_days,
        "forward_days": forward_days, "min_signals_per_fold": min_signals_per_fold,
    }

    dated: list[tuple[date, dict]] = []
    for sig in signals:
        d = _parse_date(sig.get("scan_time") or sig.get("signal_date"))
        if d is not None:
            dated.append((d, sig))

    if not dated:
        return {
            "observe_only": _OBSERVE_ONLY,
            "method": "walk_forward_out_of_sample",
            "params": params,
            "folds": [],
            "aggregate": {"status": "insufficient", "n": 0, "folds_ok": 0,
                          "folds_insufficient": 0, "hit_rate": None,
                          "avg_return": None, "hit_rate_ci95": None},
        }

    dated.sort(key=lambda pair: pair[0])
    earliest = dated[0][0].toordinal()
    latest = dated[-1][0].toordinal()

    folds: list[dict[str, Any]] = []
    oos_keys: set[tuple[str, str]] = set()
    oos_pool: list[dict] = []

    cursor = earliest
    fold_idx = 0
    # A fold exists while its test window can begin at or before the last signal.
    while cursor + train_days <= latest:
        test_start = cursor + train_days
        test_end = test_start + test_days
        window = [s for (d, s) in dated if test_start <= d.toordinal() < test_end]

        fold: dict[str, Any] = {
            "fold": fold_idx,
            "train_start": date.fromordinal(cursor).isoformat(),
            "train_end": date.fromordinal(test_start).isoformat(),
            "test_start": date.fromordinal(test_start).isoformat(),
            "test_end": date.fromordinal(test_end).isoformat(),
            "n": len(window),
            "status": "insufficient",
            "hit_rate": None,
            "avg_return": None,
            "hit_rate_ci95": None,
        }

        if len(window) >= min_signals_per_fold:
            summary = _evaluate_fold(window, bt, forward_days)
            if summary is not None:
                fold.update(status="ok", n=summary["n"], hit_rate=summary["hit_rate"],
                            avg_return=summary["avg_return"],
                            hit_rate_ci95=summary["hit_rate_ci95"])
                for s in window:
                    key = (str(s.get("ticker") or s.get("symbol") or "").upper(),
                           str(s.get("scan_time") or s.get("signal_date") or "")[:10])
                    if key not in oos_keys:
                        oos_keys.add(key)
                        oos_pool.append(s)

        folds.append(fold)
        cursor += step_days
        fold_idx += 1

    folds_ok = sum(1 for f in folds if f["status"] == "ok")
    folds_insufficient = len(folds) - folds_ok

    agg_summary = _evaluate_fold(oos_pool, bt, forward_days) if oos_pool else None
    if agg_summary is not None and folds_ok >= 1:
        aggregate = {
            "status": "ok", "n": agg_summary["n"], "folds_ok": folds_ok,
            "folds_insufficient": folds_insufficient,
            "hit_rate": agg_summary["hit_rate"], "avg_return": agg_summary["avg_return"],
            "hit_rate_ci95": agg_summary["hit_rate_ci95"],
        }
    else:
        aggregate = {
            "status": "insufficient", "n": 0, "folds_ok": folds_ok,
            "folds_insufficient": folds_insufficient, "hit_rate": None,
            "avg_return": None, "hit_rate_ci95": None,
        }

    return {
        "observe_only": _OBSERVE_ONLY,
        "method": "walk_forward_out_of_sample",
        "params": params,
        "folds": folds,
        "aggregate": aggregate,
    }


def oos_window_status(
    signals: list[dict],
    *,
    train_days: int = 252,
    test_days: int = 63,
    today: date | None = None,
) -> dict[str, Any]:
    """Calendar-day maturity countdown for the walk-forward OOS window.

    ``walk_forward`` measures its window in calendar-day ordinals (it compares
    ``date.toordinal()`` values), so ``train_days``/``test_days`` are CALENDAR
    days. The first fold's loop iterates once the observed span reaches
    ``train_days``; the first test window sits fully inside observed history once
    the span reaches ``train_days + test_days``. This reports how far the
    accumulated signal history is from that point.

    Pure and total: empty or undatable input yields ``calendar_days_observed=0``
    and ``folds_possible=False`` and never raises. ``today`` is injectable for
    deterministic tests (the caller passes ``date.today()``). The ETA is a
    calendar-day projection, flagged ``estimate: True``.
    """
    full_window_days = train_days + test_days
    dates = sorted(
        d for d in (_parse_date(s.get("scan_time") or s.get("signal_date")) for s in signals)
        if d is not None
    )
    if not dates:
        return {
            "calendar_days_observed": 0,
            "first_fold_threshold_days": train_days,
            "full_window_days": full_window_days,
            "folds_possible": False,
            "days_until_full_window": full_window_days,
            "full_window_eta": None,
            "earliest_signal": None,
            "latest_signal": None,
            "estimate": True,
        }
    earliest, latest = dates[0], dates[-1]
    observed = latest.toordinal() - earliest.toordinal()
    days_remaining = max(0, full_window_days - observed)
    ref = today or date.today()
    eta = date.fromordinal(ref.toordinal() + days_remaining).isoformat()
    return {
        "calendar_days_observed": observed,
        "first_fold_threshold_days": train_days,
        "full_window_days": full_window_days,
        "folds_possible": observed >= train_days,
        "days_until_full_window": days_remaining,
        "full_window_eta": eta,
        "earliest_signal": earliest.isoformat(),
        "latest_signal": latest.isoformat(),
        "estimate": True,
    }
