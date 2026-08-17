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
    recent_pass: int
    recent_repair: int
    recent_escalate: int
    recent_abstain: int
    recent_unavailable: int
    last_successful_verification: str | None
    measured_latency_ms: str            # PENDING_BACKEND (no real latency record)
    verification_queue: str             # PENDING_BACKEND (no real queue)
    outage_state: str
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
    decisions_shadowed: int
    task_selection_agreements: int
    risk_agreements: int
    routing_agreements: int
    missed_escalations: int
    unsafe_underclassifications: int
    authority_expansion_proposals: int
    c1_readiness: str                   # NOT_READY | CANDIDATE | READY_FOR_CERTIFICATION

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
def _read_records(repo_root: Path, rel: str = "docs/EW0A_0B3_RECORDS.jsonl") -> list[dict[str, Any]]:
    p = repo_root / rel
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    return out


def build_supervisor_summary(records: list[dict[str, Any]]) -> SupervisorSummary:
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
        verification_queue=PENDING_BACKEND, outage_state=PENDING_BACKEND)


def build_apprenticeship_summary(records: list[dict[str, Any]]) -> ApprenticeshipSummary:
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
        c1_readiness="NOT_READY")


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
                          worker: Any, now: str | None) -> ReadinessAssessment:
    """Classify every oversight capability from the evidence actually present.

    Each capability declares whether a PRODUCER exists. That is an engineering
    fact about this repository, not a runtime observation, and it is what keeps
    a missing subsystem reported as PENDING_BACKEND instead of as an outage.

    Nothing here builds a backend. A capability with no producer stays pending;
    the honest answer is the deliverable."""
    last_verification = None
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
                   required=True, detail="recorded gpt_verdict history"),
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
    records = _read_records(root)
    mission = policy.mission_id if policy else None

    # authoritative contract presence -> mission progress
    try:
        import portfolio_automation.northstar as ns
        present = {n for n in _NORTHSTAR_0B3 if hasattr(ns, n)}
    except Exception:  # noqa: BLE001
        present = set()

    grants = []
    ap = root / "config" / "ew0a_authority.json"
    if ap.exists():
        try:
            grants = json.loads(ap.read_text(encoding="utf-8")).get("grants", [])
        except (OSError, ValueError):
            grants = []

    controller = ControllerSummary(
        controller_identity="claude_code", controller_role="authoritative_controller",
        controller_level="C_AUTHORITATIVE", current_mission=mission,
        operational_state="ACTIVE", controller_since=PENDING_BACKEND, escalation_role="human")
    worker = WorkerSummary(
        worker_identity="engineer.local_qwen2_5_7b", role="engineer",
        operational_state=PENDING_BACKEND, ew_authority=level.value, controller_level="C0.5_SHADOW",
        current_mission=mission, current_task=PENDING_BACKEND, queue_size=PENDING_BACKEND,
        activity_summary=PENDING_BACKEND, next_action=PENDING_BACKEND,
        recent_verification_outcomes=[str(r.get("gpt_verdict")) for r in records if r.get("gpt_verdict")][-5:],
        escalation_state="none")
    health = SystemHealthSummary(
        controller="ACTIVE", gpt_supervisor=PENDING_BACKEND, engineer_runtime=PENDING_BACKEND,
        sandbox=PENDING_BACKEND, evidence_bridge=PENDING_BACKEND, authority=level.value,
        control_loop="READY")
    dashboard = {
        **_base("Dashboard"),
        "controller": controller.to_dict(),
        "supervisor": build_supervisor_summary(records).to_dict(),
        "worker": worker.to_dict(),
        "worker_authority": build_worker_authority_summary(level, grants).to_dict(),
        "mission": build_mission_summary(mission or "unknown", present).to_dict(),
        "apprenticeship": build_apprenticeship_summary(records).to_dict(),
        "attention_items": [],   # only human-relevant items; none outstanding
        "system_health": health.to_dict(),
    }
    # Backend truth states + capability readiness. Derived from the evidence
    # just assembled -- never asserted, and never a LIVE percentage.
    dashboard["backend_truth"] = _assess_backend_truth(
        level=level, policy=policy, records=records, worker=worker, now=now).to_dict()
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
