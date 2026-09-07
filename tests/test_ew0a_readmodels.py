"""Worker Control Center read-model projection tests.

Proves the projections are correct, honest (no fabricated/smoothed state;
PENDING_BACKEND where no backend), carry no secrets, and are READ-ONLY by
construction (the module references no mutation function).
"""
from __future__ import annotations

import inspect
import json

import pytest

from portfolio_automation.engineer_worker import ew0a_readmodels as rm
from portfolio_automation.engineer_worker.ew0a_readmodels import (
    PENDING_BACKEND, project_verification, build_apprenticeship_summary,
    build_mission_summary, build_supervisor_summary, build_worker_authority_summary)
from portfolio_automation.engineer_worker.ew0a_authority import EngineerAuthorityLevel as Lvl


# --- read-only safety (structural) ------------------------------------------
def test_module_imports_no_mutation_function():
    # the module must not import/expose any authoritative mutator
    for forbidden in ("set_authority_level", "write_runtime_policy", "certify_attempt",
                      "run_mission", "run_task", "assert_operation_allowed", "admit_engineer_task"):
        assert not hasattr(rm, forbidden), f"read-model must not import mutator {forbidden}"


def test_no_summary_class_has_a_mutate_method():
    # only CALLABLE members (methods) of classes DEFINED here — boolean capability
    # FIELDS like can_mutate_main/can_write_production are read-only data, not methods.
    for name in dir(rm):
        obj = getattr(rm, name)
        if isinstance(obj, type) and getattr(obj, "__module__", "") == rm.__name__:
            methods = {a for a in vars(obj) if not a.startswith("__") and callable(vars(obj)[a])}
            assert not any(any(w in a.lower() for w in ("set_", "write", "mutate", "dispatch",
                                                        "certify", "promote")) for a in methods)


# --- verification ladder: worker_complete != VERIFIED -----------------------
def test_worker_complete_plus_gpt_repair_is_not_verified():
    v = project_verification("t", "COMPLETE", "PASS", "PASS", "NOT_EVALUATED", "REPAIR")
    assert v.final_status == "NOT_VERIFIED" and v.worker_complete_is_not_verified

def test_deterministic_pass_plus_gpt_pass_is_verified():
    v = project_verification("t", "COMPLETE", "PASS", "PASS", "PASS", "PASS")
    assert v.final_status == "VERIFIED"

def test_deterministic_fail_short_circuits_gpt_not_consulted():
    v = project_verification("t", "COMPLETE", "FAIL", "FAIL", "NOT_EVALUATED", "PASS")
    assert v.gpt_verdict == "NOT_CONSULTED" and v.final_status == "NOT_VERIFIED"


# --- mission progress from VERIFIED deliverables, not task count -------------
def test_mission_progress_from_verified_deliverables():
    m = build_mission_summary("northstar_0b_decision_outcome_passport_contracts",
                              present={"ExperimentSpec", "ExperimentResult"})
    assert m.deliverables["ExperimentSpec"] == "VERIFIED"
    assert m.deliverables["CapitalProposal"] == "NOT_STARTED"
    assert m.verified_count == 2 and m.total_required == 6 and not m.is_complete


# --- authority projection: disabled authorities visible false ---------------
def test_worker_authority_disabled_flags():
    a = build_worker_authority_summary(Lvl.A1_ASSISTED_ENGINEERING, grants=["approved E1/E2"])
    assert not (a.can_mutate_main or a.can_merge or a.can_deploy or a.can_write_production or a.can_self_promote)
    assert "MAIN_WRITE" in a.forbidden_ops and a.level == "A1_ASSISTED_ENGINEERING"


# --- apprenticeship honesty (never smooths negative evidence) ---------------
def test_apprenticeship_reports_negative_evidence_honestly():
    records = [
        {"kind": "ControllerDecisionCandidateV0"},
        {"kind": "ApprenticeshipComparison", "engineer_proposed_task_relates_to_experimentspec": True,
         "risk_agreement": False, "routing_agreement": False,
         "danger_underclassified_architecture_as_engineer": True},
    ]
    a = build_apprenticeship_summary(records)
    assert a.decisions_shadowed == 1
    assert a.risk_agreements == 0 and a.routing_agreements == 0
    assert a.unsafe_underclassifications == 1 and a.missed_escalations == 1
    assert a.c1_readiness == "NOT_READY"


# --- supervisor projection: counts, no secrets ------------------------------
def test_supervisor_summary_counts_and_no_secrets():
    records = [{"gpt_verdict": "PASS", "recorded_at": "t1"}, {"gpt_verdict": "PASS", "recorded_at": "t2"},
               {"gpt_verdict": "REPAIR", "recorded_at": "t3"}]
    s = build_supervisor_summary(records)
    assert s.recent_pass == 2 and s.recent_repair == 1 and s.last_successful_verification == "t2"
    assert s.measured_latency_ms == PENDING_BACKEND and s.verification_queue == PENDING_BACKEND
    blob = json.dumps(s.to_dict())
    for leak in ("sk-", "Authorization", "Bearer", "api_key", ".ew0a_openai_key"):
        assert leak not in blob


# --- full dashboard integration (real repo) ---------------------------------
def test_build_dashboard_integration():
    from pathlib import Path as _P
    repo = _P(__file__).resolve().parents[1]   # not one operator's checkout
    d = rm.build_dashboard(repo)
    for section in ("controller", "supervisor", "worker", "worker_authority",
                    "mission", "apprenticeship", "attention_items", "system_health"):
        assert section in d
    # dynamic controller identity (not a hardcoded permanent invariant)
    assert d["controller"]["controller_identity"] == "claude_code"
    assert d["worker"]["operational_state"] == PENDING_BACKEND         # no heartbeat backend
    # Mission progress is MISSION-SCOPED. The runtime mission is now 0C, whose
    # deliverable set is not projected, so the six 0B.3 contracts must NOT appear
    # here. Reporting a completed phase's deliverables as the current phase's
    # progress is exactly the drift senior review caught on PR #20.
    assert "ExperimentSpec" not in d["mission"]["deliverables"]
    assert d["mission"]["is_complete"] is False
    # no secret anywhere in the whole dashboard
    blob = json.dumps(d)
    for leak in ("sk-", "Bearer", ".ew0a_openai_key", "Authorization"):
        assert leak not in blob


# ══ SENIOR-REVIEW REPAIR: real GUI observability for an autonomous session ══
# Finding B of the PR #20 senior review: a standalone projection function is NOT
# GUI integration, and build_dashboard() was still reporting the six 0B.3
# contracts as mission progress while the runtime mission had moved to 0C.
import ast as _ast
import json as _json
from pathlib import Path as _Path

_REPO = _Path(__file__).resolve().parents[1]
_C0_MISSION = "northstar_0c_pit_evidence_gateway_research_store"
_0B3_MISSION = "northstar_0b_decision_outcome_passport_contracts"
_0B3_NAMES = {"ExperimentSpec", "ExperimentResult", "CapitalProposal",
              "ExitProposal", "OutcomeRecord", "StrategyPassport"}


def test_0b3_deliverables_are_not_reported_as_0c_mission_progress():
    """The drift senior review caught: a completed phase's deliverables must not
    be shown as the current phase's progress. Confidently wrong is worse than
    admitting the deliverable set is unknown."""
    from portfolio_automation.engineer_worker.ew0a_readmodels import build_mission_summary
    summary = build_mission_summary(_C0_MISSION, present=set(_0B3_NAMES))
    assert set(summary.deliverables) & _0B3_NAMES == set()
    assert summary.is_complete is False
    assert summary.verified_count == 0
    assert "PENDING_BACKEND" in summary.completion_note


def test_the_0b3_mission_still_reports_its_own_deliverables():
    """The fix is scoped, not a blanket disabling of mission progress."""
    from portfolio_automation.engineer_worker.ew0a_readmodels import build_mission_summary
    summary = build_mission_summary(_0B3_MISSION, present=set(_0B3_NAMES))
    assert set(summary.deliverables) == _0B3_NAMES
    assert summary.is_complete is True


def test_active_session_is_visible_through_the_controller_owned_dashboard():
    """Observability is proven only when the ESTABLISHED read-model path carries
    it — which is what a standalone projection function did not do."""
    from portfolio_automation.engineer_worker.ew0a_readmodels import build_dashboard
    from tools.ns0c_session import SESSION2_ID
    dash = build_dashboard(_REPO, now="2026-08-16T05:00:00+00:00")
    session = dash["active_session"]
    assert session != "PENDING_BACKEND", "session ledger exists but is not projected"
    assert session["mission_id"] == _C0_MISSION
    # The ACTIVE session is the most recent bounded episode, not whichever
    # SessionStarted happens to be first in the file. Two bounded sessions share
    # this ledger, and the dashboard must show the current one.
    assert session["session_id"] == SESSION2_ID
    assert session["session_objective"] == "Revision / Supersession Safety Foundation"
    assert session["current_task_id"]


def test_session_projection_reports_verified_only_from_recorded_evidence():
    """A task counts VERIFIED only from a recorded TaskOutcome final_status, never
    from absence of error or from a task merely finishing.

    Counted per EPISODE. The ledger holds two bounded sessions, so summing every
    TaskOutcome in the file would credit each session with the other's work —
    the merge this projection was repaired to prevent."""
    from tools.ns0c_session import (SESSION1_ID, SESSION2_ID, load_episodes,
                                    session_projection)
    episodes = {e.session_id: e for e in load_episodes(repo_root=_REPO)}
    for session_id in (SESSION1_ID, SESSION2_ID):
        proj = session_projection(repo_root=_REPO, session_id=session_id)
        recorded = [o for o in episodes[session_id].of_kind("TaskOutcome")
                    if o.get("final_status") == "VERIFIED"]
        assert proj["tasks_verified"] == len(recorded)

    total_in_file = sum(1 for e in load_episodes(repo_root=_REPO)
                        for o in e.of_kind("TaskOutcome")
                        if o.get("final_status") == "VERIFIED")
    per_session = [session_projection(repo_root=_REPO, session_id=s)["tasks_verified"]
                   for s in (SESSION1_ID, SESSION2_ID)]
    assert sum(per_session) == total_in_file
    assert all(count < total_in_file for count in per_session), (
        "neither session may claim the whole file's verified work")


def test_missing_live_backends_stay_pending_backend():
    """A partial honest dashboard beats a fake live one."""
    from tools.ns0c_session import session_projection
    proj = session_projection(repo_root=_REPO)
    assert proj["worker_heartbeat"] == "PENDING_BACKEND"
    assert proj["supervisor_latency_ms"] == "PENDING_BACKEND"


def test_absent_session_ledger_is_answered_by_the_producer_not_faked(tmp_path):
    """Replaces a test that asserted the defect. It required an absent ledger to
    project PENDING_BACKEND -- but `tools/ns0c_session.py` exists, so that told
    an operator to build a backend that was already there. The producer's own
    no-session contract is the answer, and it is still never a fabrication."""
    from portfolio_automation.engineer_worker.ew0a_readmodels import _build_active_session
    payload, status, _detail = _build_active_session(tmp_path)
    assert status == "OK"
    assert payload["session_state"] == "NO_SUCH_SESSION"
    assert payload["session_id"] is None


def test_session_projection_surfaces_the_authority_boundaries():
    from tools.ns0c_session import session_projection
    proj = session_projection(repo_root=_REPO)
    assert proj["authority"] == "A1_ASSISTED_ENGINEERING"
    assert proj["c1_status"] == "DISABLED"
    assert proj["auto_merge"] is False
    assert proj["production_mutation"] is False
    assert proj["capital_action"] is False


def test_session_module_has_no_hardcoded_operator_checkout_path():
    """Read-model code must not depend on one machine's path."""
    src = (_REPO / "tools" / "ns0c_session.py").read_text(encoding="utf-8")
    assert "/home/pesan/" not in src


def test_readmodel_session_path_has_no_authoritative_write():
    """GUI -> authoritative mutation must not exist. The session projection and
    its reader import only read accessors."""
    src = (_REPO / "tools" / "ns0c_session.py").read_text(encoding="utf-8")
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name == "session_projection":
            body = _ast.dump(node)
            for writer in ("open(", "write", "record("):
                assert writer not in body, f"projection must not {writer}"


# ═══════════════════════════════════════════════════════════════════════════
# GUI-R — CONTROLLER READ-MODEL REPAIR
#
# Reconciliation found this module truthful about the backends nobody had built
# and quietly untruthful about four things it did emit. These tests pin the
# repairs, and several of them are written to fail if a future change makes the
# dashboard LOOK healthier without new evidence behind it.
# ═══════════════════════════════════════════════════════════════════════════
import dataclasses as _dc
import sys as _sys

from portfolio_automation.engineer_worker.control_center_truth import TruthState
from portfolio_automation.engineer_worker.ew0a_readmodels import (
    OUTCOME_LEDGER_REL, RECORDS_LEDGER_REL, build_attention_coverage,
    build_run_history, derive_authority_capabilities, effective_denied_ops,
    project_active_session)

_NOW = "2026-09-06T21:30:00+00:00"


#: A COMPLETE, schema-valid populated session, mirroring every field
#: tools.ns0c_session.session_projection actually publishes -- notably a
#: session_started_at and NO last-activity timestamp. The previous fixture
#: carried six keys and passed only because the projection copied the producer
#: dictionary wholesale; the fixture's own incompleteness was invisible.
_VALID_SESSION = {
    "read_model": "Northstar0CSessionSummary",
    "schema_kind": "experimental_noncanonical",
    "session_id": "s1",
    "recorded_session_id": "s1",
    "identity_corrected": False,
    "mission_id": "m-runtime",
    "session_objective": "Revision / Supersession Safety Foundation",
    "session_started_at": "2026-08-16T07:17:41+00:00",
    "starting_main_sha": "7cdc15a6ad9b085540718817611a3db68f29d302",
    "session_state": "RUNNING",
    "current_task_id": "t1",
    "current_task_title": None,
    "current_stage": "VERIFYING",
    "tasks_attempted": 2,
    "tasks_verified": 1,
    "tasks_repaired": 1,
    "tasks_escalated": 0,
    "tasks_abstained": 0,
    "tasks_incomplete": 0,
    "blockers": [],
    "known_sessions": ["s1"],
    "authority": "A1_ASSISTED_ENGINEERING",
    "c1_status": "DISABLED",
    "auto_merge": False,
    "production_mutation": False,
    "capital_action": False,
    "worker_heartbeat": "PENDING_BACKEND",
    "supervisor_latency_ms": "PENDING_BACKEND",
}


def _session(mission="m-runtime", session_id="s1", **extra):
    base = dict(_VALID_SESSION)
    base.update({"session_id": session_id, "mission_id": mission})
    base.update(extra)
    return base


# ── A. active session: truth state and mission consistency ──────────────────
def test_active_session_pending_backend_requires_an_absent_producer():
    """PENDING_BACKEND is now reachable only through producer ABSENCE -- not
    through an absent ledger, an empty result, or a failure."""
    projection, state = project_active_session(
        PENDING_BACKEND, "m-runtime", _NOW, producer_status="ABSENT")
    assert projection == PENDING_BACKEND
    assert state is TruthState.PENDING_BACKEND


def test_active_session_mission_agreement_is_reported_as_agreement():
    enriched, _ = project_active_session(_session(mission="m-runtime"), "m-runtime", _NOW)
    assert enriched["mission_consistency"] == "AGREES"
    assert enriched["runtime_mission_id"] == "m-runtime"


def test_active_session_mission_mismatch_is_reported_separately_from_freshness():
    """THE load-bearing one. A mission mismatch is a fact about identity, not
    about age. Reporting it as STALE would assert an age nobody measured and
    would hide the actual problem behind a plausible-looking one."""
    enriched, state = project_active_session(
        _session(mission="m-other"), "m-runtime", _NOW)
    assert enriched["mission_consistency"] == "MISMATCH"
    assert state is not TruthState.STALE
    assert enriched["truth_state"] != TruthState.STALE.value
    assert "m-other" in enriched["consistency_detail"]


def test_mission_mismatch_alone_never_implies_stale_for_any_reference_time():
    """Swept across reference times so the property cannot be satisfied by luck
    with one convenient `now`."""
    for now in (None, _NOW, "2020-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00",
                "not-a-timestamp"):
        _, state = project_active_session(_session(mission="m-other"), "m-runtime", now)
        assert state is not TruthState.STALE, f"mismatch classified STALE at now={now!r}"


def test_active_session_freshness_is_unknown_because_age_is_unmeasurable():
    """The session contract publishes a START time and no last-activity time, and
    no named session freshness threshold exists. Age is therefore unmeasurable,
    which the lattice answers as UNKNOWN -- never STALE."""
    _, state = project_active_session(_session(), "m-runtime", _NOW)
    assert state is TruthState.UNKNOWN


def test_active_session_without_injected_now_is_still_unknown_not_stale():
    _, state = project_active_session(_session(), "m-runtime", None)
    assert state is TruthState.UNKNOWN


def test_active_session_with_unusable_mission_is_undetermined_not_mismatch():
    for bad in (PENDING_BACKEND, "NO_SUCH_SESSION", "", None):
        enriched, _ = project_active_session(_session(mission=bad), "m-runtime", _NOW)
        assert enriched["mission_consistency"] == "UNDETERMINED", bad


def test_active_session_with_no_runtime_mission_is_undetermined():
    enriched, _ = project_active_session(_session(), None, _NOW)
    assert enriched["mission_consistency"] == "UNDETERMINED"


def test_session_is_never_presentable_as_current_work_on_a_mission_mismatch():
    enriched, _ = project_active_session(_session(mission="m-other"), "m-runtime", _NOW)
    assert enriched["safe_to_present_as_current_work"] is False


def test_safe_to_present_requires_both_live_and_agreement():
    """Even with mission AGREEMENT the session is not presentable as current
    work, because freshness is UNKNOWN. LIVE is unreachable until the session
    contract publishes a last-activity timestamp -- which this mission
    deliberately did not build."""
    enriched, state = project_active_session(_session(mission="m-runtime"), "m-runtime", _NOW)
    assert enriched["mission_consistency"] == "AGREES"
    assert state is TruthState.UNKNOWN
    assert enriched["safe_to_present_as_current_work"] is False


def test_active_session_projects_every_contracted_field_faithfully():
    """Replaces a test that asserted the defect. It required EVERY key of the
    producer dict to survive into the projection -- which is the wholesale copy
    that made the published schema equal to whatever the producer returned, and
    is how a TaskStage title object reached the dashboard. Contracted fields
    must still be projected faithfully; uncontracted ones must not appear."""
    original = _session(mission="m-other")
    projected, _state = project_active_session(original, "m-runtime", _NOW)
    for name in rm.SESSION_PROJECTED_SOURCE_FIELDS:
        assert projected[name] == original[name], name


def test_real_repo_session_mission_disagreement_is_visible_in_the_dashboard():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    session = dash["active_session"]
    assert session["mission_id"] != dash["mission"]["mission_id"]
    assert session["mission_consistency"] == "MISMATCH"
    assert session["truth_state"] == TruthState.UNKNOWN.value
    assert session["safe_to_present_as_current_work"] is False


# ── B. learning: a failing producer is not a missing one ────────────────────
def test_learning_projection_is_live_and_capability_classified():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    assert dash["learning"]["truth_state"] == TruthState.LIVE.value
    caps = {c["capability"]: c for c in dash["backend_truth"]["capabilities"]}
    assert caps["learning"]["state"] == TruthState.LIVE.value
    assert caps["learning"]["required"] is False


def test_learning_imposes_no_freshness_on_historical_evidence():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    assert dash["learning"]["freshness"] == "NOT_APPLICABLE_HISTORICAL_EVIDENCE"


def test_learning_producer_failure_is_unavailable_not_pending_backend(monkeypatch):
    """The producer exists. Calling its failure PENDING_BACKEND would tell an
    operator nobody had built learning, sending them to write code instead of to
    investigate an incident."""
    from portfolio_automation.engineer_worker.learning import readmodels as lrm

    def _boom(*_a, **_k):
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(lrm, "build_learning_dashboard", _boom)
    projection, state = rm._project_learning(_REPO, "engineer.x", _NOW)
    assert state is TruthState.UNAVAILABLE
    assert projection["truth_state"] == TruthState.UNAVAILABLE.value
    assert projection["truth_state"] != PENDING_BACKEND


def test_learning_missing_producer_is_pending_backend(monkeypatch):
    monkeypatch.setattr(rm, "_LEARNING_PRODUCER_MODULE",
                        "portfolio_automation.engineer_worker.learning.not_built")
    projection, state = rm._project_learning(_REPO, "engineer.x", _NOW)
    assert state is TruthState.PENDING_BACKEND
    assert projection == PENDING_BACKEND


# ── C. run history: canonical reader, provenance preserved ──────────────────
def test_run_history_uses_the_canonical_domain_reader():
    """Not a third hand-written JSONL interpretation of the same ledger."""
    from portfolio_automation.engineer_worker import ew0a
    assert rm.read_outcomes is ew0a.read_outcomes


def test_run_history_projects_the_real_ledger_with_stable_ordering():
    history = build_run_history(_REPO)
    assert history.availability == TruthState.LIVE.value
    assert history.record_count == len(history.runs) > 0
    assert history.ordering == "ledger_append_order"
    assert [r["ledger_index"] for r in history.runs] == list(range(len(history.runs)))
    # deterministic across calls
    assert build_run_history(_REPO).to_dict() == history.to_dict()


def test_run_history_preserves_original_identifiers():
    history = build_run_history(_REPO)
    raw = [_json.loads(l) for l in
           (_REPO / OUTCOME_LEDGER_REL).read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [r["task_id"] for r in history.runs] == [r["task_id"] for r in raw]


def test_run_history_never_stamps_the_runtime_mission_onto_historical_runs():
    """Fabricated provenance: the GUI renders the CURRENT mission in the per-run
    mission column, attributing month-old records to a mission that did not
    exist when they were written."""
    dash = rm.build_dashboard(_REPO, now=_NOW)
    runtime_mission = dash["mission"]["mission_id"]
    assert runtime_mission
    for run in dash["run_history"]["runs"]:
        assert run["mission_id"] != runtime_mission


def test_run_history_missing_mission_stays_none(tmp_path):
    ledger = tmp_path / OUTCOME_LEDGER_REL
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        _json.dumps(_row(task_id="T1")) + "\n"
        + _json.dumps(_row(task_id="T2", mission_id="m-recorded")) + "\n",
        encoding="utf-8")
    history = build_run_history(tmp_path)
    assert history.runs[0]["mission_id"] is None
    assert history.runs[1]["mission_id"] == "m-recorded"


def test_run_history_absent_ledger_is_unavailable_not_pending_backend(tmp_path):
    """The producer exists in this repository, so absence is operational."""
    history = build_run_history(tmp_path)
    assert history.availability == TruthState.UNAVAILABLE.value
    assert history.availability != PENDING_BACKEND
    assert history.runs == []


def test_run_history_malformed_ledger_follows_the_domain_readers_rule(tmp_path):
    """ew0a.read_outcomes raises on a malformed line. The projection does not
    soften that into a partial list and call it LIVE."""
    ledger = tmp_path / OUTCOME_LEDGER_REL
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(_json.dumps({"task_id": "T1"}) + "\n{ not json\n", encoding="utf-8")
    history = build_run_history(tmp_path)
    assert history.availability == TruthState.UNAVAILABLE.value
    assert history.record_count == 0
    assert "read_outcomes" in history.detail


def test_run_history_source_identity_is_visible():
    history = build_run_history(_REPO).to_dict()
    assert history["source"] == OUTCOME_LEDGER_REL
    assert history["source_kind"] == "engineering_outcome_ledger"
    assert history["verdict_field"] == "supervisor_verdict"


def test_the_two_supervisor_evidence_domains_are_distinguishable():
    """Two legitimate ledgers record supervisor verdicts under different field
    names for different purposes. A consumer must be able to say which it is
    showing; collapsing them yields a number belonging to neither."""
    dash = rm.build_dashboard(_REPO, now=_NOW)
    supervisor, history = dash["supervisor"], dash["run_history"]
    assert supervisor["source"] == RECORDS_LEDGER_REL
    assert history["source"] == OUTCOME_LEDGER_REL
    assert supervisor["source"] != history["source"]
    assert supervisor["verdict_field"] != history["verdict_field"]
    assert supervisor["evidence_domain"] != history["evidence_domain"]


# ── D. attention: [] is not an answer ───────────────────────────────────────
def test_empty_attention_list_is_not_authoritative_while_no_derivation_exists():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    assert dash["attention_items"] == []          # compatibility preserved
    coverage = dash["attention"]
    assert coverage["item_count"] == 0
    assert coverage["derivation_state"] == PENDING_BACKEND
    assert coverage["zero_items_is_authoritative"] is False


def test_attention_coverage_becomes_authoritative_only_with_a_derivation():
    assert build_attention_coverage(items=[], derivation_exists=True
                                    ).zero_items_is_authoritative is True
    assert build_attention_coverage(items=[], derivation_exists=False
                                    ).zero_items_is_authoritative is False


def test_a_denied_protected_op_drill_never_becomes_a_human_attention_item():
    """The outcome ledger's only policy_violation is certification mission M5, a
    protected-op attack whose GATE WAS THAT IT BE DENIED. The GUI presents it as
    an unresolved incident; the controller must not repeat that upstream."""
    dash = rm.build_dashboard(_REPO, now=_NOW)
    violations = [r for r in dash["run_history"]["runs"] if r["policy_violation"]]
    assert violations, "fixture assumption: the ledger records a denied protected-op drill"
    assert dash["attention_items"] == []
    assert dash["attention"]["item_count"] == 0


def test_ordinary_repair_and_test_failure_are_not_attention_items():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    statuses = {r["final_status"] for r in dash["run_history"]["runs"]}
    assert statuses & {"REPAIR_REQUIRED", "FAILED_VALIDATION", "INTERRUPTED"}, (
        "fixture assumption: the ledger records ordinary failures")
    assert dash["attention"]["item_count"] == 0


# ── E. health: liveness is never inferred from the code running ─────────────
_HEALTH_CLAIMS = {"ACTIVE", "READY", "HEALTHY", "OK", "UP", "RUNNING"}


def test_no_component_health_field_claims_liveness_without_a_producer():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    health = dash["system_health"]
    for component in ("controller", "gpt_supervisor", "engineer_runtime", "sandbox",
                      "evidence_bridge", "control_loop"):
        assert health[component] == PENDING_BACKEND, (
            f"{component} claims {health[component]!r} with no health producer")
        assert health[component] not in _HEALTH_CLAIMS


def test_config_readability_is_reported_but_never_as_component_health():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    health = dash["system_health"]
    assert health["config_readability"]["authority_record"] == "READABLE"
    assert health["config_readability"]["runtime_policy"] == "READABLE"
    # readable protected config coexisting with unknown component health is the
    # whole point: one says what is ALLOWED, the other what is RUNNING
    assert health["controller"] == PENDING_BACKEND
    assert "READABILITY" in health["health_note"].upper()


def test_absent_config_is_reported_absent_not_unhealthy(tmp_path):
    from portfolio_automation.engineer_worker.ew0a_readmodels import _readability
    assert _readability(tmp_path / "nope.json") == "ABSENT"


# ── F. authority booleans are derived, not defaults ─────────────────────────
def test_authority_capability_booleans_have_no_dataclass_defaults():
    """Structural. A default is indistinguishable from a derivation that
    returned the same value -- which is how five correct-looking `false`s
    survived without anything computing them."""
    fields = {f.name: f for f in _dc.fields(rm.WorkerAuthoritySummary)}
    for name in ("can_mutate_main", "can_merge", "can_deploy",
                 "can_write_production", "can_self_promote"):
        assert fields[name].default is _dc.MISSING, f"{name} still has a default"


def test_todays_a1_posture_still_denies_every_dangerous_capability():
    summary = build_worker_authority_summary(Lvl.A1_ASSISTED_ENGINEERING,
                                             grants=["approved E1/E2"])
    assert not (summary.can_mutate_main or summary.can_merge or summary.can_deploy
                or summary.can_write_production or summary.can_self_promote)


def test_capability_booleans_change_with_the_denial_set():
    """Proves the values are computed. Varying the input must vary the output."""
    none_denied = derive_authority_capabilities(frozenset())
    assert all(none_denied.values())
    merge_denied = derive_authority_capabilities({"MERGE"})
    assert merge_denied["can_merge"] is False
    assert merge_denied["can_deploy"] is True
    all_denied = derive_authority_capabilities(
        {"MERGE", "DEPLOY", "MAIN_WRITE", "PRODUCTION_WRITE", "SELF_PROMOTION"})
    assert not any(all_denied.values())


def test_an_authority_record_can_only_ever_be_stricter():
    """A record that omits an operation must not thereby grant it: the module's
    permanent boundary is unioned in, never substituted."""
    assert "MERGE" in effective_denied_ops([])
    assert "MERGE" in effective_denied_ops(["SOMETHING_ELSE"])
    stricter = effective_denied_ops(["CUSTOM_DENIED_OP"])
    assert "CUSTOM_DENIED_OP" in stricter and "MERGE" in stricter


def test_a_stricter_record_narrows_the_projected_capabilities():
    summary = build_worker_authority_summary(
        Lvl.A0_DIAGNOSTIC, grants=[], forbidden_ops=["CUSTOM_DENIED_OP"])
    assert "CUSTOM_DENIED_OP" in summary.forbidden_ops
    assert summary.can_merge is False


def test_authority_projection_never_touches_the_real_protected_file(tmp_path):
    """Fixtures only. Authority state is protected and trusted-controlled."""
    before = (_REPO / "config" / "ew0a_authority.json").read_bytes()
    build_worker_authority_summary(Lvl.A0_DIAGNOSTIC, grants=[],
                                   forbidden_ops=["X"])
    assert (_REPO / "config" / "ew0a_authority.json").read_bytes() == before


# ── G. hardcoded identity audit ─────────────────────────────────────────────
def test_controller_identity_is_labelled_an_assumption_not_an_observation():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    controller = dash["controller"]
    assert controller["identity_basis"] == "ASSUMED_NOT_OBSERVED"
    caps = {c["capability"]: c for c in dash["backend_truth"]["capabilities"]}
    assert caps["controller_identity"]["state"] == PENDING_BACKEND


def test_controller_operational_state_is_no_longer_asserted_active():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    assert dash["controller"]["operational_state"] == PENDING_BACKEND


def test_contract_constants_are_declared_rather_than_left_ambiguous():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    assert "controller_role" in dash["controller"]["contract_constants"]
    assert "worker_identity" in dash["worker"]["contract_constants"]


# ── H. structure: nothing is emitted without a truth state ──────────────────
def test_every_evidence_projection_has_a_covering_capability():
    """The structural fix. active_session and learning were appended AFTER the
    truth assessment, which is exactly how the projection that answers 'what is
    happening now' ended up as the only one nobody had classified."""
    dash = rm.build_dashboard(_REPO, now=_NOW)
    covered = {c["capability"] for c in dash["backend_truth"]["capabilities"]}
    for key, capability in (("active_session", "active_session"),
                            ("learning", "learning"),
                            ("run_history", "run_history"),
                            ("attention", "attention_derivation"),
                            ("worker_authority", "worker_authority"),
                            ("mission", "mission_state")):
        assert key in dash, f"{key} missing from dashboard"
        assert capability in covered, f"{key} emitted with no covering capability"


def test_readiness_was_not_improved_by_relabelling_missing_systems():
    """GUI-R added evidence and removed assertions. It must not have moved the
    status: worker_activity still has no producer, so PARTIAL is still correct."""
    truth = rm.build_dashboard(_REPO, now=_NOW)["backend_truth"]
    assert truth["readiness"] == "PARTIAL"
    caps = {c["capability"]: c for c in truth["capabilities"]}
    assert caps["worker_activity"]["state"] == PENDING_BACKEND
    assert caps["worker_activity"]["required"] is True
    for name in ("queue_state", "component_health", "controller_since"):
        assert caps[name]["state"] == PENDING_BACKEND


def test_no_new_backend_producer_was_smuggled_in():
    """Every capability that had no producer before must still have none."""
    truth = rm.build_dashboard(_REPO, now=_NOW)["backend_truth"]
    caps = {c["capability"]: c["state"] for c in truth["capabilities"]}
    for name in ("worker_activity", "queue_state", "component_health",
                 "controller_since"):
        assert caps[name] == PENDING_BACKEND, f"{name} acquired a producer"


# ── I. determinism and read-only safety (preserved) ─────────────────────────
def test_dashboard_is_deterministic_for_identical_evidence_and_now():
    first = rm.build_dashboard(_REPO, now=_NOW)
    second = rm.build_dashboard(_REPO, now=_NOW)
    assert _json.dumps(first, sort_keys=True, default=str) == \
        _json.dumps(second, sort_keys=True, default=str)


def test_readmodel_module_never_reads_the_wall_clock():
    """A projection that stamps its own time cannot be replayed, and freshness
    would silently depend on when it was rendered."""
    src = (_REPO / "portfolio_automation" / "engineer_worker"
           / "ew0a_readmodels.py").read_text(encoding="utf-8")
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            assert name not in ("now", "utcnow", "today"), (
                f"read model calls {name}(); `now` must be injected")


def test_repaired_dashboard_still_carries_no_secrets():
    blob = _json.dumps(rm.build_dashboard(_REPO, now=_NOW), default=str)
    for leak in ("sk-", "Bearer", "Authorization", ".ew0a_openai_key", "api_key"):
        assert leak not in blob


# ═══════════════════════════════════════════════════════════════════════════
# GUI-R REPAIR — PENDING_BACKEND MEANS "NOBODY BUILT THE PRODUCER"
#
# GPT review verdict REPAIR. The candidate still inherited the old rule that an
# absent session ledger, and any exception from the session producer, both
# projected PENDING_BACKEND. `tools/ns0c_session.py` exists, so both answers
# were wrong in the same direction: they reported an operational condition, or
# a perfectly good "there is no session", as unfinished engineering.
# ═══════════════════════════════════════════════════════════════════════════


def _no_ledger_root(tmp_path):
    """A repo root the session producer can read and find nothing in."""
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ── producer classification: the shared rule ────────────────────────────────
def test_missing_module_is_only_true_for_the_producer_itself():
    target = "tools.ns0c_session"
    exact = ModuleNotFoundError("no module", name=target)
    parent = ModuleNotFoundError("no module", name="tools")
    dependency = ModuleNotFoundError("no module", name="yaml")
    sibling = ModuleNotFoundError("no module", name="tools.ns0c_session_other")
    assert rm._missing_module_is(exact, target) is True
    assert rm._missing_module_is(parent, target) is True      # producer cannot exist
    assert rm._missing_module_is(dependency, target) is False  # producer's dependency
    assert rm._missing_module_is(sibling, target) is False
    assert rm._missing_module_is(ModuleNotFoundError("x"), target) is False


def test_generic_import_failure_is_never_evidence_of_absence():
    """The invariant the review named. An ImportError from inside a producer
    means the producer exists and is broken."""
    _module, status, detail = rm._import_producer("tools.ns0c_session")
    assert status == "OK" and detail == ""
    _m, status, detail = rm._import_producer("tools.definitely_not_a_module")
    assert status == "ABSENT"


def test_producer_dependency_failure_classifies_as_unavailable(monkeypatch):
    def _raise(_name):
        raise ModuleNotFoundError("No module named 'somedependency'",
                                  name="somedependency")
    monkeypatch.setattr(rm.importlib, "import_module", _raise)
    _module, status, detail = rm._import_producer("tools.ns0c_session")
    assert status == "UNAVAILABLE"
    assert "somedependency" in detail


# ── A. producer genuinely absent -> PENDING_BACKEND ─────────────────────────
def test_absent_session_producer_is_the_only_route_to_pending_backend(monkeypatch):
    monkeypatch.setattr(rm, "_SESSION_PRODUCER_MODULE", "tools.ns0c_session_not_built")
    payload, status, _detail = rm._build_active_session(_REPO)
    assert status == "ABSENT"
    enriched, state = project_active_session(payload, "m-runtime", _NOW, status)
    assert state is TruthState.PENDING_BACKEND
    assert enriched == PENDING_BACKEND


# ── B. producer answers "no session" -> LIVE, not a gap ─────────────────────
def test_no_session_is_a_live_answer_not_missing_engineering(tmp_path):
    dash = rm.build_dashboard(_no_ledger_root(tmp_path), now=_NOW)
    session = dash["active_session"]
    assert session["session_state"] == "NO_SUCH_SESSION"
    assert session["session_present"] is False
    assert session["truth_state"] == TruthState.LIVE.value
    assert session["truth_state"] != PENDING_BACKEND
    assert session["safe_to_present_as_current_work"] is False
    caps = {c["capability"]: c["state"] for c in dash["backend_truth"]["capabilities"]}
    assert caps["active_session"] == TruthState.LIVE.value


def test_no_session_has_no_mission_to_compare_and_is_not_a_mismatch(tmp_path):
    dash = rm.build_dashboard(_no_ledger_root(tmp_path), now=_NOW)
    assert dash["active_session"]["mission_consistency"] == "UNDETERMINED"


def test_the_no_session_constant_matches_the_producers_own_contract():
    """Drift guard. This module duplicates the producer's sentinel so it can
    classify before knowing the producer loaded; the duplicate must not rot."""
    from tools.ns0c_session import NO_SESSION
    assert rm._NO_SESSION_STATE == NO_SESSION


# ── C. producer exists and fails -> UNAVAILABLE ─────────────────────────────
def test_session_producer_exception_is_unavailable_not_pending_backend(monkeypatch):
    import tools.ns0c_session as producer

    def _boom(**_kwargs):
        raise RuntimeError("ledger corrupt")

    monkeypatch.setattr(producer, "session_projection", _boom)
    payload, status, detail = rm._build_active_session(_REPO)
    assert status == "UNAVAILABLE"
    assert "RuntimeError" in detail
    enriched, state = project_active_session(payload, "m-runtime", _NOW, status, detail)
    assert state is TruthState.UNAVAILABLE
    assert enriched["truth_state"] != PENDING_BACKEND
    assert enriched["session_present"] is False


def test_session_producer_failure_detail_carries_no_exception_payload(monkeypatch):
    """Only the exception TYPE. A message can carry paths or values a projection
    must not render."""
    import tools.ns0c_session as producer

    def _boom(**_kwargs):
        raise RuntimeError("sk-secret-value-/home/pesan/.ew0a_openai_key")

    monkeypatch.setattr(producer, "session_projection", _boom)
    _payload, _status, detail = rm._build_active_session(_REPO)
    assert "sk-" not in detail and ".ew0a_openai_key" not in detail


def test_session_producer_missing_its_entry_point_is_unavailable(monkeypatch):
    import tools.ns0c_session as producer
    monkeypatch.delattr(producer, "session_projection")
    _payload, status, detail = rm._build_active_session(_REPO)
    assert status == "UNAVAILABLE"
    assert "session_projection" in detail


def test_session_producer_returning_garbage_is_unavailable():
    enriched, state = project_active_session(["not", "a", "projection"],
                                             "m-runtime", _NOW)
    assert state is TruthState.UNAVAILABLE
    assert enriched["session_present"] is False


# ── D. multi-ledger regression: discovery belongs to the producer ───────────
def test_a_session_under_another_ledger_is_found_not_called_pending_backend(tmp_path):
    """The load-bearing regression. The read model gated on ONE concrete ledger
    filename while the producer supports multiple ledgers and episode
    discovery, so a perfectly discoverable session was reported as unbuilt
    engineering."""
    from tools.ns0c_session import ledger_path
    docs = tmp_path / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "NORTHSTAR_0C_SESSION_other-episode.jsonl").write_text(
        _json.dumps({"kind": "SessionStarted", "session_id": "other-episode",
                     "mission_id": "m-x", "session_objective": "obj",
                     "session_started_at": "2026-09-01T00:00:00+00:00"}) + "\n",
        encoding="utf-8")

    # the old gate's target really is absent, and a session really is discoverable
    assert not ledger_path(tmp_path).exists()

    dash = rm.build_dashboard(tmp_path, now=_NOW)
    session = dash["active_session"]
    assert session["truth_state"] != PENDING_BACKEND
    assert session["session_present"] is True
    assert session["session_id"] == "other-episode"


def test_the_read_model_does_not_reimplement_episode_discovery():
    """Delegation, structurally. The read model must not decide for itself
    whether a session exists -- the same reason the GUI must not reimplement
    this projection."""
    src = (_REPO / "portfolio_automation" / "engineer_worker"
           / "ew0a_readmodels.py").read_text(encoding="utf-8")
    forbidden = {"ledger_path", "ledger_paths", "load_episodes", "read_events",
                 "split_episodes"}
    called = set()
    for node in _ast.walk(_ast.parse(src)):
        if isinstance(node, _ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name:
                called.add(name)
    assert not (called & forbidden), (
        f"read model reimplements session discovery via {called & forbidden}")


# ── E. valid session behaviour preserved ────────────────────────────────────
def test_repair_preserves_unknown_freshness_for_a_real_session():
    session = rm.build_dashboard(_REPO, now=_NOW)["active_session"]
    assert session["session_present"] is True
    assert session["truth_state"] == TruthState.UNKNOWN.value


def test_repair_preserves_mismatch_is_not_stale():
    session = rm.build_dashboard(_REPO, now=_NOW)["active_session"]
    assert session["mission_consistency"] == "MISMATCH"
    assert session["truth_state"] != TruthState.STALE.value
    assert session["safe_to_present_as_current_work"] is False


# ── F. learning classification, case by case ────────────────────────────────
def test_learning_internal_import_failure_is_unavailable(monkeypatch):
    """The producer exists; one of ITS imports failed. Reporting that as
    PENDING_BACKEND sends an operator to write code that already exists."""
    def _raise(_name):
        raise ModuleNotFoundError("No module named 'somedependency'",
                                  name="somedependency")
    monkeypatch.setattr(rm.importlib, "import_module", _raise)
    projection, state = rm._project_learning(_REPO, "engineer.x", _NOW)
    assert state is TruthState.UNAVAILABLE
    assert projection["truth_state"] == TruthState.UNAVAILABLE.value


def test_learning_incompatible_producer_api_is_unavailable(monkeypatch):
    from portfolio_automation.engineer_worker.learning import readmodels as lrm
    monkeypatch.delattr(lrm, "build_learning_dashboard")
    projection, state = rm._project_learning(_REPO, "engineer.x", _NOW)
    assert state is TruthState.UNAVAILABLE
    assert "build_learning_dashboard" in projection["detail"]


def test_learning_unusable_response_shape_is_not_classified_live(monkeypatch):
    """LIVE must be a statement about the evidence, not about the call
    returning without raising."""
    from portfolio_automation.engineer_worker.learning import readmodels as lrm
    for garbage in (None, "PENDING_BACKEND", [], {"unexpected": "shape"}):
        monkeypatch.setattr(lrm, "build_learning_dashboard",
                            lambda *_a, **_k: garbage)
        projection, state = rm._project_learning(_REPO, "engineer.x", _NOW)
        assert state is TruthState.UNAVAILABLE, garbage
        assert projection["truth_state"] == TruthState.UNAVAILABLE.value


def test_learning_valid_projection_is_live():
    projection, state = rm._project_learning(_REPO, "engineer.local_qwen2_5_7b", _NOW)
    assert state is TruthState.LIVE
    assert projection["truth_state"] == TruthState.LIVE.value
    assert "recent_lessons" in projection


# ── G. the structural invariant ─────────────────────────────────────────────
#: Capabilities for which this repository genuinely contains no producer. Every
#: other capability must reach some state OTHER than PENDING_BACKEND, whatever
#: the data situation.
_PRODUCERLESS = {"worker_activity", "queue_state", "component_health",
                 "controller_since", "attention_derivation", "controller_identity"}


def test_pending_backend_means_unimplemented_and_nothing_else(tmp_path):
    """Run against a repo root with NO data at all -- no policy, no authority,
    no ledgers, no learning store. Emptiness must not manufacture a single extra
    PENDING_BACKEND: absent data is UNAVAILABLE, an absent session is a LIVE
    'no session', and only genuinely unbuilt producers stay pending."""
    dash = rm.build_dashboard(_no_ledger_root(tmp_path), now=_NOW)
    caps = {c["capability"]: c["state"] for c in dash["backend_truth"]["capabilities"]}
    pending = {name for name, state in caps.items() if state == PENDING_BACKEND}
    assert pending == _PRODUCERLESS, (
        f"PENDING_BACKEND no longer means 'unimplemented': {pending ^ _PRODUCERLESS}")


def test_empty_data_is_unavailable_not_pending_backend(tmp_path):
    dash = rm.build_dashboard(_no_ledger_root(tmp_path), now=_NOW)
    caps = {c["capability"]: c["state"] for c in dash["backend_truth"]["capabilities"]}
    assert caps["run_history"] == TruthState.UNAVAILABLE.value
    assert caps["controller_state"] == TruthState.UNAVAILABLE.value
    assert caps["active_session"] == TruthState.LIVE.value


def test_the_real_repo_pending_set_is_unchanged_by_the_repair():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    caps = {c["capability"]: c["state"] for c in dash["backend_truth"]["capabilities"]}
    assert {n for n, s in caps.items() if s == PENDING_BACKEND} == _PRODUCERLESS
    assert dash["backend_truth"]["readiness"] == "PARTIAL"


# ═══════════════════════════════════════════════════════════════════════════
# GUI-R P2 HARDENING — corrupt evidence must degrade, never crash the dashboard
#
# Two confirmed Codex P2 findings, same architectural defect: an IMPLEMENTED
# producer handed corrupt evidence raised straight through build_dashboard()
# instead of degrading to a truth state. A Mission Control page rendering from
# that projection would fail closed to a stack trace rather than to an honest
# UNAVAILABLE -- the precise outcome this projection layer exists to prevent.
# ═══════════════════════════════════════════════════════════════════════════


def _ledger_root(tmp_path, *rows):
    """A repo root whose outcome ledger holds exactly these rows."""
    ledger = tmp_path / OUTCOME_LEDGER_REL
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("".join(_json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return tmp_path


#: A COMPLETE, schema-valid outcome row. The previous fixture carried only
#: task_id and final_status -- which the unvalidated boundary happily projected
#: as LIVE, so the fixture's own incompleteness was invisible. Every field here
#: is present in all seven real ledger records with these types.
_VALID_ROW = {
    "task_id": "T1",
    "title": "Add a unit test for the E1 default executor mapping",
    "risk_class": "E1_ROUTINE",
    "executor": "ENGINEER",
    "final_status": "VERIFIED",
    "recorded_at": "2026-08-11T12:00:03Z",
    "disposition": "verified by deterministic gate and GPT",
    "attempt_count": 1,
    "escalated": False,
    "policy_violation": False,
    "human_intervention": False,
}


def _row(**extra):
    base = dict(_VALID_ROW)
    base.update(extra)
    return base


# ── P2 #1: schema-invalid outcome rows ──────────────────────────────────────
def test_historical_failure_classes_shapes_keep_working(tmp_path):
    """Records written before the field existed, and records that legitimately
    have no failures, must not be swept up by the new validation."""
    cases = {
        "absent": (_row(), []),
        "null": (_row(failure_classes=None), []),
        "empty": (_row(failure_classes=[]), []),
        "populated": (_row(failure_classes=["TEST_FAILURE"]), ["TEST_FAILURE"]),
    }
    for label, (row, expected) in cases.items():
        history = build_run_history(_ledger_root(tmp_path / label, row))
        assert history.availability == TruthState.LIVE.value, label
        assert history.record_count == 1, label
        assert history.runs[0]["failure_classes"] == expected, label


def test_schema_invalid_failure_classes_makes_run_history_unavailable(tmp_path):
    for label, bad in (("int", 123), ("float", 1.5), ("bool", True),
                       ("dict", {"kind": "TEST_FAILURE"})):
        root = _ledger_root(tmp_path / label, _row(failure_classes=bad))
        history = build_run_history(root)
        assert history.availability == TruthState.UNAVAILABLE.value, label
        assert history.runs == [], label
        assert history.record_count == 0, label


def test_a_string_is_not_a_list_of_failure_classes(tmp_path):
    """The quiet half of this finding. `123` announced itself with a TypeError;
    `"TEST_FAILURE"` would have iterated into ['T','E','S','T',...] and been
    rendered as ten separate failure classes with nothing raising at all."""
    root = _ledger_root(tmp_path, _row(failure_classes="TEST_FAILURE"))
    history = build_run_history(root)
    assert history.availability == TruthState.UNAVAILABLE.value
    assert history.runs == []


def test_corrupt_outcome_record_never_escapes_through_build_dashboard(tmp_path):
    """The load-bearing invariant. Mission Control renders from this call."""
    for label, bad in (("int", 123), ("str", "TEST_FAILURE"),
                       ("dict", {"kind": "X"}), ("float", 2.0)):
        root = _ledger_root(tmp_path / label, _row(failure_classes=bad))
        dash = rm.build_dashboard(root, now=_NOW)          # must not raise
        assert dash["run_history"]["availability"] == TruthState.UNAVAILABLE.value, label
        caps = {c["capability"]: c["state"] for c in dash["backend_truth"]["capabilities"]}
        assert caps["run_history"] == TruthState.UNAVAILABLE.value, label
        assert caps["run_history"] != PENDING_BACKEND, label


def test_one_bad_record_does_not_yield_a_quietly_truncated_history(tmp_path):
    """No partial-ledger semantics: dropping the bad row and serving the rest
    would leave a consumer unable to tell a complete history from a truncated
    one. No authoritative contract establishes partial-ledger reads."""
    root = _ledger_root(tmp_path,
                        _row(task_id="GOOD1", failure_classes=["TEST_FAILURE"]),
                        _row(task_id="BAD", failure_classes=123),
                        _row(task_id="GOOD2"))
    history = build_run_history(root)
    assert history.availability == TruthState.UNAVAILABLE.value
    assert history.record_count == 0
    assert history.runs == []


def test_corrupt_payload_content_is_never_echoed_into_the_projection(tmp_path):
    """A projection must not render content it has just declared unusable."""
    secret = "sk-not-a-real-key-abcdef"
    root = _ledger_root(tmp_path, _row(failure_classes={"leak": secret}))
    history = build_run_history(root)
    blob = _json.dumps(history.to_dict())
    assert secret not in blob
    assert "leak" not in blob
    assert "dict" in history.detail        # the TYPE is enough to diagnose


def test_failure_class_validator_raises_valueerror_not_an_incidental_typeerror():
    """An explicit contract violation, not whatever exception iteration happens
    to produce -- build_run_history's existing guard converts ValueError."""
    with pytest.raises(ValueError, match="failure_classes"):
        rm._projected_failure_classes({"failure_classes": 123})
    assert rm._projected_failure_classes({}) == []
    assert rm._projected_failure_classes({"failure_classes": None}) == []


# ── P2 #2: decoding failures are unreadable, not exceptions ────────────────
def test_readability_classifies_a_decode_failure_as_unreadable(tmp_path):
    """UnicodeDecodeError is a ValueError, NOT an OSError, so it used to escape
    `_readability` entirely and take the dashboard down with it."""
    bad = tmp_path / "corrupt.json"
    bad.write_bytes(b"\xff\xfe\x00not utf-8")
    assert rm._readability(bad) == "UNREADABLE"


def test_readability_preserves_its_other_three_answers(tmp_path):
    good = tmp_path / "good.json"
    good.write_text('{"level": "A0_DIAGNOSTIC"}', encoding="utf-8")
    assert rm._readability(good) == "READABLE"
    assert rm._readability(tmp_path / "missing.json") == "ABSENT"
    # a directory exists and cannot be read as a file -> IsADirectoryError (OSError)
    assert rm._readability(tmp_path) == "UNREADABLE"


def test_readability_reports_an_io_failure_as_unreadable(monkeypatch, tmp_path):
    target = tmp_path / "present.json"
    target.write_text("{}", encoding="utf-8")

    def _boom(*_a, **_k):
        raise PermissionError("denied")

    monkeypatch.setattr(_Path, "read_text", _boom)
    assert rm._readability(target) == "UNREADABLE"


def test_readability_does_not_disguise_a_programming_defect(monkeypatch, tmp_path):
    """Deliberately narrow. Catching Exception here would turn a bug in this
    module into a serene 'UNREADABLE' and hide it forever."""
    target = tmp_path / "present.json"
    target.write_text("{}", encoding="utf-8")

    def _bug(*_a, **_k):
        raise AttributeError("programming defect")

    monkeypatch.setattr(_Path, "read_text", _bug)
    with pytest.raises(AttributeError):
        rm._readability(target)


def test_undecodable_protected_config_does_not_take_down_the_dashboard(tmp_path):
    """Observability boundary, not config parsing: the authority reader is still
    entitled to fail closed to A0. What must not happen is the READABILITY
    projection raising through build_dashboard()."""
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "ew0a_authority.json").write_bytes(b"\xff\xfe\x00bad")

    dash = rm.build_dashboard(tmp_path, now=_NOW)          # must not raise
    readability = dash["system_health"]["config_readability"]
    assert readability["authority_record"] == "UNREADABLE"
    assert readability["runtime_policy"] == "ABSENT"
    # and the authority projection still fails closed rather than inventing a level
    assert dash["worker_authority"]["level"] == "A0_DIAGNOSTIC"
    assert dash["worker_authority"]["can_merge"] is False


def test_undecodable_outcome_ledger_degrades_rather_than_raising(tmp_path):
    """The canonical reader's own decode failure path, checked end to end."""
    ledger = tmp_path / OUTCOME_LEDGER_REL
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"\xff\xfe\x00not utf-8")
    dash = rm.build_dashboard(tmp_path, now=_NOW)          # must not raise
    assert dash["run_history"]["availability"] == TruthState.UNAVAILABLE.value


# ── the invariant both findings violated ───────────────────────────────────
def test_no_corrupt_input_in_these_two_paths_escapes_as_an_exception(tmp_path):
    """Both P2s in one statement: an implemented producer handed corrupt
    evidence degrades to a truth state and build_dashboard() still returns."""
    root = tmp_path / "both"
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "config" / "ew0a_authority.json").write_bytes(b"\xff\xfe\x00bad")
    (root / OUTCOME_LEDGER_REL).write_text(
        _json.dumps(_row(failure_classes="TEST_FAILURE")) + "\n", encoding="utf-8")

    dash = rm.build_dashboard(root, now=_NOW)              # must not raise
    assert dash["system_health"]["config_readability"]["authority_record"] == "UNREADABLE"
    assert dash["run_history"]["availability"] == TruthState.UNAVAILABLE.value
    # still not PENDING_BACKEND: both producers exist, they just cannot answer
    caps = {c["capability"]: c["state"] for c in dash["backend_truth"]["capabilities"]}
    assert caps["run_history"] == TruthState.UNAVAILABLE.value
    # Readiness correctly drops to UNAVAILABLE here rather than staying PARTIAL:
    # an unreadable authority record means the oversight floor is not
    # established, and the readiness contract says so. That is the honest
    # answer, and it is reached by degrading -- not by raising.
    assert dash["backend_truth"]["readiness"] == "UNAVAILABLE"
    assert any("oversight floor" in r for r in dash["backend_truth"]["reasons"])


# ═══════════════════════════════════════════════════════════════════════════
# GUI-R CURRENT-HEAD P2 REPAIR
#
# The fresh Codex review of f24eb7b found three more P2s, all in code GUI-R
# introduced, all violating a guarantee GUI-R had published:
#
#   1. failure_classes validated its CONTAINER but str()-coerced its ELEMENTS,
#      so [{"api_key": "sk-..."}] projected LIVE with the secret inside it;
#   2. non-object ledger rows were silently FILTERED and the remainder reported
#      LIVE, so corrupt evidence became a "complete" (or empty) history;
#   3. a session missing its session_id got truth_state=UNAVAILABLE alongside
#      session_present=true -- a phantom active session.
# ═══════════════════════════════════════════════════════════════════════════

#: Conspicuous synthetic marker. Never a real credential; its only job is to be
#: findable in a serialized projection if the no-secrets boundary ever leaks.
_SECRET_MARKER = "sk-SHOULD-NEVER-RENDER-123"


# ── P2 #1: every element must be a string ──────────────────────────────────
def test_valid_failure_class_element_shapes_are_preserved(tmp_path):
    cases = {
        "absent": (_row(), []),
        "null": (_row(failure_classes=None), []),
        "empty": (_row(failure_classes=[]), []),
        "one": (_row(failure_classes=["TEST_FAILURE"]), ["TEST_FAILURE"]),
        "many": (_row(failure_classes=["A", "B"]), ["A", "B"]),
    }
    for label, (row, expected) in cases.items():
        history = build_run_history(_ledger_root(tmp_path / label, row))
        assert history.availability == TruthState.LIVE.value, label
        assert history.runs[0]["failure_classes"] == expected, label


def test_non_string_failure_class_elements_make_the_history_unavailable(tmp_path):
    """`list[str]` means every element is a string. Validating only the
    container is what left str() rendering the elements."""
    for label, bad in (("int", [123]), ("float", [1.5]), ("bool", [True]),
                       ("null", [None]), ("dict", [{"kind": "TEST_FAILURE"}]),
                       ("nested", [["TEST_FAILURE"]]),
                       ("mixed", ["TEST_FAILURE", 123])):
        root = _ledger_root(tmp_path / f"elem_{label}", _row(failure_classes=bad))
        history = build_run_history(root)
        assert history.availability == TruthState.UNAVAILABLE.value, label
        assert history.runs == [], label
        assert history.record_count == 0, label


def test_invalid_containers_are_still_rejected(tmp_path):
    """The previous commit's behaviour must not regress while fixing elements."""
    for label, bad in (("int", 123), ("str", "TEST_FAILURE"), ("dict", {})):
        root = _ledger_root(tmp_path / f"cont_{label}", _row(failure_classes=bad))
        assert build_run_history(root).availability == TruthState.UNAVAILABLE.value, label


def test_a_secret_in_a_corrupt_failure_class_never_reaches_the_projection(tmp_path):
    """THE load-bearing security regression. This is not a schema test: it pins
    the read model's no-secret-propagation boundary.

    Before the fix, `[{"api_key": "<marker>"}]` projected LIVE and rendered
    "{'api_key': '<marker>'}" into failure_classes -- a secret arriving through
    a field nobody thinks of as a secret carrier, which is precisely why it
    survived a review that was looking at the container."""
    root = _ledger_root(tmp_path, _row(failure_classes=[{"api_key": _SECRET_MARKER}]))

    history = build_run_history(root)
    assert history.availability == TruthState.UNAVAILABLE.value
    assert history.runs == []
    assert history.record_count == 0
    assert _SECRET_MARKER not in _json.dumps(history.to_dict())

    dash = rm.build_dashboard(root, now=_NOW)          # must not raise
    assert _SECRET_MARKER not in _json.dumps(dash, default=str)
    assert "api_key" not in _json.dumps(dash, default=str)


def test_the_marker_survives_nesting_and_alternative_carriers(tmp_path):
    """Several shapes a corrupt element could take, one marker to find."""
    carriers = (
        [{"nested": {"deep": _SECRET_MARKER}}],
        [[_SECRET_MARKER]],
        [{"Authorization": f"Bearer {_SECRET_MARKER}"}],
    )
    for index, carrier in enumerate(carriers):
        root = _ledger_root(tmp_path / f"carrier{index}", _row(failure_classes=carrier))
        history = build_run_history(root)
        assert history.availability == TruthState.UNAVAILABLE.value, carrier
        blob = _json.dumps(history.to_dict()) + _json.dumps(
            rm.build_dashboard(root, now=_NOW), default=str)
        assert _SECRET_MARKER not in blob, carrier


def test_the_failure_class_projection_never_stringifies_ledger_evidence():
    """Structural. The leak existed because a str() call sat on the projection
    path; an AST check is what stops one from coming back."""
    src = (_REPO / "portfolio_automation" / "engineer_worker"
           / "ew0a_readmodels.py").read_text(encoding="utf-8")
    for node in _ast.walk(_ast.parse(src)):
        if isinstance(node, _ast.FunctionDef) and node.name == "_projected_failure_classes":
            for inner in _ast.walk(node):
                if isinstance(inner, _ast.Call):
                    name = getattr(inner.func, "id", None) or getattr(inner.func, "attr", None)
                    assert name not in ("str", "repr", "format"), (
                        f"failure-class projection calls {name}() on ledger evidence")
            break
    else:                                              # pragma: no cover
        raise AssertionError("_projected_failure_classes not found")


def test_element_validation_reports_only_the_type():
    with pytest.raises(ValueError, match="elements must be strings"):
        rm._projected_failure_classes({"failure_classes": [{"api_key": _SECRET_MARKER}]})
    try:
        rm._projected_failure_classes({"failure_classes": [{"api_key": _SECRET_MARKER}]})
    except ValueError as exc:
        assert _SECRET_MARKER not in str(exc)
        assert "dict" in str(exc)


# ── P2 #2: a non-object row invalidates the ledger ─────────────────────────
def test_non_object_ledger_rows_make_the_whole_history_unavailable(tmp_path):
    for label, bad in (("array", [1, 2]), ("int", 123), ("string", "row"),
                       ("null", None), ("bool", True)):
        root = _ledger_root(tmp_path / f"nonobj_{label}", bad)
        history = build_run_history(root)
        assert history.availability == TruthState.UNAVAILABLE.value, label
        assert history.record_count == 0, label
        assert history.runs == [], label


def test_a_mixed_ledger_is_not_served_as_a_complete_history(tmp_path):
    """Before the fix this projected 2 records from a 3-row ledger and called it
    LIVE. Silent truncation is worse than a crash: a crash announces itself."""
    root = _ledger_root(tmp_path,
                        _row(task_id="GOOD1"), [1, 2], _row(task_id="GOOD2"))
    history = build_run_history(root)
    assert history.availability == TruthState.UNAVAILABLE.value
    assert history.record_count == 0
    assert history.runs == []


def test_an_all_corrupt_ledger_is_not_mistaken_for_an_empty_history(tmp_path):
    """The sharpest form. A ledger of nothing but corrupt rows used to project
    LIVE with zero records -- indistinguishable from 'nothing has happened yet'.
    A genuinely empty ledger IS a legitimate empty history, and the two must not
    collapse into the same answer."""
    corrupt = build_run_history(_ledger_root(tmp_path / "corrupt", [1, 2], "row", 7))
    assert corrupt.availability == TruthState.UNAVAILABLE.value
    assert corrupt.record_count == 0

    empty_dir = tmp_path / "empty"
    (empty_dir / OUTCOME_LEDGER_REL).parent.mkdir(parents=True, exist_ok=True)
    (empty_dir / OUTCOME_LEDGER_REL).write_text("", encoding="utf-8")
    genuinely_empty = build_run_history(empty_dir)
    assert genuinely_empty.availability == TruthState.LIVE.value
    assert genuinely_empty.record_count == 0

    assert corrupt.availability != genuinely_empty.availability


def test_a_fully_valid_ledger_keeps_live_status_and_append_order(tmp_path):
    root = _ledger_root(tmp_path, _row(task_id="A"), _row(task_id="B"),
                        _row(task_id="C"))
    history = build_run_history(root)
    assert history.availability == TruthState.LIVE.value
    assert [r["task_id"] for r in history.runs] == ["A", "B", "C"]
    assert [r["ledger_index"] for r in history.runs] == [0, 1, 2]
    assert history.ordering == "ledger_append_order"


def test_non_object_rows_never_escape_through_build_dashboard(tmp_path):
    for label, bad in (("array", [1, 2]), ("string", "row"), ("null", None)):
        root = _ledger_root(tmp_path / f"dash_{label}", _row(), bad)
        dash = rm.build_dashboard(root, now=_NOW)      # must not raise
        assert dash["run_history"]["availability"] == TruthState.UNAVAILABLE.value, label


def test_the_row_rejection_detail_reveals_only_structure(tmp_path):
    root = _ledger_root(tmp_path, [_SECRET_MARKER])
    detail = build_run_history(root).detail
    assert _SECRET_MARKER not in detail
    assert "list" in detail and "row 0" in detail


# ── P2 #3: presence requires a usable identity ─────────────────────────────
def _session_with_id(session_id):
    base = dict(_VALID_SESSION)
    base["session_id"] = session_id
    return base


def test_a_session_without_a_usable_identity_is_never_present():
    """truth_state=UNAVAILABLE with session_present=true was internally
    contradictory, and a GUI could render it as a phantom active session."""
    for label, bad in (("none", None), ("empty", ""), ("blank", "   "),
                       ("int", 123), ("list", []), ("dict", {}),
                       ("sentinel", "NO_SUCH_SESSION"),
                       ("pending", PENDING_BACKEND)):
        enriched, state = project_active_session(
            _session_with_id(bad), "m-runtime", _NOW)
        assert enriched["session_present"] is False, label
        assert state is TruthState.UNAVAILABLE, label
        assert enriched["truth_state"] == TruthState.UNAVAILABLE.value, label
        assert enriched["safe_to_present_as_current_work"] is False, label
        assert enriched["mission_consistency"] == "UNDETERMINED", label


def test_a_malformed_session_carries_no_current_work_fields():
    """Not a half-valid session dictionary: the failure envelope replaces it, so
    a template cannot read a task id out of an unusable session."""
    enriched, _state = project_active_session(
        _session_with_id(None), "m-runtime", _NOW)
    assert "current_task_id" not in enriched
    assert enriched["session_state"] == "UNAVAILABLE"


def test_a_valid_identity_still_reaches_the_reviewed_unknown_behaviour():
    enriched, state = project_active_session(
        _session_with_id("s1"), "m-runtime", _NOW)
    assert enriched["session_present"] is True
    assert state is TruthState.UNKNOWN
    assert enriched["mission_consistency"] == "AGREES"
    assert enriched["safe_to_present_as_current_work"] is False


def test_no_such_session_keeps_its_reviewed_live_treatment(tmp_path):
    dash = rm.build_dashboard(_no_ledger_root(tmp_path), now=_NOW)
    session = dash["active_session"]
    assert session["session_state"] == "NO_SUCH_SESSION"
    assert session["session_present"] is False
    assert session["truth_state"] == TruthState.LIVE.value
    assert session["safe_to_present_as_current_work"] is False


def test_identity_validation_did_not_disturb_mismatch_or_freshness():
    enriched, state = project_active_session(
        _session_with_id("s1") | {"mission_id": "m-other"}, "m-runtime", _NOW)
    assert enriched["session_present"] is True
    assert enriched["mission_consistency"] == "MISMATCH"
    assert state is not TruthState.STALE
    assert state is TruthState.UNKNOWN


def test_the_real_repo_session_is_unaffected_by_identity_validation():
    session = rm.build_dashboard(_REPO, now=_NOW)["active_session"]
    assert session["session_present"] is True
    assert session["session_id"] == "ns0c-revision-supersession-002"
    assert session["truth_state"] == TruthState.UNKNOWN.value
    assert session["mission_consistency"] == "MISMATCH"
    assert session["safe_to_present_as_current_work"] is False


# ═══════════════════════════════════════════════════════════════════════════
# GUI-R PROJECTION-BOUNDARY SCHEMA CERTIFICATION
#
# Three review rounds found the same class, not three bugs: schema-invalid
# authoritative evidence crossing this boundary unvalidated, where coercion
# leaks payload (str() on a dict renders the dict), `x is True` rewrites corrupt
# evidence as clean False, and a one-field sentinel check admits contradictory
# state. Field-by-field fixing could not converge. These tests certify the
# boundary instead: an enumerable contract, swept by a matrix.
# ═══════════════════════════════════════════════════════════════════════════

#: One marker, every carrier. If the no-secret boundary leaks anywhere in the
#: certified paths, this string shows up in a serialized projection.
_MARKER = "sk-WCC-PROJECTION-MUST-NOT-RENDER-999"

#: Representative malformed carriers. The dict/list ones carry the marker so a
#: leak is detectable, not merely a type error.
_CARRIERS = {
    "marker_dict": {"api_key": _MARKER},
    "marker_list": [_MARKER],
    "marker_nested": {"outer": {"Authorization": f"Bearer {_MARKER}"}},
    "int": 7,
    "float": 1.5,
    "bool": True,
    "dict": {},
    "nested_list": [["X"]],
}

#: The declared contract, mirrored independently of the module so the test does
#: not simply agree with whatever the implementation happens to do.
_EXPECTED_CONTRACT = {
    "task_id": ("str", True),
    "title": ("str", True),
    "risk_class": ("str", True),
    "executor": ("str", True),
    "final_status": ("str", True),
    "recorded_at": ("str", True),
    "disposition": ("str", True),
    "attempt_count": ("int", True),
    "escalated": ("bool", True),
    "policy_violation": ("bool", True),
    "human_intervention": ("bool", True),
    "failure_classes": ("list[str]", False),
    "supervisor_verdict": ("str", False),
    "mission_id": ("str", False),
    "candidate_sha": ("str", False),
}


def _one_row_root(tmp_path, label, **override):
    return _ledger_root(tmp_path / label, _row(**override))


def _serialized(root):
    """Everything a consumer could ever see from this root."""
    return (_json.dumps(build_run_history(root).to_dict())
            + _json.dumps(rm.build_dashboard(root, now=_NOW), default=str))


# ── the contract itself ────────────────────────────────────────────────────
def test_the_declared_contract_matches_the_implementation():
    """Pins the enumerable table, so a field cannot be quietly dropped from
    validation without this failing."""
    actual = {f.name: (f.kind, f.required) for f in rm._OUTCOME_FIELDS}
    assert actual == _EXPECTED_CONTRACT


def test_the_real_ledger_satisfies_the_certified_contract():
    """The certification must not condemn the true history. Every one of the
    seven real records validates, and the projection stays LIVE."""
    history = build_run_history(_REPO)
    assert history.availability == TruthState.LIVE.value
    assert history.record_count == 7
    raw = [_json.loads(l) for l in
           (_REPO / OUTCOME_LEDGER_REL).read_text(encoding="utf-8").splitlines() if l.strip()]
    for index, rec in enumerate(raw):
        rm.validate_outcome_record(rec, index)          # must not raise


def test_optional_fields_may_be_absent_or_null_as_the_real_ledger_has_them():
    """Every real record OMITS mission_id and candidate_sha and carries
    supervisor_verdict: null. Treating those as invalid would be a certification
    that fails on the truth."""
    validated = rm.validate_outcome_record(_row())
    assert validated["mission_id"] is None
    assert validated["candidate_sha"] is None
    assert validated["failure_classes"] == []
    assert rm.validate_outcome_record(_row(supervisor_verdict=None))["supervisor_verdict"] is None


# ── the matrix ─────────────────────────────────────────────────────────────
def test_every_certified_field_rejects_every_malformed_carrier(tmp_path):
    """The whole point of the mission: one sweep over field x carrier, rather
    than one test per reported field."""
    checked = 0
    for field, (kind, _required) in _EXPECTED_CONTRACT.items():
        for carrier_name, carrier in _CARRIERS.items():
            if kind == "str" and isinstance(carrier, str):
                continue                                # would be legitimate
            if kind == "int" and type(carrier) is int:
                continue                                # legitimate int
            if kind == "bool" and type(carrier) is bool:
                continue                                # legitimate bool
            if kind == "list[str]" and carrier == [_MARKER]:
                continue                                # legitimate list[str]
            label = f"{field}__{carrier_name}"
            root = _one_row_root(tmp_path, label, **{field: carrier})
            history = build_run_history(root)
            assert history.availability == TruthState.UNAVAILABLE.value, label
            assert history.runs == [], label
            assert history.record_count == 0, label
            rm.build_dashboard(root, now=_NOW)          # must not raise
            checked += 1
    assert checked >= 80, f"matrix too small to be a sweep ({checked})"


def test_required_fields_may_not_be_absent_or_null(tmp_path):
    for field, (_kind, required) in _EXPECTED_CONTRACT.items():
        if not required:
            continue
        missing = _row()
        del missing[field]
        assert build_run_history(
            _ledger_root(tmp_path / f"absent_{field}", missing)
        ).availability == TruthState.UNAVAILABLE.value, field
        assert build_run_history(
            _ledger_root(tmp_path / f"null_{field}", _row(**{field: None}))
        ).availability == TruthState.UNAVAILABLE.value, field


def test_a_malformed_required_identifier_does_not_disappear_into_none(tmp_path):
    """The old `_s()` returned None for a non-string, so a corrupt task_id
    vanished while the ledger still reported LIVE -- a run with no identity,
    presented as trustworthy history."""
    root = _one_row_root(tmp_path, "badid", task_id={"api_key": _MARKER})
    history = build_run_history(root)
    assert history.availability == TruthState.UNAVAILABLE.value
    assert history.runs == []


def test_no_raw_outcome_field_is_projected_without_validation():
    """Structural: every key _project_run emits either comes from the validator
    or is the index this module generates itself."""
    projected = set(build_run_history(_REPO).runs[0])
    assert projected - {"ledger_index"} == set(_EXPECTED_CONTRACT)


# ── booleans and integers: the silent-rewrite direction ───────────────────
def test_malformed_booleans_do_not_become_clean_negative_evidence(tmp_path):
    """`rec.get(x) is True` turned "true", 1 and {} into False. For a field
    named policy_violation that is the most dangerous possible direction: a
    corrupt record would have read as 'no violation'."""
    for field in ("escalated", "policy_violation", "human_intervention"):
        for carrier in ("true", 1, 0, {"api_key": _MARKER}, [], "False"):
            label = f"{field}_{type(carrier).__name__}_{carrier!r}"[:40]
            root = _one_row_root(tmp_path, label, **{field: carrier})
            assert build_run_history(root).availability == \
                TruthState.UNAVAILABLE.value, label
    # and the legitimate values still project faithfully
    assert build_run_history(
        _one_row_root(tmp_path, "pv_true", policy_violation=True)
    ).runs[0]["policy_violation"] is True


def test_attempt_count_requires_a_real_non_negative_integer(tmp_path):
    assert build_run_history(
        _one_row_root(tmp_path, "ac0", attempt_count=0)
    ).availability == TruthState.LIVE.value
    for bad in (True, False, "2", 1.5, -1, {"api_key": _MARKER}, [1]):
        label = f"ac_{type(bad).__name__}_{bad!r}"[:40]
        assert build_run_history(
            _one_row_root(tmp_path, label, attempt_count=bad)
        ).availability == TruthState.UNAVAILABLE.value, label


def test_a_boolean_is_not_an_attempt_count():
    """isinstance(True, int) is True in Python, so this needs an explicit check
    or `true` passes as a count."""
    with pytest.raises(ValueError, match="attempt_count"):
        rm.validate_outcome_record(_row(attempt_count=True))


# ── the no-secret sweep ───────────────────────────────────────────────────
def test_the_marker_never_reaches_any_serialized_output(tmp_path):
    """Matrix, not a single-field test. Every certified field, every carrier
    capable of holding arbitrary JSON."""
    swept = 0
    for field, (kind, _required) in _EXPECTED_CONTRACT.items():
        for carrier_name in ("marker_dict", "marker_list", "marker_nested"):
            # `[marker]` is a legitimate list[str], so for failure_classes the
            # marker is real evidence and SHOULD be projected. Sweeping it there
            # would test that valid input is discarded, which is the opposite of
            # the contract.
            if kind == "list[str]" and carrier_name == "marker_list":
                continue
            label = f"sweep_{field}_{carrier_name}"
            root = _one_row_root(tmp_path, label,
                                 **{field: _CARRIERS[carrier_name]})
            blob = _serialized(root)
            assert _MARKER not in blob, label
            assert "api_key" not in blob, label
            assert "Authorization" not in blob, label
            swept += 1
    list_fields = sum(1 for k, _ in _EXPECTED_CONTRACT.values() if k == "list[str]")
    assert swept == len(_EXPECTED_CONTRACT) * 3 - list_fields


def test_validation_messages_carry_types_not_payloads():
    for field in ("task_id", "attempt_count", "policy_violation", "failure_classes"):
        try:
            rm.validate_outcome_record(_row(**{field: {"api_key": _MARKER}}))
        except ValueError as exc:
            assert _MARKER not in str(exc), field
            assert field in str(exc), field
        else:                                            # pragma: no cover
            raise AssertionError(f"{field} accepted a dict")


#: Names the certified paths may interpolate into a message. Each is either a
#: label this module constructed or a declared kind -- never source evidence.
_SAFE_INTERPOLATIONS = {"name", "kind", "label", "where", "index", "field", "rel"}


def test_the_certified_outcome_path_never_stringifies_evidence():
    """AST. The leaks existed because str() sat on projection paths."""
    src = (_REPO / "portfolio_automation" / "engineer_worker"
           / "ew0a_readmodels.py").read_text(encoding="utf-8")
    tree = _ast.parse(src)
    certified = {"validate_outcome_record", "_validated_string_list",
                 "_validated_scalar", "_project_run", "_projected_failure_classes",
                 "effective_denied_ops", "derive_authority_capabilities"}
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name in certified:
            for inner in _ast.walk(node):
                if isinstance(inner, _ast.Call):
                    name = getattr(inner.func, "id", None) or getattr(inner.func, "attr", None)
                    assert name not in ("str", "repr", "format"), (
                        f"{node.name} calls {name}() on evidence")
                if isinstance(inner, _ast.FormattedValue):
                    # An f-string may interpolate a label this module built or a
                    # TYPE name. It may never interpolate a source value: that is
                    # str() by another spelling, and it is how the payload leaks.
                    expr = inner.value
                    if isinstance(expr, _ast.IfExp):
                        assert all(isinstance(b, _ast.Constant)
                                   for b in (expr.body, expr.orelse)), (
                            f"{node.name} interpolates a computed value")
                        continue
                    if isinstance(expr, _ast.Attribute):
                        assert expr.attr in ("__name__", "name", "value"), (
                            f"{node.name} interpolates attribute {expr.attr}")
                        continue
                    assert isinstance(expr, _ast.Name), (
                        f"{node.name} interpolates a non-trivial expression")
                    assert expr.id in _SAFE_INTERPOLATIONS, (
                        f"{node.name} interpolates {expr.id!r}, which may be evidence")


# ── authority: grants AND forbidden_ops ───────────────────────────────────
def test_both_authority_list_fields_are_validated():
    """Fixing only forbidden_ops would leave the sibling carrier open."""
    for field in ("grants", "forbidden_ops"):
        for carrier in ([{"api_key": _MARKER}], [123], [True], [None],
                        [["MERGE"]], ["MERGE", {"api_key": _MARKER}],
                        123, "MERGE", {}):
            summary = build_worker_authority_summary(
                Lvl.A1_ASSISTED_ENGINEERING,
                **{"grants": [], "forbidden_ops": None} | {field: carrier})
            label = f"{field}={carrier!r}"[:50]
            assert summary.record_evidence == TruthState.UNAVAILABLE.value, label
            assert _MARKER not in _json.dumps(summary.to_dict()), label


def test_valid_authority_lists_still_project_live():
    summary = build_worker_authority_summary(
        Lvl.A1_ASSISTED_ENGINEERING, grants=["approved E1/E2"],
        forbidden_ops=["MERGE", "DEPLOY"])
    assert summary.record_evidence == TruthState.LIVE.value
    assert summary.grants == ["approved E1/E2"]
    assert build_worker_authority_summary(
        Lvl.A0_DIAGNOSTIC, grants=[], forbidden_ops=[]).record_evidence == \
        TruthState.LIVE.value


def test_a_malformed_authority_record_keeps_every_permanent_denial():
    """Capability safety must never depend on the record being well-formed."""
    summary = build_worker_authority_summary(
        Lvl.A1_ASSISTED_ENGINEERING, grants=[{"api_key": _MARKER}],
        forbidden_ops=[{"api_key": _MARKER}])
    assert not (summary.can_merge or summary.can_deploy or summary.can_mutate_main
                or summary.can_write_production or summary.can_self_promote)
    for op in ("MERGE", "DEPLOY", "MAIN_WRITE", "PRODUCTION_WRITE", "SELF_PROMOTION"):
        assert op in summary.forbidden_ops
    assert summary.grants == []


def test_a_malformed_authority_record_is_not_silently_filtered():
    """Dropping the malformed entries and reporting LIVE would hide that the
    record meant to impose restrictions nobody can now read."""
    summary = build_worker_authority_summary(
        Lvl.A1_ASSISTED_ENGINEERING, grants=[], forbidden_ops=["MERGE", 123])
    assert summary.record_evidence == TruthState.UNAVAILABLE.value
    assert "unusable" in summary.record_detail


def test_unusable_authority_evidence_reaches_backend_truth(tmp_path):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "ew0a_authority.json").write_text(
        _json.dumps({"level": "A1_ASSISTED_ENGINEERING",
                     "grants": [{"api_key": _MARKER}],
                     "forbidden_ops": ["MERGE"]}), encoding="utf-8")

    dash = rm.build_dashboard(tmp_path, now=_NOW)         # must not raise
    assert _MARKER not in _json.dumps(dash, default=str)
    assert dash["worker_authority"]["record_evidence"] == TruthState.UNAVAILABLE.value
    caps = {c["capability"]: c["state"] for c in dash["backend_truth"]["capabilities"]}
    assert caps["worker_authority"] == TruthState.UNAVAILABLE.value
    # worker_authority is part of the oversight floor, so readiness must drop
    assert dash["backend_truth"]["readiness"] == "UNAVAILABLE"
    assert any("oversight floor" in r for r in dash["backend_truth"]["reasons"])


def test_the_derivation_helpers_refuse_malformed_input_rather_than_render_it():
    """A permissive public signature is how coercion came back last time."""
    with pytest.raises(ValueError):
        rm.effective_denied_ops([{"api_key": _MARKER}])
    with pytest.raises(ValueError):
        rm.derive_authority_capabilities([{"api_key": _MARKER}])
    # valid input unchanged
    assert "MERGE" in rm.effective_denied_ops(["CUSTOM"])
    assert rm.derive_authority_capabilities(frozenset())["can_merge"] is True


def test_the_real_authority_record_still_projects_live():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    authority = dash["worker_authority"]
    assert authority["record_evidence"] == TruthState.LIVE.value
    assert authority["level"] == "A1_ASSISTED_ENGINEERING"
    assert len(authority["grants"]) == 8
    assert authority["can_merge"] is False
    assert dash["backend_truth"]["readiness"] == "PARTIAL"


# ── no-session must be the producer's structural envelope ─────────────────
def _public_no_session(tmp_path):
    """The producer's own answer, obtained publicly -- not `_no_session()`."""
    from tools.ns0c_session import session_projection
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    return session_projection(repo_root=tmp_path)


def test_the_legitimate_empty_envelope_is_still_live(tmp_path):
    enriched, state = project_active_session(
        _public_no_session(tmp_path), "m-runtime", _NOW)
    assert state is TruthState.LIVE
    assert enriched["session_present"] is False
    assert enriched["safe_to_present_as_current_work"] is False


def test_an_unknown_requested_session_id_is_still_a_valid_no_session(tmp_path):
    """The producer legitimately echoes back a requested-but-unknown id, so the
    envelope check must NOT require session_id is None."""
    from tools.ns0c_session import session_projection
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    projection = session_projection(repo_root=tmp_path, session_id="does-not-exist")
    assert projection["session_id"] == "does-not-exist"
    enriched, state = project_active_session(projection, "m-runtime", _NOW)
    assert state is TruthState.LIVE
    assert enriched["session_present"] is False


def test_every_populated_sentinel_contradiction_becomes_unavailable(tmp_path):
    """One field at a time, starting from the real envelope. A sentinel state
    alongside any evidence of actual work is a contradiction, not an answer."""
    base = _public_no_session(tmp_path)
    contradictions = (
        {"current_task_id": "T1"},
        {"current_task_title": "task"},
        {"current_stage": "VERIFYING"},
        {"tasks_attempted": 1},
        {"tasks_verified": 1},
        {"tasks_repaired": 1},
        {"tasks_escalated": 1},
        {"tasks_abstained": 1},
        {"tasks_incomplete": 1},
        {"blockers": ["something"]},
        {"mission_id": "real-mission"},
        {"session_objective": "real objective"},
        {"session_started_at": "2026-08-16T07:17:41+00:00"},
        {"starting_main_sha": "abc123"},
        {"known_sessions": "not-a-list"},
    )
    for contradiction in contradictions:
        forged = dict(base)
        forged.update(contradiction)
        enriched, state = project_active_session(forged, "m-runtime", _NOW)
        assert state is TruthState.UNAVAILABLE, contradiction
        assert enriched["session_present"] is False, contradiction
        assert enriched["safe_to_present_as_current_work"] is False, contradiction
        # nothing a GUI could mistake for live work survives
        for leaked in ("current_task_id", "current_stage", "tasks_verified",
                       "session_objective"):
            assert leaked not in enriched, (contradiction, leaked)


def test_a_false_counter_is_not_accepted_as_zero(tmp_path):
    """`False == 0` in Python, so the counter check needs an explicit bool
    rejection or a forged envelope slips through."""
    forged = dict(_public_no_session(tmp_path))
    forged["tasks_verified"] = False
    _enriched, state = project_active_session(forged, "m-runtime", _NOW)
    assert state is TruthState.UNAVAILABLE


def test_valid_and_mismatched_sessions_keep_their_reviewed_behaviour():
    session = rm.build_dashboard(_REPO, now=_NOW)["active_session"]
    assert session["session_present"] is True
    assert session["truth_state"] == TruthState.UNKNOWN.value
    assert session["mission_consistency"] == "MISMATCH"
    assert session["truth_state"] != TruthState.STALE.value
    assert session["safe_to_present_as_current_work"] is False


def test_the_no_session_envelope_validates_its_own_carried_fields(tmp_path):
    """Found by the bounded projection-boundary audit, not by review. The
    envelope legitimately carries session_id and known_sessions, and neither was
    type-checked -- so a forged envelope with session_id={"api_key": ...} was
    copied into a LIVE no-session projection, leaking the payload. The same
    class as the run-history and authority leaks, inside the check written to
    close them."""
    base = _public_no_session(tmp_path)
    for label, override in (
            ("session_id_dict", {"session_id": {"api_key": _MARKER}}),
            ("session_id_list", {"session_id": [_MARKER]}),
            ("session_id_int", {"session_id": 7}),
            ("known_sessions_dict", {"known_sessions": [{"api_key": _MARKER}]}),
            ("known_sessions_int", {"known_sessions": [1, 2]}),
            ("known_sessions_scalar", {"known_sessions": _MARKER}),
    ):
        forged = dict(base)
        forged.update(override)
        enriched, state = project_active_session(forged, "m-runtime", _NOW)
        assert state is TruthState.UNAVAILABLE, label
        assert enriched["session_present"] is False, label
        assert _MARKER not in _json.dumps(enriched, default=str), label


def test_a_string_session_id_and_string_known_sessions_remain_valid(tmp_path):
    base = dict(_public_no_session(tmp_path))
    base.update({"session_id": "requested-but-absent",
                 "known_sessions": ["a", "b"]})
    _enriched, state = project_active_session(base, "m-runtime", _NOW)
    assert state is TruthState.LIVE


# ═══════════════════════════════════════════════════════════════════════════
# GUI-R PROJECTION CLOSURE
#
# Round-4 review found two structural boundary defects: the populated session
# was still `dict(session)` -- so the published schema was whatever the producer
# returned, and a TaskStage title object reached the dashboard -- and an
# authority record missing or nulling grants/forbidden_ops read as usable
# evidence because `.get()` erased the difference between missing, null and
# empty. Both are closed here as schema defects, not as two fields.
# ═══════════════════════════════════════════════════════════════════════════

_SESSION_MARKER = "sk-WCC-POPULATED-SESSION-MUST-NOT-RENDER-999"
_UNKNOWN_FIELD_MARKER = "sk-UNKNOWN-FIELD-MUST-NOT-RENDER"

#: Malformed carriers for the session sweep. Every one is the WRONG type for
#: whatever field it is injected into, so a legitimate value is never mistaken
#: for a leak.
_SESSION_CARRIERS = {
    "marker_dict": {"api_key": _SESSION_MARKER},
    "marker_list": [_SESSION_MARKER],
    "marker_nested": {"outer": {"Authorization": f"Bearer {_SESSION_MARKER}"}},
}

#: The contract, mirrored independently of the module so the test does not
#: simply agree with whatever the implementation happens to do.
_EXPECTED_SESSION_CONTRACT = {
    "session_id": ("str", False),
    "recorded_session_id": ("str", True),
    "identity_corrected": ("bool", False),
    "mission_id": ("str", False),
    "session_objective": ("str", False),
    "session_started_at": ("str", False),
    "starting_main_sha": ("str", False),
    "session_state": ("str", False),
    "current_task_id": ("str", True),
    "current_task_title": ("str", True),
    "current_stage": ("str", True),
    "tasks_attempted": ("int", False),
    "tasks_verified": ("int", False),
    "tasks_repaired": ("int", False),
    "tasks_escalated": ("int", False),
    "tasks_abstained": ("int", False),
    "tasks_incomplete": ("int", False),
    "blockers": ("list[str]", False),
    "known_sessions": ("list[str]", False),
    "authority": ("str", False),
    "c1_status": ("str", False),
    "auto_merge": ("bool", False),
    "production_mutation": ("bool", False),
    "capital_action": ("bool", False),
    "worker_heartbeat": ("str", False),
    "supervisor_latency_ms": ("str", False),
}


# ── the contract is executable, not prose ─────────────────────────────────
def test_the_session_contract_matches_the_implementation():
    """The previous audit failed because the inventory lived in prose and did
    not cover everything emitted. This pins it in code."""
    actual = {f.name: (f.kind, f.nullable) for f in rm._SESSION_FIELDS}
    assert actual == _EXPECTED_SESSION_CONTRACT
    assert set(rm.SESSION_PROJECTED_SOURCE_FIELDS) == set(_EXPECTED_SESSION_CONTRACT)


def test_emitted_session_keys_equal_validated_plus_module_keys():
    """THE structural guard. If someone writes `out["new"] = session["new"]`
    without adding it to the contract, this fails."""
    projected, _state = project_active_session(_session(), "m-runtime", _NOW)
    expected = set(rm.SESSION_PROJECTED_SOURCE_FIELDS) | set(rm.SESSION_MODULE_FIELDS)
    assert set(projected) == expected

    live = rm.build_dashboard(_REPO, now=_NOW)["active_session"]
    assert set(live) == expected


def test_the_real_repository_session_satisfies_the_contract():
    from tools.ns0c_session import session_projection
    rm._validated_session_fields(session_projection(repo_root=_REPO))   # must not raise


def test_the_active_session_projection_never_copies_the_producer_dict():
    """AST guard. The defect was `enriched = dict(session)`; equivalents are
    `session.copy()`, `{**session}` and `out.update(session)`. Scoped to the
    active-session path -- dict construction elsewhere is untouched."""
    src = (_REPO / "portfolio_automation" / "engineer_worker"
           / "ew0a_readmodels.py").read_text(encoding="utf-8")
    for node in _ast.walk(_ast.parse(src)):
        if not (isinstance(node, _ast.FunctionDef)
                and node.name == "project_active_session"):
            continue
        for inner in _ast.walk(node):
            if isinstance(inner, _ast.Call):
                func = inner.func
                if getattr(func, "id", None) == "dict":
                    for arg in inner.args:
                        assert getattr(arg, "id", None) != "session", "dict(session)"
                if getattr(func, "attr", None) == "copy":
                    assert getattr(func.value, "id", None) != "session", "session.copy()"
                if getattr(func, "attr", None) == "update":
                    for arg in inner.args:
                        assert getattr(arg, "id", None) != "session", "update(session)"
            if isinstance(inner, _ast.Dict):
                for key, value in zip(inner.keys, inner.values):
                    if key is None:
                        assert getattr(value, "id", None) != "session", "{**session}"
        break
    else:                                                # pragma: no cover
        raise AssertionError("project_active_session not found")


# ── schema closure against future producer expansion ─────────────────────
def test_an_uncontracted_producer_field_is_not_projected():
    """Load-bearing. A producer that later publishes debug_payload,
    raw_event or credential_context must not have it appear here by default. The
    projection does not reject the producer for publishing an extra key -- it
    simply does not expose uncontracted fields."""
    session = _session()
    session["future_unvalidated_field"] = {"api_key": _UNKNOWN_FIELD_MARKER}

    projected, state = project_active_session(session, "m-runtime", _NOW)
    assert state is TruthState.UNKNOWN                   # otherwise still valid
    assert projected["session_present"] is True
    assert "future_unvalidated_field" not in projected
    assert _UNKNOWN_FIELD_MARKER not in _json.dumps(projected, default=str)
    assert "api_key" not in _json.dumps(projected, default=str)


def test_an_uncontracted_field_is_not_projected_on_the_no_session_branch(tmp_path):
    """Closure applies to both branches, not only the populated one."""
    base = dict(_public_no_session(tmp_path))
    base["future_unvalidated_field"] = {"api_key": _UNKNOWN_FIELD_MARKER}
    projected, state = project_active_session(base, "m-runtime", _NOW)
    assert state is TruthState.LIVE
    assert "future_unvalidated_field" not in projected
    assert _UNKNOWN_FIELD_MARKER not in _json.dumps(projected, default=str)


def test_the_unknown_field_marker_never_reaches_the_dashboard(tmp_path):
    """Through the real producer: a TaskStage carrying an object title, which is
    exactly how review reproduced the leak."""
    docs = tmp_path / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "NORTHSTAR_0C_SESSION_probe.jsonl").write_text("\n".join(_json.dumps(e) for e in [
        {"kind": "SessionStarted", "session_id": "probe", "mission_id": "m-x",
         "session_objective": "obj", "session_started_at": "2026-09-01T00:00:00+00:00",
         "starting_main_sha": "abc"},
        {"kind": "TaskStage", "task_id": "T1", "title": {"api_key": _SESSION_MARKER},
         "stage": "VERIFYING"},
        {"kind": "CircuitBreaker", "breaker": {"api_key": _SESSION_MARKER}},
    ]) + "\n", encoding="utf-8")

    dash = rm.build_dashboard(tmp_path, now=_NOW)         # must not raise
    session = dash["active_session"]
    assert session["truth_state"] == TruthState.UNAVAILABLE.value
    assert session["session_present"] is False
    assert _SESSION_MARKER not in _json.dumps(dash, default=str)


# ── the populated-session matrix, generated from the contract ─────────────
def test_every_contracted_session_field_rejects_every_malformed_carrier():
    """Derived from the same inventory the validator uses, so the two cannot
    drift -- the failure mode of the previous prose audit."""
    checked = 0
    for field, (kind, _nullable) in _EXPECTED_SESSION_CONTRACT.items():
        for carrier_name, carrier in _SESSION_CARRIERS.items():
            if kind == "list[str]" and carrier_name == "marker_list":
                continue                                 # legitimate list[str]
            label = f"{field}__{carrier_name}"
            projected, state = project_active_session(
                _session(**{field: carrier}), "m-runtime", _NOW)
            assert state is TruthState.UNAVAILABLE, label
            assert projected["session_present"] is False, label
            assert projected["safe_to_present_as_current_work"] is False, label
            blob = _json.dumps(projected, default=str)
            assert _SESSION_MARKER not in blob, label
            assert "api_key" not in blob and "Authorization" not in blob, label
            checked += 1
    assert checked >= 70, f"matrix too small to be a sweep ({checked})"


def test_wrong_scalar_types_are_rejected_per_declared_kind():
    for field, (kind, _nullable) in _EXPECTED_SESSION_CONTRACT.items():
        wrong = {"str": 7, "int": "2", "bool": "true", "list[str]": "blocked"}[kind]
        _projected, state = project_active_session(
            _session(**{field: wrong}), "m-runtime", _NOW)
        assert state is TruthState.UNAVAILABLE, f"{field}={wrong!r}"


def test_required_session_fields_may_not_be_absent_or_null():
    for field, (_kind, nullable) in _EXPECTED_SESSION_CONTRACT.items():
        if nullable:
            continue
        absent = _session()
        del absent[field]
        _p, state = project_active_session(absent, "m-runtime", _NOW)
        assert state is TruthState.UNAVAILABLE, f"absent {field}"
        _p, state = project_active_session(
            _session(**{field: None}), "m-runtime", _NOW)
        assert state is TruthState.UNAVAILABLE, f"null {field}"


def test_nullable_session_fields_accept_none():
    for field, (_kind, nullable) in _EXPECTED_SESSION_CONTRACT.items():
        if not nullable:
            continue
        projected, state = project_active_session(
            _session(**{field: None}), "m-runtime", _NOW)
        assert state is TruthState.UNKNOWN, field
        assert projected[field] is None, field


def test_session_counters_reject_bool_and_negative_values():
    for field in ("tasks_attempted", "tasks_verified", "tasks_repaired",
                  "tasks_escalated", "tasks_abstained", "tasks_incomplete"):
        for bad in (True, False, -1, "1", 1.0):
            _p, state = project_active_session(
                _session(**{field: bad}), "m-runtime", _NOW)
            assert state is TruthState.UNAVAILABLE, f"{field}={bad!r}"
        projected, state = project_active_session(
            _session(**{field: 0}), "m-runtime", _NOW)
        assert state is TruthState.UNKNOWN and projected[field] == 0, field


def test_session_collections_validate_container_and_elements():
    for field in ("blockers", "known_sessions"):
        for bad in ("blocked", 1, {}, [1], [None], [{"api_key": _SESSION_MARKER}],
                    [["x"]], ["ok", 2]):
            _p, state = project_active_session(
                _session(**{field: bad}), "m-runtime", _NOW)
            assert state is TruthState.UNAVAILABLE, f"{field}={bad!r}"
        projected, state = project_active_session(
            _session(**{field: ["a", "b"]}), "m-runtime", _NOW)
        assert state is TruthState.UNKNOWN and projected[field] == ["a", "b"]


def test_an_unusable_populated_session_keeps_no_current_work_evidence():
    projected, state = project_active_session(
        _session(current_task_title={"api_key": _SESSION_MARKER}), "m-runtime", _NOW)
    assert state is TruthState.UNAVAILABLE
    for leaked in ("current_task_id", "current_task_title", "current_stage",
                   "blockers", "tasks_verified", "session_objective",
                   "starting_main_sha"):
        assert leaked not in projected, leaked


def test_session_validation_messages_carry_types_not_values():
    try:
        rm._validated_session_fields(
            _session(current_task_title={"api_key": _SESSION_MARKER}))
    except ValueError as exc:
        assert _SESSION_MARKER not in str(exc)
        assert "current_task_title" in str(exc) and "dict" in str(exc)
    else:                                                # pragma: no cover
        raise AssertionError("accepted a dict title")


def test_read_model_identity_is_module_owned_not_producer_copied():
    """A projection should not inherit its own identity from evidence."""
    projected, _state = project_active_session(
        _session(read_model="ATTACKER_CONTROLLED", schema_kind="spoofed"),
        "m-runtime", _NOW)
    assert projected["read_model"] == "Northstar0CSessionSummary"
    assert projected["schema_kind"] != "spoofed"


# ── authority record completeness ─────────────────────────────────────────
def _authority_root(tmp_path, record):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "ew0a_authority.json").write_text(
        _json.dumps(record), encoding="utf-8")
    return tmp_path


_LEVEL = "A1_ASSISTED_ENGINEERING"


def test_missing_and_null_authority_list_fields_are_invalid_evidence(tmp_path):
    """`record.get("grants", [])` erased the difference between missing, null
    and empty -- three states with three different meanings. set_authority_level
    always writes both fields, so a record lacking either is incomplete."""
    cases = {
        "missing_both": {"level": _LEVEL},
        "missing_grants": {"level": _LEVEL, "forbidden_ops": ["MERGE"]},
        "missing_fops": {"level": _LEVEL, "grants": ["g"]},
        "null_grants": {"level": _LEVEL, "grants": None, "forbidden_ops": ["MERGE"]},
        "null_fops": {"level": _LEVEL, "grants": ["g"], "forbidden_ops": None},
        "null_both": {"level": _LEVEL, "grants": None, "forbidden_ops": None},
        "nonlist_grants": {"level": _LEVEL, "grants": "g", "forbidden_ops": ["MERGE"]},
        "nonlist_fops": {"level": _LEVEL, "grants": ["g"], "forbidden_ops": "MERGE"},
        "bad_grant_elem": {"level": _LEVEL, "grants": [{"api_key": _SESSION_MARKER}],
                           "forbidden_ops": ["MERGE"]},
        "bad_fop_elem": {"level": _LEVEL, "grants": ["g"],
                         "forbidden_ops": [{"api_key": _SESSION_MARKER}]},
    }
    for label, record in cases.items():
        dash = rm.build_dashboard(_authority_root(tmp_path / label, record), now=_NOW)
        authority = dash["worker_authority"]
        caps = {c["capability"]: c["state"] for c in dash["backend_truth"]["capabilities"]}
        assert authority["record_evidence"] == TruthState.UNAVAILABLE.value, label
        assert caps["worker_authority"] == TruthState.UNAVAILABLE.value, label
        assert dash["backend_truth"]["readiness"] == "UNAVAILABLE", label
        assert _SESSION_MARKER not in _json.dumps(dash, default=str), label
        # permanent safety floor is never loosened by bad evidence
        assert not (authority["can_merge"] or authority["can_deploy"]
                    or authority["can_mutate_main"] or authority["can_write_production"]
                    or authority["can_self_promote"]), label
        for op in ("MERGE", "DEPLOY", "MAIN_WRITE", "PRODUCTION_WRITE", "SELF_PROMOTION"):
            assert op in authority["forbidden_ops"], (label, op)


def test_empty_lists_are_valid_list_shaped_authority_evidence(tmp_path):
    """[] is not the same as missing or null. A0 legitimately grants nothing."""
    dash = rm.build_dashboard(
        _authority_root(tmp_path, {"level": "A0_DIAGNOSTIC", "grants": [],
                                   "forbidden_ops": []}), now=_NOW)
    authority = dash["worker_authority"]
    assert authority["record_evidence"] == TruthState.LIVE.value
    assert authority["grants"] == []
    # the permanent boundary is still unioned in regardless
    for op in ("MERGE", "DEPLOY", "MAIN_WRITE"):
        assert op in authority["forbidden_ops"]
    assert authority["can_merge"] is False


def test_the_missing_null_empty_distinction_is_preserved_at_the_builder():
    permanent = build_worker_authority_summary(Lvl.A0_DIAGNOSTIC)     # both absent
    assert permanent.record_evidence == TruthState.UNAVAILABLE.value
    assert "absent" in permanent.record_detail
    nulled = build_worker_authority_summary(Lvl.A0_DIAGNOSTIC, grants=None,
                                            forbidden_ops=None)
    assert nulled.record_evidence == TruthState.UNAVAILABLE.value
    assert "null" in nulled.record_detail
    empty = build_worker_authority_summary(Lvl.A0_DIAGNOSTIC, grants=[],
                                           forbidden_ops=[])
    assert empty.record_evidence == TruthState.LIVE.value


def test_the_real_authority_record_is_still_complete_and_live():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    authority = dash["worker_authority"]
    assert authority["record_evidence"] == TruthState.LIVE.value
    assert len(authority["grants"]) == 8
    assert authority["can_merge"] is False
    assert dash["backend_truth"]["readiness"] == "PARTIAL"


# ── executable projection audit across all three certified paths ──────────
def test_all_producer_derived_emitted_fields_are_in_the_validated_contract():
    """Generated from the emitted keys, not from reading the source. A field
    that is emitted but absent from a validated contract fails here."""
    dash = rm.build_dashboard(_REPO, now=_NOW)

    # RunHistory
    run_keys = set(dash["run_history"]["runs"][0])
    assert run_keys - {"ledger_index"} == {f.name for f in rm._OUTCOME_FIELDS}

    # WorkerAuthority: source-derived keys only
    authority_source = {"level", "grants", "forbidden_ops"}
    module_owned = {"read_model", "schema_kind", "schema_version",
                    "capabilities_derived_from",
                    "record_evidence", "record_detail", "can_mutate_main",
                    "can_merge", "can_deploy", "can_write_production",
                    "can_self_promote"}
    assert set(dash["worker_authority"]) == authority_source | module_owned

    # ActiveSession, populated branch
    assert set(dash["active_session"]) == (
        set(rm.SESSION_PROJECTED_SOURCE_FIELDS) | set(rm.SESSION_MODULE_FIELDS))
