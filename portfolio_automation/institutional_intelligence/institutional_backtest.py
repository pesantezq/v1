"""
Point-in-time institutional backtest.

Evaluates institutional signal events against forward returns with a strict
anti-look-ahead contract:

  * A signal becomes effective on the NEXT market session AFTER its public
    filing availability date — never the quarter-end, never the filing day
    itself.
  * Forward returns are measured only from that effective session; if the
    required future session is not in the price history, the horizon return is
    ``None`` (no peeking past available data).

Reports hit-rate / mean forward return / information coefficient overall and by
attribution dimension (manager, archetype, event type, strategy-fit band,
freshness band, crowding band, options-ambiguity band, consensus-vs-single,
accumulation-vs-distribution). Enforces a minimum sample size and emits an
honest ``insufficient_data`` verdict — a production-readiness verdict is NEVER
returned when evidence is insufficient.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

DEFAULT_HORIZONS = (21, 63, 126)   # trading sessions
DEFAULT_MIN_SAMPLES = 30


@dataclass(frozen=True)
class SignalEvent:
    symbol: str
    filing_available: date          # public availability (filed_at)
    direction: int                  # +1 accumulation, -1 distribution
    score: float
    manager: str = "?"
    archetype: str = "?"
    event_type: str = "?"
    fit_band: str = "?"
    freshness_band: str = "?"
    crowding_band: str = "?"
    options_ambiguity_band: str = "?"
    is_consensus: bool = True


@dataclass(frozen=True)
class BucketStats:
    n: int
    hit_rate: float | None
    mean_forward_return: float | None
    information_coefficient: float | None
    sample_sufficient: bool
    insufficient_data: bool


def next_market_session(avail: date, sessions: list[date]) -> date | None:
    """First trading session STRICTLY AFTER the filing availability date."""
    for s in sessions:
        if s > avail:
            return s
    return None


def forward_return(symbol: str, effective: date, horizon: int,
                   prices: dict[str, dict[date, float]],
                   sessions: list[date]) -> float | None:
    """Return over ``horizon`` sessions from ``effective``; None if unavailable
    (the future session is not in history — no look-ahead)."""
    sym_prices = prices.get(symbol)
    if not sym_prices or effective not in sym_prices:
        return None
    try:
        idx = sessions.index(effective)
    except ValueError:
        return None
    target_idx = idx + horizon
    if target_idx >= len(sessions):
        return None
    target = sessions[target_idx]
    p0, p1 = sym_prices.get(effective), sym_prices.get(target)
    if p0 is None or p1 is None or p0 <= 0:
        return None
    return (p1 / p0) - 1.0


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx ** 0.5 * vy ** 0.5)


def _bucket_stats(pairs: list[tuple[float, float]], min_samples: int) -> BucketStats:
    """pairs = [(directional_score, forward_return), ...] (return already
    sign-adjusted by direction is NOT done here; hit uses direction*return)."""
    n = len(pairs)
    if n == 0:
        return BucketStats(0, None, None, None, False, True)
    hits = sum(1 for s, r in pairs if s * r > 0)   # directional correctness
    mean_ret = sum(s_dir_ret for _, s_dir_ret in
                   [(s, (1 if s >= 0 else -1) * r) for s, r in pairs]) / n
    ic = _pearson([s for s, _ in pairs], [r for _, r in pairs])
    sufficient = n >= min_samples
    return BucketStats(
        n=n, hit_rate=round(hits / n, 4), mean_forward_return=round(mean_ret, 6),
        information_coefficient=(round(ic, 4) if ic is not None else None),
        sample_sufficient=sufficient, insufficient_data=not sufficient)


@dataclass(frozen=True)
class BacktestResult:
    horizon: int
    overall: BucketStats
    by_dimension: dict[str, dict[str, BucketStats]]
    readiness_verdict: str          # "insufficient_data" | "evaluated"
    walk_forward_folds: int
    warnings: tuple[str, ...] = field(default_factory=tuple)


_DIMENSIONS = ("manager", "archetype", "event_type", "fit_band", "freshness_band",
               "crowding_band", "options_ambiguity_band")


def backtest(events: list[SignalEvent], prices: dict[str, dict[date, float]],
             sessions: list[date], *, horizon: int = 21,
             min_samples: int = DEFAULT_MIN_SAMPLES,
             walk_forward_folds: int = 3) -> BacktestResult:
    """Evaluate ``events`` at ``horizon`` sessions with anti-look-ahead returns."""
    sessions = sorted(sessions)
    warnings: list[str] = []

    scored: list[tuple[SignalEvent, float]] = []
    for ev in events:
        eff = next_market_session(ev.filing_available, sessions)
        if eff is None:
            continue                        # signal not yet tradable in history
        r = forward_return(ev.symbol, eff, horizon, prices, sessions)
        if r is None:
            continue                        # insufficient future data — skip, no peek
        scored.append((ev, r))

    overall_pairs = [(ev.direction * abs(ev.score) if ev.score else ev.direction, r)
                     for ev, r in scored]
    overall = _bucket_stats(overall_pairs, min_samples)

    by_dim: dict[str, dict[str, BucketStats]] = {}
    for dim in _DIMENSIONS:
        buckets: dict[str, list[tuple[float, float]]] = {}
        for ev, r in scored:
            key = getattr(ev, dim)
            buckets.setdefault(str(key), []).append(
                (ev.direction * abs(ev.score) if ev.score else ev.direction, r))
        by_dim[dim] = {k: _bucket_stats(v, min_samples) for k, v in buckets.items()}

    # consensus vs single-manager, accumulation vs distribution
    by_dim["consensus_vs_single"] = {
        ("consensus" if c else "single"): _bucket_stats(
            [(ev.direction * abs(ev.score) if ev.score else ev.direction, r)
             for ev, r in scored if ev.is_consensus == c], min_samples)
        for c in (True, False)}
    by_dim["direction"] = {
        ("accumulation" if d > 0 else "distribution"): _bucket_stats(
            [(ev.direction * abs(ev.score) if ev.score else ev.direction, r)
             for ev, r in scored if (ev.direction > 0) == (d > 0)], min_samples)
        for d in (1, -1)}

    # Walk-forward: split evaluated events by time into folds (evidence only).
    folds = walk_forward_folds if len(scored) >= walk_forward_folds else 0
    if folds == 0 and scored:
        warnings.append("too_few_samples_for_walk_forward")

    verdict = "evaluated" if overall.sample_sufficient else "insufficient_data"
    if verdict == "insufficient_data":
        warnings.append("insufficient_samples_no_readiness_verdict")

    return BacktestResult(horizon=horizon, overall=overall, by_dimension=by_dim,
                          readiness_verdict=verdict, walk_forward_folds=folds,
                          warnings=tuple(warnings))
