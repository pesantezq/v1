"""VS-001 preregistration: frozen before the first Northstar vertical slice ran.

WHY A SEPARATE FREEZE EXISTS AT ALL.

``ExperimentSpec`` already carries a content-addressed identity, so a changed
hypothesis or metric is a different experiment. What it does not carry is the
*operational* detail this slice needs to be reproducible: which file the
evidence came from and its content hash, which rows were eligible, what the
friction assumption was, and how the acceptance rule reads. Freezing those here
means the question cannot be quietly re-aimed after the answer is known, which
is the failure this whole phase exists to prevent.

WHAT WAS ALREADY KNOWN WHEN THIS WAS WRITTEN.

Disclosed deliberately, because pretending to more innocence than we had would
itself be a form of p-hacking. ``outputs/performance/performance_summary.json``
already reported raw 7d win rate 0.607 and mean 7d return +2.76% over this
window. The excess-over-benchmark figure -- the actual subject of this
experiment -- had never been computed by anything in the repository, and was
not computed before this file was frozen.

``experimental_noncanonical``.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from portfolio_automation.northstar.canonical import canonical_dumps, deterministic_id

SCHEMA_VERSION = "engineering.vertical_slice.preregistration.v0"
SCHEMA_KIND = "experimental_noncanonical"

EXPERIMENT_ID = "VS-001"
PREREGISTRATION_VERSION = "v1"

#: The evidence file. Its content hash participates in the freeze, so editing
#: the data invalidates the preregistration rather than silently changing the
#: experiment underneath it.
EVIDENCE_REL = "outputs/performance/signal_outcomes.csv"

#: Round-trip friction, in percentage points, charged once per signal.
#: 10 bps = 5 bps entry + 5 bps exit, covering half-spread plus slippage on
#: large-cap US equities and ETFs. Commission is 0.0 because the operator's
#: broker is zero-commission on US equities. Frozen BEFORE any result was seen.
FRICTION_ROUND_TRIP_PCT = 0.10
FRICTION_COMMISSION_PCT = 0.0


def evidence_digest(repo_root: Path) -> str:
    """sha256 of the evidence file exactly as it sits on disk."""
    data = (Path(repo_root) / EVIDENCE_REL).read_bytes()
    return hashlib.sha256(data).hexdigest()


def preregistration_content(repo_root: Path) -> dict[str, Any]:
    """The frozen preregistration. Pure: derived from the repo, never from a result."""
    return {
        "schema_version": SCHEMA_VERSION,
        "schema_kind": SCHEMA_KIND,
        "experiment_id": EXPERIMENT_ID,
        "preregistration_version": PREREGISTRATION_VERSION,

        "research_question": (
            "Over the recorded 2026-05-12 to 2026-06-01 window, did StockBot's "
            "watchlist signals earn a positive 7-day return IN EXCESS OF SPY "
            "over the same interval, net of frozen friction -- or is the "
            "reported 60.7% raw win rate explained by market drift?"),

        "hypothesis": {
            "primary": (
                "Signals carry skill beyond the market: the scan-clustered mean "
                "7-day excess return over SPY, net of friction, is greater "
                "than zero."),
            "null": (
                "Signals carry no skill beyond the market: the scan-clustered "
                "mean net excess return is zero or negative, and the raw win "
                "rate reflects broad drift over the window rather than "
                "selection."),
            "direction": "increase",
        },

        "universe": {
            "definition": (
                "Every (ticker, scan) observation recorded in the evidence file "
                "whose 7-day outcome had matured, excluding the benchmark "
                "itself."),
            "eligible_entities": "all non-SPY tickers present in the evidence file",
            "excluded": [
                "SPY -- it is the benchmark and cannot be its own excess",
                "rows with no matured outcome_return_7d",
                "rows whose scan has no matured SPY 7d return to difference against",
            ],
            "membership_semantics": (
                "Membership is fixed by the recorded file and is NOT reconstructed "
                "from a live universe, so survivorship cannot be introduced by "
                "re-querying a present-day ticker list. The window is 20 calendar "
                "days and no listed constituent was delisted within it."),
            "survivorship_note": (
                "The 22 tickers were selected by the live system BEFORE outcomes "
                "existed. This is a fixed watchlist, not a survivor-filtered one."),
        },

        "evidence": {
            "source": EVIDENCE_REL,
            "source_type": "market_data",
            "access_class": "file",
            "pit_capability": "reconstructable",
            "signal_known_at_field": "signal_time",
            "signal_known_at_basis": "source_reported",
            "outcome_known_at_field": "evaluated_at_7d",
            "outcome_known_at_basis": "source_reported",
            "fields_used": [
                "ticker", "signal_time", "signal_score", "price_at_signal",
                "outcome_return_7d", "evaluated_at_7d",
            ],
            "missing_data_behavior": (
                "An unmatured or absent outcome is EXCLUDED, never imputed and "
                "never treated as zero. Exclusions are counted and reported."),
            "revision_vintage_required": False,
            "revision_note": (
                "The file records one immutable observation per (ticker, scan); "
                "no restatement mechanism exists upstream, so vintage handling is "
                "NOT_REQUIRED_FOR_FIRST_SLICE rather than assumed correct."),
        },

        "time": {
            "signal_observation_time": "row signal_time (33 distinct scans)",
            "evaluation_horizon": "7 calendar days, as resolved upstream into outcome_return_7d",
            "window_start": "2026-05-12T20:39:45.890480",
            "window_end": "2026-06-01T09:00:56.500240",
            "train_test_split": (
                "NONE. Nothing is fitted, so there is no parameter that could "
                "overfit and no split to leak across. The signal scores were "
                "produced live by the system before any outcome existed."),
            "embargo_rule": (
                "An outcome may enter the computation only at an as_of at or "
                "after its evaluated_at_7d. Enforced by the EvidenceGateway, "
                "not by convention."),
        },

        "benchmark": {
            "primary": "SPY 7-day return over the SAME scan timestamp",
            "rationale": (
                "Matching on the scan timestamp holds the interval fixed, so the "
                "difference is selection skill rather than a timing artifact."),
            "secondary_reference": (
                "raw (unadjusted) 7d return, reported alongside so the size of "
                "the drift correction is visible"),
            "no_action_baseline": (
                "Holding cash earns 0% excess by construction; the null of zero "
                "net excess IS the do-nothing arm."),
        },

        "evaluation": {
            "primary_metric": "scan_clustered_mean_net_excess_return_pct",
            "primary_metric_definition": (
                "For each scan: mean over eligible tickers of "
                "(outcome_return_7d - SPY outcome_return_7d at that scan) minus "
                "friction. Then the mean across scans, with a 95% normal-"
                "approximation CI over the scan-level means."),
            "clustering_rationale": (
                "Observations inside one scan share the same interval and are "
                "strongly cross-correlated; ~21 tickers per scan are close to one "
                "bet, not 21. Treating rows as independent would shrink the CI by "
                "roughly sqrt(21) and manufacture significance. The scan is the "
                "unit of independent observation, so N is ~20, not ~420."),
            "secondary_metrics": [
                "naive_per_row_mean_net_excess_return_pct (diagnostic only)",
                "excess_win_rate with Wilson 95% CI",
                "raw_win_rate (for contrast with the published 0.607)",
                "spearman_ic_signal_score_vs_net_excess",
                "mean_raw_return_pct",
                "mean_benchmark_return_pct",
            ],
            "risk_metrics": [
                "worst_scan_mean_net_excess_pct",
                "share_of_scans_with_negative_mean_net_excess",
            ],
            "acceptance_rule": (
                "SUPPORTED only if the scan-clustered mean net excess return is "
                "> 0 AND the lower bound of its 95% CI is > 0. "
                "NOT_SUPPORTED if the mean is <= 0 or the CI includes 0. "
                "INCONCLUSIVE_SMALL_SAMPLE if fewer than 10 scans are eligible."),
            "threshold_position": (
                "No effect size is required to 'pass'. This slice asks whether "
                "the effect is distinguishable from zero at all. A negative or "
                "null result is a successful execution of the experiment, not a "
                "failure of it."),
            "known_limitations_declared_in_advance": [
                "~20 scans over 20 calendar days is a very small sample; the CI "
                "will be wide and a null result will NOT prove the absence of skill.",
                "Overlapping 7-day horizons across adjacent scans induce serial "
                "correlation that scan-clustering reduces but does not remove.",
                "One market regime only; regime_label is degenerate ('neutral' in "
                "all 726 rows, a known upstream collapse), so no regime "
                "conditioning is attempted.",
                "confidence_score spans only [0.835, 1.0], too narrow to calibrate.",
                "Returns are unadjusted for corporate actions; over a 7-day window "
                "on these large-cap names the risk is small but not zero.",
            ],
        },

        "friction": {
            "round_trip_pct": FRICTION_ROUND_TRIP_PCT,
            "commission_pct": FRICTION_COMMISSION_PCT,
            "components": "5 bps entry + 5 bps exit (half-spread plus slippage)",
            "execution_delay": (
                "Assumed zero. The recorded price_at_signal is the price at the "
                "scan instant, and the outcome price is taken at the same daily "
                "granularity, so no intraday delay is modelled. This FLATTERS the "
                "strategy and is declared rather than hidden."),
            "turnover": "one round trip per signal; no netting across overlapping holds",
            "rationale": (
                "Friction is charged to the signal arm only. The benchmark arm is "
                "buy-and-hold SPY over the same interval and incurs no round trip, "
                "which is the honest comparison: the do-nothing alternative does "
                "not trade."),
        },

        "leakage_controls": {
            "future_data_exclusion": (
                "Outcome snapshots carry known_at = evaluated_at_7d and are "
                "refused by the EvidenceGateway at any as_of before it."),
            "pit_join": (
                "Signal and outcome are separate EvidenceSnapshots joined only by "
                "(ticker, scan); the outcome is never visible at signal as_of."),
            "survivorship": "fixed recorded watchlist; no present-day re-query",
            "revisions": "none upstream; declared NOT_REQUIRED_FOR_FIRST_SLICE",
            "label_construction": (
                "The label is the upstream-recorded outcome_return_7d. This slice "
                "does not recompute it from prices and does not choose the exit."),
            "outcome_maturation": (
                "286 of 726 rows had no matured 7d outcome and are excluded, not "
                "waited for and not imputed."),
        },

        "reproducibility": {
            "code_identity": "git commit of the candidate that executes the run",
            "evidence_identity": "sha256 of " + EVIDENCE_REL,
            "configuration": "this preregistration, by digest",
            "random_seeds": "NONE — the computation is fully deterministic",
            "model_identity": (
                "NONE — no model is invoked. The signal scores were produced "
                "earlier by the live system and are read as recorded data."),
        },
    }


def preregistration_digest(repo_root: Path) -> str:
    """Identity of the frozen question, including the data it will be asked of."""
    content = preregistration_content(repo_root)
    return deterministic_id("vsfreeze", {
        "content": content,
        "evidence_sha256": evidence_digest(repo_root),
    })


def frozen_document(repo_root: Path) -> dict[str, Any]:
    return {
        "preregistration": preregistration_content(repo_root),
        "evidence_sha256": evidence_digest(repo_root),
        "freeze_digest": preregistration_digest(repo_root),
    }


def verify_freeze(repo_root: Path, registered: dict[str, Any]) -> tuple[bool, list[str]]:
    """Re-derive the freeze from current code and data and compare.

    Fails closed: any drift in the question, the acceptance rule, the friction
    assumption or the evidence file itself is a mismatch, not a warning."""
    reasons: list[str] = []
    current = preregistration_content(repo_root)
    if registered.get("preregistration") != current:
        reasons.append("preregistration content differs from current code")
    current_evidence = evidence_digest(repo_root)
    if registered.get("evidence_sha256") != current_evidence:
        reasons.append(
            "evidence file changed: registered "
            f"{registered.get('evidence_sha256')} != current {current_evidence}")
    current_digest = preregistration_digest(repo_root)
    if registered.get("freeze_digest") != current_digest:
        reasons.append(
            f"freeze digest differs: registered {registered.get('freeze_digest')} "
            f"!= current {current_digest}")
    return (not reasons), reasons


def write_frozen(repo_root: Path, rel: str) -> Path:
    path = Path(repo_root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(frozen_document(repo_root), indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return path
