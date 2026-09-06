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


def _session(mission="m-runtime", session_id="s1", **extra):
    """Minimal session projection shape. Deliberately mirrors what
    tools.ns0c_session.session_projection actually publishes -- notably it has
    session_started_at and NO last-activity timestamp."""
    base = {"read_model": "Northstar0CSessionSummary", "session_id": session_id,
            "mission_id": mission, "session_state": "RUNNING",
            "session_started_at": "2026-08-16T07:17:41+00:00",
            "current_task_id": "t1"}
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


def test_active_session_preserves_every_original_projection_key():
    """Enrichment is additive. The session's own evidence must survive intact."""
    original = _session(mission="m-other", extra_key="kept")
    enriched, _ = project_active_session(original, "m-runtime", _NOW)
    for key, value in original.items():
        assert enriched[key] == value


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
        _json.dumps({"task_id": "T1", "final_status": "VERIFIED"}) + "\n"
        + _json.dumps({"task_id": "T2", "final_status": "VERIFIED",
                       "mission_id": "m-recorded"}) + "\n", encoding="utf-8")
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


def _row(**extra):
    base = {"task_id": "T1", "final_status": "VERIFIED"}
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
