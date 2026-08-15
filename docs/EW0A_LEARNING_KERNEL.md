# Engineering Learning Kernel

Turns verified engineering experience into reusable, evidence-backed lessons.

```
LEARNING MAY CHANGE FUTURE CONTEXT
LEARNING MAY NOT CHANGE AUTHORITY
```

`experimental_noncanonical`. These are engineering-organization records — they are
**not** canonical Northstar investment contracts and are kept structurally separate
(different package, different schema era `engineering.learning.v0`, no import path
from `portfolio_automation/northstar/`).

## Pipeline

```
Task / Controller Decision
  → retrieve relevant lessons      (retriever.py    — structured, bounded, recorded)
  → Engineer proposal              (a CLAIM, never authority)
  → Claude authoritative decision
  → execution
  → deterministic verification     (ew0a.certify_attempt — unchanged)
  → GPT independent verification   (gpt_supervisor — unchanged)
  → verified outcome
  → Learning Evaluator             (evaluator.py)
  → lesson candidate               (extractor.py)
  → evidence validation            (validation.py — anti-poisoning)
  → active lesson                  (store.py — append-only)
  → competence update              (competence.py)
  → graduation readiness           (graduation.py — grants nothing)
  → future task retrieves lesson
  ↺
```

`ew0a_loop` still owns execution, routing and certification. The kernel adds exactly
two touchpoints — retrieval **before** a decision, evaluation **after** a verified
outcome — rather than a second orchestration framework.

## Modules

| Module | Responsibility |
|---|---|
| `contracts.py` | `EngineeringLessonV0`, `LessonRetrievalRecordV0`, `OutcomeEvaluationV0`, `TaskClassPerformanceV0`, `WorkerCompetenceProfileV0`, `CapabilityReadinessV0` |
| `config.py` | controller-owned policy at `config/ew0a_learning.json`; trusted-actor gate |
| `store.py` | append-only JSONL persistence; lifecycle transitions; evidence index |
| `extractor.py` | automatic extraction after meaningful outcomes |
| `validation.py` | anti-poisoning gate + independent semantic review + consensus vote |
| `retriever.py` | structured retrieval + retrieval records |
| `evaluator.py` | proposal vs authoritative vs verified outcome |
| `competence.py` | per-capability statistics |
| `graduation.py` | readiness gate + hard safety overrides |
| `binding.py` | SHA-bound verification evidence |
| `kernel.py` | the integrated cycle |
| `bootstrap.py` | import of the four already-proven lessons |
| `readmodels.py` | GUI projections (read-only) |
| `worker_view.py` | the Worker's read-only surface |

## Lesson lifecycle

```
CANDIDATE ──evidence validated──▶ ACTIVE ──▶ SUPERSEDED
                                     ├──▶ CONTRADICTED
                                     └──▶ RETIRED
```

Only `ACTIVE` lessons are retrievable. Transitions are **appended**, never rewritten;
`load_lessons` folds the log (last record wins) while `read_lesson_log` preserves the
full audit history. Illegal transitions (e.g. `CONTRADICTED → ACTIVE`) are refused, so
evidence history cannot be laundered.

The Worker may propose a lesson candidate. The Worker may **not** activate one.

## Anti-poisoning gate

In order, fail-closed, deterministic checks first (they short-circuit, so a poisoned
candidate never reaches or spends the independent reviewer):

1. evidence refs resolve against authoritative records;
2. the reported event actually occurred;
3. the correction is supported by an authoritative outcome;
4. the principle is not overgeneralized;
5. independent GPT semantic review returns PASS.

### Consensus review

An LLM judge is not deterministic — the same candidate returned `REPAIR` and then
`PASS` during this build. A single sample makes activation a coin flip and invites
re-running until the desired verdict appears, which is the validator-gaming the gate
exists to prevent. `consensus_reviewer` therefore takes a majority of independent
samples, applied uniformly to every candidate before any verdict is seen.

Transport failures are treated differently from verdicts, deliberately:
`SUPERVISOR_UNAVAILABLE` means the reviewer was never reached, so retrying asks the
question for the first time; retrying a `REPAIR` would re-roll an answer already
given. Any `ESCALATE` vote vetoes outright.

### Descriptive, not prescriptive

A lesson states what authoritative decisions **were**, not what routing **is
required**. Prescriptive wording reads as a lesson asserting authority, which
violates the core invariant. The independent reviewer caught exactly this in
bootstrap lesson A and it was reworded — the same discipline is now built into
`extractor._principle_for`.

## Retrieval

Structured, not vector. The match dimensions are the ones the authority model
already routes on, so retrieval is explainable and auditable — and retrieval is part
of the evidence chain, so an unexplainable ranking would be a real cost. Introduce
embeddings only when evidence shows structured retrieval is insufficient.

Relevance requires a **topic** match (capability or task_class). Matching only
subsystem/risk_domain is the classic false positive: sharing a location is not
sharing a lesson. Retrieval is capped (default 5) — an oversized packet is
functionally identical to no retrieval, because nothing in it is salient.

Every retrieval is recorded, **including empty ones**. Without that record,
"the lesson existed but was not retrieved" and "the lesson was retrieved and
ignored" are indistinguishable — and they demand opposite fixes.

## Competence

Per-capability only. There is deliberately **no** aggregate "worker intelligence"
score: a worker that routes routine tasks well may still be dangerous at security
escalation, and one number would hide exactly the signal that matters.

`consecutive_safe` resets to zero on any unsafe observation, so a long safe streak
cannot survive an authority violation.

## Graduation gate

```
readiness != certification
certification != automatic authority
```

States: `NOT_READY` · `LEARNING` · `CANDIDATE` · `READY_FOR_CERTIFICATION`.

**Hard blockers are absolute and evaluated before any statistic:**
`FALSE_CERTIFICATION`, `AUTHORITY_BOUNDARY_VIOLATION`, `MISSED_E4_ESCALATION`,
`SECURITY_BOUNDARY_FAILURE`, `UNAUTHORIZED_PRODUCTION_ACTION`. 99 correct decisions
and 1 authority violation is `NOT_READY` — not "almost ready".

High-risk capabilities (security escalation, capital governance escalation,
canonical contract routing, secret handling) carry stricter thresholds.

## Authority enforcement (technical, not prompt)

| Boundary | Mechanism |
|---|---|
| Worker cannot edit lessons / competence / thresholds | trusted-actor gate + protected paths |
| Worker cannot activate its own candidate | activation only via `store.transition_lesson` (controller) |
| Worker cannot reach a mutator | `WorkerLearningView` defines none and imports none (AST-tested) |
| Config cannot enable self-promotion | `automatic_certification` / `automatic_authority_change` pinned `False` on read |
| Empty trusted-actor list | falls back to defaults, never empty |

## SHA-bound verification

New verification records bind to `base_sha`, `candidate_sha`, `diff_hash`,
`evidence_manifest_hash`, both verdicts, verifier identity and timestamp.

Historical 0B.3 records are `LEGACY_CORROBORATED` — still valid evidence, explicitly
marked as weaker-bound, and **not** retroactively upgraded (that would fabricate
binding strength never measured). `binding_required_for_authority_expansion`
requires at least one strongly-bound record, which is why this is a prerequisite
before controller authority expands.

## Configuration

`config/ew0a_learning.json` — same mechanism and directory as `ew0a_authority.json`
and `ew0a_runtime.json` (no parallel config source), and a protected path.

## Operating

```bash
.venv/bin/python tools/ew0a_learning_bootstrap.py   # import proven lessons (live GPT)
.venv/bin/python tools/ew0a_learning_replay.py      # replay real decisions through the kernel
.venv/bin/python -m pytest tests/test_ew0a_learning.py tests/test_ew0a_evals_registry.py
```
