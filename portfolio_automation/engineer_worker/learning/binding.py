"""SHA-bound verification evidence (Phase 12).

The 0B.3 certifier identified a real hardening gap: GPT verification records were
corroborated through task identity, timestamp, and independent artifact
re-verification rather than DIRECT cryptographic binding to the candidate artifact.
That is adequate for a supervised single-controller mission and NOT adequate before
controller authority expands — an unbound record cannot prove WHICH artifact was
verified.

New records bind to: task_id, attempt_id, base_sha, candidate_sha, diff_hash,
evidence_manifest_hash, deterministic_verdict, GPT verdict, verifier identity, and
timestamp.

Historical compatibility is explicit, not accidental: records predating this
contract are ``LEGACY_CORROBORATED`` — still valid evidence, marked as
weaker-bound. Existing 0B.3 certification is NOT invalidated and is NOT retroactively
upgraded (which would be fabricating binding strength that was never measured).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Any

from portfolio_automation.engineer_worker.learning import SCHEMA_KIND

BINDING_SCHEMA_VERSION = "engineering.verification_binding.v0"

_SHA1_LEN, _SHA256_LEN = 40, 64


class BindingStrength(str, Enum):
    CANDIDATE_BOUND = "CANDIDATE_BOUND"          # cryptographically bound to an artifact
    LEGACY_CORROBORATED = "LEGACY_CORROBORATED"  # pre-contract; identity/timestamp corroborated
    UNBOUND = "UNBOUND"                          # insufficient binding — never treat as certified


class BindingError(ValueError):
    """Deterministic, fail-closed binding error."""


def _is_sha(value: str) -> bool:
    if not isinstance(value, str):
        return False
    v = value.strip().lower()
    return len(v) in (_SHA1_LEN, _SHA256_LEN) and all(c in "0123456789abcdef" for c in v)


def diff_hash(diff_text: str) -> str:
    """Stable hash of the candidate diff. Normalizes line endings only — content is
    otherwise hashed verbatim so an altered diff cannot reuse an old verdict."""
    normalized = (diff_text or "").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def evidence_manifest_hash(manifest: dict[str, Any]) -> str:
    """Hash of the evidence manifest (changed paths, tests run, results).

    Canonical JSON (sorted keys) so an equivalent manifest always hashes equal and a
    reordered one does not read as a different body of evidence."""
    blob = json.dumps(manifest, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_evidence_manifest(changed_paths: list[str], tests_run: list[str],
                            test_results: dict[str, str],
                            acceptance_criteria: list[str]) -> dict[str, Any]:
    return {
        "changed_paths": sorted(changed_paths),
        "tests_run": sorted(tests_run),
        "test_results": {k: test_results[k] for k in sorted(test_results)},
        "acceptance_criteria": list(acceptance_criteria),
    }


@dataclass(frozen=True)
class VerificationBindingV0:
    """Binds a verification verdict to the exact artifact it judged."""
    task_id: str
    attempt_id: str
    base_sha: str
    candidate_sha: str
    diff_hash: str
    evidence_manifest_hash: str
    deterministic_verdict: str
    gpt_verdict: str
    verifier_identity: str                  # e.g. "gpt-4o" — the INDEPENDENT verifier
    verified_at: str
    binding_strength: str = BindingStrength.CANDIDATE_BOUND.value
    legacy_corroboration: list[str] = field(default_factory=list)
    schema_version: str = BINDING_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND

    def __post_init__(self) -> None:
        if self.binding_strength == BindingStrength.CANDIDATE_BOUND.value:
            if not _is_sha(self.base_sha) or not _is_sha(self.candidate_sha):
                raise BindingError("CANDIDATE_BOUND requires real base_sha and candidate_sha")
            if not _is_sha(self.diff_hash) or not _is_sha(self.evidence_manifest_hash):
                raise BindingError("CANDIDATE_BOUND requires diff_hash and evidence_manifest_hash")
            if self.base_sha.strip().lower() == self.candidate_sha.strip().lower():
                raise BindingError("candidate_sha must differ from base_sha")
        if not self.verifier_identity:
            raise BindingError("verifier identity is required")

    @property
    def is_strongly_bound(self) -> bool:
        return self.binding_strength == BindingStrength.CANDIDATE_BOUND.value

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "VerificationBindingV0", **asdict(self)}


def bind_verification(*, task_id: str, attempt_id: str, base_sha: str, candidate_sha: str,
                      diff_text: str, manifest: dict[str, Any], deterministic_verdict: str,
                      gpt_verdict: str, verifier_identity: str, verified_at: str
                      ) -> VerificationBindingV0:
    """Build a strongly-bound verification record for NEW verifications."""
    return VerificationBindingV0(
        task_id=task_id, attempt_id=attempt_id, base_sha=base_sha, candidate_sha=candidate_sha,
        diff_hash=diff_hash(diff_text), evidence_manifest_hash=evidence_manifest_hash(manifest),
        deterministic_verdict=deterministic_verdict, gpt_verdict=gpt_verdict,
        verifier_identity=verifier_identity, verified_at=verified_at,
        binding_strength=BindingStrength.CANDIDATE_BOUND.value)


def legacy_binding(*, task_id: str, attempt_id: str, deterministic_verdict: str,
                   gpt_verdict: str, verifier_identity: str, verified_at: str,
                   corroboration: list[str]) -> VerificationBindingV0:
    """Wrap a PRE-CONTRACT verification as legacy-compatible evidence.

    Marked LEGACY_CORROBORATED and never claimed as candidate-bound: the binding
    strength records what was actually measured, not what we wish had been."""
    return VerificationBindingV0(
        task_id=task_id, attempt_id=attempt_id, base_sha="", candidate_sha="",
        diff_hash="", evidence_manifest_hash="", deterministic_verdict=deterministic_verdict,
        gpt_verdict=gpt_verdict, verifier_identity=verifier_identity, verified_at=verified_at,
        binding_strength=BindingStrength.LEGACY_CORROBORATED.value,
        legacy_corroboration=list(corroboration))


def verify_binding(binding: VerificationBindingV0, *, diff_text: str,
                   manifest: dict[str, Any]) -> bool:
    """Re-derive the hashes and confirm the record still describes THIS artifact.

    A legacy record always returns False here: it was never bound, so it can never
    be re-proven bound — that is the honest answer, not a failure."""
    if not binding.is_strongly_bound:
        return False
    return (binding.diff_hash == diff_hash(diff_text)
            and binding.evidence_manifest_hash == evidence_manifest_hash(manifest))


def binding_required_for_authority_expansion(bindings: list[VerificationBindingV0]) -> bool:
    """Whether the evidence base is strong enough to support expanding controller
    authority. Requires at least one strongly-bound record and no UNBOUND record."""
    if not bindings:
        return False
    if any(b.binding_strength == BindingStrength.UNBOUND.value for b in bindings):
        return False
    return any(b.is_strongly_bound for b in bindings)
