# Execution Identity — which configuration produced this decision?

**Module:** `portfolio_automation/engineer_worker/execution_identity.py`
**Schema:** `engineering.execution_identity.v1`
**Tests:** `tests/test_execution_identity.py`

## Why

Identity information was real but scattered: a prompt version in the controller's
telemetry dict, a model name on the supervisor decision, a repository SHA in two places
with two different meanings, an authority level recorded nowhere at all. Each piece was
individually reasonable and collectively unusable — there was no way to ask *"show the
supervisor PASS decisions produced by model X under prompt version Y"* without
reconstructing the answer by hand, if at all.

Over a multi-year autonomous program models change, prompts change, toolsets change and
authority changes. Apprenticeship history that cannot name the configuration that
produced it is not evidence; it is anecdote.

## Fields

**Identity material** (participates in `execution_id`; the list is explicit in
`ExecutionIdentity.IDENTITY_FIELDS`, so a field cannot silently join or leave):

| Group | Fields |
|---|---|
| worker | `worker_id`, `worker_role`, `authority_level` |
| model | `model_provider`, `model_name`, `model_version` |
| instruction | `prompt_version`, `instruction_version` |
| tools | `toolset_id`, `toolset_digest` |
| repository | `repository_sha`, `candidate_sha`, `branch` |
| task / input | `task_id`, `mission_id`, `input_id` |

**Record metadata** (NOT identity material): `recorded_at`, `schema_version`,
`schema_kind`, `toolset_safe_config`, `unavailable_attributes`.

`repository_sha` and `candidate_sha` are deliberately distinct. The first is the tree an
execution *ran against*; the second is the tree a verdict *certifies*. Collapsing them
once produced a report claiming a PASS covered code the reviewer never saw.

## Determinism

`execution_id = "exid_" + sha256(canonical(identity_material))[:32]`.

`recorded_at` is excluded. If the timestamp participated, every replay would mint a new
identity and grouping — the entire purpose — would be impossible. Serialization is
`sort_keys=True`, so dictionary ordering cannot change an id.

## Unknown / unavailable semantics

| Value | Meaning |
|---|---|
| `UNAVAILABLE_AT_RECORD_TIME` | the producer genuinely could not determine this attribute |
| `LEGACY_UNATTRIBUTED` | the record predates this schema; attribution was never captured |

Chat APIs do not return the build actually served, so `model_version` is normally
`UNAVAILABLE_AT_RECORD_TIME`. Recording the configured *name* there would claim precision
nobody has. Every field defaults to unavailable rather than to a plausible value, so a
producer that forgets to supply something records that it did not know — which is
recoverable — instead of something false, which is not.

The sentinels are deliberately not `"legacy"`, `"unknown"` or `"default"`: those could be
mistaken for real identifiers.

`unavailable_attributes` is emitted on every record, so a later audit can ask *"which
records came from a model whose exact version was unavailable?"* directly.

## Backward compatibility

- A record with **no** identity block, or with `schema_version` absent, loads as
  `LEGACY_UNATTRIBUTED` with all attributes unavailable. Nothing is inferred — filling in
  today's configuration would attribute a past decision to a configuration that may never
  have produced it.
- A record with an **unknown future** schema version raises. A silent misreading would
  attribute records to configurations that never produced them, which is worse than
  refusing to load.

## Security

Tool configuration is the realistic leak path — it is the one place an API key or endpoint
credential plausibly lives. Identity records are durable, replicated and append-only, so a
leak there is permanent.

Two independent filters, because either alone fails:
1. **key allowlist** (`_SAFE_TOOL_KEYS`) — a denylist would fail open on the key nobody
   thought of;
2. **value screen** — catches a credential smuggled under an allowlisted-looking key, and
   drops over-long values.

Only the safe projection plus a digest over it is kept, so two configurations remain
distinguishable without either being readable. `dropped_keys` records what was excluded.

**A pre-existing leak was found and closed by this work.** The `REVIEWER_CALLED` journal
record wrote `reviewer_identity` as a raw dict; a caller placing `api_key` or `key_file`
there would have written a credential into an append-only ledger. It is now screened
through the same projection. No production security detector was weakened.

## Production integration

The identity is built **once** per review by `ReviewContext.execution_identity()` and
attached to the `PACKET_BUILT`, `REVIEWER_CALLED` and `VERDICT_PERSISTED` records in
`dispatch_durably` — the mandatory certification path the operating loop is forced
through. A test asserts all records naming one review carry one identical `execution_id`;
divergence would make the attribution useless for grouping.

Field-building lives in `build_execution_identity()` alone, so producers cannot drift and
tool configuration is screened exactly once.
