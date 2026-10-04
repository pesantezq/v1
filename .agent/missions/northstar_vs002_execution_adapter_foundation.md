# Mission: northstar_vs002_execution_adapter_foundation

- **Executor:** claude
- **Risk class:** E3
- **Status when this file ships:** PAUSED_NOT_EXECUTED (resumes only after the
  orchestration foundation is durable on `main` with post-merge CI green).
- **Authority source (authoritative):** the EXACT
  `phase_status.yaml` `active_phase.bounded_authorization.paused_bounded_authorization`
  object on base `main`. This prompt EXPLAINS the mission; it never grants, widens,
  or redefines that authorization. On any disagreement, protected state wins and
  the controllers fail closed.

## Objective (bounded, synthetic-only)
Implement and certify the THIN VS-002 execution adapter and governed
result-artifact support: resolve an EXPLICIT caller-supplied package path (no
discovery) → `consumer.validate` (once) → `ValidatedSnapshot` →
`verify_evidence_binding` against the frozen preregistration → `result_runner.run`
→ canonical `ExperimentResult` → governed immutable, collision-refused result
artifact. SYNTHETIC fixtures ONLY.

Execution-time package integrity = recomputable artifact digests + deterministic
`package_id` (`consumer.validate`) PLUS `ValidatedSnapshot` manifest `code_sha`
== frozen `source_production_sha`. The frozen `package_transport_digest` is a
HISTORICAL ATTESTATION ONLY and is NOT recomputed.

## Hard boundaries (fail closed)
Does NOT authorize `northstar_vs002_frozen_execution`, real-package access,
VS-002 metric computation, Phase 0C completion, the 0C Research Store, Phase 0D
or later, C1, production deployment, broker access, or trading/capital authority.
Claude does not merge its own PR.

## Note
An implementation already exists on PR #63 (head `8033ba0`) but is stale against
`main`; it is NOT part of this mission and must not be resumed/repaired here. After
the resume transition is durable, PR #63 may be separately rebased and
re-certified under the controller.
