"""Tests for the Temporary Direct Production Evidence Bridge V0 (local half).

No live VPS: the SSH transport is dependency-injected with fakes. Every
adversarial vector fails closed; the model/sandbox only ever see admitted,
sanitized evidence with no connection facts.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from portfolio_automation.rd_control import registry as reg
from portfolio_automation.rd_control.contracts import JobStatus
from portfolio_automation.engineer_worker import prod_evidence as PE
from portfolio_automation.engineer_worker.prod_evidence import (
    CollectorConfig, ProductionEvidenceCollector, ProductionEvidenceCapability as Cap,
    ProductionEvidenceStatus as St, SshResult, validate_selector, ProdEvidenceError,
)
from portfolio_automation.engineer_worker import controller as ctrl
from portfolio_automation.engineer_worker.contracts import (
    EngineeringJobSpecV0, EngineeringJobType,
)


def _clock():
    n = {"i": 0}
    def now():
        n["i"] += 1
        return f"2026-08-10T20:00:{n['i']:02d}Z"
    return now


def _cfg(tmp_path, **over):
    return CollectorConfig(host="prod.example", identity_file=str(tmp_path / "k"),
                           known_hosts_file=str(tmp_path / "kh"),
                           snapshot_dir=str(tmp_path / "snap"),
                           audit_log=str(tmp_path / "audit.jsonl"), **over)


def _collector(tmp_path, transport, **over):
    return ProductionEvidenceCollector(_cfg(tmp_path, **over), _clock(), transport=transport)


def fixed(resp: str, rc: int = 0, err: str = ""):
    return lambda argv: SshResult(rc, resp, err)


def raising(argv):
    raise AssertionError("transport must NOT be called for a rejected selector")


# --- selector validation ----------------------------------------------------
def test_selector_valid():
    assert validate_selector(Cap.DAILY_LOG, "2026-08-10") == "2026-08-10"
    assert validate_selector(Cap.DAILY_LOG, "today") == "today"
    assert validate_selector(Cap.RUN_MANIFEST, "latest") == "latest"
    assert validate_selector(Cap.ARTIFACT, "daily_status") == "daily_status"
    assert validate_selector(Cap.DB_QUERY, "latest-daily-run") == "latest-daily-run"
    assert validate_selector(Cap.DAILY_STATUS, "") == ""


@pytest.mark.parametrize("cap,sel", [
    (Cap.DAILY_LOG, "../../etc/passwd"),
    (Cap.DAILY_LOG, "2026-08-10; rm -rf /"),
    (Cap.ARTIFACT, "../secret"),
    (Cap.ARTIFACT, "not_allowlisted"),
    (Cap.DB_QUERY, "DROP TABLE runs"),
    (Cap.DB_QUERY, "latest-daily-run; DELETE"),
    (Cap.RUN_MANIFEST, "$(whoami)"),
    (Cap.DAILY_LOG, "a\nb"),
])
def test_selector_rejects_hostile(cap, sel):
    with pytest.raises(ProdEvidenceError):
        validate_selector(cap, sel)


# --- collector admission ----------------------------------------------------
def test_available_admits_and_hashes(tmp_path):
    c = _collector(tmp_path, fixed(
        '{"run_id":"r-123","source_commit":"abcdef1","complete":false,"failed_stage":"broker_sync"}'))
    ev = c.retrieve(Cap.DAILY_STATUS, "")
    assert ev.status is St.AVAILABLE and ev.admitted
    assert ev.run_id == "r-123" and ev.source_commit == "abcdef1"
    assert ev.content_sha256.startswith("sha256:") and ev.byte_count > 0
    assert ev.content["failed_stage"] == "broker_sync"
    # immutable local snapshot written read-only
    assert ev.snapshot_path and os.path.exists(ev.snapshot_path)
    assert not (stat.S_IMODE(os.stat(ev.snapshot_path).st_mode) & stat.S_IWUSR)
    # audit line recorded, no secrets
    audit = open(tmp_path / "audit.jsonl").read()
    assert "r-123" in audit and "identity_file" not in audit


def test_hard_secret_rejected(tmp_path):
    key = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
    c = _collector(tmp_path, fixed(key))
    ev = c.retrieve(Cap.DAILY_LOG, "today")
    assert ev.status is St.REJECTED and not ev.admitted and ev.content is None
    assert ev.snapshot_path is None


def test_soft_secret_redacted(tmp_path):
    c = _collector(tmp_path, fixed(
        "line1\napi_key=SUPERSECRETVALUE\nstage broker_sync OK\n"))
    ev = c.retrieve(Cap.DAILY_LOG, "today")
    assert ev.status is St.AVAILABLE
    assert "SUPERSECRETVALUE" not in ev.content and "<redacted>" in ev.content


def test_size_bound_rejected(tmp_path):
    c = _collector(tmp_path, fixed("x" * (PE._MAX_EVIDENCE_BYTES + 10)))
    ev = c.retrieve(Cap.DAILY_LOG, "today")
    assert ev.status is St.REJECTED and "size" in ev.rejection_reason


def test_host_key_failure_identity_unverified(tmp_path):
    c = _collector(tmp_path, fixed("", rc=255, err="Host key verification failed."))
    ev = c.retrieve(Cap.DAILY_STATUS, "")
    assert ev.status is St.IDENTITY_UNVERIFIED and not ev.admitted


def test_transport_failure_unavailable(tmp_path):
    c = _collector(tmp_path, fixed("", rc=1, err="connection refused"))
    ev = c.retrieve(Cap.DAILY_STATUS, "")
    assert ev.status is St.UNAVAILABLE and not ev.admitted


def test_rejected_selector_never_calls_transport(tmp_path):
    c = _collector(tmp_path, raising)
    ev = c.retrieve(Cap.ARTIFACT, "../etc/passwd")
    assert ev.status is St.REJECTED  # transport (raising) was never invoked


def test_model_view_has_no_connection_facts(tmp_path):
    c = _collector(tmp_path, fixed('{"run_id":"r1","ok":true}'))
    ev = c.retrieve(Cap.DAILY_STATUS, "")
    view = json.dumps(ev.to_model_view())
    for leak in ("identity_file", "known_hosts", "prod.example", "stockbot-observer", str(tmp_path / "k")):
        assert leak not in view


# --- default transport builds a fixed, hardened argv ------------------------
def test_default_transport_fixed_argv(tmp_path, monkeypatch):
    captured = {}
    class P:
        returncode = 0; stdout = '{"ok":1}'; stderr = ""
    monkeypatch.setattr(PE.subprocess, "run", lambda argv, **kw: (captured.__setitem__("argv", argv), P())[1])
    cfg = _cfg(tmp_path)
    PE._default_ssh_transport(cfg)(["daily-log", "today"])
    a = captured["argv"]
    for opt in ("BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes",
                "ClearAllForwardings=yes", "ForwardAgent=no", "RequestTTY=no"):
        assert opt in a
    assert "-i" in a and str(tmp_path / "k") in a
    assert "stockbot-observer@prod.example" in a
    # model-controlled selector is a trailing positional arg, NOT an ssh option
    assert a[-2:] == ["daily-log", "today"]


# --- controller integration -------------------------------------------------
def _scripted(*responses):
    q = list(responses)
    def infer(system, user):
        assert q, "no scripted response"
        return q.pop(0)
    return infer


def _daily_spec():
    return EngineeringJobSpecV0(job_type=EngineeringJobType.DAILY_RUN_DIAGNOSTIC,
                               title="prod daily diag", daily_artifact_paths=[])


def _finding(**over):
    d = {"summary": "s", "severity": "LOW", "confidence": 0.5, "evidence_refs": [],
         "abstain": False, "repair_recommended": False, "repair_scope": "NONE"}
    d.update(over)
    return json.dumps(d)


def test_controller_reads_admitted_prod_evidence(tmp_path):
    (tmp_path / "repo").mkdir()
    collector = _collector(tmp_path, fixed(
        '{"run_id":"r-9","complete":false,"failed_stage":"broker_sync"}'))
    model = _scripted(
        json.dumps({"tool_request": {"capability": "READ_PRODUCTION_DAILY_EVIDENCE",
                                     "argument": "daily-status"}}),
        _finding(summary="prod daily failed at broker_sync", severity="HIGH", confidence=0.8,
                 evidence_refs=["source:prod_evidence"]))
    cfg = ctrl.ControllerConfig(repo_root=str(tmp_path / "repo"), registry_db=str(tmp_path / "rd.db"),
                                now_fn=_clock(), infer_fn=model, prod_evidence_collector=collector)
    res = ctrl.run_engineering_job(_daily_spec(), cfg)
    assert res.status == "SUCCEEDED"
    assert "READ_PRODUCTION_DAILY_EVIDENCE" in res.telemetry["tools_granted"]


def test_controller_prod_evidence_unavailable_when_unconfigured(tmp_path):
    (tmp_path / "repo").mkdir()
    model = _scripted(
        json.dumps({"tool_request": {"capability": "READ_PRODUCTION_DAILY_EVIDENCE",
                                     "argument": "daily-status"}}),
        _finding(summary="abstaining; prod evidence unavailable", severity="LOW",
                 confidence=0.2, abstain=True, abstain_reason="prod evidence bridge not configured"))
    cfg = ctrl.ControllerConfig(repo_root=str(tmp_path / "repo"), registry_db=str(tmp_path / "rd.db"),
                                now_fn=_clock(), infer_fn=model, prod_evidence_collector=None)
    res = ctrl.run_engineering_job(_daily_spec(), cfg)
    assert res.status == "SUCCEEDED" and res.finding.abstain is True
    # the tool was granted-but-unavailable (fail closed), not a hard denial
    assert "READ_PRODUCTION_DAILY_EVIDENCE" in res.telemetry["tools_granted"]


def test_repair_job_cannot_use_prod_evidence():
    # REPAIR_CANDIDATE does not grant READ_PRODUCTION_DAILY_EVIDENCE -> hard denial.
    from portfolio_automation.engineer_worker import policy
    import portfolio_automation.engineer_worker.contracts as C
    with pytest.raises(policy.PolicyError):
        policy.check_tool_allowed(EngineeringJobType.REPAIR_CANDIDATE,
                                  C.ToolCapability.READ_PRODUCTION_DAILY_EVIDENCE)
