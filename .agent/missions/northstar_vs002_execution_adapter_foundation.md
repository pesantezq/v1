# northstar_vs002_execution_adapter_foundation

This is the protected fallback contract for the VS-002 execution-adapter mission.

If an existing open implementation PR is already tagged for this mission, do not create a duplicate. Repair/update that exact PR instead.

## Durable scientific and authority boundaries

- Phase remains northstar_phase_0c.
- Real frozen VS-002 execution is NOT authorized.
- Real package discovery/open/enumeration/validation/evaluation is NOT authorized.
- Synthetic fixtures only.
- Frozen preregistration/scientific rules remain unchanged.
- Frozen transport digest is HISTORICAL_ATTESTATION_ONLY; do not recompute it or invent a new transport algorithm.
- Execution-time package integrity is consumer.validate() recomputable artifact digests + deterministic package_id + manifest code_sha binding.
- The adapter owns only explicit package path -> validate once -> ValidatedSnapshot -> evidence binding -> deterministic result runner -> canonical ExperimentResult -> governed immutable/collision-refused result -> deterministic verification.
- No network, FMP, Schwab, production mutation, broker access, trades, Phase 0D, C1, or capital authority.
- Existing durable result runner remains the sole scientific calculation owner.
- Engineering failures must never be converted into scientific outcomes.

## Orchestration contract

- Do not wait or poll for CI after pushing the candidate.
- Do not merge.
- Do not advance protected state.
- Push the candidate branch and return a structured final report.
- The trusted GitHub controller owns exact-head CI/Codex gating, merge, post-merge certification, and the next handoff.
- Any next mission marked human_required must stop for operator approval.

Read the full durable VS-002 preregistration, result-runner contracts, adapter tests/docs, and current protected state before modifying code.
