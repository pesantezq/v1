"""Worker Control Center — controller-owned read-model projections.

Trusted, NON-AUTHORITATIVE projections of authoritative EW-0A state for the GUI:

    authoritative state  ->  trusted projection (here)  ->  GUI

NOT the reverse. This module is READ-ONLY by construction: it imports only the
read accessors (read_authority_level, read_runtime_policy, read_outcomes) and
never any mutation function (no set_authority_level, no write_runtime_policy, no
certify/dispatch). Projections carry NO secrets (no API key/headers/hidden
reasoning). Fields with no authoritative backend are ``PENDING_BACKEND`` — never
fabricated (no invented heartbeat/health/latency/queue).

``experimental_noncanonical``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER
from portfolio_automation.engineer_worker.ew0a_authority import (
    read_authority_level, EngineerAuthorityLevel, FORBIDDEN_OPS)
from portfolio_automation.engineer_worker.ew0a_loop import read_runtime_policy
from portfolio_automation.engineer_worker.control_center_truth import (
    Capability, Readiness, ReadinessAssessment, TruthState, assess_readiness,
    classify,
)

SCHEMA_KIND = EXPERIMENTAL_MARKER
READMODEL_SCHEMA_VERSION = "engineering.readmodel.v0"
PENDING_BACKEND = "PENDING_BACKEND"


def _base(kind: str) -> dict[str, Any]:
    return {"schema_version": READMODEL_SCHEMA_VERSION, "schema_kind": SCHEMA_KIND, "read_model": kind}


# --- ControllerSummary (dynamic identity — never hardcodes Claude==controller) -
@dataclass(frozen=True)
class ControllerSummary:
    controller_identity: str            # e.g. "claude_code" (the CURRENT controller; may change)
    controller_role: str                # "authoritative_controller"
    controller_level: str               # "C_AUTHORITATIVE" (controller ladder; Engineer C0.5 tracked separately)
    current_mission: str | None
    operational_state: str
    controller_since: str               # PENDING_BACKEND if not authoritatively recorded
    escalation_role: str                # who this controller escalates TO
    security_classification: str = "operational"
    is_current_state: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {**_base("ControllerSummary"), **asdict(self)}


# --- SupervisorSummary (GPT independent verifier — never exposes the key) -----
@dataclass(frozen=True)
class SupervisorSummary:
    availability: str                   # "AVAILABLE" | "OUTAGE" | PENDING_BACKEND
    current_state: str
    #: ``None`` means the records ledger could not answer -- never a measured 0.
    recent_pass: int | None
    recent_repair: int | None
    recent_escalate: int | None
    recent_abstain: int | None
    recent_unavailable: int | None
    last_successful_verification: str | None
    measured_latency_ms: str            # PENDING_BACKEND (no real latency record)
    verification_queue: str             # PENDING_BACKEND (no real queue)
    outage_state: str
    #: Whether the underlying records ledger was usable at all.
    records_evidence: str = TruthState.LIVE.value
    security_classification: str = "operational"

    def to_dict(self) -> dict[str, Any]:
        return {**_base("SupervisorSummary"), **asdict(self)}


# --- Worker + authority ------------------------------------------------------
@dataclass(frozen=True)
class WorkerAuthoritySummary:
    level: str
    grants: list[str]
    forbidden_ops: list[str]
    can_mutate_main: bool = False
    can_merge: bool = False
    can_deploy: bool = False
    can_write_production: bool = False
    can_self_promote: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {**_base("WorkerAuthoritySummary"), **asdict(self)}


@dataclass(frozen=True)
class WorkerSummary:
    worker_identity: str                # persistent identity, e.g. "engineer.local_qwen2_5_7b"
    role: str                           # "engineer"
    operational_state: str              # PENDING_BACKEND (no WorkerHeartbeatV0 yet)
    ew_authority: str
    controller_level: str               # "C0.5_SHADOW" (apprenticeship)
    current_mission: str | None
    current_task: str                   # PENDING_BACKEND (no live dispatch record)
    queue_size: str                     # PENDING_BACKEND
    activity_summary: str               # PENDING_BACKEND
    next_action: str                    # PENDING_BACKEND
    recent_verification_outcomes: list[str]
    escalation_state: str
    #: Whether the records ledger behind recent_verification_outcomes was usable.
    #: An empty list from an unusable ledger is not an absence of verdicts, and a
    #: consumer reading only this summary must be able to tell the difference.
    records_evidence: str = TruthState.LIVE.value

    def to_dict(self) -> dict[str, Any]:
        return {**_base("WorkerSummary"), **asdict(self)}


# --- Mission (progress from VERIFIED deliverables, NOT raw task counts) -------
@dataclass(frozen=True)
class MissionSummary:
    mission_id: str
    deliverables: dict[str, str]        # name -> VERIFIED|NOT_STARTED|IN_PROGRESS
    verified_count: int
    total_required: int
    is_complete: bool                   # only when ALL required milestone deliverables VERIFIED
    completion_note: str = "progress is (verified required deliverables); never raw task count"

    def to_dict(self) -> dict[str, Any]:
        return {**_base("MissionSummary"), **asdict(self)}


# --- Verification ladder (worker_complete != VERIFIED) -----------------------
@dataclass(frozen=True)
class VerificationSummary:
    task_id: str
    implementation_result: str          # e.g. "COMPLETE" (a CLAIM, not authority)
    scope_policy_gate: str              # PASS|FAIL
    deterministic_verification: str     # PASS|FAIL
    acceptance_criteria: str            # PASS|FAIL|NOT_EVALUATED
    gpt_verdict: str                    # PASS|REPAIR|ESCALATE|ABSTAIN|NOT_CONSULTED|UNAVAILABLE
    final_status: str                   # VERIFIED only if deterministic PASS AND gpt PASS
    worker_complete_is_not_verified: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {**_base("VerificationSummary"), **asdict(self)}


def project_verification(task_id: str, implementation_result: str, scope_policy_gate: str,
                         deterministic: str, acceptance: str, gpt_verdict: str) -> VerificationSummary:
    """Project the verification ladder + final status. A deterministic failure that
    short-circuits the supervisor projects GPT as NOT_CONSULTED (never PASS/FAIL)."""
    det_ok = deterministic == "PASS" and scope_policy_gate == "PASS"
    if not det_ok:
        gpt = "NOT_CONSULTED"
        final = "NOT_VERIFIED"
    else:
        gpt = gpt_verdict
        final = "VERIFIED" if gpt_verdict == "PASS" and acceptance in ("PASS", "NOT_EVALUATED") else "NOT_VERIFIED"
    return VerificationSummary(task_id=task_id, implementation_result=implementation_result,
                               scope_policy_gate=scope_policy_gate, deterministic_verification=deterministic,
                               acceptance_criteria=acceptance, gpt_verdict=gpt, final_status=final)


# --- Apprenticeship (honest; never smooths negative evidence) ----------------
@dataclass(frozen=True)
class ApprenticeshipSummary:
    controller_level: str
    #: ``None`` means the records ledger could not answer -- never a measured 0.
    decisions_shadowed: int | None
    task_selection_agreements: int | None
    risk_agreements: int | None
    routing_agreements: int | None
    missed_escalations: int | None
    unsafe_underclassifications: int | None
    authority_expansion_proposals: int | None
    c1_readiness: str                   # NOT_READY | CANDIDATE | READY_FOR_CERTIFICATION
    #: Whether the underlying records ledger was usable at all.
    records_evidence: str = TruthState.LIVE.value

    def to_dict(self) -> dict[str, Any]:
        return {**_base("ApprenticeshipSummary"), **asdict(self)}


@dataclass(frozen=True)
class AttentionItem:
    kind: str                           # E4 | CAPITAL_POLICY | ARCHITECTURE_FORK | SECURITY | COMPLIANCE | SPENDING | UNRESOLVED_ESCALATION | CERTIFICATION_APPROVAL
    summary: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {**_base("AttentionItem"), **asdict(self)}


@dataclass(frozen=True)
class SystemHealthSummary:
    controller: str
    gpt_supervisor: str
    engineer_runtime: str
    sandbox: str
    evidence_bridge: str
    authority: str
    control_loop: str

    def to_dict(self) -> dict[str, Any]:
        return {**_base("SystemHealthSummary"), **asdict(self)}


# ---------------------------------------------------------------------------
# Builders over authoritative sources (READ-ONLY)
# ---------------------------------------------------------------------------
CONTROLLER_RECORDS_REL = "docs/EW0A_0B3_RECORDS.jsonl"


@dataclass(frozen=True)
class ControllerRecordsRead:
    """The outcome of reading the controller records ledger.

    The previous reader was unsafe in both directions at once. A malformed JSON
    line was silently skipped, so an unreadable ledger produced a SHORTER list
    that consumers then reported as measured history — an unparseable file became
    ``0 PASS`` as though zero had been observed. And a syntactically valid
    non-object row was admitted into the list, so the first ``row.get(...)``
    downstream raised ``AttributeError`` and took the dashboard with it.

    Neither is repaired by filtering: dropping bad rows is precisely how partial
    evidence comes to masquerade as complete evidence. The ledger is either
    usable in full or it is unusable, and a genuinely empty ledger stays
    distinguishable from a corrupt one."""

    availability: str                       # LIVE | UNAVAILABLE
    records: list[dict[str, Any]]
    detail: str = ""

    @property
    def is_usable(self) -> bool:
        return self.availability == TruthState.LIVE.value

    def to_dict(self) -> dict[str, Any]:
        return {**_base("ControllerRecordsRead"),
                "availability": self.availability,
                "record_count": len(self.records),
                "source": CONTROLLER_RECORDS_REL,
                "detail": self.detail}


def read_controller_records(repo_root: str | Path,
                            rel: str = CONTROLLER_RECORDS_REL) -> ControllerRecordsRead:
    """Read the controller records ledger, or refuse it whole.

    Absence is UNAVAILABLE rather than PENDING_BACKEND: the controller writes
    these records, so a missing ledger is an operational condition and not
    evidence that nobody built the producer."""
    path = Path(repo_root) / rel
    if not path.exists():
        return ControllerRecordsRead(TruthState.UNAVAILABLE.value, [],
                                     f"{rel} is absent")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return ControllerRecordsRead(
            TruthState.UNAVAILABLE.value, [],
            f"{rel} is unreadable ({type(exc).__name__})")

    rows: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            # NOT skipped. A ledger we cannot fully parse is not a shorter ledger.
            return ControllerRecordsRead(
                TruthState.UNAVAILABLE.value, [],
                f"{rel} line {index} is not valid JSON")
        if not isinstance(row, dict):
            # Type name only; the malformed payload is never echoed.
            return ControllerRecordsRead(
                TruthState.UNAVAILABLE.value, [],
                f"{rel} line {index} is not a JSON object, got {type(row).__name__}")
        rows.append(row)
    return ControllerRecordsRead(TruthState.LIVE.value, rows,
                                 f"{len(rows)} record(s) from {rel}")


def _read_records(repo_root: Path, rel: str = CONTROLLER_RECORDS_REL) -> list[dict[str, Any]]:
    """Backward-compatible accessor: the usable rows, or none at all.

    Retained so existing callers keep working. New code should use
    :func:`read_controller_records` and consult ``availability`` -- an empty list
    from here cannot distinguish an empty ledger from an unusable one."""
    return read_controller_records(repo_root, rel).records


def build_supervisor_summary(records: list[dict[str, Any]],
                            records_evidence: str = TruthState.LIVE.value
                            ) -> SupervisorSummary:
    """Project supervisor verdict history.

    ``records_evidence`` defaults to LIVE so existing callers passing a plain
    list are unchanged. When the ledger is UNUSABLE the counts are ``None`` --
    NOT zero. A measured zero and an unanswerable question look identical as
    ``0``, and for a verdict history that difference is the whole point: an
    operator reading ``0 REPAIR`` from an unreadable ledger would conclude
    nothing had gone wrong."""
    usable = records_evidence == TruthState.LIVE.value
    if not usable:
        return SupervisorSummary(
            availability=PENDING_BACKEND, current_state=PENDING_BACKEND,
            recent_pass=None, recent_repair=None, recent_escalate=None,
            recent_abstain=None, recent_unavailable=None,
            last_successful_verification=None, measured_latency_ms=PENDING_BACKEND,
            verification_queue=PENDING_BACKEND, outage_state=PENDING_BACKEND,
            records_evidence=records_evidence)
    verdicts = [r.get("gpt_verdict") for r in records if r.get("gpt_verdict")]
    def c(v):
        return sum(1 for x in verdicts if str(x).upper() == v)
    last_pass = next((r.get("recorded_at") for r in reversed(records)
                      if str(r.get("gpt_verdict", "")).upper() == "PASS"), None)
    return SupervisorSummary(
        availability=PENDING_BACKEND, current_state=PENDING_BACKEND,
        recent_pass=c("PASS"), recent_repair=c("REPAIR"), recent_escalate=c("ESCALATE"),
        recent_abstain=c("ABSTAIN"), recent_unavailable=c("SUPERVISOR_UNAVAILABLE"),
        last_successful_verification=last_pass, measured_latency_ms=PENDING_BACKEND,
        verification_queue=PENDING_BACKEND, outage_state=PENDING_BACKEND,
        records_evidence=records_evidence)


def build_apprenticeship_summary(records: list[dict[str, Any]],
                                 records_evidence: str = TruthState.LIVE.value
                                 ) -> ApprenticeshipSummary:
    """Project the C0.5 apprenticeship metrics.

    When the ledger is unusable every count is ``None``, not zero. Zero
    ``unsafe_underclassifications`` is the single most flattering number this
    projection can emit, and it must never be produced by a ledger that could
    not be read."""
    if records_evidence != TruthState.LIVE.value:
        return ApprenticeshipSummary(
            controller_level="C0.5_SHADOW", decisions_shadowed=None,
            task_selection_agreements=None, risk_agreements=None,
            routing_agreements=None, missed_escalations=None,
            unsafe_underclassifications=None, authority_expansion_proposals=None,
            c1_readiness="NOT_READY", records_evidence=records_evidence)
    comps = [r for r in records if r.get("kind") == "ApprenticeshipComparison"]
    shadowed = len([r for r in records if r.get("kind") == "ControllerDecisionCandidateV0"])
    return ApprenticeshipSummary(
        controller_level="C0.5_SHADOW",
        decisions_shadowed=shadowed,
        task_selection_agreements=sum(1 for c in comps if c.get("engineer_proposed_task_relates_to_experimentspec")),
        risk_agreements=sum(1 for c in comps if c.get("risk_agreement")),
        routing_agreements=sum(1 for c in comps if c.get("routing_agreement")),
        missed_escalations=sum(1 for c in comps if c.get("danger_underclassified_architecture_as_engineer")),
        unsafe_underclassifications=sum(1 for c in comps if c.get("danger_underclassified_architecture_as_engineer")),
        authority_expansion_proposals=0,
        c1_readiness="NOT_READY", records_evidence=records_evidence)


def build_worker_authority_summary(level: EngineerAuthorityLevel, grants: list[str]) -> WorkerAuthoritySummary:
    return WorkerAuthoritySummary(level=level.value, grants=grants, forbidden_ops=sorted(FORBIDDEN_OPS))


_NORTHSTAR_0B3 = ("ExperimentSpec", "ExperimentResult", "CapitalProposal",
                  "ExitProposal", "OutcomeRecord", "StrategyPassport")

# Deliverable sets are MISSION-SCOPED. The six 0B.3 contracts describe that
# milestone and nothing else; reporting them under a different mission would
# show a completed phase's progress as the current phase's progress. That drift
# was caught in senior review of the first 0C session, where the runtime mission
# had moved to 0C while this projection still reported the 0B.3 six.
_MISSION_DELIVERABLES: dict[str, tuple[str, ...]] = {
    "northstar_0b_decision_outcome_passport_contracts": _NORTHSTAR_0B3,
}


def build_mission_summary(mission_id: str, present: set[str]) -> MissionSummary:
    """Project mission progress ONLY for a mission whose deliverable set is known.

    For any other mission the deliverables are reported as unknown rather than
    borrowed from a different milestone: a dashboard that shows 0B completion
    while the controller is running 0C is worse than one that admits it does not
    know, because the first is confidently wrong."""
    required = _MISSION_DELIVERABLES.get(mission_id)
    if required is None:
        return MissionSummary(
            mission_id=mission_id, deliverables={}, verified_count=0,
            total_required=0, is_complete=False,
            completion_note=(
                f"{PENDING_BACKEND}: no authoritative deliverable set is projected "
                f"for mission {mission_id!r}; progress is deliberately NOT inferred "
                f"from another mission's deliverables"))
    deliverables = {name: ("VERIFIED" if name in present else "NOT_STARTED")
                    for name in required}
    verified = sum(1 for v in deliverables.values() if v == "VERIFIED")
    return MissionSummary(mission_id=mission_id, deliverables=deliverables,
                          verified_count=verified, total_required=len(required),
                          is_complete=(verified == len(required)))


def _assess_backend_truth(*, level: Any, policy: Any, records: list[dict[str, Any]],
                          worker: Any, now: str | None,
                          records_evidence: str = TruthState.LIVE.value
                          ) -> ReadinessAssessment:
    """Classify every oversight capability from the evidence actually present.

    Each capability declares whether a PRODUCER exists. That is an engineering
    fact about this repository, not a runtime observation, and it is what keeps
    a missing subsystem reported as PENDING_BACKEND instead of as an outage.

    Nothing here builds a backend. A capability with no producer stays pending;
    the honest answer is the deliverable."""
    # Every row here has been validated as an object by read_controller_records,
    # so .get() is safe. When the ledger is unusable there is no verification to
    # age, and supervisor_state resolves to UNAVAILABLE rather than to a
    # freshness verdict computed over evidence nobody could read.
    last_verification = None
    if records_evidence == TruthState.LIVE.value:
        for rec in reversed(records):
            if rec.get("gpt_verdict") and rec.get("recorded_at"):
                last_verification = rec["recorded_at"]
                break

    caps = [
        # Authority and mission come from protected config files that are read
        # directly. They are authoritative-by-file and do not decay, so
        # demanding a timestamp would manufacture UNKNOWNs.
        Capability("controller_state",
                   classify(producer_exists=True,
                            value=policy.mission_id if policy else None,
                            requires_freshness=False),
                   required=True,
                   detail="config/ew0a_runtime.json (protected, read-only here)"),
        Capability("worker_authority",
                   classify(producer_exists=True,
                            value=getattr(level, "value", None),
                            requires_freshness=False),
                   required=True,
                   detail="config/ew0a_authority.json (protected, read-only here)"),
        Capability("mission_state",
                   classify(producer_exists=True,
                            value=policy.mission_id if policy else None,
                            requires_freshness=False),
                   required=True, detail="runtime policy mission_id"),
        # A producer EXISTS for verification history (the records ledger), so
        # its freshness is measurable and it can legitimately go STALE.
        Capability("supervisor_state",
                   classify(producer_exists=True, value=last_verification,
                            recorded_at=last_verification, now=now,
                            threshold="verification"),
                   required=True,
                   detail=(f"recorded gpt_verdict history; records ledger "
                           f"{records_evidence}")),
        # No producer has been built for any of these. Building them is
        # explicitly out of scope for this mission.
        Capability("worker_activity",
                   classify(producer_exists=False, value=None),
                   required=True,
                   detail="no WorkerHeartbeatV0 producer exists"),
        Capability("queue_state", classify(producer_exists=False, value=None),
                   required=False, detail="no dispatch queue producer exists"),
        Capability("component_health", classify(producer_exists=False, value=None),
                   required=False, detail="no health-probe producer exists"),
        Capability("controller_since", classify(producer_exists=False, value=None),
                   required=False, detail="no controller-session record exists"),
    ]
    return assess_readiness(caps)


def build_dashboard(repo_root: str | Path, now: str | None = None) -> dict[str, Any]:
    """Assemble the full read-only dashboard from authoritative sources.

    ``now`` is injected rather than read from the clock (the no-fabricated-time
    discipline used across the Northstar contracts); readiness assessment needs a
    timestamp and a projection must never invent one."""
    root = Path(repo_root)
    level = read_authority_level(root)
    policy = read_runtime_policy(root)
    records_read = read_controller_records(root)
    records = records_read.records
    mission = policy.mission_id if policy else None

    # authoritative contract presence -> mission progress
    try:
        import portfolio_automation.northstar as ns
        present = {n for n in _NORTHSTAR_0B3 if hasattr(ns, n)}
    except Exception:  # noqa: BLE001
        present = set()

    # A SECOND read of the same protected record, and the same defect class as
    # blocker A:  on a non-object root raised AttributeError,
    # which the guard did not name, so a scalar/array/null authority file took
    # the whole dashboard down even after the canonical reader was made total.
    # The root is checked before it is indexed, and a non-list grants value is
    # not rendered.
    #
    # This makes the READ total and non-leaking. Classifying the QUALITY of this
    # record as evidence (record_evidence LIVE/UNAVAILABLE) is PR #35 GUI-R work
    # and is deliberately not duplicated here.
    grants: list[str] = []
    ap = root / "config" / "ew0a_authority.json"
    if ap.exists():
        try:
            record = json.loads(ap.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
        raw_grants = record.get("grants") if isinstance(record, dict) else None
        if isinstance(raw_grants, list) and all(isinstance(g, str) for g in raw_grants):
            grants = list(raw_grants)

    controller = ControllerSummary(
        controller_identity="claude_code", controller_role="authoritative_controller",
        controller_level="C_AUTHORITATIVE", current_mission=mission,
        operational_state="ACTIVE", controller_since=PENDING_BACKEND, escalation_role="human")
    worker = WorkerSummary(
        worker_identity="engineer.local_qwen2_5_7b", role="engineer",
        operational_state=PENDING_BACKEND, ew_authority=level.value, controller_level="C0.5_SHADOW",
        current_mission=mission, current_task=PENDING_BACKEND, queue_size=PENDING_BACKEND,
        activity_summary=PENDING_BACKEND, next_action=PENDING_BACKEND,
        recent_verification_outcomes=(
            [str(r.get("gpt_verdict")) for r in records if r.get("gpt_verdict")][-5:]
            if records_read.is_usable else []),
        escalation_state="none",
        records_evidence=records_read.availability)
    health = SystemHealthSummary(
        controller="ACTIVE", gpt_supervisor=PENDING_BACKEND, engineer_runtime=PENDING_BACKEND,
        sandbox=PENDING_BACKEND, evidence_bridge=PENDING_BACKEND, authority=level.value,
        control_loop="READY")
    dashboard = {
        **_base("Dashboard"),
        "controller": controller.to_dict(),
        "supervisor": build_supervisor_summary(
            records, records_read.availability).to_dict(),
        "worker": worker.to_dict(),
        "worker_authority": build_worker_authority_summary(level, grants).to_dict(),
        "mission": build_mission_summary(mission or "unknown", present).to_dict(),
        "apprenticeship": build_apprenticeship_summary(
            records, records_read.availability).to_dict(),
        "attention_items": [],   # only human-relevant items; none outstanding
        "system_health": health.to_dict(),
        # The ledger's own usability, stated once and authoritatively, so a
        # consumer does not have to infer it from three separate summaries.
        "controller_records": records_read.to_dict(),
    }
    # Backend truth states + capability readiness. Derived from the evidence
    # just assembled -- never asserted, and never a LIVE percentage.
    dashboard["backend_truth"] = _assess_backend_truth(
        level=level, policy=policy, records=records, worker=worker, now=now,
        records_evidence=records_read.availability).to_dict()
    # Learning projections (Phase 13). Degrade to PENDING_BACKEND rather than
    # failing the whole dashboard if the learning store is absent.
    try:
        from portfolio_automation.engineer_worker.learning.readmodels import (
            build_learning_dashboard)
        dashboard["learning"] = build_learning_dashboard(
            root, worker.worker_identity, now or PENDING_BACKEND)
    except Exception:  # noqa: BLE001
        dashboard["learning"] = PENDING_BACKEND

    # Active autonomous-session projection. This is what makes an unattended
    # session WATCHABLE through the established controller-owned path:
    #
    #     session ledger (controller evidence) -> read model (here) -> GUI
    #
    # Read-only and NON-AUTHORITATIVE, like every other projection in this
    # module. Absent when no session ledger exists — an absent session is
    # reported as absent, never synthesized.
    dashboard["active_session"] = _build_active_session(root)
    return dashboard


def _build_active_session(repo_root: Path) -> dict[str, Any] | str:
    """Project the current autonomous session, or PENDING_BACKEND if none.

    Degrades rather than failing the dashboard: an observability problem must
    never make the engineering evidence unreadable."""
    try:
        from tools.ns0c_session import ledger_path, session_projection
        if not ledger_path(repo_root).exists():
            return PENDING_BACKEND
        return session_projection(repo_root=repo_root)
    except Exception:  # noqa: BLE001
        return PENDING_BACKEND
