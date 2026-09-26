"""GUI-1: /dashboard/mission-control — read-only Northstar Mission Control.

Certifies, against the certified read model on main:

  Rendering
    - renders 200 from certified read-model output (populated / empty / partial)
    - core sections render WITHOUT direct authoritative-file parsing
      (static: the adapter binds no IO facility; dynamic: page renders against
      a repo root that does not exist once build_dashboard is stubbed)
  Missing backend
    - PENDING_BACKEND stays visible and explicit
    - missing worker heartbeat / supervisor telemetry / component health are
      never shown as healthy (never a green badge, never health words)
  Authority
    - presentation only: no form, no button, no non-GET route; POST -> 405
    - malformed / non-canonical authority stays fail-closed on the page
  Learning
    - UNAVAILABLE_PENDING_CERTIFICATION quarantine is visible and preserved
  Read-only
    - rendering changes no byte and creates no file in the repository
  Determinism
    - same dashboard dict -> same view; same repo + fixed now -> same view
  Failure behaviour
    - malformed evidence degrades to UNAVAILABLE, never to a stack trace
    - a raising read model yields an honest banner that echoes no payload
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import gui_v2.app as app_module
from gui_v2.data import dash_mission_control as mc
from portfolio_automation.engineer_worker import ew0a_readmodels as rm
from portfolio_automation.engineer_worker import ew0a_authority as au

_REPO = Path(__file__).resolve().parents[1]
_NOW = "2026-09-20T09:30:00+00:00"
_ROUTE = "/dashboard/mission-control"
_TEMPLATE = _REPO / "gui_v2" / "templates" / "dashboard" / "mission_control.html"
_ADAPTER = _REPO / "gui_v2" / "data" / "dash_mission_control.py"

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
_GOOD_RECORD = {"kind": "ApprenticeshipComparison", "gpt_verdict": "PASS",
                "recorded_at": "2026-08-15T20:45:22+00:00"}

#: Words that would misreport an absent producer as a healthy component.
_HEALTH_WORDS = ("healthy", "online", "idle")
#: A green badge immediately followed by an absence state.
_GREEN_ABSENCE = tuple(f'border-emerald-500/30">{s}' for s in
                       ("PENDING_BACKEND", "UNAVAILABLE", "UNKNOWN"))
_FORBIDDEN_RENDERED = ("execute trade", "buy now", "sell now", "place order",
                       "auto-trade", "auto trade", "auto-approve")


# ---------------------------------------------------------------------------
# Fixture helpers (repo roots built from real protected config + ledgers)
# ---------------------------------------------------------------------------

def _row(**extra):
    base = dict(_VALID_ROW)
    base.update(extra)
    return base


def _root(tmp_path: Path, *, authority=None, runtime=None, outcomes=None,
          records=None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "docs").mkdir(exist_ok=True)
    for name, override in (("ew0a_authority.json", authority),
                           ("ew0a_runtime.json", runtime)):
        target = tmp_path / "config" / name
        if override is None:
            shutil.copy(_REPO / "config" / name, target)
        else:
            target.write_text(override, encoding="utf-8")
    if outcomes is not None:
        (tmp_path / rm.OUTCOME_LEDGER_REL).write_text(
            "".join(json.dumps(r) + "\n" for r in outcomes), encoding="utf-8")
    if records is not None:
        (tmp_path / rm.CONTROLLER_RECORDS_REL).write_text(records, encoding="utf-8")
    return tmp_path


def _populated(tmp_path: Path) -> Path:
    return _root(tmp_path, outcomes=[
        _row(),
        _row(task_id="T2", final_status="REPAIR_REQUIRED",
             failure_classes=["TEST_FAILURE"], recorded_at="2026-08-11T12:00:05Z"),
    ], records=json.dumps(_GOOD_RECORD) + "\n")


def _snapshot(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def client(monkeypatch, tmp_path):
    """TestClient whose app is pointed at a per-test repo root."""
    def _make(root: Path) -> TestClient:
        monkeypatch.setattr(app_module, "REPO_ROOT", root)
        return TestClient(app_module.app)
    return _make


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_populated_repo_renders_every_section(client, tmp_path):
    root = _populated(tmp_path)
    r = client(root).get(_ROUTE)
    assert r.status_code == 200
    html = r.text
    assert "Mission Control" in html
    for label in ("Mission", "Session", "Authority", "Workers and components",
                  "Readiness and evidence", "Run history", "Attention required",
                  "Failures and blockers", "Unavailable or pending information",
                  "Provenance"):
        assert f'aria-label="{label}"' in html, label
    # facts from the read model reach the page unchanged
    policy = json.loads((root / "config" / "ew0a_runtime.json").read_text())
    assert policy["mission_id"] in html
    assert "T1" in html and "T2" in html and "TEST_FAILURE" in html
    assert "engineering.readmodel.v1" in html
    assert "Observe-only" in html


def test_empty_repository_renders_and_hides_nothing(client, tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    r = client(root).get(_ROUTE)
    assert r.status_code == 200
    html = r.text
    assert "PENDING_BACKEND" in html
    assert "UNAVAILABLE" in html
    assert 'aria-label="Read model unavailable"' not in html  # the read model answered
    for w in _GREEN_ABSENCE:
        assert w not in html


def test_partial_evidence_renders(client, tmp_path):
    """Authority present, nothing else: the page still renders and says what
    is missing rather than failing or filling in."""
    root = _root(tmp_path)
    view = mc.collect_mission_control_view(root, _NOW)
    assert view["read_model_status"] == "LIVE"
    assert view["run_session"]["state"] == "UNAVAILABLE"      # absent ledger: UNAVAILABLE
    assert view["authority"]["state"] == "LIVE"
    r = client(root).get(_ROUTE)
    assert r.status_code == 200


def test_view_contract_is_observe_only_mission_control(tmp_path):
    view = mc.collect_mission_control_view(_populated(tmp_path), _NOW)
    assert view["persona"] == "mission_control"
    assert view["observe_only"] is True
    assert view["route"] == _ROUTE
    assert view["read_model_schema"]["schema_version"] == "engineering.readmodel.v1"


# ---------------------------------------------------------------------------
# No direct authoritative-file interpretation
# ---------------------------------------------------------------------------

def test_adapter_binds_no_io_facility_and_imports_only_the_read_model():
    """Static: the module's only import is the read model; it never binds a
    file, path, json, os or subprocess facility. (Bindings, not text.)"""
    tree = ast.parse(_ADAPTER.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported == {"__future__", "typing",
                        "portfolio_automation.engineer_worker.ew0a_readmodels"}, imported
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                called.add(f.id)
            elif isinstance(f, ast.Attribute):
                called.add(f.attr)
    forbidden = {"open", "read_text", "read_bytes", "write_text", "write_bytes",
                 "load", "loads", "dump", "dumps", "Path", "stat", "exists",
                 "glob", "rglob", "iterdir", "mkdir", "unlink", "rename",
                 "run", "Popen", "system", "connect", "urlopen"}
    assert not (called & forbidden), called & forbidden


def test_page_renders_from_read_model_output_alone(client, tmp_path, monkeypatch):
    """Dynamic: stub build_dashboard and point the app at a repo root that does
    not exist. If the page renders with the stubbed facts, the GUI read nothing
    itself."""
    canned = rm.build_dashboard(_populated(tmp_path / "src"), _NOW)
    canned["controller"]["current_mission"] = "mission-from-read-model-only"
    monkeypatch.setattr(mc, "build_dashboard", lambda root, now=None: copy.deepcopy(canned))
    ghost = tmp_path / "does-not-exist"
    assert not ghost.exists()
    r = client(ghost).get(_ROUTE)
    assert r.status_code == 200
    assert "mission-from-read-model-only" in r.text
    assert not ghost.exists()


# ---------------------------------------------------------------------------
# Missing backend stays explicit
# ---------------------------------------------------------------------------

def test_missing_heartbeat_supervisor_and_component_health_stay_pending(client, tmp_path):
    root = _populated(tmp_path)
    view = mc.collect_mission_control_view(root, _NOW)
    by_card = {c["title"]: {f["key"]: f for f in c["fields"]} for c in view["workers"]}
    for key in ("operational_state", "current_task", "queue_size", "activity_summary",
                "next_action"):
        f = by_card["Engineer worker"][key]
        assert f["state"] == "PENDING_BACKEND" and f["value"] == "PENDING_BACKEND", key
        assert f["severity"] != "green"
    for key in ("availability", "current_state", "measured_latency_ms",
                "verification_queue", "outage_state"):
        f = by_card["GPT supervisor"][key]
        assert f["state"] == "PENDING_BACKEND", key
        assert f["severity"] != "green"
    for key in ("controller_since", "operational_state"):
        assert by_card["Controller"][key]["state"] == "PENDING_BACKEND", key
    assert by_card["Controller"]["identity_basis"]["state"] == "PENDING_BACKEND"
    for f in view["system_health"]["fields"]:
        assert f["state"] == "PENDING_BACKEND", f["key"]
        assert f["severity"] != "green"
    # readability is labelled as readability, never as liveness
    for f in view["system_health"]["readability"]:
        assert "never liveness" in f["note"]
    html = client(root).get(_ROUTE).text.lower()
    for w in _HEALTH_WORDS:
        assert w not in html, w
    for w in _GREEN_ABSENCE:
        assert w.lower() not in html


def test_state_severity_never_maps_absence_to_green():
    for state in ("PENDING_BACKEND", "UNAVAILABLE", "UNKNOWN", "STALE"):
        assert mc.STATE_SEVERITY[state] != "green", state
        assert mc.state_severity(state) != "green"
    assert mc.state_severity("something-new") == "gray"
    assert mc.state_severity("LIVE") == "green"


def test_unavailable_inventory_names_the_known_backend_gaps(tmp_path):
    view = mc.collect_mission_control_view(_populated(tmp_path), _NOW)
    inv = {(u["section"], u["field"]): u["state"] for u in view["unavailable"]}
    for section, field in (("worker", "queue_size"), ("worker", "activity_summary"),
                           ("supervisor", "availability"),
                           ("supervisor", "measured_latency_ms"),
                           ("controller", "controller_since"),
                           ("system_health", "controller"),
                           ("attention", "derivation_state")):
        assert inv.get((section, field)) == "PENDING_BACKEND", (section, field)
    assert inv.get(("learning", "truth_state")) == "UNAVAILABLE"
    # sorted, hence stable
    keys = [(u["section"], u["field"], u["state"]) for u in view["unavailable"]]
    assert keys == sorted(keys)


def test_attention_never_claims_all_clear_while_derivation_is_pending(tmp_path):
    view = mc.collect_mission_control_view(_populated(tmp_path), _NOW)
    att = view["attention"]
    assert att["state"] == "PENDING_BACKEND"
    assert att["zero_items_is_authoritative"] is False
    hero = {h["key"]: h for h in view["hero"]}
    assert hero["attention"]["value"] == "PENDING_BACKEND"
    assert "item(s)" not in hero["attention"]["value"]
    assert hero["attention"]["severity"] != "green"


# ---------------------------------------------------------------------------
# Authority: presentation only, fail-closed
# ---------------------------------------------------------------------------

def test_template_and_app_offer_no_control_surface():
    src = _TEMPLATE.read_text(encoding="utf-8")
    for token in ("<form", "<button", "<input", "<select", "<textarea",
                  "hx-post", "hx-put", "hx-delete", "hx-patch", "method="):
        assert token not in src, token
    routes = [r for r in app_module.app.routes if getattr(r, "path", "") == _ROUTE]
    assert routes, "route missing"
    for r in routes:
        assert set(r.methods) <= {"GET", "HEAD"}, r.methods
    assert not any(getattr(r, "path", "").startswith(_ROUTE + "/")
                   for r in app_module.app.routes)


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_mutating_methods_are_rejected(client, tmp_path, method):
    c = client(_populated(tmp_path))
    r = c.request(method.upper(), _ROUTE, data={"level": "A1_ASSISTED_ENGINEERING"})
    assert r.status_code == 405


def test_non_canonical_authority_record_fails_closed_on_the_page(client, tmp_path):
    """A0 carrying A1's grants is a record the trusted writer could never have
    written. The read model refuses it; the page must show that refusal, not
    the grants."""
    root = _root(tmp_path)
    au.set_authority_level(root, au.EngineerAuthorityLevel.A0_DIAGNOSTIC,
                           actor="operator", now=_NOW)
    p = root / au.DEFAULT_STATE_REL
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["grants"] = list(au.A1_GRANTS)
    p.write_text(json.dumps(rec), encoding="utf-8")
    view = mc.collect_mission_control_view(root, _NOW)
    assert view["authority"]["state"] == "UNAVAILABLE"
    assert view["authority"]["grants"] == []
    assert {f["key"]: f["value"] for f in view["authority"]["fields"]}["level"] == "A0_DIAGNOSTIC"
    assert any(f["kind"] == "authority" for f in view["failures"])
    html = client(root).get(_ROUTE).text
    for grant in au.A1_GRANTS:
        assert grant not in html, grant
    assert "A0_DIAGNOSTIC" in html
    assert "unusable" in html  # the read model's own refusal detail


def test_escalated_level_in_record_is_not_projected(tmp_path):
    root = _root(tmp_path)
    p = root / au.DEFAULT_STATE_REL
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["level"] = "A9_TOTAL_CONTROL"
    p.write_text(json.dumps(rec), encoding="utf-8")
    view = mc.collect_mission_control_view(root, _NOW)
    assert "A9_TOTAL_CONTROL" not in json.dumps(view)
    assert view["authority"]["state"] == "UNAVAILABLE"
    caps = {f["key"]: f for f in view["authority"]["capabilities"]}
    assert all(f["raw"] is False for f in caps.values())
    assert all(f["state"] == "DERIVED" for f in caps.values())


# ---------------------------------------------------------------------------
# Learning quarantine
# ---------------------------------------------------------------------------

def test_learning_quarantine_is_visible_and_untouched(client, tmp_path):
    root = _populated(tmp_path)
    dashboard = rm.build_dashboard(root, _NOW)
    view = mc.project_mission_control(dashboard, _NOW)
    assert dashboard["learning"]["truth_state"] == "UNAVAILABLE"
    assert view["learning"]["state"] == "UNAVAILABLE"
    assert view["learning"]["severity"] != "green"
    assert view["learning"]["detail"] == dashboard["learning"]["detail"]
    html = client(root).get(_ROUTE).text
    assert "learning UNAVAILABLE" in html
    assert "not yet certified" in html
    # the quarantined payload is not reconstructed by the GUI
    assert set(view["learning"]) == {"state", "severity", "freshness", "detail"}


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------

def test_rendering_writes_nothing_and_creates_no_file(client, tmp_path):
    root = _populated(tmp_path)
    before = _snapshot(root)
    c = client(root)
    for _ in range(2):
        assert c.get(_ROUTE).status_code == 200
        mc.collect_mission_control_view(root, _NOW)
    assert _snapshot(root) == before


def test_rendering_an_empty_root_creates_nothing(client, tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    assert client(root).get(_ROUTE).status_code == 200
    assert list(root.rglob("*")) == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_projection_is_deterministic_for_a_fixed_dashboard(tmp_path):
    dashboard = rm.build_dashboard(_populated(tmp_path), _NOW)
    a = mc.project_mission_control(copy.deepcopy(dashboard), _NOW)
    b = mc.project_mission_control(copy.deepcopy(dashboard), _NOW)
    assert a == b
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def test_collect_is_deterministic_for_fixed_repo_and_time(tmp_path):
    root = _populated(tmp_path)
    a = mc.collect_mission_control_view(root, _NOW)
    b = mc.collect_mission_control_view(root, _NOW)
    assert a == b


# ---------------------------------------------------------------------------
# Failure behaviour
# ---------------------------------------------------------------------------

def test_malformed_evidence_degrades_to_unavailable_not_a_stack_trace(client, tmp_path):
    root = _root(tmp_path, authority="{oops", records="[1,2]\n",
                 outcomes=[_row(failure_classes=123)])
    view = mc.collect_mission_control_view(root, _NOW)
    assert view["read_model_status"] == "LIVE"
    assert view["authority"]["state"] == "UNAVAILABLE"
    assert view["run_session"]["state"] == "UNAVAILABLE"
    assert view["run_session"]["runs"] == []
    kinds = {(f["kind"], f["ref"]) for f in view["failures"]}
    assert ("authority", "worker_authority") in kinds
    r = client(root).get(_ROUTE)
    assert r.status_code == 200
    assert "Traceback" not in r.text


def test_raising_read_model_yields_honest_banner_without_echoing_payload(client, tmp_path, monkeypatch):
    def boom(root, now=None):
        raise RuntimeError("sk-SECRET-PAYLOAD-MUST-NOT-RENDER")
    monkeypatch.setattr(mc, "build_dashboard", boom)
    r = client(_populated(tmp_path)).get(_ROUTE)
    assert r.status_code == 200
    assert 'aria-label="Read model unavailable"' in r.text
    assert "RuntimeError" in r.text
    assert "SECRET-PAYLOAD" not in r.text
    view = mc.collect_mission_control_view(tmp_path, _NOW)
    assert view["read_model_status"] == "UNAVAILABLE"
    assert all(h["severity"] != "green" for h in view["hero"])
    assert view["mission"]["fields"] == [] and view["workers"] == []


# ---------------------------------------------------------------------------
# Organisation of failures / blockers (from published fields only)
# ---------------------------------------------------------------------------

def _synthetic_dashboard(tmp_path: Path, **session_overrides):
    d = rm.build_dashboard(_populated(tmp_path), _NOW)
    session = {
        "read_model": "Northstar0CSessionSummary", "schema_kind": "experimental_noncanonical",
        "session_id": "s1", "recorded_session_id": "s1", "identity_corrected": False,
        "mission_id": "m-other", "session_objective": "obj",
        "session_started_at": "2026-08-16T07:17:41+00:00",
        "starting_main_sha": "0" * 40, "session_state": "BLOCKED",
        "current_task_id": "t1", "current_task_title": None, "current_stage": "VERIFYING",
        "tasks_attempted": 2, "tasks_verified": 1, "tasks_repaired": 1,
        "tasks_escalated": 0, "tasks_abstained": 0, "tasks_incomplete": 0,
        "blockers": ["waiting on human"], "known_sessions": ["s1"],
        "authority": "A1_ASSISTED_ENGINEERING", "c1_status": "DISABLED",
        "auto_merge": False, "production_mutation": False, "capital_action": False,
        "worker_heartbeat": "PENDING_BACKEND", "supervisor_latency_ms": "PENDING_BACKEND",
        "session_present": True, "truth_state": "UNKNOWN",
        "runtime_mission_id": d["controller"]["current_mission"],
        "mission_consistency": "MISMATCH",
        "consistency_detail": "session mission 'm-other' is NOT the runtime mission",
        "safe_to_present_as_current_work": False,
        "freshness_evidence": "start time only",
    }
    session.update(session_overrides)
    d["active_session"] = session
    return d


def test_blocked_mismatched_session_is_surfaced_and_not_presented_as_current_work(tmp_path):
    view = mc.project_mission_control(_synthetic_dashboard(tmp_path), _NOW)
    s = view["mission"]["session"]
    assert s["present"] and s["session_state"] == "BLOCKED"
    assert s["safe_to_present_as_current_work"] is False
    assert s["mission_consistency"] == "MISMATCH"
    refs = {(f["kind"], f["ref"]) for f in view["failures"]}
    assert ("session", "session_state") in refs
    assert ("session_blocker", "blockers") in refs
    assert ("session", "mission_consistency") in refs
    assert ("run", "T2") in refs            # REPAIR_REQUIRED with TEST_FAILURE
    assert ("run", "T1") not in refs        # VERIFIED is not a failure
    heartbeat = {f["key"]: f for f in s["fields"]}["worker_heartbeat"]
    assert heartbeat["state"] == "PENDING_BACKEND"


def test_session_rendering_labels_non_current_work(client, tmp_path, monkeypatch):
    canned = _synthetic_dashboard(tmp_path)
    monkeypatch.setattr(mc, "build_dashboard", lambda root, now=None: copy.deepcopy(canned))
    html = client(tmp_path).get(_ROUTE).text
    assert "NOT current work" in html
    assert "session BLOCKED" in html
    assert "mission MISMATCH" in html
    assert "waiting on human" in html


def test_agreeing_running_session_carries_no_session_failures(tmp_path):
    d = _synthetic_dashboard(tmp_path, session_state="RUNNING", blockers=[],
                             mission_consistency="AGREES", consistency_detail="",
                             safe_to_present_as_current_work=True,
                             mission_id=None)
    d["active_session"]["mission_id"] = d["controller"]["current_mission"]
    view = mc.project_mission_control(d, _NOW)
    assert not [f for f in view["failures"] if f["kind"].startswith("session")]
    assert view["mission"]["session"]["safe_to_present_as_current_work"] is True


# ---------------------------------------------------------------------------
# Shell integration
# ---------------------------------------------------------------------------

def test_navigation_links_to_mission_control(client, tmp_path):
    html = client(_populated(tmp_path)).get("/dashboard/system").text
    assert 'href="/dashboard/mission-control"' in html
    assert "Mission Control" in html


def test_no_forbidden_action_labels_rendered(client, tmp_path):
    html = client(_populated(tmp_path)).get(_ROUTE).text.lower()
    for label in _FORBIDDEN_RENDERED:
        assert label not in html, label
    src = _TEMPLATE.read_text(encoding="utf-8").lower()
    for label in _FORBIDDEN_RENDERED + ("restart", "retry", "elevate"):
        assert label not in src, label


# ---------------------------------------------------------------------------
# Freshness through the REAL HTTP route (PR #48 review repair)
#
# The escaped defect: the route called the adapter without a reference instant,
# and control_center_truth.classify() deliberately answers UNKNOWN when `now`
# is absent, so a real page load could never distinguish a fresh verdict from a
# stale one although adapter tests that pass a fixed _NOW could. The route now
# supplies an aware UTC instant; the read model still does ALL classification.
# ---------------------------------------------------------------------------

import re as _re
from datetime import datetime as _dt, timedelta as _td, timezone as _tz


def _records_root(tmp_path: Path, recorded_at: str) -> Path:
    """A repo root whose controller-records ledger holds one PASS verdict."""
    row = dict(_GOOD_RECORD, recorded_at=recorded_at)
    return _root(tmp_path, records=json.dumps(row) + "\n")


def _capability_state(html: str, capability: str) -> str:
    """The badge label in the readiness table row for `capability`."""
    m = _re.search(
        rf'<td[^>]*>{capability}</td>\s*<td[^>]*>.*?>([A-Z_]+)</span>', html, _re.S)
    assert m, f"capability row {capability!r} not rendered"
    return m.group(1)


def test_reference_now_is_an_aware_utc_iso_instant():
    raw = app_module._mission_control_reference_now()
    parsed = _dt.fromisoformat(raw)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == _td(0)
    assert abs((_dt.now(_tz.utc) - parsed).total_seconds()) < 5


def test_http_fresh_verification_is_live(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "_mission_control_reference_now", lambda: _NOW)
    fresh = (_dt.fromisoformat(_NOW) - _td(hours=1)).isoformat()
    html = client(_records_root(tmp_path, fresh)).get(_ROUTE).text
    assert _capability_state(html, "supervisor_state") == "LIVE"
    assert _NOW in html  # the reference instant is shown to the operator


def test_http_stale_verification_is_stale(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "_mission_control_reference_now", lambda: _NOW)
    stale = (_dt.fromisoformat(_NOW) - _td(days=3)).isoformat()
    html = client(_records_root(tmp_path, stale)).get(_ROUTE).text
    assert _capability_state(html, "supervisor_state") == "STALE"
    # a STALE required capability is organised into Failures & blockers
    assert _re.search(r'aria-label="Failures and blockers".*?supervisor_state', html, _re.S)


def test_http_real_clock_path_classifies_fresh_evidence_live(client, tmp_path):
    """No monkeypatch: the shipped helper, the shipped route, the shipped read model."""
    recent = (_dt.now(_tz.utc) - _td(minutes=5)).isoformat()
    html = client(_records_root(tmp_path, recent)).get(_ROUTE).text
    assert _capability_state(html, "supervisor_state") == "LIVE"


def test_http_naive_or_invalid_timestamp_stays_unknown(client, tmp_path, monkeypatch):
    """A reference instant does not let the read model invent an age it cannot
    measure: a naive or unparseable timestamp is still UNKNOWN, not STALE."""
    monkeypatch.setattr(app_module, "_mission_control_reference_now", lambda: _NOW)
    for label, ts in (("naive", "2026-09-20T08:30:00"), ("garbage", "not-a-time")):
        html = client(_records_root(tmp_path / label, ts)).get(_ROUTE).text
        assert _capability_state(html, "supervisor_state") == "UNKNOWN", label


def test_http_without_reference_instant_is_unknown_which_is_the_escaped_defect(
        client, tmp_path, monkeypatch):
    """Documents the read model's rule the route must satisfy: no `now`, no
    freshness verdict. This is what every real request used to do."""
    monkeypatch.setattr(app_module, "_mission_control_reference_now", lambda: None)
    fresh = (_dt.fromisoformat(_NOW) - _td(hours=1)).isoformat()
    html = client(_records_root(tmp_path, fresh)).get(_ROUTE).text
    assert _capability_state(html, "supervisor_state") == "UNKNOWN"


def test_http_pending_and_unavailable_are_untouched_by_the_reference_instant(
        client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "_mission_control_reference_now", lambda: _NOW)
    fresh = (_dt.fromisoformat(_NOW) - _td(hours=1)).isoformat()
    html = client(_records_root(tmp_path, fresh)).get(_ROUTE).text
    for cap in ("worker_activity", "queue_state", "component_health",
                "controller_since", "attention_derivation", "controller_identity"):
        assert _capability_state(html, cap) == "PENDING_BACKEND", cap
    assert _capability_state(html, "learning") == "UNAVAILABLE"
    assert _capability_state(html, "run_history") == "UNAVAILABLE"  # no outcome ledger here


def test_gui_holds_no_freshness_rule_of_its_own():
    """The GUI passes an instant and renders a verdict. It never computes an age,
    never holds a threshold and never classifies."""
    import inspect
    import textwrap

    def calls(src: str) -> set[str]:
        out: set[str] = set()
        for node in ast.walk(ast.parse(textwrap.dedent(src))):
            if isinstance(node, ast.Call):
                f = node.func
                out.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
        return out

    def names(src: str) -> set[str]:
        return {n.id for n in ast.walk(ast.parse(textwrap.dedent(src)))
                if isinstance(n, ast.Name)}

    adapter_src = _ADAPTER.read_text(encoding="utf-8")
    template_src = _TEMPLATE.read_text(encoding="utf-8")
    route_src = inspect.getsource(app_module.page_dash_mission_control)
    helper_src = inspect.getsource(app_module._mission_control_reference_now)
    freshness_machinery = {"timedelta", "total_seconds", "classify", "FRESHNESS_SECONDS",
                           "fromisoformat", "_parse"}
    for src, name in ((adapter_src, "adapter"), (route_src, "route"), (helper_src, "helper")):
        assert not (calls(src) & freshness_machinery), (name, calls(src) & freshness_machinery)
        assert not (names(src) & freshness_machinery), (name, names(src) & freshness_machinery)
    for token in ("total_seconds", "timedelta", "now -", "- now"):
        assert token not in template_src, token
    # the route hands the instant over; the helper only reads the clock
    assert "_mission_control_reference_now" in calls(route_src)
    assert calls(helper_src) <= {"now", "isoformat"}, calls(helper_src)
    # the adapter still consumes build_dashboard and nothing else (see the AST test)
    assert "build_dashboard(repo_root, now)" in adapter_src


# ---------------------------------------------------------------------------
# Supervisor verification freshness vs ledger usability (PR #48 review, P2)
#
# Two different questions about the same supervisor evidence:
#   supervisor.records_evidence     -> was the controller-records LEDGER usable?
#   backend_truth.supervisor_state  -> is the latest verification FRESH?
# A readable ledger holding an old PASS is LIVE on the first axis and STALE on
# the second. The "Last successful verification" row must carry the second,
# published by the read model; the verdict COUNTS stay on the first.
# ---------------------------------------------------------------------------

def _run_fields(view):
    return {f["key"]: f for f in view["run_session"]["fields"]}


def _with_supervisor_state(dashboard, state):
    d = copy.deepcopy(dashboard)
    for c in d["backend_truth"]["capabilities"]:
        if c["capability"] == "supervisor_state":
            c["state"] = state
    return d


def _stale_ledger_dashboard(tmp_path):
    """A REAL read-model dashboard: readable ledger, PASS verdict 3 days old."""
    stale = (_dt.fromisoformat(_NOW) - _td(days=3)).isoformat()
    return rm.build_dashboard(_records_root(tmp_path, stale), _NOW)


def test_last_verification_row_uses_published_supervisor_state_not_ledger_usability(tmp_path):
    """Case A: records_evidence=LIVE, supervisor_state=STALE -> row STALE, not green;
    verdict counts stay on ledger usability (LIVE)."""
    d = _stale_ledger_dashboard(tmp_path)
    assert d["supervisor"]["records_evidence"] == "LIVE"          # ledger usable
    assert _capability_state_in(d, "supervisor_state") == "STALE"  # verification old
    view = mc.project_mission_control(d, _NOW)
    row = _run_fields(view)["last_successful_verification"]
    assert row["state"] == "STALE"
    assert row["severity"] != "green"
    assert "backend_truth.supervisor_state" in row["note"]
    counts = _run_fields(view)["supervisor_recent"]
    assert counts["state"] == "LIVE"                               # different question


def test_fresh_verification_with_live_ledger_is_live(tmp_path):
    """Case B."""
    fresh = (_dt.fromisoformat(_NOW) - _td(hours=1)).isoformat()
    d = rm.build_dashboard(_records_root(tmp_path, fresh), _NOW)
    assert _capability_state_in(d, "supervisor_state") == "LIVE"
    view = mc.project_mission_control(d, _NOW)
    assert _run_fields(view)["last_successful_verification"]["state"] == "LIVE"
    assert _run_fields(view)["supervisor_recent"]["state"] == "LIVE"


def test_unknown_verification_freshness_is_shown_as_unknown(tmp_path):
    """Case C: the read model says UNKNOWN (no instant, or an unmeasurable
    timestamp); the GUI shows UNKNOWN and infers nothing else."""
    fresh = (_dt.fromisoformat(_NOW) - _td(hours=1)).isoformat()
    d = rm.build_dashboard(_records_root(tmp_path, fresh), None)   # no reference instant
    assert d["supervisor"]["records_evidence"] == "LIVE"
    assert _capability_state_in(d, "supervisor_state") == "UNKNOWN"
    view = mc.project_mission_control(d, None)
    row = _run_fields(view)["last_successful_verification"]
    assert row["state"] == "UNKNOWN" and row["severity"] != "green"


def test_gui_carries_every_published_supervisor_state_verbatim(tmp_path):
    """The row is a copy of the published state for every value the read model
    can publish -- including ones the fixture cannot naturally produce."""
    base = _stale_ledger_dashboard(tmp_path)
    for state in mc.READ_MODEL_STATES:
        view = mc.project_mission_control(_with_supervisor_state(base, state), _NOW)
        row = _run_fields(view)["last_successful_verification"]
        assert row["state"] == state, state
        assert row["severity"] == mc.state_severity(state)
        if state != "LIVE":
            assert row["severity"] != "green", state


def test_unpublished_or_unrecognised_supervisor_state_is_unavailable_not_live(tmp_path):
    """Case D: absence of the published capability is not health."""
    base = _stale_ledger_dashboard(tmp_path)
    no_cap = copy.deepcopy(base)
    no_cap["backend_truth"]["capabilities"] = [
        c for c in no_cap["backend_truth"]["capabilities"] if c["capability"] != "supervisor_state"]
    for d in (no_cap, _with_supervisor_state(base, "HEALTHY"), {**base, "backend_truth": "x"}):
        row = _run_fields(mc.project_mission_control(d, _NOW))["last_successful_verification"]
        assert row["state"] == "UNAVAILABLE"
        assert row["severity"] != "green"


def test_unusable_ledger_keeps_both_dimensions_unavailable(tmp_path):
    """Case D: a corrupt ledger -> records_evidence UNAVAILABLE, no verification
    value -> supervisor_state UNAVAILABLE; neither becomes LIVE or PENDING."""
    d = rm.build_dashboard(_root(tmp_path, records="[1,2]\n"), _NOW)
    assert d["supervisor"]["records_evidence"] == "UNAVAILABLE"
    assert _capability_state_in(d, "supervisor_state") == "UNAVAILABLE"
    fields = _run_fields(mc.project_mission_control(d, _NOW))
    assert fields["last_successful_verification"]["state"] == "UNAVAILABLE"
    assert fields["supervisor_recent"]["state"] == "UNAVAILABLE"


def test_http_page_shows_one_freshness_answer_for_the_supervisor_evidence(
        client, tmp_path, monkeypatch):
    """Case E: the rendered page's readiness table and its 'Last successful
    verification' row agree (both STALE), and the row is not a green badge."""
    monkeypatch.setattr(app_module, "_mission_control_reference_now", lambda: _NOW)
    stale = (_dt.fromisoformat(_NOW) - _td(days=3)).isoformat()
    html = client(_records_root(tmp_path, stale)).get(_ROUTE).text
    assert _capability_state(html, "supervisor_state") == "STALE"
    row = _re.search(
        r'Last successful verification</div>.*?</div>\s*<div class="flex-shrink-0">.*?>([A-Z_]+)</span>',
        html, _re.S)
    assert row, "row not rendered"
    assert row.group(1) == "STALE"
    row_html = _re.search(r'Last successful verification</div>.*?</span>', html, _re.S).group(0)
    assert "border-emerald-500/30" not in row_html


def _capability_state_in(dashboard, capability):
    return next(c["state"] for c in dashboard["backend_truth"]["capabilities"]
                if c["capability"] == capability)


# ---------------------------------------------------------------------------
# Truth preservation: readiness and authority evidence (PR #48 review, round 4)
#
# Codex A: backend_truth.readiness == PARTIAL was relabelled LIVE (readiness
#          row and hero) because PARTIAL is not a TruthState and the fallback
#          was a positive literal.
# Codex B: the worker card's "Authority" row hard-coded LIVE, so a valid A1
#          level with refused (non-canonical) grants rendered a green LIVE
#          badge beside record_evidence=UNAVAILABLE on the same page.
# Sweep:   every other literal-LIVE fallback in the adapter is replaced by the
#          published backend_truth capability state; a missing or unrecognised
#          state fails closed to UNAVAILABLE.
# ---------------------------------------------------------------------------

READINESS_VALUES = ("READY", "MOSTLY_LIVE", "PARTIAL", "UNAVAILABLE")


def _with_readiness(dashboard, value):
    d = copy.deepcopy(dashboard)
    d["backend_truth"]["readiness"] = value
    return d


def _with_capability(dashboard, capability, state):
    d = copy.deepcopy(dashboard)
    for c in d["backend_truth"]["capabilities"]:
        if c["capability"] == capability:
            c["state"] = state
    return d


def _hero_by_key(view):
    return {h["key"]: h for h in view["hero"]}


def _readiness_row(view):
    return {f["key"]: f for f in view["readiness"]["fields"]}["readiness"]


def _worker_field(view, card_title, key):
    card = next(c for c in view["workers"] if c["title"] == card_title)
    return {f["key"]: f for f in card["fields"]}[key]


def test_partial_readiness_is_never_relabelled_live(client, tmp_path):
    """Codex A. The populated fixture is genuinely PARTIAL (worker_activity has
    no producer). PARTIAL must stay PARTIAL in the row, the hero and the HTML."""
    d = rm.build_dashboard(_populated(tmp_path), _NOW)
    assert d["backend_truth"]["readiness"] == "PARTIAL"
    view = mc.project_mission_control(d, _NOW)
    row = _readiness_row(view)
    assert row["state"] == "PARTIAL" and row["severity"] != "green"
    hero = _hero_by_key(view)["readiness"]
    assert hero["state"] == "PARTIAL" and hero["severity"] != "green"
    assert view["readiness"]["readiness_severity"] == mc._READINESS_SEVERITY["PARTIAL"]
    html = client(_populated(tmp_path / "http")).get(_ROUTE).text
    hero_html = _re.search(r'<span[^>]*>Readiness</span>.*?</div>', html, _re.S).group(0)
    assert "PARTIAL" in hero_html and "LIVE" not in hero_html
    row_html = _re.search(r'Readiness</div>.*?</span>', html, _re.S).group(0)
    assert ">PARTIAL</span>" in row_html and "LIVE" not in row_html
    assert "border-emerald-500/30" not in row_html


def test_every_readiness_value_maps_through_the_readiness_taxonomy(tmp_path):
    base = rm.build_dashboard(_populated(tmp_path), _NOW)
    for value in READINESS_VALUES:
        view = mc.project_mission_control(_with_readiness(base, value), _NOW)
        row, hero = _readiness_row(view), _hero_by_key(view)["readiness"]
        assert row["state"] == value and hero["state"] == value, value
        assert row["severity"] == hero["severity"] == mc._READINESS_SEVERITY[value], value
        assert row["state"] not in mc.READ_MODEL_STATES or value == "UNAVAILABLE"
    # unrecognised or missing readiness fails closed, never positive
    for bogus in ("BOGUS", "", None):
        view = mc.project_mission_control(_with_readiness(base, bogus), _NOW)
        assert _readiness_row(view)["state"] == "UNAVAILABLE"
        assert _hero_by_key(view)["readiness"]["severity"] != "green"


def test_authority_row_carries_unavailable_evidence_beside_a_valid_level(client, tmp_path):
    """Codex B. Valid A1 level, non-canonical grants -> the read model reads the
    effective level A1 but refuses the record (record_evidence UNAVAILABLE)."""
    root = _root(tmp_path)
    p = root / au.DEFAULT_STATE_REL
    rec = json.loads(p.read_text(encoding="utf-8"))
    assert rec["level"] == "A1_ASSISTED_ENGINEERING"
    rec["grants"] = rec["grants"][:-1]                     # subset: non-canonical
    p.write_text(json.dumps(rec), encoding="utf-8")
    d = rm.build_dashboard(root, _NOW)
    assert d["worker"]["ew_authority"] == "A1_ASSISTED_ENGINEERING"
    assert d["worker_authority"]["record_evidence"] == "UNAVAILABLE"
    view = mc.project_mission_control(d, _NOW)
    f = _worker_field(view, "Engineer worker", "ew_authority")
    assert f["state"] == "UNAVAILABLE" and f["severity"] != "green"
    assert "fail-closed" in f["note"]
    assert view["authority"]["state"] == "UNAVAILABLE"
    assert _hero_by_key(view)["authority"]["state"] == "UNAVAILABLE"
    html = client(root).get(_ROUTE).text
    card = _re.search(r'Engineer worker</div>.*?GPT supervisor', html, _re.S).group(0)
    auth_row = _re.search(r'>Authority</div>.*?</span>', card, _re.S).group(0)
    assert ">UNAVAILABLE</span>" in auth_row and "border-emerald-500/30" not in auth_row
    assert "A1_ASSISTED_ENGINEERING" in auth_row          # effective level still shown


def test_authority_row_is_live_only_when_the_record_evidence_is_live(tmp_path):
    d = rm.build_dashboard(_populated(tmp_path), _NOW)
    assert d["worker_authority"]["record_evidence"] == "LIVE"
    view = mc.project_mission_control(d, _NOW)
    assert _worker_field(view, "Engineer worker", "ew_authority")["state"] == "LIVE"
    for state in ("STALE", "UNKNOWN", "UNAVAILABLE", "PENDING_BACKEND"):
        d2 = copy.deepcopy(d)
        d2["worker_authority"]["record_evidence"] = state
        f = _worker_field(mc.project_mission_control(d2, _NOW), "Engineer worker", "ew_authority")
        assert f["state"] == state and f["severity"] != "green", state


def test_mission_and_controller_rows_carry_published_capability_states(tmp_path):
    """Sweep: no row invents LIVE; each takes the backend_truth capability the
    read model published for it, and a missing capability is UNAVAILABLE."""
    base = rm.build_dashboard(_populated(tmp_path), _NOW)
    view = mc.project_mission_control(base, _NOW)
    mission_fields = {f["key"]: f for f in view["mission"]["fields"]}
    assert mission_fields["runtime_mission"]["state"] == "LIVE"          # published mission_state
    assert _hero_by_key(view)["mission"]["state"] == "LIVE"
    ctl = lambda v, k: _worker_field(v, "Controller", k)
    assert ctl(view, "identity_basis")["state"] == "PENDING_BACKEND"     # controller_identity
    assert ctl(view, "controller_since")["state"] == "PENDING_BACKEND"
    for cap, check in (("mission_state", lambda v: (mission_fields_of(v)["runtime_mission"]["state"],
                                                     _hero_by_key(v)["mission"]["state"])),
                       ("controller_state", lambda v: (ctl(v, "operational_state")["state"],)),
                       ("controller_identity", lambda v: (ctl(v, "identity_basis")["state"],))):
        for state in ("UNAVAILABLE", "STALE", "UNKNOWN"):
            v = mc.project_mission_control(_with_capability(base, cap, state), _NOW)
            got = check(v)
            # the PENDING_BACKEND sentinel value always wins over section evidence
            assert all(g in (state, "PENDING_BACKEND") for g in got), (cap, state, got)
            assert any(g == state for g in got) or cap == "controller_state", (cap, state, got)
    # a capability the read model did not publish at all -> UNAVAILABLE, not LIVE
    d = copy.deepcopy(base)
    d["backend_truth"]["capabilities"] = [c for c in d["backend_truth"]["capabilities"]
                                          if c["capability"] != "mission_state"]
    v = mc.project_mission_control(d, _NOW)
    assert mission_fields_of(v)["runtime_mission"]["state"] == "UNAVAILABLE"
    assert _hero_by_key(v)["mission"]["state"] == "UNAVAILABLE"


def mission_fields_of(view):
    return {f["key"]: f for f in view["mission"]["fields"]}


def test_component_health_and_readability_do_not_invent_live(tmp_path):
    base = rm.build_dashboard(_populated(tmp_path), _NOW)
    view = mc.project_mission_control(base, _NOW)
    for f in view["system_health"]["fields"]:
        assert f["state"] == "PENDING_BACKEND", f["key"]
    # readability rows carry the probe's own published state verbatim
    for f in view["system_health"]["readability"]:
        assert f["state"] == f["raw"] == "READABLE"
        assert "never liveness" in f["note"]
    d = copy.deepcopy(base)
    d["system_health"]["config_readability"]["authority_record"] = "MALFORMED"
    d["system_health"]["controller"] = "GREEN"          # a value without a producer
    v = mc.project_mission_control(d, _NOW)
    rd = {f["key"]: f for f in v["system_health"]["readability"]}["authority_record"]
    assert rd["state"] == "MALFORMED" and rd["severity"] != "green"
    ctl = {f["key"]: f for f in v["system_health"]["fields"]}["controller"]
    assert ctl["state"] == "PENDING_BACKEND" and ctl["severity"] != "green"   # component_health capability


def test_adapter_has_no_literal_live_fallback_inside_any_projection():
    """The only place the adapter may spell LIVE as a value is the module-level
    taxonomy tables and the page-level read_model_status; no projection
    function may fall back to a positive truth state."""
    tree = ast.parse(_ADAPTER.read_text(encoding="utf-8"))
    offenders = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if isinstance(node, ast.Constant) and node.value == "LIVE":
                offenders.append(fn.name)
    assert set(offenders) <= {"project_mission_control"}, sorted(set(offenders))
