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


def test_absent_session_ledger_projects_pending_backend_not_a_fabrication(tmp_path):
    from portfolio_automation.engineer_worker.ew0a_readmodels import _build_active_session
    assert _build_active_session(tmp_path) == "PENDING_BACKEND"


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
# GUI-SR — BLOCKER C and the end-to-end corruption matrix
#
# The records reader was unsafe in both directions at once: a malformed JSON
# line was silently skipped (so an unreadable ledger produced a SHORTER list
# that consumers reported as measured history -- 0 PASS as though zero had been
# observed), and a valid non-object row was admitted (so the first row.get()
# downstream raised AttributeError and took the dashboard with it).
# ═══════════════════════════════════════════════════════════════════════════
import shutil as _gsr_shutil
from pathlib import Path as _GsrPath

_GSR_MARKER = "sk-GUI-SR-MUST-NOT-RENDER-999"
_GSR_NOW = "2026-09-06T21:30:00+00:00"
_GSR_REPO = _GsrPath(__file__).resolve().parents[1]
_GSR_LF = "\n"

#: Malformed carriers. Each holds the marker so a leak is detectable rather than
#: merely a type error.
_GSR_CARRIERS = {
    "dict": {"api_key": _GSR_MARKER},
    "nested": {"outer": {"Authorization": f"Bearer {_GSR_MARKER}"}},
    "list": [_GSR_MARKER],
}

_GSR_GOOD_ROW = {"kind": "ApprenticeshipComparison", "gpt_verdict": "PASS",
                 "recorded_at": "2026-08-15T20:45:22+00:00"}


def _gsr_root(tmp_path, *, authority=None, runtime=None, records=None):
    """A repo root with real protected config unless a fixture overrides it."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "docs").mkdir(exist_ok=True)
    for name, override in (("ew0a_authority.json", authority),
                           ("ew0a_runtime.json", runtime)):
        target = tmp_path / "config" / name
        if override is None:
            _gsr_shutil.copy(_GSR_REPO / "config" / name, target)
        else:
            target.write_text(override, encoding="utf-8")
    if records is not None:
        (tmp_path / "docs" / "EW0A_0B3_RECORDS.jsonl").write_text(
            records, encoding="utf-8")
    return tmp_path


# ── the records read result ───────────────────────────────────────────────
def test_gsr_records_reader_refuses_the_whole_ledger_on_any_invalid_row(tmp_path):
    good = json.dumps(_GSR_GOOD_ROW)
    cases = {
        "invalid_json_line": "{oops" + _GSR_LF,
        "scalar_row": "123" + _GSR_LF,
        "list_row": "[1,2]" + _GSR_LF,
        "string_row": '"text"' + _GSR_LF,
        "null_row": "null" + _GSR_LF,
        "bool_row": "true" + _GSR_LF,
        "mixed_valid_invalid": good + _GSR_LF + "[1,2]" + _GSR_LF + good + _GSR_LF,
        "trailing_invalid": good + _GSR_LF + "{oops" + _GSR_LF,
        "all_invalid": "[1,2]" + _GSR_LF + "7" + _GSR_LF,
    }
    for label, body in cases.items():
        root = _gsr_root(tmp_path / f"c_{label}", records=body)
        result = rm.read_controller_records(root)
        assert result.availability == "UNAVAILABLE", label
        assert result.records == [], label
        assert result.is_usable is False, label


def test_gsr_a_genuinely_empty_ledger_stays_distinguishable_from_a_corrupt_one(tmp_path):
    """This distinction is the point. Filtering bad rows would collapse them."""
    empty = rm.read_controller_records(_gsr_root(tmp_path / "empty", records=""))
    corrupt = rm.read_controller_records(_gsr_root(tmp_path / "corrupt", records="[1,2]" + _GSR_LF))
    assert empty.availability == "LIVE" and empty.records == []
    assert corrupt.availability == "UNAVAILABLE" and corrupt.records == []
    assert empty.availability != corrupt.availability


def test_gsr_an_absent_ledger_is_unavailable_not_pending_backend(tmp_path):
    """The controller writes these records, so absence is operational -- not
    evidence that nobody built the producer."""
    result = rm.read_controller_records(_gsr_root(tmp_path / "absent"))
    assert result.availability == "UNAVAILABLE"
    assert result.availability != PENDING_BACKEND
    assert "absent" in result.detail


def test_gsr_a_valid_ledger_is_read_in_full_and_in_order(tmp_path):
    rows = [dict(_GSR_GOOD_ROW, gpt_verdict=v) for v in ("PASS", "REPAIR", "PASS")]
    body = "".join(json.dumps(r) + _GSR_LF for r in rows)
    result = rm.read_controller_records(_gsr_root(tmp_path, records=body))
    assert result.availability == "LIVE"
    assert [r["gpt_verdict"] for r in result.records] == ["PASS", "REPAIR", "PASS"]


def test_gsr_records_detail_names_structure_not_payload(tmp_path):
    body = json.dumps(_GSR_CARRIERS["list"]) + _GSR_LF
    result = rm.read_controller_records(_gsr_root(tmp_path, records=body))
    assert _GSR_MARKER not in result.detail
    assert "line 0" in result.detail and "list" in result.detail


def test_gsr_legacy_accessor_still_returns_rows_but_cannot_signal_usability(tmp_path):
    """_read_records is retained for existing callers; its empty list is exactly
    why the explicit read result exists."""
    good = json.dumps(_GSR_GOOD_ROW) + _GSR_LF
    assert len(rm._read_records(_gsr_root(tmp_path / "ok", records=good))) == 1
    assert rm._read_records(_gsr_root(tmp_path / "bad", records="[1,2]" + _GSR_LF)) == []


# ── consumers must not manufacture measured zeros ────────────────────────
def test_gsr_unavailable_records_do_not_become_measured_zeros(tmp_path):
    """An operator reading 0 REPAIR from an unreadable ledger would conclude
    nothing had gone wrong. None says the question could not be answered."""
    root = _gsr_root(tmp_path, records="{oops" + _GSR_LF)
    dash = rm.build_dashboard(root, now=_GSR_NOW)

    supervisor = dash["supervisor"]
    assert supervisor["records_evidence"] == "UNAVAILABLE"
    for field in ("recent_pass", "recent_repair", "recent_escalate",
                  "recent_abstain", "recent_unavailable"):
        assert supervisor[field] is None, field
        assert supervisor[field] != 0, field
    assert supervisor["last_successful_verification"] is None

    apprenticeship = dash["apprenticeship"]
    assert apprenticeship["records_evidence"] == "UNAVAILABLE"
    for field in ("decisions_shadowed", "task_selection_agreements",
                  "risk_agreements", "routing_agreements", "missed_escalations",
                  "unsafe_underclassifications", "authority_expansion_proposals"):
        assert apprenticeship[field] is None, field

    assert dash["worker"]["records_evidence"] == "UNAVAILABLE"
    assert dash["controller_records"]["availability"] == "UNAVAILABLE"


def test_gsr_an_empty_ledger_does_produce_authoritative_zeros(tmp_path):
    """Zero IS the answer when the ledger is readable and empty."""
    dash = rm.build_dashboard(_gsr_root(tmp_path, records=""), now=_GSR_NOW)
    assert dash["controller_records"]["availability"] == "LIVE"
    assert dash["supervisor"]["recent_pass"] == 0
    assert dash["apprenticeship"]["decisions_shadowed"] == 0
    assert dash["supervisor"]["records_evidence"] == "LIVE"


def test_gsr_a_mixed_ledger_is_never_served_as_shortened_history(tmp_path):
    """[valid, corrupt, valid] must not become 2 records reported LIVE."""
    good = json.dumps(_GSR_GOOD_ROW)
    body = good + _GSR_LF + "[1,2]" + _GSR_LF + good + _GSR_LF
    dash = rm.build_dashboard(_gsr_root(tmp_path, records=body), now=_GSR_NOW)
    assert dash["controller_records"]["record_count"] == 0
    assert dash["controller_records"]["availability"] == "UNAVAILABLE"
    assert dash["supervisor"]["recent_pass"] is None
    assert dash["worker"]["recent_verification_outcomes"] == []
    assert dash["worker"]["records_evidence"] == "UNAVAILABLE"


def test_gsr_supervisor_state_capability_follows_the_ledger(tmp_path):
    caps = lambda d: {c["capability"]: c["state"]
                      for c in d["backend_truth"]["capabilities"]}
    corrupt = rm.build_dashboard(_gsr_root(tmp_path / "bad", records="[1,2]" + _GSR_LF),
                                 now=_GSR_NOW)
    assert caps(corrupt)["supervisor_state"] == "UNAVAILABLE"
    fresh = json.dumps(dict(_GSR_GOOD_ROW, recorded_at=_GSR_NOW)) + _GSR_LF
    live = rm.build_dashboard(_gsr_root(tmp_path / "fresh", records=fresh), now=_GSR_NOW)
    assert caps(live)["supervisor_state"] == "LIVE"


def test_gsr_consumers_default_to_live_for_existing_callers():
    """Signature compatibility: a plain list still means usable evidence."""
    records = [{"gpt_verdict": "PASS", "recorded_at": "t"}]
    assert build_supervisor_summary(records).recent_pass == 1
    assert build_apprenticeship_summary(records).decisions_shadowed == 0


# ── the end-to-end corruption matrix ─────────────────────────────────────
def _gsr_dashboard_is_honest(dash, label):
    blob = json.dumps(dash, default=str)
    assert _GSR_MARKER not in blob, f"{label}: marker leaked"
    assert "api_key" not in blob, f"{label}: api_key leaked"
    assert "Authorization" not in blob, f"{label}: Authorization leaked"
    caps = {c["capability"]: c["state"] for c in dash["backend_truth"]["capabilities"]}
    producerless = {"worker_activity", "queue_state", "component_health",
                    "controller_since"}
    pending = {k for k, v in caps.items() if v == PENDING_BACKEND}
    assert pending <= producerless, (
        f"{label}: PENDING_BACKEND misused for implemented producers: "
        f"{sorted(pending - producerless)}")


def test_gsr_authority_corruption_never_crashes_or_leaks(tmp_path):
    cases = {
        "root_null": "null", "root_list": "[1,2]", "root_int": "123",
        "root_string": '"text"', "root_bool": "true",
        "invalid_level": '{"level": "NOPE"}',
        "level_dict": json.dumps({"level": _GSR_CARRIERS["dict"]}),
        "grants_dict": json.dumps({"level": "A1_ASSISTED_ENGINEERING",
                                   "grants": _GSR_CARRIERS["dict"]}),
        "grants_marker_elem": json.dumps({"level": "A1_ASSISTED_ENGINEERING",
                                          "grants": [_GSR_CARRIERS["dict"]]}),
        "root_marker_dict": json.dumps(_GSR_CARRIERS["dict"]),
        "root_marker_list": json.dumps(_GSR_CARRIERS["list"]),
        "invalid_json": "{oops",
    }
    for label, body in cases.items():
        dash = rm.build_dashboard(_gsr_root(tmp_path / f"A_{label}", authority=body),
                                  now=_GSR_NOW)          # must not raise
        _gsr_dashboard_is_honest(dash, label)
        # fail-closed enforcement result is preserved and visible
        assert dash["worker_authority"]["level"] == "A0_DIAGNOSTIC" or \
            dash["worker_authority"]["level"] == "A1_ASSISTED_ENGINEERING", label
        assert dash["worker_authority"]["can_merge"] is False, label


def test_gsr_runtime_corruption_never_crashes_or_leaks(tmp_path):
    cases = {
        "root_null": "null", "root_list": "[1,2]", "root_int": "123",
        "root_string": '"text"', "no_mission": "{}", "invalid_json": "{oops",
        "bool_as_int": json.dumps({"mission_id": "m", "auto_merge": 1}),
        "str_as_bool": json.dumps({"mission_id": "m", "auto_merge": "true"}),
        "bool_as_count": json.dumps({"mission_id": "m", "max_concurrent_tasks": True}),
        "marker_field": json.dumps({"mission_id": _GSR_CARRIERS["dict"]}),
        "marker_nested": json.dumps({"mission_id": "m",
                                     "engineering_mode": _GSR_CARRIERS["nested"]}),
    }
    for label, body in cases.items():
        dash = rm.build_dashboard(_gsr_root(tmp_path / f"B_{label}", runtime=body),
                                  now=_GSR_NOW)          # must not raise
        _gsr_dashboard_is_honest(dash, label)
        caps = {c["capability"]: c["state"] for c in dash["backend_truth"]["capabilities"]}
        assert caps["controller_state"] == "UNAVAILABLE", label
        assert caps["mission_state"] == "UNAVAILABLE", label
        assert dash["backend_truth"]["readiness"] == "UNAVAILABLE", label


def test_gsr_records_corruption_never_crashes_or_leaks(tmp_path):
    cases = {
        "invalid_json": "{oops" + _GSR_LF,
        "scalar_row": "123" + _GSR_LF,
        "list_row": "[1,2]" + _GSR_LF,
        "marker_dict_row": json.dumps(_GSR_CARRIERS["dict"])[1:] + _GSR_LF,
        "marker_list_row": json.dumps(_GSR_CARRIERS["list"]) + _GSR_LF,
        "marker_nested_row": json.dumps(_GSR_CARRIERS["nested"])[1:] + _GSR_LF,
        "mixed": json.dumps(_GSR_GOOD_ROW) + _GSR_LF + "[1,2]" + _GSR_LF,
        "all_invalid": "[1,2]" + _GSR_LF + "7" + _GSR_LF,
    }
    for label, body in cases.items():
        dash = rm.build_dashboard(_gsr_root(tmp_path / f"C_{label}", records=body),
                                  now=_GSR_NOW)          # must not raise
        _gsr_dashboard_is_honest(dash, label)
        assert dash["controller_records"]["availability"] == "UNAVAILABLE", label
        assert dash["supervisor"]["recent_pass"] is None, label


def test_gsr_a_valid_row_carrying_a_marker_is_legitimate_evidence(tmp_path):
    """Discipline check: the sweep must test MALFORMED carriers, not punish a
    schema-valid row whose string content happens to contain the marker."""
    row = json.dumps({"kind": "ApprenticeshipComparison", "notes": _GSR_MARKER})
    result = rm.read_controller_records(_gsr_root(tmp_path, records=row + _GSR_LF))
    assert result.availability == "LIVE"
    assert result.records[0]["notes"] == _GSR_MARKER


def test_gsr_the_real_repository_is_unchanged_by_the_hardening():
    dash = rm.build_dashboard(_GSR_REPO, now=_GSR_NOW)
    assert dash["controller_records"]["availability"] == "LIVE"
    assert dash["supervisor"]["recent_pass"] == 8
    assert dash["apprenticeship"]["decisions_shadowed"] == 5
    assert dash["worker_authority"]["level"] == "A1_ASSISTED_ENGINEERING"
    assert dash["backend_truth"]["readiness"] == "PARTIAL"
    assert dash["supervisor"]["records_evidence"] == "LIVE"


# ═══════════════════════════════════════════════════════════════════════════
# GUI-SR CONTRACT CLOSURE
#
# The source-reader architecture changed the published read-model contract in
# four observable ways -- nullable counts, three records_evidence fields and a
# new top-level surface -- and the first candidate shipped them while still
# declaring engineering.readmodel.v0 and leaving the interface document
# describing the old shape. A schema version exists precisely to let a consumer
# tell those two contracts apart.
# ═══════════════════════════════════════════════════════════════════════════
_V1 = "engineering.readmodel.v1"
_ALT_LEDGER = "alternate/x.jsonl"


def _gsr_ledger_root(tmp_path, rel, body=None, raw=None):
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        target.write_bytes(raw)
    elif body is not None:
        target.write_text(body, encoding="utf-8")
    return tmp_path


# ── the declared version ──────────────────────────────────────────────────
def test_gsr_read_model_declares_schema_v1():
    """Not v0. The contract changed incompatibly for any consumer that assumed
    the count fields were always integers."""
    assert rm.READMODEL_SCHEMA_VERSION == _V1


def test_gsr_every_projection_carries_v1():
    dash = rm.build_dashboard(_GSR_REPO, now=_GSR_NOW)
    assert dash["schema_version"] == _V1
    for key in ("controller", "supervisor", "worker", "worker_authority",
                "mission", "apprenticeship", "system_health", "controller_records"):
        assert dash[key]["schema_version"] == _V1, key


def test_gsr_the_interface_document_declares_the_same_version():
    """A doc that still describes v0 leaves a consumer unable to distinguish an
    intentional nullable contract from malformed output -- which is exactly the
    finding this commit closes."""
    doc = (_GSR_REPO / "docs" / "WORKER_CONTROL_CENTER_INTERFACE.md").read_text(
        encoding="utf-8")
    assert _V1 in doc
    assert "engineering.readmodel.v0" not in doc
    for documented in ("controller_records", "records_evidence"):
        assert documented in doc, documented


def test_gsr_the_learning_contract_was_not_swept_along():
    """A different contract with its own version. Bumping it because a sibling
    changed would be exactly the global rewrite the mission forbade."""
    from portfolio_automation.engineer_worker.learning.readmodels import (
        LEARNING_READMODEL_SCHEMA_VERSION)
    assert LEARNING_READMODEL_SCHEMA_VERSION == "engineering.learning_readmodel.v0"


# ── the controller_records surface ───────────────────────────────────────
def test_gsr_controller_records_emits_its_declared_key_set():
    result = rm.read_controller_records(_GSR_REPO).to_dict()
    assert set(result) == {"schema_version", "schema_kind", "read_model",
                           "availability", "record_count", "source", "detail"}
    assert result["read_model"] == "ControllerRecordsRead"
    assert result["availability"] == "LIVE"
    assert isinstance(result["record_count"], int)


def test_gsr_live_empty_and_unavailable_empty_are_different_answers(tmp_path):
    """LIVE + 0 is an authoritative empty history. UNAVAILABLE + 0 does NOT mean
    zero events occurred."""
    empty = rm.read_controller_records(
        _gsr_root(tmp_path / "empty", records="")).to_dict()
    unusable = rm.read_controller_records(
        _gsr_root(tmp_path / "bad", records="[1,2]" + _GSR_LF)).to_dict()
    assert empty["availability"] == "LIVE" and empty["record_count"] == 0
    assert unusable["availability"] == "UNAVAILABLE" and unusable["record_count"] == 0
    assert empty["availability"] != unusable["availability"]


# ── instance-owned provenance ────────────────────────────────────────────
def test_gsr_the_selected_ledger_is_carried_through_every_branch(tmp_path):
    """to_dict() hardcoded the module default, so an alternate read produced one
    evidence object whose detail named the file read and whose source named a
    different file. An evidence result that contradicts itself is worse than one
    that is merely incomplete."""
    branches = {
        "valid": dict(body=json.dumps({"kind": "X"}) + _GSR_LF),
        "empty": dict(body=""),
        "invalid_json": dict(body="{oops" + _GSR_LF),
        "non_object_row": dict(body="[1,2]" + _GSR_LF),
        "decode_failure": dict(raw=b"\xff\xfebad"),
    }
    for label, kwargs in branches.items():
        root = _gsr_ledger_root(tmp_path / f"prov_{label}", _ALT_LEDGER, **kwargs)
        result = rm.read_controller_records(root, _ALT_LEDGER)
        assert result.source == _ALT_LEDGER, label
        assert result.to_dict()["source"] == _ALT_LEDGER, label
        assert _ALT_LEDGER in result.detail, label
        # never the module default when an alternate was selected
        assert result.to_dict()["source"] != rm.CONTROLLER_RECORDS_REL, label

    # an absent alternate ledger keeps its provenance too: a failed read does
    # not lose the identity of what was attempted
    absent = rm.read_controller_records(tmp_path / "nothing", _ALT_LEDGER)
    assert absent.availability == "UNAVAILABLE"
    assert absent.source == _ALT_LEDGER
    assert absent.to_dict()["source"] == _ALT_LEDGER
    assert _ALT_LEDGER in absent.detail


def test_gsr_detail_and_source_never_disagree(tmp_path):
    """No result may say source=default while detail names an alternate."""
    for rel in (rm.CONTROLLER_RECORDS_REL, _ALT_LEDGER, "docs/other.jsonl"):
        root = _gsr_ledger_root(tmp_path / f"agree_{rel.replace('/', '_')}", rel,
                                body=json.dumps({"kind": "X"}) + _GSR_LF)
        result = rm.read_controller_records(root, rel).to_dict()
        assert result["source"] == rel
        assert rel in result["detail"]


def test_gsr_the_default_caller_still_reports_the_default_ledger():
    result = rm.read_controller_records(_GSR_REPO).to_dict()
    assert result["source"] == "docs/EW0A_0B3_RECORDS.jsonl"
    assert rm.build_dashboard(_GSR_REPO, now=_GSR_NOW)["controller_records"]["source"] \
        == "docs/EW0A_0B3_RECORDS.jsonl"


def test_gsr_provenance_is_carried_not_canonicalised(tmp_path):
    """Exactly the identifier the reader was given -- not resolved, absolutised
    or normalised into something else."""
    odd = "docs/./nested/../weird-name.jsonl"
    root = _gsr_ledger_root(tmp_path, odd, body="")
    assert rm.read_controller_records(root, odd).source == odd


# ── the nullable-count contract, end to end ──────────────────────────────
def test_gsr_v1_unavailable_ledger_nulls_every_measured_count(tmp_path):
    dash = rm.build_dashboard(_gsr_root(tmp_path, records="{oops" + _GSR_LF),
                              now=_GSR_NOW)
    supervisor = dash["supervisor"]
    assert supervisor["records_evidence"] == "UNAVAILABLE"
    for field in ("recent_pass", "recent_repair", "recent_escalate",
                  "recent_abstain", "recent_unavailable"):
        assert supervisor[field] is None, field
    assert supervisor["last_successful_verification"] is None
    # independent from the supervisor's OWN operational availability
    assert supervisor["availability"] == PENDING_BACKEND

    apprenticeship = dash["apprenticeship"]
    assert apprenticeship["records_evidence"] == "UNAVAILABLE"
    for field in ("decisions_shadowed", "task_selection_agreements",
                  "risk_agreements", "routing_agreements", "missed_escalations",
                  "unsafe_underclassifications", "authority_expansion_proposals"):
        assert apprenticeship[field] is None, field

    assert dash["worker"]["records_evidence"] == "UNAVAILABLE"
    assert dash["worker"]["recent_verification_outcomes"] == []


def test_gsr_v1_usable_empty_ledger_yields_authoritative_zeros(tmp_path):
    dash = rm.build_dashboard(_gsr_root(tmp_path, records=""), now=_GSR_NOW)
    assert dash["controller_records"]["availability"] == "LIVE"
    supervisor = dash["supervisor"]
    assert supervisor["records_evidence"] == "LIVE"
    for field in ("recent_pass", "recent_repair", "recent_escalate",
                  "recent_abstain", "recent_unavailable"):
        assert supervisor[field] == 0, field
    apprenticeship = dash["apprenticeship"]
    assert apprenticeship["records_evidence"] == "LIVE"
    assert apprenticeship["decisions_shadowed"] == 0
    assert apprenticeship["unsafe_underclassifications"] == 0


def test_gsr_zero_and_null_are_never_interchanged(tmp_path):
    """The load-bearing distinction, stated as one assertion."""
    usable = rm.build_dashboard(_gsr_root(tmp_path / "z", records=""), now=_GSR_NOW)
    unusable = rm.build_dashboard(_gsr_root(tmp_path / "n", records="[1,2]" + _GSR_LF),
                                  now=_GSR_NOW)
    assert usable["supervisor"]["recent_repair"] == 0
    assert unusable["supervisor"]["recent_repair"] is None
    assert usable["apprenticeship"]["unsafe_underclassifications"] == 0
    assert unusable["apprenticeship"]["unsafe_underclassifications"] is None


def test_gsr_records_evidence_accompanies_every_dependent_summary():
    dash = rm.build_dashboard(_GSR_REPO, now=_GSR_NOW)
    for surface in ("supervisor", "worker", "apprenticeship"):
        assert "records_evidence" in dash[surface], surface
        assert dash[surface]["records_evidence"] == "LIVE", surface
