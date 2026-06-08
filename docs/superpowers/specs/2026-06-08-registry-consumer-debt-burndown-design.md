# Registry Consumer-Debt Burn-Down — Design Spec

**Date:** 2026-06-08
**Status:** Approved (design); pending implementation plan
**Author:** Claude Code (brainstormed with operator; proposed by GPT roadmap-control, operator-approved)
**Lens:** Developer / meta-governance
**Depends on:** `feat/artifact-registry-governance` (the registry + validator). This work branches from it and extends it; rebase onto main when the parent PR merges.
**Roadmap:** GPT-proposed → operator-approved governance step `registry_consumer_debt_burn_down`; `next_official_step.primary` stays `observe_and_iterate`.

---

## 1. Problem

The artifact registry's first live read surfaced **21 producer-without-consumer artifacts** — real work that exists but feeds no skill, agent, GUI, memo, or decision-confidence path. Today that debt is a single opaque count (`unattributed`) with no way to tell *justified* "no analysis consumer is fine" (diagnostic/archive artifacts) from *real* debt (dead producers, or `source_of_truth`-class artifacts that lost their reader). The registry can *measure* coverage but can't yet help *burn it down with intent*.

Separately, the registry hardening exposed two gaps the layer should close as part of becoming a safety surface:
- The validator has no test proving it leaves the **registry contract file itself** unmodified.
- Running the full test suite **mutates the protected `config/signal_registry.yaml`** (a test-isolation defect in `test_registry_apply` / `test_tuning_proposals`) — the exact protected-file class of bug the registry is meant to guard against.

## 2. Goals / Non-Goals

**Goals**
- Replace the opaque `UNATTRIBUTED` sentinel with a first-class `consumer_status` judgment, so debt is measurable and actionable.
- Redefine the debt metric: `unjustified_debt` (deprecated/dead) vs `justified_no_consumer` (diagnostic/archive).
- Classify **all** registry rows (100% classified; 0 unjustified is the target the validator reports against).
- Add two safety guards: validator-immutability of `artifact_registry.yaml`; a protected-file guard for `config/signal_registry.yaml` + isolate the two offending tests.
- Prove the wiring pattern by connecting 1–2 high-value artifacts to real consumers.

**Non-Goals (deferred follow-ups)**
- Wiring the *rest* of the high-value artifacts into consumers (bespoke per skill/agent).
- Actually deleting `deprecated_candidate` producers (touches the pipeline — separate careful change).
- GUI "System Coverage / debt" card (consumes this layer later).
- Changing decision/score/allocation behavior. Strictly observe-only; debt stays non-blocking.

## 3. Schema change

Each row in `portfolio_automation/artifact_registry.yaml` gains:

```yaml
consumer_status: consumed | diagnostic_only | archive_only | deprecated_candidate
```

- `consumers` becomes a **plain factual list** of who actually reads the artifact; **empty list is allowed**. The `UNATTRIBUTED` sentinel is **removed from every row**.
- Semantics:
  - `consumed` — has ≥1 real consumer (a skill/agent/GUI/pipeline reader). **Invariant:** `consumed ⟹ consumers non-empty`.
  - `diagnostic_only` — intentionally has no analysis consumer; exists for ad-hoc operator/debug reads. Justified.
  - `archive_only` — intentionally retained for history/retrospective mining (e.g. the ledger archive). Justified.
  - `deprecated_candidate` — no consumer and no justification; flagged for removal/consolidation. **Debt.**
- Every row MUST carry a `consumer_status` (no blank/default) — that's the "100% classified" target.

### Debt definition (validator)
```
unjustified_debt   = rows where consumer_status == deprecated_candidate
                     OR (consumer_status == consumed AND consumers is empty)   # invariant violation
justified_no_consumer = rows where consumer_status in {diagnostic_only, archive_only}
classified         = rows with a consumer_status in the enum
target             = classified == total  AND  len(unjustified_debt) == 0
```
Debt is **non-blocking**: it never moves `overall_status` (already the contract — `unattributed` never contributed to severity).

## 4. Module changes (`portfolio_automation/artifact_registry.py`)

- Add `CONSUMER_STATUSES = {"consumed", "diagnostic_only", "archive_only", "deprecated_candidate"}`.
- `schema_errors`:
  - Add `consumer_status` to the required-field + enum checks.
  - Change the `consumers` rule: must be a list (empty allowed); the `UNATTRIBUTED` sentinel is no longer special.
  - Enforce the invariant: `consumer_status == "consumed"` with empty/blank `consumers` → schema error.
- `validate_registry` status dict gains:
  - `classified` (int), `unjustified_debt` (list of keys), `justified_no_consumer` (int),
    `by_consumer_status` ({status: count}), and a `debt_target_met` (bool).
  - The old `unattributed` field is **cleanly replaced** by these (no alias). Its only consumer is `/daily-tool-analysis`, which this unit edits (§8), so there is nothing else to break.
- `overall_status` logic unchanged (debt never raises it).

## 5. Safety guards

### 5a. Validator immutability (artifact_registry.yaml)
A test runs `run_artifact_registry(root=tmp, write_files=True)` and asserts
`portfolio_automation/artifact_registry.yaml` is byte-identical before/after (content hash unchanged). Proves the validator never writes the contract file as the module grows.

### 5b. Protected-file guard (config/signal_registry.yaml) + test isolation
- `tests/conftest.py`: a session-scoped autouse fixture that records a hash of
  `config/signal_registry.yaml` at session start and, at session teardown, **fails the
  session if the hash changed** (loud, not silent). Also snapshots/restores
  `config/history/` additions so a stray run leaves no residue.
- Fix `tests/test_registry_apply.py` and `tests/test_tuning_proposals.py` to operate on a
  `tmp_path` copy of the registry (parameterize the registry path / monkeypatch the module
  constant), so they exercise the apply path without touching the real protected file.
- Net: the protected-file class of bug becomes impossible to reintroduce silently.

## 6. Classification of the current debt (~21 rows)

Re-attribute each currently-unattributed artifact by a **deeper grep** than v1 (include
`.claude/commands/monthly-tool-analysis.md`, `yearly-tool-analysis.md`, `*.py` consumers,
and `gui_v2`), then assign `consumer_status`. Provisional buckets (finalized in
implementation by actual grep evidence):

- **Likely `consumed` once deep-grepped:** `pattern_efficacy_weekly` (monthly/yearly skills),
  `scraped_intel_*` (discovery health), `data_quality_report`, `memo_delivery_status`,
  `ai_decision_validation` (pipeline/monitoring).
- **Likely `diagnostic_only`:** `alpha_attribution_report`, `kelly_sizing_advisor`,
  `decision_triage`, `top100_weekly`, `daily_memo.txt` (email copy).
- **Likely `archive_only`:** `approved_ranking_config`, `approved_allocation_policy`
  (on-demand approved snapshots), `theme_opportunities`.
- **Likely `deprecated_candidate` (verify before tagging):** any with zero readers AND no
  diagnostic/archive justification — e.g. an advisor superseded by a newer one.
- **Must NOT remain unjustified:** any `source_of_truth` row (none are currently unattributed;
  the invariant test enforces this going forward).

Every one of the ~54 rows (not just the 21) gets an explicit `consumer_status`.

## 7. Proof wires (1–2 high-value)

Wire 1–2 currently-debt artifacts into a real consumer to prove the pattern end-to-end.
Candidates (final pick at plan time):
- `confidence_calibration` → a daily-tool-analysis read that feeds decision-confidence
  (ties directly to the governance-gate philosophy).
- `correlation_risk_advisor` → a risk-lens read in the daily body.
- `pattern_efficacy_weekly` → an explicit monthly-tool-analysis trend read.

Each wired artifact flips to `consumer_status: consumed` with the real consumer named, and
the wiring is a genuine edit to the consuming skill (not a cosmetic registry change).

## 8. Surfacing (daily skill)

`/daily-tool-analysis` Coverage heartbeat (added by the parent feature) gains:
`· debt {unjustified_debt} (target 0) · classified {classified}/{total}`.
Non-empty `unjustified_debt` routes to `portfolio-discovery-health` (advisory, not RED).
Update the Step-1 artifact-read note for `artifact_registry_status.json` to mention the new
fields.

## 9. Observe-only / contract compliance
- All changes observe-only; debt never moves `overall_status`. No decision/score/allocation
  mutation. `artifact_registry_status.json` keeps `observe_only: true`.
- The two guards (5a/5b) only READ/snapshot; the conftest guard fails the test session on
  mutation but mutates nothing itself.
- No schema break to `daily_run_status` (untouched here; `required_artifacts()` ignores
  `consumer_status`).

## 10. Testing (`tests/test_artifact_registry.py` + `tests/conftest.py` + edits)
1. `schema_errors` flags a missing/invalid `consumer_status`.
2. `schema_errors` flags `consumed` + empty consumers (invariant).
3. `schema_errors` accepts `diagnostic_only`/`archive_only` with empty consumers.
4. Shipped registry: every row has a valid `consumer_status` (100% classified).
5. Shipped registry: every `consumed` row has a non-empty consumers list.
6. `validate_registry` reports `classified`, `unjustified_debt`, `justified_no_consumer`,
   `by_consumer_status`, `debt_target_met` correctly on a fixture with one of each bucket.
7. Debt does NOT change `overall_status` (a deprecated_candidate-only registry is still green/amber by presence rules, not red from debt).
8. Validator immutability: `artifact_registry.yaml` byte-unchanged after `run_artifact_registry(write_files=True)`.
9. conftest protected-file guard: a deliberate in-test mutation of a *tmp* copy is fine; the real file's hash is unchanged at session end (meta — assert the fixture exists and computes a hash).
10. `test_registry_apply` / `test_tuning_proposals` operate on tmp copies (assert the real `config/signal_registry.yaml` is untouched after they run).
11. Proof-wire: the chosen artifact's `consumer_status == consumed` AND its named consumer file actually references the artifact (grep assertion).
12. Existing `critical ⟺ source_of_truth` + golden-output tests still pass.

## 11. Analysis + Health pairing (CLAUDE.md)
- Cadence: daily → `/daily-tool-analysis` (Coverage line gains the debt metric).
- Lens: developer / meta-governance.
- The `unjustified_debt` list routes to `portfolio-discovery-health`; the burn-down target
  (`debt_target_met`) becomes a tracked monthly trend in `/monthly-tool-analysis` (note for
  follow-up; not required for v1 merge).

## 12. Risks & Mitigations
| Risk | Mitigation |
|---|---|
| Removing UNATTRIBUTED breaks the daily skill's read | Daily skill is edited in the same unit; no other consumer reads `unattributed` |
| Misclassifying a real consumer as debt | Deep-grep evidence required before `deprecated_candidate`; `diagnostic_only` is the safe default for "no consumer but intentional" |
| conftest guard flakiness | Hash compare is deterministic; guard only fails on actual content change |
| Isolating registry-apply tests changes their behavior | They already assert apply outcomes; redirecting the path preserves assertions, only the target file moves to tmp |
| Scope creep into full burn-down | Wiring limited to 1–2 proof artifacts; rest deferred (§2) |

## 13. Out of scope / follow-ups
- Wire remaining high-value artifacts; remove `deprecated_candidate` producers.
- GUI debt card; monthly debt-trend section.
- A filesystem-derived completeness test (every `outputs/latest/*.json` has a row) — still deferred from the parent spec.
