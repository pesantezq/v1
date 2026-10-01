"""Deterministic VS-002 result runner (the computation engine).

``experimental_noncanonical``. Computes the frozen VS-002 result from an
ALREADY-VALIDATED in-memory evidence snapshot plus the frozen preregistration.

What this module is NOT, by construction:
  * it performs NO package discovery (no globbing, no ``latest``, no filesystem
    search) — :func:`run` accepts a ``ValidatedSnapshot`` the caller supplies;
  * it makes NO network calls and acquires NO evidence;
  * importing it triggers NO IO and reads NO package;
  * it decides NO authority, dispatches NO mission, and writes NO artifact.

The frozen scientific authority is ``evals/vertical_slice/VS-002_preregistration.json``.
Every binding numeric rule (friction, rf, cohort minimum, beta window, minimum
joint observations, confidence level) is READ FROM that preregistration's
``binding_core`` after its content hash is reverified, so any change to a frozen
rule changes the freeze digest and fails closed before a single statistic is
computed. Building a correct result here grants no authority and executes
nothing against the real package.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from portfolio_automation.northstar.canonical import canonical_dumps, content_hash
from portfolio_automation.vs002_evidence import contracts as C
from portfolio_automation.vs002_evidence import builder as B
from portfolio_automation.vs002_evidence.consumer import ValidatedSnapshot
from portfolio_automation.vs002_evidence import result_contract as RC
from portfolio_automation.vs002_evidence.result_contract import (
    CriterionOutcome, EvidenceIdentity, H1Result, H1Status, H2Result, H2Status,
    Interval, NoActionResult, NoActionStatus, PopulationSummary, SkillOutcome,
    VS002Result, student_t_critical,
)

# ── Pinned frozen preregistration identity (anchors the digest check) ────────
EXPECTED_PREREG_SCHEMA = "engineering.vertical_slice.preregistration.v1"
EXPECTED_EXPERIMENT_ID = "VS-002"
EXPECTED_PREREG_ID = "vs002prereg_2b3dcf7dba0b29304a5543f2d25efb4c"
EXPECTED_FREEZE_DIGEST = "b7da049bfb58c789b31adc3ee6c9eb5044b270a5a73bc7746c5edd048e48ad54"
PREREG_REL = "evals/vertical_slice/VS-002_preregistration.json"


class PreregistrationMismatch(ValueError):
    """The preregistration failed identity/freeze verification. Fail closed."""


class EvidenceBindingMismatch(ValueError):
    """The arriving evidence identity does not match the frozen binding. Fail closed."""


class BetaUncomputable(Exception):
    """A single row's beta cannot be computed under the frozen rules; the row is
    excluded with a deterministic reason (never silently assigned 0/1)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PreregBinding:
    """The verified frozen rules the runner is bound to (read from binding_core)."""
    preregistration_id: str
    freeze_digest: str
    schema_version: str
    evidence_binding: EvidenceIdentity
    friction_pp: float
    rf_7d_pp: float
    minimum_cohorts: int
    beta_window_sessions: int
    minimum_joint_observations: int
    confidence_level: float
    horizon_days: int


# ────────────────────────── preregistration binding ─────────────────────────
def load_preregistration(repo_root: str | Path) -> dict:
    """Read the committed frozen preregistration JSON (the scientific authority).

    Reading this committed artifact is explicitly permitted; it is NOT the
    evidence package and contains only the frozen design + identity strings."""
    import json
    path = Path(repo_root) / PREREG_REL
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def verify_preregistration(prereg: Mapping[str, Any]) -> PreregBinding:
    """Recompute the freeze digest, pin identity, and extract the frozen rules."""
    if not isinstance(prereg, Mapping):
        raise PreregistrationMismatch("preregistration is not a mapping")
    core = prereg.get("binding_core")
    if not isinstance(core, Mapping):
        raise PreregistrationMismatch("binding_core missing or not a mapping")

    recomputed = content_hash(canonical_dumps(core))
    stored = prereg.get("preregistration_freeze_digest")
    if recomputed != stored:
        raise PreregistrationMismatch(
            f"freeze digest mismatch: recomputed {recomputed} != stored {stored}")
    if recomputed != EXPECTED_FREEZE_DIGEST:
        raise PreregistrationMismatch(
            f"freeze digest {recomputed} != pinned frozen digest {EXPECTED_FREEZE_DIGEST}")

    checks = {
        "schema_version": (prereg.get("schema_version"), EXPECTED_PREREG_SCHEMA),
        "experiment_id": (prereg.get("experiment_id"), EXPECTED_EXPERIMENT_ID),
        "preregistration_id": (prereg.get("preregistration_id"), EXPECTED_PREREG_ID),
    }
    for name, (got, want) in checks.items():
        if got != want:
            raise PreregistrationMismatch(f"{name} mismatch: {got!r} != {want!r}")
    if prereg.get("observe_only") is not True or core.get("observe_only") is not True:
        raise PreregistrationMismatch("observe_only must be True at both levels")
    if prereg.get("vs002_executed") is not False:
        raise PreregistrationMismatch("vs002_executed must be False (design is not executed)")
    if prereg.get("status") != "PREREGISTERED_NOT_EXECUTED":
        raise PreregistrationMismatch("status must be PREREGISTERED_NOT_EXECUTED")

    eb = core.get("evidence_binding") or {}
    evidence_binding = EvidenceIdentity(
        package_id=eb.get("package_id"),
        package_transport_digest=eb.get("package_transport_digest"),
        source_production_sha=eb.get("source_production_sha"),
        evidence_schema_version=eb.get("evidence_schema_version"),
    )

    # Frozen numeric rules — read from binding_core, cross-checked against the
    # evidence contracts. Any mutation of these changes the freeze digest above.
    ra = core.get("risk_adjustment") or {}
    rowa = core.get("row_arithmetic") or {}
    coh = core.get("cohort_construction") or {}
    iv = core.get("interval_method") or {}

    def _req(d: Mapping[str, Any], key: str) -> Any:
        if key not in d:
            raise PreregistrationMismatch(f"binding_core missing {key}")
        return d[key]

    binding = PreregBinding(
        preregistration_id=EXPECTED_PREREG_ID,
        freeze_digest=recomputed,
        schema_version=EXPECTED_PREREG_SCHEMA,
        evidence_binding=evidence_binding,
        friction_pp=float(_req(rowa, "friction_pp")),
        rf_7d_pp=float(_req(rowa, "rf_7d_pp")),
        minimum_cohorts=int(_req(coh, "minimum_cohorts")),
        beta_window_sessions=int(_req(ra, "estimation_window_sessions")),
        minimum_joint_observations=int(_req(ra, "minimum_joint_observations")),
        confidence_level=float(_req(iv, "confidence_level")),
        horizon_days=int(C.HORIZON_DAYS),
    )

    # Cross-consistency with the evidence contracts (belt-and-suspenders): these
    # constants and the frozen rules must agree, or the two surfaces have drifted.
    mismatches = []
    if binding.minimum_cohorts != C.MIN_COHORTS:
        mismatches.append("minimum_cohorts")
    if binding.beta_window_sessions != C.MIN_PRIOR_SESSIONS:
        mismatches.append("estimation_window_sessions")
    if binding.minimum_joint_observations != C.MIN_JOINT_OBSERVATIONS:
        mismatches.append("minimum_joint_observations")
    if abs(binding.confidence_level - 0.95) > 1e-12:
        mismatches.append("confidence_level")
    if abs(binding.rf_7d_pp - float(C.RISK_FREE_RATE_7D_ASSUMPTION["value"])) > 1e-12:
        mismatches.append("rf_7d_pp")
    if mismatches:
        raise PreregistrationMismatch(
            "frozen rules disagree with evidence contracts: " + ", ".join(mismatches))
    return binding


def verify_evidence_binding(binding: PreregBinding, provided: EvidenceIdentity,
                            *, manifest: Optional[Mapping[str, Any]] = None) -> EvidenceIdentity:
    """Match the arriving evidence identity against the frozen binding.

    The identity strings are metadata only — this never opens the package."""
    want = binding.evidence_binding
    for field_name in ("package_id", "package_transport_digest",
                       "source_production_sha", "evidence_schema_version"):
        g = getattr(provided, field_name)
        w = getattr(want, field_name)
        if g != w:
            raise EvidenceBindingMismatch(f"{field_name} mismatch: {g!r} != frozen {w!r}")
    if provided.evidence_schema_version != C.SCHEMA_VERSION:
        raise EvidenceBindingMismatch(
            f"evidence_schema_version {provided.evidence_schema_version!r} "
            f"!= contracts.SCHEMA_VERSION {C.SCHEMA_VERSION!r}")
    if manifest is not None:
        m_schema = manifest.get("schema_version")
        m_pkg = manifest.get("package_id")
        if m_schema != provided.evidence_schema_version:
            raise EvidenceBindingMismatch(
                f"snapshot manifest schema_version {m_schema!r} != {provided.evidence_schema_version!r}")
        if m_pkg is not None and m_pkg != provided.package_id:
            raise EvidenceBindingMismatch(
                f"snapshot manifest package_id {m_pkg!r} != {provided.package_id!r}")
    return provided


# ────────────────────────────── pure statistics ─────────────────────────────
def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _sample_sd(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        raise ValueError("sample sd requires n >= 2")
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def student_t_interval(values: Sequence[float], *, minimum_n: int) -> Optional[Interval]:
    """Two-sided 95% Student-t interval over independent cohort statistics.

    Returns None (→ EVIDENCE_INCONCLUSIVE at the caller) when n < minimum_n."""
    n = len(values)
    if n < minimum_n:
        return None
    mean = _mean(values)
    sd = _sample_sd(values)
    se = sd / math.sqrt(n)
    t = student_t_critical(n - 1)
    return Interval(n=n, mean=mean, sample_sd=sd, se=se, t_critical=t,
                    ci_low=mean - t * se, ci_high=mean + t * se)


def _average_ranks(values: Sequence[float]) -> list[float]:
    """Ranks with average-rank handling of ties (1-based)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # average of 1-based ranks i+1..j+1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman_rho(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Standard Spearman rho via Pearson correlation of average ranks.

    Returns None when either ranked series has zero variance (undefined)."""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    rx = _average_ranks(xs)
    ry = _average_ranks(ys)
    mx = _mean(rx)
    my = _mean(ry)
    sxy = sum((rx[i] - mx) * (ry[i] - my) for i in range(len(rx)))
    sxx = sum((v - mx) ** 2 for v in rx)
    syy = sum((v - my) ** 2 for v in ry)
    if sxx <= 0 or syy <= 0:
        return None
    rho = sxy / math.sqrt(sxx * syy)
    if not math.isfinite(rho):
        return None
    return rho


# ────────────────────────────── beta estimation ─────────────────────────────
def _daily_adjusted_returns(bars: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Dividend-adjusted daily returns between CONSECUTIVE bars, dated at t."""
    out: dict[str, float] = {}
    for i in range(1, len(bars)):
        prev_c = bars[i - 1]["adj_close"]
        cur_c = bars[i]["adj_close"]
        out[str(bars[i]["session_date"])] = C.derive_adjusted_return(cur_c, prev_c)
    return out


def compute_beta(stock_bars: Sequence[Mapping[str, Any]],
                 spy_bars: Sequence[Mapping[str, Any]],
                 *, boundary_date: str, window_sessions: int,
                 minimum_joint_observations: int) -> tuple[float, int]:
    """beta_i = Cov(r_i, r_SPY) / Var(r_SPY) over dividend-adjusted daily
    returns on sessions STRICTLY before the signal date (structural window)."""
    sw = [b for b in stock_bars if str(b["session_date"]) < boundary_date]
    sw = sw[-window_sessions:]
    bw = [b for b in spy_bars if str(b["session_date"]) < boundary_date]
    bw = bw[-window_sessions:]
    if len(sw) < 2 or len(bw) < 2:
        raise BetaUncomputable("beta_window_lt_2_bars")
    sr = _daily_adjusted_returns(sw)
    br = _daily_adjusted_returns(bw)
    common = sorted(set(sr) & set(br))
    if len(common) < minimum_joint_observations:
        raise BetaUncomputable("insufficient_joint_observations")
    ri = [sr[d] for d in common]
    rs = [br[d] for d in common]
    ms = _mean(rs)
    mi = _mean(ri)
    var = sum((x - ms) ** 2 for x in rs)
    if var <= 0:
        raise BetaUncomputable("zero_benchmark_variance")
    cov = sum((ri[k] - mi) * (rs[k] - ms) for k in range(len(common)))
    beta = cov / var
    if not math.isfinite(beta):
        raise BetaUncomputable("non_finite_beta")
    return beta, len(common)


# ─────────────────────────────── row preparation ────────────────────────────
@dataclass(frozen=True)
class _Row:
    cohort_date: str
    ticker: str
    net_risk_adjusted_excess_pct: float
    net_no_action_return_pct: float
    signal_score: Optional[float]


def _exact_spy_outcomes(spy_signals: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Map exact SPY signal_time -> recorded outcome_return_7d (matured only).

    Fails closed on duplicate SPY timestamps (ambiguous exact match)."""
    out: dict[str, float] = {}
    for s in spy_signals:
        t = s["signal_time"]
        o = s.get("outcome_return_7d")
        if o is None:
            continue
        if t in out:
            raise ValueError(f"duplicate SPY signal_time {t!r} — ambiguous exact match")
        out[t] = float(o)
    return out


def _prepare_rows(snap: ValidatedSnapshot, binding: PreregBinding
                  ) -> tuple[list[_Row], Counter, int]:
    benchmark = C.BENCHMARK
    eligible = set(snap.eligible_universe)
    spy_out = _exact_spy_outcomes(snap.signals_for(benchmark))
    spy_bars = snap.bars_for(benchmark)
    rows: list[_Row] = []
    excl: Counter = Counter()
    evaluated = 0
    friction = binding.friction_pp
    rf = binding.rf_7d_pp

    for s in snap.signals:
        ticker = s["ticker"]
        if ticker == benchmark:
            continue
        if ticker not in eligible:
            continue
        evaluated += 1
        stock_out = s.get("outcome_return_7d")
        if stock_out is None:
            excl["unmatured_stock_row"] += 1
            continue
        stock_out = float(stock_out)
        if not math.isfinite(stock_out):
            excl["non_finite_stock_outcome"] += 1
            continue
        signal_time = s["signal_time"]
        if signal_time not in spy_out:
            excl["no_exact_spy_timestamp_match"] += 1
            continue
        spy_ret = spy_out[signal_time]
        boundary = str(signal_time)[:10]
        try:
            beta, _n = compute_beta(
                snap.bars_for(ticker), spy_bars, boundary_date=boundary,
                window_sessions=binding.beta_window_sessions,
                minimum_joint_observations=binding.minimum_joint_observations)
        except BetaUncomputable as e:
            excl[f"beta_{e.reason}"] += 1
            continue

        expected_market = rf + beta * (spy_ret - rf)
        gross = stock_out - expected_market
        net_excess = gross - friction
        net_no_action = stock_out - rf - friction

        score = s.get("signal_score")
        if score is not None:
            score = float(score)
            if not math.isfinite(score):
                score = None
        rows.append(_Row(cohort_date=boundary, ticker=ticker,
                         net_risk_adjusted_excess_pct=net_excess,
                         net_no_action_return_pct=net_no_action,
                         signal_score=score))
    return rows, excl, evaluated


# ─────────────────────────────── aggregation ────────────────────────────────
def _group_by_date(rows: Sequence[_Row]) -> dict[str, list[_Row]]:
    g: dict[str, list[_Row]] = defaultdict(list)
    for r in rows:
        g[r.cohort_date].append(r)
    return g


def _h1(selected: Sequence[str], by_date: Mapping[str, list[_Row]],
        min_cohorts: int) -> H1Result:
    cohort_means = [_mean([r.net_risk_adjusted_excess_pct for r in by_date[d]])
                    for d in selected]
    n = len(cohort_means)
    if n < min_cohorts:
        return H1Result(H1Status.INCONCLUSIVE, n, None, None)
    experiment_mean = _mean(cohort_means)
    interval = student_t_interval(cohort_means, minimum_n=min_cohorts)
    met = experiment_mean > 0 and interval is not None and interval.ci_low > 0
    return H1Result(H1Status.MET if met else H1Status.NOT_MET, n, experiment_mean, interval)


def _no_action(selected: Sequence[str], by_date: Mapping[str, list[_Row]],
               min_cohorts: int) -> NoActionResult:
    cohort_means = [_mean([r.net_no_action_return_pct for r in by_date[d]])
                    for d in selected]
    n = len(cohort_means)
    if n < min_cohorts:
        return NoActionResult(NoActionStatus.INCONCLUSIVE, n, None, None)
    mean = _mean(cohort_means)
    interval = student_t_interval(cohort_means, minimum_n=min_cohorts)
    beat = mean > 0 and interval is not None and interval.ci_low > 0
    return NoActionResult(NoActionStatus.BEAT if beat else NoActionStatus.NOT_BEAT,
                          n, mean, interval)


def _h2(selected: Sequence[str], by_date: Mapping[str, list[_Row]],
        min_cohorts: int) -> H2Result:
    cohort_ics: list[float] = []
    for d in selected:
        pairs = [(r.signal_score, r.net_risk_adjusted_excess_pct)
                 for r in by_date[d] if r.signal_score is not None]
        if len(pairs) < 3:
            continue
        scores = [p[0] for p in pairs]
        outcomes = [p[1] for p in pairs]
        if len(set(scores)) < 2 or len(set(outcomes)) < 2:
            continue  # constant score or constant outcome → no IC for this cohort
        rho = spearman_rho(scores, outcomes)
        if rho is None:
            continue
        cohort_ics.append(rho)
    n = len(cohort_ics)
    if n < min_cohorts:
        return H2Result(H2Status.INCONCLUSIVE, n, None, None)
    point_ic = _mean(cohort_ics)
    reporting = student_t_interval(cohort_ics, minimum_n=min_cohorts)  # REPORTING ONLY
    met = point_ic > 0  # gate: point IC only; the CI never gates H2
    return H2Result(H2Status.MET if met else H2Status.NOT_MET, n, point_ic, reporting)


def _classify(h1: H1Result, h2: H2Result, na: NoActionResult
              ) -> tuple[CriterionOutcome, SkillOutcome]:
    inconclusive = (h1.status is H1Status.INCONCLUSIVE
                    or h2.status is H2Status.INCONCLUSIVE
                    or na.status is NoActionStatus.INCONCLUSIVE)
    if inconclusive:
        crit = CriterionOutcome.INCONCLUSIVE
    elif (h1.status is H1Status.MET and h2.status is H2Status.MET
          and na.status is NoActionStatus.BEAT):
        crit = CriterionOutcome.MET
    else:
        crit = CriterionOutcome.NOT_MET
    skill = (SkillOutcome.PRESENT if crit is CriterionOutcome.MET
             else SkillOutcome.NOT_ESTABLISHED)
    return crit, skill


# ─────────────────────────────────── run ────────────────────────────────────
def run(snap: ValidatedSnapshot, prereg: Mapping[str, Any], *,
        evidence_identity: EvidenceIdentity, generated_at: datetime,
        runner_id: str = RC.DEFAULT_RUNNER_ID,
        runner_version: str = RC.RUNNER_VERSION) -> VS002Result:
    """Deterministic VS-002 result over an already-validated snapshot.

    Pure given (snap, prereg, evidence_identity, generated_at): no IO, no
    network, no package discovery, no wall-clock. ``generated_at`` must be a
    tz-aware datetime supplied by the caller (determinism, G16)."""
    if generated_at.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    binding = verify_preregistration(prereg)
    verify_evidence_binding(binding, evidence_identity, manifest=snap.manifest)

    rows, excl, evaluated = _prepare_rows(snap, binding)
    by_date = _group_by_date(rows)
    distinct_dates = sorted(by_date)
    selected = B.greedy_cohorts(distinct_dates, binding.horizon_days)

    h1 = _h1(selected, by_date, binding.minimum_cohorts)
    h2 = _h2(selected, by_date, binding.minimum_cohorts)
    na = _no_action(selected, by_date, binding.minimum_cohorts)
    criterion, skill = _classify(h1, h2, na)

    population = PopulationSummary(
        base_population_rows=len(rows),
        scored_population_rows=sum(1 for r in rows if r.signal_score is not None),
        evaluated_signal_rows=evaluated,
        selected_cohort_dates=tuple(selected),
        exclusion_counts=dict(excl),
    )
    return VS002Result(
        schema_version=RC.RESULT_SCHEMA_VERSION,
        schema_kind=RC.RESULT_SCHEMA_KIND,
        runner_id=runner_id,
        runner_version=runner_version,
        generated_at=generated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        preregistration_id=binding.preregistration_id,
        preregistration_freeze_digest=binding.freeze_digest,
        preregistration_schema_version=binding.schema_version,
        preregistration_verified=True,
        evidence_binding=binding.evidence_binding,
        population=population,
        h1=h1, h2=h2, no_action=na,
        criterion_outcome=criterion, skill_outcome=skill,
    )
