# Controller Evaluation Suite

The evaluation structure for future controller-authority certification.

## Design decision: a registry, not a second test framework

The repository already has a mature pytest suite (10,000+ tests, hermetic CI via
`.github/workflows/northstar-ci.yml`). Building a parallel `evals/` runner would
duplicate collection, fixtures, and CI wiring, and would let the two drift — the
classic failure where the "real" suite is green and the eval suite is stale.

So `evals/` is a **registry over real pytest node IDs**, not a second harness:

* `registry.json` maps each eval category to node IDs that already exist and run in CI.
* Running an eval category means running those node IDs. There is one source of truth.
* A node ID that disappears is a registry error, checked by
  `tests/test_ew0a_evals_registry.py` — so the registry cannot rot silently.

## Categories

```
regression/     one entry per REAL failure that has occurred
  authority/            authority boundary violations
  false_certification/  self-certification / unverified-as-verified
  stale_state/          stale roadmap or branch state
  reconciliation/       safe repository reconciliation
  lesson_poisoning/     fabricated or overgeneralized lessons
  retry/                bounded retry / repair limits
  escalation/           missed or incorrect escalation
  crash_recovery/       interrupted work never reported as success

capability/     per-capability competence evidence
  task_selection/ risk_routing/ executor_routing/ acceptance_criteria/
  verification_planning/ lesson_transfer/ safe_reconciliation/

certification/
  hidden/         held-out cases, NOT used during development
```

## The hidden set

`certification/hidden/` is deliberately empty in this commit. A held-out set that
was visible while the system was being built is not held out — it has already
leaked into the design. It must be authored by a separate certification mission,
from failures the Learning Kernel has not seen.

Recording this emptiness explicitly is the point: an eval suite that quietly ships
a "hidden" set built by the same session that built the system would overstate the
evidence available for authority expansion.

## Promotion rule

Every real failure is eligible to become a regression fixture. When a genuine
failure occurs:

1. add a test reproducing it to the appropriate `tests/` file;
2. register its node ID under the matching `evals/registry.json` category;
3. if it produced a transferable principle, it also becomes an `EngineeringLessonV0`.

Steps 1 and 3 are different artifacts answering different questions: the test asks
"does this specific bug recur?", the lesson asks "does the worker still make this
class of judgment error?". Both are needed; neither substitutes for the other.
