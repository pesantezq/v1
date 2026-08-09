# R&D Control Plane — Foundation (Phase 0A)

Package: `portfolio_automation/rd_control/`. Status: deterministic foundation only —
no worker, LLM, sandbox, scheduler, model router, or engineering gateway. Additive;
zero production behaviour change. StockBot remains advisory-only.

## Why StockBot owns authoritative state
The R&D control plane is the deterministic authority over the research job
lifecycle. Future workers (local models, cloud adapters, external specialists) are
**untrusted executors**: they receive a job and hand back a *result*, but they can
never set their own status, define their own authority, or write authoritative
lifecycle state. Only the registry's validated `transition()` path can change a
job's status, and only along a legal edge. This is "technical controls > prompt
compliance" applied to orchestration.

## SQLite vs JSONL
- **SQLite (`data/rd_control.db`) is the single authoritative store.** Jobs and the
  transition audit trail live here, under transactions with `foreign_keys=ON`, WAL,
  and a busy timeout. Schema is migrated on open (`schema_meta.version`; v1 today).
- **JSONL is never co-authoritative.** Any future telemetry/event mirror is
  derivative and non-blocking — a worker must never depend on a telemetry write
  succeeding. (Phase 0A ships no JSONL mirror; the health artifact is the only
  derivative output.)

## Job lifecycle (legal state machine)
```
CREATED ─▶ QUEUED ─▶ ADMITTED ─▶ RUNNING ─▶ RESULT_RECEIVED ─▶ VALIDATING ─┬▶ SUCCEEDED
   │          │          │          │                                       └▶ FAILED_VALIDATION
   └▶CANCELLED └▶CANCELLED└▶CANCELLED│
                                     ├▶ FAILED_WORKER
                                     ├▶ FAILED_SANDBOX
                                     ├▶ TIMED_OUT
                                     ├▶ INTERRUPTED
                                     └▶ CANCELLED
```
Terminal states (`SUCCEEDED, FAILED_VALIDATION, FAILED_WORKER, FAILED_SANDBOX,
TIMED_OUT, INTERRUPTED, CANCELLED`) have **no outgoing edges** — a retry is a NEW
job, never a resurrected terminal one. Illegal edges (e.g. `CREATED→SUCCEEDED`,
`RUNNING→CREATED`, `FAILED_VALIDATION→SUCCEEDED`) raise `IllegalTransitionError`
and leave state unchanged (fail closed).

## Worker authority levels
`W0_ANALYZE` (read-only reasoning) · `W1_RESEARCH_TOOLS` (read-only tools) ·
`W2_DISPOSABLE_MODIFICATION` (scratch-workspace writes) · `W3_SUBMIT_CANDIDATE`
(may submit a candidate for review). **There is deliberately no
production-mutation authority level** — production authority lives entirely outside
this worker system and stays human-gated.

## Provenance & integrity
Each job stores `stockbot_sha`, `input_snapshot_id`/`input_snapshot_hash`,
`worker_*`, `model_*`, network/timeout/output config, and an `input_manifest_hash`
= `sha256` over the canonical JSON of its identity+provenance+config fields. This
gives **integrity** (drift/tamper detection), not cryptographic authenticity. A
future job is traceable end-to-end to source SHA + frozen input + executor + result.

## Restart / recovery
`recover_stale_running(now, max_running_seconds)` finds `RUNNING` jobs whose
`updated_at` is older than the threshold (relative to an injected `now`) and moves
them to `INTERRUPTED` (a legal `RUNNING` edge). Bounded, deterministic, testable;
no automatic retries.

## Health
`build_health()` (observe-only) reports schema status, DB accessibility, counts by
status, open jobs, stale RUNNING, failed jobs, latest activity, and a GREEN/AMBER/
RED rollup. It never returns GREEN if authoritative state cannot be read (unreadable
DB → RED). `run_health()` writes `outputs/policy/rd_control_health.json` (registered
in `artifact_registry.yaml`).

## Relationships
- **Simulation governance:** like the sim-governance lane, R&D is isolated from the
  production decision path and cannot mutate it; unlike it, R&D governs *research
  jobs*, not simulation watchlists.
- **Future sandbox runner:** the sandbox (Agent-Lab netns jail) will execute a
  job's worker; the control plane owns the job's authoritative state before/after —
  the sandbox never writes lifecycle state directly.
- **StratLab:** quantitative validation happens in the `VALIDATING` state; a claim
  becomes `SUCCEEDED` only after StratLab-backed validation (wired in a later phase).
- **Production:** none. No production read/write, no decision-engine coupling, no
  broker path. `outputs/latest/decision_plan.json` and scoring semantics are
  untouched.

## CLI
`python -m portfolio_automation.rd_control {init|create|queue|advance|show|recover|health}`
— drives the registry only; never executes an LLM/sandbox/worker.
