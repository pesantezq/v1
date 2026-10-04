"""Thin trusted VS-002 execution adapter.

``experimental_noncanonical``. The narrow bridge between an already-validated
frozen evidence package and the pure, certified result runner:

    explicit caller-supplied package path
        -> consumer.validate (ONCE)
        -> ValidatedSnapshot
        -> execution-time identity checks (package_id, manifest code_sha,
           historical transport attestation, evidence binding)
        -> result_runner.run (the pure science; unchanged)
        -> VS002Result
        -> canonical ExperimentResult (observations = VS002Result.to_observations())
        -> governed immutable, collision-refused result artifact
        -> deterministic verification (+ replay on the SAME snapshot)

What this adapter does NOT do, by construction:
  * no package discovery (no glob/latest/newest/scan/fallback/env selection);
  * no evidence acquisition and no network/provider/production calls;
  * no governance-state mutation (.agent / config untouched);
  * no mission dispatch; it grants NO authority and executes NO real experiment
    unless a caller supplies a real validated package under a separate future
    authorization. This module and its tests are SYNTHETIC-ONLY.

Transport-digest ruling (durable governance, PR #61): the frozen
``package_transport_digest`` is a HISTORICAL ATTESTATION ONLY and is NEVER
recomputed here. Execution-time package integrity is established by
``consumer.validate`` (recomputed artifact digests + deterministic package_id +
schema/PIT/witness) PLUS ``manifest["code_sha"] == frozen source_production_sha``;
``result_runner.verify_evidence_binding`` still matches the frozen EvidenceIdentity
metadata (including the historical transport digest) exactly.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from portfolio_automation import data_governance as DG
from portfolio_automation.northstar.canonical import (
    canonical_dumps,
    content_hash,
    deterministic_id,
)
from portfolio_automation.northstar.experiments import ExperimentResult, ExperimentSpec
from portfolio_automation.northstar.provenance import PRODUCER_SYSTEM, Provenance
from portfolio_automation.vs002_evidence import consumer as CON
from portfolio_automation.vs002_evidence import result_contract as RC
from portfolio_automation.vs002_evidence import result_runner as RR
from portfolio_automation.vs002_evidence.result_contract import EvidenceIdentity

ADAPTER_ID = "vs002_evidence.execution_adapter"
ADAPTER_VERSION = "v1"
EXECUTION_RESULT_SCHEMA = "engineering.vs002_execution_result.v1"
EXECUTION_RESULT_SCHEMA_KIND = "experimental_noncanonical"
HORIZON_WINDOW = "7d"
TRANSPORT_DIGEST_VERIFICATION_MODE = "HISTORICAL_ATTESTATION_ONLY"

_DISCOVERY_TOKENS = ("latest", "newest", "current", "*", "?", "[")


class VS002ExecutionError(Exception):
    """An engineering / certification blocker in the adapter path.

    This is NEVER a scientific outcome. ``category`` names the exact blocker and
    the original cause is preserved via ``__cause__`` so it stays testable."""

    def __init__(self, category: str, message: str):
        super().__init__(f"{category}: {message}")
        self.category = category


@dataclass(frozen=True)
class VS002ExecutionRequest:
    """Trusted execution inputs. Grants NO execution authority by itself; the real
    frozen-execution mission remains separately gated."""
    package_path: Path
    preregistration: Mapping[str, Any]       # loaded frozen preregistration dict
    historical_transport_digest: str         # frozen attestation metadata (NOT recomputed)
    runner_source_sha: str                   # durable main SHA the runner ships at
    generated_at: datetime                   # injected, tz-aware (determinism)
    result_filename: str                     # path within the VS002_RESULT namespace
    user_id: str = "owner"
    base_dir: Path | str = "outputs"
    runner_id: str = RC.DEFAULT_RUNNER_ID
    runner_version: str = RC.RUNNER_VERSION


def _require(cond: bool, category: str, message: str) -> None:
    if not cond:
        raise VS002ExecutionError(category, message)


def _no_discovery(package_path: Path) -> None:
    """Structurally forbid discovery/wildcard selection; the path is explicit.

    This never touches the filesystem — existence/validity is consumer.validate's
    job. It only refuses a path that is empty or looks like a selection pattern,
    so the caller cannot smuggle 'pick the latest package' past the contract."""
    text = str(package_path).strip()
    _require(bool(text), "PACKAGE_PATH_INVALID", "package_path must be a non-empty explicit path")
    low = text.lower()
    for token in _DISCOVERY_TOKENS:
        _require(token not in low, "PACKAGE_DISCOVERY_FORBIDDEN",
                 f"package_path must be an exact path, not a discovery pattern ({token!r})")


def _resolve_destination(req: VS002ExecutionRequest) -> Path:
    """Canonical intended path inside the VS002_RESULT namespace, with containment
    (``..``/symlink traversal fails closed via validate_output_path)."""
    try:
        intended = DG.get_output_path(DG.OutputNamespace.VS002_RESULT, req.result_filename,
                                      user_id=req.user_id, base_dir=req.base_dir)
        return DG.validate_output_path(DG.OutputNamespace.VS002_RESULT, intended,
                                       user_id=req.user_id, base_dir=req.base_dir)
    except DG.DataGovernanceError as e:
        raise VS002ExecutionError("INVALID_RESULT_DESTINATION", str(e)) from e


def _build_envelope(result: "RC.VS002Result", snap: "CON.ValidatedSnapshot",
                    req: VS002ExecutionRequest, run_identity: str) -> dict[str, Any]:
    """Assemble the canonical Northstar ExperimentResult (observations =
    VS002Result.to_observations(), authority-screened) plus a top-level adapter
    provenance wrapper (kept OUT of observations because 'execution' is an
    authority-screened key)."""
    recorded_at = req.generated_at
    evidence_refs = tuple(snap.evidence_refs())
    universe = tuple(sorted(snap.eligible_universe))
    prov = Provenance(producer_id=ADAPTER_ID, producer_type=PRODUCER_SYSTEM,
                      recorded_at=recorded_at, code_version=ADAPTER_VERSION)
    # The hypothesis under test is the FROZEN preregistration, not a worker-minted
    # claim — so the spec references it by a deterministic rcl_ id pinned to the
    # frozen prereg identity rather than instantiating a fresh ResearchClaim
    # (which would require citing its own evidence sources).
    hypothesis_claim_id = deterministic_id("rcl", {
        "frozen_preregistration_id": result.preregistration_id,
        "freeze_digest": result.preregistration_freeze_digest,
        "hypothesis": "VS-002",
        "testable_metric": "return.vs002_risk_adjusted_excess_7d",
        "direction": "increase",
    })
    spec = ExperimentSpec(
        hypothesis_claim_id=hypothesis_claim_id,
        universe=universe,
        as_of=recorded_at,
        evaluation_windows=(HORIZON_WINDOW,),
        metrics=("return.vs002_net_risk_adjusted_excess_7d",
                 "return.vs002_cohort_spearman_ic",
                 "return.vs002_no_action_net_7d"),
        success_gate="PREREGISTERED_CRITERIA_MET iff H1_MET AND H2_MET AND NO_ACTION_BEAT",
        abandon_gate="PREREGISTERED_CRITERIA_NOT_MET when gates fail with sufficient cohorts",
        provenance=prov,
        notes="VS-002 execution-adapter spec; scientific rules are frozen in the preregistration.",
    )
    exp = ExperimentResult(
        experiment_spec_id=spec.experiment_spec_id,
        provenance=prov,
        windows_evaluated=(HORIZON_WINDOW,),
        evidence_refs=evidence_refs,
        observations=result.to_observations(),
        notes="VS-002 result via the thin execution adapter; observe_only, grants no authority.",
    )
    adapter_provenance = {
        "adapter_id": ADAPTER_ID,
        "adapter_version": ADAPTER_VERSION,
        "runner_id": req.runner_id,
        "runner_version": req.runner_version,
        "runner_source_sha": req.runner_source_sha,
        "preregistration_id": result.preregistration_id,
        "preregistration_freeze_digest": result.preregistration_freeze_digest,
        "evidence_package_id": result.evidence_binding.package_id,
        "transport_digest_historical_attestation": result.evidence_binding.package_transport_digest,
        "transport_digest_verification_mode": TRANSPORT_DIGEST_VERIFICATION_MODE,
        "transport_digest_recomputed": False,
        "source_production_sha": result.evidence_binding.source_production_sha,
        "evidence_schema_version": result.evidence_binding.evidence_schema_version,
        "result_schema_version": result.schema_version,
        "generated_at": result.generated_at,
        "run_identity": run_identity,
        "experiment_spec_id": spec.experiment_spec_id,
        "experiment_result_id": exp.experiment_result_id,
        "hypothesis_claim_id": hypothesis_claim_id,
    }
    envelope = {
        "schema_version": EXECUTION_RESULT_SCHEMA,
        "schema_kind": EXECUTION_RESULT_SCHEMA_KIND,
        "observe_only": True,
        "grants_authority": False,
        "vs002_executed": result.vs002_executed,
        "adapter_provenance": adapter_provenance,
        # Persist BOTH the spec and the result so the exs_… reference the result
        # carries is resolvable/reproducible from the artifact alone (a result with
        # an unreproducible spec reference is contract-invalid).
        "experiment_spec": spec.to_canonical_dict(),
        "experiment_result": exp.to_canonical_dict(),
    }
    envelope["result_digest"] = content_hash(envelope)
    return envelope


def _execution_identity(result: "RC.VS002Result", req: VS002ExecutionRequest) -> str:
    """Deterministic one-shot identity: exactly this frozen experiment, evidence
    package, and runner. No timestamp, no result values, no filesystem path."""
    return deterministic_id("vs002exec", {
        "preregistration_id": result.preregistration_id,
        "preregistration_freeze_digest": result.preregistration_freeze_digest,
        "package_id": result.evidence_binding.package_id,
        "package_transport_digest": result.evidence_binding.package_transport_digest,
        "runner_id": req.runner_id,
        "runner_version": req.runner_version,
        "runner_source_sha": req.runner_source_sha,
    })


def execute_frozen_vs002(req: VS002ExecutionRequest) -> dict[str, Any]:
    """Run the frozen VS-002 evaluation over ONE explicitly-supplied validated
    package and persist an immutable, collision-refused result artifact.

    Returns a record with the written path, result digest, execution identity,
    and the scientific classification. Raises VS002ExecutionError for every
    engineering/certification blocker (never a scientific outcome)."""
    if req.generated_at.tzinfo is None:
        raise VS002ExecutionError("NON_DETERMINISTIC_TIMESTAMP",
                                  "generated_at must be timezone-aware")
    _no_discovery(req.package_path)

    # Collision refusal BEFORE validation/compute: never overwrite a durable result.
    destination = _resolve_destination(req)
    _require(not destination.exists(), "RESULT_COLLISION",
             f"a result artifact already exists at {destination}; refusing to recompute/overwrite")

    # Validate the ONE supplied package EXACTLY ONCE; reuse the snapshot everywhere.
    try:
        snap = CON.validate(req.package_path)
    except CON.SnapshotInvalid as e:
        raise VS002ExecutionError("PACKAGE_VALIDATION_FAILED", str(e)) from e

    try:
        binding = RR.verify_preregistration(req.preregistration)
    except RR.PreregistrationMismatch as e:
        raise VS002ExecutionError("PREREGISTRATION_MISMATCH", str(e)) from e

    # G8 execution-time integrity: manifest package_id + code_sha vs the frozen binding.
    _require(snap.manifest.get("package_id") == binding.evidence_binding.package_id,
             "PACKAGE_ID_MISMATCH",
             f"manifest package_id {snap.manifest.get('package_id')!r} != frozen "
             f"{binding.evidence_binding.package_id!r}")
    _require(snap.manifest.get("code_sha") == binding.evidence_binding.source_production_sha,
             "SOURCE_PRODUCTION_SHA_MISMATCH",
             f"manifest code_sha {snap.manifest.get('code_sha')!r} != frozen "
             f"source_production_sha {binding.evidence_binding.source_production_sha!r}")

    # Actual evidence identity: package_id/source_sha/schema from the validated
    # manifest; transport digest from the frozen HISTORICAL ATTESTATION (not recomputed).
    try:
        provided = EvidenceIdentity(
            package_id=snap.manifest["package_id"],
            package_transport_digest=req.historical_transport_digest,
            source_production_sha=snap.manifest["code_sha"],
            evidence_schema_version=snap.manifest["schema_version"],
        )
    except KeyError as e:
        raise VS002ExecutionError("MANIFEST_INCOMPLETE",
                                  f"manifest missing identity field {e}") from e
    try:
        RR.verify_evidence_binding(binding, provided, manifest=snap.manifest)
    except RR.EvidenceBindingMismatch as e:
        raise VS002ExecutionError("EVIDENCE_BINDING_MISMATCH", str(e)) from e

    # The pure certified runner. StudentTTableError is an ENGINEERING blocker,
    # NEVER EVIDENCE_INCONCLUSIVE.
    try:
        result = RR.run(snap, req.preregistration, evidence_identity=provided,
                        generated_at=req.generated_at, runner_id=req.runner_id,
                        runner_version=req.runner_version)
    except RC.StudentTTableError as e:
        raise VS002ExecutionError("UNSUPPORTED_STUDENT_T_DF", str(e)) from e

    run_identity = _execution_identity(result, req)
    envelope = _build_envelope(result, snap, req, run_identity)
    try:
        payload = canonical_dumps(envelope)
    except Exception as e:  # finite/strict-JSON failure is an engineering blocker
        raise VS002ExecutionError("RESULT_SERIALIZATION_FAILED", str(e)) from e

    # VERIFY BEFORE PUBLISH: digest, invariants, and deterministic replay on the
    # SAME snapshot (no reopen). If anything fails, NOTHING is written — the
    # immutable destination is never occupied by an unverified artifact, so a
    # corrected retry with the same filename is still possible.
    _verify_doc(envelope, req, snap=snap, expected_identity=provided)

    # Exclusive, atomic publication: immutable, collision-refused, never overwrites.
    written = _publish_exclusive(destination, payload)

    # Post-publication byte-integrity read-back (cheap; replay already ran above).
    verify_frozen_vs002_result(written, req)

    return {
        "written_path": str(written),
        "result_digest": envelope["result_digest"],
        "run_identity": run_identity,
        "criterion_outcome": result.criterion_outcome.value,
        "skill_outcome": result.skill_outcome.value,
        "vs002_executed": result.vs002_executed,
        "package_validation_count": 1,
    }


def _publish_exclusive(destination: Path, payload: str, encoding: str = "utf-8") -> Path:
    """Atomic, EXCLUSIVE publication: write a temp file in the destination dir then
    ``os.link`` it to the final name. ``os.link`` is atomic and fails closed if the
    destination already exists, so two concurrent publishers cannot both succeed and
    an existing immutable result is never overwritten (no check-then-replace window)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(destination.parent),
                               prefix=f".{destination.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(payload)
        try:
            os.link(tmp, destination)
        except FileExistsError as e:
            raise VS002ExecutionError(
                "RESULT_COLLISION",
                f"a result artifact already exists at {destination} at publication") from e
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return destination


def _verify_doc(doc: Mapping[str, Any], req: VS002ExecutionRequest, *,
                snap: Optional["CON.ValidatedSnapshot"] = None,
                expected_identity: Optional[EvidenceIdentity] = None) -> dict[str, Any]:
    """Pure verification of a result envelope (the in-memory dict pre-publish OR a
    reloaded artifact post-publish). Optionally replays the runner on the SAME
    already-loaded snapshot (NO package reopen) and compares observations.

    Replay is VERIFICATION of the same authorized execution, not a new experiment,
    and writes nothing."""
    _require(doc.get("schema_version") == EXECUTION_RESULT_SCHEMA,
             "RESULT_VERIFICATION_FAILED", "unexpected result schema_version")
    stored_digest = doc.get("result_digest")
    recomputed = {k: v for k, v in doc.items() if k != "result_digest"}
    _require(content_hash(recomputed) == stored_digest,
             "RESULT_VERIFICATION_FAILED", "result_digest does not recompute")
    ap = doc.get("adapter_provenance") or {}
    _require(ap.get("preregistration_freeze_digest") == RR.EXPECTED_FREEZE_DIGEST,
             "RESULT_VERIFICATION_FAILED", "preregistration freeze digest mismatch")
    _require(ap.get("transport_digest_recomputed") is False
             and ap.get("transport_digest_verification_mode") == TRANSPORT_DIGEST_VERIFICATION_MODE,
             "RESULT_VERIFICATION_FAILED", "transport-digest attestation mode drift")
    _require(doc.get("observe_only") is True and doc.get("grants_authority") is False,
             "RESULT_VERIFICATION_FAILED", "artifact must be observe_only and grant no authority")
    # The persisted spec must be present and must be the one the result references.
    spec_doc = doc.get("experiment_spec") or {}
    result_doc = doc.get("experiment_result") or {}
    _require(bool(spec_doc.get("experiment_spec_id"))
             and spec_doc.get("experiment_spec_id") == result_doc.get("experiment_spec_id"),
             "RESULT_VERIFICATION_FAILED",
             "experiment_result references a spec not persisted in the artifact")
    # Deterministic replay (same snapshot, no reopen) — observations must match.
    if snap is not None and expected_identity is not None:
        replay = RR.run(snap, req.preregistration, evidence_identity=expected_identity,
                        generated_at=req.generated_at, runner_id=req.runner_id,
                        runner_version=req.runner_version)
        _require(replay.to_observations() == result_doc.get("observations"),
                 "RESULT_VERIFICATION_FAILED", "deterministic replay observations differ")
    return {"verified": True, "result_digest": stored_digest}


def verify_frozen_vs002_result(path: Path, req: VS002ExecutionRequest, *,
                               snap: Optional["CON.ValidatedSnapshot"] = None,
                               expected_identity: Optional[EvidenceIdentity] = None
                               ) -> dict[str, Any]:
    """Reload + verify a written adapter artifact (optionally replaying on the SAME
    snapshot). Reads the file once; never reopens the evidence package."""
    _require(path.exists(), "RESULT_VERIFICATION_FAILED", f"artifact absent: {path}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    return _verify_doc(doc, req, snap=snap, expected_identity=expected_identity)
