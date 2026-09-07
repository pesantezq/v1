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
`current_mission`, `operational_state`, `controller_since`, `escalation_role`.
Security: operational. Current-state. The GUI may **never** request an action
against it.

**`operational_state` is `PENDING_BACKEND`.** It previously read `ACTIVE`, which was
an assertion: the only evidence behind it was that the projection code was running,
and the interface is explicit that liveness must not be inferred from process
existence.

**`controller_since` is `PENDING_BACKEND`** — no `ControllerStateV0` producer exists.

**`identity_basis` = `ASSUMED_NOT_OBSERVED`.** `controller_identity` is a literal in
the builder, not a read. It is true today and there is no producer that could confirm
it, so the projection now says which of those two it is. A consumer must not render
"the controller is X" as an observation while this field says otherwise. The matching
capability `controller_identity` is `PENDING_BACKEND` in `backend_truth`.

**`contract_constants` = `("controller_role", "controller_level", "escalation_role")`.**
These are constant because THIS interface defines them, not because a derivation is
missing. The field exists so the distinction between "contract constant" and "unbuilt
lookup" is machine-readable rather than a comment.

### SupervisorSummary (GPT independent verifier)
`availability` / `current_state` / `outage_state` (`PENDING_BACKEND` — no live health
probe), `recent_pass|repair|escalate|abstain|unavailable` (derived counts from
records), `last_successful_verification`, `measured_latency_ms` (`PENDING_BACKEND`),
`verification_queue` (`PENDING_BACKEND`). **NEVER exposes** the API key, auth headers,
request bodies, or hidden reasoning (asserted by test). Security: operational.

**Source is now explicit**: `source` = `docs/EW0A_0B3_RECORDS.jsonl`, `source_kind` =
`controller_records_ledger`, `verdict_field` = `gpt_verdict`, `evidence_domain` =
`controller_apprenticeship_and_certification`.

The repository holds **two** legitimate ledgers that record supervisor verdicts, for
different purposes and under different field names. This summary counts `gpt_verdict`
in the records ledger; `RunHistorySummary` counts `supervisor_verdict` in the outcome
ledger. They are different numbers about different evidence, and a consumer that
cannot tell which one it is showing will eventually present one as the other.

### WorkerSummary (Engineer)
Persistent identity `engineer.local_qwen2_5_7b`, `role`, `ew_authority` (A1),
`controller_level` (`C0.5_SHADOW`), `current_mission`, `recent_verification_outcomes`,
`escalation_state`. `operational_state` / `current_task` / `queue_size` /
`activity_summary` / `next_action` = `PENDING_BACKEND` — **no `WorkerHeartbeatV0`
exists; do NOT fabricate online/heartbeat state.**

**`identity_basis` = `CONTRACT_CONSTANT`**, with `contract_constants` =
`("worker_identity", "role", "controller_level")`. EW-0A defines exactly one Engineer
Worker with a persistent identity, so these are contract constants and not unbuilt
lookups. They become a derivation the moment a second worker exists — which is a
later bounded mission, and no worker registry is created here.

### WorkerAuthoritySummary
`level`, `grants`, `forbidden_ops`, and the explicit booleans
`can_mutate_main|can_merge|can_deploy|can_write_production|can_self_promote`.
Authoritative (from `config/ew0a_authority.json`). All five are **false** today.

**The booleans are now DERIVED, not dataclass defaults.** They previously carried
`= False` defaults that the builder never overrode. The values were right, which is
what made them dangerous: a default is indistinguishable from a derivation that
returned the same answer, so a future authority change would have left five stale
`false` values looking like current truth.

Each boolean maps to one forbidden operation
(`can_merge` → `MERGE`, `can_deploy` → `DEPLOY`, `can_mutate_main` → `MAIN_WRITE`,
`can_write_production` → `PRODUCTION_WRITE`, `can_self_promote` → `SELF_PROMOTION`)
and is computed by `derive_authority_capabilities(denied_ops)` over the effective
denial set.

`effective_denied_ops(record)` is the **union** of the module's permanent
`FORBIDDEN_OPS` boundary and whatever the authority record additionally forbids — a
union, never a substitution. A record that omits an operation therefore cannot grant
it; a record may only ever be stricter. `capabilities_derived_from` records this on
the projection itself.

#### The record's own list fields are validated

`grants` and `forbidden_ops` are both `list[str]`, and **both** are validated — fixing
only `forbidden_ops` would have left the sibling carrier open to the identical leak.
Neither is ever stringified: `{str(op) for op in …}` rendered a record element
`{"api_key": "sk-…"}` straight into `worker_authority.forbidden_ops`. The container is
checked before any iteration, because `list("MERGE")` would otherwise validate a bare
string as five one-character operation names.

A malformed record produces two new fields — `record_evidence` (`LIVE` | `UNAVAILABLE`)
and `record_detail` — and behaves as follows:

- the record's contents are **not** rendered;
- the record's contents are **not** silently filtered either. Dropping the malformed
  entries and still reporting `LIVE` would discard restrictions the record meant to
  impose while claiming the authority evidence is good — dishonest in the dangerous
  direction;
- the permanent `FORBIDDEN_OPS` boundary is projected on its own, so **every forbidden
  operation stays forbidden and all five `can_*` booleans stay false**. Capability safety
  never depends on the record being well-formed;
- the `worker_authority` capability becomes `UNAVAILABLE`. Because that capability is
  part of the oversight floor, **readiness drops to `UNAVAILABLE`** — the honest answer
  when an operator cannot see what the worker is permitted to do.

The authority *level* is unaffected: it comes from a separate reader that fails closed to
`A0_DIAGNOSTIC`, so authority can remain enforceable while the record's lists are
unreadable. No enforcement path, `FORBIDDEN_OPS` entry, or A0/A1 semantic is changed by
any of this.

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
disagreement by itself.

**`attention_items` is `[]`, and that `[]` is NOT an answer.** It was a literal in the
builder — no derivation has ever run. An operator reading an empty list cannot tell
"a derivation ran and found nothing outstanding" from "nothing has ever derived
this", and those two license opposite behaviour.

`dashboard["attention"]` (`AttentionCoverage`) now states which one it is:
`items`, `item_count`, `derivation_state` (`PENDING_BACKEND` today),
`zero_items_is_authoritative` (**`false`** today), and `detail`.

> A GUI may present "nothing requires you" **only** when
> `attention.zero_items_is_authoritative` is `true`.

The list itself keeps its shape and location, so existing consumers are unaffected.

**Deliberately NOT derived from the outcome ledger.** That ledger's only
`policy_violation` is certification mission M5 (`tools/ew0a_certify.py`), a
protected-op attack whose **gate was that it be denied** — a passed security control.
Deriving attention from failure-shaped statuses would promote it into an unresolved
human incident, which is the bug the GUI has today and is not improved by moving it
upstream. Building a real attention producer is a later mission.

### SystemHealthSummary
`controller` / `gpt_supervisor` / `engineer_runtime` / `sandbox` / `evidence_bridge` /
`control_loop` = **all `PENDING_BACKEND`**. No health-probe producer exists for any of
them.

`controller` and `control_loop` previously read `ACTIVE` and `READY`. Nothing measured
that; the evidence was that this code was executing. That is exactly the inference
this document forbids — **do NOT infer "healthy" from process existence** — and the
projection was making it.

`authority` still carries the authority level. That is **configuration**, not health,
and `health_note` says so on the projection.

**`config_readability`** is new and is separate on purpose: `authority_record`,
`runtime_policy`, `outcome_ledger`, `records_ledger`, each `READABLE` / `UNREADABLE` /
`ABSENT`. A readable protected config proves what the system is *allowed* to do and
proves nothing whatever about whether anything is running. It is named for what it
measures so it cannot be re-labelled as component health later.

A file that cannot be decoded as UTF-8 is `UNREADABLE`. `UnicodeDecodeError` is a
`ValueError`, **not** an `OSError`, so it previously escaped this check and took the
whole dashboard down. The catch is deliberately narrow (`OSError`, `UnicodeError`):
catching `Exception` here would disguise a programming defect in this module as file
unreadability. The authoritative config readers remain free to fail closed on their own
terms — this is the observability boundary, and its job is to report, not to crash.

### RunHistorySummary (engineering outcome/run history)
`dashboard["run_history"]`. New in GUI-R. The GUI previously read
`docs/EW0A_CERTIFICATION_OUTCOMES.jsonl` itself, which is how a second, independent
interpretation of controller evidence came to exist. The controller owns it now.

Built on the **canonical domain reader** `ew0a.read_outcomes`, not a fresh hand-written
JSONL parse. Fields: `source`, `source_kind` (`engineering_outcome_ledger`),
`availability`, `record_count`, `runs[]`, `verdict_counts`, `verdict_field`
(`supervisor_verdict`), `evidence_domain` (`engineering_outcome_runs`), `ordering`.

**Ordering** is `ledger_append_order` — the append-only order of the ledger. Records
are not re-sorted by timestamp, because a record with no usable timestamp would then
have to be placed somewhere, and any placement would be an invention.

**Identifiers are preserved, never synthesized.** Each run carries its own `task_id`
plus a `ledger_index` for disambiguation. The projection does not mint a composite id
and present it as one the control plane issued.

**Provenance rule — `mission_id` is projected exactly as recorded, and stays `None`
when absent.** `OutcomeRecord.mission_id` defaults to `None` and the historical records
predate the field, so `None` is the true answer. The runtime mission is **never**
stamped onto a historical run: doing so attributes month-old records to whatever
mission happens to be current, which is fabricated provenance.

**Availability**, not pending: the producer (the certification runner plus
`ew0a.append_outcome`) exists in this repository. An absent or unreadable ledger is
therefore `UNAVAILABLE` — an operational condition — never `PENDING_BACKEND`, which
would claim nobody had built it. A malformed line makes the canonical reader raise;
this projection does not soften that into a partial list, it reports the ledger
unusable and says why.

**Schema-invalid rows are unusable too, not only unparseable ones.** A row can be valid
JSON and still violate `OutcomeRecord`. Two independent checks apply, and both make the
whole projection `UNAVAILABLE`:

1. **Every row must be a JSON object.** A scalar or array row is not filtered out. It
   used to be, and the remainder was reported `LIVE` — so a three-row ledger with one
   corrupt row projected two records as a complete history, and a ledger of nothing but
   corrupt rows projected an *empty* history as complete. Silent evidence loss is worse
   than a crash, because a crash announces itself. A genuinely empty ledger remains
   `LIVE` with zero records; those two answers must never collapse into one.
2. **`failure_classes` is `list[str]` — container *and* elements.** Absent and `null`
   remain `[]` (records predating the field are legitimately shaped that way). A non-list
   container is rejected, and so is any non-string element. Validating only the container
   left `str()` coercing the elements, and `str()` on a dict renders the dict: a row
   carrying `[{"api_key": "sk-…"}]` was projected `LIVE` with the secret inside it. That
   defeated the no-secrets guarantee through a field nobody thinks of as a secret
   carrier, which is why it survived a review that was looking at the container.

Nothing on this path calls `str()`/`repr()`/`format` on ledger evidence — an AST test
enforces it, because the leak existed precisely because one such call sat there. Invalid
values are **not** coerced to `[]` or sanitised and served, which would manufacture clean
evidence out of corrupt evidence, and the offending payload is never echoed into
`detail`; only its type and the row index are. One bad row invalidates the whole history:
no authoritative contract establishes partial-ledger semantics, and a consumer cannot
tell a complete history from a silently shortened one.

The invariant behind both rules: **no corrupt record may escape through
`build_dashboard()` as an uncaught projection exception.** A Mission Control page
rendering from this projection must fail closed to an honest `UNAVAILABLE`, never to a
stack trace.

#### The certified field contract

`validate_outcome_record()` checks **every** `OutcomeRecord` field this projection reads,
against an enumerable table (`_OUTCOME_FIELDS`) rather than field by field as defects are
reported. Only fields the projection actually consumes are listed — certifying the
boundary is not a licence to start emitting more.

| Field | Declared | Required |
|---|---|---|
| `task_id` `title` `risk_class` `executor` `final_status` `recorded_at` `disposition` | `str` | present and non-null |
| `attempt_count` | `int` | present and non-null |
| `escalated` `policy_violation` `human_intervention` | `bool` | present and non-null |
| `failure_classes` | `list[str]` | optional — absent or `null` → `[]` |
| `supervisor_verdict` `mission_id` `candidate_sha` | `str` | optional — absent or `null` → `None` |

The optional set is not a convenience: every record in the real ledger **omits**
`mission_id` and `candidate_sha` and carries `supervisor_verdict: null`, so treating
those as invalid would be a certification that fails on the truth. All seven real records
validate, and a test asserts it.

Three edges are load-bearing:

- **`bool` is a subclass of `int` in Python**, so `attempt_count` explicitly rejects
  `true`. A flag is not a count. Negative counts are rejected too (`>= 0`); every real
  value is 1, 2 or 4.
- **Booleans are type-checked, not truth-tested.** The previous `rec.get(x) is True`
  turned `"true"`, `1` and `{}` into a clean `False` — schema-invalid evidence becoming
  clean **negative** evidence, which for a field named `policy_violation` is the most
  dangerous possible direction.
- **A malformed required identifier does not disappear.** The previous `_s()` returned
  `None` for a non-string, so a corrupt `task_id` vanished while the ledger still
  reported `LIVE` — a run with no identity, presented as trustworthy history.

### Active-session truth and mission consistency
`dashboard["active_session"]`. Episode discovery belongs to the producer
(`tools/ns0c_session.session_projection`); the read model asks it and classifies the
answer. It is built **before** the truth assessment so it is classified with everything
else — it used to be appended afterwards, which is how the one projection that answers
"what is happening right now" ended up as the only one carrying no truth state at all.

Added keys: `session_present`, `truth_state`, `runtime_mission_id`,
`mission_consistency`, `consistency_detail`, `safe_to_present_as_current_work`,
`freshness_evidence`.

#### The four producer outcomes

| Situation | `session_present` | Truth state | Why |
|---|---|---|---|
| `tools.ns0c_session` genuinely absent | — | `PENDING_BACKEND` | nobody built the producer |
| producer exists, import/entry-point/call fails | `false` | `UNAVAILABLE` | it exists and could not answer |
| producer answers `NO_SUCH_SESSION` | `false` | `LIVE` | it answered the question — with "no" |
| producer returns a session with no usable `session_id` | `false` | `UNAVAILABLE` | see identity below |
| producer returns a session | `true` | `UNKNOWN` | see freshness below |

**Presence requires a usable identity.** `session_present` is `true` only when
`session_id` is a non-empty string that is not one of the producer's sentinels. Without
that check a corrupt `SessionStarted` record missing its `session_id` produced
`truth_state = UNAVAILABLE` alongside `session_present = true` — a contradictory
half-session still carrying current-work fields such as `current_task_id`, which a GUI
could render as a phantom active session. A malformed session now returns the same
producer-failure envelope as any other unusable answer, so there are no current-work
fields for a template to read out of it.

**The no-session answer is recognised STRUCTURALLY, not by one field.** Matching only
`session_state == NO_SUCH_SESSION` was not enough: a corrupted ledger whose last
`SessionState` happens to read that value yields a fully **populated** projection, which
was then accepted as the legitimate empty answer and copied wholesale — so a consumer
received `session_present: false` and `truth_state: LIVE` from a dict still carrying
`session_id`, `current_task_id` and `current_stage`. The whole envelope is now required:
the five sentinel fields, the three null current-task fields, six zero counters (with an
explicit `bool` rejection, since `False == 0`), empty `blockers`, and `known_sessions` as
a list of strings. `session_id` may be `None` **or** a string — the producer legitimately
echoes back a requested-but-unknown id — but not an arbitrary value. Any contradiction
yields `UNAVAILABLE` with no current-work fields for a template to read.

**`PENDING_BACKEND` is reachable only through producer ABSENCE.** An earlier version
returned it when one concrete ledger filename was missing and when the producer raised.
Both told an operator to go build a backend that was already in the tree. "There is no
session right now" is an *answer*, not an engineering gap, and it does not decay — there
is no recorded value whose age would have to be inferred, so `LIVE` is correct and
`STALE`/`UNKNOWN` would both be inventions.

**The read model no longer decides whether a session exists.** It previously gated on
`ledger_path(repo_root).exists()`, which tests ONE ledger filename while the producer
supports multiple ledgers, episode discovery, corrected session identities and
latest-episode selection. A perfectly discoverable session under any other ledger name
was reported as `PENDING_BACKEND`. Discovery is delegated, never reimplemented here —
an AST test asserts this module calls no `ledger_path`/`ledger_paths`/`load_episodes`/
`read_events`/`split_episodes`.

Failure detail carries only the exception **type** and the missing module **name** —
never an exception payload, which can carry paths or values a projection must not
render.

#### Freshness and consistency are two independent questions, never merged

*Freshness* — the session contract publishes `session_started_at` and **no
last-activity timestamp**. A start time is not a liveness signal, and no named session
freshness threshold exists in `FRESHNESS_SECONDS`. Age is therefore unmeasurable, and
the lattice already has the answer: **`UNKNOWN`**. It is **not `STALE`** — `STALE`
requires a valid recorded timestamp, an injected `now`, a named threshold and a
measured age beyond it. No arbitrary session-age threshold was invented to manufacture
one.

*Consistency* — `AGREES` / `MISMATCH` / `UNDETERMINED`, comparing the session's own
recorded `mission_id` against the runtime policy's. A mismatch is a fact about
identity, not about age, and is **never** reported by downgrading freshness.

`safe_to_present_as_current_work` is `true` only when `truth_state` is `LIVE` **and**
`mission_consistency` is `AGREES`. It is published as one boolean so the GUI does not
re-derive it — a consumer inventing its own staleness rule is the frontend bypass this
architecture exists to prevent.

### Learning projection truth
`dashboard["learning"]`. A learning producer **exists** (`learning/readmodels.py`, with
lessons in the store). An earlier version wrapped the whole call in one `except` and
returned `PENDING_BACKEND` on any failure — telling an operator that nobody had built
learning while the package sat in the tree.

| Situation | Truth state |
|---|---|
| `ModuleNotFoundError` naming the producer module (or a parent package) | `PENDING_BACKEND` |
| `ModuleNotFoundError` naming one of the producer's **dependencies** | `UNAVAILABLE` |
| any other `ImportError`, or the module raising on import | `UNAVAILABLE` |
| module present but exposes no `build_learning_dashboard` | `UNAVAILABLE` |
| builder raises | `UNAVAILABLE` |
| builder returns a shape that is not the published projection | `UNAVAILABLE` |
| valid projection | `LIVE` |

**A generic `ImportError` is not evidence that nobody built the producer.** It is
raised just as readily when the module exists and one of *its* imports fails, or when
its API has changed incompatibly. Only a `ModuleNotFoundError` naming the producer
itself — or a parent package, without which it cannot exist — proves absence.

**The response shape is validated.** `LIVE` must be a statement about the evidence, not
about the call returning without raising, so a response that is not a dict carrying
`recent_lessons` is `UNAVAILABLE`.

`truth_state` is set on the projection and `freshness` is
`NOT_APPLICABLE_HISTORICAL_EVIDENCE`. **No freshness threshold is imposed on lesson
records** — they are historical evidence, and inventing an age limit would manufacture
`STALE` out of nothing. The `learning` capability is **secondary**: the interface does
not make learning an oversight requirement.

## Status/enum semantics
- Task/verification: `VERIFIED` (terminal success), `REPAIR_REQUIRED`,
  `ESCALATION_REQUIRED`, `ABSTAINED`, `FAILED_VALIDATION`, `INTERRUPTED`, `VERIFYING`
  (unverified, incl. supervisor outage). GPT verdicts: `PASS|REPAIR|ESCALATE|ABSTAIN|
  SUPERVISOR_UNAVAILABLE`, plus projection-only `NOT_CONSULTED`.
- Authority: `A0_DIAGNOSTIC` | `A1_ASSISTED_ENGINEERING`. Controller ladder:
  `C0` (executor) · `C0.5` (shadow — propose only) · `C1/C2/C3` (future, certification-
  gated; **C1 disabled**).

## Fields LIVE vs PENDING_BACKEND
- **LIVE:** authority level + grants + forbidden ops + the derived `can_*` capability
  booleans; runtime policy + mission + AUTO_* flags; mission deliverable
  VERIFIED/NOT_STARTED; supervisor verdict counts + last-pass (records ledger);
  apprenticeship comparison metrics; verification ladder projection; learning
  projection; run/outcome history (outcome ledger, via `ew0a.read_outcomes`).
- **PENDING_BACKEND (no backend yet):** worker heartbeat/online/current-task/queue;
  supervisor availability/latency/queue/outage; component health
  (controller/gpt/engineer/sandbox/bridge/control-loop); `controller_since`;
  attention derivation; controller identity.
- **UNKNOWN:** active-session freshness when a session IS present — a value is held,
  and its age cannot be measured from the evidence the session contract publishes.
- **LIVE (no session):** when the session producer answers `NO_SUCH_SESSION`. That is
  an answer, not a gap, and it has no age to measure.
- **UNAVAILABLE:** an existing producer that cannot answer — an unreadable outcome
  ledger, a session producer that raises, a learning producer whose own imports fail.

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

**THE PROJECTION-BOUNDARY RULE.** Validate first; copy only validated values; never
stringify arbitrary evidence. Converting a `Path` to `str` or an `Enum` to `.value` is
conversion of something this module owns — `str(record_field)` is not validation and is
never a substitute for it. Three consecutive review rounds found the same class rather
than three unrelated bugs: schema-invalid authoritative evidence crossing this boundary
unvalidated, where `str()` on a dict renders the dict (leaking payload), `x is True`
rewrites a corrupt value as a clean `False`, and a one-field sentinel check admits
contradictory state. Fixing the reported field each round could not converge, because
the hole was the boundary. Invalid evidence is therefore **not** sanitised into
valid-looking evidence — it makes the projection `UNAVAILABLE`.

**`PENDING_BACKEND` means the producer has not been implemented — and nothing else.**
Not: no records · an empty dataset · no currently active session · a malformed response
· an operational failure · an internal import failure · stale evidence · unknown
freshness. Each of those has its own state above, and each sends an operator somewhere
different. `tests/test_ew0a_readmodels.py::test_pending_backend_means_unimplemented_and_nothing_else`
builds the whole dashboard against a repo root with no data at all and asserts that
emptiness manufactures not one extra `PENDING_BACKEND`.

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
`learning` and `run_history` are LIVE from producers that exist. `active_session` is
`UNKNOWN` — see the active-session section: its age is unmeasurable, which is not the
same as old.

**Remaining `PENDING_BACKEND` capabilities** — no producer exists for any of these, and
building them was explicitly out of scope for this mission and for the GUI-R repair:
- `worker_activity` (no `WorkerHeartbeatV0` producer) — **required**, so it alone
  prevents `READY`
- `queue_state` (no dispatch-queue producer)
- `component_health` (no health-probe producer)
- `controller_since` (no controller-session record)
- `attention_derivation` (no attention producer; an empty item list is therefore not
  an authoritative "nothing needs you")
- `controller_identity` (no `ControllerStateV0`; the projected identity is an
  assumption, see `controller.identity_basis`)

`PARTIAL` is the honest answer while a required capability has no producer. A fabricated
`LIVE` would be worse than a truthful `PENDING_BACKEND`.

GUI-R added capabilities and removed assertions; it deliberately did **not** move
readiness. `PARTIAL` before, `PARTIAL` after. The objective was more truthful
visibility, not a better-looking status.

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
