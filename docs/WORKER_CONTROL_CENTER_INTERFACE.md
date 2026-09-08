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
All live in `ew0a_readmodels.py`; `build_dashboard(repo_root)` assembles them. Fields
with no authoritative backend are the string `PENDING_BACKEND` — **never fabricated**.

**Current read-model schema: `engineering.readmodel.v1`.** Every projection carries
`schema_version="engineering.readmodel.v1"`.

### The v0 → v1 contract delta

v1 was required because the contract changed **incompatibly** for any consumer that
assumed the supervisor and apprenticeship count fields were always integers.

| Change | v0 | v1 |
|---|---|---|
| `SupervisorSummary.recent_pass\|repair\|escalate\|abstain\|unavailable` | `int` | **`int \| None`** |
| `ApprenticeshipSummary` count fields | `int` | **`int \| None`** |
| `SupervisorSummary.records_evidence` | — | **new** |
| `WorkerSummary.records_evidence` | — | **new** |
| `ApprenticeshipSummary.records_evidence` | — | **new** |
| `dashboard["controller_records"]` | — | **new top-level surface** |

A schema version exists precisely to let a consumer tell those two contracts apart.
Leaving it at v0 because this read model is `experimental_noncanonical` would have
defeated the one mechanism that communicates the difference.

**v1 rather than v2:** authoritative `main` still publishes v0, and the GUI-R branch —
which also changed this shape — is frozen and unmerged. This is therefore the first
candidate to establish the next *published* contract. When GUI-R is integrated onto
hardened main it should reconcile to this same v1 rather than inventing v2 because an
unmerged branch also moved. The separate learning contract
(`engineering.learning_readmodel.v0`) is **unchanged** — it is a different contract and
was not touched.

### `dashboard["controller_records"]`

The controller-record ledger's own read outcome, stated once and authoritatively so a
consumer does not have to infer it from three separate summaries.

| Field | Meaning |
|---|---|
| `availability` | `LIVE` \| `UNAVAILABLE` |
| `record_count` | integer — rows actually projected |
| `source` | the ledger identifier **this read used**, carried unchanged; not the module default |
| `detail` | structural, module-owned diagnostic (line index and type only — never payload) |

**The two zero-count cases are not the same answer:**

```
LIVE        + record_count=0   ->  authoritative empty history; nothing has been recorded
UNAVAILABLE + record_count=0   ->  the ledger could not be read; this does NOT mean
                                   zero events occurred
```

#### What `LIVE` claims

```
controller_records.availability = LIVE
```

means **both**:

1. the selected ledger was fully readable as JSON objects; **and**
2. every field consumed by the WCC read model satisfied the WCC consumption contract.

It does **not** mean every field of every historical controller-record kind has been
canonically certified. Fields the WCC does not read stay opaque and unprojected, and a
record is never rejected merely for carrying them — if another subsystem consumes such a
field later, its own contract must add it explicitly.

The **WCC consumption contract** (`WCC_CONSUMED_RECORD_FIELDS`) covers exactly the
fields these projections read, with presence semantics derived from the tracked ledger
rather than assumed:

| Field | Type | Presence |
|---|---|---|
| `kind` | non-empty `str` | **required** — on all 24 tracked records |
| `gpt_verdict` | non-empty `str` | optional — on 10 of 24 |
| `recorded_at` | non-empty `str` | optional — on 22 of 24; two legitimately omit it |
| `engineer_proposed_task_relates_to_experimentspec` | `bool` | optional — `ApprenticeshipComparison` only |
| `risk_agreement` `routing_agreement` `danger_underclassified_architecture_as_engineer` | `bool` | optional — `ApprenticeshipComparison` only |

Booleans must be **real booleans**: `0`, `1`, `"true"`, `"false"`, `[]` and `{}` are
rejected rather than read through truthiness. A wrong type is unavailable evidence, not
`False` — conflating "not applicable to this record kind" with "malformed value that
happens to be falsey" is how a corrupt boolean becomes clean negative evidence on a field
named `unsafe_underclassifications`. An explicit JSON `null` on an *optional* field is
treated as absence.

Validation happens in the shared admission path before a row is accepted, so the three
consumers cannot receive evidence this read result has already called usable. They
consume typed values directly — no `str()`, `repr()` or `format()` on record evidence,
enforced by a scoped test.

A ledger is usable in full or not at all. Any invalid JSON line, any non-object row, any
WCC-consumed-field violation, a decode failure or an I/O failure makes the whole read
`UNAVAILABLE` with **no** rows exposed — dropping bad rows is precisely how partial evidence comes to masquerade as
complete evidence. An **absent** ledger is `UNAVAILABLE`, not `PENDING_BACKEND`: the
controller writes these records, so absence is operational and not evidence that nobody
built the producer.

### `records_evidence`

Carried by `supervisor`, `worker` and `apprenticeship`, with values from the existing
truth lattice (`LIVE` / `UNAVAILABLE` in practice today).

> `records_evidence` describes whether the controller-record ledger could answer. It is
> **independent** from `SupervisorSummary.availability`, which is the supervisor's own
> operational availability and remains `PENDING_BACKEND` because no health probe exists.

Each summary carries its own copy deliberately: a consumer reading only `worker` must be
able to tell that its `recent_verification_outcomes` came from an unusable ledger,
without having to cross-reference another surface.

### Nullable measured counts

Under v1, these may be `integer | null`:

```
SupervisorSummary   recent_pass · recent_repair · recent_escalate ·
                    recent_abstain · recent_unavailable
Apprenticeship      decisions_shadowed · task_selection_agreements · risk_agreements ·
                    routing_agreements · missed_escalations ·
                    unsafe_underclassifications · authority_expansion_proposals
```

```
0     = a measured zero from a USABLE ledger
null  = the source evidence was unavailable; the question could not be answered
```

**This distinction is load-bearing, and `null` is not replaced with `0` to avoid a
schema change.** A measured zero and an unanswerable question look identical as `0`, and
for these fields that difference is the entire point: `0 REPAIR` read from an unreadable
ledger reads as nothing having gone wrong, and `0 unsafe_underclassifications` is the
single most flattering number the apprenticeship projection can emit.

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
probe), `recent_pass|repair|escalate|abstain|unavailable` (**`int | None`** — derived
counts from records when the ledger is usable, `None` when it is not),
`last_successful_verification`, `measured_latency_ms` (`PENDING_BACKEND`),
`verification_queue` (`PENDING_BACKEND`), `records_evidence` (whether the ledger could
answer — independent of `availability` above). **NEVER exposes** the API key, auth
headers, request bodies, or hidden reasoning (asserted by test). Security: operational.

When `records_evidence` is `UNAVAILABLE` every count is `None` and
`last_successful_verification` is `None`. They are **not** zeroed.

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

**Both list fields are REQUIRED evidence.** `set_authority_level` always writes `grants`
and `forbidden_ops`, so a record lacking either is not a complete authority record.
`record.get("grants", [])` erased the difference between three states that mean three
different things:

| Record | Meaning | Result |
|---|---|---|
| field absent | incomplete record | `record_evidence: UNAVAILABLE` |
| field `null` | incomplete record | `record_evidence: UNAVAILABLE` |
| field `[]` | list-shaped evidence; A0 legitimately grants nothing | `record_evidence: LIVE` |

**`level` is projection-relevant evidence as well**, and validating only the two lists
was a defect: `read_authority_level` fails closed to `A0_DIAGNOSTIC` on a corrupt
record, and certifying the lists while ignoring `level` let a record with an absent or
unrecognised level report `record_evidence: LIVE` through a **required** capability. The
record's `level` must be present, a string, and an exact `EngineerAuthorityLevel`
member, and it must agree with the canonical reader — two disagreeing reads of the same
protected record are not resolved by picking one and reporting `LIVE`.

**Effective level and record evidence are different questions**, and reporting both is
not a contradiction:

```
worker_authority.level            A0_DIAGNOSTIC     <- the safe enforcement fallback
worker_authority.record_evidence  UNAVAILABLE       <- the stored record is untrustworthy
```

A0 is what enforcement does; `UNAVAILABLE` is what the evidence is worth. Only the
fields this projection consumes are certified — `actor`, `updated_at` and `schema_*` are
not consumed and are deliberately not validated here.

The permanent `FORBIDDEN_OPS` boundary is unioned in regardless of which of the three
applies, so an empty `forbidden_ops` never loosens anything.

A malformed or incomplete record produces two fields — `record_evidence`
(`LIVE` | `UNAVAILABLE`) and `record_detail` — and behaves as follows:

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
`unsafe_underclassifications`, `authority_expansion_proposals`, `c1_readiness`, and
`records_evidence`. Every count is **`int | None`**: `None` when the records ledger could
not answer, never a flattering zero.
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

#### The populated session has an explicit output schema

The projection used to be `dict(session)` — a wholesale copy, which made the published
schema equal to *whatever the producer happens to return today or tomorrow*. That is not
a certified interface, and it is how a `TaskStage.title` of `{"api_key": "sk-…"}` arrived
in the dashboard under `current_task_title`.

Every published field is now validated and copied individually, from `_SESSION_FIELDS`:

| Fields | Declared | Nullable |
|---|---|---|
| `session_id` `mission_id` `session_objective` `session_started_at` `starting_main_sha` `session_state` `authority` `c1_status` `worker_heartbeat` `supervisor_latency_ms` | `str` | no |
| `recorded_session_id` `current_task_id` `current_task_title` `current_stage` | `str` | yes |
| `identity_corrected` `auto_merge` `production_mutation` `capital_action` | `bool` | no |
| `tasks_attempted` `tasks_verified` `tasks_repaired` `tasks_escalated` `tasks_abstained` `tasks_incomplete` | `int` `>= 0`, **bool rejected** | no |
| `blockers` `known_sessions` | `list[str]` | no |

`worker_heartbeat` and `supervisor_latency_ms` legitimately carry the string
`PENDING_BACKEND` in the producer's own contract, so `str` accepts them without this
module inventing a sentinel rule of its own.

**Schema closure.** An uncontracted producer key does **not** appear here. If the
producer later publishes `debug_payload`, `raw_event` or `credential_context`, none of
them is exposed until it is added to the table deliberately — and the projection does not
reject the producer merely for publishing an extra key. `read_model` and `schema_kind`
come from this module's constants rather than the producer's copy: a projection should
not inherit its own identity from evidence.

The closure applies to the no-session branch too, which is assembled from
`_NO_SESSION_PROJECTED_FIELDS` rather than copied.

**Any malformed contracted field** makes the answer unusable: `session_present: false`,
`truth_state: UNAVAILABLE`, `safe_to_present_as_current_work: false`, and **no**
current-work evidence — no `current_task_id`, `current_stage`, counters, `blockers`,
`session_objective` or `starting_main_sha` survives from an answer declared unusable.

`SESSION_PROJECTED_SOURCE_FIELDS` and `SESSION_MODULE_FIELDS` are exported so a test can
assert *emitted keys == validated keys + module keys* mechanically. The previous audit
lived in prose and did not cover everything emitted, which is exactly how this defect
survived it; the relationship is now executable, and adding
`out["new"] = session["new"]` without extending the contract fails a test.

### Learning projection truth
`dashboard["learning"]`. A learning producer **exists** (`learning/readmodels.py`, with
lessons in the store). An earlier version wrapped the whole call in one `except` and
returned `PENDING_BACKEND` on any failure — telling an operator that nobody had built
learning while the package sat in the tree.

**Reachable today**, while the quarantine below is active:

| Situation | Truth state |
|---|---|
| `ModuleNotFoundError` naming the producer module (or a parent package) | `PENDING_BACKEND` |
| `ModuleNotFoundError` naming one of the producer's **dependencies** | `UNAVAILABLE` |
| any other `ImportError`, or the module raising on import | `UNAVAILABLE` |
| module present but exposes no `build_learning_dashboard` | `UNAVAILABLE` |
| module **and** entry point present | `UNAVAILABLE` — payload not invoked, not admitted |

**A generic `ImportError` is not evidence that nobody built the producer.** It is
raised just as readily when the module exists and one of *its* imports fails, or when
its API has changed incompatibly. Only a `ModuleNotFoundError` naming the producer
itself — or a parent package, without which it cannot exist — proves absence.

**NOT reachable through the WCC today**, because the builder is deliberately not
invoked: *builder raises* · *builder returns a malformed shape* · *valid payload →
`LIVE`*. Those become GUI-L concerns when learning is certified; they are not current
runtime branches and must not be read as such.

#### The learning payload is QUARANTINED

The producer exists and exposes `build_learning_dashboard`, so this is **`UNAVAILABLE`,
never `PENDING_BACKEND`** — claiming nobody built learning would send an operator to
write code that is in the tree with lessons in it.

But the payload was admitted on a shape check alone (a dict containing
`recent_lessons`) and then emitted wholesale, so a lesson whose `principle` is
`{"api_key": "sk-…"}` reached the dashboard as trusted GUI evidence. Validating
`principle` would be another instance-level repair, and this interface's history shows
where that leads: the learning dashboard carries four independently shaped projections
(`recent_lessons`, `capability_competence`, `lesson_transfer`, `graduation_readiness`)
which themselves derive from stored lessons, competence, retrieval and evaluation
records. Certifying that is its own bounded mission (**GUI-L**).

Until then: **no value returned by `build_learning_dashboard()` reaches this dashboard.**
The builder is deliberately not invoked — there is no payload to discard, so there is
nothing to leak — and the envelope carries only `read_model`, `schema_kind`,
`schema_version`, `truth_state`, `freshness` and a module-owned `detail`. Absence by
construction, not by sanitisation.

`learning` remains a **secondary** capability, so readiness follows mechanically from the
required gaps; it was not adjusted to preserve the previous display.

#### The top-level projection inventory is executable

`DASHBOARD_PROJECTION_REGISTRY` declares a boundary classification for **every**
top-level key `build_dashboard()` emits, and a test asserts the registry equals the
actual emitted key set — on the real repository and on an empty one. A new surface
cannot be added without declaring who owns its boundary.

| Boundary | Surfaces |
|---|---|
| `GUI_R_VALIDATED` | `run_history` · `worker_authority` · `active_session` · `controller_records` |
| `UNAVAILABLE_PENDING_CERTIFICATION` | `learning` |
| `MODULE_OWNED` | `schema_version` · `schema_kind` · `read_model` · `attention_items` · `attention` |
| `HARDENED_SOURCE_READER` | `controller` · `mission` · `worker` · `supervisor` · `apprenticeship` · `system_health` · `backend_truth` |
| `KNOWN_SOURCE_READER_BLOCKER` | *(nothing today — A/B/C were repaired by GUI-SR)* |
| `DERIVED_FROM_REGISTERED_INPUTS` | *(nothing today — see below)* |

The registry is an **audit artifact, not a second truth engine**: it derives no
authority, readiness, mission state, health or freshness, and it is not emitted in the
dashboard.

### The blocker declarations were reassessed, not deleted (GUI-RI)

GUI-R marked seven surfaces `KNOWN_SOURCE_READER_BLOCKER` because they reached this
module through canonical readers with **known, deliberately unrepaired** failure modes:

- **A** — `ew0a_authority.read_authority_level` raised `TypeError` on a non-object JSON root
- **B** — `ew0a_loop.read_runtime_policy` raised `AttributeError` on the same shape
- **C** — `_read_records` admitted non-dict rows whose `.get()` then raised in
  `build_supervisor_summary`

**GUI-SR repaired all three.** A and B are now total and fail closed; C was replaced by
whole-ledger admission (`read_controller_records`) carrying the WCC consumed-field
contract — **not** by filtering, which would have recreated the silent-evidence-loss
defect this interface fixed for run history.

So the blocker premise is now false, and at integration each declaration was reassessed
against the actual call path rather than dropped. Every one of those seven surfaces
**still depends on a canonical reader** — that dependency is real and worth being able
to see — so they moved to `HARDENED_SOURCE_READER`: *consumes the output of a reader
GUI-SR made total; the surface does not validate that evidence itself, but the reader can
no longer raise or hand it silently-truncated data.*

`KNOWN_SOURCE_READER_BLOCKER` is **retained in the enum and claimed by nothing**. That
distinction is the one this registry exists to make, and a future reader could regress
into it; deleting the value would force a later regression to choose between two wrong
labels.

`RawSourceReader` correspondingly no longer records a **defect** — it records a
**dependency**. The A/B/C letters are kept as historical anchors because the PR trail
and the deferred-debt tables refer to them by letter.

**The hardened label is discharged, not asserted.** `_RI_READER_TOTALITY_PROOF` maps each
reader to the corruption test that establishes its totality, and tests require: every
`RawSourceReader` has a proof that exists; every `HARDENED_SOURCE_READER` surface declares
only proven readers; and each entry's prose names *exactly* the readers it declares.
Adding a fourth reader, or labelling a surface hardened without a proof, fails. A further
test corrupts A, B and C **simultaneously** — the isolated matrices each corrupt one, but
after integration a single `build_dashboard` call consumes all three.

### `backend_truth` is hardened, still not derived-only

It first claimed `DERIVED_FROM_REGISTERED_INPUTS`, which was an optimistic entry in the
very artifact built to prevent optimistic entries. `build_dashboard` passes `level`,
`policy` and `records` straight into `_assess_backend_truth`, which reads them directly
rather than reading already-registered projections. What changed at integration is that
all three are now **total reader output** — a non-object records row can no longer raise
there, and the authority/runtime readers can no longer prevent the surface being assembled
at all.

So its honest label moved from blocked to `HARDENED_SOURCE_READER`, **not** to
`DERIVED_FROM_REGISTERED_INPUTS`. That value may only mean *this surface introduces no
direct dependency on raw authoritative evidence*, and `backend_truth` still has one.
Restructuring `_assess_backend_truth` to consume validated projections remains
deliberately out of scope: it is not needed for safety, only for the label.

### Source acquisition is concentrated behind named gateways (GUI-RI)

WCC source acquisition is concentrated behind named gateways and converted into a frozen
evidence object before projection. Gateway dependencies are explicit architectural
contracts backed by targeted tests and review. **Static and behavioural guards are
defence in depth and are not claimed to constitute whole-program IO or security proofs.**

That last sentence is a correction. Four review rounds established that the stronger
claim this document previously made was unsound:

> ~~"a bounded AST test derives every authoritative access"~~
> ~~"the guard fails closed on every unrecognized path operation"~~
> ~~"every gateway dependency is automatically complete"~~
> ~~"`build_dashboard` is IO-free by construction"~~

Each round replaced one enumeration with a narrower enumeration — a reader enum, then a
method-name list, then two `ast.Call.func` shapes, then a gateway-to-dependency map — and
each time the residue was still an enumeration. Python is too expressive for a small
bounded analyzer to establish absence of arbitrary behaviour. **The repeated defect was
the completeness claim itself, not a missing case.** The analyzers are retired; a test
asserts nothing binds their names.

#### Structure

```text
source acquisition → named gateways → _DashboardEvidence → projection / assembly
```

```python
def build_dashboard(repo_root, now=None):
    evidence = _collect_dashboard_evidence(Path(repo_root), now)
    return _build_dashboard_from_evidence(evidence, now)
```

**What is claimed, and enforced:**

| Claim | How |
|---|---|
| `_build_dashboard_from_evidence` takes no `repo_root`, `Path`, or source filename — it cannot reach the repository *through its parameters* | signature test; a structural property, not a syntax survey |
| `build_dashboard` is collect-then-project, nothing else | AST test on a ≤4-statement body |
| every source-backed input is frozen into `_DashboardEvidence` before projection | frozen dataclass, 14 declared fields |
| gateway dependencies agree with the registry declarations | consistency test between two reviewed artifacts |

**What is NOT claimed:** that the projection layer is pure, sandboxed, or mechanically
incapable of IO; that every possible collector call is discovered; that gateway interiors
are proven to acquire only what they declare. Those are review-and-test properties.

#### Gateway contracts

Explicit, human-reviewable, regression-tested. No claim is made that Python introspection
proves a gateway could never acquire another source.

| Gateway | Source | Returns | On failure |
|---|---|---|---|
| `read_authority_level` | `config/ew0a_authority.json` | effective `EngineerAuthorityLevel` | `A0_DIAGNOSTIC` (fail closed) |
| `read_runtime_policy` | `config/ew0a_runtime.json` | typed runtime policy or `None` | `None`; malformed fields rejected by declared type |
| `read_controller_records` | `docs/EW0A_0B3_RECORDS.jsonl` | `ControllerRecordsRead` (whole-ledger) | `UNAVAILABLE`; never a filtered partial ledger |
| `_read_authority_record_evidence` | `config/ew0a_authority.json` | raw `grants`/`forbidden_ops`/`level` behind `_MISSING` | `_MISSING` for all three; validation owned by `build_worker_authority_summary` |
| `_read_system_config_readability` | the four config sources | finite readability states | per-source `ABSENT`/`UNREADABLE`; never contents or liveness |
| `_read_northstar_contract_presence` | `portfolio_automation.northstar` | present contract names | empty set (fail closed) |
| `build_run_history` · `_project_learning` · `_build_active_session` | own sources | own DTOs | separately certified producers, own boundaries |

`read_authority_level` and `_read_authority_record_evidence` are **deliberately not
collapsed**: one answers *what authority is in force* and fails closed to A0, the other
answers *what the stored record says*, so the projection can report evidence quality
without being able to escalate authority. The two reads disagreeing is itself refused
rather than resolved by picking one.

For `_read_system_config_readability` the contract is **executable**:
`SYSTEM_CONFIG_READABILITY_SOURCES` drives the actual probes, so a probe cannot be claimed
without being performed.

#### Supplemental behavioural observation

A subprocess test installs a CPython audit hook and observes that the projection phase
raises no filesystem, import, network or process audit events while the collection phase
does. It carries a **positive control** — without one, an empty event list would be
ambiguous between "no IO happened" and "the hook never fired".

**CPython audit events are observational and do not prove absence of all IO or provide a
sandbox.** Hooks cannot be removed, see only events the interpreter raises, and can be
bypassed at C level. This is supplemental evidence only; nothing depends on it for
correctness. It is included because it observes *effects* rather than recognising syntax,
so it catches a regression the static guards structurally cannot — demonstrated: a helper
added to the projection layer that reads a file passes every static guard and is caught
here.

#### The real security and truth boundary

The source-inventory tests are **not** the security boundary, and that distinction
matters. What actually prevents malformed evidence from becoming clean-looking GUI state:

- typed reader admission (readers A/B/C total and fail-closed)
- whole-ledger refusal — a partial ledger is never silently filtered
- field-level validation of every WCC-consumed record field
- fail-closed authority; the record can only ever be stricter
- no arbitrary evidence coercion — no `str()` of unvalidated payloads
- measured zero kept distinguishable from unanswerable
- learning quarantine — the uncertified payload is not admitted
- the GUI receives controller projections only, with no shell, Git or production authority

Those are behavioural and mutation-tested. GUI-RI is read-only and non-authoritative — no
Git mutation, no production mutation, no capital action, no trading — and its job is
exactly the one those contracts defend. **Mission Control does not require solving
general Python program analysis first.**

#### Where mechanical enforcement stops

`DASHBOARD_PROJECTION_REGISTRY` is an architectural/audit declaration describing the
source dependencies each projection is designed to consume. **It is not a security sandbox
or a whole-program dependency derivation engine.** Per-surface attribution remains
human-reviewed plus tested; each entry's `detail` states its reasoning so a reviewer can
check it against the call path. `SourceAccessKind` and `DirectSource` classify *permitted*
dependencies — they do not discover arbitrary Python IO, and conflating those two jobs is
what produced every finding on this PR.

#### `_readability` is certified as a probe

Its published output belongs to a finite contract, `READABILITY_STATES` =
`{ABSENT, UNREADABLE, READABLE}`. It must never emit file contents, parsed JSON, exception
text quoting the source, or a repr of any payload. A marker-based proof puts
`sk-READABILITY-MUST-NOT-RENDER-999` inside all four probed files, all READABLE, and
requires the marker to be absent from the serialized dashboard; the probe is also exercised
against an absent file, a directory in file position, an undecodable file and a
mode-denied read.

It deliberately carries **no runtime self-check**. Its contract is that it never raises,
and an internal assertion that could raise would trade that guarantee for a redundant one.
`config_readability` remains exactly what this document already said: **file readability
evidence, not liveness** — declaring the probes did not turn them into health.

#### The authority-record evidence gateway

`build_dashboard`'s second read of `config/ew0a_authority.json` is now a named function,
`_read_authority_record_evidence`, rather than an anonymous `.read_text()` embedded in the
assembler — so the boundary it crosses has a name to declare:

```text
build_dashboard → named source gateway → validated worker_authority projection
```

It **validates nothing on purpose**: `build_worker_authority_summary` remains the sole
validator of authority-record evidence, and a second opinion here would be a second
authority policy engine. It is **not** a duplicate of `read_authority_level` — that reader
answers *what authority is in force* and fails closed to A0, while the gateway answers
*what the record literally says*, so the projection can report evidence quality without
being able to escalate authority. A record claiming `A9_TOTAL_CONTROL` still yields
`A0_DIAGNOSTIC`, proven by test. Missing / null / empty stay three distinguishable states,
and a malformed record yields no evidence for **all three** fields rather than a partial
read.

Flipping the enum back, or dropping any declaration, fails tests.

**Mutation-proven — the safety properties, which is what matters.** Each mutation below
was applied and reverted; the failure count is how many tests caught it.

| | Mutation | Caught by |
|---|---|---|
| S1 | `_readability` returns file contents | 20 |
| S2 | authority record evidence bypasses validation | 3 |
| S3 | a non-dict controller-record row is admitted | 3 |
| S4 | `str()` coercion of controller evidence reintroduced | 9 |
| S5 | learning quarantine lifted — uncertified payload reported LIVE | 6 |
| S6 | runtime policy accepts a malformed bool | 3 |
| S7b | the unvalidated record level published as effective authority | 4 |

**S7 as originally specified is an equivalent mutant and is reported as such rather than
as a pass.** Publishing `str(raw_level)` on the valid-record branch changes nothing,
because `_validated_raw_level` already refuses any record whose `level` disagrees with
the canonical reader — the two values are provably equal there. S7b removes that guard as
well, which is the property the mutation was reaching for, and it is caught.

Earlier rounds also mutation-tested the retired static analyzers. Those results are
historical: the mechanism they exercised has been retired, and the guards that remain are
labelled defence in depth rather than proofs.

The registry deliberately does **not** claim the dashboard is certified.

The reason this registry exists: `learning` escaped five review rounds because the set of
projection paths lived in prose — including in this document's own audit tables. It is a
test now.

`truth_state` is set on the envelope and `freshness` is
`NOT_APPLICABLE_HISTORICAL_EVIDENCE`. **No freshness threshold is imposed on lesson
records** — they are historical evidence, and inventing an age limit would manufacture
`STALE` out of nothing (this remains the rule GUI-L inherits). The `learning`
capability is **secondary**: the interface does not make learning an oversight
requirement, so readiness follows from the required gaps whatever learning reports.

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
  VERIFIED/NOT_STARTED; verification ladder projection; run/outcome history (outcome
  ledger, via `ew0a.read_outcomes`).
- **CONDITIONAL on the controller-records ledger** — `LIVE` with measured counts when
  `controller_records.availability` is `LIVE`, `UNAVAILABLE` with `null` counts when it
  is not: supervisor verdict counts + last-pass; apprenticeship comparison metrics;
  `worker.recent_verification_outcomes`. These are **not** unconditionally LIVE, and an
  unreadable ledger is neither `PENDING_BACKEND` nor `LIVE`-with-zeros.
- **PENDING_BACKEND (no backend yet):** worker heartbeat/online/current-task/queue;
  supervisor availability/latency/queue/outage; component health
  (controller/gpt/engineer/sandbox/bridge/control-loop); `controller_since`;
  attention derivation; controller identity.
- **UNKNOWN:** active-session freshness when a session IS present — a value is held,
  and its age cannot be measured from the evidence the session contract publishes.
- **LIVE (no session):** when the session producer answers `NO_SUCH_SESSION`. That is
  an answer, not a gap, and it has no age to measure.
- **UNAVAILABLE:** an existing producer that cannot answer — an unreadable outcome
  ledger, a session producer that raises, a learning producer whose own imports fail,
  an authority record whose `level`/`grants`/`forbidden_ops` evidence is malformed, and
  the **learning projection while its payload contract is uncertified**.

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
`run_history` is LIVE from a producer that exists. `learning` is **`UNAVAILABLE`** —
its producer exists but its payload contract is uncertified, and it is a **secondary**
capability, so it does not by itself move readiness. `active_session` is `UNKNOWN` — see
the active-session section: its age is unmeasurable, which is not the same as old.

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
