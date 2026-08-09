"""Tests for the Engineer Worker MVP 0A (experimental, non-canonical).

Hermetic: the local model is a deterministic scripted fake (real Ollama is
exercised separately in the Agent-Lab live evidence run). Every adversarial
vector must fail closed.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from portfolio_automation.rd_control import registry as reg
from portfolio_automation.rd_control.contracts import JobStatus
from portfolio_automation.engineer_worker import contracts as C
from portfolio_automation.engineer_worker import policy, model_adapter, adapters
from portfolio_automation.engineer_worker.contracts import (
    EngineeringJobSpecV0, EngineeringJobType, ToolCapability, VerificationStatus,
)
from portfolio_automation.engineer_worker import controller as ctrl


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class ScriptedModel:
    """Deterministic fake InferenceFn: returns scripted responses in order."""
    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if not self.responses:
            raise AssertionError("ScriptedModel ran out of responses")
        return self.responses.pop(0)


class LoopingModel:
    """Always requests a tool (never converges) — for endless-loop tests."""
    def __call__(self, system: str, user: str) -> str:
        return json.dumps({"tool_request": {"capability": "CHECK_REPO_STATUS", "argument": ""}})


def finding_json(**over) -> str:
    d = {"summary": "diagnostic summary", "severity": "LOW", "confidence": 0.5,
         "observations": [], "likely_causes": [], "evidence_refs": [],
         "recommended_checks": [], "repair_recommended": False,
         "repair_scope": "NONE", "abstain": False, "abstain_reason": None}
    d.update(over)
    return json.dumps(d)


def repair_json(path: str, new_content: str, tests=None, rationale="fix") -> str:
    return json.dumps({"rationale": rationale,
                       "edits": [{"path": path, "new_content": new_content}],
                       "tests_to_run": tests or []})


def _clock():
    n = {"i": 0}
    def now():
        n["i"] += 1
        return f"2026-08-09T20:00:{n['i']:02d}Z"
    return now


def _cfg(tmp_path, infer, repo_root=None, **over):
    return ctrl.ControllerConfig(
        repo_root=str(repo_root or tmp_path / "repo"),
        registry_db=str(tmp_path / "rd.db"),
        now_fn=_clock(), infer_fn=infer,
        workspace_parent=str(tmp_path / "ws"), **over)


def _mk_repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    (r / "logs").mkdir(parents=True)
    (r / "artifacts").mkdir(parents=True)
    Path(tmp_path / "ws").mkdir(exist_ok=True)
    return r


def _git_init(root: Path):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root)}
    subprocess.run(["git", "init", "-q"], cwd=root, env=env, check=False)
    subprocess.run(["git", "add", "-A"], cwd=root, env=env, check=False)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=root, env=env, check=False)


FAILING_LOG = (
    "2026-08-09 06:00 preflight OK\n"
    "2026-08-09 06:01 STAGE news_intelligence OK\n"
    "2026-08-09 06:02 STAGE broker_sync FAILED: connection refused to schwab endpoint\n"
    "2026-08-09 06:03 STAGE main_daily ABORTED due to upstream failure\n"
    "2026-08-09 06:03 daily run did NOT complete\n"
)
HEALTHY_LOG = (
    "2026-08-09 06:00 preflight OK\n"
    "2026-08-09 06:01 STAGE news_intelligence OK\n"
    "2026-08-09 06:02 STAGE broker_sync OK\n"
    "2026-08-09 06:03 STAGE main_daily OK\n"
    "2026-08-09 06:05 daily run complete: all stages green\n"
)


# --------------------------------------------------------------------------- #
# 1. contract / schema validation
# --------------------------------------------------------------------------- #
def test_finding_validation_accepts_good():
    f = C.validate_finding(json.loads(finding_json(severity="HIGH", confidence=0.9)),
                           job_id="j1", valid_evidence_refs={"source:daily_log"})
    assert f.severity is C.Severity.HIGH and f.confidence == 0.9


@pytest.mark.parametrize("bad", [
    {"summary": "x", "severity": "BOGUS"},
    {"summary": "x", "confidence": 2.0},
    {"summary": "x", "confidence": "high"},
    {"summary": ""},
    {"severity": "LOW"},                       # missing summary
])
def test_finding_validation_rejects_bad(bad):
    with pytest.raises(C.ContractError):
        C.validate_finding(bad, job_id="j1", valid_evidence_refs=set())


def test_finding_rejects_unsupported_evidence_ref():
    with pytest.raises(C.ContractError):
        C.validate_finding(json.loads(finding_json(evidence_refs=["source:fabricated"])),
                           job_id="j1", valid_evidence_refs={"source:daily_log"})


def test_finding_rejects_job_id_laundering():
    with pytest.raises(C.ContractError):
        C.validate_finding({"summary": "x", "severity": "LOW", "confidence": 0.1,
                            "job_id": "other"}, job_id="j1", valid_evidence_refs=set())


def test_repair_validation():
    p = C.validate_repair_proposal(json.loads(repair_json("docs/x.md", "hi")), job_id="j1")
    assert p.edits[0]["path"] == "docs/x.md"
    with pytest.raises(C.ContractError):
        C.validate_repair_proposal({"edits": []}, job_id="j1")


# --------------------------------------------------------------------------- #
# 2. policy
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("p", [
    "portfolio_automation/decision_engine.py", ".git/config", "ops/agent_lab/sandbox-run.sh",
    "portfolio_automation/rd_control/sandbox.py", "config/agent_policy.yaml",
    ".agent/project_state.yaml", "portfolio_automation/engineer_worker/policy.py",
    ".env", "portfolio_automation/scoring/x.py",
])
def test_protected_paths(p):
    assert policy.is_protected(p) is True
    assert policy.is_repair_allowed(p) is False


@pytest.mark.parametrize("p", ["docs/x.md", "tests/test_x.py", "devtools/adder.py"])
def test_repair_allowed_paths(p):
    assert policy.is_repair_allowed(p) is True


def test_safe_join_rejects_traversal_and_abs(tmp_path):
    (tmp_path / "ok.txt").write_text("hi")
    assert policy.safe_join(tmp_path, "ok.txt").name == "ok.txt"
    for bad in ["../etc/passwd", "/etc/passwd", "a/../../b", ""]:
        with pytest.raises(policy.PolicyError):
            policy.safe_join(tmp_path, bad)


def test_safe_join_rejects_symlink(tmp_path):
    outside = tmp_path / "outside.txt"; outside.write_text("secret")
    link = tmp_path / "root" / "link.txt"; (tmp_path / "root").mkdir()
    os.symlink(outside, link)
    with pytest.raises(policy.PolicyError):
        policy.safe_join(tmp_path / "root", "link.txt")


def test_tool_allowlist_per_job():
    policy.check_tool_allowed(EngineeringJobType.DAILY_RUN_DIAGNOSTIC, ToolCapability.READ_DAILY_LOG)
    with pytest.raises(policy.PolicyError):
        policy.check_tool_allowed(EngineeringJobType.DAILY_RUN_DIAGNOSTIC, ToolCapability.RUN_APPROVED_TEST)


def test_test_allowlist():
    policy.check_test_allowed(["tests/test_adder.py"], "tests/test_adder.py")
    policy.check_test_allowed(["tests/test_adder.py"], "tests/test_adder.py::test_add")
    with pytest.raises(policy.PolicyError):
        policy.check_test_allowed(["tests/test_adder.py"], "tests/test_secret.py")
    with pytest.raises(policy.PolicyError):
        policy.check_test_allowed(["portfolio_automation/rd_control/sandbox.py"],
                                  "portfolio_automation/rd_control/sandbox.py")


# --------------------------------------------------------------------------- #
# 3. adapters
# --------------------------------------------------------------------------- #
def test_disk_status():
    ds = adapters.disk_status(".")
    assert ds.ok and ds.data["total_gb"] > 0


def test_daily_log_reader_bounded(tmp_path):
    r = _mk_repo(tmp_path)
    (r / "logs" / "d.log").write_text(FAILING_LOG)
    ds = adapters.daily_log_reader(r, "logs/d.log")
    assert ds.ok and any("broker_sync FAILED" in l for l in ds.data["tail_excerpt"])


def test_daily_log_reader_rejects_traversal(tmp_path):
    r = _mk_repo(tmp_path)
    with pytest.raises(policy.PolicyError):
        adapters.daily_log_reader(r, "../../etc/passwd")


def test_artifact_reader_json(tmp_path):
    r = _mk_repo(tmp_path)
    (r / "artifacts" / "m.json").write_text(json.dumps({"complete": False, "failed_stage": "broker_sync"}))
    ds = adapters.daily_artifact_reader(r, "artifacts/m.json")
    assert ds.ok and ds.data["parsed"]["failed_stage"] == "broker_sync"


def test_test_status_runs_allowlisted(tmp_path):
    r = _mk_repo(tmp_path)
    (r / "tests").mkdir()
    (r / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1 == 1\n")
    ds = adapters.test_status(r, "tests/test_ok.py")
    assert ds.ok and "passed" in ds.data["summary"]


# --------------------------------------------------------------------------- #
# 4. model adapter (structured output parsing)
# --------------------------------------------------------------------------- #
def test_extract_json_plain_and_fenced():
    assert model_adapter.extract_json_object('{"a":1}')["a"] == 1
    assert model_adapter.extract_json_object('```json\n{"a":2}\n```')["a"] == 2
    assert model_adapter.extract_json_object('here it is: {"a":3} thanks')["a"] == 3


def test_extract_json_rejects_bad():
    with pytest.raises(C.ContractError):
        model_adapter.extract_json_object("no json here")
    with pytest.raises(C.ContractError):
        model_adapter.extract_json_object("[1,2,3]")               # not an object
    with pytest.raises(C.ContractError):
        model_adapter.extract_json_object("{" + "x" * (model_adapter.MAX_MODEL_OUTPUT_BYTES + 10))


# --------------------------------------------------------------------------- #
# 5. daily diagnostic acceptance (failing / healthy / abstain / missing)
# --------------------------------------------------------------------------- #
def _daily_spec(**over):
    d = dict(job_type=EngineeringJobType.DAILY_RUN_DIAGNOSTIC, title="daily diag",
             daily_log_path="logs/d.log", daily_artifact_paths=["artifacts/m.json"],
             expected_stages=["preflight", "broker_sync", "main_daily"])
    d.update(over)
    return EngineeringJobSpecV0(**d)


def test_daily_diagnostic_identifies_failure(tmp_path):
    r = _mk_repo(tmp_path); _git_init(r)
    (r / "logs" / "d.log").write_text(FAILING_LOG)
    (r / "artifacts" / "m.json").write_text(json.dumps({"complete": False, "failed_stage": "broker_sync"}))
    model = ScriptedModel(finding_json(
        summary="Daily run aborted: broker_sync failed (connection refused)",
        severity="HIGH", confidence=0.8, repair_recommended=False,
        observations=["broker_sync FAILED", "daily run did not complete"],
        likely_causes=["schwab endpoint unreachable"],
        evidence_refs=["source:daily_log", "source:artifact:artifacts/m.json"]))
    res = ctrl.run_engineering_job(_daily_spec(), _cfg(tmp_path, model, repo_root=r))
    assert res.status == "SUCCEEDED"
    assert res.finding.severity is C.Severity.HIGH
    assert "broker_sync" in res.finding.summary
    assert not res.finding.abstain


def test_daily_diagnostic_healthy_no_fabrication(tmp_path):
    r = _mk_repo(tmp_path); _git_init(r)
    (r / "logs" / "d.log").write_text(HEALTHY_LOG)
    (r / "artifacts" / "m.json").write_text(json.dumps({"complete": True}))
    model = ScriptedModel(finding_json(summary="Daily run completed; all stages green",
                                       severity="INFO", confidence=0.9,
                                       evidence_refs=["source:daily_log"]))
    res = ctrl.run_engineering_job(_daily_spec(), _cfg(tmp_path, model, repo_root=r))
    assert res.status == "SUCCEEDED" and res.finding.severity is C.Severity.INFO
    assert res.finding.repair_recommended is False


def test_daily_diagnostic_abstains_on_ambiguity(tmp_path):
    r = _mk_repo(tmp_path)
    (r / "logs" / "d.log").write_text("2026-08-09 partial log, truncated mid-run\n")
    model = ScriptedModel(finding_json(summary="insufficient evidence", severity="LOW",
                                       confidence=0.2, abstain=True,
                                       abstain_reason="log truncated; cannot determine cause"))
    res = ctrl.run_engineering_job(_daily_spec(daily_artifact_paths=[]),
                                   _cfg(tmp_path, model, repo_root=r))
    assert res.status == "SUCCEEDED" and res.finding.abstain is True


def test_daily_diagnostic_missing_log_reports_missing(tmp_path):
    r = _mk_repo(tmp_path)   # no log file written
    model = ScriptedModel(finding_json(summary="required daily log is missing",
                                       severity="MEDIUM", confidence=0.6, abstain=True,
                                       abstain_reason="daily_log source unavailable"))
    res = ctrl.run_engineering_job(_daily_spec(daily_artifact_paths=[]),
                                   _cfg(tmp_path, model, repo_root=r))
    assert res.status == "SUCCEEDED"
    # controller recorded the missing evidence (ok=False), model did not invent a cause
    assert res.finding.abstain is True


# --------------------------------------------------------------------------- #
# 6. tool-request loop
# --------------------------------------------------------------------------- #
def test_tool_request_granted_then_finding(tmp_path):
    r = _mk_repo(tmp_path); _git_init(r)
    (r / "logs" / "d.log").write_text(HEALTHY_LOG)
    (r / "artifacts" / "m.json").write_text(json.dumps({"complete": True}))
    model = ScriptedModel(
        json.dumps({"tool_request": {"capability": "READ_DAILY_ARTIFACT",
                                     "argument": "artifacts/m.json"}}),
        finding_json(summary="ok after reading artifact", severity="INFO", confidence=0.7,
                     evidence_refs=["source:artifact:artifacts/m.json"]))
    res = ctrl.run_engineering_job(_daily_spec(daily_artifact_paths=[]),
                                   _cfg(tmp_path, model, repo_root=r))
    assert res.status == "SUCCEEDED"
    assert "READ_DAILY_ARTIFACT" in res.telemetry["tools_granted"]


def test_tool_request_denied_wrong_capability(tmp_path):
    r = _mk_repo(tmp_path); _git_init(r)
    (r / "logs" / "d.log").write_text(HEALTHY_LOG)
    model = ScriptedModel(
        json.dumps({"tool_request": {"capability": "RUN_APPROVED_TEST", "argument": "tests/x.py"}}),
        finding_json(summary="proceeding without denied tool", severity="LOW", confidence=0.4))
    res = ctrl.run_engineering_job(_daily_spec(daily_artifact_paths=[]),
                                   _cfg(tmp_path, model, repo_root=r))
    assert res.status == "SUCCEEDED"
    assert "RUN_APPROVED_TEST" in res.telemetry["tools_denied"]


def test_endless_tool_requests_fail_closed(tmp_path):
    r = _mk_repo(tmp_path); _git_init(r)
    (r / "logs" / "d.log").write_text(HEALTHY_LOG)
    spec = _daily_spec(daily_artifact_paths=[], max_tool_rounds=3)
    res = ctrl.run_engineering_job(spec, _cfg(tmp_path, LoopingModel(), repo_root=r))
    assert res.status == "FAILED_VALIDATION"


# --------------------------------------------------------------------------- #
# 7. malformed / adversarial model output
# --------------------------------------------------------------------------- #
def test_malformed_json_fails_validation(tmp_path):
    r = _mk_repo(tmp_path); (r / "logs" / "d.log").write_text(HEALTHY_LOG)
    res = ctrl.run_engineering_job(_daily_spec(daily_artifact_paths=[]),
                                   _cfg(tmp_path, ScriptedModel("not json at all"), repo_root=r))
    assert res.status == "FAILED_VALIDATION"


def test_fake_evidence_ref_fails_validation(tmp_path):
    r = _mk_repo(tmp_path); (r / "logs" / "d.log").write_text(HEALTHY_LOG)
    model = ScriptedModel(finding_json(evidence_refs=["source:does_not_exist"]))
    res = ctrl.run_engineering_job(_daily_spec(daily_artifact_paths=[]),
                                   _cfg(tmp_path, model, repo_root=r))
    assert res.status == "FAILED_VALIDATION"


@pytest.mark.parametrize("jt", ["PRODUCTION_DAILY_EXECUTION", "DEPLOYMENT", "MERGE", "PUSH"])
def test_forbidden_job_types_fail_closed(tmp_path, jt):
    # Build a spec whose job_type is a forbidden string (bypassing the enum).
    spec = _daily_spec()
    object.__setattr__(spec, "job_type", type("X", (), {"value": jt})())
    with pytest.raises(policy.PolicyError):
        ctrl.run_engineering_job(spec, _cfg(tmp_path, ScriptedModel(finding_json())))


# --------------------------------------------------------------------------- #
# 8. repair candidate (disposable) + isolation + verification
# --------------------------------------------------------------------------- #
def _mk_repair_repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    (r / "devtools").mkdir(parents=True)
    (r / "tests").mkdir(parents=True)
    (r / "docs").mkdir(parents=True)
    Path(tmp_path / "ws").mkdir(exist_ok=True)
    (r / "devtools" / "__init__.py").write_text("")
    (r / "devtools" / "adder.py").write_text("def add(a, b):\n    return a - b  # BUG\n")
    (r / "tests" / "test_adder.py").write_text(
        "import os, sys\n"
        "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "from devtools.adder import add\n"
        "def test_add():\n    assert add(2, 3) == 5\n")
    return r


def _repair_spec(targets, tests, **over):
    d = dict(job_type=EngineeringJobType.REPAIR_CANDIDATE, title="repair",
             repair_allowed=True, repair_targets=targets, allowed_tests=tests)
    d.update(over)
    return EngineeringJobSpecV0(**d)


def test_repair_candidate_verified_and_repo_unchanged(tmp_path):
    r = _mk_repair_repo(tmp_path)
    original = (r / "devtools" / "adder.py").read_text()
    model = ScriptedModel(
        finding_json(summary="adder.add subtracts instead of adds", severity="MEDIUM",
                     confidence=0.9, repair_recommended=True, repair_scope="DEV_TOOLING"),
        repair_json("devtools/adder.py", "def add(a, b):\n    return a + b\n",
                    tests=["tests/test_adder.py"]))
    spec = _repair_spec(["devtools/adder.py"], ["tests/test_adder.py"])
    res = ctrl.run_engineering_job(spec, _cfg(tmp_path, model, repo_root=r))
    assert res.status == "SUCCEEDED"
    assert res.candidate.verification.verification_status is VerificationStatus.VERIFIED
    assert res.candidate.diff_text and "+    return a + b" in res.candidate.diff_text
    # canonical repo MUST be unchanged
    assert (r / "devtools" / "adder.py").read_text() == original


def test_repair_rejects_protected_path(tmp_path):
    r = _mk_repair_repo(tmp_path)
    (r / "portfolio_automation").mkdir(parents=True, exist_ok=True)
    (r / "portfolio_automation" / "decision_engine.py").write_text("PROTECTED = 1\n")
    orig = (r / "portfolio_automation" / "decision_engine.py").read_text()
    model = ScriptedModel(
        finding_json(summary="wants to touch decision engine", severity="LOW",
                     confidence=0.5, repair_recommended=True),
        repair_json("portfolio_automation/decision_engine.py", "PROTECTED = 999\n"))
    spec = _repair_spec(["portfolio_automation/decision_engine.py"], [])
    res = ctrl.run_engineering_job(spec, _cfg(tmp_path, model, repo_root=r))
    # target isn't even repair-allowed -> policy failure, nothing written anywhere
    assert res.candidate.verification.verification_status is VerificationStatus.FAILED_POLICY
    assert (r / "portfolio_automation" / "decision_engine.py").read_text() == orig


def test_repair_rejects_non_target_path(tmp_path):
    r = _mk_repair_repo(tmp_path)
    model = ScriptedModel(
        finding_json(summary="x", severity="LOW", confidence=0.5, repair_recommended=True),
        repair_json("docs/sneaky.md", "gotcha"))     # docs is allowed-prefix but NOT a target
    spec = _repair_spec(["devtools/adder.py"], [])    # only adder is an approved target
    res = ctrl.run_engineering_job(spec, _cfg(tmp_path, model, repo_root=r))
    assert res.candidate.verification.verification_status is VerificationStatus.FAILED_POLICY


def test_repair_rejects_traversal(tmp_path):
    r = _mk_repair_repo(tmp_path)
    model = ScriptedModel(
        finding_json(summary="x", severity="LOW", confidence=0.5, repair_recommended=True),
        repair_json("../escape.py", "x = 1"))
    spec = _repair_spec(["../escape.py"], [])
    res = ctrl.run_engineering_job(spec, _cfg(tmp_path, model, repo_root=r))
    assert res.candidate.verification.verification_status is VerificationStatus.FAILED_POLICY
    assert not (tmp_path / "escape.py").exists()


def test_repair_failing_tests_not_verified(tmp_path):
    r = _mk_repair_repo(tmp_path)
    model = ScriptedModel(
        finding_json(summary="x", severity="LOW", confidence=0.5, repair_recommended=True),
        repair_json("devtools/adder.py", "def add(a, b):\n    return a - b  # still wrong\n",
                    tests=["tests/test_adder.py"]))
    spec = _repair_spec(["devtools/adder.py"], ["tests/test_adder.py"])
    res = ctrl.run_engineering_job(spec, _cfg(tmp_path, model, repo_root=r))
    assert res.candidate.verification.verification_status is VerificationStatus.FAILED_TESTS


# --------------------------------------------------------------------------- #
# 9. Phase 0A lifecycle integration
# --------------------------------------------------------------------------- #
def test_phase0a_lifecycle_trail(tmp_path):
    r = _mk_repo(tmp_path); (r / "logs" / "d.log").write_text(HEALTHY_LOG)
    cfg = _cfg(tmp_path, ScriptedModel(finding_json(severity="INFO")), repo_root=r)
    res = ctrl.run_engineering_job(_daily_spec(daily_artifact_paths=[]), cfg)
    with reg.connect(cfg.registry_db) as conn:
        trail = [e["to_status"] for e in reg.job_events(conn, res.job_id)]
    assert trail == ["CREATED", "QUEUED", "ADMITTED", "RUNNING",
                     "RESULT_RECEIVED", "VALIDATING", "SUCCEEDED"]


def test_failed_validation_has_no_succeeded(tmp_path):
    r = _mk_repo(tmp_path); (r / "logs" / "d.log").write_text(HEALTHY_LOG)
    cfg = _cfg(tmp_path, ScriptedModel("garbage"), repo_root=r)
    res = ctrl.run_engineering_job(_daily_spec(daily_artifact_paths=[]), cfg)
    with reg.connect(cfg.registry_db) as conn:
        trail = [e["to_status"] for e in reg.job_events(conn, res.job_id)]
    assert "SUCCEEDED" not in trail and trail[-1] == "FAILED_VALIDATION"


def test_telemetry_recorded(tmp_path):
    r = _mk_repo(tmp_path); (r / "logs" / "d.log").write_text(HEALTHY_LOG)
    cfg = _cfg(tmp_path, ScriptedModel(finding_json()), repo_root=r)
    res = ctrl.run_engineering_job(_daily_spec(daily_artifact_paths=[]), cfg)
    t = res.telemetry
    assert t["job_id"] == res.job_id and t["prompt_version"] == ctrl.PROMPT_VERSION
    assert t["schema_valid"] is True and "job_type" in t
