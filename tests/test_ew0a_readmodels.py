"""Worker Control Center read-model projection tests.

Proves the projections are correct, honest (no fabricated/smoothed state;
PENDING_BACKEND where no backend), carry no secrets, and are READ-ONLY by
construction (the module references no mutation function).
"""
from __future__ import annotations

import inspect
import json
import os
import sys

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

#: Capabilities for which this repository genuinely contains no producer, AFTER
#: integration. GUI-SR's matrix knew four; GUI-R's projections added
#: attention_derivation (no attention producer exists) and controller_identity
#: (no ControllerStateV0 exists). Neither is an implemented-but-broken producer,
#: so PENDING_BACKEND remains correct for both.
_INTEGRATED_PRODUCERLESS = {"worker_activity", "queue_state", "component_health",
                            "controller_since", "attention_derivation",
                            "controller_identity"}


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
    # The integrated producer-less set. GUI-SR knew four; GUI-R added
    # attention_derivation and controller_identity, both genuinely unbuilt.
    pending = {k for k, v in caps.items() if v == PENDING_BACKEND}
    assert pending <= _INTEGRATED_PRODUCERLESS, (
        f"{label}: PENDING_BACKEND misused for implemented producers: "
        f"{sorted(pending - _INTEGRATED_PRODUCERLESS)}")


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


# ═══════════════════════════════════════════════════════════════════════════
# GUI-SR — THE WCC CONSUMED-FIELD CONTRACT
#
# read_controller_records established "valid JSON + row is a dict" and then
# declared the ledger LIVE, while the projections read individual FIELDS out of
# those rows. Validating the container and not the contents is the same mistake
# this repository has now recorded in two separate missions, so the coupling
# between what the consumers read and what the contract declares is executable
# here rather than hand-maintained.
# ═══════════════════════════════════════════════════════════════════════════
_CR_MARKER = "sk-CONTROLLER-RECORD-MUST-NOT-RENDER-999"

#: Functions that consume ControllerRecordsRead.records.
_CR_CONSUMERS = ("build_supervisor_summary", "build_apprenticeship_summary",
                 "_assess_backend_truth", "_recent_verification_outcomes",
                 "build_dashboard")

#: Local names inside those functions that do NOT hold a controller record, each
#: justified. The default is that an unrecognised base IS a record, so a new
#: consumer using a new variable name is caught rather than skipped. Asserted
#: exactly, so widening this is a deliberate, visible act.
_CR_NON_RECORD_BASES = {
    "authority_record": ("the authority JSON object read in build_dashboard; named "
                         "`record` before GUI-R's _MISSING sentinel extraction "
                         "renamed it"),
    "dashboard": "this module's own output dict",
}

_CR_CARRIERS = {
    "dict": {"api_key": _CR_MARKER},
    "nested": {"outer": {"Authorization": f"Bearer {_CR_MARKER}"}},
    "list": [_CR_MARKER],
}

_CR_GOOD = {"kind": "ApprenticeshipComparison", "gpt_verdict": "PASS",
            "recorded_at": "2026-08-15T20:45:22+00:00", "risk_agreement": True}

_CR_BOOL_FIELDS = ("engineer_proposed_task_relates_to_experimentspec",
                   "risk_agreement", "routing_agreement",
                   "danger_underclassified_architecture_as_engineer")


def _cr_root(tmp_path, rows):
    return _gsr_root(tmp_path, records="".join(
        json.dumps(r) + _GSR_LF for r in rows))


def _cr_consumer_field_reads():
    """Every constant field name the consumers read off a record-bearing local.

    Scoped AST over five named functions -- not a repo-wide framework."""
    src = (_GSR_REPO / "portfolio_automation" / "engineer_worker"
           / "ew0a_readmodels.py").read_text(encoding="utf-8")
    found = {}
    for node in _ast.walk(_ast.parse(src)):
        if not (isinstance(node, _ast.FunctionDef) and node.name in _CR_CONSUMERS):
            continue
        for inner in _ast.walk(node):
            base = field = None
            if (isinstance(inner, _ast.Call)
                    and getattr(inner.func, "attr", None) == "get"
                    and inner.args
                    and isinstance(inner.args[0], _ast.Constant)
                    and isinstance(inner.args[0].value, str)):
                base = getattr(inner.func.value, "id", None)
                field = inner.args[0].value
            elif (isinstance(inner, _ast.Subscript)
                  and isinstance(inner.slice, _ast.Constant)
                  and isinstance(inner.slice.value, str)):
                base = getattr(inner.value, "id", None)
                field = inner.slice.value
            if field is None or base is None:
                continue
            if base in _CR_NON_RECORD_BASES:
                continue
            found.setdefault(field, set()).add(node.name)
    return found


# ── the mechanical coupling ───────────────────────────────────────────────
def test_cr_every_field_the_consumers_read_is_in_the_contract():
    """THE control. A new consumer starting to read row.get("new_field") without
    extending the contract fails here -- which is exactly what a hand-maintained
    declaration failed to do twice in GUI-R."""
    read = _cr_consumer_field_reads()
    assert read, "the AST scope found no record field reads at all"
    unregistered = set(read) - rm.WCC_CONSUMED_RECORD_FIELD_NAMES
    assert unregistered == set(), (
        f"consumed but not in the WCC field contract: {sorted(unregistered)}")


def test_cr_the_contract_has_no_dead_entries():
    """Honest in both directions: a declared field nobody reads is stale."""
    read = set(_cr_consumer_field_reads())
    unused = rm.WCC_CONSUMED_RECORD_FIELD_NAMES - read
    assert unused == set(), f"declared but never consumed: {sorted(unused)}"


def test_cr_the_non_record_exclusion_list_is_exactly_justified():
    """The default is 'unknown base is a record'. Widening the exclusions must
    be deliberate, so the set is pinned."""
    assert set(_CR_NON_RECORD_BASES) == {"authority_record", "dashboard"}
    for base, reason in _CR_NON_RECORD_BASES.items():
        assert reason.strip(), base


def test_cr_declared_consumers_match_the_ast_scope():
    """Each contract entry names its consuming surfaces; those must be the
    functions the AST actually found reading it."""
    read = _cr_consumer_field_reads()
    for spec in rm.WCC_CONSUMED_RECORD_FIELDS:
        assert spec.consumers, spec.name
        assert set(spec.consumers) <= set(read.get(spec.name, set())) | {
            "_recent_verification_outcomes"}, spec.name


# ── no coercion of record evidence ────────────────────────────────────────
def test_cr_record_consumers_never_coerce_evidence():
    """Scoped to the record-consuming functions, not repo-wide. If the schema
    validation is ever removed, a coercion fallback must not silently restore
    arbitrary-object serialization."""
    src = (_GSR_REPO / "portfolio_automation" / "engineer_worker"
           / "ew0a_readmodels.py").read_text(encoding="utf-8")
    guarded = {"build_supervisor_summary", "build_apprenticeship_summary",
               "_assess_backend_truth", "_recent_verification_outcomes"}
    for node in _ast.walk(_ast.parse(src)):
        if not (isinstance(node, _ast.FunctionDef) and node.name in guarded):
            continue
        for inner in _ast.walk(node):
            if isinstance(inner, _ast.Call):
                name = getattr(inner.func, "id", None) or getattr(inner.func, "attr", None)
                assert name not in ("str", "repr", "format"), (
                    f"{node.name} coerces evidence via {name}()")


# ── the real ledger must still be usable ─────────────────────────────────
def test_cr_the_tracked_ledger_satisfies_the_consumption_contract():
    """A validator that condemns the true history is not a validator."""
    result = rm.read_controller_records(_GSR_REPO)
    assert result.availability == "LIVE"
    assert len(result.records) == 24
    for index, row in enumerate(result.records):
        assert rm._record_field_violation(row) is None, (index, row.get("kind"))


def test_cr_presence_semantics_match_the_tracked_ledger():
    """Presence was derived from evidence, not assumed. Only `kind` is on every
    record; making the rest required would condemn legitimate history."""
    rows = rm.read_controller_records(_GSR_REPO).records
    required = {f.name for f in rm.WCC_CONSUMED_RECORD_FIELDS if f.required}
    assert required == {"kind"}
    for name in required:
        assert all(name in r for r in rows), name
    # and the optional ones genuinely are absent on some legitimate records
    for name in ("gpt_verdict", "recorded_at", "risk_agreement"):
        assert any(name not in r for r in rows), name


# ── the two reported leaks ───────────────────────────────────────────────
def test_cr_a_wrong_typed_gpt_verdict_makes_the_whole_ledger_unavailable(tmp_path):
    for label, carrier in _CR_CARRIERS.items():
        dash = rm.build_dashboard(
            _cr_root(tmp_path / f"gv_{label}", [dict(_CR_GOOD, gpt_verdict=carrier)]),
            now=_GSR_NOW)
        assert dash["controller_records"]["availability"] == "UNAVAILABLE", label
        assert dash["controller_records"]["record_count"] == 0, label
        assert dash["worker"]["recent_verification_outcomes"] == [], label
        assert dash["supervisor"]["recent_pass"] is None, label
        blob = json.dumps(dash, default=str)
        assert _CR_MARKER not in blob, label
        assert "api_key" not in blob and "Authorization" not in blob, label


def test_cr_a_wrong_typed_recorded_at_never_reaches_last_successful(tmp_path):
    """The exact reported case: a dict recorded_at on a PASS row was copied
    directly into supervisor.last_successful_verification, whose declared
    contract is str | None."""
    for label, carrier in _CR_CARRIERS.items():
        dash = rm.build_dashboard(
            _cr_root(tmp_path / f"ra_{label}",
                     [dict(_CR_GOOD, gpt_verdict="PASS", recorded_at=carrier)]),
            now=_GSR_NOW)
        assert dash["controller_records"]["availability"] == "UNAVAILABLE", label
        assert dash["supervisor"]["last_successful_verification"] is None, label
        assert dash["supervisor"]["records_evidence"] == "UNAVAILABLE", label
        assert _CR_MARKER not in json.dumps(dash, default=str), label


def test_cr_apprenticeship_booleans_reject_every_falsey_lookalike(tmp_path):
    """Wrong type is unavailable evidence, not False. 0 and "false" are the
    dangerous ones: they would have become clean negative evidence on a field
    named unsafe_underclassifications."""
    for field in _CR_BOOL_FIELDS:
        for bad in (0, 1, "true", "false", [], {}, _CR_CARRIERS["dict"],
                    _CR_CARRIERS["list"]):
            label = f"{field}_{type(bad).__name__}_{bad!r}"[:48]
            dash = rm.build_dashboard(
                _cr_root(tmp_path / label.replace("/", "_"),
                         [dict(_CR_GOOD, **{field: bad})]), now=_GSR_NOW)
            assert dash["controller_records"]["availability"] == "UNAVAILABLE", label
            assert dash["apprenticeship"]["unsafe_underclassifications"] is None, label
            assert dash["apprenticeship"]["risk_agreements"] is None, label
            assert _CR_MARKER not in json.dumps(dash, default=str), label


def test_cr_kind_must_be_a_non_empty_string(tmp_path):
    for label, bad in (("absent", None), ("null", None), ("empty", ""),
                       ("int", 7), ("dict", _CR_CARRIERS["dict"])):
        row = dict(_CR_GOOD)
        if label == "absent":
            del row["kind"]
        else:
            row["kind"] = bad
        dash = rm.build_dashboard(_cr_root(tmp_path / f"k_{label}", [row]),
                                  now=_GSR_NOW)
        assert dash["controller_records"]["availability"] == "UNAVAILABLE", label
        assert _CR_MARKER not in json.dumps(dash, default=str), label


# ── whole-ledger policy and the zero/unavailable distinction ──────────────
def test_cr_one_bad_field_invalidates_the_whole_ledger(tmp_path):
    """valid, valid, malformed, valid must NOT become 3 usable rows."""
    rows = [_CR_GOOD, _CR_GOOD, dict(_CR_GOOD, gpt_verdict=_CR_CARRIERS["dict"]),
            _CR_GOOD]
    dash = rm.build_dashboard(_cr_root(tmp_path, rows), now=_GSR_NOW)
    assert dash["controller_records"]["availability"] == "UNAVAILABLE"
    assert dash["controller_records"]["record_count"] == 0
    assert dash["supervisor"]["recent_pass"] is None
    assert dash["apprenticeship"]["decisions_shadowed"] is None


def test_cr_a_genuinely_empty_ledger_is_still_an_authoritative_zero(tmp_path):
    dash = rm.build_dashboard(_gsr_root(tmp_path, records=""), now=_GSR_NOW)
    assert dash["controller_records"]["availability"] == "LIVE"
    assert dash["supervisor"]["recent_pass"] == 0
    assert dash["apprenticeship"]["unsafe_underclassifications"] == 0


def test_cr_records_missing_optional_fields_remain_usable(tmp_path):
    """The ledger is heterogeneous: most kinds carry neither gpt_verdict nor the
    apprenticeship booleans."""
    rows = [{"kind": "AuthoritativeControllerDecision"},
            {"kind": "ExperimentResultOutcome",
             "recorded_at": "2026-08-15T20:45:22+00:00"}]
    dash = rm.build_dashboard(_cr_root(tmp_path, rows), now=_GSR_NOW)
    assert dash["controller_records"]["availability"] == "LIVE"
    assert dash["controller_records"]["record_count"] == 2
    assert dash["supervisor"]["recent_pass"] == 0


def test_cr_explicit_null_on_an_optional_field_is_treated_as_absence(tmp_path):
    rows = [{"kind": "ExperimentResultOutcome", "gpt_verdict": None,
             "recorded_at": None, "risk_agreement": None}]
    dash = rm.build_dashboard(_cr_root(tmp_path, rows), now=_GSR_NOW)
    assert dash["controller_records"]["availability"] == "LIVE"
    assert dash["supervisor"]["recent_pass"] == 0


# ── unconsumed fields stay opaque, and are not a rejection reason ────────
def test_cr_an_unconsumed_field_is_neither_rejected_nor_projected(tmp_path):
    """Validate what the WCC consumes; do not certify or expose what it does
    not. If another subsystem consumes it later, its contract adds it."""
    opaque = "sk-UNCONSUMED-OPAQUE-999"
    rows = [dict(_CR_GOOD, future_unconsumed_field={"api_key": opaque})]
    dash = rm.build_dashboard(_cr_root(tmp_path, rows), now=_GSR_NOW)
    assert dash["controller_records"]["availability"] == "LIVE"
    assert dash["controller_records"]["record_count"] == 1
    blob = json.dumps(dash, default=str)
    assert "future_unconsumed_field" not in blob
    assert opaque not in blob


# ── shape, not content ───────────────────────────────────────────────────
def test_cr_a_schema_valid_string_containing_the_marker_is_still_evidence(tmp_path):
    """No substring sanitisation. The control is rejecting wrong-SHAPED
    evidence, not deleting strings containing particular text."""
    rows = [dict(_CR_GOOD, gpt_verdict=_CR_MARKER)]
    result = rm.read_controller_records(_cr_root(tmp_path, rows))
    assert result.availability == "LIVE"
    assert result.records[0]["gpt_verdict"] == _CR_MARKER
    dash = rm.build_dashboard(_cr_root(tmp_path, rows), now=_GSR_NOW)
    assert dash["worker"]["recent_verification_outcomes"] == [_CR_MARKER]


def test_cr_violation_detail_names_the_field_and_type_not_the_value():
    violation = rm._record_field_violation(
        dict(_CR_GOOD, gpt_verdict=_CR_CARRIERS["dict"]))
    assert violation is not None
    assert "gpt_verdict" in violation and "dict" in violation
    assert _CR_MARKER not in violation


def test_cr_live_still_means_what_the_document_says():
    """LIVE is now a two-part claim, and the doc must say so rather than letting
    it read as full certification of every historical record kind."""
    doc = (_GSR_REPO / "docs" / "WORKER_CONTROL_CENTER_INTERFACE.md").read_text(
        encoding="utf-8")
    assert "WCC consumption contract" in doc or "consumed by the WCC" in doc
    assert "canonically certified" in doc

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
    OUTCOME_LEDGER_REL, CONTROLLER_RECORDS_REL, build_attention_coverage,
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
def test_learning_projection_is_quarantined_and_capability_classified():
    """Was: learning is LIVE. Round-5 review showed the payload was admitted on a
    shape check alone and then emitted wholesale, so a lesson whose `principle`
    is an object reached the dashboard as trusted evidence. The producer exists,
    so this is UNAVAILABLE and never PENDING_BACKEND."""
    dash = rm.build_dashboard(_REPO, now=_NOW)
    assert dash["learning"]["truth_state"] == TruthState.UNAVAILABLE.value
    assert dash["learning"]["truth_state"] != PENDING_BACKEND
    caps = {c["capability"]: c for c in dash["backend_truth"]["capabilities"]}
    assert caps["learning"]["state"] == TruthState.UNAVAILABLE.value
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
    assert supervisor["source"] == CONTROLLER_RECORDS_REL
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
        Lvl.A0_DIAGNOSTIC, grants=[], forbidden_ops=["CUSTOM_DENIED_OP"],
        raw_level="A0_DIAGNOSTIC")
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


def test_a_working_learning_producer_is_still_withheld_not_live():
    """The producer works. It is withheld anyway, because its nested payload
    contract is uncertified -- that is the architectural decision, not a bug."""
    projection, state = rm._project_learning(_REPO, "engineer.local_qwen2_5_7b", _NOW)
    assert state is TruthState.UNAVAILABLE
    assert projection["truth_state"] == TruthState.UNAVAILABLE.value
    assert "recent_lessons" not in projection


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
    """The fixture now carries the CANONICAL A1 grant set.

    It previously passed `["approved E1/E2"]` -- a plausible-looking list the
    trusted writer would never emit -- and passed only because the projection
    did not yet check what the grant strings mean."""
    canonical = list(rm.grants_for_level(Lvl.A1_ASSISTED_ENGINEERING))
    summary = build_worker_authority_summary(
        Lvl.A1_ASSISTED_ENGINEERING, grants=canonical,
        forbidden_ops=["MERGE", "DEPLOY"], raw_level="A1_ASSISTED_ENGINEERING")
    assert summary.record_evidence == TruthState.LIVE.value
    assert summary.grants == canonical
    assert build_worker_authority_summary(
        Lvl.A0_DIAGNOSTIC, grants=[], forbidden_ops=[],
        raw_level="A0_DIAGNOSTIC").record_evidence == TruthState.LIVE.value


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
    # a valid raw_level is supplied so the level check passes and this test
    # still exercises the grants/forbidden_ops distinction it is about
    permanent = build_worker_authority_summary(
        Lvl.A0_DIAGNOSTIC, raw_level="A0_DIAGNOSTIC")               # both absent
    assert permanent.record_evidence == TruthState.UNAVAILABLE.value
    assert "grants" in permanent.record_detail and "absent" in permanent.record_detail
    nulled = build_worker_authority_summary(Lvl.A0_DIAGNOSTIC, grants=None,
                                            forbidden_ops=None,
                                            raw_level="A0_DIAGNOSTIC")
    assert nulled.record_evidence == TruthState.UNAVAILABLE.value
    assert "grants" in nulled.record_detail and "null" in nulled.record_detail
    empty = build_worker_authority_summary(Lvl.A0_DIAGNOSTIC, grants=[],
                                           forbidden_ops=[],
                                           raw_level="A0_DIAGNOSTIC")
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


# ═══════════════════════════════════════════════════════════════════════════
# GUI-R FINAL CONTAINMENT AND DASHBOARD PROJECTION INVENTORY
#
# Round 5 found the P2 in `learning` -- a FOURTH projection, never enumerated,
# including in my own audit tables twice. Architectural review decided against
# certifying learning inside this PR: its four nested projections derive from
# stored lessons, competence, retrieval and evaluation records, which is its own
# bounded mission. So the payload is quarantined, and the set of projection
# paths stops living in human memory.
# ═══════════════════════════════════════════════════════════════════════════

_LEAK_MARKER = "LEAK_MARKER_sk_999"


def _learning_root(tmp_path, field, carrier):
    """A repo root whose lesson log carries a schema-invalid nested field."""
    import shutil
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    for name in ("ew0a_authority.json", "ew0a_runtime.json"):
        shutil.copy(_REPO / "config" / name, tmp_path / "config" / name)
    rows = [_json.loads(l) for l in
            (_REPO / "docs" / "EW0A_LEARNING_LESSONS.jsonl").read_text(
                encoding="utf-8").splitlines() if l.strip()]
    bad = dict(rows[0])
    bad[field] = carrier
    (tmp_path / "docs" / "EW0A_LEARNING_LESSONS.jsonl").write_text(
        _json.dumps(bad) + "\n", encoding="utf-8")
    return tmp_path


# ── learning containment ──────────────────────────────────────────────────
def test_the_learning_envelope_carries_no_producer_derived_key():
    """Absence by construction. The marker cannot be sanitised out of a payload
    that was never admitted."""
    projection, _state = rm._project_learning(_REPO, "engineer.x", _NOW)
    for producer_key in ("recent_lessons", "capability_competence",
                         "lesson_transfer", "graduation_readiness"):
        assert producer_key not in projection, producer_key
    assert set(projection) == {"schema_version", "schema_kind", "read_model",
                               "truth_state", "freshness", "detail"}
    assert "not yet certified" in projection["detail"]


def test_the_learning_builder_is_not_invoked_while_quarantined(monkeypatch):
    """No uncertified work is executed to produce a result this module would
    then throw away."""
    from portfolio_automation.engineer_worker.learning import readmodels as lrm
    calls = []

    def _record(*a, **k):
        calls.append(a)
        return {"recent_lessons": {"recent": [{"principle": _LEAK_MARKER}]}}

    monkeypatch.setattr(lrm, "build_learning_dashboard", _record)
    projection, state = rm._project_learning(_REPO, "engineer.x", _NOW)
    assert calls == [], "the quarantined path invoked the learning builder"
    assert state is TruthState.UNAVAILABLE
    assert _LEAK_MARKER not in _json.dumps(projection, default=str)


def test_a_malformed_learning_field_never_reaches_the_dashboard(tmp_path):
    """The round-5 reproduction, plus a second nested carrier so the test does
    not depend on which field review happened to name."""
    for label, (field, carrier) in {
            "principle_dict": ("principle", {"api_key": _LEAK_MARKER}),
            "observed_dict": ("observed_behavior", {"api_key": _LEAK_MARKER}),
            "principle_nested": ("principle",
                                 {"outer": {"Authorization": f"Bearer {_LEAK_MARKER}"}}),
            "trigger_list": ("trigger", [_LEAK_MARKER]),
    }.items():
        root = _learning_root(tmp_path / label, field, carrier)
        dash = rm.build_dashboard(root, now=_NOW)          # must not raise
        caps = {c["capability"]: c["state"] for c in dash["backend_truth"]["capabilities"]}
        assert caps["learning"] == TruthState.UNAVAILABLE.value, label
        assert dash["learning"]["truth_state"] == TruthState.UNAVAILABLE.value, label
        assert "recent_lessons" not in dash["learning"], label
        blob = _json.dumps(dash, default=str)
        assert _LEAK_MARKER not in blob, label
        assert "api_key" not in blob and "Authorization" not in blob, label


def test_an_absent_learning_producer_is_still_pending_backend(monkeypatch):
    """Quarantine must not collapse the absent/exists distinction that commit 2
    established."""
    monkeypatch.setattr(rm, "_LEARNING_PRODUCER_MODULE",
                        "portfolio_automation.engineer_worker.learning.not_built")
    projection, state = rm._project_learning(_REPO, "engineer.x", _NOW)
    assert state is TruthState.PENDING_BACKEND
    assert projection == PENDING_BACKEND


def test_learning_readiness_effect_is_derived_not_asserted():
    """learning is a SECONDARY capability, so the readiness the lattice derives
    is whatever the required gaps already dictate. Not hardcoded to preserve the
    previous display."""
    truth = rm.build_dashboard(_REPO, now=_NOW)["backend_truth"]
    caps = {c["capability"]: c for c in truth["capabilities"]}
    assert caps["learning"]["required"] is False
    required_gaps = [c["capability"] for c in truth["capabilities"]
                     if c["required"] and c["state"] != TruthState.LIVE.value]
    assert required_gaps, "readiness must still be driven by required gaps"
    assert truth["readiness"] == "PARTIAL"


# ── the executable projection inventory ───────────────────────────────────
def test_every_top_level_dashboard_surface_is_registered():
    """THE control that would have caught learning before review. The set of
    projection paths lived in prose -- including in my own audit tables, twice.
    It is executable now."""
    dash = rm.build_dashboard(_REPO, now=_NOW)
    registered = set(rm.DASHBOARD_PROJECTION_REGISTRY)
    actual = set(dash)
    assert actual - registered == set(), (
        f"unregistered dashboard projections: {sorted(actual - registered)}")
    assert registered - actual == set(), (
        f"registered but not emitted: {sorted(registered - actual)}")


def test_the_registry_holds_on_an_empty_repository_too(tmp_path):
    """The emitted key set must not depend on the data present."""
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    dash = rm.build_dashboard(tmp_path, now=_NOW)
    assert set(dash) == set(rm.DASHBOARD_PROJECTION_REGISTRY)


def test_the_three_certified_paths_are_marked_validated():
    for name in ("run_history", "worker_authority", "active_session"):
        entry = rm.DASHBOARD_PROJECTION_REGISTRY[name]
        assert entry.boundary is rm.ProjectionBoundary.GUI_R_VALIDATED, name
        assert entry.detail


def test_learning_is_marked_pending_certification_not_validated_or_pending_backend():
    entry = rm.DASHBOARD_PROJECTION_REGISTRY["learning"]
    assert entry.boundary is rm.ProjectionBoundary.UNAVAILABLE_PENDING_CERTIFICATION
    assert entry.boundary is not rm.ProjectionBoundary.GUI_R_VALIDATED
    assert "not admitted" in entry.detail


def test_the_registry_does_not_claim_the_whole_dashboard_is_certified():
    """Known A/B/C source-reader debt must stay visible. A registry that marked
    everything validated would be the confidently-wrong artifact this whole PR
    exists to avoid."""
    blocked = {name for name, entry in rm.DASHBOARD_PROJECTION_REGISTRY.items()
               if entry.boundary is rm.ProjectionBoundary.KNOWN_SOURCE_READER_BLOCKER}
    # A/B/C were repaired by GUI-SR, so NOTHING is blocked any more -- the seven
    # previously blocked surfaces declare a HARDENED reader dependency instead.
    # The value is retained in the enum precisely so a future regression can be
    # labelled with it.
    assert blocked == set()
    hardened = {name for name, entry in rm.DASHBOARD_PROJECTION_REGISTRY.items()
                if entry.boundary is rm.ProjectionBoundary.HARDENED_SOURCE_READER}
    assert hardened == {"controller", "mission", "worker", "supervisor",
                        "apprenticeship", "system_health", "backend_truth"}
    validated = {name for name, entry in rm.DASHBOARD_PROJECTION_REGISTRY.items()
                 if entry.boundary is rm.ProjectionBoundary.GUI_R_VALIDATED}
    assert validated == {"run_history", "worker_authority", "active_session",
                         "controller_records"}
    # every blocked surface names which reader blocks it
    for name in hardened:
        assert "reader" in rm.DASHBOARD_PROJECTION_REGISTRY[name].detail.lower(), name


def test_every_registered_surface_carries_a_boundary_and_a_reason():
    for name, entry in rm.DASHBOARD_PROJECTION_REGISTRY.items():
        assert isinstance(entry.boundary, rm.ProjectionBoundary), name
        assert entry.detail.strip(), name


def test_the_registry_derives_no_truth_of_its_own():
    """An audit artifact, not a second truth engine. It must not appear in the
    dashboard, and it must not carry truth states or readiness."""
    dash = rm.build_dashboard(_REPO, now=_NOW)
    assert "projection_registry" not in dash
    assert not (set(rm.DASHBOARD_PROJECTION_REGISTRY) & {"backend_truth_registry"})
    for entry in rm.DASHBOARD_PROJECTION_REGISTRY.values():
        assert not hasattr(entry, "truth_state")
        assert not hasattr(entry, "readiness")


# ═══════════════════════════════════════════════════════════════════════════
# GUI-R FINAL BOUNDARY CORRECTION
#
# Round-6 review found two more instances of the pattern that has run through
# this whole PR: closing the set I was handed rather than deriving it from the
# code. For authority I validated the two fields review had named and left
# `level` -- the third field in the same record -- unvalidated. For the registry
# I classified backend_truth by reading the intent of the enum name instead of
# checking what build_dashboard actually passes into _assess_backend_truth.
# ═══════════════════════════════════════════════════════════════════════════

_LEVEL_MARKER = "sk-LEVEL-MARKER-999"


def _authority_dashboard(tmp_path, label, record):
    import shutil
    root = tmp_path / label
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    shutil.copy(_REPO / "config" / "ew0a_runtime.json",
                root / "config" / "ew0a_runtime.json")
    (root / "config" / "ew0a_authority.json").write_text(
        _json.dumps(record), encoding="utf-8")
    return rm.build_dashboard(root, now=_NOW)


# ── P2 #1: the record's own level is evidence ─────────────────────────────
def test_a_malformed_authority_level_is_never_presented_as_live(tmp_path):
    """`read_authority_level` failing closed to A0 is the ENFORCEMENT result. It
    is not evidence that the stored record says A0. Certifying the two list
    fields while ignoring `level` let a corrupt protected record report
    record_evidence LIVE through a REQUIRED capability."""
    cases = {
        "missing": {},
        "null": {"level": None},
        "invalid_str": {"level": "NOT_A_LEVEL"},
        "empty_str": {"level": ""},
        "dict": {"level": {"api_key": _LEVEL_MARKER}},
        "list": {"level": [_LEVEL_MARKER]},
        "int": {"level": 1},
        "bool": {"level": True},
    }
    for label, extra in cases.items():
        record = {"grants": [], "forbidden_ops": []}
        record.update(extra)
        dash = _authority_dashboard(tmp_path, f"lvl_{label}", record)
        authority = dash["worker_authority"]
        caps = {c["capability"]: c["state"] for c in dash["backend_truth"]["capabilities"]}

        assert authority["record_evidence"] == TruthState.UNAVAILABLE.value, label
        assert caps["worker_authority"] == TruthState.UNAVAILABLE.value, label
        assert dash["backend_truth"]["readiness"] == "UNAVAILABLE", label
        # effective fail-closed enforcement result is preserved and visible
        assert authority["level"] == "A0_DIAGNOSTIC", label
        # permanent safety never depends on record quality
        assert not (authority["can_merge"] or authority["can_deploy"]
                    or authority["can_mutate_main"] or authority["can_write_production"]
                    or authority["can_self_promote"]), label
        for op in ("MERGE", "DEPLOY", "MAIN_WRITE", "PRODUCTION_WRITE", "SELF_PROMOTION"):
            assert op in authority["forbidden_ops"], (label, op)
        # the raw malformed value is never rendered
        blob = _json.dumps(dash, default=str)
        assert _LEVEL_MARKER not in blob, label
        assert "api_key" not in blob, label


def test_both_valid_levels_still_project_live_evidence(tmp_path):
    for label, value in (("a0", "A0_DIAGNOSTIC"), ("a1", "A1_ASSISTED_ENGINEERING")):
        # Each level's own canonical grant set: A0 grants nothing, A1 grants
        # A1_GRANTS. `grants: []` used to be accepted for BOTH.
        dash = _authority_dashboard(tmp_path, f"ok_{label}", {
            "level": value, "grants": list(rm.grants_for_level(Lvl(value))),
            "forbidden_ops": []})
        assert dash["worker_authority"]["record_evidence"] == TruthState.LIVE.value, label
        assert dash["worker_authority"]["level"] == value, label
        assert dash["backend_truth"]["readiness"] == "PARTIAL", label


def test_effective_level_and_record_evidence_are_reported_separately(tmp_path):
    """Not a contradiction: A0 is the safe enforcement fallback, UNAVAILABLE says
    the stored record cannot be trusted as evidence of intended configuration.
    An operator must be able to read both."""
    dash = _authority_dashboard(tmp_path, "separate", {
        "level": "NOT_A_LEVEL", "grants": [], "forbidden_ops": []})
    authority = dash["worker_authority"]
    assert authority["level"] == "A0_DIAGNOSTIC"
    assert authority["record_evidence"] == TruthState.UNAVAILABLE.value
    assert "level" in authority["record_detail"]


def test_two_disagreeing_reads_of_the_record_are_not_resolved_silently():
    """If the raw level and the canonical reader ever disagree, picking one and
    reporting LIVE would hide that the protected record is inconsistent."""
    for label, (effective, raw) in {
            "raw_a0_canon_a1": (Lvl.A1_ASSISTED_ENGINEERING, "A0_DIAGNOSTIC"),
            "raw_a1_canon_a0": (Lvl.A0_DIAGNOSTIC, "A1_ASSISTED_ENGINEERING"),
    }.items():
        summary = build_worker_authority_summary(
            effective, grants=[], forbidden_ops=[], raw_level=raw)
        assert summary.record_evidence == TruthState.UNAVAILABLE.value, label
        assert "disagrees" in summary.record_detail, label
        assert summary.can_merge is False, label


def test_the_level_validator_reports_facts_not_values():
    with pytest.raises(ValueError, match="absent"):
        rm._validated_raw_level(rm._MISSING, Lvl.A0_DIAGNOSTIC)
    with pytest.raises(ValueError, match="null"):
        rm._validated_raw_level(None, Lvl.A0_DIAGNOSTIC)
    with pytest.raises(ValueError, match="invalid type dict"):
        rm._validated_raw_level({"api_key": _LEVEL_MARKER}, Lvl.A0_DIAGNOSTIC)
    try:
        rm._validated_raw_level({"api_key": _LEVEL_MARKER}, Lvl.A0_DIAGNOSTIC)
    except ValueError as exc:
        assert _LEVEL_MARKER not in str(exc)
    with pytest.raises(ValueError, match="not a recognized authority enum value"):
        rm._validated_raw_level("NOT_A_LEVEL", Lvl.A0_DIAGNOSTIC)
    # membership is checked against the canonical enum, not a copied list
    assert rm._AUTHORITY_LEVEL_VALUES == {lvl.value for lvl in Lvl}


def test_only_the_consumed_authority_fields_are_validated(tmp_path):
    """Scope discipline: actor/updated_at/schema_* are not consumed by this
    projection and are deliberately not validated."""
    dash = _authority_dashboard(tmp_path, "extras", {
        "level": "A1_ASSISTED_ENGINEERING",
        # the CONSUMED fields must be genuinely valid, or this test would prove
        # nothing about the unconsumed ones
        "grants": list(rm.grants_for_level(Lvl.A1_ASSISTED_ENGINEERING)),
        "forbidden_ops": [],
        "actor": {"unexpected": "shape"}, "updated_at": 12345,
        "schema_version": None})
    assert dash["worker_authority"]["record_evidence"] == TruthState.LIVE.value


def test_the_real_authority_record_is_unaffected_by_level_validation():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    authority = dash["worker_authority"]
    assert authority["record_evidence"] == TruthState.LIVE.value
    assert authority["level"] == "A1_ASSISTED_ENGINEERING"
    assert dash["backend_truth"]["readiness"] == "PARTIAL"


# ── P2 #2: raw dependency declared AND coupled to classification ─────────
def test_backend_truth_is_classified_blocked_on_its_raw_readers():
    entry = rm.DASHBOARD_PROJECTION_REGISTRY["backend_truth"]
    assert entry.boundary is rm.ProjectionBoundary.HARDENED_SOURCE_READER
    assert entry.boundary is not rm.ProjectionBoundary.DERIVED_FROM_REGISTERED_INPUTS
    assert set(entry.canonical_readers) == {
        rm.RawSourceReader.AUTHORITY_LEVEL,
        rm.RawSourceReader.RUNTIME_POLICY,
        rm.RawSourceReader.CONTROLLER_RECORDS,
    }
    for reader in ("reader A", "reader B", "reader C"):
        assert reader in entry.detail


def test_no_surface_claims_freedom_from_raw_evidence_while_declaring_it():
    """THE executable coupling. The registry alone was a hand-maintained
    assertion and shipped with backend_truth marked derived-only while it
    consumed three raw readers -- an optimistic entry in the artifact built to
    prevent optimistic entries."""
    assert rm.registry_classification_violations() == []


def test_the_call_site_proves_backend_truth_receives_raw_inputs():
    """Bounded AST over ONE call, not repo-wide analysis.

    Reads the actual _build_dashboard_from_evidence -> _assess_backend_truth
    call and requires the registry to declare whatever reader-output variables
    it passes. Narrow enough to stay sound: it inspects the keywords of a single
    named call, and makes no claim about anything else."""
    src = (_REPO / "portfolio_automation" / "engineer_worker"
           / "ew0a_readmodels.py").read_text(encoding="utf-8")
    passed: set = set()
    for node in _ast.walk(_ast.parse(src)):
        if not (isinstance(node, _ast.FunctionDef)
                and node.name == "_build_dashboard_from_evidence"):
            continue
        for inner in _ast.walk(node):
            if (isinstance(inner, _ast.Call)
                    and getattr(inner.func, "id", None) == "_assess_backend_truth"):
                for keyword in inner.keywords:
                    name = getattr(keyword.value, "id", None)
                    if name in rm.RAW_SOURCE_VARIABLES:
                        passed.add(rm.RAW_SOURCE_VARIABLES[name])
        break
    else:                                                # pragma: no cover
        raise AssertionError("_build_dashboard_from_evidence not found")

    assert passed, "expected _assess_backend_truth to receive raw blocked inputs"
    declared = set(rm.DASHBOARD_PROJECTION_REGISTRY["backend_truth"].canonical_readers)
    assert passed <= declared, (
        f"backend_truth receives undeclared raw sources: "
        f"{sorted(s.value for s in passed - declared)}")
    assert rm.DASHBOARD_PROJECTION_REGISTRY["backend_truth"].boundary is \
        rm.ProjectionBoundary.HARDENED_SOURCE_READER


def test_derived_from_registered_inputs_is_now_claimed_by_nobody():
    """It may only mean 'introduces no direct dependency on raw authoritative
    evidence'. Nothing in this dashboard qualifies today; a future mission may
    legitimately reclassify backend_truth after GUI-SR restructures its inputs."""
    claiming = {name for name, entry in rm.DASHBOARD_PROJECTION_REGISTRY.items()
                if entry.boundary is rm.ProjectionBoundary.DERIVED_FROM_REGISTERED_INPUTS}
    assert claiming == set()


def test_every_reader_dependent_surface_declares_at_least_one_reader():
    for name, entry in rm.DASHBOARD_PROJECTION_REGISTRY.items():
        if entry.boundary in (rm.ProjectionBoundary.HARDENED_SOURCE_READER,
                              rm.ProjectionBoundary.KNOWN_SOURCE_READER_BLOCKER):
            assert entry.canonical_readers, f"{name} declares no reader dependency"


def test_the_registry_still_matches_the_emitted_surfaces():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    assert set(dash) == set(rm.DASHBOARD_PROJECTION_REGISTRY)


# ═══════════════════════════════════════════════════════════════════════════
# GUI-RI — INTEGRATION SEAM REGRESSIONS
#
# Both seams below were caught by the merged guards rather than by reading the
# diff, which is the whole reason those guards exist.
# ═══════════════════════════════════════════════════════════════════════════
def test_ri_the_two_producerless_declarations_agree():
    """GUI-SR's corruption matrix and GUI-R's readiness tests each carry a
    producer-less set. After integration they must be the same six, or one of
    them is silently tolerating a PENDING_BACKEND that has a producer."""
    assert _INTEGRATED_PRODUCERLESS == _PRODUCERLESS


def test_ri_the_producerless_set_matches_the_real_dashboard():
    truth = rm.build_dashboard(_REPO, now=_NOW)["backend_truth"]
    pending = {c["capability"] for c in truth["capabilities"]
               if c["state"] == PENDING_BACKEND}
    assert pending == _INTEGRATED_PRODUCERLESS


def test_ri_the_authority_second_read_is_still_covered_by_the_coupling_guard():
    """The renamed local must be the one the guard excludes -- if the exclusion
    named a variable that no longer exists, the guard would silently stop
    covering build_dashboard's authority read."""
    src = (_REPO / "portfolio_automation" / "engineer_worker"
           / "ew0a_readmodels.py").read_text(encoding="utf-8")
    assert "authority_record.get(" in src
    assert "authority_record" in _CR_NON_RECORD_BASES


def test_ri_guir_projections_consume_the_hardened_records_reader():
    """The integration's central claim: GUI-R's projections now read the
    controller ledger through GUI-SR's admission path, not a raw reader."""
    src = (_REPO / "portfolio_automation" / "engineer_worker"
           / "ew0a_readmodels.py").read_text(encoding="utf-8")
    # exactly one admission path, and no resurrected raw reader
    assert src.count("def read_controller_records(") == 1
    assert src.count("def _read_records(") == 1
    assert "except json.JSONDecodeError:\n                pass" not in src
    dash = rm.build_dashboard(_REPO, now=_NOW)
    assert dash["controller_records"]["availability"] == "LIVE"
    assert dash["supervisor"]["records_evidence"] == "LIVE"


def test_ri_one_ledger_constant_survives_the_merge():
    src = (_REPO / "portfolio_automation" / "engineer_worker"
           / "ew0a_readmodels.py").read_text(encoding="utf-8")
    assert src.count('CONTROLLER_RECORDS_REL = "docs/EW0A_0B3_RECORDS.jsonl"') == 1
    assert "RECORDS_LEDGER_REL =" not in src
    assert rm.CONTROLLER_RECORDS_REL == "docs/EW0A_0B3_RECORDS.jsonl"


def test_ri_v1_is_carried_by_every_wcc_projection_after_integration():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    assert dash["schema_version"] == "engineering.readmodel.v1"
    for key, value in dash.items():
        if isinstance(value, dict) and "schema_version" in value:
            assert value["schema_version"] in (
                "engineering.readmodel.v1",
                "engineering.control_center_truth.v0",
            ), key


# ═══════════════════════════════════════════════════════════════════════════
# GUI-RI — THE HARDENED LABEL MUST BE DISCHARGED, NOT ASSERTED
#
# HARDENED_SOURCE_READER replaced KNOWN_SOURCE_READER_BLOCKER because GUI-SR
# made A, B and C total. That is a claim about behaviour, so it is bound to the
# corruption proofs that establish it. Adding a fourth reader, or labelling a
# surface hardened without a proof, fails here rather than shipping a label
# nobody checked -- which is the failure mode that produced eight GUI-R rounds.
# ═══════════════════════════════════════════════════════════════════════════
#: Reader -> the test that proves build_dashboard survives its corruption.
_RI_READER_TOTALITY_PROOF = {
    rm.RawSourceReader.AUTHORITY_LEVEL:
        "test_gsr_authority_corruption_never_crashes_or_leaks",
    rm.RawSourceReader.RUNTIME_POLICY:
        "test_gsr_runtime_corruption_never_crashes_or_leaks",
    rm.RawSourceReader.CONTROLLER_RECORDS:
        "test_gsr_records_corruption_never_crashes_or_leaks",
}


def test_ri_every_canonical_reader_has_a_totality_proof():
    assert set(_RI_READER_TOTALITY_PROOF) == set(rm.RawSourceReader)
    for reader, proof in _RI_READER_TOTALITY_PROOF.items():
        assert callable(globals().get(proof)), (
            f"{reader.value} claims totality via {proof}, which does not exist")


def test_ri_every_hardened_surface_declares_only_proven_readers():
    """The label means "consumes a reader GUI-SR made total". A surface may not
    claim it while depending on a reader whose totality nothing establishes."""
    for name, entry in rm.DASHBOARD_PROJECTION_REGISTRY.items():
        if entry.boundary is not rm.ProjectionBoundary.HARDENED_SOURCE_READER:
            continue
        assert entry.canonical_readers, name
        for reader in entry.canonical_readers:
            assert reader in _RI_READER_TOTALITY_PROOF, f"{name} -> {reader.value}"


def test_ri_hardened_details_name_exactly_the_readers_they_declare():
    """Prose and declaration cannot drift: the detail must name every reader the
    surface declares, and must not name one it does not."""
    letters = {rm.RawSourceReader.AUTHORITY_LEVEL: "A",
               rm.RawSourceReader.RUNTIME_POLICY: "B",
               rm.RawSourceReader.CONTROLLER_RECORDS: "C"}
    for name, entry in rm.DASHBOARD_PROJECTION_REGISTRY.items():
        if entry.boundary is not rm.ProjectionBoundary.HARDENED_SOURCE_READER:
            continue
        declared = {letters[r] for r in entry.canonical_readers}
        for letter in letters.values():
            mentioned = f"reader {letter}" in entry.detail
            assert mentioned == (letter in declared), (
                f"{name}: detail {'names' if mentioned else 'omits'} reader "
                f"{letter} but declares {sorted(declared)}")


def test_ri_nothing_claims_the_blocker_label_any_more():
    """A/B/C are repaired, so the blocker label must be unclaimed. The value
    stays in the enum so a future regression can be labelled honestly rather
    than forcing a choice between two wrong labels."""
    blocked = [n for n, e in rm.DASHBOARD_PROJECTION_REGISTRY.items()
               if e.boundary is rm.ProjectionBoundary.KNOWN_SOURCE_READER_BLOCKER]
    assert blocked == []
    assert rm.ProjectionBoundary.KNOWN_SOURCE_READER_BLOCKER in rm.ProjectionBoundary


def test_ri_all_three_readers_corrupt_at_once_still_yields_an_honest_dashboard(
        tmp_path):
    """The isolated matrices each corrupt one reader. After integration a single
    build_dashboard call consumes all three, so the totality claim has to hold
    when every source is simultaneously unusable."""
    root = _gsr_root(tmp_path / "ABC",
                     authority=json.dumps(_GSR_CARRIERS["dict"]),
                     runtime="[1,2]",
                     records="[1,2]" + _GSR_LF + "{oops" + _GSR_LF)
    dash = rm.build_dashboard(root, now=_GSR_NOW)          # must not raise
    _gsr_dashboard_is_honest(dash, "ABC")

    # A fail-closed, B unavailable, C unusable -- and each said in its own voice.
    assert dash["worker_authority"]["can_merge"] is False
    assert dash["controller_records"]["availability"] == "UNAVAILABLE"
    caps = {c["capability"]: c["state"] for c in dash["backend_truth"]["capabilities"]}
    assert caps["controller_state"] == "UNAVAILABLE"
    assert dash["backend_truth"]["readiness"] == "UNAVAILABLE"
    # unanswerable is not zero
    assert dash["supervisor"]["recent_pass"] is None
    # and the learning quarantine holds: still uncertified, still not admitted
    learning = dash["learning"]
    assert learning["truth_state"] == "UNAVAILABLE"
    assert learning["schema_kind"] == "experimental_noncanonical"
    assert set(learning) == {"schema_version", "schema_kind", "read_model",
                             "truth_state", "freshness", "detail"}


def test_ri_the_registry_still_covers_every_emitted_surface_after_reclassification():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    assert set(dash) == set(rm.DASHBOARD_PROJECTION_REGISTRY)
    assert rm.registry_classification_violations() == []


# ═══════════════════════════════════════════════════════════════════════════
# GUI-RI — SOURCE BOUNDARY: WHAT IS ACTUALLY CLAIMED
#
# Four review rounds established that this claim was too strong:
#
#     "static analysis proves arbitrary source IO cannot escape this boundary"
#
# Each round replaced one enumeration with a narrower enumeration -- a reader
# enum, then a method-name list, then two `ast.Call.func` shapes, then a
# gateway-to-dependency map -- and each time the residue was still an
# enumeration. Python is too expressive for a small bounded AST test to
# establish absence of arbitrary behaviour. THE REPEATED DEFECT WAS THE
# COMPLETENESS CLAIM ITSELF, not a missing case.
#
# So the claim is corrected rather than the analyzer extended.
#
# CLAIMED (structural, and genuinely enforced):
#   - `_build_dashboard_from_evidence` takes NO repo_root, Path, or source
#     filename. It cannot reach the repository through its parameters. This is
#     a signature property, not a syntax survey.
#   - `build_dashboard` is two lines: collect, then project.
#   - Source acquisition is concentrated in named gateways with explicit,
#     reviewed contracts, frozen into a `_DashboardEvidence` before projection.
#
# NOT CLAIMED:
#   - that the projection layer is pure, sandboxed, or mechanically incapable
#     of IO;
#   - that every possible collector call is discovered;
#   - that gateway interiors are mechanically proven to acquire only what they
#     declare.
#
# THE REAL SAFETY BOUNDARY IS ELSEWHERE, and always was: typed reader
# admission, whole-ledger refusal, field-level validation, fail-closed
# authority, no evidence coercion, and the learning quarantine. Those are
# behavioural, mutation-tested, and are what actually stop malformed evidence
# from becoming clean-looking GUI state. The static guards below are
# DEFENCE IN DEPTH against accidental regression. They are not the boundary,
# and nothing here should be cited as if they were.
# ═══════════════════════════════════════════════════════════════════════════

#: Each gateway's explicit architectural contract: the source it acquires, and
#: which dependency identities the registry therefore declares. REVIEWED AND
#: REGRESSION-TESTED, not derived -- no claim is made that Python introspection
#: proves a gateway could never acquire another source. Content-bearing readers
#: additionally carry their own corruption/totality/non-leak proofs.
def _ri_gateway_contracts():
    ident = rm.source_access_identity
    kinds = rm.SourceAccessKind
    return {
        "read_authority_level": {
            "source": "config/ew0a_authority.json",
            "returns": "effective EngineerAuthorityLevel",
            "failure": "A0_DIAGNOSTIC (fail closed)",
            "declares": {ident(kinds.CANONICAL_READER,
                               rm.RawSourceReader.AUTHORITY_LEVEL.value)}},
        "read_runtime_policy": {
            "source": "config/ew0a_runtime.json",
            "returns": "typed runtime policy or None",
            "failure": "None; malformed fields rejected by declared type",
            "declares": {ident(kinds.CANONICAL_READER,
                               rm.RawSourceReader.RUNTIME_POLICY.value)}},
        "read_controller_records": {
            "source": rm.CONTROLLER_RECORDS_REL,
            "returns": "ControllerRecordsRead (whole-ledger admission)",
            "failure": "UNAVAILABLE; never a filtered partial ledger",
            "declares": {ident(kinds.CANONICAL_READER,
                               rm.RawSourceReader.CONTROLLER_RECORDS.value)}},
        "_read_authority_record_evidence": {
            "source": "config/ew0a_authority.json",
            "returns": "raw grants / forbidden_ops / level behind _MISSING",
            "failure": "_MISSING for all three; never a partial read",
            "validation_owner": "build_worker_authority_summary",
            "declares": {rm.DirectSource.AUTHORITY_RECORD_EVIDENCE.value}},
        "_read_system_config_readability": {
            "source": tuple(rm.SYSTEM_CONFIG_READABILITY_SOURCES.values()),
            "returns": "finite readability states per source",
            "never": "file contents, parsed evidence, or liveness",
            "declares": {ident(kinds.DIRECT_READABILITY_PROBE, rel)
                         for rel in rm.SYSTEM_CONFIG_READABILITY_SOURCES.values()}},
        "_read_northstar_contract_presence": {
            "source": "portfolio_automation.northstar (module attributes)",
            "returns": "frozenset of present contract names",
            "failure": "empty set (fail closed)",
            "declares": {rm.DirectSource.NORTHSTAR_CONTRACT_PRESENCE.value}},
        "build_run_history": {
            "source": rm.OUTCOME_LEDGER_REL,
            "returns": "RunHistorySummary",
            "boundary": "separately certified producer; own tests",
            "declares": set()},
        "_project_learning": {
            "source": "learning producer module",
            "returns": "quarantined envelope + truth state",
            "boundary": "UNAVAILABLE_PENDING_CERTIFICATION; payload not admitted",
            "declares": set()},
        "_build_active_session": {
            "source": "session producer module",
            "returns": "(payload, producer_status, detail)",
            "boundary": "separately certified producer; own tests",
            "declares": set()},
    }


def _ri_function_ast(name):
    src = (_REPO / "portfolio_automation" / "engineer_worker"
           / "ew0a_readmodels.py").read_text(encoding="utf-8")
    for node in _ast.walk(_ast.parse(src)):
        if isinstance(node, _ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _ri_direct_named_calls(func):
    """Directly named calls in `func`. NOT a complete call discovery -- a
    subscript-, lambda-, partial- or attribute-dispatched callee is invisible
    here, and this is deliberately no longer presented as proof of anything."""
    return [n.func.id for n in _ast.walk(func)
            if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)]


# ── the structural claims that ARE enforced ──────────────────────────────
def test_ri_the_projection_layer_takes_no_source_input():
    """The strongest honest claim here, and a signature property rather than a
    syntax survey: the projection function cannot reach the repository through
    its parameters."""
    import inspect
    params = list(inspect.signature(rm._build_dashboard_from_evidence).parameters)
    assert params == ["evidence", "now"]
    for forbidden in ("repo_root", "root", "path", "paths", "source", "config"):
        assert forbidden not in params

    annotations = rm._build_dashboard_from_evidence.__annotations__
    assert annotations["evidence"] is rm._DashboardEvidence \
        or annotations["evidence"] == "_DashboardEvidence"

    # and it produces the whole dashboard from evidence alone
    evidence = rm._collect_dashboard_evidence(_REPO, _NOW)
    dash = rm._build_dashboard_from_evidence(evidence, _NOW)
    assert set(dash) == set(rm.DASHBOARD_PROJECTION_REGISTRY)
    assert dash == rm.build_dashboard(_REPO, now=_NOW)


def test_ri_build_dashboard_is_collect_then_project():
    """Two phases, one entry point, no interleaving."""
    func = _ri_function_ast("build_dashboard")
    calls = _ri_direct_named_calls(func)
    assert calls.count("_collect_dashboard_evidence") == 1
    assert calls.count("_build_dashboard_from_evidence") == 1
    assert calls.count("Path") == 1
    assert set(calls) == {"_collect_dashboard_evidence",
                          "_build_dashboard_from_evidence", "Path"}
    # small enough to read at a glance -- that is the actual control
    assert len(func.body) <= 4, "build_dashboard grew beyond collect-then-project"


def test_ri_the_evidence_boundary_is_frozen_and_complete():
    """Every source-backed input the projection needs travels through it."""
    import dataclasses
    assert dataclasses.is_dataclass(rm._DashboardEvidence)
    assert rm._DashboardEvidence.__dataclass_params__.frozen
    fields = {f.name for f in dataclasses.fields(rm._DashboardEvidence)}
    assert fields == {
        "level", "policy", "records_read", "authority_grants",
        "authority_forbidden_ops", "authority_raw_level", "contract_presence",
        "config_readability", "run_history", "learning", "learning_state",
        "session_payload", "session_status", "session_detail"}


# ── gateway contracts: reviewed declarations, regression-tested ───────────
def test_ri_every_gateway_contract_names_a_real_gateway():
    for name in _ri_gateway_contracts():
        assert callable(getattr(rm, name)), name


def test_ri_declared_dependencies_match_the_reviewed_gateway_contracts():
    """Registry declarations must agree with the gateway contracts.

    This is a consistency check between two REVIEWED artifacts, not a
    whole-program derivation. It catches a declaration drifting from its
    contract; it cannot catch a gateway interior quietly acquiring a source
    neither artifact mentions -- see the honesty test below."""
    acquired = set()
    for contract in _ri_gateway_contracts().values():
        acquired |= contract["declares"]
    declared = set(rm.declared_source_dependency_union())
    assert acquired == declared, (
        f"contracts declare but registry does not: {sorted(acquired - declared)}; "
        f"registry declares but no contract does: {sorted(declared - acquired)}")


def test_ri_the_readability_contract_is_the_implementation_mapping():
    """For this one gateway the contract IS executable: the same mapping drives
    the probes, so a probe cannot be claimed without being performed."""
    mapped = set(rm.SYSTEM_CONFIG_READABILITY_SOURCES.values())
    declared = {rm.DIRECT_SOURCE_TARGETS[d] for d in rm.DirectSource
                if rm.DIRECT_SOURCE_KINDS[d]
                is rm.SourceAccessKind.DIRECT_READABILITY_PROBE}
    assert mapped == declared
    assert len(rm.SYSTEM_CONFIG_READABILITY_SOURCES) == 4
    health = rm.build_dashboard(_REPO, now=_NOW)["system_health"]
    assert set(health["config_readability"]) == set(rm.SYSTEM_CONFIG_READABILITY_SOURCES)


# ── defence in depth: regression guards, explicitly not proofs ────────────
def test_dind_the_collector_still_only_calls_named_gateways():
    """DEFENCE IN DEPTH — regression guard, NOT a completeness proof.

    Catches someone adding an obvious new direct call to the collector. It does
    NOT discover a subscript-, lambda-, partial- or attribute-dispatched callee,
    and is not claimed to. Adding shape support was tried and rejected: that
    path produced four findings."""
    func = _ri_function_ast("_collect_dashboard_evidence")
    allowed = set(_ri_gateway_contracts()) | {"_DashboardEvidence"}
    unexpected = sorted(set(_ri_direct_named_calls(func)) - allowed)
    assert not unexpected, f"new directly-named call in the collector: {unexpected}"


def test_dind_the_collector_calls_each_gateway_once():
    """DEFENCE IN DEPTH — occurrence regression guard, not a proof.

    A multiset so a duplicated acquisition is visible rather than collapsing
    under a name that already appears."""
    from collections import Counter
    func = _ri_function_ast("_collect_dashboard_evidence")
    counts = Counter(_ri_direct_named_calls(func))
    expected = Counter({name: 1 for name in _ri_gateway_contracts()})
    expected["_DashboardEvidence"] = 1
    assert counts == expected, f"collector call counts drifted: {dict(counts)}"


def test_dind_the_collector_builds_no_source_paths():
    """DEFENCE IN DEPTH — regression guard, not a proof.

    Source paths belong inside their gateways, so the collector coordinates
    rather than constructs. Catches the obvious regression only."""
    func = _ri_function_ast("_collect_dashboard_evidence")
    assert not [n for n in _ast.walk(func)
                if isinstance(n, _ast.BinOp) and isinstance(n.op, _ast.Div)]
    assert "Path" not in _ri_direct_named_calls(func)


def test_dind_no_obvious_source_acquisition_in_the_projection_layer():
    """DEFENCE IN DEPTH — regression guard, NOT proof of IO-freedom.

    Confirms the reviewed implementation contains no obvious acquisition. A
    helper called from here could still perform IO of its own; that is exactly
    the claim being retracted, and it is why this test is labelled rather than
    trusted."""
    func = _ri_function_ast("_build_dashboard_from_evidence")
    obvious = {"read_text", "read_bytes", "open", "iterdir", "glob", "rglob",
               "stat", "readlink", "exists"}
    attrs = {n.func.attr for n in _ast.walk(func)
             if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)}
    assert not (attrs & obvious), sorted(attrs & obvious)
    named = set(_ri_direct_named_calls(func))
    assert "open" not in named and "Path" not in named
    assert not (named & set(_ri_gateway_contracts())), (
        "the projection layer calls a gateway; acquisition belongs in the collector")
    assert not [n for n in _ast.walk(func)
                if isinstance(n, (_ast.Import, _ast.ImportFrom))]


def test_ri_the_static_guards_do_not_claim_to_be_proofs():
    """The correction, asserted so it cannot quietly regress into an overclaim.

    Every defence-in-depth guard must say so in its own docstring, and the
    honest-limits helper must document what it cannot see."""
    import inspect
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_dind_"):
            continue
        doc = inspect.getdoc(fn) or ""
        assert "DEFENCE IN DEPTH" in doc, f"{name} does not label itself"
        assert "proof" in doc.lower(), f"{name} does not disclaim proof"
    assert "NOT a complete call discovery" in (
        inspect.getdoc(_ri_direct_named_calls) or "")


def test_ri_the_retired_analyzers_are_gone():
    """Checks BINDINGS, not mentions -- the banners deliberately name the retired
    pieces to record why the claim was corrected, and that history is worth
    keeping."""
    retired = {"_RI_CONTENT_ACQUISITION", "_RI_BENIGN_PREDICATES",
               "_RI_ANALYSED_FUNCTIONS", "_RI_DELEGATED_ROOT_CONSUMERS",
               "_RI_FORBIDDEN_IN_ASSEMBLER", "_RI_CERTIFIED_GATEWAY_CALLS",
               "_ri_derive_source_accesses", "_ri_resolve_path",
               "_ri_called_names", "_ri_gateway_dependencies"}

    def bound_names(source):
        names = set()
        for node in _ast.walk(_ast.parse(source)):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                                 _ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, _ast.Assign):
                names |= {t.id for t in node.targets if isinstance(t, _ast.Name)}
            elif isinstance(node, _ast.AnnAssign) and isinstance(
                    node.target, _ast.Name):
                names.add(node.target.id)
        return names

    import pathlib
    for label, source in (
            ("test module", pathlib.Path(__file__).read_text(encoding="utf-8")),
            ("read model", (_REPO / "portfolio_automation" / "engineer_worker"
                            / "ew0a_readmodels.py").read_text(encoding="utf-8"))):
        survived = sorted(retired & bound_names(source))
        assert not survived, f"{label} still binds {survived}"


def test_ri_collecting_evidence_twice_is_stable():
    """A read-only collector: same root, same evidence."""
    a = rm._collect_dashboard_evidence(_REPO, _NOW)
    b = rm._collect_dashboard_evidence(_REPO, _NOW)
    assert a.level == b.level
    assert a.config_readability == b.config_readability
    assert a.contract_presence == b.contract_presence
    assert a.records_read.availability == b.records_read.availability


def test_ri_the_northstar_gateway_fails_closed():
    presence = rm._read_northstar_contract_presence()
    assert isinstance(presence, frozenset)
    assert presence <= set(rm._NORTHSTAR_0B3)


# ═══════════════════════════════════════════════════════════════════════════
# GUI-RI — SUPPLEMENTAL BEHAVIOURAL OBSERVATION
#
# CPython audit events are OBSERVATIONAL. They do not prove absence of all IO
# and they are not a sandbox: a hook can be added but never removed, hooks see
# only events the interpreter chooses to raise, and C-level or ctypes access can
# bypass them entirely.
#
# This is therefore SUPPLEMENTAL EVIDENCE ONLY. It is deliberately NOT the
# completeness or security boundary, and nothing depends on it for correctness.
# It is here because it observes EFFECTS rather than recognising syntax, so it
# notices a regression that the static guards structurally cannot -- for example
# a helper called from the projection layer that reads a file of its own.
#
# It carries its own POSITIVE CONTROL. Without one, an empty event list is
# ambiguous between "no IO happened" and "the hook never fired at all", and the
# second reading would make this test worse than useless. The collection phase
# must register events; the projection phase must not.
#
# Runs in a dedicated subprocess so the un-removable audit hook never persists
# into the normal pytest interpreter.
# ═══════════════════════════════════════════════════════════════════════════
_RI_AUDIT_SCRIPT = r"""
import json
import pathlib
import sys

import portfolio_automation.engineer_worker.ew0a_readmodels as rm

ROOT = pathlib.Path(sys.argv[1])
NOW = sys.argv[2]
WATCHED = ("open", "os.", "import", "socket.", "subprocess.", "urllib.",
           "shutil.", "pathlib.", "glob.", "tempfile.")

observed = []


def hook(event, args):
    if event.startswith(WATCHED):
        observed.append(event)


# Warm both phases first so lazy imports and caches cannot register as events
# during measurement.
warm = rm._collect_dashboard_evidence(ROOT, NOW)
rm._build_dashboard_from_evidence(warm, NOW)

sys.addaudithook(hook)

# POSITIVE CONTROL -- collection is supposed to touch the filesystem.
rm._collect_dashboard_evidence(ROOT, NOW)
control = sorted(set(observed))
observed.clear()

# MEASURED -- projection should touch nothing.
rm._build_dashboard_from_evidence(warm, NOW)
measured = sorted(set(observed))
observed.clear()

sys.stdout.write(json.dumps({"control": control, "measured": measured}))
"""


def test_ri_projection_registers_no_audited_source_events(tmp_path):
    """SUPPLEMENTAL OBSERVATION — not a proof, not a sandbox.

    CPython audit events do not prove absence of all IO. This observes that in
    the reviewed implementation the projection phase raises no filesystem,
    import, network or process audit events, while the collection phase does.
    Treat it as a behavioural regression detector, never as the boundary."""
    import json
    import subprocess

    script = tmp_path / "audit_observe.py"
    script.write_text(_RI_AUDIT_SCRIPT, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), str(_REPO), _NOW],
        capture_output=True, text=True, cwd=str(_REPO), timeout=180,
        env={**os.environ, "PYTHONPATH": str(_REPO), "PYTHONDONTWRITEBYTECODE": "1"})

    assert result.returncode == 0, result.stderr[-2000:]
    payload = json.loads(result.stdout)

    # positive control: if this is empty the hook never fired and the
    # measurement below would be meaningless rather than reassuring
    assert payload["control"], (
        "audit hook registered nothing even for the collection phase; the "
        "observation is inconclusive, not clean")
    assert "open" in payload["control"]

    assert payload["measured"] == [], (
        f"projection phase raised audited source events: {payload['measured']}")


def test_ri_the_audit_observation_is_not_described_as_a_proof():
    """The disclaimer is load-bearing: this instrument is easy to over-trust."""
    import inspect
    doc = inspect.getdoc(test_ri_projection_registers_no_audited_source_events) or ""
    assert "SUPPLEMENTAL OBSERVATION" in doc
    assert "not a proof" in doc
    assert "not a sandbox" in doc
    assert "never as the boundary" in doc


# ═══════════════════════════════════════════════════════════════════════════
# GUI-RI — _readability CERTIFIED AS A DIRECT SOURCE PROBE
#
# A probe is not a reader. It answers ONE structural question and is forbidden
# from emitting content, which is the whole reason it may be declared as a
# separate SourceAccessKind rather than forced into RawSourceReader.
#
# Deliberately no runtime assertion inside _readability: its contract is that it
# NEVER raises, and a self-check that could raise would trade that guarantee for
# a redundant one. The proof belongs here.
# ═══════════════════════════════════════════════════════════════════════════
_RI_MARKER = "sk-READABILITY-MUST-NOT-RENDER-999"


def _ri_marked_root(tmp_path):
    """A repo root whose every probed source is READABLE and marker-bearing."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "config" / "ew0a_authority.json").write_text(json.dumps({
        "level": "A1_ASSISTED_ENGINEERING",
        "grants": ["read"], "forbidden_ops": ["MERGE"],
        "operator_note": _RI_MARKER}), encoding="utf-8")
    (tmp_path / "config" / "ew0a_runtime.json").write_text(json.dumps({
        "mission_id": "m-ri", "operator_note": _RI_MARKER}), encoding="utf-8")
    (tmp_path / rm.OUTCOME_LEDGER_REL).write_text(
        json.dumps({"note": _RI_MARKER}) + "\n", encoding="utf-8")
    (tmp_path / rm.CONTROLLER_RECORDS_REL).write_text(
        json.dumps({"kind": "X", "note": _RI_MARKER}) + "\n", encoding="utf-8")
    return tmp_path


def test_ri_readability_output_belongs_to_a_finite_contract(tmp_path):
    root = _ri_marked_root(tmp_path / "finite")
    probes = {
        "existing_readable": root / "config" / "ew0a_authority.json",
        "absent": root / "config" / "does_not_exist.json",
        "directory_in_file_position": root / "config",
        "outcome_ledger": root / rm.OUTCOME_LEDGER_REL,
    }
    binary = root / "docs" / "binary.bin"
    binary.write_bytes(b"\xff\xfe " + _RI_MARKER.encode() + b" \x00\x80")
    probes["undecodable"] = binary

    for label, path in probes.items():
        value = rm._readability(path)                      # must not raise
        assert value in rm.READABILITY_STATES, f"{label}: {value!r} off-contract"
        assert _RI_MARKER not in value, f"{label}: probe rendered content"

    assert rm._readability(probes["existing_readable"]) == "READABLE"
    assert rm._readability(probes["absent"]) == "ABSENT"
    assert rm._readability(probes["directory_in_file_position"]) == "UNREADABLE"
    assert rm._readability(probes["undecodable"]) == "UNREADABLE"


def test_ri_readability_reports_an_io_failure_without_quoting_the_file(tmp_path):
    """An unreadable file must be explained as UNREADABLE, not by echoing why."""
    root = _ri_marked_root(tmp_path / "io")
    secret = root / "docs" / "locked.json"
    secret.write_text(json.dumps({"api_key": _RI_MARKER}), encoding="utf-8")
    secret.chmod(0o000)
    try:
        value = rm._readability(secret)
    finally:
        secret.chmod(0o644)
    if value == "READABLE":
        pytest.skip("running with privileges that ignore file mode")
    assert value == "UNREADABLE"
    assert _RI_MARKER not in value


def test_ri_no_probed_source_content_reaches_the_serialized_dashboard(tmp_path):
    """The behavioural proof the previous guard could not make.

    Every one of the four probed files is READABLE and contains the marker. The
    dashboard must report readability and nothing else."""
    root = _ri_marked_root(tmp_path / "nonleak")
    dash = rm.build_dashboard(root, now=_NOW)
    blob = json.dumps(dash, default=str)
    assert _RI_MARKER not in blob, "probed source content reached the dashboard"

    readability = dash["system_health"]["config_readability"]
    assert set(readability) == {"authority_record", "runtime_policy",
                                "outcome_ledger", "records_ledger"}
    for field, value in readability.items():
        assert value in rm.READABILITY_STATES, (field, value)
        assert value == "READABLE", field           # all four exist and decode


def test_ri_readability_is_readability_not_liveness(tmp_path):
    """The contract says file readability evidence, NOT liveness. Declaring the
    probes as dependencies must not have turned them into health."""
    root = _ri_marked_root(tmp_path / "notliveness")
    health = rm.build_dashboard(root, now=_NOW)["system_health"]
    for component in ("controller", "gpt_supervisor", "engineer_runtime",
                      "sandbox", "evidence_bridge", "control_loop"):
        assert health[component] == PENDING_BACKEND, component
    # a READABLE config must never be reported as a live component
    assert "READABLE" not in {health[c] for c in
                              ("controller", "gpt_supervisor", "control_loop")}


# ═══════════════════════════════════════════════════════════════════════════
# GUI-RI — THE AUTHORITY-RECORD EVIDENCE GATEWAY
#
# Proves the gateway itself cannot cause a dashboard crash, arbitrary payload
# exposure, or an evidence bypass. It deliberately validates nothing --
# build_worker_authority_summary remains the sole validator, and a second
# opinion here would be a second authority policy engine.
# ═══════════════════════════════════════════════════════════════════════════
def test_ri_the_authority_gateway_is_total_over_malformed_records(tmp_path):
    cases = {
        "absent": None,
        "root_null": "null", "root_list": "[1,2]", "root_int": "123",
        "root_string": '"text"', "root_bool": "true",
        "invalid_json": "{oops",
        "empty_file": "",
        "marker_root_list": json.dumps([_RI_MARKER]),
    }
    for label, body in cases.items():
        root = tmp_path / f"gw_{label}"
        (root / "config").mkdir(parents=True, exist_ok=True)
        if body is not None:
            (root / "config" / "ew0a_authority.json").write_text(
                body, encoding="utf-8")
        grants, forbidden, level = rm._read_authority_record_evidence(root)
        assert grants is rm._MISSING, label
        assert forbidden is rm._MISSING, label
        assert level is rm._MISSING, label


def test_ri_the_authority_gateway_never_partially_reads(tmp_path):
    """A malformed record yields no evidence for ALL three fields, so a caller
    cannot see a half-populated record and treat it as complete."""
    root = tmp_path / "partial"
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "ew0a_authority.json").write_bytes(b"\xff\xfe\x00binary")
    triple = rm._read_authority_record_evidence(root)
    assert all(v is rm._MISSING for v in triple)
    assert len(triple) == 3


def test_ri_the_authority_gateway_preserves_missing_null_and_empty(tmp_path):
    """Three distinguishable states, all the way to the validator. Collapsing
    them is how a projection stops being able to say 'unanswerable'."""
    root = tmp_path / "tristate"
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "ew0a_authority.json").write_text(
        json.dumps({"grants": None, "forbidden_ops": []}), encoding="utf-8")
    grants, forbidden, level = rm._read_authority_record_evidence(root)
    assert grants is None                    # explicit null
    assert forbidden == []                   # measured empty
    assert level is rm._MISSING              # absent
    assert grants is not rm._MISSING and forbidden is not rm._MISSING


def test_ri_the_authority_gateway_hands_over_raw_values_unvalidated(tmp_path):
    """It is a GATEWAY, not a validator. If it started sanitising, there would be
    two places that decide what authority evidence means."""
    root = tmp_path / "raw"
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "ew0a_authority.json").write_text(json.dumps(
        {"grants": {"nested": _RI_MARKER}, "forbidden_ops": 7,
         "level": "NOT_A_LEVEL"}), encoding="utf-8")
    grants, forbidden, level = rm._read_authority_record_evidence(root)
    assert grants == {"nested": _RI_MARKER}      # handed over as-is
    assert forbidden == 7
    assert level == "NOT_A_LEVEL"

    # ...and the CONSUMER is what keeps it out of the projection
    dash = rm.build_dashboard(root, now=_NOW)
    assert _RI_MARKER not in json.dumps(dash, default=str)
    assert dash["worker_authority"]["can_merge"] is False


def test_ri_the_gateway_cannot_escalate_authority(tmp_path):
    """Two deliberate reads of one file. The gateway reports what the record
    SAYS; only read_authority_level decides what is in force. A record claiming
    a level must not become that level through the evidence path."""
    root = tmp_path / "escalate"
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "ew0a_authority.json").write_text(json.dumps(
        {"level": "A9_TOTAL_CONTROL", "grants": ["merge"],
         "forbidden_ops": []}), encoding="utf-8")
    _, _, raw_level = rm._read_authority_record_evidence(root)
    assert raw_level == "A9_TOTAL_CONTROL"                  # the record's claim

    dash = rm.build_dashboard(root, now=_NOW)
    assert dash["worker_authority"]["level"] == "A0_DIAGNOSTIC"   # fail closed
    assert "A9_TOTAL_CONTROL" not in json.dumps(dash["worker_authority"], default=str)
    assert dash["worker_authority"]["can_merge"] is False


def test_ri_the_gateway_is_not_a_duplicate_of_the_canonical_reader():
    """Both read config/ew0a_authority.json, and the registry declares both --
    as different kinds, because they answer different questions."""
    entry = rm.DASHBOARD_PROJECTION_REGISTRY["worker_authority"]
    assert rm.RawSourceReader.AUTHORITY_LEVEL in entry.canonical_readers
    assert rm.DirectSource.AUTHORITY_RECORD_EVIDENCE in entry.direct_sources
    assert (rm.DIRECT_SOURCE_KINDS[rm.DirectSource.AUTHORITY_RECORD_EVIDENCE]
            is rm.SourceAccessKind.DIRECT_EVIDENCE_PARSE)
    # same target, two kinds -- which is why identity is (kind, target)
    assert (rm.DIRECT_SOURCE_TARGETS[rm.DirectSource.AUTHORITY_RECORD_EVIDENCE]
            == rm.DIRECT_SOURCE_TARGETS[rm.DirectSource.READABILITY_AUTHORITY_RECORD])
    assert (rm.DirectSource.AUTHORITY_RECORD_EVIDENCE.value
            != rm.DirectSource.READABILITY_AUTHORITY_RECORD.value)


def test_ri_extraction_did_not_change_authority_behaviour():
    """The extraction was for source-boundary clarity only."""
    dash = rm.build_dashboard(_REPO, now=_NOW)
    live = dash["worker_authority"]
    grants, forbidden, raw_level = rm._read_authority_record_evidence(_REPO)
    rebuilt = build_worker_authority_summary(
        rm.read_authority_level(_REPO), grants, forbidden, raw_level).to_dict()
    assert live == rebuilt


# ═══════════════════════════════════════════════════════════════════════════
# GUI-RI — the two concepts must stay distinct
# ═══════════════════════════════════════════════════════════════════════════
def test_ri_probes_were_not_forced_into_the_canonical_reader_enum():
    reader_values = {r.value for r in rm.RawSourceReader}
    for direct in rm.DirectSource:
        assert direct.value not in reader_values, direct
    assert len(rm.RawSourceReader) == 3          # A/B/C keep their identity
    kinds = {rm.DIRECT_SOURCE_KINDS[d] for d in rm.DirectSource}
    assert rm.SourceAccessKind.CANONICAL_READER not in kinds


def test_ri_every_direct_source_is_typed_and_targeted():
    for direct in rm.DirectSource:
        kind = rm.DIRECT_SOURCE_KINDS[direct]
        target = rm.DIRECT_SOURCE_TARGETS[direct]
        assert direct.value == rm.source_access_identity(kind, target), direct
    assert set(rm.DIRECT_SOURCE_KINDS) == set(rm.DirectSource)
    assert set(rm.DIRECT_SOURCE_TARGETS) == set(rm.DirectSource)


def test_ri_the_blocker_state_survives_with_no_claimants():
    assert rm.ProjectionBoundary.KNOWN_SOURCE_READER_BLOCKER in rm.ProjectionBoundary
    claimants = [n for n, e in rm.DASHBOARD_PROJECTION_REGISTRY.items()
                 if e.boundary is rm.ProjectionBoundary.KNOWN_SOURCE_READER_BLOCKER]
    assert claimants == []


# ═══════════════════════════════════════════════════════════════════════════
# GUI-RI — WHERE MECHANICAL ENFORCEMENT STOPS
#
# Stated as a test so the boundary cannot quietly be forgotten and the registry
# cannot be cited for more than it establishes.
#
# ENFORCED MECHANICALLY, at the evidence-gateway boundary:
#     build_dashboard performs no acquisition; the collector's complete call
#     multiset is closed and exact; every dependency a certified gateway
#     acquires is declared, and every declaration is acquired by one.
#
# NOT ENFORCED — per-surface attribution:
#     that `system_health` rather than some other surface consumes a given
#     probe is a DECLARED, HUMAN-REVIEWED architectural contract. It is not
#     inferred through dataflow from acquisition site to dashboard key, and
#     this deliberately does not attempt that.
#
# Each entry's `detail` therefore states its reasoning so a reviewer can check
# it against the call path. Over-attribution is the residual risk: it can
# overstate a dependency, but it cannot admit an unacquired one or hide an
# acquired one -- the gateway boundary covers both of those.
# ═══════════════════════════════════════════════════════════════════════════
def test_ri_attribution_is_declared_not_derived_and_says_so():
    """The gateway boundary is set-complete, not attribution-complete."""
    baseline = rm.declared_source_dependency_union()

    # moving a live dependency onto an extra surface does NOT change the union,
    # which is exactly the blind spot being documented
    moved = dict(rm.declared_source_dependencies())
    moved["controller"] = frozenset(
        moved["controller"] | {rm.DirectSource.READABILITY_RUNTIME_POLICY.value})
    union_after = frozenset().union(*moved.values())
    assert union_after == baseline, (
        "if this ever differs, attribution became derivable and this test "
        "should be replaced by a real attribution guard")

    # so every attributing entry must carry reasoning a reviewer can check
    for name, entry in rm.DASHBOARD_PROJECTION_REGISTRY.items():
        if entry.source_dependencies:
            assert len(entry.detail) > 40, f"{name} attributes sources without reasoning"


def test_ri_each_declared_dependency_is_attributed_to_at_least_one_surface():
    """No dependency may be declared 'in general' without an owning surface."""
    per_surface = rm.declared_source_dependencies()
    for identity in rm.declared_source_dependency_union():
        owners = [n for n, deps in per_surface.items() if identity in deps]
        assert owners, f"{identity} is declared by no surface"


def test_ri_the_registry_still_covers_every_emitted_surface():
    dash = rm.build_dashboard(_REPO, now=_NOW)
    assert set(dash) == set(rm.DASHBOARD_PROJECTION_REGISTRY)
    assert rm.registry_classification_violations() == []
    assert dash["schema_version"] == "engineering.readmodel.v1"


def test_ri_no_surface_claims_freedom_from_evidence_while_declaring_any():
    """The violation rule now spans BOTH dependency kinds. Before this commit it
    inspected canonical readers only, so a surface could claim MODULE_OWNED
    while opening a file through a probe."""
    for name, entry in rm.DASHBOARD_PROJECTION_REGISTRY.items():
        if entry.boundary.value in rm._NO_RAW_DEPENDENCY_BOUNDARIES:
            assert not entry.canonical_readers, name
            assert not entry.direct_sources, name
            assert not entry.source_dependencies, name


def test_ri_system_health_stays_hardened_and_that_is_honest():
    """Its label was checked against the call path, not chosen to clear the P2.

    HARDENED_SOURCE_READER means every dependency is total reader-or-probe
    output. That now holds for all five of system_health's: reader A is total,
    and the four probes are certified to a finite contract. The label survives
    because it became MORE true, not because it was convenient."""
    entry = rm.DASHBOARD_PROJECTION_REGISTRY["system_health"]
    assert entry.boundary is rm.ProjectionBoundary.HARDENED_SOURCE_READER
    assert entry.canonical_readers == (rm.RawSourceReader.AUTHORITY_LEVEL,)
    assert len(entry.direct_sources) == 4
    for direct in entry.direct_sources:
        assert (rm.DIRECT_SOURCE_KINDS[direct]
                is rm.SourceAccessKind.DIRECT_READABILITY_PROBE), direct
    assert "not liveness" in entry.detail


def test_ri_backend_truth_stays_hardened_and_reader_only():
    """It consumes canonical reader OUTPUT directly and reaches no file itself,
    so it declares readers and no direct sources."""
    entry = rm.DASHBOARD_PROJECTION_REGISTRY["backend_truth"]
    assert entry.boundary is rm.ProjectionBoundary.HARDENED_SOURCE_READER
    assert len(entry.canonical_readers) == 3
    assert entry.direct_sources == ()
    assert entry.boundary is not rm.ProjectionBoundary.DERIVED_FROM_REGISTERED_INPUTS


# ═══════════════════════════════════════════════════════════════════════════
# GUI-RI — CANONICAL GRANT SEMANTICS (Codex finding 3961812723)
#
# `_validated_string_list` proved `grants` was a list of strings. Nothing proved
# the strings were grants the level could hold, so a structurally perfect record
# could claim `record_evidence: LIVE` while listing grants the trusted writer
# would never emit -- an A0 worker advertising broker and capital grants,
# presented to an operator as trustworthy configuration through a REQUIRED
# capability.
#
# The same mistake as every earlier round of this review, one layer deeper:
# validating the container without validating what its contents MEAN.
#
# The canonical semantics stay owned by ew0a_authority. One pure helper,
# `grants_for_level`, expresses them; the trusted writer and this validator
# consume the SAME helper, so there is no second copy to drift.
#
# EXACT EQUALITY is deliberate. set_authority_level emits one canonical shape
# per level, so subset / superset / duplicate / reordered are all records the
# writer did not write. Certifying them as equivalent would be a read model
# inventing an authority contract nobody declared.
# ═══════════════════════════════════════════════════════════════════════════
_T_A0 = Lvl.A0_DIAGNOSTIC
_T_A1 = Lvl.A1_ASSISTED_ENGINEERING
_T_IMPOSSIBLE_MARKER = "sk-IMPOSSIBLE-GRANT-MUST-NOT-RENDER-999"


def _t_canonical(level):
    return list(rm.grants_for_level(level))


def test_grants_for_level_is_the_canonical_authority_semantics():
    """A0 grants nothing; A1 grants A1_GRANTS. Owned by the authority module."""
    from portfolio_automation.engineer_worker.ew0a_authority import A1_GRANTS
    assert rm.grants_for_level(_T_A0) == ()
    assert rm.grants_for_level(_T_A1) == A1_GRANTS
    # total over the canonical enum -- there is no unknown-level branch
    for level in Lvl:
        assert isinstance(rm.grants_for_level(level), tuple)
        assert all(isinstance(g, str) for g in rm.grants_for_level(level))


def test_the_writer_and_the_read_model_consume_the_same_helper():
    """The point of the helper: one source of truth, not two copies in step."""
    import inspect
    from portfolio_automation.engineer_worker import ew0a_authority as auth
    writer = inspect.getsource(auth.set_authority_level)
    assert "grants_for_level(level)" in writer
    assert "A1_GRANTS) if level is" not in writer, "the writer restates the mapping"
    validator = inspect.getsource(rm._validated_canonical_grants)
    assert "grants_for_level(effective)" in validator


# ── A0: grants nothing ───────────────────────────────────────────────────
def test_a0_with_no_grants_is_live(tmp_path):
    dash = _authority_dashboard(tmp_path, "a0_ok", {
        "level": "A0_DIAGNOSTIC", "grants": [], "forbidden_ops": []})
    assert dash["worker_authority"]["record_evidence"] == TruthState.LIVE.value
    assert dash["worker_authority"]["grants"] == []


@pytest.mark.parametrize("label,grants", [
    ("forbidden_op", ["BROKER_ACTION"]),
    ("capital", ["CAPITAL_DECISION"]),
    ("arbitrary", ["arbitrary-string"]),
    ("a1_grants_at_a0", None),          # filled in below: A1's set under A0
])
def test_a0_with_any_non_empty_grant_list_is_unavailable(tmp_path, label, grants):
    """A0 is read-only diagnostics. Any grant at A0 is a record the writer could
    not have produced, whether or not the string names a real operation."""
    grants = _t_canonical(_T_A1) if grants is None else grants
    dash = _authority_dashboard(tmp_path, f"a0_bad_{label}", {
        "level": "A0_DIAGNOSTIC", "grants": grants, "forbidden_ops": []})
    authority = dash["worker_authority"]
    assert authority["record_evidence"] == TruthState.UNAVAILABLE.value, label
    assert authority["grants"] == [], label
    # effective authority still comes from the canonical reader, never the record
    assert authority["level"] == "A0_DIAGNOSTIC", label


# ── A1: grants exactly A1_GRANTS ─────────────────────────────────────────
def test_a1_with_the_exact_canonical_set_is_live(tmp_path):
    dash = _authority_dashboard(tmp_path, "a1_ok", {
        "level": "A1_ASSISTED_ENGINEERING", "grants": _t_canonical(_T_A1),
        "forbidden_ops": []})
    authority = dash["worker_authority"]
    assert authority["record_evidence"] == TruthState.LIVE.value
    assert authority["grants"] == _t_canonical(_T_A1)


def _t_a1_variants():
    """Non-canonical A1 grant lists.

    Indexing is guarded so this stays collectable even if the canonical set
    changes or empties: a parametrize helper that raises at COLLECTION time
    takes the whole module down and reports as an error rather than as the
    failures it should be. Discovered while mutation-testing `grants_for_level`.
    """
    canonical = _t_canonical(_T_A1)
    variants = {
        "empty": [],
        "subset": canonical[:-1],
        "extra": canonical + ["EXTRA_GRANT"],
        "reordered": list(reversed(canonical)),
    }
    if canonical:
        variants["duplicate"] = canonical + [canonical[0]]
        variants["single"] = [canonical[0]]
    return variants


@pytest.mark.parametrize("label", sorted(_t_a1_variants()))
def test_a1_with_a_non_canonical_grant_list_is_unavailable(tmp_path, label):
    """Subset, superset, duplicate and reordered are refused alike.

    Not cosmetic strictness: the trusted writer emits ONE canonical shape, so a
    differing list means something other than the writer produced the record."""
    dash = _authority_dashboard(tmp_path, f"a1_bad_{label}", {
        "level": "A1_ASSISTED_ENGINEERING", "grants": _t_a1_variants()[label],
        "forbidden_ops": []})
    authority = dash["worker_authority"]
    assert authority["record_evidence"] == TruthState.UNAVAILABLE.value, label
    assert authority["grants"] == [], label
    assert authority["level"] == "A1_ASSISTED_ENGINEERING", label


# ── carrier corruption stays covered ─────────────────────────────────────
@pytest.mark.parametrize("label,grants", [
    ("missing", "__ABSENT__"),
    ("null", None),
    ("string", "approved"),
    ("dict", {"grants": ["a"]}),
    ("int", 7),
    ("non_string_element", ["ok", 7]),
    ("nested_list", [["ok"]]),
])
def test_malformed_grant_carriers_remain_unavailable(tmp_path, label, grants):
    record = {"level": "A1_ASSISTED_ENGINEERING", "forbidden_ops": []}
    if grants != "__ABSENT__":
        record["grants"] = grants
    dash = _authority_dashboard(tmp_path, f"carrier_{label}", record)
    assert dash["worker_authority"]["record_evidence"] == \
        TruthState.UNAVAILABLE.value, label
    assert dash["worker_authority"]["grants"] == [], label


# ── the refusal must not become a rendering path ─────────────────────────
def test_an_impossible_grant_never_reaches_the_projection(tmp_path):
    """T1. A malicious grant string must not become a GUI payload carrier just
    because validation rejected it."""
    dash = _authority_dashboard(tmp_path, "marker", {
        "level": "A0_DIAGNOSTIC", "grants": [_T_IMPOSSIBLE_MARKER],
        "forbidden_ops": []})
    authority = dash["worker_authority"]
    assert authority["record_evidence"] == TruthState.UNAVAILABLE.value
    assert _T_IMPOSSIBLE_MARKER not in json.dumps(dash, default=str)
    # the detail explains the refusal structurally, without quoting the value
    assert "canonical grant set" in authority["record_detail"]
    assert _T_IMPOSSIBLE_MARKER not in authority["record_detail"]


def test_the_refusal_detail_carries_no_grant_content(tmp_path):
    for label, grants in (("marker", [_T_IMPOSSIBLE_MARKER]),
                          ("many", [f"secret-{i}" for i in range(5)])):
        dash = _authority_dashboard(tmp_path, f"detail_{label}", {
            "level": "A0_DIAGNOSTIC", "grants": grants, "forbidden_ops": []})
        detail = dash["worker_authority"]["record_detail"]
        for value in grants:
            assert value not in detail, label
        assert "[" not in detail and "{" not in detail, label


# ── enforcement is unchanged: capabilities stay denial-derived ───────────
def test_impossible_grants_never_loosen_a_capability(tmp_path):
    """The defect was operator-truth, NOT escalation, and that must stay true.

    A record advertising every forbidden operation as a grant must leave every
    capability denied, because capabilities are derived from the denial boundary
    and never from the grant list."""
    from portfolio_automation.engineer_worker.ew0a_authority import FORBIDDEN_OPS
    dash = _authority_dashboard(tmp_path, "no_escalation", {
        "level": "A0_DIAGNOSTIC", "grants": sorted(FORBIDDEN_OPS),
        "forbidden_ops": []})
    authority = dash["worker_authority"]
    assert authority["record_evidence"] == TruthState.UNAVAILABLE.value
    for capability in ("can_merge", "can_deploy", "can_mutate_main",
                       "can_write_production", "can_self_promote"):
        assert authority[capability] is False, capability
    for op in FORBIDDEN_OPS:
        assert op in authority["forbidden_ops"], op


def test_a_stricter_record_is_still_usable_evidence(tmp_path):
    """forbidden_ops semantics are deliberately NOT changed here.

    A record may be STRICTER than the writer -- an extra denial cannot weaken
    the permanent boundary, so it stays usable evidence. An extra GRANT has the
    opposite safety meaning. Conflating the two would be a policy change this
    mission is not making."""
    dash = _authority_dashboard(tmp_path, "stricter", {
        "level": "A1_ASSISTED_ENGINEERING", "grants": _t_canonical(_T_A1),
        "forbidden_ops": ["EXTRA_DENIAL"]})
    authority = dash["worker_authority"]
    assert authority["record_evidence"] == TruthState.LIVE.value
    assert "EXTRA_DENIAL" in authority["forbidden_ops"]


# ── the trusted writer is unchanged by the deduplication ─────────────────
def test_set_authority_level_persists_the_same_payload_after_the_refactor(tmp_path):
    """§12. The helper replaced an inline conditional; the bytes must not move."""
    import json as _json
    from portfolio_automation.engineer_worker import ew0a_authority as auth
    from portfolio_automation.engineer_worker.ew0a_authority import (
        A1_GRANTS, FORBIDDEN_OPS)

    for level, expected_grants in ((_T_A0, []), (_T_A1, list(A1_GRANTS))):
        root = tmp_path / f"writer_{level.value}"
        auth.set_authority_level(root, level, actor="operator",
                                 now="2026-09-08T00:00:00+00:00")
        written = _json.loads(
            (root / auth.DEFAULT_STATE_REL).read_text(encoding="utf-8"))
        assert written["grants"] == expected_grants, level
        assert written["level"] == level.value, level
        assert written["forbidden_ops"] == sorted(FORBIDDEN_OPS), level
        assert written["actor"] == "operator", level
        assert written["updated_at"] == "2026-09-08T00:00:00+00:00", level
        assert written["schema_version"] == auth.AUTHORITY_SCHEMA_VERSION, level
        assert written["schema_kind"] == auth.SCHEMA_KIND, level
        assert set(written) == {"schema_version", "schema_kind", "level", "actor",
                                "updated_at", "grants", "forbidden_ops"}, level


def test_what_the_writer_writes_is_what_the_read_model_certifies(tmp_path):
    """The end-to-end point of sharing one helper: a record the trusted writer
    produced must always read back as LIVE evidence."""
    from portfolio_automation.engineer_worker import ew0a_authority as auth
    for level in Lvl:
        root = tmp_path / f"roundtrip_{level.value}"
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "docs").mkdir(parents=True, exist_ok=True)
        auth.set_authority_level(root, level, actor="operator", now=_NOW)
        (root / "config" / "ew0a_runtime.json").write_text(
            json.dumps({"mission_id": "m"}), encoding="utf-8")
        authority = rm.build_dashboard(root, now=_NOW)["worker_authority"]
        assert authority["record_evidence"] == TruthState.LIVE.value, level
        assert authority["level"] == level.value, level
        assert authority["grants"] == list(rm.grants_for_level(level)), level


def test_the_real_protected_record_is_canonical():
    """The repository's own authority record must satisfy the new semantics --
    if it did not, this check would be wrong rather than the record."""
    authority = rm.build_dashboard(_REPO, now=_NOW)["worker_authority"]
    assert authority["record_evidence"] == TruthState.LIVE.value
    assert authority["grants"] == list(
        rm.grants_for_level(Lvl(authority["level"])))
