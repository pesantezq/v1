# Northstar Canonical Contracts — Phase 0B Architecture

Status: milestone 2 (`northstar_0b_prediction_research_experiment_contracts`)
— evidence kernel + **prediction family (PredictionTask, PredictionRecord)**
and **research family (ResearchTask, WorkerResult, ResearchClaim)**
**implemented** (`portfolio_automation/northstar/`); the experiment and
decision/outcome/passport families remain **specified here, not implemented**
(milestone 3). Machine-readable status:
`.agent/phase_status.yaml:northstar_phase_0b`. Charter context:
`docs/NORTHSTAR_REDESIGN.md`.

This document is the Phase 0B design authority for the stable domain language
used by future Northstar components. Contracts are **references/consumption
relationships, never inheritance**: a `CapitalProposal` references
`PredictionRecord`s; it is not a subclass of one and can never mutate one.

---

## 1. The two pipelines the contracts serve

Decision path:

```text
Evidence → Prediction → Capital → Exit → Outcome
```

Research path (independent):

```text
Evidence → ResearchTask → WorkerResult → ResearchClaim
        → ExperimentSpec → ExperimentResult → Certification
```

## 2. Contract technology (decided)

**Frozen dataclasses (`frozen=True, slots=True`) + explicit `__post_init__`
validation + string constants/frozensets for enums + a single strict
canonical serializer.**

- Matches the repository: 69 modules use dataclasses, 44 use `frozen=True`,
  `sim_governance/schemas.py` sets the string-constant/frozenset enum
  convention, and there is **zero direct pydantic usage**.
- Rejected: **Pydantic** — available only transitively via FastAPI; adopting
  it as the contract foundation would introduce a new framework commitment
  and a second validation idiom for no capability we lack. **TypedDict** —
  no runtime validation or immutability. **attrs** — new dependency.
- Python 3.12, deterministic tests, JSON serialization and schema-version
  representation are all satisfied by the kernel primitives below.

## 3. Shared kernel primitives (implemented)

`portfolio_automation/northstar/canonical.py`:

- `canonical_dumps` — THE canonical serializer: sorted keys, compact
  separators, ASCII, `allow_nan=False`; str/int/finite-float/bool/None;
  tz-aware datetimes → ISO-8601 UTC `Z` (naive datetimes are a hard error);
  dates → ISO; str-keyed mappings; sequences → arrays; anything else raises
  `CanonicalizationError` (fail closed — no `default=str` coercion).
- `content_hash` — sha256 hex over canonical bytes (single hash primitive).
- `deterministic_id(prefix, identity_payload)` — `prefix_<sha256[:32]>`.

Inherited from `intraday_lab/identity.py`: the algorithm family (sha256 over
sorted compact canonical JSON), the single-primitive discipline, the
schema-versioned identity philosophy, and the 32-hex truncation width.
**Deliberate divergence**: intraday's `default=str` coercion is rejected here
— open-ended contract payloads must never take identity from Python reprs.

Every persisted contract carries explicit `contract_type` + `schema_version`
("1.0.0" initially; evolution is additive; module/class names are never
historical schema identity). Deserializers validate both and **recompute
identity** — a serialized object whose recorded id/hash does not reproduce is
rejected (tamper evidence).

**Schema-version policy (hardened 2026-08-10; exact rules):**

- Deserialization (`from_dict`) requires the EXACT supported version via the
  single gate `serde.require_schema_version`: missing, empty, non-string, and
  unknown/future versions are all rejected. Kernel v1 has **no migration or
  compatibility mechanism** — deliberately; accepting an unknown version would
  mean interpreting bytes under semantics this code cannot know.
- Constructors pin `schema_version` to the module's `SCHEMA_VERSION`: running
  code IS kernel v1 and can only mint v1 objects.
- **Identity participation — the schema era.** `schema_version` participates
  in every deterministic identity through its MAJOR component only
  (`canonical.schema_era`, strict `MAJOR.MINOR.PATCH` parse). A major bump
  re-defines field semantics, so identical bytes under a new major version are
  a new object — a new identity era (the Intraday Lab discipline: integrity ≠
  eligibility, and era changes are explicit, never accidental). Minor/patch
  versions are additive/non-semantic by definition and do NOT fragment
  identity. Because `snapshot_id` embeds `source_id` and features embed input
  snapshot ids, an era bump cascades through the identity graph by
  construction; cross-era mapping is an explicit future migration concern.

**Identifier integrity (centralized in `canonical.py`):**
`validate_contract_id` (`<prefix>_` + exactly 32 lowercase hex) and
`validate_content_hash` (exactly 64 lowercase hex) are the single format
validators. `EvidenceRef` — which future contracts embed everywhere — carries
its own explicit `schema_version` and validates `snapshot_id` (`evs_`),
`source_id` (`src_`), and `payload_hash` at construction;
`supersedes_snapshot_id` and snapshot/provenance `source_id`s are validated
the same way.

**Provenance consistency (canonical provenance can never contradict its
owning contract):** `producer_type="source_adapter"` REQUIRES
`provenance.source_id` (an adapter without a source is incoherent — fail
closed); when a snapshot's provenance names a source it must equal
`snapshot.source_id`; a FeatureRecord's `provenance.transformation_id`, when
present, must be exactly `"<derivation_id>@<derivation_version>"` (the
explicit derivation fields remain the identity-bearing authority — they are
not collapsed into Provenance).

Identity rule used across all contracts:

```text
stable semantic fields → canonical serialization → sha256 → deterministic ID
```

Excluded from every identity: acquisition metadata (`retrieved_at`,
`provenance.recorded_at`) — re-acquiring identical information reproduces the
identical ID. No random UUIDs, PIDs, paths, or object identity.

`provenance.py` — small explicit `Provenance` (producer_id, producer_type ∈
{source_adapter, derivation, system, ai_worker, human}, recorded_at,
code_version?, model_id?, source_id?, transformation_id?). Not an arbitrary
metadata dict; new dimensions arrive via schema evolution.

`pit.py` — the point-in-time envelope (see §4).

## 4. Point-in-time model (implemented)

| Field | Meaning |
|---|---|
| `observed_at` | when the underlying event/value occurred (e.g. price timestamp) |
| `published_at` | when the source released it (e.g. filing publication) |
| `known_at` | earliest **defensible** moment StockBot could have known/used it — the future anti-lookahead authority; Phase 0C enforces `evidence.known_at <= experiment.as_of` |
| `retrieved_at` | when StockBot acquired this copy |
| `effective_period_start/end` (+label) | the business period the information refers to (fiscal 2026-Q2 ended 06-30, published 08-05 → a July-15 backtest must NOT see it) |

**No fabricated time**: all timestamps optional; absence is explicit;
`known_at` requires a documented basis ∈ {`source_reported`,
`system_observed`, `derived_conservative`, `unknown`} and the pairing is
validated in both directions. The only sanctioned derivation is the explicit
`with_conservative_known_at()` (`known_at := retrieved_at`, basis
`derived_conservative` — it can under-claim knowledge but never lookahead).
Naive datetimes are rejected everywhere.

## 5. Evidence family (implemented)

```text
External Source → DataSourceDescriptor → EvidenceSnapshot → EvidenceRef → FeatureRecord
```

### DataSourceDescriptor (`src_…`)

Identifies/characterizes a provider dataset. Identity = (provider, dataset,
source_type) + schema era (§3); characterization (access/rights/cost/pit_capability/
historical_capability/status) may be re-stated without minting a new source.
Rights are never claimed unverified (`rights_class` defaults `unknown`).
**No authentication surface by design** — no url/endpoint/key/token fields
exist, and a structural guard rejects credential-looking material in any
string field. Auth belongs to future source adapters/runtime config.

### EvidenceSnapshot (`evs_…`)

The immutable canonical evidence unit. Canonical **envelope** (source_id,
entity_id, entity_type, evidence_type, PIT, hashes, provenance,
supersedes_snapshot_id?) is separated from the domain **payload** (strict
JSON, frozen to canonical bytes at construction; `payload_copy()` returns
fresh copies so no caller mutation can touch stored evidence or identity).
Identity includes the PIT view minus `retrieved_at` and the payload hash.
**Revision**: never mutate — `revise()` produces a new snapshot with
`supersedes_snapshot_id` linking the chain. **Multi-source coexistence**: no
uniqueness on entity+metric+period; SEC and FMP snapshots of the same revenue
coexist as distinct records (Phase 0C may emit `DATA_CONFLICT` from such
pairs).

### EvidenceRef

Lightweight immutable pointer (snapshot_id, source_id, entity_id,
evidence_type, payload_hash, schema_version) with strict identifier-format
validation at construction (§3). Future contracts embed refs, never copies, so
any downstream artifact can prove exactly which evidence it depended on;
`matches()` verifies id AND content hash.

### FeatureRecord (`ftr_…`)

A **derived transformation**, never raw evidence: value (JSON scalar or small
numeric sequence, ≤32; floats per repo convention — Decimal appears once in
a charting module and nowhere in quantitative code; precision-sensitive value
kinds can be added additively later), `as_of` PIT anchor, derivation_id +
derivation_version, mandatory input `EvidenceRef`s (except explicit
`quality="missing"` with `value=None`), provenance, quality ∈ {ok, degraded,
missing}. Identity covers name/derivation/version/entity/as_of/value/quality/
input ids — a recomputation that differs is a different record.
**Input semantics (decided, test-enforced):** `inputs` is an UNORDERED
DEPENDENCY SET — how each snapshot is used is the derivation's business,
versioned by `derivation_id`/`derivation_version`, not by argument order.
Identity sorts input snapshot ids (reordered refs produce the identical
`feature_id`) and duplicate snapshot ids are rejected. Role-labeled inputs
would be an additive future change if a concrete correctness need arises.

## 6. Contract families beyond the evidence kernel

Milestone 2 implemented the prediction + research families below. Milestone 3
families (ExperimentSpec onward) remain specified-only. Implemented families
add these kernel-wide conventions (0B.2 hardening-final):

- **Unordered collections** accept ONLY list/tuple (strings/bytes/mappings/
  sets/generators rejected — `entity_ids="IBM"` raises), reject duplicates,
  and normalize to sorted canonical tuples (equality ≡ identity ≡
  serialization). Where `"*"` means explicitly unrestricted
  (`allowed_evidence_types` on PredictionTask/ResearchTask) it must be the
  SOLE value.
- **Provenance is REQUIRED on all five Milestone-2 contracts**
  (PredictionTask, PredictionRecord, ResearchTask, WorkerResult,
  ResearchClaim). For PredictionTask/ResearchTask/ResearchClaim it is
  attribution metadata and does NOT participate in semantic identity — a
  `recorded_at`-only change never changes `ptk_`/`rtk_`/`rcl_` ids. For
  WorkerResult, `worker_id == provenance.producer_id` is enforced, producer
  type must be `ai_worker` or `system`, and `provenance.model_id` IS
  identity-bearing (a different model is a different result);
  `recorded_at` stays non-identity everywhere, and worker `code_version` is
  deliberately excluded from identity (reproduction/attribution is the
  ExperimentSpec path's job — deploys must not fragment result identity).
  PredictionRecord's `provenance.model_id`, when present, must equal
  `<model_id>@<model_version>`.
- **Deep immutability**: identity-bearing mappings freeze to canonical bytes
  at construction (snapshot-payload discipline: `EvidenceSnapshot.payload`,
  `WorkerResult.findings`, `PredictionTask.target_params`); numeric sequence
  values freeze to tuples (`PredictionRecord.prediction_value`/
  `uncertainty_value`) — caller mutation can never change identity.
- **First-class abstention** on PredictionRecord (like WorkerResult):
  `abstained=True` requires a reason and Nones for prediction/uncertainty
  (never zeros/NaN/magic values); evidence refs may be empty or retained;
  abstention fields are identity-bearing. Normal records still require
  value, uncertainty, and non-empty evidence.
- **Authority/action surfaces structurally banned** (prediction contracts
  reject allocation/action keys; worker findings reject authority keys).

Each family below lists responsibility / producer / consumer / key fields /
identity / lifecycle / failure semantics. All inherit the kernel rules:
frozen, canonical serialization, `contract_type` + `schema_version`,
deterministic ID from semantic fields, explicit provenance, EvidenceRefs
instead of copies, additive evolution.

### PredictionTask (`ptk_…`) — IMPLEMENTED (milestone 2, predictions.py)

- **Responsibility:** define the prediction question: entity/universe, as_of,
  horizon, target definition, allowed evidence/feature scope.
- **Producer:** R&D control plane / StratLab schedulers. **Consumer:**
  Prediction Engine v2, experiment tooling.
- **Required:** task id fields, entity/universe spec, `as_of` (tz-aware),
  horizon, target spec, allowed evidence/feature classes. **Optional:**
  notes, constraints.
- **Identity:** all semantic question fields. **Immutable**; a changed
  question is a new task.
- **Failure semantics:** malformed universe/horizon/target → construction
  error; tasks never carry results.

### PredictionRecord (`prd_…`) — IMPLEMENTED (milestone 2, predictions.py)

- **Responsibility:** one estimate: prediction value/distribution,
  uncertainty/confidence, model identity, horizon; references its
  PredictionTask, EvidenceRefs, FeatureRecord ids; eventual resolution
  linkage (OutcomeRecord id, set by a NEW linking record, never mutation).
- **Producer:** Prediction Engine (future certified) / research predictors.
  **Consumer:** Capital & Risk, StratLab attribution, OutcomeRecord builders.
- **Hard boundary:** records an estimate; carries NO allocation/action
  surface. `known_at`-clean inputs only (Phase 0C enforcement).
- **Failure semantics:** a prediction that cannot state its evidence is
  invalid; uncertainty is required (no implied certainty).

### ResearchTask (`rtk_…`) — IMPLEMENTED (milestone 2, research.py)

- **Responsibility:** bounded research work definition compatible with the
  future sandbox/control-plane execution (Prime-free R&D direction).
- **Producer:** R&D control plane / human. **Consumer:** generic workers.
- **Required:** question, scope bounds, allowed evidence, output contract
  expectations, budget/effort class.

### WorkerResult (`wkr_…`) — IMPLEMENTED (milestone 2, research.py)

- **Responsibility:** structured output of a research worker. **Never
  production truth** — evidence/candidate material only (authority:
  `config/agent_policy.yaml`).
- **Required:** research task ref, producing worker identity (provenance
  `producer_type="ai_worker"` or tool identity), EvidenceRefs consumed,
  structured findings, abstention/uncertainty representation.
- **Failure semantics:** a result claiming authority fields fails validation.

### ResearchClaim (`rcl_…`) — IMPLEMENTED (milestone 2, research.py)

- **Responsibility:** a falsifiable hypothesis distilled from worker results/
  evidence: claim statement, direction, scope, supporting EvidenceRefs +
  WorkerResult refs. A claim is NOT certified alpha.
- **Consumer:** ExperimentSpec authors; StratLab routing (quant_router).

### ExperimentSpec (`exs_…`) — milestone 3

- **Responsibility:** immutable **preregistered** test definition (reuses the
  Intraday Lab preregistration/identity-era discipline): hypothesis
  (ResearchClaim ref), universe, PIT `as_of` policy, evaluation windows,
  metrics, success/abandon gates, allowed evidence classes.
- **Identity:** the full preregistration — any change is a new experiment.
  Registered BEFORE results exist; results reference the spec, never edit it.

### ExperimentResult (`exr_…`) — milestone 3

- **Responsibility:** results generated under one ExperimentSpec: metrics,
  windows, evidence consumed (refs), code/model provenance, verdict inputs
  for certification. **Not** a promotion decision.
- **Failure semantics:** a result whose spec ref is missing/unreproducible is
  invalid; partial runs must say so explicitly.

### CapitalProposal (`cap_…`) — milestone 3

- **Responsibility:** independent ADVISORY allocation proposal: sizing/limits
  under deterministic risk+governance constraints, referencing the
  PredictionRecord(s) and evidence it relies on.
- **Hard boundaries:** references predictions, never contains or mutates
  them; no execution surface; advisory-only; human real-action gate lives
  outside contracts (`production_control_plane`).

### ExitProposal (`xit_…`) — milestone 3

- **Responsibility:** independent continuation/trim/exit/replacement
  proposal, separately attributable from entry prediction and allocation.
  References position context, predictions, evidence.

### OutcomeRecord (`out_…`) — milestone 3

- **Responsibility:** resolved results enabling component-level attribution:
  what the prediction said, what capital/exit proposed, what actually
  happened — linked by refs so prediction/allocation/exit quality are
  separately measurable per the North Star.

### StrategyPassport (`spp_…`) — milestone 3

- **Responsibility:** identity, evidence trail, certification status, and
  lifecycle of a strategy/capability:
  `experiment evidence → passport → challenger → certified influence
  eligibility → retain/reduce/suspend/retire`.
- Certification status changes are append-style (new versions referencing
  prior), never silent mutation. Commercial attractiveness can never alter a
  passport's evidence or certification standards.

## 7. Cross-contract invariants (architecture law, test-enforced now and later)

```text
Evidence != Feature                    Feature != Prediction
Prediction != Allocation               Allocation != Execution
Exit != Prediction                     WorkerResult != ResearchClaim certification
ResearchClaim != proven alpha          ExperimentResult != production promotion
AI output != real portfolio authority  Vendor schema != canonical/engine schema
```

Milestone 1 enforces mechanically: evidence contracts expose no
execute/order/trade/approve/promote/allocate/broker/position surface; feature
ids cannot impersonate evidence ids; descriptors cannot carry credentials;
later families are asserted absent until their milestone.

## 8. Data-source extensibility (standing requirement)

```text
Vendor / Source → Source Adapter → Canonical Evidence → Feature Assembly → Consumers
```

Consumers (Prediction, Capital & Risk, Exit, StratLab, AI workers, Product
Factory) depend on canonical evidence/features only. Adding a future source —
e.g. a new sentiment vendor — is: author a `DataSourceDescriptor`
(rights/cost/PIT capability honestly classified), build an adapter that emits
`EvidenceSnapshot`s (`evidence_type="social.sentiment_score"`, PIT populated
per §4, payload = canonical facts not vendor response), derive
`FeatureRecord`s (e.g. `attention_acceleration_3d`). **No Prediction/Capital/
Exit contract changes.** Sources that are currently unaffordable/unlicensed
(FINRA short interest, institutional holdings, congress trades, analyst
revisions, options surfaces, transcripts, macro, search interest, commercial
data) require no architectural change later — only adapters. Unknown rights
stay `rights_class="unknown"`; missing PIT history stays
`pit_capability="none"|"unknown"` and `known_at_basis="unknown"` — historical
incompatibility is documented at the adapter, never papered over in the
contract.

## 9. Failure semantics (kernel-wide)

Construction validates or raises (`ValueError` /
`CanonicalizationError`) — no partially-valid contract objects.
Deserialization recomputes hashes/ids and rejects mismatches. Unknown
`contract_type` is rejected; `schema_version` must be exactly the supported
version (missing/empty/unknown/future all rejected — see §3, no migration
framework exists). Nothing in this
package writes files, calls networks, or is wired into any pipeline — a
consumer arrives with the Phase 0C EvidenceGateway.
