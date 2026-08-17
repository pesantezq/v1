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

### Structural validation is the boundary

A field is safe because **its contract constrains what it can contain**, not because a
generic substring filter failed to recognise the value as a secret. Each allowlisted key
has a validator:

| Field | Accepts | Rejects |
|---|---|---|
| `toolset` | dotted lowercase identifier, ≥1 dot, ≤64 (`gpt_supervisor.review`) | bare opaque tokens (`ghp_zzz…`), uppercase |
| `toolset_version` | `v1`, `1.2.3`, or lowercase token, ≤24 | `AKIA…`, uppercase, opaque strings |
| `protocol`, `mode` | lowercase hyphenated token, ≤24 (`one-shot`, `strict`) | `sk-A1b2…`, `ghp_…`, uppercase, underscores |
| `timeout` | `int` 1–86 400 (**not** `bool`) | strings, floats, `True`, out of range |
| `max_tokens`, `max_completion_tokens` | `int` 1–1 000 000 (**not** `bool`) | strings, dicts, floats |
| `api_base_host` | dotted host or `localhost`, optional `:port`, normalised lowercase | userinfo, query, fragment, path, bare single labels |

An earlier version relied on a substring filter and **leaked**: `mode = "sk-A1b2C3d4"`
contains none of key/token/secret/password/bearer, and `timeout` accepted a string at all
because nothing enforced its type.

`api_base_host` **rejects rather than strips**. A URL carrying userinfo, a query or a
fragment is evidence the caller is passing secret-bearing material; keeping just the
hostname would discard that signal. A bare single label is also rejected — `sk-a1b2c3d4`
is a syntactically valid hostname, so grammar alone would admit a credential there.

A credential-pattern check is retained as **defence in depth**, applied to values after
the structural contract has passed. No claim of reliable generic secret detection is made
or needed. Note the allowlist governs **keys** only: an earlier over-eager key-name screen
dropped the legitimate `max_tokens` and `max_completion_tokens` because their names
contain "token".

Only the safe projection plus a digest over it is kept, so two configurations remain
distinguishable without either being readable. `dropped_keys` records what was excluded.

**A pre-existing leak was found and closed by this work.** The `REVIEWER_CALLED` journal
record wrote `reviewer_identity` as a raw dict; a caller placing `api_key` or `key_file`
there would have written a credential into an append-only ledger. It is now screened
through the same projection. No production security detector was weakened.

### `review_invocation_id` — accurate statement of the property

`review_invocation_id` hashes the **raw** reviewer identity mapping, and the resulting
identifier **is** written into lifecycle records. The accurate property is therefore:

> Raw credential material is not persisted by that path; only the derived identifier is.

That is weaker than "nothing persists", and it is the correct claim. A one-way derived
identifier is not the same as absence.

**Recorded as debt, not changed here.** If a caller placed a low-entropy secret or a
guessable key-file path into the reviewer identity, the persisted digest would in
principle be subject to offline dictionary correlation. Current callers pass only
`{provider, model, protocol}`, so no such value is known to have entered the hash.
Screening the invocation-id material would change **every historical invocation id** and
therefore the recorded review lineage, which is outside this repair's authorised scope.

## Production integration

The identity is built **once** per review by `ReviewContext.execution_identity()` and
attached to the `PACKET_BUILT`, `REVIEWER_CALLED` and `VERDICT_PERSISTED` records in
`dispatch_durably` — the mandatory certification path the operating loop is forced
through. A test asserts all records naming one review carry one identical `execution_id`;
divergence would make the attribution useless for grouping.

Field-building lives in `build_execution_identity()` alone, so producers cannot drift and
tool configuration is screened exactly once.
