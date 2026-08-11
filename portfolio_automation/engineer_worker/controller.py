"""Trusted deterministic controller for the Engineer Worker MVP.

This is the ONLY component that: transitions Phase 0A job state, decides which
adapters run, creates the disposable repair workspace, writes candidate edits
(workspace-only, policy-checked), computes diffs, and sets verification status.
The model contributes text only; it never executes commands, never transitions
state, and never touches the canonical checkout.
"""
from __future__ import annotations

import difflib
import json
import py_compile
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from portfolio_automation.rd_control import registry as reg
from portfolio_automation.rd_control.contracts import (
    JobType, JobStatus, WorkerAuthority,
)
from portfolio_automation.engineer_worker import adapters, policy, model_adapter, prod_evidence
from portfolio_automation.engineer_worker.contracts import (
    ContractError, DiagnosticSource, EngineeringJobSpecV0, EngineeringJobType,
    EngineeringDiagnosticBundleV0, EngineeringFindingV0, EngineeringCandidateV0,
    EngineeringVerificationV0, VerificationStatus, ToolCapability, RepairScope,
    FORBIDDEN_JOB_TYPES, validate_finding, validate_repair_proposal,
)

PROMPT_VERSION = "ew0a.1"
_MAX_BUNDLE_BYTES = 48_000
_HARD_MAX_TOOL_ROUNDS = 3


@dataclass
class ControllerConfig:
    repo_root: str
    registry_db: str
    now_fn: Callable[[], str]
    infer_fn: model_adapter.InferenceFn
    model_name: str = model_adapter.DEFAULT_MODEL
    ollama_url: str | None = None
    sandbox_verify_script: str | None = None
    sandbox_src_dir: str | None = None
    committed_sha: str | None = None
    deployed_sha: str | None = None
    worker_id: str = "engineer-worker"
    worker_version: str = "0A"
    workspace_parent: str | None = None   # trusted-chosen; NOT worker-controlled
    # Temporary Direct Production Evidence Bridge (trusted-side ONLY). When None,
    # READ_PRODUCTION_DAILY_EVIDENCE fails closed to UNAVAILABLE. This object holds
    # the SSH connection facts and NEVER enters the sandbox or model input.
    prod_evidence_collector: "prod_evidence.ProductionEvidenceCollector | None" = None


@dataclass
class EngineeringResult:
    job_id: str
    status: str
    finding: EngineeringFindingV0 | None = None
    candidate: EngineeringCandidateV0 | None = None
    telemetry: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def _authority_for(job_type: EngineeringJobType) -> WorkerAuthority:
    if job_type == EngineeringJobType.REPAIR_CANDIDATE:
        return WorkerAuthority.W3_SUBMIT_CANDIDATE
    return WorkerAuthority.W0_ANALYZE


# ---------------------------------------------------------------------------
# Diagnostic bundle construction (only trusted adapters, only granted sources)
# ---------------------------------------------------------------------------
def _build_bundle(cfg: ControllerConfig, spec: EngineeringJobSpecV0,
                  job_id: str) -> EngineeringDiagnosticBundleV0:
    granted = policy.tools_for_job(spec.job_type)
    sources: list[DiagnosticSource] = []

    repo_commit = None
    if ToolCapability.CHECK_REPO_STATUS in granted:
        rs = adapters.repo_status(cfg.repo_root)
        sources.append(rs)
        repo_commit = rs.data.get("head")

    # disk is always safe/read-only context
    sources.append(adapters.disk_status(cfg.repo_root))

    if ToolCapability.CHECK_RD_HEALTH in granted:
        sources.append(adapters.rd_control_health(cfg.registry_db))
    if ToolCapability.CHECK_OLLAMA in granted:
        sources.append(adapters.ollama_status(cfg.ollama_url))
    if ToolCapability.CHECK_SANDBOX in granted:
        sources.append(adapters.sandbox_status(cfg.sandbox_verify_script, cfg.sandbox_src_dir))
    if cfg.committed_sha or cfg.deployed_sha:
        sources.append(adapters.runtime_provenance(cfg.committed_sha, cfg.deployed_sha))

    if spec.job_type == EngineeringJobType.DAILY_RUN_DIAGNOSTIC:
        if spec.daily_log_path and ToolCapability.READ_DAILY_LOG in granted:
            try:
                sources.append(adapters.daily_log_reader(cfg.repo_root, spec.daily_log_path))
            except policy.PolicyError as e:
                sources.append(DiagnosticSource(name="daily_log", ok=False,
                               provenance="daily_log_reader", error=f"policy: {e}"))
        for rel in spec.daily_artifact_paths[:10]:
            if ToolCapability.READ_DAILY_ARTIFACT in granted:
                try:
                    sources.append(adapters.daily_artifact_reader(cfg.repo_root, rel))
                except policy.PolicyError as e:
                    sources.append(DiagnosticSource(name=f"artifact:{rel}", ok=False,
                                   provenance="daily_artifact_reader", error=f"policy: {e}"))

    constraints = [
        "worker is untrusted; only allowlisted diagnostics were collected",
        "no shell/network/authority available to the model",
    ]
    if spec.expected_stages:
        constraints.append("expected_stages=" + ",".join(spec.expected_stages[:30]))

    return EngineeringDiagnosticBundleV0(
        job_id=job_id, job_type=spec.job_type, created_at=cfg.now_fn(),
        repo_commit=repo_commit, runtime_commit=cfg.deployed_sha,
        diagnostic_sources=sources, known_constraints=constraints,
    )


def _run_tool(cfg: ControllerConfig, spec: EngineeringJobSpecV0,
              cap: ToolCapability, argument: str) -> DiagnosticSource:
    """Execute ONE allowlisted tool request. Raises PolicyError on any denial."""
    policy.check_tool_allowed(spec.job_type, cap)
    if cap == ToolCapability.CHECK_REPO_STATUS:
        return adapters.repo_status(cfg.repo_root)
    if cap == ToolCapability.CHECK_RD_HEALTH:
        return adapters.rd_control_health(cfg.registry_db)
    if cap == ToolCapability.CHECK_OLLAMA:
        return adapters.ollama_status(cfg.ollama_url)
    if cap == ToolCapability.CHECK_SANDBOX:
        return adapters.sandbox_status(cfg.sandbox_verify_script, cfg.sandbox_src_dir)
    if cap == ToolCapability.READ_DAILY_LOG:
        return adapters.daily_log_reader(cfg.repo_root, argument)
    if cap == ToolCapability.READ_DAILY_ARTIFACT:
        return adapters.daily_artifact_reader(cfg.repo_root, argument)
    if cap == ToolCapability.RUN_APPROVED_TEST:
        policy.check_test_allowed(spec.allowed_tests, argument)
        return adapters.test_status(cfg.repo_root, argument)
    if cap == ToolCapability.READ_PRODUCTION_DAILY_EVIDENCE:
        return _read_production_evidence(cfg, argument)
    raise policy.PolicyError(f"unknown capability: {cap}")


def _read_production_evidence(cfg: ControllerConfig, argument: str) -> DiagnosticSource:
    """Trusted-side production evidence retrieval. The argument is
    "<capability>[:<selector>]" (e.g. "daily-log:today", "db-query:latest-daily-run").
    The collector (if configured) runs SSH in the trusted controller; the sandbox
    /model receive ONLY the admitted, sanitized view — never raw stdout, host, or
    key. Fails closed to UNAVAILABLE when no collector is wired."""
    prov = "prod-evidence-bridge (trusted-side, admitted only)"
    if cfg.prod_evidence_collector is None:
        return DiagnosticSource(name="prod_evidence", ok=False, provenance=prov,
                                data={"status": prod_evidence.ProductionEvidenceStatus.UNAVAILABLE.value},
                                error="production evidence bridge not configured")
    # The model names a PROD_* capability (+ optional date selector) — never a
    # raw verb, path, or SQL. Resolution is deterministic + fail-closed.
    raw_cap, _, sel = (argument or "").partition(":")
    try:
        cap, resolved_sel = prod_evidence.resolve_model_capability(raw_cap.strip(), sel.strip())
    except prod_evidence.ProdEvidenceError as e:
        return DiagnosticSource(name="prod_evidence", ok=False, provenance=prov,
                                data={"status": prod_evidence.ProductionEvidenceStatus.REJECTED.value},
                                error=str(e))
    ev = cfg.prod_evidence_collector.retrieve(cap, resolved_sel)
    ok = ev.status is prod_evidence.ProductionEvidenceStatus.AVAILABLE
    return DiagnosticSource(name="prod_evidence", ok=ok, provenance=prov,
                            data=ev.to_model_view(),
                            error=None if ok else (ev.rejection_reason or ev.status.value))


# ---------------------------------------------------------------------------
# The bounded model interaction: converge on a validated finding
# ---------------------------------------------------------------------------
def _run_model_loop(cfg: ControllerConfig, spec: EngineeringJobSpecV0,
                    bundle: EngineeringDiagnosticBundleV0, telem: dict) -> str:
    """RUNNING phase: run the bounded tool-request loop and return the model's
    final RAW finding text. Parsing/validation of that text is deferred to the
    VALIDATING phase so a bad finding maps to a *legal* FAILED_VALIDATION edge.
    Tool granted/denied decisions are made here by trusted policy."""
    allowed_caps = sorted(c.value for c in policy.tools_for_job(spec.job_type))
    tool_results: list[dict] = []
    max_rounds = min(spec.max_tool_rounds, _HARD_MAX_TOOL_ROUNDS)
    last_raw = ""

    for round_idx in range(max_rounds + 1):
        telem["tool_rounds_used"] = round_idx
        payload = bundle.to_model_payload(_MAX_BUNDLE_BYTES)
        user = model_adapter.build_finding_user_prompt(payload, tool_results, allowed_caps)
        last_raw = cfg.infer_fn(model_adapter.FINDING_SYSTEM, user)
        try:
            obj = model_adapter.extract_json_object(last_raw)
        except ContractError:
            return last_raw   # malformed -> VALIDATING will reject it

        if isinstance(obj.get("tool_request"), dict) and round_idx < max_rounds:
            tr = obj["tool_request"]
            telem["tools_requested"].append(tr.get("capability"))
            try:
                cap = ToolCapability(str(tr.get("capability")))
            except ValueError:
                telem["tools_denied"].append(str(tr.get("capability")))
                tool_results.append({"request": tr, "denied": "unknown_capability"})
                continue
            try:
                ds = _run_tool(cfg, spec, cap, str(tr.get("argument", "")))
            except policy.PolicyError as e:
                telem["tools_denied"].append(cap.value)
                tool_results.append({"request": tr, "denied": str(e)})
                continue
            telem["tools_granted"].append(cap.value)
            bundle.diagnostic_sources.append(ds)   # keep evidence refs valid
            tool_results.append({"request": tr, "result": {"name": ds.name, "ok": ds.ok,
                                 "data": ds.data, "error": ds.error}})
            continue

        return last_raw   # a finding candidate (or, at the last round, a lingering
                          # tool_request that VALIDATING will reject)
    return last_raw


# ---------------------------------------------------------------------------
# Disposable repair workspace + deterministic verification
# ---------------------------------------------------------------------------
def _copy_workspace(cfg: ControllerConfig, job_id: str) -> Path:
    parent = cfg.workspace_parent or tempfile.gettempdir()
    ws = Path(tempfile.mkdtemp(prefix=f"ew_ws_{job_id}_", dir=parent))
    src = Path(cfg.repo_root)
    dst = ws / "repo"
    shutil.copytree(src, dst, symlinks=False,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc",
                                                  ".venv", "node_modules", "ew_ws_*"))
    return dst


def _verify_candidate(cfg: ControllerConfig, spec: EngineeringJobSpecV0, ws_repo: Path,
                      proposal, candidate_id: str) -> tuple[EngineeringVerificationV0, str]:
    changed: list[str] = []
    violations: list[str] = []
    diff_chunks: list[str] = []
    approved_targets = set(spec.repair_targets)

    for edit in proposal.edits:
        rel = edit["path"]
        # trusted path gates (defense in depth): allowlist prefix, not protected,
        # operator-approved target, and safe within the workspace.
        if not policy.is_repair_allowed(rel):
            violations.append(f"path not in repair scope: {rel}")
            continue
        if rel not in approved_targets:
            violations.append(f"path not an operator-approved repair target: {rel}")
            continue
        try:
            dst = policy.safe_join(ws_repo, rel)
        except policy.PolicyError as e:
            violations.append(f"unsafe path {rel}: {e}")
            continue
        original = ""
        src_orig = Path(cfg.repo_root) / rel
        if src_orig.is_file():
            original = src_orig.read_text(encoding="utf-8", errors="replace")
        new_content = edit["new_content"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(new_content, encoding="utf-8")
        changed.append(rel)
        diff_chunks.extend(difflib.unified_diff(
            original.splitlines(keepends=True), new_content.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}"))

    diff_text = "".join(diff_chunks)

    if violations:
        return (EngineeringVerificationV0(
            candidate_id=candidate_id, verification_status=VerificationStatus.FAILED_POLICY,
            changed_paths=changed, policy_violations=violations, protected_path_ok=False,
            diff_size_bytes=len(diff_text.encode("utf-8"))), diff_text)

    # py_compile changed python files (in the workspace copy)
    pycompile_ok = True
    for rel in changed:
        if rel.endswith(".py"):
            try:
                py_compile.compile(str(ws_repo / rel), doraise=True)
            except py_compile.PyCompileError:
                pycompile_ok = False

    # run allowlisted tests inside the workspace
    tests_requested = list(proposal.tests_to_run)
    tests_run: list[str] = []
    test_results: dict[str, str] = {}
    all_pass = True
    for t in tests_requested:
        try:
            policy.check_test_allowed(spec.allowed_tests, t)
        except policy.PolicyError as e:
            test_results[t] = f"DENIED: {e}"
            all_pass = False
            continue
        ds = adapters.test_status(ws_repo, t)
        tests_run.append(t)
        test_results[t] = ("PASS" if ds.ok else "FAIL") + f" ({ds.data.get('summary','')})"
        all_pass = all_pass and ds.ok

    status = VerificationStatus.VERIFIED
    if not pycompile_ok or not all_pass:
        status = VerificationStatus.FAILED_TESTS

    return (EngineeringVerificationV0(
        candidate_id=candidate_id, verification_status=status, changed_paths=changed,
        tests_requested=tests_requested, tests_run=tests_run, test_results=test_results,
        py_compile_ok=pycompile_ok, policy_violations=[], protected_path_ok=True,
        diff_size_bytes=len(diff_text.encode("utf-8"))), diff_text)


def _propose_repair(cfg: ControllerConfig, spec: EngineeringJobSpecV0,
                    finding: EngineeringFindingV0, job_id: str,
                    telem: dict) -> EngineeringCandidateV0:
    ws_repo = _copy_workspace(cfg, job_id)
    candidate_id = f"cand-{job_id}"
    # Offer ONLY operator-approved, repair-allowed, existing files (with content).
    repairable_files = []
    for rel in spec.repair_targets:
        if not policy.is_repair_allowed(rel):
            continue
        f = ws_repo / rel
        if f.is_file():
            repairable_files.append({"path": rel,
                                     "content": f.read_text(encoding="utf-8", errors="replace")[:40_000]})
    user = model_adapter.build_repair_user_prompt(finding.to_dict(), repairable_files,
                                                  spec.allowed_tests)
    raw = cfg.infer_fn(model_adapter.REPAIR_SYSTEM, user)
    obj = model_adapter.extract_json_object(raw)
    proposal = validate_repair_proposal(obj, job_id=job_id)
    verification, diff_text = _verify_candidate(cfg, spec, ws_repo, proposal, candidate_id)
    telem["candidate_paths"] = verification.changed_paths
    telem["verification_status"] = verification.verification_status.value
    return EngineeringCandidateV0(
        job_id=job_id, candidate_id=candidate_id, finding=finding,
        workspace_path=str(ws_repo), diff_text=diff_text, verification=verification,
    )


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------
def run_engineering_job(spec: EngineeringJobSpecV0, cfg: ControllerConfig) -> EngineeringResult:
    """Execute an engineering job end-to-end through the Phase 0A lifecycle.
    Only this function transitions authoritative job state."""
    # Fail closed on forbidden / unknown job types.
    if not isinstance(spec.job_type, EngineeringJobType) or spec.job_type.value in FORBIDDEN_JOB_TYPES:
        raise policy.PolicyError(f"forbidden or unknown job type: {spec.job_type!r}")

    telem: dict[str, Any] = {
        "job_type": spec.job_type.value, "model": cfg.model_name,
        "prompt_version": PROMPT_VERSION, "committed_sha": cfg.committed_sha,
        "deployed_sha": cfg.deployed_sha, "tools_requested": [], "tools_granted": [],
        "tools_denied": [], "schema_valid": False,
    }

    with reg.connect(cfg.registry_db) as conn:
        rec = reg.create_job(conn, job_type=JobType.DEVELOPMENT,
                             authority=_authority_for(spec.job_type),
                             created_at=cfg.now_fn(), worker_id=cfg.worker_id,
                             worker_version=cfg.worker_version)
        job_id = rec.job_id
        telem["job_id"] = job_id
        reg.transition(conn, job_id, JobStatus.QUEUED, at=cfg.now_fn(), reason="queued", actor="controller")
        reg.transition(conn, job_id, JobStatus.ADMITTED, at=cfg.now_fn(), reason="admitted", actor="controller")
        reg.transition(conn, job_id, JobStatus.RUNNING, at=cfg.now_fn(), reason="running", actor="controller")

        # RUNNING: build the bundle and obtain the model's raw finding text.
        # Infrastructure failures here map to FAILED_SANDBOX (legal from RUNNING).
        try:
            bundle = _build_bundle(cfg, spec, job_id)
            raw = _run_model_loop(cfg, spec, bundle, telem)
        except Exception as e:  # noqa: BLE001 - controller/infra failure, fail closed
            reg.transition(conn, job_id, JobStatus.FAILED_SANDBOX, at=cfg.now_fn(),
                           reason="controller error building/executing job", actor="controller",
                           error_class="engineering_controller", error_message=str(e)[:300])
            return EngineeringResult(job_id=job_id, status="FAILED_SANDBOX",
                                     telemetry=telem, error=str(e)[:300])

        # A result (raw model text) was received from the untrusted worker.
        reg.transition(conn, job_id, JobStatus.RESULT_RECEIVED, at=cfg.now_fn(),
                       reason="model result received", actor="controller")
        reg.transition(conn, job_id, JobStatus.VALIDATING, at=cfg.now_fn(),
                       reason="validating", actor="controller")

        # VALIDATING: deterministic validation of the model's finding. Any failure
        # (malformed JSON, bad schema, unsupported evidence, non-convergence) maps
        # to FAILED_VALIDATION.
        try:
            obj = model_adapter.extract_json_object(raw)
            finding = validate_finding(obj, job_id=job_id,
                                       valid_evidence_refs=bundle.evidence_ids())
            telem["schema_valid"] = True
        except (ContractError, ValueError) as e:
            reg.transition(conn, job_id, JobStatus.FAILED_VALIDATION, at=cfg.now_fn(),
                           reason="model output failed validation", actor="controller",
                           error_class="engineering_validation", error_message=str(e)[:300])
            return EngineeringResult(job_id=job_id, status="FAILED_VALIDATION",
                                     telemetry=telem, error=str(e)[:300])

        candidate = None
        if (spec.job_type == EngineeringJobType.REPAIR_CANDIDATE and spec.repair_allowed
                and finding.repair_recommended and not finding.abstain):
            try:
                candidate = _propose_repair(cfg, spec, finding, job_id, telem)
            except (ContractError, ValueError) as e:
                reg.transition(conn, job_id, JobStatus.FAILED_VALIDATION, at=cfg.now_fn(),
                               reason="repair proposal failed validation", actor="controller",
                               error_class="engineering_repair_validation", error_message=str(e)[:300])
                return EngineeringResult(job_id=job_id, status="FAILED_VALIDATION",
                                         finding=finding, telemetry=telem, error=str(e)[:300])

        reg.transition(conn, job_id, JobStatus.SUCCEEDED, at=cfg.now_fn(),
                       reason="engineering job complete", actor="controller")
        return EngineeringResult(job_id=job_id, status="SUCCEEDED", finding=finding,
                                 candidate=candidate, telemetry=telem)
