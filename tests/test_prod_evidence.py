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

from portfolio_automation.engineer_worker import prod_evidence as PE
from portfolio_automation.engineer_worker.prod_evidence import (
    CollectorConfig, ProductionEvidenceCollector, ProductionEvidenceCapability as Cap,
    ModelCapability as MC, ProductionEvidenceStatus as St, SshResult, validate_selector,
    resolve_model_capability, assert_coherent, ProdEvidenceError,
)
from portfolio_automation.engineer_worker import controller as ctrl
from portfolio_automation.engineer_worker.contracts import EngineeringJobSpecV0, EngineeringJobType


def _clock():
    n = {"i": 0}
    def now():
        n["i"] += 1
        return f"2026-08-10T21:00:{n['i']:02d}Z"
    return now


def _cfg(tmp_path, **over):
    d = dict(host="prod.example", identity_file=str(tmp_path / "k"),
             known_hosts_file=str(tmp_path / "kh"), snapshot_dir=str(tmp_path / "snap"),
             audit_log=str(tmp_path / "audit.jsonl"))
    d.update(over)
    return CollectorConfig(**d)


def _collector(tmp_path, transport, **over):
    return ProductionEvidenceCollector(_cfg(tmp_path, **over), _clock(), transport=transport)


def fixed(resp, rc=0, err=""):
    return lambda argv: SshResult(rc, resp, err)


def raising(argv):
    raise AssertionError("transport must NOT be called for a rejected selector")


# --- selector + capability mapping ------------------------------------------
def test_selector_valid():
    assert validate_selector(Cap.DAILY_LOG, "2026-08-10") == "2026-08-10"
    assert validate_selector(Cap.DAILY_CHECK, "today") == "today"
    assert validate_selector(Cap.DAILY_STATUS, "") == ""
    assert validate_selector(Cap.DB_QUERY, "latest-daily-run") == "latest-daily-run"


@pytest.mark.parametrize("cap,sel", [
    (Cap.DAILY_LOG, "../../etc/passwd"), (Cap.DAILY_LOG, "2026-08-10; rm -rf /"),
    (Cap.DAILY_LOG, "a\nb"), (Cap.DAILY_CHECK, "$(id)"),
    (Cap.DB_QUERY, "DROP TABLE runs"), (Cap.DB_QUERY, "evil-id"),
])
def test_selector_rejects_hostile(cap, sel):
    with pytest.raises(ProdEvidenceError):
        validate_selector(cap, sel)


def test_resolve_model_capability():
    assert resolve_model_capability("PROD_DAILY_STATUS") == (Cap.DAILY_STATUS, "")
    assert resolve_model_capability("PROD_DAILY_LOG", "2026-08-10") == (Cap.DAILY_LOG, "2026-08-10")
    assert resolve_model_capability("PROD_DB_LATEST_DAILY_RUN") == (Cap.DB_QUERY, "latest-daily-run")
    # selector ignored for no-selector capabilities (can't smuggle a path)
    assert resolve_model_capability("PROD_DAILY_STATUS", "../etc") == (Cap.DAILY_STATUS, "")
    for bad in ("BOGUS", "daily-status", "PROD_DB_ARBITRARY"):
        with pytest.raises(ProdEvidenceError):
            resolve_model_capability(bad)
    with pytest.raises(ProdEvidenceError):
        resolve_model_capability("PROD_DAILY_LOG", "../../etc/passwd")


# --- admission --------------------------------------------------------------
def test_available_admits_and_hashes(tmp_path):
    c = _collector(tmp_path, fixed(
        '{"run_id":"r-123","source_commit":"abcdef1","generated_at":"2026-08-10T06:05Z","complete":false,"failed_stage":"broker_sync"}'))
    ev = c.retrieve(Cap.DAILY_STATUS)
    assert ev.status is St.AVAILABLE and ev.admitted
    assert ev.run_id == "r-123" and ev.source_commit == "abcdef1" and ev.generated_at
    assert ev.content_sha256.startswith("sha256:") and ev.content_byte_count > 0
    assert ev.requested_at and ev.retrieved_at and ev.source_environment == "production-vps"
    assert ev.content["failed_stage"] == "broker_sync"
    assert ev.snapshot_path and os.path.exists(ev.snapshot_path)
    assert not (stat.S_IMODE(os.stat(ev.snapshot_path).st_mode) & stat.S_IWUSR)  # read-only
    audit = open(tmp_path / "audit.jsonl").read()
    assert "r-123" in audit and "identity_file" not in audit


@pytest.mark.parametrize("body", [
    "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
    "stage OK\napi_key=SUPERSECRETVALUE12\n",
    "authorization: Bearer abcdefgh12345\n",
    "SCHWAB_TOKEN=zzzzzzzzzzzz\n",
])
def test_secret_material_rejected_fail_closed(tmp_path, body):
    ev = _collector(tmp_path, fixed(body)).retrieve(Cap.DAILY_LOG, "today")
    assert ev.status is St.REJECTED and not ev.admitted and ev.content is None
    assert ev.snapshot_path is None


def test_already_redacted_evidence_is_allowed(tmp_path):
    ev = _collector(tmp_path, fixed("stage broker_sync OK\napi_key=<redacted>\n")).retrieve(Cap.DAILY_LOG, "today")
    assert ev.status is St.AVAILABLE


def test_size_bound_rejected(tmp_path):
    ev = _collector(tmp_path, fixed("x" * (PE._MAX_EVIDENCE_BYTES + 10))).retrieve(Cap.DAILY_LOG, "today")
    assert ev.status is St.REJECTED and "size" in ev.rejection_reason


def test_host_key_failure_unavailable(tmp_path):
    ev = _collector(tmp_path, fixed("", rc=255, err="Host key verification failed.")).retrieve(Cap.DAILY_STATUS)
    assert ev.status is St.UNAVAILABLE and not ev.admitted


def test_auth_denied_unavailable(tmp_path):
    ev = _collector(tmp_path, fixed("", rc=255, err="Permission denied (publickey).")).retrieve(Cap.DAILY_STATUS)
    assert ev.status is St.UNAVAILABLE


def test_transport_failure_unavailable(tmp_path):
    ev = _collector(tmp_path, fixed("", rc=1, err="connection refused")).retrieve(Cap.DAILY_STATUS)
    assert ev.status is St.UNAVAILABLE


def test_rejected_selector_never_calls_transport(tmp_path):
    ev = _collector(tmp_path, raising).retrieve(Cap.DAILY_LOG, "../etc/passwd")
    assert ev.status is St.REJECTED


# --- run-identity (Phase O) -------------------------------------------------
def test_run_identity_mismatch_unverified(tmp_path):
    ev = _collector(tmp_path, fixed('{"run_id":"r-2","x":1}')).retrieve(Cap.RUN_MANIFEST, expected_run_id="r-1")
    assert ev.status is St.IDENTITY_UNVERIFIED and not ev.admitted


def test_run_identity_match_ok(tmp_path):
    ev = _collector(tmp_path, fixed('{"run_id":"r-1","x":1}')).retrieve(Cap.RUN_MANIFEST, expected_run_id="r-1")
    assert ev.status is St.AVAILABLE


def test_run_identity_absent_is_ok(tmp_path):
    ev = _collector(tmp_path, fixed('{"status":"green"}')).retrieve(Cap.DAILY_STATUS, expected_run_id="r-1")
    assert ev.status is St.AVAILABLE and ev.run_id is None


def test_assert_coherent_rejects_mixed_runs(tmp_path):
    a = _collector(tmp_path, fixed('{"run_id":"r-1"}')).retrieve(Cap.DAILY_STATUS)
    b = _collector(tmp_path, fixed('{"run_id":"r-2"}')).retrieve(Cap.RUN_MANIFEST)
    with pytest.raises(ProdEvidenceError):
        assert_coherent([a, b])
    assert_coherent([a, a])   # same run -> coherent


# --- snapshot hardening (Phase Q) -------------------------------------------
def test_snapshot_no_overwrite(tmp_path):
    c = _collector(tmp_path, fixed('{"ok":1}'))
    (tmp_path / "snap").mkdir()
    (tmp_path / "snap" / "pe-fixed.snapshot").write_text("existing")
    with pytest.raises(ProdEvidenceError):
        c._write_snapshot("pe-fixed", b"new")


def test_snapshot_symlink_dir_fails_closed(tmp_path):
    real = tmp_path / "real"; real.mkdir()
    link = tmp_path / "snaplink"; os.symlink(real, link)
    ev = _collector(tmp_path, fixed('{"ok":1}'), snapshot_dir=str(link)).retrieve(Cap.DAILY_STATUS)
    assert ev.status is St.REJECTED and "snapshot" in ev.rejection_reason


# --- no connection-fact leakage to the model --------------------------------
def test_model_view_has_no_connection_facts(tmp_path):
    ev = _collector(tmp_path, fixed('{"run_id":"r1","ok":true}')).retrieve(Cap.DAILY_STATUS)
    view = json.dumps(ev.to_model_view())
    for leak in ("identity_file", "known_hosts", "prod.example", "stockbot-engineer",
                 "source_identity", str(tmp_path / "k")):
        assert leak not in view


# --- fixed, hardened argv; connection facts come only from cfg --------------
def test_default_transport_fixed_argv(tmp_path, monkeypatch):
    captured = {}
    class P:
        returncode = 0; stdout = '{"ok":1}'; stderr = ""
    monkeypatch.setattr(PE.subprocess, "run", lambda argv, **kw: (captured.__setitem__("argv", argv), P())[1])
    PE._default_ssh_transport(_cfg(tmp_path))(["daily-log", "today"])
    a = captured["argv"]
    for opt in ("BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes",
                "ClearAllForwardings=yes", "ForwardAgent=no", "RequestTTY=no"):
        assert opt in a
    assert "-i" in a and str(tmp_path / "k") in a and "stockbot-engineer@prod.example" in a
    assert a[-2:] == ["daily-log", "today"]   # selector is a trailing positional, not an option


# --- controller integration + worker boundary ------------------------------
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
    collector = _collector(tmp_path, fixed('{"run_id":"r-9","complete":false,"failed_stage":"broker_sync"}'))
    model = _scripted(
        json.dumps({"tool_request": {"capability": "READ_PRODUCTION_DAILY_EVIDENCE",
                                     "argument": "PROD_DAILY_STATUS"}}),
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
                                     "argument": "PROD_DAILY_STATUS"}}),
        _finding(summary="abstain; prod evidence unavailable", severity="LOW", confidence=0.2,
                 abstain=True, abstain_reason="bridge not configured"))
    cfg = ctrl.ControllerConfig(repo_root=str(tmp_path / "repo"), registry_db=str(tmp_path / "rd.db"),
                                now_fn=_clock(), infer_fn=model, prod_evidence_collector=None)
    res = ctrl.run_engineering_job(_daily_spec(), cfg)
    assert res.status == "SUCCEEDED" and res.finding.abstain is True


def test_controller_rejects_unknown_model_capability(tmp_path):
    (tmp_path / "repo").mkdir()
    collector = _collector(tmp_path, raising)   # must never be reached
    model = _scripted(
        json.dumps({"tool_request": {"capability": "READ_PRODUCTION_DAILY_EVIDENCE",
                                     "argument": "PROD_ARBITRARY; rm -rf /"}}),
        _finding(summary="proceeding", severity="LOW", confidence=0.3))
    cfg = ctrl.ControllerConfig(repo_root=str(tmp_path / "repo"), registry_db=str(tmp_path / "rd.db"),
                                now_fn=_clock(), infer_fn=model, prod_evidence_collector=collector)
    res = ctrl.run_engineering_job(_daily_spec(), cfg)
    assert res.status == "SUCCEEDED"   # controller handled the rejection, model carried on


def test_repair_job_cannot_use_prod_evidence():
    from portfolio_automation.engineer_worker import policy
    import portfolio_automation.engineer_worker.contracts as C
    with pytest.raises(policy.PolicyError):
        policy.check_tool_allowed(EngineeringJobType.REPAIR_CANDIDATE,
                                  C.ToolCapability.READ_PRODUCTION_DAILY_EVIDENCE)
