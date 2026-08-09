"""R&D Control Plane — versioned contracts (Phase 0A).

The single source of truth for the deterministic R&D lifecycle: job types, the
job status enum, worker authority levels, the legal state-transition table, and
the :class:`JobRecord` payload. No worker, LLM, sandbox, scheduler, or router is
defined here — those are later phases. Nothing in this module trades, calls a
broker, mutates holdings, or touches production decision state.

Design invariants (mirrors run_manifest.py / next_stage/contracts.py):
  * ``schema_version`` is explicit and stored with every record.
  * Timestamps are always injected by the caller (this module never calls
    ``datetime.now``) so behaviour is deterministic in tests.
  * Authority belongs to deterministic StockBot code. A future worker/LLM result
    can never set its own authoritative status — only the registry's validated
    transition path can, and only along a legal edge.
  * There is deliberately NO production-mutation worker authority level.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = "1"


class JobType(str, Enum):
    """Conceptual worker categories. No type has a real worker yet (Phase 0A)."""
    FINANCE_RESEARCH = "FINANCE_RESEARCH"
    QUANT_EXPERIMENT = "QUANT_EXPERIMENT"
    DEVELOPMENT = "DEVELOPMENT"
    DESIGN_REVIEW = "DESIGN_REVIEW"


class JobStatus(str, Enum):
    """Authoritative lifecycle states. Only the registry may set these, and only
    along an edge in :data:`LEGAL_TRANSITIONS`."""
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    ADMITTED = "ADMITTED"
    RUNNING = "RUNNING"
    RESULT_RECEIVED = "RESULT_RECEIVED"
    VALIDATING = "VALIDATING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_VALIDATION = "FAILED_VALIDATION"
    FAILED_WORKER = "FAILED_WORKER"
    FAILED_SANDBOX = "FAILED_SANDBOX"
    TIMED_OUT = "TIMED_OUT"
    INTERRUPTED = "INTERRUPTED"
    CANCELLED = "CANCELLED"


class WorkerAuthority(str, Enum):
    """Bounded authority a future worker may be granted. There is intentionally
    no production-mutation level here; production authority lives entirely outside
    this worker system and is gated separately by humans."""
    W0_ANALYZE = "W0_ANALYZE"                      # read-only reasoning/analysis
    W1_RESEARCH_TOOLS = "W1_RESEARCH_TOOLS"        # read-only research tools
    W2_DISPOSABLE_MODIFICATION = "W2_DISPOSABLE_MODIFICATION"  # scratch workspace writes
    W3_SUBMIT_CANDIDATE = "W3_SUBMIT_CANDIDATE"    # may submit a candidate for review


# ---------------------------------------------------------------------------
# Legal state machine (deterministic; illegal edges are refused by the registry)
# ---------------------------------------------------------------------------
LEGAL_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.CREATED:          frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    JobStatus.QUEUED:           frozenset({JobStatus.ADMITTED, JobStatus.CANCELLED}),
    JobStatus.ADMITTED:         frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING:          frozenset({
        JobStatus.RESULT_RECEIVED, JobStatus.FAILED_WORKER, JobStatus.FAILED_SANDBOX,
        JobStatus.TIMED_OUT, JobStatus.INTERRUPTED, JobStatus.CANCELLED,
    }),
    JobStatus.RESULT_RECEIVED:  frozenset({JobStatus.VALIDATING}),
    JobStatus.VALIDATING:       frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED_VALIDATION}),
    # Terminal states have no outgoing edges. A retry is a NEW job, never a
    # resurrection of a terminal one.
    JobStatus.SUCCEEDED:        frozenset(),
    JobStatus.FAILED_VALIDATION: frozenset(),
    JobStatus.FAILED_WORKER:    frozenset(),
    JobStatus.FAILED_SANDBOX:   frozenset(),
    JobStatus.TIMED_OUT:        frozenset(),
    JobStatus.INTERRUPTED:      frozenset(),
    JobStatus.CANCELLED:        frozenset(),
}

TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    s for s, nxt in LEGAL_TRANSITIONS.items() if not nxt
)


class RDControlError(Exception):
    """Base class for R&D control-plane errors."""


class IllegalTransitionError(RDControlError):
    """Raised when a status change is not a legal edge (fail closed)."""


class JobNotFoundError(RDControlError):
    """Raised when a job_id is not present in the registry."""


def is_legal_transition(current: JobStatus, target: JobStatus) -> bool:
    """True iff ``current -> target`` is an allowed edge."""
    return target in LEGAL_TRANSITIONS.get(current, frozenset())


def assert_legal_transition(current: JobStatus, target: JobStatus) -> None:
    """Raise :class:`IllegalTransitionError` unless the edge is legal."""
    if not is_legal_transition(current, target):
        raise IllegalTransitionError(
            f"illegal transition {current.value} -> {target.value}"
        )


# ---------------------------------------------------------------------------
# Job record
# ---------------------------------------------------------------------------
@dataclass
class JobRecord:
    """One R&D job. Persisted authoritatively in SQLite (see registry.py).

    Provenance fields (stockbot_sha, input_snapshot_*, worker_*, model_*) let a
    future job be traced to the exact source + frozen input + executor. Hashes
    here provide INTEGRITY (tamper/drift detection), not cryptographic
    authenticity.
    """
    job_id: str
    job_type: JobType
    status: JobStatus
    authority: WorkerAuthority
    created_at: str
    updated_at: str
    # Provenance
    stockbot_sha: str | None = None
    input_snapshot_id: str | None = None
    input_snapshot_hash: str | None = None
    worker_id: str | None = None
    worker_version: str | None = None
    model_id: str | None = None
    model_provider: str | None = None
    # Execution config
    network_profile: str | None = None
    timeout_seconds: int | None = None
    max_output_bytes: int | None = None
    # Integrity
    input_manifest_hash: str | None = None
    result_hash: str | None = None
    # Failure detail
    error_class: str | None = None
    error_message: str | None = None
    # Schema
    schema_version: str = SCHEMA_VERSION

    def to_row(self) -> dict[str, Any]:
        """Flat dict for SQLite (enums -> their string values)."""
        d = asdict(self)
        d["job_type"] = self.job_type.value
        d["status"] = self.status.value
        d["authority"] = self.authority.value
        return d

    @staticmethod
    def from_row(row: dict[str, Any]) -> "JobRecord":
        return JobRecord(
            job_id=row["job_id"],
            job_type=JobType(row["job_type"]),
            status=JobStatus(row["status"]),
            authority=WorkerAuthority(row["authority"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            stockbot_sha=row.get("stockbot_sha"),
            input_snapshot_id=row.get("input_snapshot_id"),
            input_snapshot_hash=row.get("input_snapshot_hash"),
            worker_id=row.get("worker_id"),
            worker_version=row.get("worker_version"),
            model_id=row.get("model_id"),
            model_provider=row.get("model_provider"),
            network_profile=row.get("network_profile"),
            timeout_seconds=row.get("timeout_seconds"),
            max_output_bytes=row.get("max_output_bytes"),
            input_manifest_hash=row.get("input_manifest_hash"),
            result_hash=row.get("result_hash"),
            error_class=row.get("error_class"),
            error_message=row.get("error_message"),
            schema_version=row.get("schema_version", SCHEMA_VERSION),
        )


# Fields folded into the input-manifest integrity hash (identity + provenance +
# config that define WHAT this job is; excludes mutable lifecycle fields like
# status/updated_at/result/error).
_MANIFEST_FIELDS = (
    "job_id", "job_type", "authority", "stockbot_sha",
    "input_snapshot_id", "input_snapshot_hash",
    "worker_id", "worker_version", "model_id", "model_provider",
    "network_profile", "timeout_seconds", "max_output_bytes",
    "schema_version",
)


def compute_input_manifest_hash(record: JobRecord) -> str:
    """``sha256:<hex>`` over the canonical JSON of the job's identity/provenance/
    config fields. Integrity only (detects drift/tamper), not authenticity."""
    row = record.to_row()
    core = {k: row.get(k) for k in _MANIFEST_FIELDS}
    canon = json.dumps(core, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, allow_nan=False)
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()
