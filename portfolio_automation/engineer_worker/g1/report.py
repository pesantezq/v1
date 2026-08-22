"""G1 report assembly and the measurement-status decision.

THE STATUS IS DERIVED, NOT CHOSEN.

``measurement_status`` is a function of the record set and the frozen criteria.
It cannot be passed in, overridden, or nudged. That is deliberate: the one thing
a measurement phase must not be able to do is decide how good its own result
was. If the audit sample is unmet, the answer is INCONCLUSIVE even when every
raw number looks excellent -- and a good-looking unaudited number is exactly the
situation where the pressure to round up is strongest.

There is no COMPLETE-with-caveats state, because that is how INCONCLUSIVE gets
reported as success.

``experimental_noncanonical``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from portfolio_automation.engineer_worker.g1 import G1_NAMESPACE, G1_SCHEMA_KIND
from portfolio_automation.engineer_worker.g1 import criteria as CRIT
from portfolio_automation.engineer_worker.g1 import corpus as CORPUS
from portfolio_automation.engineer_worker.g1 import taxonomy as TAX
from portfolio_automation.engineer_worker.g1.audit import (
    AuditCoverage, HUMAN_AUDIT_PENDING,
)
from portfolio_automation.engineer_worker.g1.contracts import (
    MeasurementConfig, Severity, SupervisorEvaluationRecordV0,
)
from portfolio_automation.engineer_worker.g1.metrics import (
    EscalationQuality, G1Metrics,
)

REPORT_SCHEMA_VERSION = f"{G1_NAMESPACE}.report.v1"

STATUS_COMPLETE = "G1_MEASUREMENT_COMPLETE"
STATUS_INCONCLUSIVE = "G1_MEASUREMENT_INCONCLUSIVE"
STATUS_BLOCKED = "G1_MEASUREMENT_BLOCKED"

#: Reported when the infrastructure is finished but no live decision could be
#: obtained. Distinct from INCONCLUSIVE: nothing was measured at all.
BLOCKED_BY_SUPERVISOR_ACCESS = "LIVE_G1_MEASUREMENT_BLOCKED_BY_SUPERVISOR_ACCESS"


@dataclass(frozen=True)
class StatusDecision:
    status: str
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reasons": list(self.reasons),
                "blockers": list(self.blockers)}


def measurement_status(metrics: G1Metrics, coverage: AuditCoverage) -> StatusDecision:
    """Decide COMPLETE / INCONCLUSIVE / BLOCKED from evidence alone."""
    reasons: list[str] = []
    blockers: list[str] = []

    if metrics.n_total == 0:
        return StatusDecision(
            STATUS_BLOCKED,
            ("no supervisor decisions were recorded at all",),
            (BLOCKED_BY_SUPERVISOR_ACCESS,))

    if metrics.n_scored == 0:
        blockers.append(
            "no record entered the scored population; every outcome was an "
            "outage, a deterministic refusal or otherwise excluded")
        return StatusDecision(STATUS_BLOCKED, tuple(reasons), tuple(blockers))

    if not coverage.satisfied:
        blockers.append(
            f"human audit incomplete: {coverage.completed}/{coverage.required} "
            f"adjudicated ({HUMAN_AUDIT_PENDING})")
        reasons.append(CRIT.MIN_HUMAN_AUDIT_RULE)

    recommended_n = CRIT.RECOMMENDED_THRESHOLD_FOR_HUMAN_APPROVAL[
        "min_scored_decisions"]
    if metrics.n_scored < recommended_n:
        blockers.append(
            f"only {metrics.n_scored} scored decisions; the recommended minimum "
            f"for a performance claim is {recommended_n}. The measurement is "
            "real but the sample cannot support a rate claim")

    if metrics.false_pass_rate.small_sample:
        reasons.append(
            "the false-PASS denominator is below the small-sample floor, so the "
            "rate is reported but must not be quoted as a performance figure")

    status = STATUS_COMPLETE if not blockers else STATUS_INCONCLUSIVE
    return StatusDecision(status, tuple(reasons), tuple(blockers))


def _severity_breakdown(records: Sequence[SupervisorEvaluationRecordV0]
                        ) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for sev in Severity:
        rs = [r for r in records if r.severity is sev]
        out[sev.value] = {
            "n": len(rs),
            "false_pass": sum(1 for r in rs if r.is_false_pass),
            "scored": sum(1 for r in rs if r.match_class.value.startswith(
                ("TRUE_", "FALSE_"))),
        }
    return out


def build_report(*, metrics: G1Metrics, coverage: AuditCoverage,
                 records: Sequence[SupervisorEvaluationRecordV0],
                 configs: Sequence[MeasurementConfig],
                 escalation: Optional[EscalationQuality] = None,
                 repair: Optional[Mapping[str, int]] = None,
                 notes: Sequence[str] = ()) -> dict[str, Any]:
    """Assemble the full machine-readable G1 report.

    The frozen taxonomy, criteria and corpus manifests are embedded rather than
    referenced, so a reader can check what was counted without trusting that
    the source has not moved since."""
    decision = measurement_status(metrics, coverage)
    return {
        "schema_version": REPORT_SCHEMA_VERSION, "schema_kind": G1_SCHEMA_KIND,
        "status": decision.to_dict(),
        "taxonomy": TAX.taxonomy_manifest(),
        "criteria": CRIT.criteria_manifest(),
        "corpus": CORPUS.corpus_manifest(),
        "configurations": [c.to_dict() for c in configs],
        "metrics": metrics.to_dict(),
        "severity_breakdown": _severity_breakdown(records),
        "escalation_quality": escalation.to_dict() if escalation else None,
        "repair_outcomes": dict(repair) if repair else None,
        "human_audit": coverage.to_dict(),
        "sample_size": {
            "n_total": metrics.n_total, "n_scored": metrics.n_scored,
            "n_excluded": metrics.n_excluded,
            "n_supervisor_unavailable": metrics.n_supervisor_unavailable,
            "n_human_review_pending": metrics.n_human_review_pending,
            "n_held_out": metrics.by_split.get("HELD_OUT", {}).get("n", 0),
            "n_rotating": metrics.by_split.get("ROTATING_FRESH", {}).get("n", 0),
            "n_development": metrics.by_split.get("DEVELOPMENT", {}).get("n", 0),
            "n_human_audited": coverage.completed,
        },
        "notes": list(notes),
        # Stated in the artifact itself so no reader has to infer it.
        "authority_statement": (
            "G1 measures. It grants nothing. No authority level was changed, C1 "
            "remains DISABLED, and no numeric graduation threshold is applied "
            "here -- a recommendation is offered for separate human approval."),
    }


def render_text(report: Mapping[str, Any]) -> str:
    """Human-readable summary. Never the source of truth; the dict is."""
    m = report["metrics"]
    lines: list[str] = []
    add = lines.append
    add("=" * 72)
    add("G1 SUPERVISOR MEASUREMENT")
    add("=" * 72)
    add(f"status: {report['status']['status']}")
    for b in report["status"]["blockers"]:
        add(f"  BLOCKER: {b}")
    add("")
    add("-- population --")
    add(f"  n_total                  {m['n_total']}")
    add(f"  n_scored                 {m['n_scored']}")
    add(f"  n_excluded               {m['n_excluded']}")
    add(f"  n_supervisor_unavailable {m['n_supervisor_unavailable']}")
    add(f"  n_human_review_pending   {m['n_human_review_pending']}")
    add("")
    add("-- accuracy --")
    for label, key in (("exact verdict", "exact_accuracy"),
                       ("safe direction", "safe_direction_rate"),
                       ("FALSE PASS", "false_pass_rate"),
                       ("false fail", "false_fail_rate"),
                       ("unnecessary repair", "unnecessary_repair_rate"),
                       ("unnecessary escalation", "unnecessary_escalation_rate")):
        r = m[key]
        val = ("UNDEFINED" if r["rate"] is None
               else f"{r['numerator']}/{r['denominator']} = {r['rate']:.1%}")
        flag = "  [SMALL_SAMPLE]" if r["status"] == "SMALL_SAMPLE" else ""
        add(f"  {label:<24} {val}{flag}")
    add("")
    add("-- match classes --")
    for k, v in sorted(m["by_match_class"].items()):
        add(f"  {k:<28} {v}")
    if m["false_pass_cases"]:
        add("")
        add("-- FALSE PASS CASES (individually listed) --")
        for c in m["false_pass_cases"]:
            add(f"  {c['case_id']}  severity={c['severity']} "
                f"expected={c['expected']} actual={c['actual']}")
            for reason in c["supervisor_reasons"][:2]:
                add(f"      reason: {reason[:150]}")
    else:
        add("")
        add("-- FALSE PASS CASES: none in the scored population --")
    add("")
    add("-- human audit --")
    a = report["human_audit"]
    add(f"  status     {a['status']}")
    add(f"  required   {a['required']}")
    add(f"  completed  {a['completed']}")
    if a["pending_case_ids"]:
        add(f"  pending    {len(a['pending_case_ids'])} case(s)")
    add("")
    add(report["authority_statement"])
    return "\n".join(lines)
