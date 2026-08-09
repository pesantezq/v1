# Engineer Worker MVP 0A — Daily Diagnostics + Disposable Repair Candidates

**Status: experimental / non-canonical.** The first local engineering worker for
the StockBot Agent Lab. It diagnoses local environment/repository/daily-run
health and produces *disposable* repair candidates for human review. It has **no
production authority** and cannot modify the authoritative environment.

## Purpose
Turn the certified R&D foundation (Phase 0A control plane, Phase 0B sandbox,
Phase 0C Prime-free runtime, local Ollama inference) into a useful worker that:
1. gathers **approved** local diagnostics, 2. analyses StockBot daily-run
evidence, 3. identifies likely engineering/environment failures, 4. emits a
structured finding, 5. optionally proposes a repair **inside a disposable
workspace**, 6. runs **allowlisted** verification, 7. returns a patch + evidence
package, 8. **never** modifies the authoritative environment.

## Authority (the hard rule)
**The model is NOT an executor of arbitrary shell.** Trusted deterministic code
(`controller.py` + `adapters.py` + `policy.py`) decides what actually runs. The
model may only: analyse the bundle, *request* an approved diagnostic capability,
propose a finding, and propose edits to *operator-approved, repair-allowed* files
in a disposable copy. The model may never receive sudo/root, systemctl/nft
mutation, arbitrary subprocess, arbitrary network, production/broker credentials,
SSH, main-branch writes, `git push`/`merge`, production deployment/approval, or
protected scoring authority.

## Architecture
```
EngineeringJobSpecV0
   -> trusted controller (Phase 0A: CREATED->QUEUED->ADMITTED->RUNNING)
   -> approved diagnostic adapters  -> EngineeringDiagnosticBundleV0 (bounded, sanitized)
   -> local model via inference-only Ollama facade  (bounded tool-request loop <=3)
   -> RESULT_RECEIVED -> VALIDATING -> deterministic validation (fail-closed)
   -> EngineeringFindingV0
   -> [REPAIR_CANDIDATE only] disposable workspace copy -> model edits (policy-checked,
      workspace-only) -> controller diff + allowlisted tests -> EngineeringVerificationV0
   -> EngineeringCandidateV0 -> SUCCEEDED -> STOP (human/operator review)
```
Only the controller transitions authoritative Phase 0A job state; the worker never does.

## Trusted vs untrusted boundary
- **Trusted (deterministic code):** job lifecycle, which adapters run, tool
  allowlist decisions, path/protected-path checks, workspace creation, writing
  candidate edits, diff computation, verification status.
- **Untrusted (the model):** free text only — a proposed finding or a single tool
  request or a proposed repair. Everything it returns is schema-validated and
  policy-checked before it has any effect. Malformed output or unsupported
  evidence references → `FAILED_VALIDATION`.

## Supported job types
`ENVIRONMENT_DIAGNOSTIC`, `DAILY_RUN_DIAGNOSTIC`, `REPOSITORY_DIAGNOSTIC`,
`TEST_FAILURE_DIAGNOSTIC`, `REPAIR_CANDIDATE`. **Refused (fail closed):**
`PRODUCTION_REPAIR`, `SERVICE_MUTATION`, `PRODUCTION_DAILY_EXECUTION`,
`DEPLOYMENT`, `MERGE`, `PUSH`, and any unknown type.

## Tool allowlist (per job type)
`READ_DAILY_LOG`, `READ_DAILY_ARTIFACT`, `CHECK_RD_HEALTH`, `CHECK_SANDBOX`,
`CHECK_OLLAMA`, `CHECK_REPO_STATUS`, `RUN_APPROVED_TEST`. Each job type grants a
subset; the model can request only granted capabilities, at most 3 rounds, with
bounded results. Anything else is recorded as a denial and the run continues (or
fails closed). There is deliberately **no** `run_command("...")`.

## Diagnostic adapters (deterministic, bounded, sanitized)
`repo_status` (git status/HEAD), `disk_status`, `ollama_status` (facade
`/api/version`), `rd_control_health` (Phase 0A read-only), `sandbox_status`
(`verify.sh`), `runtime_provenance` (committed vs deployed sha256),
`daily_log_reader` (bounded tail), `daily_artifact_reader` (bounded JSON),
`test_status` (allowlisted pytest target). Fixed argv (never `shell=True`),
output size-bounded, paths symlink/traversal-guarded, no secret values.

## Daily-run relationship — **THIS MVP DOES NOT OWN PRODUCTION DAILY EXECUTION**
`scripts/run_daily_safe.sh` (preflight → venv → .env → run manifest → news
intelligence → Schwab read-only sync → `main.py --run-mode daily` → advisory
artifacts → diagnostics) is the **authoritative** pipeline. The untrusted worker
**must not** invoke it. Instead:
- `DAILY_RUN_DIAGNOSTIC` (implemented): reads an existing daily log + run
  manifest + allowlisted health artifacts and answers *did it complete? which
  stage failed? what's missing/stale? causal vs consequential? enough evidence,
  or abstain?*
- `TRUSTED_DAILY_RUN_REQUEST` (contract/stub, **NOT_IMPLEMENTED**): the worker can
  only *request* `RUN_LOCAL_DRY_DIAGNOSTIC`; it cannot execute a production daily
  run. Rationale: a production run touches broker sync, `.env`, and production
  artifacts — none of which the untrusted worker may hold; production run
  ownership stays with cron / operator / a future trusted production-control-plane
  runner. No VPS execution, no SSH, no production credentials are added here.

## Repair scope + protected paths
Candidate edits are allowed **only** under `docs/`, `tests/`, `devtools/`,
`scripts/dev/`, **and** only for operator-listed `repair_targets`, **and** never
for a protected path. Protected (may diagnose, never modify): `decision_engine.py`,
`decision_plan.json`, `portfolio_automation/scoring/*`, broker execution paths,
`.agent/*`, `config/agent_policy.yaml`, `ops/agent_lab/*` (sandbox security
runtime), `portfolio_automation/rd_control/{sandbox,registry,contracts,health}.py`,
`systemd/*`, `.git/*`, `.env`, credentials/secrets, and the worker's **own**
`engineer_worker/*` core (no self-modification). The MVP may not repair the
sandbox security boundary or its own authority/policy — that needs separate
operator authorization.

## Disposable repair workflow
The **controller** (not the worker) creates a disposable workspace (a copy of the
repo **excluding `.git`** — git internals are never exposed), offers the model
only the approved repairable files' content, writes validated edits into the
workspace **only**, computes a unified diff, and runs allowlisted tests inside
the workspace. The canonical checkout is never touched (proven by test). The
worker cannot `git push`/`merge`/`commit` or choose the workspace location.

## Verification (trusted code sets the status)
`EngineeringVerificationV0`: `changed_paths`, `tests_requested/run`,
`test_results`, `py_compile_ok`, `policy_violations`, `protected_path_ok`,
`diff_size_bytes`, `verification_status ∈ {VERIFIED, FAILED_TESTS, FAILED_POLICY,
FAILED_VALIDATION}`. A candidate is VERIFIED only if all edits are in scope, all
changed Python compiles, and all requested allowlisted tests pass. The model
saying "it works" means nothing.

## Failure semantics (fail-closed)
Unknown/forbidden job type → `PolicyError`. Controller/infra error → job
`FAILED_SANDBOX`. Malformed model output / bad schema / unsupported evidence /
non-convergence → job `FAILED_VALIDATION`. Out-of-scope / protected / traversal
edit → verification `FAILED_POLICY` (edit not written). Failing tests / compile →
verification `FAILED_TESTS`.

## Observability (telemetry, NOT authority)
Recorded per job: `job_id`, `job_type`, `model`, `prompt_version`, repo/runtime
sha, tools requested/granted/denied, tool rounds, schema-valid flag, candidate
paths, verification status. This is evaluation telemetry only; the Phase 0A
registry — not telemetry — is the authoritative lifecycle.

## Northstar compatibility
All schemas are `Engineering*V0` and marked `experimental_noncanonical`. They do
**not** define or claim the canonical Northstar names (`EvidenceRef`,
`EvidenceSnapshot`, `ResearchTask`, `WorkerResult`, `ResearchClaim`,
`ExperimentSpec`, `ExperimentResult`). Intended mapping when Northstar Phase 0B
canonical contracts land: `EngineeringDiagnosticBundleV0.diagnostic_sources[*]`
→ `EvidenceRef`/`EvidenceSnapshot`; `EngineeringFindingV0` →
`WorkerResult`/`ResearchClaim`; `EngineeringJobSpecV0` → a governed
`ResearchTask`/engineering-task; verification → an `ExperimentResult`-style
record. The worker would then live under the Northstar governed-worker runtime
(Phase 5), constrained by `config/agent_policy.yaml` (advisory-only, human-only
production/capital authority).

## Future self-healing (explicitly not enabled)
This MVP stops at a VERIFIED candidate for human review. It does **not**
auto-apply, auto-commit, restart services, or repair production. A future,
separately-authorized phase could add governed apply — but self-modification and
production repair remain out of scope.
