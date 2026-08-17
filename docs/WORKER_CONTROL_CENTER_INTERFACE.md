# Worker Control Center — Controller-Owned Interface (read-model contract)

**Owner:** the control-plane / controller session (Claude Code, currently).
**Consumer:** the Worker Control Center GUI session
(`feature/worker-control-center-gui`). The GUI **consumes** this; it must **not**
create competing authoritative definitions, and it must **never** read authority by
scraping logs.

```
authoritative state  ->  trusted projection (portfolio_automation/engineer_worker/ew0a_readmodels.py)  ->  GUI
```
NOT the reverse. Every projection here is **non-authoritative** and **read-only by
construction** — the projection module imports only read accessors and no mutator
(proven by `tests/test_ew0a_readmodels.py`).

## Authoritative sources (map)
| Domain | Authoritative source | Producer |
|---|---|---|
| Engineer authority level + grants + forbidden ops | `config/ew0a_authority.json` (protected) | trusted controller |
| Runtime policy + current mission + AUTO_* flags | `config/ew0a_runtime.json` (protected) | trusted controller |
| Task/verification outcomes | `ew0a.OutcomeRecord` (append-only JSONL) | orchestrator |
| Verification verdicts | `ew0a.EngineeringVerificationV0` / `gpt_supervisor.SupervisorDecision` | deterministic gate + GPT |
| Controller apprenticeship (C0.5) | `docs/EW0A_0B3_RECORDS.jsonl` (`ControllerDecisionCandidateV0` / `ApprenticeshipComparison`) | controller |
| Mission progress | contract presence in `portfolio_automation/northstar/` + records | northstar contracts |

## Read models (projections)
All live in `ew0a_readmodels.py`; `build_dashboard(repo_root)` assembles them. Each
projection carries `schema_version="engineering.readmodel.v0"`. Fields with no
authoritative backend are the string `PENDING_BACKEND` — **never fabricated**.

### ControllerSummary
Dynamic controller identity — **`claude_code == controller` is NOT a permanent
schema invariant**; a future controller may be the Engineer / Daily Manager / another
certified controller. Fields: `controller_identity` (str, current), `controller_role`,
`controller_level` (controller ladder; the Engineer's C0.5 is tracked separately),
`current_mission`, `operational_state`, `controller_since` (`PENDING_BACKEND` — no
authoritative record yet), `escalation_role`. Security: operational. Current-state.
The GUI may **never** request an action against it.

### SupervisorSummary (GPT independent verifier)
`availability` / `current_state` / `outage_state` (`PENDING_BACKEND` — no live health
probe), `recent_pass|repair|escalate|abstain|unavailable` (derived counts from
records), `last_successful_verification`, `measured_latency_ms` (`PENDING_BACKEND`),
`verification_queue` (`PENDING_BACKEND`). **NEVER exposes** the API key, auth headers,
request bodies, or hidden reasoning (asserted by test). Security: operational.

### WorkerSummary (Engineer)
Persistent identity `engineer.local_qwen2_5_7b`, `role`, `ew_authority` (A1),
`controller_level` (`C0.5_SHADOW`), `current_mission`, `recent_verification_outcomes`,
`escalation_state`. `operational_state` / `current_task` / `queue_size` /
`activity_summary` / `next_action` = `PENDING_BACKEND` — **no `WorkerHeartbeatV0`
exists; do NOT fabricate online/heartbeat state.**

### WorkerAuthoritySummary
`level`, `grants`, `forbidden_ops`, and explicit booleans
`can_mutate_main|can_merge|can_deploy|can_write_production|can_self_promote` — all
**false**. Authoritative (from `config/ew0a_authority.json`).

### MissionSummary
`deliverables` (name → `VERIFIED|NOT_STARTED`), `verified_count`, `total_required`,
`is_complete`. **Progress is derived from VERIFIED required deliverables, never from
raw task counts.** 0B.3 today: ExperimentSpec `VERIFIED`, ExperimentResult
`VERIFIED`, CapitalProposal/ExitProposal/OutcomeRecord/StrategyPassport `NOT_STARTED`.

### VerificationSummary (the ladder — `worker_complete != VERIFIED`)
`implementation_result` (a *claim*, not authority) → `scope_policy_gate` →
`deterministic_verification` → `acceptance_criteria` → `gpt_verdict` → `final_status`.
`final_status = VERIFIED` **only** when deterministic PASS **and** GPT PASS. A run with
implementation `COMPLETE` + GPT `REPAIR` projects `final = NOT_VERIFIED`. A deterministic
failure that short-circuits the supervisor projects `gpt_verdict = NOT_CONSULTED` (never
PASS/FAIL). Use `project_verification(...)`.

### ApprenticeshipSummary (C0.5 — honest, never smoothed)
`controller_level`, `decisions_shadowed`, `task_selection_agreements`,
`risk_agreements`, `routing_agreements`, `missed_escalations`,
`unsafe_underclassifications`, `authority_expansion_proposals`, `c1_readiness`.
**Current honest evidence:** decisions_shadowed=1, task_selection AGREE,
risk DISAGREE, routing DISAGREE, unsafe_underclassifications=1, **c1_readiness =
NOT_READY**. Negative evidence is displayed, not hidden — it is valuable.

### AttentionItem (human-relevant only)
Emitted only for: `E4`, `CAPITAL_POLICY`, `ARCHITECTURE_FORK`, `SECURITY`,
`COMPLIANCE`, `SPENDING`, `UNRESOLVED_ESCALATION`, `CERTIFICATION_APPROVAL`. **Never**
for ordinary Engineer REPAIR, a normal test failure, normal Claude E3 work, or a C0.5
disagreement by itself. Currently: none outstanding.

### SystemHealthSummary
`controller`/`authority`/`control_loop` = derived; `gpt_supervisor`/`engineer_runtime`/
`sandbox`/`evidence_bridge` = `PENDING_BACKEND` (no authoritative health record —
**do NOT infer "healthy" from process existence**).

## Status/enum semantics
- Task/verification: `VERIFIED` (terminal success), `REPAIR_REQUIRED`,
  `ESCALATION_REQUIRED`, `ABSTAINED`, `FAILED_VALIDATION`, `INTERRUPTED`, `VERIFYING`
  (unverified, incl. supervisor outage). GPT verdicts: `PASS|REPAIR|ESCALATE|ABSTAIN|
  SUPERVISOR_UNAVAILABLE`, plus projection-only `NOT_CONSULTED`.
- Authority: `A0_DIAGNOSTIC` | `A1_ASSISTED_ENGINEERING`. Controller ladder:
  `C0` (executor) · `C0.5` (shadow — propose only) · `C1/C2/C3` (future, certification-
  gated; **C1 disabled**).

## Fields LIVE vs PENDING_BACKEND
- **LIVE:** authority level + grants + forbidden ops; runtime policy + mission +
  AUTO_* flags; mission deliverable VERIFIED/NOT_STARTED; supervisor verdict counts +
  last-pass; apprenticeship comparison metrics; verification ladder projection.
- **PENDING_BACKEND (no backend yet):** worker heartbeat/online/current-task/queue;
  supervisor availability/latency/queue/outage; component health
  (gpt/engineer/sandbox/bridge); `controller_since`.

## Backend truth states (`control_center_truth.py`)
`PENDING_BACKEND` alone was carrying at least three meanings — nobody built the
producer, the producer cannot answer right now, and we hold a value but cannot tell
whether it is still true. Those lead an operator to different actions, so they are now
distinct. Emitted at `dashboard["backend_truth"]`.

| State | Meaning | Produced when |
|---|---|---|
| `LIVE` | authoritative value within its freshness threshold | producer exists, value present, age ≤ threshold (or the value does not decay) |
| `STALE` | authoritative value **measured** as too old | producer exists, value present, **valid** timestamp, age > threshold |
| `PENDING_BACKEND` | engineering incompleteness | no producer has been built |
| `UNAVAILABLE` | operational condition | producer exists but returned no usable value |
| `UNKNOWN` | undecidable from evidence | timestamp missing/unparseable/naive/future, or no reference time |

**Missing timestamp is `UNKNOWN`, never `STALE`.** Calling an untimestamped value stale
asserts an age nobody measured. It looks conservative, which is why it is the tempting
mistake.

### Freshness
Thresholds are named in `FRESHNESS_SECONDS` (heartbeat 300s · supervisor 900s ·
verification 86400s · default 3600s), never buried literals. Age is measured against the
**injected** `now` passed to `build_dashboard`, never the wall clock — an AST test
asserts the module calls no `now()`/`utcnow()`/`today()`. Identical evidence at an
identical reference time yields an identical classification.

## Readiness (capability-based, not a percentage)
A LIVE percentage is the wrong summary: dozens of live cosmetic fields can coexist with
an operator who cannot see what the worker is doing. Readiness is decided by whole
capability groups; `state_counts` is emitted as **diagnostics only**.

| Readiness | Meaning |
|---|---|
| `READY` | every required capability live |
| `MOSTLY_LIVE` | required capabilities live; only secondary gaps |
| `PARTIAL` | one or more **required** capabilities not live |
| `UNAVAILABLE` | the oversight floor (`controller_state`, `worker_authority`) is not established |

Required: `controller_state`, `worker_authority`, `mission_state`, `supervisor_state`,
`worker_activity`. Secondary: `queue_state`, `component_health`, `controller_since`.

### Current classification — `PARTIAL`
Derived, not asserted (`tests/test_control_center_truth.py`). `controller_state`,
`worker_authority` and `mission_state` are LIVE from protected config;
`supervisor_state` ages against recorded verdicts and can legitimately go STALE.

**Remaining `PENDING_BACKEND` capabilities** — no producer exists for any of these, and
building them was explicitly out of scope for this mission:
- `worker_activity` (no `WorkerHeartbeatV0` producer) — **required**, so it alone
  prevents `READY`
- `queue_state` (no dispatch-queue producer)
- `component_health` (no health-probe producer)
- `controller_since` (no controller-session record)

`PARTIAL` is the honest answer while a required capability has no producer. A fabricated
`LIVE` would be worse than a truthful `PENDING_BACKEND`.

## Things the GUI must NEVER do
Mutate EW authority · mutate mission/task state · change risk · certify tasks · bypass
GPT · merge · deploy · write production/`/opt/stockbot` · issue capital/broker actions ·
access credentials · execute shell through GUI input. Projections are structurally
separate from any controller mutation path; there is **no** action endpoint here.

## Handoff
- Controller branch/SHA: `feature/ew-0a-safe-operations` (see the accompanying
  GUI-handoff report for the exact SHA).
- Interface doc: this file. Projection module: `ew0a_readmodels.py` (protected).
  Schemas/enums: `ew0a` / `ew0a_authority` / `ew0a_loop` / `gpt_supervisor`.
- The GUI session (`feature/worker-control-center-gui`) performs the reconciliation
  and reruns `tests/test_ew0a_readmodels.py`. It must independently integrate + validate
  before claiming `WORKER_CONTROL_CENTER_GUI_0A_READY`. This interface being ready is
  only `WORKER_CONTROL_CENTER_INTERFACE_READY`.
- If this interface must change, the change is documented here first; do not break it
  casually.
