"""Mission Control (GUI-1) — read-only presentation over the certified read model.

Answers, at a glance: what mission Northstar is running, the authority level,
which workers/components are represented, what happened recently, what is
ready / pending / unavailable / failed / blocked, what needs a human, what
evidence supports each value, and what the system genuinely does not know.

ARCHITECTURE (the invariant this module exists to keep):

    authoritative state -> certified readers -> ew0a_readmodels.build_dashboard()
                                                          |
                                                          v
                                            project_mission_control()  (pure)
                                                          |
                                                          v
                                                 mission_control.html

* The ONLY source of data is ``build_dashboard``. This module opens no file,
  reads no ledger, parses no authority record and imports no reader. A test
  asserts the module binds none of those facilities, and a second test renders
  the page against a repo root that does not exist with ``build_dashboard``
  stubbed — if the page still renders, it read nothing itself.
* It never writes. Rendering is proven not to change a single byte of the
  repository it is pointed at.
* It never re-derives truth. Every status shown is a status the read model
  already published (``TruthState``: LIVE / STALE / PENDING_BACKEND /
  UNAVAILABLE / UNKNOWN), plus two labels the contract itself uses for
  values that are not evidence: ``DERIVED`` (the ``can_*`` capability booleans,
  which the read model says are derived from FORBIDDEN_OPS) and
  ``CONTRACT_CONSTANT`` (identities the contract fixes rather than observes).
  No new status taxonomy is introduced.
* ``PENDING_BACKEND`` is shown as PENDING_BACKEND. It is never mapped to
  healthy / online / idle / ready, and never to the green severity.
* The "Failures & blockers" and "Unavailable / pending" sections are
  ORGANISATION of status fields the read model already returned. They are not
  an attention engine: while ``attention.derivation_state`` is PENDING_BACKEND
  the page says so and does not claim that an empty list means nothing needs
  the operator.

Split, mirroring the read model's own collect-then-project shape:
``collect_mission_control_view`` performs the one read-model call;
``project_mission_control`` is a pure function of the returned dict, so the
presentation model is deterministic for a fixed dashboard.
"""
from __future__ import annotations

from typing import Any

from portfolio_automation.engineer_worker.ew0a_readmodels import (
    PENDING_BACKEND,
    build_dashboard,
)

PERSONA = "mission_control"
ROUTE = "/dashboard/mission-control"

#: TruthState values published by the read model (control_center_truth.TruthState).
READ_MODEL_STATES: tuple[str, ...] = (
    "LIVE", "STALE", "PENDING_BACKEND", "UNAVAILABLE", "UNKNOWN")
#: Labels the contract uses for non-evidence values. Not truth states.
DERIVED = "DERIVED"
CONTRACT_CONSTANT = "CONTRACT_CONSTANT"
UNAVAILABLE = "UNAVAILABLE"

#: The ONE mapping from a published state to a gui_v2 severity token
#: (green / yellow / red / blue / gray — see components/_ui.html).
#: PENDING_BACKEND, UNKNOWN and UNAVAILABLE are never green: absence of a
#: producer, an unmeasurable value and a producer that could not answer are
#: none of them evidence of health.
STATE_SEVERITY: dict[str, str] = {
    "LIVE": "green",
    DERIVED: "blue",
    CONTRACT_CONSTANT: "blue",
    "STALE": "yellow",
    "UNAVAILABLE": "yellow",
    "PENDING_BACKEND": "gray",
    "UNKNOWN": "gray",
}

#: Outcome-ledger final statuses (interface doc "Status/enum semantics").
_RUN_STATUS_SEVERITY: dict[str, str] = {
    "VERIFIED": "green",
    "VERIFYING": "blue",
    "REPAIR_REQUIRED": "yellow",
    "ABSTAINED": "yellow",
    "INTERRUPTED": "yellow",
    "ESCALATION_REQUIRED": "red",
    "FAILED_VALIDATION": "red",
}
_SESSION_STATE_SEVERITY: dict[str, str] = {
    "BLOCKED": "red",
    "RUNNING": "blue",
    "COMPLETE": "green",
    "NO_SUCH_SESSION": "gray",
}
_READINESS_SEVERITY: dict[str, str] = {
    "READY": "green",
    "MOSTLY_LIVE": "blue",
    "PARTIAL": "yellow",
    "UNAVAILABLE": "red",
}
_CONSISTENCY_SEVERITY: dict[str, str] = {"AGREES": "green", "MISMATCH": "red"}

#: Keys whose string value is itself a published evidence/truth state.
_EVIDENCE_KEYS = frozenset({
    "availability", "record_evidence", "records_evidence", "truth_state",
    "derivation_state", "state"})


def state_severity(state: Any) -> str:
    """Severity token for a published state; anything unrecognised is gray."""
    return STATE_SEVERITY.get(str(state), "gray")


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

def _is_pending(value: Any) -> bool:
    return isinstance(value, str) and value == PENDING_BACKEND


def _state_of(value: Any, evidence: str) -> str:
    """The state of ONE field: the sentinel value wins over section evidence."""
    if _is_pending(value):
        return PENDING_BACKEND
    if isinstance(value, str) and value == UNAVAILABLE:
        return UNAVAILABLE
    return evidence


def _field(label: str, value: Any, state: str, note: str = "",
           key: str = "") -> dict[str, Any]:
    if isinstance(value, bool):
        display = "yes" if value else "no"
    elif value is None:
        display = "—"
    elif isinstance(value, (list, tuple)):
        display = ", ".join(str(v) for v in value) if value else "(none)"
    elif isinstance(value, dict):
        display = ", ".join(f"{k}={v}" for k, v in value.items()) if value else "(none)"
    else:
        display = str(value)
    return {"key": key or label, "label": label, "value": display, "raw": value,
            "state": state, "severity": state_severity(state), "note": note}


def _section_evidence(section: dict[str, Any], *keys: str, default: str) -> str:
    """The section-level evidence marker the read model published, if any."""
    for k in keys:
        v = section.get(k)
        if isinstance(v, str) and v in READ_MODEL_STATES:
            return v
    return default


def _get(dashboard: dict[str, Any], key: str) -> dict[str, Any]:
    v = dashboard.get(key)
    return v if isinstance(v, dict) else {}


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def collect_mission_control_view(repo_root, now: str | None = None) -> dict[str, Any]:
    """The single read-model call, then the pure projection.

    ``build_dashboard`` is contractually total (no corrupt record may escape
    it as an uncaught exception). If it nevertheless raises, the page must
    fail closed to an honest UNAVAILABLE banner, never to a stack trace and
    never to a page that looks populated. Only the exception TYPE is shown:
    a message could echo evidence the read model deliberately refused to.
    """
    try:
        dashboard = build_dashboard(repo_root, now)
    except Exception as exc:  # noqa: BLE001 - fail closed, visibly
        return _read_model_failure_view(type(exc).__name__, now)
    return project_mission_control(dashboard, now)


def _read_model_failure_view(exc_type: str, now: str | None) -> dict[str, Any]:
    detail = f"build_dashboard raised {exc_type}; nothing below is populated"
    return {
        "persona": PERSONA, "observe_only": True, "route": ROUTE,
        "now": now, "read_model_status": UNAVAILABLE,
        "read_model_detail": detail,
        "read_model_schema": {"schema_version": None, "schema_kind": None},
        "hero": [_field("Read model", UNAVAILABLE, UNAVAILABLE, detail)],
        "mission": {"fields": [], "session": None, "state": UNAVAILABLE},
        "authority": {"fields": [], "grants": [], "forbidden_ops": [],
                      "capabilities": [], "state": UNAVAILABLE, "detail": detail},
        "run_session": {"runs": [], "fields": [], "state": UNAVAILABLE},
        "workers": [], "system_health": {"fields": [], "readability": []},
        "readiness": {"fields": [], "capabilities": [], "reasons": [],
                      "state": UNAVAILABLE},
        "failures": [{"kind": "read_model", "ref": "build_dashboard",
                      "state": UNAVAILABLE, "severity": "red", "detail": detail}],
        "attention": {"fields": [], "entries": [], "state": UNAVAILABLE,
                      "zero_items_is_authoritative": False, "detail": detail},
        "unavailable": [{"section": "dashboard", "field": "build_dashboard",
                         "state": UNAVAILABLE, "severity": "yellow"}],
        "provenance": [],
        "learning": {"state": UNAVAILABLE, "detail": detail},
    }


def project_mission_control(dashboard: dict[str, Any],
                            now: str | None = None) -> dict[str, Any]:
    """Pure: the same dashboard dict always yields the same view dict."""
    controller = _get(dashboard, "controller")
    supervisor = _get(dashboard, "supervisor")
    worker = _get(dashboard, "worker")
    authority = _get(dashboard, "worker_authority")
    mission = _get(dashboard, "mission")
    apprenticeship = _get(dashboard, "apprenticeship")
    attention = _get(dashboard, "attention")
    health = _get(dashboard, "system_health")
    run_history = _get(dashboard, "run_history")
    learning = _get(dashboard, "learning")
    records = _get(dashboard, "controller_records")
    truth = _get(dashboard, "backend_truth")
    session_raw = dashboard.get("active_session")

    view: dict[str, Any] = {
        "persona": PERSONA, "observe_only": True, "route": ROUTE, "now": now,
        "read_model_status": "LIVE",
        "read_model_detail": "",
        "read_model_schema": {
            "schema_version": dashboard.get("schema_version"),
            "schema_kind": dashboard.get("schema_kind"),
            "read_model": dashboard.get("read_model")},
    }
    view["mission"] = _mission_section(controller, mission, session_raw)
    view["authority"] = _authority_section(authority, worker, apprenticeship)
    view["run_session"] = _run_section(run_history, supervisor)
    view["workers"] = _worker_cards(worker, supervisor, controller)
    view["system_health"] = _health_section(health)
    view["readiness"] = _readiness_section(truth, records)
    view["attention"] = _attention_section(attention, dashboard.get("attention_items"))
    view["learning"] = {
        "state": _section_evidence(learning, "truth_state", default=UNAVAILABLE),
        "severity": state_severity(_section_evidence(learning, "truth_state",
                                                     default=UNAVAILABLE)),
        "freshness": learning.get("freshness"),
        "detail": learning.get("detail", "")}
    view["failures"] = _failures(view, truth, authority)
    view["unavailable"] = _unavailable_inventory(dashboard)
    view["provenance"] = _provenance(dashboard)
    view["hero"] = _hero(view, controller, authority, truth, attention)
    return view


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _mission_section(controller: dict[str, Any], mission: dict[str, Any],
                     session_raw: Any) -> dict[str, Any]:
    fields = []
    runtime_mission = controller.get("current_mission")
    fields.append(_field("Runtime mission", runtime_mission,
                         _state_of(runtime_mission, "LIVE" if runtime_mission else UNAVAILABLE),
                         "from the runtime policy via the controller summary",
                         key="runtime_mission"))
    note = str(mission.get("completion_note") or "")
    progress_state = PENDING_BACKEND if note.startswith(PENDING_BACKEND) else (
        "LIVE" if mission else UNAVAILABLE)
    fields.append(_field("Deliverables verified",
                         f"{mission.get('verified_count', '—')} / {mission.get('total_required', '—')}",
                         progress_state, note, key="deliverables"))
    fields.append(_field("Mission complete", mission.get("is_complete"),
                         progress_state, "", key="is_complete"))
    fields.append(_field("Controller operational state",
                         controller.get("operational_state"),
                         _state_of(controller.get("operational_state"), "LIVE"),
                         key="operational_state"))
    return {"fields": fields, "session": _session_block(session_raw)}


def _session_block(session_raw: Any) -> dict[str, Any]:
    """The active-session projection, presented under ITS OWN truth flags.

    ``safe_to_present_as_current_work`` and ``mission_consistency`` are
    published booleans/enums; the page obeys them rather than re-deriving
    whether a recorded session is the current work.
    """
    if _is_pending(session_raw):
        return {"present": False, "state": PENDING_BACKEND,
                "severity": state_severity(PENDING_BACKEND),
                "detail": "session producer absent", "fields": []}
    if not isinstance(session_raw, dict):
        return {"present": False, "state": UNAVAILABLE,
                "severity": state_severity(UNAVAILABLE),
                "detail": "session projection unavailable", "fields": []}
    s = session_raw
    truth_state = _section_evidence(s, "truth_state", default="UNKNOWN")
    present = bool(s.get("session_present"))
    if not present:
        return {"present": False, "state": truth_state,
                "severity": state_severity(truth_state),
                "detail": "no session recorded (a producer answer, not a gap)",
                "fields": [], "session_state": s.get("session_state")}
    consistency = str(s.get("mission_consistency") or "UNKNOWN")
    session_state = str(s.get("session_state") or "UNKNOWN")
    fields = [
        _field("Session", s.get("session_id"), truth_state, key="session_id"),
        _field("Session mission", s.get("mission_id"), truth_state, key="mission_id"),
        _field("Objective", s.get("session_objective"), truth_state, key="objective"),
        _field("Stage", s.get("current_stage"), truth_state, key="stage"),
        _field("Current task", s.get("current_task_id"), truth_state, key="current_task"),
        _field("Started", s.get("session_started_at"), truth_state,
               str(s.get("freshness_evidence") or ""), key="started"),
        _field("Tasks attempted / verified / repaired / escalated",
               f"{s.get('tasks_attempted', '—')} / {s.get('tasks_verified', '—')} / "
               f"{s.get('tasks_repaired', '—')} / {s.get('tasks_escalated', '—')}",
               truth_state, key="task_counters"),
        _field("Worker heartbeat", s.get("worker_heartbeat"),
               _state_of(s.get("worker_heartbeat"), truth_state), key="worker_heartbeat"),
        _field("Supervisor latency (ms)", s.get("supervisor_latency_ms"),
               _state_of(s.get("supervisor_latency_ms"), truth_state),
               key="supervisor_latency_ms"),
    ]
    return {
        "present": True, "state": truth_state, "severity": state_severity(truth_state),
        "session_state": session_state,
        "session_state_severity": _SESSION_STATE_SEVERITY.get(session_state, "gray"),
        "mission_consistency": consistency,
        "consistency_severity": _CONSISTENCY_SEVERITY.get(consistency, "gray"),
        "consistency_detail": str(s.get("consistency_detail") or ""),
        "safe_to_present_as_current_work": bool(s.get("safe_to_present_as_current_work")),
        "blockers": [str(b) for b in (s.get("blockers") or [])]
        if isinstance(s.get("blockers"), list) else [],
        "fields": fields, "detail": "",
    }


def _authority_section(authority: dict[str, Any], worker: dict[str, Any],
                       apprenticeship: dict[str, Any]) -> dict[str, Any]:
    """Presentation ONLY. There is no control here and none in the template."""
    evidence = _section_evidence(authority, "record_evidence", default=UNAVAILABLE)
    fields = [
        _field("Authority level", authority.get("level"), evidence,
               key="level"),
        _field("Record evidence", evidence, evidence,
               str(authority.get("record_detail") or ""), key="record_evidence"),
        _field("Controller ladder level", worker.get("controller_level"),
               CONTRACT_CONSTANT, "fixed by contract, not observed",
               key="controller_level"),
        _field("C1 readiness", apprenticeship.get("c1_readiness"),
               _section_evidence(apprenticeship, "records_evidence", default=UNAVAILABLE),
               key="c1_readiness"),
    ]
    capabilities = [
        _field(name, authority.get(name), DERIVED,
               str(authority.get("capabilities_derived_from") or ""), key=name)
        for name in ("can_mutate_main", "can_merge", "can_deploy",
                     "can_write_production", "can_self_promote")
        if name in authority]
    grants = authority.get("grants")
    forbidden = authority.get("forbidden_ops")
    return {
        "state": evidence, "severity": state_severity(evidence),
        "detail": str(authority.get("record_detail") or ""),
        "fields": fields, "capabilities": capabilities,
        "grants": [str(g) for g in grants] if isinstance(grants, list) else [],
        "forbidden_ops": [str(f) for f in forbidden] if isinstance(forbidden, list) else [],
    }


def _run_section(run_history: dict[str, Any], supervisor: dict[str, Any]) -> dict[str, Any]:
    availability = _section_evidence(run_history, "availability", default=UNAVAILABLE)
    runs_raw = run_history.get("runs") if isinstance(run_history.get("runs"), list) else []
    runs = []
    for r in runs_raw:
        if not isinstance(r, dict):
            continue
        status = str(r.get("final_status") or "UNKNOWN")
        classes = r.get("failure_classes") if isinstance(r.get("failure_classes"), list) else []
        runs.append({
            "ledger_index": r.get("ledger_index"),
            "task_id": r.get("task_id"), "title": r.get("title"),
            "risk_class": r.get("risk_class"), "executor": r.get("executor"),
            "final_status": status,
            "severity": _RUN_STATUS_SEVERITY.get(status, "gray"),
            "recorded_at": r.get("recorded_at"),
            "supervisor_verdict": r.get("supervisor_verdict"),
            "failure_classes": [str(c) for c in classes],
            "escalated": bool(r.get("escalated")),
            "human_intervention": bool(r.get("human_intervention")),
            "mission_id": r.get("mission_id"),
            "candidate_sha": r.get("candidate_sha"),
        })
    # Presentation order only: newest ledger entry first. ledger_index stays visible.
    runs.reverse()
    sup_evidence = _section_evidence(supervisor, "records_evidence", default=UNAVAILABLE)
    fields = [
        _field("Run history", availability, availability,
               str(run_history.get("detail") or ""), key="availability"),
        _field("Records", run_history.get("record_count"), availability, key="record_count"),
        _field("Verdict counts", run_history.get("verdict_counts"), availability,
               key="verdict_counts"),
        _field("Supervisor verdicts (recent) pass / repair / escalate / abstain / unavailable",
               f"{supervisor.get('recent_pass', '—')} / {supervisor.get('recent_repair', '—')} / "
               f"{supervisor.get('recent_escalate', '—')} / {supervisor.get('recent_abstain', '—')} / "
               f"{supervisor.get('recent_unavailable', '—')}",
               sup_evidence, "conditional on the controller-records ledger",
               key="supervisor_recent"),
        _field("Last successful verification",
               supervisor.get("last_successful_verification"), sup_evidence,
               key="last_successful_verification"),
    ]
    return {"state": availability, "severity": state_severity(availability),
            "fields": fields, "runs": runs,
            "source": run_history.get("source"), "ordering": run_history.get("ordering"),
            "provenance_note": run_history.get("provenance_note")}


def _worker_cards(worker: dict[str, Any], supervisor: dict[str, Any],
                  controller: dict[str, Any]) -> list[dict[str, Any]]:
    """One card per represented component. Every value keeps its own state."""
    w_ev = _section_evidence(worker, "records_evidence", default=UNAVAILABLE)
    s_ev = _section_evidence(supervisor, "records_evidence", default=UNAVAILABLE)
    cards = []
    cards.append({
        "title": "Engineer worker", "identity": worker.get("worker_identity"),
        "identity_basis": worker.get("identity_basis"),
        "fields": [
            _field("Role", worker.get("role"), CONTRACT_CONSTANT, key="role"),
            _field("Authority", worker.get("ew_authority"), "LIVE", key="ew_authority"),
            _field("Operational state", worker.get("operational_state"),
                   _state_of(worker.get("operational_state"), w_ev), key="operational_state"),
            _field("Current task", worker.get("current_task"),
                   _state_of(worker.get("current_task"), w_ev), key="current_task"),
            _field("Queue size", worker.get("queue_size"),
                   _state_of(worker.get("queue_size"), w_ev), key="queue_size"),
            _field("Activity", worker.get("activity_summary"),
                   _state_of(worker.get("activity_summary"), w_ev), key="activity_summary"),
            _field("Next action", worker.get("next_action"),
                   _state_of(worker.get("next_action"), w_ev), key="next_action"),
            _field("Recent verification outcomes",
                   worker.get("recent_verification_outcomes"), w_ev,
                   "conditional on the controller-records ledger",
                   key="recent_verification_outcomes"),
            _field("Escalation state", worker.get("escalation_state"),
                   _state_of(worker.get("escalation_state"), w_ev), key="escalation_state"),
        ]})
    cards.append({
        "title": "GPT supervisor", "identity": supervisor.get("read_model"),
        "identity_basis": None,
        "fields": [
            _field("Availability", supervisor.get("availability"),
                   _state_of(supervisor.get("availability"), s_ev), key="availability"),
            _field("Current state", supervisor.get("current_state"),
                   _state_of(supervisor.get("current_state"), s_ev), key="current_state"),
            _field("Measured latency (ms)", supervisor.get("measured_latency_ms"),
                   _state_of(supervisor.get("measured_latency_ms"), s_ev),
                   key="measured_latency_ms"),
            _field("Verification queue", supervisor.get("verification_queue"),
                   _state_of(supervisor.get("verification_queue"), s_ev),
                   key="verification_queue"),
            _field("Outage state", supervisor.get("outage_state"),
                   _state_of(supervisor.get("outage_state"), s_ev), key="outage_state"),
            _field("Records evidence", s_ev, s_ev, str(supervisor.get("source") or ""),
                   key="records_evidence"),
        ]})
    cards.append({
        "title": "Controller", "identity": controller.get("controller_identity"),
        "identity_basis": controller.get("identity_basis"),
        "fields": [
            _field("Role", controller.get("controller_role"), CONTRACT_CONSTANT, key="role"),
            _field("Level", controller.get("controller_level"), CONTRACT_CONSTANT, key="level"),
            _field("Identity basis", controller.get("identity_basis"),
                   PENDING_BACKEND if controller.get("identity_basis") == "ASSUMED_NOT_OBSERVED"
                   else "LIVE",
                   "identity is assumed by contract until a controller identity producer exists"
                   if controller.get("identity_basis") == "ASSUMED_NOT_OBSERVED" else "",
                   key="identity_basis"),
            _field("Operational state", controller.get("operational_state"),
                   _state_of(controller.get("operational_state"), "LIVE"),
                   key="operational_state"),
            _field("Controller since", controller.get("controller_since"),
                   _state_of(controller.get("controller_since"), "LIVE"),
                   key="controller_since"),
            _field("Escalation role", controller.get("escalation_role"),
                   CONTRACT_CONSTANT, key="escalation_role"),
        ]})
    return cards


def _health_section(health: dict[str, Any]) -> dict[str, Any]:
    components = ("controller", "gpt_supervisor", "engineer_runtime", "sandbox",
                  "evidence_bridge", "control_loop")
    fields = [_field(name.replace("_", " "), health.get(name),
                     _state_of(health.get(name), "LIVE" if name in health else UNAVAILABLE),
                     key=name)
              for name in components]
    readability_raw = health.get("config_readability")
    readability = []
    if isinstance(readability_raw, dict):
        for name, value in readability_raw.items():
            readability.append(_field(str(name), value,
                                      "LIVE" if value == "READABLE" else UNAVAILABLE,
                                      "file readability only — never liveness",
                                      key=str(name)))
    return {"fields": fields, "readability": readability,
            "note": str(health.get("health_note") or ""),
            "authority": health.get("authority")}


def _readiness_section(truth: dict[str, Any], records: dict[str, Any]) -> dict[str, Any]:
    readiness = str(truth.get("readiness") or UNAVAILABLE)
    caps_raw = truth.get("capabilities") if isinstance(truth.get("capabilities"), list) else []
    capabilities = []
    for c in caps_raw:
        if not isinstance(c, dict):
            continue
        state = str(c.get("state") or "UNKNOWN")
        capabilities.append({
            "capability": c.get("capability"), "state": state,
            "severity": state_severity(state), "required": bool(c.get("required")),
            "detail": str(c.get("detail") or "")})
    rec_av = _section_evidence(records, "availability", default=UNAVAILABLE)
    counts = truth.get("state_counts") if isinstance(truth.get("state_counts"), dict) else {}
    fields = [
        _field("Readiness", readiness, readiness if readiness in READ_MODEL_STATES else "LIVE",
               "capability-based, not a percentage", key="readiness"),
        _field("Controller records ledger", rec_av, rec_av,
               str(records.get("detail") or ""), key="controller_records"),
    ]
    reasons = truth.get("reasons") if isinstance(truth.get("reasons"), list) else []
    return {"readiness": readiness,
            "readiness_severity": _READINESS_SEVERITY.get(readiness, "gray"),
            "state_counts": [{"state": str(k), "count": v, "severity": state_severity(k)}
                             for k, v in counts.items()],
            "fields": fields, "capabilities": capabilities,
            "reasons": [str(r) for r in reasons],
            "schema_version": truth.get("schema_version"),
            "records_source": records.get("source")}


def _attention_section(attention: dict[str, Any], legacy_items: Any) -> dict[str, Any]:
    derivation = _section_evidence(attention, "derivation_state", default=UNAVAILABLE)
    items_raw = attention.get("items") if isinstance(attention.get("items"), list) else (
        legacy_items if isinstance(legacy_items, list) else [])
    items = [i for i in items_raw if isinstance(i, dict)]
    authoritative = bool(attention.get("zero_items_is_authoritative"))
    fields = [
        _field("Attention derivation", derivation, derivation,
               str(attention.get("detail") or ""), key="derivation_state"),
        _field("Items", attention.get("item_count", len(items)), derivation, key="item_count"),
        _field("Empty list is authoritative", authoritative, derivation,
               key="zero_items_is_authoritative"),
    ]
    return {"state": derivation, "severity": state_severity(derivation),
            "fields": fields, "entries": items,
            "zero_items_is_authoritative": authoritative,
            "detail": str(attention.get("detail") or "")}


def _failures(view: dict[str, Any], truth: dict[str, Any],
              authority: dict[str, Any]) -> list[dict[str, Any]]:
    """ORGANISATION of failure/blocker facts the read model already returned.

    Not an attention engine: nothing here is inferred, weighed or scored, and
    an empty result is not a claim that nothing is wrong (see the attention
    section's ``zero_items_is_authoritative``).
    """
    out: list[dict[str, Any]] = []
    for run in view["run_session"]["runs"]:
        if run["final_status"] != "VERIFIED":
            out.append({"kind": "run", "ref": str(run["task_id"]),
                        "state": run["final_status"], "severity": run["severity"],
                        "detail": ", ".join(run["failure_classes"]) or
                        f"supervisor verdict: {run['supervisor_verdict']}"})
    session = view["mission"]["session"]
    if session.get("present"):
        if session.get("session_state") == "BLOCKED":
            out.append({"kind": "session", "ref": "session_state", "state": "BLOCKED",
                        "severity": "red", "detail": "the recorded session is BLOCKED"})
        for b in session.get("blockers", []):
            out.append({"kind": "session_blocker", "ref": "blockers", "state": "BLOCKED",
                        "severity": "red", "detail": b})
        if session.get("mission_consistency") == "MISMATCH":
            out.append({"kind": "session", "ref": "mission_consistency",
                        "state": "MISMATCH", "severity": "red",
                        "detail": session.get("consistency_detail", "")})
    for reason in view["readiness"]["reasons"]:
        out.append({"kind": "readiness", "ref": str(truth.get("readiness")),
                    "state": "REASON", "severity": view["readiness"]["readiness_severity"],
                    "detail": reason})
    for cap in view["readiness"]["capabilities"]:
        if cap["state"] in ("UNAVAILABLE", "STALE"):
            out.append({"kind": "capability", "ref": str(cap["capability"]),
                        "state": cap["state"], "severity": cap["severity"],
                        "detail": cap["detail"]})
    if view["authority"]["state"] == UNAVAILABLE:
        out.append({"kind": "authority", "ref": "worker_authority",
                    "state": UNAVAILABLE, "severity": "yellow",
                    "detail": str(authority.get("record_detail") or "")})
    return out


def _unavailable_inventory(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    """Every field the read model marked PENDING_BACKEND / UNAVAILABLE / UNKNOWN,
    found mechanically by walking the published dict. Sorted, so deterministic."""
    found: list[tuple[str, str, str]] = []

    def walk(section: str, obj: Any, prefix: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                path = f"{prefix}{k}"
                if _is_pending(v):
                    found.append((section, path, PENDING_BACKEND))
                elif isinstance(v, str) and k in _EVIDENCE_KEYS and v in ("UNAVAILABLE", "UNKNOWN", "STALE"):
                    found.append((section, path, v))
                elif isinstance(v, (dict, list)):
                    walk(section, v, path + ".")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                if isinstance(v, (dict, list)):
                    walk(section, v, f"{prefix}{i}.")

    for section, body in dashboard.items():
        if _is_pending(body):
            found.append((str(section), str(section), PENDING_BACKEND))
        elif isinstance(body, (dict, list)):
            walk(str(section), body)
    seen = sorted(set(found))
    return [{"section": s, "field": f, "state": st, "severity": state_severity(st)}
            for s, f, st in seen]


def _provenance(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for section, body in dashboard.items():
        if not isinstance(body, dict):
            rows.append({"section": str(section), "read_model": None,
                         "schema_version": None, "source": None,
                         "evidence": str(body) if _is_pending(body) else None,
                         "note": None})
            continue
        evidence = None
        for k in ("record_evidence", "records_evidence", "availability",
                  "truth_state", "derivation_state", "readiness"):
            if isinstance(body.get(k), str):
                evidence = f"{k}={body[k]}"
                break
        note = body.get("provenance_note") or body.get("freshness_evidence") or \
            body.get("health_note") or body.get("detail")
        rows.append({"section": str(section), "read_model": body.get("read_model"),
                     "schema_version": body.get("schema_version"),
                     "source": body.get("source"), "evidence": evidence,
                     "note": str(note) if note else None})
    return rows


def _hero(view: dict[str, Any], controller: dict[str, Any], authority: dict[str, Any],
          truth: dict[str, Any], attention: dict[str, Any]) -> list[dict[str, Any]]:
    mission = controller.get("current_mission")
    auth_state = view["authority"]["state"]
    readiness = view["readiness"]["readiness"]
    att_state = view["attention"]["state"]
    return [
        _field("Mission", mission, _state_of(mission, "LIVE" if mission else UNAVAILABLE),
               key="mission"),
        _field("Authority", authority.get("level"), auth_state,
               f"record evidence {auth_state}", key="authority"),
        {**_field("Readiness", readiness, readiness if readiness in READ_MODEL_STATES else "LIVE",
                  key="readiness"),
         "severity": view["readiness"]["readiness_severity"]},
        _field("Attention",
               f"{view['attention']['fields'][1]['value']} item(s)"
               if view["attention"]["zero_items_is_authoritative"] else att_state,
               att_state, "" if view["attention"]["zero_items_is_authoritative"]
               else "an empty list is NOT evidence that nothing requires you",
               key="attention"),
    ]
