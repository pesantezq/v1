"""Execute VS-001 end to end: evidence -> PIT -> spec -> result.

WHAT MAKES THIS A SLICE RATHER THAN A SCRIPT.

Every step goes through the real contracts. Evidence is admitted by the real
``EvidenceGateway`` at an explicit ``as_of``; the question is an
``ExperimentSpec`` whose identity is content-addressed; the answer is an
``ExperimentResult`` bound to that spec id and carrying an authority-screened
observations payload. Nothing here is allowed to shortcut a contract to make
the demo work -- where a capability is missing, the runner says so rather than
faking it.

THE LEAKAGE PROOF IS PART OF THE RUN.

Before computing anything, the runner asks the gateway for every outcome
snapshot at its own signal's ``as_of``. Every one of those calls must be
refused, and the refusals are counted into the result. An implementation that
accidentally let the future in would produce a nonzero admitted count here and
fail its test, rather than producing quietly better numbers.

``experimental_noncanonical``.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from portfolio_automation.evidence_gateway.admissibility import is_admissible
from portfolio_automation.northstar.experiments import ExperimentResult, ExperimentSpec
from portfolio_automation.northstar.provenance import PRODUCER_SYSTEM, Provenance
from portfolio_automation.northstar.research import ResearchClaim
from portfolio_automation.vertical_slice import preregistration as PRE
from portfolio_automation.vertical_slice.evidence import iter_row_evidence

BENCHMARK_TICKER = "SPY"
HORIZON_WINDOW = "7d"
RUNNER_ID = "vertical_slice.vs001_runner"
RUNNER_VERSION = "v1"

MIN_SCANS_FOR_CONCLUSION = 10


def _mean(xs: list[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _stdev(xs: list[float]) -> Optional[float]:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _mean_ci95(xs: list[float]) -> dict[str, Any]:
    """Normal-approximation CI on a mean. Reported with n so a reader can see
    how thin it is; with ~20 clusters this is wide on purpose."""
    if len(xs) < 2:
        return {"n": len(xs), "mean": _mean(xs), "ci_low": None, "ci_high": None,
                "status": "INSUFFICIENT_SAMPLE"}
    m = sum(xs) / len(xs)
    sd = _stdev(xs) or 0.0
    half = 1.96 * sd / math.sqrt(len(xs))
    return {"n": len(xs), "mean": m, "stdev": sd,
            "ci_low": m - half, "ci_high": m + half, "status": "OK"}


def _wilson(successes: int, n: int) -> dict[str, Any]:
    """Reused rather than reinvented: the repo already ships this."""
    from portfolio_automation.weekly_etf_bundles.calibration import wilson_interval
    if n <= 0:
        return {"n": 0, "rate": None, "ci_low": None, "ci_high": None,
                "status": "UNDEFINED_ZERO_DENOMINATOR"}
    low, high = wilson_interval(successes, n)
    return {"n": n, "successes": successes, "rate": successes / n,
            "ci_low": low, "ci_high": high, "status": "OK"}


def _spearman(xs: list[float], ys: list[float]) -> Optional[float]:
    from portfolio_automation.weekly_etf_bundles.evaluation import spearman
    if len(xs) < 3:
        return None
    return spearman(xs, ys)


@dataclass(frozen=True)
class Observation:
    ticker: str
    scan_time: datetime
    signal_score: Optional[float]
    raw_return_pct: float
    benchmark_return_pct: float
    gross_excess_pct: float
    net_excess_pct: float


def build_claim(recorded_at: datetime, evidence_refs: tuple) -> ResearchClaim:
    """The claim must cite evidence -- ResearchClaim refuses an uncited claim.

    It cites the same scored outcome snapshots the experiment evaluated, so the
    hypothesis and the test are anchored to one identical evidence set rather
    than to a convenient subset chosen afterwards."""
    return ResearchClaim(
        claim=("StockBot watchlist signals earn a positive 7-day return in "
               "excess of SPY over the same interval, net of friction."),
        testable_metric="scan_clustered_mean_net_excess_return_pct",
        direction="increase",
        provenance=Provenance(producer_id=RUNNER_ID, producer_type=PRODUCER_SYSTEM,
                              recorded_at=recorded_at, code_version=RUNNER_VERSION),
        evidence_refs=evidence_refs,
        scope_entities=("watchlist_signals",),
        notes=("Preregistered as VS-001 before execution. A null result is a "
               "valid outcome and does not disprove skill; it reports that this "
               "window cannot distinguish it from zero."),
    )


def build_spec(claim_id: str, *, as_of: datetime, universe: tuple[str, ...],
               recorded_at: datetime) -> ExperimentSpec:
    content = PRE.preregistration_content(Path("."))
    return ExperimentSpec(
        hypothesis_claim_id=claim_id,
        universe=universe,
        as_of=as_of,
        evaluation_windows=(HORIZON_WINDOW,),
        metrics=(
            "scan_clustered_mean_net_excess_return_pct",
            "excess_win_rate",
            "spearman_ic_signal_score_vs_net_excess",
        ),
        success_gate=content["evaluation"]["acceptance_rule"],
        abandon_gate=("Abandon the excess-return hypothesis for this signal "
                      "family if the scan-clustered mean net excess is "
                      "negative with a 95% CI entirely below zero."),
        provenance=Provenance(producer_id=RUNNER_ID, producer_type=PRODUCER_SYSTEM,
                              recorded_at=recorded_at, code_version=RUNNER_VERSION),
        allowed_evidence_types=("watchlist_signal", "signal_outcome_7d"),
        notes="Northstar vertical slice VS-001.",
    )


def run(repo_root: Path) -> dict[str, Any]:
    """Execute the slice. Returns the full run record; writes nothing."""
    root = Path(repo_root)
    csv_path = root / PRE.EVIDENCE_REL
    rows = list(iter_row_evidence(csv_path))

    # ---- leakage proof: no outcome may be admissible at its signal's as_of ----
    leak_checked = 0
    leak_admitted = 0
    refusal_reasons: dict[str, int] = defaultdict(int)
    for item in rows:
        if item.outcome is None:
            continue
        leak_checked += 1
        decision = is_admissible(item.outcome.pit, item.scan_time)
        if decision.admitted:
            leak_admitted += 1
        else:
            refusal_reasons[str(getattr(decision.reason, "value", decision.reason))] += 1

    # ---- admit outcomes at an as_of after every resolution instant ----
    known = [i.outcome_known_at for i in rows if i.outcome_known_at is not None]
    evaluation_as_of = max(known) if known else datetime.now(timezone.utc)

    admitted: list[Any] = []
    excluded_unmatured = 0
    for item in rows:
        if item.outcome is None:
            excluded_unmatured += 1
            continue
        if is_admissible(item.outcome.pit, evaluation_as_of).admitted:
            admitted.append(item)

    # ---- benchmark join, strictly within a scan ----
    bench: dict[datetime, float] = {}
    for item in admitted:
        if item.ticker == BENCHMARK_TICKER:
            bench[item.scan_time] = item.outcome.payload_copy()["outcome_return_7d_pct"]

    friction = PRE.FRICTION_ROUND_TRIP_PCT + PRE.FRICTION_COMMISSION_PCT
    observations: list[Observation] = []
    scored_refs: list[Any] = []
    excluded_no_benchmark = 0
    for item in admitted:
        if item.ticker == BENCHMARK_TICKER:
            continue
        if item.scan_time not in bench:
            excluded_no_benchmark += 1
            continue
        payload = item.outcome.payload_copy()
        raw = payload["outcome_return_7d_pct"]
        bm = bench[item.scan_time]
        gross = raw - bm
        scored_refs.append(item.outcome.ref())
        observations.append(Observation(
            ticker=item.ticker, scan_time=item.scan_time,
            signal_score=item.signal.payload_copy().get("signal_score"),
            raw_return_pct=raw, benchmark_return_pct=bm,
            gross_excess_pct=gross, net_excess_pct=gross - friction))

    # ---- metrics, exactly as preregistered ----
    by_scan: dict[datetime, list[float]] = defaultdict(list)
    for o in observations:
        by_scan[o.scan_time].append(o.net_excess_pct)
    scan_means = [sum(v) / len(v) for _, v in sorted(by_scan.items())]

    primary = _mean_ci95(scan_means)
    naive = _mean_ci95([o.net_excess_pct for o in observations])
    excess_wins = sum(1 for o in observations if o.net_excess_pct > 0)
    raw_wins = sum(1 for o in observations if o.raw_return_pct > 0)
    scored = [(o.signal_score, o.net_excess_pct) for o in observations
              if o.signal_score is not None]
    ic = _spearman([s for s, _ in scored], [e for _, e in scored])

    if len(scan_means) < MIN_SCANS_FOR_CONCLUSION:
        verdict = "INCONCLUSIVE_SMALL_SAMPLE"
    elif primary["mean"] is not None and primary["mean"] > 0 and \
            primary["ci_low"] is not None and primary["ci_low"] > 0:
        verdict = "SUPPORTED"
    else:
        verdict = "NOT_SUPPORTED"

    metrics = {
        "scan_clustered_mean_net_excess_return_pct": primary,
        "naive_per_row_mean_net_excess_return_pct": naive,
        "excess_win_rate": _wilson(excess_wins, len(observations)),
        "raw_win_rate": _wilson(raw_wins, len(observations)),
        "spearman_ic_signal_score_vs_net_excess": ic,
        "mean_raw_return_pct": _mean([o.raw_return_pct for o in observations]),
        "mean_benchmark_return_pct": _mean([o.benchmark_return_pct for o in observations]),
        "mean_gross_excess_pct": _mean([o.gross_excess_pct for o in observations]),
        "worst_scan_mean_net_excess_pct": min(scan_means) if scan_means else None,
        "share_of_scans_with_negative_mean_net_excess": (
            sum(1 for m in scan_means if m < 0) / len(scan_means) if scan_means else None),
    }

    population = {
        "rows_loaded": len(rows),
        "outcome_snapshots_built": leak_checked,
        "excluded_unmatured_outcome": excluded_unmatured,
        "excluded_no_benchmark_in_scan": excluded_no_benchmark,
        "observations_scored": len(observations),
        "scans_scored": len(scan_means),
        "tickers_scored": len({o.ticker for o in observations}),
        "evaluation_as_of": evaluation_as_of.isoformat(),
    }

    leakage = {
        "outcome_snapshots_tested_at_signal_as_of": leak_checked,
        "admitted_before_resolution": leak_admitted,
        "refusal_reasons": dict(sorted(refusal_reasons.items())),
        "interpretation": (
            "Every outcome snapshot was offered to the EvidenceGateway at its "
            "own signal instant and refused. A nonzero admitted count would "
            "mean the future was visible to the signal side."),
    }

    recorded_at = datetime.now(timezone.utc)
    refs = tuple(scored_refs)
    claim = build_claim(recorded_at, refs)
    universe = tuple(sorted({o.ticker for o in observations}))
    spec = build_spec(claim.claim_id, as_of=evaluation_as_of,
                      universe=universe, recorded_at=recorded_at)

    observations_payload = {
        "verdict": verdict,
        "population": population,
        "metrics": metrics,
        "leakage_controls": leakage,
        "friction_applied_pct": friction,
        "benchmark": BENCHMARK_TICKER,
        "preregistration_digest": PRE.preregistration_digest(root),
        "evidence_sha256": PRE.evidence_digest(root),
    }

    result = ExperimentResult(
        experiment_spec_id=spec.experiment_spec_id,
        provenance=Provenance(producer_id=RUNNER_ID, producer_type=PRODUCER_SYSTEM,
                              recorded_at=recorded_at, code_version=RUNNER_VERSION),
        windows_evaluated=(HORIZON_WINDOW,),
        evidence_refs=refs,
        observations=observations_payload,
        notes="Northstar vertical slice VS-001 — preregistered before execution.",
    )

    return {
        "research_claim": claim.to_canonical_dict(),
        "experiment_spec": spec.to_canonical_dict(),
        "experiment_result": result.to_canonical_dict(),
        "verdict": verdict,
        "metrics": metrics,
        "population": population,
        "leakage_controls": leakage,
    }
