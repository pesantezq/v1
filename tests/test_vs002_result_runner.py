"""Deterministic VS-002 result runner — synthetic certification.

Every datum here is FABRICATED and labelled as such. No test opens, reads,
enumerates, discovers, or derives anything from the real frozen VS-002 evidence
package. The runner is proven only against in-memory synthetic ValidatedSnapshots
constructed from scratch, plus the committed frozen preregistration (reading the
preregistration JSON is permitted; it is the scientific authority, not evidence).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from portfolio_automation.vs002_evidence import contracts as C
from portfolio_automation.vs002_evidence.consumer import ValidatedSnapshot
from portfolio_automation.vs002_evidence import result_runner as RR
from portfolio_automation.vs002_evidence import result_contract as RC
from portfolio_automation.vs002_evidence.result_contract import (
    CriterionOutcome, EvidenceIdentity, H1Status, H2Status, NoActionStatus,
    SkillOutcome,
)

REPO = Path(__file__).resolve().parent.parent
PREREG = RR.load_preregistration(REPO)

import importlib.util as _ilu


def _load_student_t_generator():
    """Load the deterministic generator SCRIPT (provenance/verification tool).

    It lives under scripts/ and is deliberately NOT imported by the runtime
    result_contract/result_runner modules."""
    path = REPO / "scripts" / "generate_vs002_student_t_table.py"
    spec = _ilu.spec_from_file_location("vs002_student_t_generator", path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
GEN_AT = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

# Frozen identity strings (metadata only — never used for discovery/IO).
_EB = PREREG["binding_core"]["evidence_binding"]
FROZEN_IDENTITY = EvidenceIdentity(
    package_id=_EB["package_id"],
    package_transport_digest=_EB["package_transport_digest"],
    source_production_sha=_EB["source_production_sha"],
    evidence_schema_version=_EB["evidence_schema_version"],
)
PKG = _EB["package_id"]


# ───────────────────────────── synthetic builders ───────────────────────────
@dataclass
class RowSpec:
    ticker: str
    signal_score: Optional[float]
    stock_out: float          # recorded outcome_return_7d (percentage points)
    time: str = "10:00:00"


@dataclass
class CohortSpec:
    day_ordinal: int          # distinct signal session date (as date.toordinal)
    spy_out: float            # recorded SPY outcome_return_7d (pp)
    rows: list[RowSpec]


def _d(ordinal: int) -> str:
    return date.fromordinal(ordinal).isoformat()


# A deterministic, nonconstant SPY daily-return cycle (decimal ratios), Var>0.
_RET_CYCLE = [0.010, -0.006, 0.008, -0.004, 0.005, -0.009, 0.007]


def _spy_adj_series(dates: list[str], start: float = 100.0) -> dict[str, float]:
    px = {}
    p = start
    for i, dt in enumerate(dates):
        if i > 0:
            p = p * (1.0 + _RET_CYCLE[i % len(_RET_CYCLE)])
        px[dt] = p
    return px


def _stock_adj_series(dates: list[str], spy_px: dict[str, float], beta: float,
                      start: float = 50.0) -> dict[str, float]:
    """adj_close path with r_stock(t) = beta * r_spy(t) exactly → beta is exact."""
    px = {}
    p = start
    for i, dt in enumerate(dates):
        if i > 0:
            r_spy = spy_px[dates[i]] / spy_px[dates[i - 1]] - 1.0
            p = p * (1.0 + beta * r_spy)
        px[dt] = p
    return px


def _bars(symbol: str, series: dict[str, float]) -> list[dict]:
    return [{"symbol": symbol, "session_date": dt, "adj_close": px, "volume": 1000}
            for dt, px in series.items()]


def build_snapshot(cohorts: list[CohortSpec], *, betas: dict[str, float],
                   eligible: Optional[list[str]] = None,
                   lookback: int = 260, package_id: str = PKG,
                   extra_bars: Optional[dict[str, list[dict]]] = None,
                   manifest_overrides: Optional[dict] = None) -> ValidatedSnapshot:
    """Assemble an in-memory ValidatedSnapshot from fabricated cohorts."""
    benchmark = C.BENCHMARK
    tickers = sorted({r.ticker for c in cohorts for r in c.rows})
    if eligible is None:
        eligible = tickers
    first = min(c.day_ordinal for c in cohorts)
    last = max(c.day_ordinal for c in cohorts)
    bar_ords = list(range(first - lookback, last + 1))
    bar_dates = [_d(o) for o in bar_ords]

    spy_px = _spy_adj_series(bar_dates)
    bars: list[dict] = _bars(benchmark, spy_px)
    for t in tickers:
        bars += _bars(t, _stock_adj_series(bar_dates, spy_px, betas.get(t, 1.0)))
    if extra_bars:
        for sym, blist in extra_bars.items():
            bars = [b for b in bars if b["symbol"] != sym] + blist

    signals: list[dict] = []
    spy_seen: dict[str, float] = {}
    for c in cohorts:
        dstr = _d(c.day_ordinal)
        for r in c.rows:
            st = f"{dstr}T{r.time}Z"
            signals.append({
                "ticker": r.ticker, "signal_time": st,
                "signal_score": r.signal_score,
                "price_at_signal": 50.0, "outcome_return_7d": r.stock_out,
                "outcome_price_7d": 51.0, "evaluated_at_7d": f"{_d(c.day_ordinal + 7)}T{r.time}Z",
                "prediction_intent": "up", "data_mode": "synthetic",
            })
            if st not in spy_seen:
                spy_seen[st] = c.spy_out
                signals.append({
                    "ticker": benchmark, "signal_time": st, "signal_score": None,
                    "price_at_signal": 400.0, "outcome_return_7d": c.spy_out,
                    "outcome_price_7d": 404.0, "evaluated_at_7d": f"{_d(c.day_ordinal + 7)}T{r.time}Z",
                    "prediction_intent": "benchmark", "data_mode": "synthetic",
                })
    manifest = {
        "schema_version": C.SCHEMA_VERSION,
        "package_id": package_id,
        "signal_evidence_cutoff": _d(last),
        "eligible_universe": list(eligible),
        "bar_endpoint": C.AUTHORIZED_ENDPOINT,
        "risk_free_rate_7d": dict(C.RISK_FREE_RATE_7D_ASSUMPTION),
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    return ValidatedSnapshot(root=Path("/synthetic/vs002"), manifest=manifest,
                             signals=signals, returns=[], bars=bars)


def _run(snap, **kw):
    return RR.run(snap, PREREG, evidence_identity=FROZEN_IDENTITY,
                  generated_at=GEN_AT, **kw)


# Scenario helpers ------------------------------------------------------------
def _uniform_cohorts(n: int, *, spy_out=1.0, scores=(0.2, 0.5, 0.8),
                     stock_base=(3.3, 3.5, 3.7), step=8, start_ord=739000,
                     ticker="AAA", jitter=0.0) -> list[CohortSpec]:
    """n cohorts spaced `step` days apart; 3 rows each with increasing
    (score, stock_out) so per-cohort Spearman IC = +1."""
    out = []
    for i in range(n):
        rows = [RowSpec(ticker, scores[k], stock_base[k] + jitter * i,
                        time=f"1{k}:00:00")
                for k in range(len(scores))]
        out.append(CohortSpec(start_ord + i * step, spy_out, rows))
    return out


# ───────────────────────────────── happy path ───────────────────────────────
def test_happy_path_all_gates_met():
    snap = build_snapshot(_uniform_cohorts(12, jitter=0.01), betas={"AAA": 1.0})
    res = _run(snap)
    assert res.preregistration_verified is True
    assert res.population.base_population_rows == 36  # 12 cohorts * 3 rows
    assert len(res.population.selected_cohort_dates) == 12
    assert res.h1.status is H1Status.MET
    assert res.h2.status is H2Status.MET
    assert res.no_action.status is NoActionStatus.BEAT
    assert res.criterion_outcome is CriterionOutcome.MET
    assert res.skill_outcome is SkillOutcome.PRESENT
    # beta==1.0, spy_out==1.0 → expected_market==1.0; net_excess=stock_out-1.1
    assert res.h1.experiment_mean is not None and res.h1.experiment_mean > 0
    assert res.h2.point_ic is not None and res.h2.point_ic == pytest.approx(1.0, abs=1e-9)


def test_beta_and_friction_arithmetic_is_exact():
    # one cohort worth of rows is enough to check a single net_excess value
    snap = build_snapshot([CohortSpec(739000, spy_out=2.0,
                                      rows=[RowSpec("AAA", 0.5, 5.0)])],
                          betas={"AAA": 1.5})
    rows, excl, evaluated = RR._prepare_rows(snap, RR.verify_preregistration(PREREG))
    assert evaluated == 1 and not excl and len(rows) == 1
    r = rows[0]
    # expected_market = 0 + 1.5*(2.0-0) = 3.0 ; gross = 5.0-3.0 = 2.0 ; net = 2.0-0.1 = 1.9
    assert r.net_risk_adjusted_excess_pct == pytest.approx(1.9, abs=1e-9)
    # NO_ACTION applies NO beta: net = 5.0 - 0.1 = 4.9
    assert r.net_no_action_return_pct == pytest.approx(4.9, abs=1e-9)


# ───────────────────────────────── H1 gates ─────────────────────────────────
def _const_cohorts(n, net_target, *, spy_out=1.0, beta_ticker="AAA", step=8,
                   start_ord=739000, spread=0.0):
    """n cohorts, one row each; stock_out set so net_excess≈net_target (±spread)."""
    # with beta=1, spy_out=1: net = stock_out - 1 - 0.1 ; stock_out = net + 1.1
    out = []
    for i in range(n):
        tgt = net_target + (spread if i % 2 else -spread)
        out.append(CohortSpec(start_ord + i * step, spy_out,
                              [RowSpec(beta_ticker, 0.5, tgt + 1.1)]))
    return out


def test_h1_positive_point_and_positive_lower_is_met():
    snap = build_snapshot(_const_cohorts(12, 2.0, spread=0.1), betas={"AAA": 1.0})
    res = _run(snap)
    assert res.h1.status is H1Status.MET
    assert res.h1.interval.ci_low > 0


def test_h1_positive_point_but_nonpositive_lower_is_not_met():
    # mean small positive but wide spread drags CI lower below 0
    snap = build_snapshot(_const_cohorts(12, 0.2, spread=2.0), betas={"AAA": 1.0})
    res = _run(snap)
    assert res.h1.experiment_mean > 0
    assert res.h1.interval.ci_low <= 0
    assert res.h1.status is H1Status.NOT_MET


def test_h1_negative_point_is_not_met():
    snap = build_snapshot(_const_cohorts(12, -1.0, spread=0.1), betas={"AAA": 1.0})
    res = _run(snap)
    assert res.h1.experiment_mean < 0
    assert res.h1.status is H1Status.NOT_MET


def test_nine_cohorts_is_inconclusive_ten_is_computable():
    nine = build_snapshot(_const_cohorts(9, 2.0, spread=0.1), betas={"AAA": 1.0})
    r9 = _run(nine)
    assert r9.h1.status is H1Status.INCONCLUSIVE
    assert r9.criterion_outcome is CriterionOutcome.INCONCLUSIVE
    ten = build_snapshot(_const_cohorts(10, 2.0, spread=0.1), betas={"AAA": 1.0})
    r10 = _run(ten)
    assert r10.h1.status is H1Status.MET
    assert r10.h1.interval.n == 10


# ───────────────────────────────── NO_ACTION ────────────────────────────────
def test_no_action_beat_and_not_beat_and_inconclusive():
    beat = _run(build_snapshot(_const_cohorts(12, 2.0, spread=0.1), betas={"AAA": 1.0}))
    assert beat.no_action.status is NoActionStatus.BEAT
    # make net_no_action small positive mean but wide spread → lower<=0.
    # net_no_action = stock_out - 0.1 ; target mean ~0.2 → stock_out ~0.3
    wide = [CohortSpec(739000 + i * 8, 1.0,
                       [RowSpec("AAA", 0.5, (0.3 + (3.0 if i % 2 else -3.0)))])
            for i in range(12)]
    rw = _run(build_snapshot(wide, betas={"AAA": 1.0}))
    assert rw.no_action.mean_net_no_action_return > 0
    assert rw.no_action.interval.ci_low <= 0
    assert rw.no_action.status is NoActionStatus.NOT_BEAT
    nine = _run(build_snapshot(_const_cohorts(9, 2.0, spread=0.1), betas={"AAA": 1.0}))
    assert nine.no_action.status is NoActionStatus.INCONCLUSIVE


# ───────────────────────────────── H2 gates ─────────────────────────────────
def test_h2_positive_mean_ic_is_met():
    res = _run(build_snapshot(_uniform_cohorts(12, jitter=0.01), betas={"AAA": 1.0}))
    assert res.h2.valid_cohort_ic_count == 12
    assert res.h2.point_ic == pytest.approx(1.0, abs=1e-9)
    assert res.h2.status is H2Status.MET


def test_h2_negative_mean_ic_is_not_met():
    # reverse score ordering vs outcome → per-cohort rho = -1
    cohorts = _uniform_cohorts(12, scores=(0.8, 0.5, 0.2), stock_base=(3.3, 3.5, 3.7),
                               jitter=0.01)
    res = _run(build_snapshot(cohorts, betas={"AAA": 1.0}))
    assert res.h2.point_ic == pytest.approx(-1.0, abs=1e-9)
    assert res.h2.status is H2Status.NOT_MET


def test_h2_fewer_than_ten_valid_cohort_ics_is_inconclusive():
    # 12 cohorts but only 1 row each → no cohort has >=3 pairs → 0 valid ICs
    cohorts = [CohortSpec(739000 + i * 8, 1.0, [RowSpec("AAA", 0.5, 3.5)])
               for i in range(12)]
    res = _run(build_snapshot(cohorts, betas={"AAA": 1.0}))
    assert res.h2.valid_cohort_ic_count == 0
    assert res.h2.status is H2Status.INCONCLUSIVE
    assert res.criterion_outcome is CriterionOutcome.INCONCLUSIVE


def test_h2_cohort_with_constant_scores_or_outcomes_excluded():
    binding = RR.verify_preregistration(PREREG)
    # constant scores
    const_scores = _uniform_cohorts(12, scores=(0.5, 0.5, 0.5), jitter=0.01)
    r1 = _run(build_snapshot(const_scores, betas={"AAA": 1.0}))
    assert r1.h2.valid_cohort_ic_count == 0 and r1.h2.status is H2Status.INCONCLUSIVE
    # constant outcomes (same stock_out across the 3 rows → constant net_excess)
    const_out = _uniform_cohorts(12, scores=(0.2, 0.5, 0.8),
                                 stock_base=(3.5, 3.5, 3.5), jitter=0.0)
    r2 = _run(build_snapshot(const_out, betas={"AAA": 1.0}))
    assert r2.h2.valid_cohort_ic_count == 0 and r2.h2.status is H2Status.INCONCLUSIVE


def test_h2_point_positive_with_reporting_lower_below_zero_still_met():
    # Per-cohort IC noisy around a small positive mean so the reporting CI lower
    # is <= 0, proving the reporting interval never gates H2.
    cohorts = []
    for i in range(12):
        # alternate strong +1 and weak/negative correlation cohorts
        if i % 2 == 0:
            sc, so = (0.2, 0.5, 0.8), (3.3, 3.5, 3.7)   # rho=+1
        else:
            sc, so = (0.2, 0.5, 0.8), (3.7, 3.4, 3.6)   # rho != +1, can be <=0
        rows = [RowSpec("AAA", sc[k], so[k], time=f"1{k}:00:00") for k in range(3)]
        cohorts.append(CohortSpec(739000 + i * 8, 1.0, rows))
    res = _run(build_snapshot(cohorts, betas={"AAA": 1.0}))
    assert res.h2.point_ic is not None and res.h2.point_ic > 0
    assert res.h2.reporting_interval is not None
    assert res.h2.reporting_interval.ci_low <= 0
    assert res.h2.status is H2Status.MET  # gate is point IC only


def test_h2_tie_handling_uses_average_ranks():
    # tied scores must use average ranks (standard Spearman)
    xs = [1.0, 2.0, 2.0, 4.0]
    ys = [1.0, 3.0, 2.0, 4.0]
    rho = RR.spearman_rho(xs, ys)
    # average ranks x=[1,2.5,2.5,4], y=[1,3,2,4]; compute expected Pearson of ranks
    import statistics
    rx = [1, 2.5, 2.5, 4]; ry = [1, 3, 2, 4]
    mx = sum(rx) / 4; my = sum(ry) / 4
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(4))
    den = math.sqrt(sum((v - mx) ** 2 for v in rx) * sum((v - my) ** 2 for v in ry))
    assert rho == pytest.approx(num / den, abs=1e-12)


# ─────────────────────────────── exact SPY join ─────────────────────────────
def test_exact_spy_timestamp_match_required():
    # stock at 10:00:00 but SPY only at 11:00:00 same date → excluded (no fallback)
    base = 739000
    dstr = _d(base)
    snap = build_snapshot([CohortSpec(base, 1.0, [RowSpec("AAA", 0.5, 3.5, time="10:00:00")])],
                          betas={"AAA": 1.0})
    snap.signals[:] = [s for s in snap.signals if s["ticker"] != "SPY"]
    snap.signals.append({"ticker": "SPY", "signal_time": f"{dstr}T11:00:00Z",
                         "signal_score": None, "price_at_signal": 400.0,
                         "outcome_return_7d": 1.0, "outcome_price_7d": 404.0,
                         "evaluated_at_7d": f"{dstr}T11:00:00Z",
                         "prediction_intent": "benchmark", "data_mode": "synthetic"})
    rows, excl, evaluated = RR._prepare_rows(snap, RR.verify_preregistration(PREREG))
    assert rows == [] and excl["no_exact_spy_timestamp_match"] == 1


def test_missing_spy_increments_exclusion_and_can_force_inconclusive():
    cohorts = _const_cohorts(12, 2.0, spread=0.1)
    snap = build_snapshot(cohorts, betas={"AAA": 1.0})
    # delete every SPY signal row → no exact matches at all
    snap.signals[:] = [s for s in snap.signals if s["ticker"] != "SPY"]
    res = _run(snap)
    assert res.population.base_population_rows == 0
    assert res.population.exclusion_counts.get("no_exact_spy_timestamp_match", 0) == 12
    assert res.criterion_outcome is CriterionOutcome.INCONCLUSIVE


# ───────────────────────────────── beta rules ───────────────────────────────
def test_beta_exactly_60_joint_works_59_excluded():
    binding = RR.verify_preregistration(PREREG)
    # SPY has a long series; stock has bars on only K sessions strictly-before.
    boundary_ord = 739000
    boundary = _d(boundary_ord)
    spy_dates = [_d(boundary_ord - k) for k in range(300, 0, -1)]
    spy_px = _spy_adj_series(spy_dates)
    spy_bars = _bars("SPY", spy_px)
    def stock_with_joint(njoint):
        # stock needs njoint joint RETURNS → njoint+1 consecutive shared bars
        shared = spy_dates[-(njoint + 1):]
        px = {d: spy_px[d] for d in shared}  # beta 1 on shared dates
        return _bars("AAA", px)
    ok = RR.compute_beta(stock_with_joint(60), spy_bars, boundary_date=boundary,
                         window_sessions=binding.beta_window_sessions,
                         minimum_joint_observations=binding.minimum_joint_observations)
    assert ok[1] == 60 and ok[0] == pytest.approx(1.0, abs=1e-9)
    with pytest.raises(RR.BetaUncomputable) as ei:
        RR.compute_beta(stock_with_joint(59), spy_bars, boundary_date=boundary,
                        window_sessions=binding.beta_window_sessions,
                        minimum_joint_observations=binding.minimum_joint_observations)
    assert "insufficient_joint_observations" in str(ei.value)


def test_beta_excludes_signal_date_and_after():
    binding = RR.verify_preregistration(PREREG)
    boundary_ord = 739000
    boundary = _d(boundary_ord)
    # bars include the signal date itself and a later date; both must be ignored
    dates = [_d(boundary_ord - k) for k in range(100, -3, -1)]  # ... boundary, +1, +2
    spy_px = _spy_adj_series(dates)
    spy_bars = _bars("SPY", spy_px)
    stock_bars = _bars("AAA", spy_px)
    beta, n = RR.compute_beta(stock_bars, spy_bars, boundary_date=boundary,
                              window_sessions=binding.beta_window_sessions,
                              minimum_joint_observations=binding.minimum_joint_observations)
    # only sessions strictly before boundary count: 100 prior bars → 99 returns
    assert n == 99
    # and no windowed date is >= boundary
    sw = [b for b in stock_bars if b["session_date"] < boundary]
    assert all(b["session_date"] < boundary for b in sw)


def test_beta_zero_benchmark_variance_fails_closed():
    binding = RR.verify_preregistration(PREREG)
    boundary_ord = 739000
    boundary = _d(boundary_ord)
    dates = [_d(boundary_ord - k) for k in range(100, 0, -1)]
    flat = {d: 100.0 for d in dates}           # constant SPY → zero variance
    with pytest.raises(RR.BetaUncomputable) as ei:
        RR.compute_beta(_bars("AAA", {d: 100.0 + i for i, d in enumerate(dates)}),
                        _bars("SPY", flat), boundary_date=boundary,
                        window_sessions=binding.beta_window_sessions,
                        minimum_joint_observations=binding.minimum_joint_observations)
    assert "zero_benchmark_variance" in str(ei.value)


def test_beta_252_session_cap_respected():
    binding = RR.verify_preregistration(PREREG)
    boundary_ord = 739000
    boundary = _d(boundary_ord)
    # 400 prior sessions; only the trailing 252 may enter → 251 returns max
    dates = [_d(boundary_ord - k) for k in range(400, 0, -1)]
    spy_px = _spy_adj_series(dates)
    beta, n = RR.compute_beta(_bars("AAA", spy_px), _bars("SPY", spy_px),
                              boundary_date=boundary,
                              window_sessions=binding.beta_window_sessions,
                              minimum_joint_observations=binding.minimum_joint_observations)
    assert n == 251   # 252 windowed bars → 251 consecutive returns


def test_beta_pairs_only_on_identical_prev_cur_session_pairs():
    # REPAIR A case 1: when both series share a consecutive session grid, every
    # native (prev, cur) pair matches and is included.
    base = 100100
    ords = list(range(base, base + 6))
    spy = _bars("SPY", {_d(o): 100.0 + i for i, o in enumerate(ords)})
    stock = _bars("AAA", {_d(o): 50.0 + i for i, o in enumerate(ords)})
    matched = set(RR._consecutive_returns(stock)) & set(RR._consecutive_returns(spy))
    assert matched == {(_d(ords[i]), _d(ords[i + 1])) for i in range(len(ords) - 1)}


def test_beta_excludes_pair_when_stock_misses_an_intermediate_benchmark_session():
    # REPAIR A case 2: stock missing d3 → its native return is (d2 -> d4); SPY has
    # (d2 -> d3) and (d3 -> d4). The mismatched (d2 -> d4) pair must be EXCLUDED,
    # never compressed into a benchmark daily return.
    base = 200000
    spy_ords = list(range(base, base + 6))                 # d0..d5
    stock_ords = [base, base + 1, base + 2, base + 4, base + 5]  # missing d3
    spy = _bars("SPY", {_d(o): 100.0 + i for i, o in enumerate(spy_ords)})
    stock = _bars("AAA", {_d(o): 50.0 + (o - base) for o in stock_ords})
    sr = RR._consecutive_returns(stock)
    br = RR._consecutive_returns(spy)
    gap = (_d(base + 2), _d(base + 4))
    assert gap in sr                                   # stock's native multi-session pair
    assert gap not in br                               # SPY never has it
    assert (_d(base + 2), _d(base + 3)) in br and (_d(base + 3), _d(base + 4)) in br
    assert gap not in (set(sr) & set(br))              # EXCLUDED
    assert (set(sr) & set(br)) == {(_d(base), _d(base + 1)),
                                   (_d(base + 1), _d(base + 2)),
                                   (_d(base + 4), _d(base + 5))}


def test_beta_excludes_pair_when_benchmark_misses_an_intermediate_stock_session():
    # REPAIR A case 3: symmetric — SPY missing d3; stock has (d2->d3),(d3->d4),
    # SPY has (d2->d4). None of the three pair.
    base = 210000
    stock_ords = list(range(base, base + 6))
    spy_ords = [base, base + 1, base + 2, base + 4, base + 5]  # SPY missing d3
    stock = _bars("AAA", {_d(o): 50.0 + (o - base) for o in stock_ords})
    spy = _bars("SPY", {_d(o): 100.0 + (o - base) for o in spy_ords})
    sr = RR._consecutive_returns(stock)
    br = RR._consecutive_returns(spy)
    assert (_d(base + 2), _d(base + 4)) in br           # SPY's native multi-session pair
    assert (_d(base + 2), _d(base + 3)) in sr and (_d(base + 3), _d(base + 4)) in sr
    matched = set(sr) & set(br)
    assert (_d(base + 2), _d(base + 4)) not in matched
    assert (_d(base + 2), _d(base + 3)) not in matched
    assert (_d(base + 3), _d(base + 4)) not in matched
    assert matched == {(_d(base), _d(base + 1)), (_d(base + 1), _d(base + 2)),
                       (_d(base + 4), _d(base + 5))}


def test_beta_computable_when_enough_exact_pairs_survive_a_gap():
    # REPAIR A case 4: a single intra-window gap drops exactly one pair; the rest
    # (>=60) still pair exactly, so beta is computable over matched pairs only.
    binding = RR.verify_preregistration(PREREG)
    base = 300000
    boundary = _d(base + 200)
    spy_ords = list(range(base, base + 64))              # 64 sessions, 63 SPY pairs
    stock_ords = [o for o in spy_ords if o != base + 30]  # one intra-window gap
    spy = _bars("SPY", _spy_adj_series([_d(o) for o in spy_ords]))
    stock = _bars("AAA", _spy_adj_series([_d(o) for o in stock_ords]))
    beta, n = RR.compute_beta(stock, spy, boundary_date=boundary,
                              window_sessions=binding.beta_window_sessions,
                              minimum_joint_observations=binding.minimum_joint_observations)
    assert n == 61   # 62 stock pairs minus the single gap pair SPY lacks
    assert math.isfinite(beta)


# ───────────────────────────────── cohorts ──────────────────────────────────
def test_greedy_cohorts_seven_day_boundary_and_six_day_rejection():
    # dates: 0, +6, +13 → greedy picks 0 and +13 (skips +6, only 6 days)
    base = 739000
    rows = lambda: [RowSpec("AAA", sc, so, time=f"1{k}:00:00")
                    for k, (sc, so) in enumerate([(0.2, 3.3), (0.5, 3.5), (0.8, 3.7)])]
    cohorts = [CohortSpec(base, 1.0, rows()),
               CohortSpec(base + 6, 1.0, rows()),
               CohortSpec(base + 13, 1.0, rows())]
    snap = build_snapshot(cohorts, betas={"AAA": 1.0})
    res = _run(snap)
    assert list(res.population.selected_cohort_dates) == [_d(base), _d(base + 13)]
    # exactly-7-days apart is eligible
    cohorts7 = [CohortSpec(base, 1.0, rows()), CohortSpec(base + 7, 1.0, rows())]
    res7 = _run(build_snapshot(cohorts7, betas={"AAA": 1.0}))
    assert list(res7.population.selected_cohort_dates) == [_d(base), _d(base + 7)]


def test_cohorts_equal_weighted_despite_unequal_row_counts():
    # cohort A: 1 row net=+10 ; cohorts B..K: 3 rows net≈0. Row-weighting would
    # let A's single big row dominate; equal cohort weighting must not.
    base = 739000
    big = CohortSpec(base, 1.0, [RowSpec("AAA", 0.5, 11.1)])  # net = 11.1-1.1 = 10
    smalls = []
    for i in range(1, 11):
        smalls.append(CohortSpec(base + i * 8, 1.0,
                                 [RowSpec("AAA", s, 1.1 + 0.0, time=f"1{k}:00:00")
                                  for k, s in enumerate([0.2, 0.5, 0.8])]))  # net≈0
    res = _run(build_snapshot([big] + smalls, betas={"AAA": 1.0}))
    # 11 cohorts; equal-weight mean = (10 + 10*0)/11 ≈ 0.909, NOT row-weighted
    assert res.h1.experiment_mean == pytest.approx((10.0 + 0.0 * 10) / 11, abs=1e-6)


# ─────────────────────────── Student-t table rules ──────────────────────────
#: Independent published two-sided 95% (0.975) Student-t critical values — NOT
#: produced by the repo's own generator, so they cross-check the static table.
PUBLISHED_T_0975 = {1: 12.706, 2: 4.303, 5: 2.571, 9: 2.262, 10: 2.228, 19: 2.093,
                    29: 2.045, 30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980,
                    200: 1.972, 500: 1.965, 1000: 1.962}


def test_student_t_static_table_matches_independent_published_anchors():
    for df, v in PUBLISHED_T_0975.items():
        assert RC.student_t_critical(df) == pytest.approx(v, abs=6e-4)


def test_student_t_static_table_is_complete_immutable_and_monotonic():
    tbl = RC.STUDENT_T_0975
    assert isinstance(tbl, tuple)              # immutable static representation
    assert len(tbl) == 1001 and tbl[0] is None  # df indexes directly; df starts at 1
    vals = tbl[1:]
    assert len(vals) == 1000                   # no gaps: df 1..1000
    assert RC.STUDENT_T_MAX_DF == 1000
    assert all(isinstance(v, float) and math.isfinite(v) and v > 0 for v in vals)
    assert all(vals[i] > vals[i + 1] for i in range(len(vals) - 1))  # strictly decreasing
    # runtime lookup returns exactly the committed entry, for every df sampled
    for df in (1, 9, 10, 40, 251, 999, 1000):
        assert RC.student_t_critical(df) == tbl[df]


def test_student_t_runtime_is_pure_lookup_independent_of_the_generator(monkeypatch):
    # The runtime module must not contain or call the generator.
    for name in ("_student_t_0975_quantile", "_regularized_incomplete_beta",
                 "_tabulated_t_0975", "student_t_table"):
        assert not hasattr(RC, name), f"runtime must not expose {name}"
    # Even if the generator script is broken/raises, runtime lookup still works,
    # proving student_t_critical does not call it.
    gen = _load_student_t_generator()
    monkeypatch.setattr(gen, "student_t_0975_quantile",
                        lambda df: (_ for _ in ()).throw(AssertionError("runtime must not generate")),
                        raising=True)
    assert RC.student_t_critical(9) == RC.STUDENT_T_0975[9]


def test_student_t_static_table_regenerates_from_committed_generator():
    # Provenance: the pure-python generator reproduces every committed value
    # exactly (both are round(quantile, 8)), across the whole domain.
    gen = _load_student_t_generator()
    assert gen.CERTIFIED_MAX_DF == RC.STUDENT_T_MAX_DF
    for df in range(1, RC.STUDENT_T_MAX_DF + 1):
        assert gen.generate_value(df) == RC.STUDENT_T_0975[df]
    # deterministic across repeated calls
    assert gen.generate_value(37) == gen.generate_value(37)


def test_student_t_df_above_certified_table_is_an_engineering_blocker():
    # A df beyond the certified static table must FAIL CLOSED (StudentTTableError)
    # — an engineering/certification blocker — never a silently computed interval
    # and never an EVIDENCE_INCONCLUSIVE classification.
    values = [float(i) for i in range(RC.STUDENT_T_MAX_DF + 2)]  # n-1 = 1001 > MAX
    with pytest.raises(RC.StudentTTableError):
        RR.student_t_interval(values, minimum_n=10)


def test_student_t_hand_computed_interval():
    values = [1.0] * 5 + [3.0] * 5   # n=10, mean=2.0, var=10/9
    iv = RR.student_t_interval(values, minimum_n=10)
    assert iv.mean == pytest.approx(2.0, abs=1e-12)
    assert iv.se == pytest.approx(math.sqrt((10 / 9)) / math.sqrt(10), abs=1e-12)
    assert iv.t_critical == RC.student_t_critical(9)   # tabulated t(9)
    assert iv.ci_low == pytest.approx(2.0 - RC.student_t_critical(9) * iv.se, abs=1e-12)


def test_student_t_below_minimum_returns_none():
    assert RR.student_t_interval([1.0] * 9, minimum_n=10) is None


def test_student_t_df_outside_tabulated_domain_fails_closed():
    # df within the generous domain is valid (no artificial 40-cohort ceiling);
    # beyond STUDENT_T_MAX_DF it FAILS CLOSED rather than approximating.
    assert RC.student_t_critical(41) > 0            # was previously rejected; now valid
    assert RC.student_t_critical(RC.STUDENT_T_MAX_DF) > 0
    with pytest.raises(RC.StudentTTableError):
        RC.student_t_critical(RC.STUDENT_T_MAX_DF + 1)
    with pytest.raises(RC.StudentTTableError):
        RC.student_t_critical(0)


# ─────────────────────────────── determinism ────────────────────────────────
def test_determinism_identical_inputs_identical_result():
    snap1 = build_snapshot(_uniform_cohorts(12, jitter=0.01), betas={"AAA": 1.0})
    snap2 = build_snapshot(_uniform_cohorts(12, jitter=0.01), betas={"AAA": 1.0})
    a = _run(snap1).to_observations()
    b = _run(snap2).to_observations()
    assert a == b
    # generated_at is injected, not wall-clock
    assert a["generated_at"] == "2026-10-01T12:00:00Z"


def test_generated_at_must_be_timezone_aware():
    snap = build_snapshot(_uniform_cohorts(10), betas={"AAA": 1.0})
    with pytest.raises(ValueError):
        RR.run(snap, PREREG, evidence_identity=FROZEN_IDENTITY,
               generated_at=datetime(2026, 10, 1, 12, 0, 0))  # naive


# ───────────────── strict-JSON / canonical / envelope readiness ─────────────
def test_observations_are_strict_json_canonicalizable():
    from portfolio_automation.northstar.canonical import canonical_dumps
    res = _run(build_snapshot(_uniform_cohorts(12, jitter=0.01), betas={"AAA": 1.0}))
    obs = res.to_observations()
    # canonical_dumps fails closed on NaN/Inf/non-JSON — must succeed here
    canonical_dumps(obs)
    assert obs["observe_only"] is True and obs["grants_authority"] is False
    assert obs["vs002_executed"] is False
    assert obs["classification"]["criterion_outcome"] == "PREREGISTERED_CRITERIA_MET"


def test_observations_carry_full_provenance_binding():
    res = _run(build_snapshot(_uniform_cohorts(10), betas={"AAA": 1.0}))
    obs = res.to_observations()
    assert obs["preregistration"]["preregistration_freeze_digest"] == RR.EXPECTED_FREEZE_DIGEST
    assert obs["preregistration"]["preregistration_id"] == RR.EXPECTED_PREREG_ID
    assert obs["evidence_binding"]["package_id"] == PKG
    assert obs["evidence_binding"]["source_production_sha"] == FROZEN_IDENTITY.source_production_sha
    assert obs["evidence_binding"]["package_transport_digest"] == FROZEN_IDENTITY.package_transport_digest


def test_observations_embed_in_canonical_experiment_result_authority_screen():
    # Proves G11 envelope-readiness: the observations pass the canonical
    # ExperimentResult authority screen (no approve/certify/promote/allocate keys).
    from portfolio_automation.northstar.experiments import _AUTHORITY_KEYS

    def _keys(o):
        ks = set()
        if isinstance(o, dict):
            for k, v in o.items():
                ks.add(k); ks |= _keys(v)
        elif isinstance(o, list):
            for v in o:
                ks |= _keys(v)
        return ks
    res = _run(build_snapshot(_uniform_cohorts(10), betas={"AAA": 1.0}))
    assert not (_keys(res.to_observations()) & set(_AUTHORITY_KEYS))


# ── P1: beta failure != base-population failure (frozen population_binding) ──
# NO_ACTION does not use beta and shares the base population with H1, so a
# beta-uncomputable row must REMAIN in the base population and NO_ACTION, and be
# excluded only from the beta-dependent risk-adjusted statistics (H1/H2).

def _short_bars(symbol, boundary_ord, n=30):
    """A short consecutive adj-close series (all sessions strictly before the
    boundary) that yields < MIN_JOINT_OBSERVATIONS exact pairs → beta uncomputable."""
    ords = list(range(boundary_ord - (n + 10), boundary_ord - 10))
    return _bars(symbol, _spy_adj_series([_d(o) for o in ords]))


def test_p1_beta_failure_row_stays_in_base_and_no_action_only():
    base = 739000
    snap = build_snapshot([CohortSpec(base, 1.0, [RowSpec("BAD", 0.5, 3.5)])],
                          betas={"BAD": 1.0}, extra_bars={"BAD": _short_bars("BAD", base)})
    rows, excl, ev = RR._prepare_rows(snap, RR.verify_preregistration(PREREG))
    assert len(rows) == 1                                   # retained in base population
    r = rows[0]
    assert r.net_risk_adjusted_excess_pct is None           # no H1 risk-adjusted value
    assert r.net_no_action_return_pct == pytest.approx(3.5 - 0.1, abs=1e-9)  # NO_ACTION, no beta
    assert excl["beta_insufficient_joint_observations"] == 1


def test_p1_no_action_retains_beta_failed_rows():
    base = 739000
    cohorts = [CohortSpec(base + i * 8, 1.0,
                          [RowSpec("AAA", 0.5, 0.3, time="10:00:00"),   # na = 0.2 (beta ok)
                           RowSpec("BAD", 0.5, 5.1, time="11:00:00")])  # na = 5.0 (beta fails)
               for i in range(10)]
    snap = build_snapshot(cohorts, betas={"AAA": 1.0, "BAD": 1.0},
                          extra_bars={"BAD": _short_bars("BAD", base)})
    res = _run(snap)
    # each cohort NO_ACTION mean = avg(0.2, 5.0) = 2.6 (BOTH rows); dropping BAD → 0.2
    assert res.no_action.cohort_count == 10
    assert res.no_action.mean_net_no_action_return == pytest.approx(2.6, abs=1e-6)
    assert res.population.base_population_rows == 20        # beta-failed BAD rows retained
    assert res.population.exclusion_counts.get("beta_insufficient_joint_observations", 0) == 10


def test_p1_cohort_selection_uses_base_population_dates_not_beta_subset():
    base = 739000
    cohorts = [CohortSpec(base, 1.0, [RowSpec("AAA", 0.5, 3.5)]),
               CohortSpec(base + 8, 1.0, [RowSpec("AAA", 0.5, 3.5)]),
               CohortSpec(base + 16, 1.0, [RowSpec("BAD", 0.5, 3.5)])]  # beta-only cohort date
    snap = build_snapshot(cohorts, betas={"AAA": 1.0, "BAD": 1.0},
                          extra_bars={"BAD": _short_bars("BAD", base)})
    res = _run(snap)
    assert _d(base + 16) in res.population.selected_cohort_dates   # chosen from base dates
    assert len(res.population.selected_cohort_dates) == 3


def test_p1_h1_valid_cohort_count_excludes_beta_only_cohorts():
    base = 739000
    cohorts = [CohortSpec(base + i * 8, 1.0, [RowSpec("AAA", 0.5, 3.5)]) for i in range(10)]
    cohorts.append(CohortSpec(base + 80, 1.0, [RowSpec("BAD", 0.5, 3.5)]))  # beta-only cohort
    snap = build_snapshot(cohorts, betas={"AAA": 1.0, "BAD": 1.0},
                          extra_bars={"BAD": _short_bars("BAD", base)})
    res = _run(snap)
    assert res.no_action.cohort_count == 11     # NO_ACTION keeps the beta-only cohort
    assert res.h1.cohort_count == 10            # H1 has no risk-adjusted mean for that date


def test_p1_mixed_cohort_no_action_uses_both_h1_uses_beta_only():
    base = 739000
    rows_mixed = [RowSpec("AAA", 0.5, 3.5, time="10:00:00"),   # beta ok → net_excess 2.4 ; na 3.4
                  RowSpec("BAD", 0.5, 9.1, time="11:00:00")]   # beta fail → na 9.0 ; no risk-adj
    snap = build_snapshot([CohortSpec(base, 1.0, rows_mixed)],
                          betas={"AAA": 1.0, "BAD": 1.0},
                          extra_bars={"BAD": _short_bars("BAD", base)})
    binding = RR.verify_preregistration(PREREG)
    rows, excl, ev = RR._prepare_rows(snap, binding)
    by = RR._group_by_date(rows)
    na_vals = sorted(round(r.net_no_action_return_pct, 4) for r in by[_d(base)])
    h1_vals = [r.net_risk_adjusted_excess_pct for r in by[_d(base)]
               if r.net_risk_adjusted_excess_pct is not None]
    assert na_vals == [3.4, 9.0]                                  # NO_ACTION uses BOTH base rows
    assert h1_vals == [pytest.approx(2.4, abs=1e-9)]             # H1 uses only the beta-computable row


def test_p1_scored_beta_uncomputable_row_is_not_an_h2_pair():
    base = 739000
    rows = [RowSpec("AAA", 0.2, 3.3, time="10:00:00"),
            RowSpec("AAA", 0.5, 3.5, time="11:00:00"),
            RowSpec("BAD", 0.8, 3.7, time="12:00:00")]   # scored but beta fails → not an H2 pair
    snap = build_snapshot([CohortSpec(base, 1.0, rows)],
                          betas={"AAA": 1.0, "BAD": 1.0},
                          extra_bars={"BAD": _short_bars("BAD", base)})
    binding = RR.verify_preregistration(PREREG)
    prows, excl, ev = RR._prepare_rows(snap, binding)
    by = RR._group_by_date(prows)
    valid_pairs = [(r.signal_score, r.net_risk_adjusted_excess_pct) for r in by[_d(base)]
                   if r.signal_score is not None and r.net_risk_adjusted_excess_pct is not None]
    assert len(valid_pairs) == 2                       # the beta-failed scored row is not a pair
    assert RR._h2([_d(base)], by, binding.minimum_cohorts).valid_cohort_ic_count == 0


def test_p1_all_beta_computable_matches_prior_semantics():
    res = _run(build_snapshot(_uniform_cohorts(12, jitter=0.01), betas={"AAA": 1.0}))
    assert res.population.base_population_rows == 36
    assert res.h1.cohort_count == res.no_action.cohort_count == 12   # no divergence when all computable
    assert res.criterion_outcome is CriterionOutcome.MET


def test_beta_window_is_anchored_to_benchmark_not_stock_trailing_bars():
    # Codex P1 (line 310): a SPARSE stock must NOT stretch the estimate past the 252
    # BENCHMARK sessions. The stock omits the in-window range [base-252..base-201]
    # and instead holds OLD bars [base-300..base-253] that lie OUTSIDE the 252-
    # benchmark window; those must be excluded from beta.
    binding = RR.verify_preregistration(PREREG)
    base = 400000
    boundary = _d(base)
    spy_ords = list(range(base - 300, base))                      # 300 benchmark sessions
    stock_ords = list(range(base - 300, base - 252)) + list(range(base - 200, base))
    spy = _bars("SPY", _spy_adj_series([_d(o) for o in spy_ords]))
    stock = _bars("AAA", _spy_adj_series([_d(o) for o in stock_ords]))
    beta, n = RR.compute_beta(stock, spy, boundary_date=boundary,
                              window_sessions=binding.beta_window_sessions,
                              minimum_joint_observations=binding.minimum_joint_observations)
    # only in-window recent pairs [base-200..base-1] count: 200 bars -> 199 pairs;
    # the 48 OLD out-of-window stock bars are excluded (a stock-trailing-252 window
    # would have wrongly admitted them).
    assert n == 199
    assert math.isfinite(beta)

