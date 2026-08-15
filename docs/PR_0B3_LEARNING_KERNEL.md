# PR draft — Northstar 0B.3 contracts + Engineering Learning Kernel

Paste the body below into https://github.com/pesantezq/v1/pull/new/feature/ew-0a-safe-operations
(base `main`, compare `feature/ew-0a-safe-operations`).

**Title**

```
Northstar 0B.3 decision/outcome/passport contracts + Engineering Learning Kernel
```

**Body**

---

## What this is

Two certified pieces of work, 32 commits on top of `main` (`0ef307a`).

### 1. Northstar 0B.3 — canonical decision/outcome/passport contracts

Six canonical contracts completing Phase 0B milestone 3, each individually VERIFIED
(deterministic gate + independent GPT verification), then certified as a graph:

| Contract | Prefix |
|---|---|
| ExperimentSpec / ExperimentResult | `exs_` / `exr_` |
| CapitalProposal | `cap_` |
| ExitProposal | `xit_` |
| OutcomeRecord | `out_` |
| StrategyPassport | `spp_` |

Final cross-contract GPT certification: **PASS** at `4d7aab68cb8be917f6509f99364048d3bbf46dc2`.

Separation invariants proven by `tests/test_northstar_0b3_cross_contract.py`:
a PredictionRecord cannot become a CapitalProposal; a CapitalProposal cannot mutate
its PredictionRecord or execute a capital action; an ExitProposal cannot execute an
exit; an ExperimentResult cannot rewrite its ExperimentSpec; an OutcomeRecord never
collapses attribution into one score; a StrategyPassport cannot grant itself
production or capital authority; no contract smuggles authority through arbitrary
payload keys.

Contracts only — nothing writes files, calls networks, or is wired into any
pipeline. The first consumer arrives with the Phase 0C EvidenceGateway.

### 2. Engineering Learning Kernel

Turns the learning demonstrated during 0B.3 into a permanent subsystem.

```
LEARNING MAY CHANGE FUTURE CONTEXT
LEARNING MAY NOT CHANGE AUTHORITY
```

Automatic lesson extraction, retrieval, outcome evaluation, per-capability
competence, and a graduation gate — wrapping the existing `ew0a_loop` with two
touchpoints rather than adding a second orchestration framework.
Independently certified **PASS** by GPT against the authority-critical invariants.

Authority is enforced technically, not by prompt: `WorkerLearningView` defines and
imports no mutator (AST-asserted), all mutation requires a trusted controller actor
from a protected config, learning state lives outside the worker's repair scope, and
`automatic_certification` / `automatic_authority_change` are pinned `False` on read
regardless of file content.

Graduation hard blockers (false certification, authority boundary violation, missed
E4 escalation, security boundary failure, unauthorized production action) are
absolute and evaluated before any statistic: **99 correct and 1 authority violation
is `NOT_READY`**, not "almost ready".

## Evidence

- **59 new tests** (56 learning + 3 eval-registry)
- **Broad suite: 10,638 passed / 15 failed.** The 15 failing node IDs are **identical**
  to a pristine `HEAD` worktree → `NEW_RELEVANT_FAILURES = 0`
- `FALSE_CERTIFICATIONS = 0`, `AUTHORITY_BOUNDARY_VIOLATIONS = 0`
- Bootstrap: 3 lessons activated through independent GPT consensus; 1 left CANDIDATE
  after failing 2-of-3 review and reported rather than forced through
- Shadow replay of the 5 real 0B.3 Engineer decisions: 4/4 lesson transfers,
  1 historical unsafe underclassification, readiness `LEARNING`

## Pre-existing failures (NOT introduced here)

These 15 fail identically on `4d7aab6` and on this branch:

```
tests/test_artifact_registry.py::test_sqg_registration_keeps_registry_green_and_debt_free
tests/test_broker_overlay.py::test_apply_overlay_to_config_object
tests/test_broker_overlay.py::test_apply_overlay_writes_source_artifact
tests/test_broker_overlay.py::test_apply_overlay_config_fallback_returns_unchanged
tests/test_broker_overlay.py::test_apply_overlay_records_config_source_on_fallback
tests/test_gui_dashboard_memo.py::test_memo_route_all_six_section_headings_present
tests/test_gui_dashboard_memo.py::test_memo_route_has_stacked_sections
tests/test_gui_dashboard_quant.py::test_quant_route_mobile_card_stack_present
tests/test_intraday_lab_session2.py::test_dates_outside_the_certified_window_are_uncertified
tests/test_intraday_lab_session2.py::test_calendar_provenance_discloses_coverage_and_backend
tests/test_intraday_lab_session2.py::test_manifest_records_calendar_provenance_and_limitations
tests/test_intraday_lab_session3_irregular.py::test_mwcb_prevalence_is_exact_and_needs_no_provider_calls
tests/test_intraday_lab_session3_irregular.py::test_sample_windows_are_deterministic_and_provider_compatible
tests/test_operator_worker_runner.py::test_safe_repair_uses_accept_edits_and_strips_api_key
tests/test_strategy_projection_anchor.py::test_projection_sets_anchor_strategy_id_when_selected
```

If CI is red on exactly these, that is the known baseline. **Any other failure is a
real regression and this should not merge.**

## Not included / known limitations

- **Live worker demonstration not achieved.** Ollama was not running, so no *new*
  Engineer proposal was obtained. The replay measures real historical behavior with
  new instrumentation; it does not prove the kernel changes behavior going forward.
- **`evals/certification/hidden` is deliberately empty.** A held-out set authored by
  the session that built the system is not held out.
- **C1 remains DISABLED.** No capability reached `READY_FOR_CERTIFICATION`; readiness
  grants no authority.
- Commits carry the email `pesantez.q@gmail.com.com` (a typo, doubled `.com`), so they
  will not link to the GitHub account. **Not corrected**, because rewriting history
  would change the certified candidate SHA and break the certification evidence chain.

## After merge

Record the certified-candidate → merged-main SHA mapping in
`.agent/phase_status.yaml` (`merged_main_sha`), then 0B.3 may move from
`certified_candidate_awaiting_durability` to complete. Next milestone is
**Northstar 0C — Point-in-Time EvidenceGateway & Research Store**.
