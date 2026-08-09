"""Experimental (non-canonical) V0 contracts for the Engineer Worker MVP.

Every schema here carries ``schema_kind == experimental_noncanonical`` and a
``V0`` suffix. They intentionally do NOT reuse Northstar canonical names
(EvidenceRef/ResearchTask/WorkerResult/...). Validation is deterministic and
fail-closed: malformed model output or unsupported evidence references raise
``ContractError`` (the caller maps that to FAILED_VALIDATION).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER

SCHEMA_KIND = EXPERIMENTAL_MARKER
FINDING_SCHEMA_VERSION = "engineering.finding.v0"
BUNDLE_SCHEMA_VERSION = "engineering.diagnostic_bundle.v0"
REPAIR_SCHEMA_VERSION = "engineering.repair_proposal.v0"
CANDIDATE_SCHEMA_VERSION = "engineering.candidate.v0"
VERIFICATION_SCHEMA_VERSION = "engineering.verification.v0"


class ContractError(ValueError):
    """Raised on any schema/validation violation (deterministic, fail-closed)."""


class EngineeringJobType(str, Enum):
    ENVIRONMENT_DIAGNOSTIC = "ENVIRONMENT_DIAGNOSTIC"
    DAILY_RUN_DIAGNOSTIC = "DAILY_RUN_DIAGNOSTIC"
    REPOSITORY_DIAGNOSTIC = "REPOSITORY_DIAGNOSTIC"
    TEST_FAILURE_DIAGNOSTIC = "TEST_FAILURE_DIAGNOSTIC"
    REPAIR_CANDIDATE = "REPAIR_CANDIDATE"


# Job types that are explicitly refused (fail closed) — production/authority.
FORBIDDEN_JOB_TYPES = frozenset({
    "PRODUCTION_REPAIR", "SERVICE_MUTATION", "PRODUCTION_DAILY_EXECUTION",
    "DEPLOYMENT", "MERGE", "PUSH",
})


class Severity(str, Enum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ToolCapability(str, Enum):
    """The only capabilities the model may request. Anything else fails closed."""
    READ_DAILY_LOG = "READ_DAILY_LOG"
    READ_DAILY_ARTIFACT = "READ_DAILY_ARTIFACT"
    CHECK_RD_HEALTH = "CHECK_RD_HEALTH"
    CHECK_SANDBOX = "CHECK_SANDBOX"
    CHECK_OLLAMA = "CHECK_OLLAMA"
    CHECK_REPO_STATUS = "CHECK_REPO_STATUS"
    RUN_APPROVED_TEST = "RUN_APPROVED_TEST"


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    FAILED_TESTS = "FAILED_TESTS"
    FAILED_POLICY = "FAILED_POLICY"
    FAILED_VALIDATION = "FAILED_VALIDATION"


class RepairScope(str, Enum):
    NONE = "NONE"
    DOCS = "DOCS"
    TESTS = "TESTS"
    DEV_TOOLING = "DEV_TOOLING"


@dataclass
class EngineeringJobSpecV0:
    job_type: EngineeringJobType
    title: str
    schema_kind: str = SCHEMA_KIND
    # Bounded, explicit inputs the controller is allowed to gather for this job.
    daily_log_path: str | None = None            # relative path, controller-validated
    daily_artifact_paths: list[str] = field(default_factory=list)
    expected_stages: list[str] = field(default_factory=list)
    allowed_tests: list[str] = field(default_factory=list)   # allowlist for RUN_APPROVED_TEST
    repair_allowed: bool = False                 # only REPAIR_CANDIDATE may set True
    # Operator-approved, repair-allowed relative paths the model may edit. The
    # model can NEVER introduce a path not in this list (defense in depth).
    repair_targets: list[str] = field(default_factory=list)
    max_tool_rounds: int = 3

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["job_type"] = self.job_type.value
        return d


@dataclass
class DiagnosticSource:
    name: str
    ok: bool
    provenance: str            # how it was gathered (deterministic adapter id)
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class EngineeringDiagnosticBundleV0:
    job_id: str
    job_type: EngineeringJobType
    created_at: str
    repo_commit: str | None
    runtime_commit: str | None
    schema_version: str = BUNDLE_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND
    diagnostic_sources: list[DiagnosticSource] = field(default_factory=list)
    known_constraints: list[str] = field(default_factory=list)

    def source(self, name: str) -> DiagnosticSource | None:
        for s in self.diagnostic_sources:
            if s.name == name:
                return s
        return None

    def evidence_ids(self) -> set[str]:
        """Stable evidence identifiers a finding may legitimately reference."""
        return {f"source:{s.name}" for s in self.diagnostic_sources}

    def to_model_payload(self, max_bytes: int) -> dict[str, Any]:
        """Bounded, sanitized view handed to the model (no secrets, size-capped)."""
        payload = {
            "schema_version": self.schema_version,
            "schema_kind": self.schema_kind,
            "job_id": self.job_id,
            "job_type": self.job_type.value,
            "repo_commit": self.repo_commit,
            "runtime_commit": self.runtime_commit,
            "known_constraints": self.known_constraints,
            "valid_evidence_refs": sorted(self.evidence_ids()),
            "diagnostic_sources": [
                {"name": s.name, "ok": s.ok, "provenance": s.provenance,
                 "data": s.data, "error": s.error}
                for s in self.diagnostic_sources
            ],
        }
        import json
        blob = json.dumps(payload, ensure_ascii=True)
        if len(blob) > max_bytes:
            # Deterministically shrink: drop the largest source 'data' fields.
            for s in payload["diagnostic_sources"]:
                s["data"] = {"_truncated": True}
            blob = json.dumps(payload, ensure_ascii=True)
            payload["_truncated_to_bytes"] = max_bytes
        return payload


@dataclass
class EngineeringFindingV0:
    job_id: str
    summary: str
    severity: Severity
    confidence: float                     # advisory only, 0..1
    abstain: bool
    repair_recommended: bool
    schema_version: str = FINDING_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND
    observations: list[str] = field(default_factory=list)
    likely_causes: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    recommended_checks: list[str] = field(default_factory=list)
    repair_scope: RepairScope = RepairScope.NONE
    abstain_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["repair_scope"] = self.repair_scope.value
        return d


@dataclass
class EngineeringRepairProposalV0:
    job_id: str
    rationale: str
    schema_version: str = REPAIR_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND
    # Each edit: {"path": <relative>, "new_content": <full file text>}
    edits: list[dict[str, str]] = field(default_factory=list)
    tests_to_run: list[str] = field(default_factory=list)


@dataclass
class EngineeringVerificationV0:
    candidate_id: str
    verification_status: VerificationStatus
    schema_version: str = VERIFICATION_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND
    changed_paths: list[str] = field(default_factory=list)
    tests_requested: list[str] = field(default_factory=list)
    tests_run: list[str] = field(default_factory=list)
    test_results: dict[str, str] = field(default_factory=dict)
    py_compile_ok: bool | None = None
    policy_violations: list[str] = field(default_factory=list)
    protected_path_ok: bool | None = None
    diff_size_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["verification_status"] = self.verification_status.value
        return d


@dataclass
class EngineeringCandidateV0:
    job_id: str
    candidate_id: str
    finding: EngineeringFindingV0
    schema_version: str = CANDIDATE_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND
    workspace_path: str | None = None
    diff_text: str | None = None
    verification: EngineeringVerificationV0 | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "schema_kind": self.schema_kind,
            "job_id": self.job_id,
            "candidate_id": self.candidate_id,
            "workspace_path": self.workspace_path,
            "diff_text": self.diff_text,
            "finding": self.finding.to_dict(),
            "verification": self.verification.to_dict() if self.verification else None,
        }


# ---------------------------------------------------------------------------
# Deterministic validators (fail-closed)
# ---------------------------------------------------------------------------
_MAX_LIST = 50
_MAX_STR = 4000


def _check_str(v: Any, name: str, *, maxlen: int = _MAX_STR, allow_none: bool = False) -> str | None:
    if v is None and allow_none:
        return None
    if not isinstance(v, str):
        raise ContractError(f"{name} must be a string, got {type(v).__name__}")
    if len(v) > maxlen:
        raise ContractError(f"{name} exceeds {maxlen} chars")
    return v


def _check_str_list(v: Any, name: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise ContractError(f"{name} must be a list")
    if len(v) > _MAX_LIST:
        raise ContractError(f"{name} exceeds {_MAX_LIST} items")
    out = []
    for i, item in enumerate(v):
        out.append(_check_str(item, f"{name}[{i}]"))
    return out


def validate_finding(raw: dict[str, Any], *, job_id: str,
                     valid_evidence_refs: set[str]) -> EngineeringFindingV0:
    """Validate model-produced finding dict. Raises ContractError on any anomaly,
    including references to evidence that was not actually collected."""
    if not isinstance(raw, dict):
        raise ContractError("finding must be a JSON object")
    if raw.get("schema_version") not in (None, FINDING_SCHEMA_VERSION):
        raise ContractError("finding schema_version mismatch")
    # The model may not declare a different job_id (no authority laundering).
    if "job_id" in raw and raw["job_id"] != job_id:
        raise ContractError("finding job_id mismatch")

    summary = _check_str(raw.get("summary"), "summary")
    if not summary:
        raise ContractError("summary is required")
    try:
        severity = Severity(str(raw.get("severity", "INFO")).upper())
    except ValueError:
        raise ContractError(f"invalid severity: {raw.get('severity')!r}")
    conf = raw.get("confidence", 0.0)
    if not isinstance(conf, (int, float)) or not (0.0 <= float(conf) <= 1.0):
        raise ContractError("confidence must be a number in [0,1]")
    abstain = bool(raw.get("abstain", False))
    repair_recommended = bool(raw.get("repair_recommended", False))
    try:
        repair_scope = RepairScope(str(raw.get("repair_scope", "NONE")).upper())
    except ValueError:
        raise ContractError(f"invalid repair_scope: {raw.get('repair_scope')!r}")

    evidence_refs = _check_str_list(raw.get("evidence_refs"), "evidence_refs")
    bad = [r for r in evidence_refs if r not in valid_evidence_refs]
    if bad:
        raise ContractError(f"unsupported evidence_refs (not collected): {bad}")

    return EngineeringFindingV0(
        job_id=job_id,
        summary=summary,
        severity=severity,
        confidence=float(conf),
        abstain=abstain,
        repair_recommended=repair_recommended,
        observations=_check_str_list(raw.get("observations"), "observations"),
        likely_causes=_check_str_list(raw.get("likely_causes"), "likely_causes"),
        evidence_refs=evidence_refs,
        recommended_checks=_check_str_list(raw.get("recommended_checks"), "recommended_checks"),
        repair_scope=repair_scope,
        abstain_reason=_check_str(raw.get("abstain_reason"), "abstain_reason", allow_none=True),
    )


def validate_repair_proposal(raw: dict[str, Any], *, job_id: str) -> EngineeringRepairProposalV0:
    if not isinstance(raw, dict):
        raise ContractError("repair proposal must be a JSON object")
    if raw.get("schema_version") not in (None, REPAIR_SCHEMA_VERSION):
        raise ContractError("repair schema_version mismatch")
    rationale = _check_str(raw.get("rationale"), "rationale") or ""
    edits_raw = raw.get("edits", [])
    if not isinstance(edits_raw, list) or not edits_raw:
        raise ContractError("edits must be a non-empty list")
    if len(edits_raw) > 20:
        raise ContractError("too many edits (max 20)")
    edits: list[dict[str, str]] = []
    for i, e in enumerate(edits_raw):
        if not isinstance(e, dict):
            raise ContractError(f"edits[{i}] must be an object")
        path = _check_str(e.get("path"), f"edits[{i}].path", maxlen=512)
        content = _check_str(e.get("new_content"), f"edits[{i}].new_content", maxlen=200_000)
        if not path:
            raise ContractError(f"edits[{i}].path required")
        edits.append({"path": path, "new_content": content or ""})
    return EngineeringRepairProposalV0(
        job_id=job_id, rationale=rationale, edits=edits,
        tests_to_run=_check_str_list(raw.get("tests_to_run"), "tests_to_run"),
    )
