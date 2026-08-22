# EW-0B — Adversarial hardening of the supervised engineering loop

`experimental_noncanonical`. This document describes engineering control-plane
behaviour. It defines no canonical Northstar contract and changes no capital,
prediction or production semantics.

EW-0A proved the loop works. EW-0B asks the harder question: **what does it do
when things go wrong?** A system that only behaves on clean tasks has not been
shown to be safe — it has been shown to be lucky.

The property this phase exists to establish:

> When the worker is wrong, evidence is weak, state is stale, infrastructure
> fails, authority is exceeded, or repair does not converge, the system does the
> safe thing automatically and leaves enough durable evidence to explain exactly
> what happened.

---

## What was already true

The loop entering EW-0B already enforced a great deal, and most of this phase is
guards and tests around it rather than new machinery:

| Property | Mechanism |
|---|---|
| worker never self-certifies | `certify_attempt` requires deterministic PASS **and** independent GPT PASS |
| supervisor is independent | `gpt_supervisor.review`, separate process/provider, prompt forbids trusting implementer claims |
| deterministic gate precedes the model | `deterministic_check` runs first; a breach never reaches the reviewer |
| protected paths | `policy.is_protected` → `POLICY_VIOLATION`, `STOP_NO_RETRY` |
| supervisor outage fails closed | `SUPERVISOR_UNAVAILABLE` → `VERIFYING`, never `VERIFIED` |
| stale candidate | `CandidateBinding.resolve_head_terminal` re-resolves HEAD immediately before dispatch |
| duplicate / replay | `review_invocation_id` + `VERDICT_ALREADY_RECORDED` |
| crash recovery | write-ahead `REVIEWER_CALLED`, fsynced, torn tail ⇒ indeterminate |
| authority envelope | `ew0a_authority.FORBIDDEN_OPS`, denied at every level including A1 |
| Claude does not bypass GPT | escalation returns through `certify_attempt` |

## What EW-0B found, and changed

### 1. The deterministic gate could be satisfied by giving it nothing

`deterministic_check` validates the tests that **ran**. It never asked whether
any did. `tests_ok` starts `True` and only a failing entry falsifies it, so
`tests_run=[]` cleared the gate vacuously. `scope_ok` had the same shape:
`all(... for p in [])` is `True`, so an attempt that changed nothing was "in
scope".

Both are correct universal quantifications over an empty set. Both mean the
cheapest way to satisfy a gate built from `all(...)` is to hand it nothing.

`evidence_sufficiency.assess_evidence` closes this. It runs **after** the
deterministic gate and **before** the reviewer, and refuses on:

| Refusal | What it catches |
|---|---|
| `NO_ACCEPTANCE_CRITERIA` | a PASS would certify agreement with an empty requirement set |
| `NO_CHANGED_PATHS` | completion claimed, nothing changed |
| `NO_TESTS_RUN` | the task has an approved test surface and the attempt used none of it |
| `NO_DIFF_EVIDENCE` | paths named, no diff behind them |
| `CHANGED_PATH_ABSENT_FROM_DIFF` | a claimed file never appears in the diff meant to substantiate it |
| `RESULT_WITHOUT_RUN` | a PASS reported for a test that was never executed |

It does **not** judge whether tests are *good*. Test quality is semantic and
belongs to the supervisor, which reads the diff. This gate answers only the
mechanically decidable half — and asking a language model to notice an omission
is strictly weaker than refusing the omission deterministically.

Failures classify as `EVIDENCE_INSUFFICIENT` and the reviewer is **not called**,
so a mechanically decidable deficiency costs no reviewer invocation.

### 2. The mission boundary compared a belief to itself

`run_mission` refused any task whose `mission_id` differed from
`policy.mission_id`. Real, but both sides came from the same caller: a runtime
policy naming `g1_supervisor_measurement` and a queue of G1 tasks agree with each
other perfectly, and the loop would have dispatched them.

This is the defect `review_candidate` was built to fix, one layer up. A boundary
constrains something only when at least one side is resolved from an authority
the caller does not author.

`roadmap_guard.RoadmapAuthorization.read` resolves the authorized mission from
`.agent/phase_status.yaml` → `engineer_runtime_state.mission_id`. That record is
a **protected path**, so the worker can read it and can never write it.

`run_task` and `run_mission` both take `roadmap` as a **required, undefaulted
keyword** — the same discipline `certification` already uses, for the same
reason: a guard that can be omitted is a guard that will be omitted.

Every failure mode (missing file, unparseable YAML, absent key, empty/IDLE
mission) resolves to *no mission is authorized*, which refuses everything. The
failure mode of this module is a stopped loop, never a widened one.

**Review repair.** Taking a `RoadmapAuthorization` is right for a harness and
wrong as the *only* door: a caller able to construct
`RoadmapAuthorization.for_mission(x)` can authorize `x`, which is the applicant
authorizing itself — precisely the self-consistency the guard was built to
break. `RoadmapAuthorization.authoritative` is now True only for `.read()`, and
`run_authorized_mission` is the **production entry point**: it resolves *both*
sides from protected on-disk state — the roadmap record for which mission is
authorized, the runtime policy for what the loop is configured to run — and has
no parameter through which authorization can be injected. Agreement between two
independently protected records is not self-consistency. The synthetic path is
not removed, only made unreachable from there.

### 3. A genuine repair could never certify

`run_task` used one `certification.candidate_binding` for every attempt. A repair
that actually changes code commits a **new** candidate, so HEAD moves and
dispatch refuses `HEAD_NOT_UNCHANGED_AT_DISPATCH`.

Failing closed was correct. But it meant the repair-to-certification path — the
core EW-0B graduation proof — could not complete against a real new candidate,
and had never been exercised end to end.

**Review repair — this was fixed wrongly the first time.** The initial fix let
`AttemptEvidence` carry the binding *object*. `AttemptEvidence` is
**worker-produced evidence**, and a binding object is **behaviour**: one whose
`resolve_head_terminal` always answered `YES` would have certified anything,
from inside the very structure the gate exists to judge. It handed the applicant
the gate.

A worker may **name** a candidate; only the controller may **bind** one.
`AttemptEvidence.claimed_candidate_sha` is a string and nothing more — inert.
`ReviewContext.candidate_binder` is controller-owned and resolves the binding
from version control, ignoring the claim and binding whatever is actually
checked out; `resolve_candidate` then checks the claim against that answer and
refuses on disagreement (`POLICY_VIOLATION` → `STOP_NO_RETRY`, supervisor never
called). With no claim, the controller's own binding is used unchanged.

Each candidate still gets its own SHA, packet, `review_invocation_id` and
verdict; candidate 1's PASS is not reachable for candidate 2.

### 4. A worker exception took down the mission

An unhandled exception from `engineer_fn` propagated through `run_task`, out of
`run_mission`, and past the outcome append — so the run left **no durable record
of what happened**. A malformed return was the quieter version of the same
hazard: a non-`AttemptEvidence` object reached the gate, where `getattr` defaults
made it look like an attempt that simply changed nothing.

`_invoke_worker` classifies both as `WORKER_FAILURE`. Neither can certify:
neither produces evidence, and evidence is what the gate consumes. Bounded retry
applies; when every attempt dies before producing evidence the task stops as
`WORKER_UNAVAILABLE` rather than spending an escalation budget handing Claude an
empty packet.

### 5. The refusal path could crash

`dispatch_durably` formatted `candidate_binding.refusals` with `r.value`,
assuming enum members — but the binding is a supplied protocol object.
An `AttributeError` there converts a clean fail-closed refusal into an unhandled
exception that escapes the loop. Defensive formatting is usually a smell; on a
refusal path it is the requirement.

### 6. The operator rehearsal harness mutated trusted state

`tools/ew0a_dry_run.py` called `write_runtime_policy(REPO, ...)`, overwriting the
live `config/ew0a_runtime.json` — the file that decides which mission the
autonomous loop may dispatch — with a synthetic mission, as a side effect of a
rehearsal. It also opened its `ReviewContext` on the live repo, so synthetic
review packets and lifecycle records would land in the **real** durable evidence
store, where a later audit could not distinguish a rehearsal from a real review.

Both were latent, not realised: the live journal contains zero dry-run entries
and no synthetic policy was ever committed. The harness now runs entirely under a
throwaway root.

It was also **silently broken**: with no candidate binding, every task reported
`SUPERVISOR_UNAVAILABLE`. It had not been re-run since durable certification
became mandatory. A rehearsal that cannot reach the reviewer rehearses nothing.

---

### 7. A candidate that failed before dispatch did not name itself

`candidate_sha` was stamped only on the post-dispatch verification records. An
attempt that failed the deterministic gate, or the evidence gate, or abstained,
recorded `None` — so reading the lineage back, a failed candidate could not be
told apart from a failure of some *other* candidate.

The candidate is now resolved **before any return path exists**, and every
outcome for a produced attempt names the commit it was about. The paired
negative control matters as much: when nothing is resolvable the record still
says `None` rather than a plausible SHA, so this did not become "always stamp
something".

### 8. `STOP_NO_RETRY` was only half-applied

`action_for_failure(POLICY_VIOLATION)` is `STOP_NO_RETRY`, and the engineer was
indeed not retried — but `run_task` then **fell through to Claude escalation**.
So a protected-path or out-of-scope breach still spent an escalation, and if
Claude returned a clean candidate the task could reach `VERIFIED`: a run whose
first act was an authority violation ending in a certification.

Escalation is for work that is legitimately hard — a REPAIR the engineer cannot
converge on, an explicit ESCALATE, an exhausted repair budget, an E3 task. A
boundary breach is not hard work; it is out of bounds, and handing it to a *more
capable* executor is the one response that cannot be right.

A `POLICY_VIOLATION` now terminates the affected branch at
`TaskStatus.FAILED_VALIDATION` (which is in `ew0a._TERMINAL`): no engineer
retry, no Claude, and no supervisor call, because the breach was caught
deterministically before dispatch. The rule does not depend on who breached it
— a violating *Claude* candidate is equally terminal and does not get a second
turn. At mission level the violation is **recorded and then stopped**: a stop
with no record is indistinguishable from a crash, and a record with no stop
walks on to the next execution path.

`TaskRunResult.policy_violation` carries the decision as a flag rather than
leaving the mission layer to re-derive it by string-matching `failure_class`.

The paired positive controls are what keep this from becoming “nothing ever
escalates”: ordinary test failure still retries, repeated legitimate REPAIR
still reaches Claude (Scenario 13), explicit ESCALATE still escalates without
burning the repair budget, E3 still routes straight to Claude, and a
non-converging REPAIR still stops at the *escalation* boundary rather than the
policy one — the two must stay distinguishable.

## Attribution

Every lifecycle record naming a decision carries an `execution_identity`
(`engineering.execution_identity.v1`), built once per dispatch so producers
cannot drift. It is surfaced onto `EngineeringVerificationV0` and onto the
`OutcomeRecord`, so the apprenticeship corpus can answer — without parsing prose
— what was attempted, which candidate was produced, what failed, what the
supervisor decided, whether repair or escalation occurred, and under which
configuration.

`OutcomeRecord.attempt_lineage` carries one row per attempt: `attempt_id`,
`executor`, `candidate_sha`, `verdict`, `supervisor_verdict`, `failure_class`,
`escalated_from_attempt_id`, `worker_error`. That last field is the Claude
escalation link — the smallest additive linkage that distinguishes

```
engineer attempt → GPT REPAIR → Claude intervention → new candidate
                 → deterministic verification → GPT independent certification
```

without inferring it from ordering. No new canonical contract was introduced.

All new `OutcomeRecord` fields are defaulted, so records written before EW-0B
still load and still validate.

### Honest limits

- The execution identity attached to a review describes the **reviewer**, so
  `worker_id` and `authority_level` remain `UNAVAILABLE_AT_RECORD_TIME`. The
  loop's authority level is known to `run_task` but is not part of the reviewer's
  configuration, and asserting it there would attribute a property to the wrong
  execution.
- A verdict recovered after a crash (`VERDICT_ALREADY_RECORDED`) carries **no**
  freshly-derived identity. The configuration that produced it is the one in the
  journal, not the one running now; attaching the current identity would
  misattribute a decision to a configuration that never made it.
- `model_version` is normally `UNAVAILABLE_AT_RECORD_TIME`: chat APIs do not
  return the build actually served.

---

## Paired controls

Every safety assertion in `tests/test_ew0b_hardening.py` has a positive control.
A suite that only proves "X is blocked" passes just as well when everything is
blocked — including the work the loop exists to do. The positive control is what
distinguishes a gate from an outage.

For deterministic refusals the tests assert the strongest available property:
the independent reviewer is **never called**. A refusal that still spends a
reviewer call is a weaker refusal than one that never asks.

## Not in this phase

No G1 measurement (no false-PASS/false-FAIL rates, no supervisor scoring, no
graduation). No cost governance thresholds. No authority change — `A1`,
`C1: DISABLED`, all `auto_*` flags false. No capital, prediction, or production
semantics. No 0C product work.
