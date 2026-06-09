# Registry Consumer-Debt Burn-Down Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the opaque `UNATTRIBUTED` sentinel with a first-class `consumer_status` judgment, classify all ~54 registry rows, redefine the debt metric (unjustified vs justified), add two safety guards (validator-immutability + a conftest protected-file guard that kills the `signal_registry.yaml` test-mutation bug), and prove the wiring pattern on 1–2 high-value artifacts.

**Architecture:** Extends `portfolio_automation/artifact_registry.{py,yaml}` (from `feat/artifact-registry-governance`). The YAML gains a `consumer_status` field per row; the validator reports debt; new tests guard the invariants; a `tests/conftest.py` session fixture snapshots/restores the protected scoring registry and fails the session if any test mutates it.

**Tech Stack:** Python 3 stdlib + PyYAML, pytest (session-scoped autouse fixtures).

**Spec:** `docs/superpowers/specs/2026-06-08-registry-consumer-debt-burndown-design.md`

**Branch:** `feat/registry-consumer-debt-burndown` (created off `feat/artifact-registry-governance`; spec committed there as `822b3be0`). Rebase onto main when the parent registry PR merges.

### Discovery refinements over the spec (from reading the code)
1. **The protected-file mutator is precisely `tests/test_registry_apply.py::test_no_approval_file_is_inert`** — it calls `apply_approved_changes()` with all-default LIVE paths (`registry_path=config/signal_registry.yaml`, `approval_path=config/approved_weight_changes.json`). It's safe only while no approval file exists; a residual `config/approved_weight_changes.json` (left by another test/run) makes it apply a real weight change to the live registry. `test_tuning_proposals.py` is **read-only / innocent** (the spec named it wrongly). Fix targets `test_no_approval_file_is_inert` + a conftest guard that also covers `config/approved_weight_changes.json` and `config/history/`.
2. **33 of 54 rows already have real consumers** (many via `today.py`); only **21** are unattributed. Attributed rows all become `consumer_status: consumed`.

### Critical discipline
- **Never `git commit -am`.** Stage explicit paths; `git diff <base> HEAD --stat` before any push. (`<base>` = `feat/artifact-registry-governance`.)
- After running the FULL suite, the new conftest guard will auto-restore `config/signal_registry.yaml` — but still verify `git status` is clean before committing.
- Observe-only: debt never moves `overall_status`.

---

## File Structure

| File | Responsibility | Path |
|---|---|---|
| Registry contract | + `consumer_status` per row; `UNATTRIBUTED` removed | `portfolio_automation/artifact_registry.yaml` (modify) |
| Registry module | `CONSUMER_STATUSES`, schema_errors invariant, validate_registry debt fields | `portfolio_automation/artifact_registry.py` (modify) |
| Tests | consumer_status + debt + invariants + immutability | `tests/test_artifact_registry.py` (modify) |
| Protected-file guard | session fixture; snapshot/restore signal_registry + approval + history | `tests/conftest.py` (create) |
| Registry-apply test fix | make `test_no_approval_file_is_inert` hermetic | `tests/test_registry_apply.py` (modify) |
| Proof-wire consumer(s) | a skill that actually reads the chosen artifact(s) | `.claude/commands/*.md` (modify) |
| Daily skill | debt heartbeat; drop `unattributed` reference | `.claude/commands/daily-tool-analysis.md` (modify) |
| Docs | module doc + changelog + roadmap step | `docs/artifact_registry.md`, `docs/CHANGELOG_DECISIONS.md`, `.agent/project_state.yaml` (modify) |

---

## Task 1: `consumer_status` schema validation

**Files:**
- Modify: `portfolio_automation/artifact_registry.py`
- Test: `tests/test_artifact_registry.py`

- [ ] **Step 1: Write the failing test**

```python
def _row(**over):
    base = {"path": "outputs/latest/x.json", "label": "x", "lens": "developer",
            "role": "telemetry", "required": False, "cadence": "daily",
            "producer": "p", "consumers": ["daily-tool-analysis"],
            "severity_if_missing": "info", "consumer_status": "consumed"}
    base.update(over)
    return base


def test_schema_errors_flags_missing_consumer_status():
    reg = {"artifacts": {"a.json": _row(consumer_status=None)}, "daily_run_status_tracked": []}
    del reg["artifacts"]["a.json"]["consumer_status"]
    errs = ar.schema_errors(reg)
    assert any("consumer_status" in e for e in errs)


def test_schema_errors_flags_bad_consumer_status():
    reg = {"artifacts": {"a.json": _row(consumer_status="nope")}, "daily_run_status_tracked": []}
    assert any("consumer_status" in e for e in ar.schema_errors(reg))


def test_schema_errors_flags_consumed_with_empty_consumers():
    reg = {"artifacts": {"a.json": _row(consumer_status="consumed", consumers=[])},
           "daily_run_status_tracked": []}
    assert any("consumed" in e and "consumers" in e for e in ar.schema_errors(reg))


def test_schema_errors_allows_diagnostic_only_with_empty_consumers():
    reg = {"artifacts": {"a.json": _row(consumer_status="diagnostic_only", consumers=[])},
           "daily_run_status_tracked": []}
    assert ar.schema_errors(reg) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k consumer_status`
Expected: FAIL (consumer_status not validated; CONSUMER_STATUSES undefined).

- [ ] **Step 3: Implement**

In `portfolio_automation/artifact_registry.py`, add the enum near the other enums:
```python
CONSUMER_STATUSES = {"consumed", "diagnostic_only", "archive_only", "deprecated_candidate"}
```
Add `"consumer_status"` to `_REQUIRED_ROW_FIELDS`. In `schema_errors`, inside the per-row loop, add:
```python
        if row.get("consumer_status") not in CONSUMER_STATUSES:
            errs.append(f"{key}: bad consumer_status {row.get('consumer_status')!r}")
        if (row.get("consumer_status") == "consumed"
                and not (isinstance(row.get("consumers"), list) and row.get("consumers"))):
            errs.append(f"{key}: consumer_status 'consumed' requires non-empty consumers")
```
Also relax the existing consumers rule: a non-`consumed` row may have an empty `consumers` list. Change the existing `consumers must be a non-empty list` check to only require a list (empty allowed), since the `consumed`-specific check above now enforces non-emptiness where it matters:
```python
        if not isinstance(row.get("consumers"), list):
            errs.append(f"{key}: consumers must be a list")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k consumer_status`
Expected: PASS (4 passed). (The shipped-registry schema test will now FAIL until Task 2 adds consumer_status to every row — that's expected; Task 2 fixes it.)

- [ ] **Step 5: Commit**

```bash
python3 -m py_compile portfolio_automation/artifact_registry.py
git add portfolio_automation/artifact_registry.py tests/test_artifact_registry.py
git commit -m "feat(registry-debt): consumer_status enum + consumed-requires-consumers invariant"
```

---

## Task 2: Add `consumer_status` to all 54 rows + remove `UNATTRIBUTED`

**Files:**
- Modify: `portfolio_automation/artifact_registry.yaml`
- Test: `tests/test_artifact_registry.py`

- [ ] **Step 1: Classify every row**

For the **33 already-attributed rows** (consumers list is real, no UNATTRIBUTED): add `consumer_status: consumed`.

For the **21 currently-UNATTRIBUTED rows**: remove the `UNATTRIBUTED` sentinel (set `consumers: []`) and assign `consumer_status` by this rule — **deep-grep first**, then tag:
```bash
# for each artifact, search BEYOND v1's dirs (include today.py + all .py + monthly/yearly):
grep -rl "<filename>" .claude today.py portfolio_automation watchlist_scanner scanner gui_v2 2>/dev/null
```
If a real reader is found → `consumer_status: consumed` + put the reader in `consumers`.
Else assign per this provisional table (override only with grep evidence):

| artifact | provisional consumer_status | reason |
|---|---|---|
| decision_plan.md | diagnostic_only | operator-facing rendered view; no skill reads it |
| decision_triage.json | diagnostic_only (→consumed if today.py reads it) | verify grep |
| correlation_risk_advisor.json | **consumed** (Task 7 proof-wire) | risk lens |
| earnings_gate.json | diagnostic_only | gate detail, operator read |
| exit_advisor.json | diagnostic_only | advisory detail |
| cash_deployment_plan.json | diagnostic_only | advisory detail |
| tax_harvest_advisor.json | diagnostic_only | advisory detail |
| pattern_efficacy_weekly.json | **consumed** (Task 7 proof-wire) | quant trend |
| confidence_calibration.json | **consumed** (Task 7 proof-wire) | decision-confidence |
| alpha_attribution_report.json | diagnostic_only | quant diagnostic |
| kelly_sizing_advisor.json | diagnostic_only | sizing diagnostic |
| top100_weekly.json | archive_only | weekly universe snapshot |
| scraped_intel_comparison.json | diagnostic_only | discovery diagnostic |
| scraped_intel_run_summary.json | diagnostic_only (→consumed if discovery-health reads it) | verify grep |
| memo_delivery_status.json | diagnostic_only | delivery telemetry |
| data_quality_report.json | diagnostic_only | data diagnostic |
| ai_decision_validation.json | diagnostic_only | validation telemetry |
| daily_memo.txt | diagnostic_only | email copy |
| approved_ranking_config.json | archive_only | on-demand approved snapshot |
| approved_allocation_policy.json | archive_only | on-demand approved snapshot |
| theme_opportunities.json | archive_only | on-demand snapshot |

Only assign `deprecated_candidate` if a row has zero readers AND no diagnostic/archive justification (none expected in this set — prefer `diagnostic_only` for "intentional, low-value but live"). Aim: **0 unjustified debt**.

Wire the 3 marked **consumed** rows' `consumers` to the actual consumers added in Task 7 (do Task 7's grep-verifiable edits first if needed, or set them here and let Task 7's test confirm the reference exists).

- [ ] **Step 2: Update the shipped-registry schema test to assert 100% classified**

Add to `tests/test_artifact_registry.py`:
```python
def test_every_row_has_valid_consumer_status():
    reg = ar.load_registry()
    bad = {k: r.get("consumer_status") for k, r in reg["artifacts"].items()
           if r.get("consumer_status") not in ar.CONSUMER_STATUSES}
    assert bad == {}, f"rows missing/invalid consumer_status: {bad}"


def test_no_unattributed_sentinel_remains():
    reg = ar.load_registry()
    leftover = [k for k, r in reg["artifacts"].items()
                if "UNATTRIBUTED" in (r.get("consumers") or [])]
    assert leftover == [], f"UNATTRIBUTED sentinel still present: {leftover}"


def test_consumed_rows_have_real_consumers():
    reg = ar.load_registry()
    bad = [k for k, r in reg["artifacts"].items()
           if r.get("consumer_status") == "consumed"
           and not (isinstance(r.get("consumers"), list) and r.get("consumers"))]
    assert bad == [], f"consumed rows with empty consumers: {bad}"
```

- [ ] **Step 3: Run tests + iterate the YAML until green**

Run: `python3 -m pytest -q tests/test_artifact_registry.py`
Expected: all green (`test_shipped_registry_schema_valid`, the 3 new ones, and the Task-1 ones). Fix YAML rows until `schema_errors()` is empty and every row is classified.

- [ ] **Step 4: Commit**

```bash
python3 -c "from portfolio_automation import artifact_registry as a; print('schema_errors:', a.schema_errors(a.load_registry()))"  # expect []
git add portfolio_automation/artifact_registry.yaml tests/test_artifact_registry.py
git commit -m "feat(registry-debt): classify all 54 rows with consumer_status; remove UNATTRIBUTED sentinel"
```

---

## Task 3: `validate_registry` debt fields

**Files:**
- Modify: `portfolio_automation/artifact_registry.py`
- Test: `tests/test_artifact_registry.py`

- [ ] **Step 1: Write the failing test**

```python
def _debt_registry():
    def r(status, consumers, sev="info"):
        return {"path": f"outputs/latest/{status}.json", "label": status,
                "lens": "developer", "role": "telemetry", "required": False,
                "cadence": "daily", "producer": "p", "consumers": consumers,
                "severity_if_missing": sev, "consumer_status": status}
    return {"daily_run_status_tracked": [], "artifacts": {
        "consumed.json": r("consumed", ["daily-tool-analysis"]),
        "diagnostic_only.json": r("diagnostic_only", []),
        "archive_only.json": r("archive_only", []),
        "deprecated_candidate.json": r("deprecated_candidate", []),
    }}


def test_validate_reports_debt_fields(tmp_path):
    # make all four present + fresh so presence rules don't interfere
    import json as J
    for name in ["consumed", "diagnostic_only", "archive_only", "deprecated_candidate"]:
        p = tmp_path / "outputs/latest" / f"{name}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(J.dumps({"x": 1}), encoding="utf-8")
    st = ar.validate_registry(_debt_registry(), tmp_path, ar.datetime.now(ar.timezone.utc))
    assert st["classified"] == 4
    assert st["counts"]["total"] == 4
    assert set(st["unjustified_debt"]) == {"deprecated_candidate.json"}
    assert st["justified_no_consumer"] == 2  # diagnostic_only + archive_only
    assert st["by_consumer_status"] == {"consumed": 1, "diagnostic_only": 1,
                                        "archive_only": 1, "deprecated_candidate": 1}
    assert st["debt_target_met"] is False  # one deprecated_candidate


def test_validate_debt_does_not_change_overall_status(tmp_path):
    import json as J
    for name in ["consumed", "diagnostic_only", "archive_only", "deprecated_candidate"]:
        p = tmp_path / "outputs/latest" / f"{name}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(J.dumps({"x": 1}), encoding="utf-8")
    st = ar.validate_registry(_debt_registry(), tmp_path, ar.datetime.now(ar.timezone.utc))
    # all present+fresh, only info severity → debt must NOT make it red/amber
    assert st["overall_status"] == "green"


def test_validate_consumed_empty_is_unjustified(tmp_path):
    reg = _debt_registry()
    reg["artifacts"]["consumed.json"]["consumers"] = []  # invariant violation at runtime
    st = ar.validate_registry(reg, tmp_path, ar.datetime.now(ar.timezone.utc))
    assert "consumed.json" in st["unjustified_debt"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k "debt_fields or debt_does_not or consumed_empty"`
Expected: FAIL — new fields not present.

- [ ] **Step 3: Implement**

In `validate_registry`, after the per-row loop, compute the debt fields and add them to the
returned dict. Inside the loop (for each non-schema_invalid row) also tally consumer_status:
```python
    # near the top, before the loop:
    by_consumer_status: dict[str, int] = {}
    unjustified_debt: list[str] = []
    justified_no_consumer = 0
    classified = 0
```
Inside the loop, for a schema-valid row (after `_row_schema_ok` passes), add:
```python
        cs = row.get("consumer_status")
        if cs in CONSUMER_STATUSES:
            classified += 1
            by_consumer_status[cs] = by_consumer_status.get(cs, 0) + 1
            consumers = row.get("consumers") or []
            if cs == "deprecated_candidate" or (cs == "consumed" and not consumers):
                unjustified_debt.append(key)
            elif cs in ("diagnostic_only", "archive_only"):
                justified_no_consumer += 1
```
Then in the returned status dict, add (and REMOVE the old `unattributed` key):
```python
        "classified": classified,
        "unjustified_debt": unjustified_debt,
        "justified_no_consumer": justified_no_consumer,
        "by_consumer_status": by_consumer_status,
        "debt_target_met": (classified == len(arts) and not unjustified_debt),
```
Update `counts` to drop `unattributed` and add `"unjustified_debt": len(unjustified_debt)`. Update `operator_message` to mention `unjustified_debt` instead of `unattributed`. (Search the function + the degraded dict in `run_artifact_registry` for `unattributed` and replace consistently; the degraded dict should carry `unjustified_debt: []`, `classified: 0`, `by_consumer_status: {}`, `justified_no_consumer: 0`, `debt_target_met: False`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest -q tests/test_artifact_registry.py`
Expected: all green (the old validate tests that referenced `unattributed` must be updated — search the test file for `unattributed` and switch those assertions to `unjustified_debt`/`justified_no_consumer` as appropriate).

- [ ] **Step 5: Commit**

```bash
python3 -m py_compile portfolio_automation/artifact_registry.py
git add portfolio_automation/artifact_registry.py tests/test_artifact_registry.py
git commit -m "feat(registry-debt): validate_registry reports classified/unjustified_debt/by_consumer_status"
```

---

## Task 4: Live debt sanity + invariant on the shipped registry

**Files:**
- Test: `tests/test_artifact_registry.py`

- [ ] **Step 1: Write the test**

```python
def test_shipped_registry_meets_debt_target():
    # The shipped registry must be 100% classified with zero unjustified debt.
    from pathlib import Path
    st = ar.run_artifact_registry(root=".", write_files=False)
    assert st["classified"] == st["counts"]["total"], "not every row is classified"
    assert st["unjustified_debt"] == [], f"unjustified debt present: {st['unjustified_debt']}"
    assert st["debt_target_met"] is True
```

- [ ] **Step 2: Run it**

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k meets_debt_target`
Expected: PASS if Task 2 classified everything with no `deprecated_candidate`. If it FAILS, the failure message names the offending rows — fix their `consumer_status` in the YAML (assign `diagnostic_only`/`archive_only`, or `consumed` + a real consumer) until the target is met. Do NOT weaken the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_artifact_registry.py
git commit -m "test(registry-debt): shipped registry meets 100%-classified / 0-unjustified target"
```

---

## Task 5: Validator immutability guard

**Files:**
- Test: `tests/test_artifact_registry.py`

- [ ] **Step 1: Write the test**

```python
def test_run_does_not_mutate_the_registry_contract(tmp_path):
    import hashlib
    from pathlib import Path
    reg_path = Path(ar.DEFAULT_REGISTRY_PATH)
    before = hashlib.sha256(reg_path.read_bytes()).hexdigest()
    ar.run_artifact_registry(root=tmp_path, write_files=True)  # full run incl. status write
    after = hashlib.sha256(reg_path.read_bytes()).hexdigest()
    assert before == after, "run_artifact_registry must never modify artifact_registry.yaml"
```

- [ ] **Step 2: Run it**

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k does_not_mutate_the_registry_contract`
Expected: PASS (the validator only reads the YAML + writes the status artifact under tmp_path).

- [ ] **Step 3: Commit**

```bash
git add tests/test_artifact_registry.py
git commit -m "test(registry-debt): guard validator never mutates artifact_registry.yaml"
```

---

## Task 6: conftest protected-file guard + fix the registry-apply leak

**Files:**
- Create: `tests/conftest.py`
- Modify: `tests/test_registry_apply.py`

- [ ] **Step 1: Write the conftest guard**

Create `tests/conftest.py`:
```python
"""Session-scoped guard: the protected scoring registry and its approval/history
must NOT be mutated by the test suite. Snapshots them before the session and fails
loudly (and restores) if any test changed them — the canonical example being a test
that calls the apply path against the live config paths."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

_PROTECTED = [
    Path("config/signal_registry.yaml"),
    Path("config/approved_weight_changes.json"),
]
_HISTORY = Path("config/history")


def _hash(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


@pytest.fixture(scope="session", autouse=True)
def _protect_scoring_registry():
    before = {p: _hash(p) for p in _PROTECTED}
    before_snaps = set(_HISTORY.glob("signal_registry.*.yaml")) if _HISTORY.is_dir() else set()
    before_bytes = {p: (p.read_bytes() if before[p] is not None else None) for p in _PROTECTED}
    yield
    violations = []
    for p in _PROTECTED:
        after = _hash(p)
        if after != before[p]:
            violations.append(str(p))
            # restore byte-for-byte (or delete if it didn't exist before)
            if before_bytes[p] is None:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            else:
                p.write_bytes(before_bytes[p])
    # remove any history snapshots a test created
    if _HISTORY.is_dir():
        for snap in set(_HISTORY.glob("signal_registry.*.yaml")) - before_snaps:
            snap.unlink()
    assert not violations, (
        f"protected scoring registry mutated by the test suite: {violations} "
        f"(restored). A test applied to the live config paths — make it hermetic.")
```

- [ ] **Step 2: Confirm the guard CATCHES the current leak**

Run: `python3 -m pytest -q tests/test_registry_apply.py 2>&1 | tail -8`
Expected: the session FAILS at teardown with "protected scoring registry mutated …
config/signal_registry.yaml" (proving the guard works) AND the file is restored
afterwards. Verify restoration: `git status --short config/signal_registry.yaml` → clean.

- [ ] **Step 3: Fix the leak — make `test_no_approval_file_is_inert` hermetic**

The leak is `apply_approved_changes()` called with all-default LIVE paths. Replace the body
of `test_no_approval_file_is_inert` in `tests/test_registry_apply.py` so it exercises the
"no approval file → inert" behavior against TMP paths that can never touch the live registry:
```python
def test_no_approval_file_is_inert(tmp_path):
    # Inert when the approval file is absent — proven against a temp registry so the
    # live config/signal_registry.yaml can never be touched (it is the apply DEFAULT).
    reg = _temp_registry(tmp_path)
    before = reg.read_bytes()
    missing_approval = tmp_path / "approved_weight_changes.json"  # does not exist
    rep = apply_approved_changes(
        registry_path=str(reg), approval_path=str(missing_approval),
        history_dir=str(tmp_path / "history"), base_dir=str(tmp_path / "out"),
        now_iso=_NOW)
    assert rep["status"] == "no_approval_file"
    assert reg.read_bytes() == before, "with no approval file, the registry must be untouched"
```

- [ ] **Step 4: Confirm the guard now PASSES**

Run: `python3 -m pytest -q tests/test_registry_apply.py 2>&1 | tail -5`
Expected: all pass, no session-teardown violation.
Run: `git status --short config/signal_registry.yaml config/history` → clean (no residue).

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/test_registry_apply.py
git commit -m "fix(tests): conftest guard for protected signal_registry + hermetic no-approval test"
```

---

## Task 7: Proof-wire 1–2 high-value artifacts into real consumers

**Files:**
- Modify: `.claude/commands/daily-tool-analysis.md` and/or `.claude/commands/monthly-tool-analysis.md`
- Modify: `portfolio_automation/artifact_registry.yaml`
- Test: `tests/test_artifact_registry.py`

Pick TWO from {`confidence_calibration.json`, `correlation_risk_advisor.json`, `pattern_efficacy_weekly.json`}. Recommended: `confidence_calibration.json` (daily) + `pattern_efficacy_weekly.json` (monthly).

- [ ] **Step 1: Wire the consumers (real edits)**

In `.claude/commands/daily-tool-analysis.md`, add `confidence_calibration.json` to the Step-1
artifact reads with a one-line purpose (e.g. "→ calibration_slope, reliability bins; informs
decision-confidence narrative") and a Step-4 body mention. In
`.claude/commands/monthly-tool-analysis.md`, add a trend read of `pattern_efficacy_weekly.json`.
These must be genuine references (the filename appears in the skill text).

- [ ] **Step 2: Flip their registry rows to `consumed`**

In `artifact_registry.yaml`, set the two rows' `consumer_status: consumed` and
`consumers: [daily-tool-analysis]` / `consumers: [monthly-tool-analysis]` respectively.

- [ ] **Step 3: Write the proof test**

```python
import re as _re
from pathlib import Path as _Path

def test_proof_wired_artifacts_are_referenced_by_their_consumer():
    reg = ar.load_registry()
    for art in ("confidence_calibration.json", "pattern_efficacy_weekly.json"):
        row = reg["artifacts"][art]
        assert row["consumer_status"] == "consumed"
        assert row["consumers"], f"{art} consumed but no consumers listed"
        # every listed skill/agent consumer file must actually reference the artifact
        for c in row["consumers"]:
            hits = list(_Path(".claude").rglob(f"{c}.md"))
            assert hits, f"consumer file {c}.md not found for {art}"
            assert any(art in h.read_text(encoding='utf-8') for h in hits), \
                f"{c}.md does not reference {art}"
```

- [ ] **Step 4: Run it**

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k proof_wired`
Expected: PASS (the two artifacts are now genuinely referenced by their named consumer skills).

- [ ] **Step 5: Commit**

```bash
git add .claude/commands/daily-tool-analysis.md .claude/commands/monthly-tool-analysis.md portfolio_automation/artifact_registry.yaml tests/test_artifact_registry.py
git commit -m "feat(registry-debt): wire confidence_calibration + pattern_efficacy_weekly to real consumers (proof)"
```

---

## Task 8: Daily heartbeat — debt metric, drop `unattributed`

**Files:**
- Modify: `.claude/commands/daily-tool-analysis.md`

- [ ] **Step 1: Update the Step-1 read note + Step-2 gate + Step-4 Coverage line**

In `.claude/commands/daily-tool-analysis.md`:
- Step-1 item 0 (artifact_registry_status.json): change the field list from `... unattributed[] ...` to `... unjustified_debt[], justified_no_consumer, by_consumer_status, classified, debt_target_met ...`.
- Step-2 governance gate bullet: change "`unattributed` entries are debt, not failures … route to discovery-health" to "`unjustified_debt` entries route to `portfolio-discovery-health` (advisory, not RED); `justified_no_consumer` is acknowledged, not debt."
- Step-4 Coverage heartbeat: extend to
  `"Coverage: {present}/{total} present · {missing} missing ({missing_required} required) · {stale} stale · debt {unjustified_debt} (target 0) · classified {classified}/{total} · {overall_status}"`.

- [ ] **Step 2: Verify**

Run: `grep -n "unjustified_debt\|debt {\|by_consumer_status\|justified_no_consumer" .claude/commands/daily-tool-analysis.md`
Expected: hits in Step 1, Step 2, Step 4. Confirm no stray `unattributed` remains: `grep -n "unattributed" .claude/commands/daily-tool-analysis.md` → no output.

- [ ] **Step 3: Commit**

```bash
git add .claude/commands/daily-tool-analysis.md
git commit -m "feat(registry-debt): daily heartbeat surfaces unjustified-debt + classified metrics"
```

---

## Task 9: Docs + roadmap + full validation (STOP before push/PR)

**Files:**
- Modify: `docs/artifact_registry.md`, `docs/CHANGELOG_DECISIONS.md`, `.agent/project_state.yaml`

- [ ] **Step 1: Update `docs/artifact_registry.md`**

Add a "## Consumer status & debt" section:
```markdown
## Consumer status & debt
Every row carries `consumer_status`: `consumed` (≥1 real consumer; consumers list non-empty),
`diagnostic_only` (intentional operator/debug read, no analysis consumer), `archive_only`
(retained for history/retrospective), or `deprecated_candidate` (no consumer, no justification
— flagged for removal). `consumers` is the factual reader list (empty allowed); the old
`UNATTRIBUTED` sentinel is gone.

Debt = `deprecated_candidate` rows OR `consumed` rows with empty consumers (invariant
violation). `diagnostic_only`/`archive_only` are justified, not debt. The validator reports
`classified`, `unjustified_debt`, `justified_no_consumer`, `by_consumer_status`, and
`debt_target_met` (target: 100% classified AND zero unjustified). Debt is observe-only — it
never changes `overall_status`.
```

- [ ] **Step 2: CHANGELOG entry**

Add a dated (2026-06-08) entry to `docs/CHANGELOG_DECISIONS.md` (match format): shipped the
registry consumer-debt burn-down — `consumer_status` replaces UNATTRIBUTED; all 54 rows
classified (target 100%/0-unjustified met); validate_registry reports debt; validator-
immutability + conftest protected-file guard (fixed the `test_no_approval_file_is_inert` live-
registry leak); proof-wired confidence_calibration + pattern_efficacy_weekly. Reference spec + plan.

- [ ] **Step 3: Roadmap step**

Append to `completed_steps` in `.agent/project_state.yaml` (match comment style):
```yaml
  - registry_consumer_debt_burn_down  # 2026-06-08 — consumer_status field (consumed/diagnostic_only/archive_only/deprecated_candidate) replaces UNATTRIBUTED; all 54 rows classified (100%/0-unjustified target met); validate_registry reports classified/unjustified_debt/by_consumer_status/debt_target_met (observe-only, never moves overall_status); validator-immutability test + tests/conftest.py protected-file guard that fixed the test_no_approval_file_is_inert live signal_registry.yaml leak; proof-wired confidence_calibration + pattern_efficacy_weekly to real consumers. GPT-proposed, operator-approved. next_official_step unchanged (observe_and_iterate).
```
Leave `next_official_step.primary: observe_and_iterate` UNCHANGED.

- [ ] **Step 4: Full validation (report verbatim)**

```bash
cd /opt/stockbot
python3 -m pytest -q tests/test_artifact_registry.py
python3 -m py_compile portfolio_automation/artifact_registry.py
python3 -c "import json; from portfolio_automation.artifact_registry import run_artifact_registry; s=run_artifact_registry(root='.', write_files=False); print('overall', s['overall_status'], '| classified', s['classified'], '/', s['counts']['total'], '| unjustified_debt', s['unjustified_debt'], '| by_status', json.dumps(s['by_consumer_status']))"
python3 -m pytest -q   # FULL suite — the conftest guard must NOT trip now; report the result line + confirm git status clean afterward
git status --short      # MUST be clean (no config/signal_registry.yaml mutation residue)
```
Expected: targeted green; live read shows `debt_target_met`-consistent output (unjustified_debt `[]`); the full suite no longer leaves `config/signal_registry.yaml` dirty.

- [ ] **Step 5: Commit (do NOT push / open PR)**

```bash
git add docs/artifact_registry.md docs/CHANGELOG_DECISIONS.md .agent/project_state.yaml
git commit -m "docs(registry-debt): module doc + changelog + roadmap step"
git diff feat/artifact-registry-governance HEAD --stat   # CONFIRM only burn-down files changed
```

---

## Self-Review

**Spec coverage:**
- §3 schema (`consumer_status`, consumers empty-allowed, UNATTRIBUTED removed, consumed⇒non-empty) → Tasks 1,2 ✓
- §3 debt definition → Task 3 ✓
- §4 module changes (CONSUMER_STATUSES, schema_errors, validate_registry fields, degraded dict) → Tasks 1,3 ✓
- §5a validator immutability → Task 5 ✓
- §5b conftest guard + isolate the offending test → Task 6 (corrected to the real culprit: `test_no_approval_file_is_inert`) ✓
- §6 classify all rows → Task 2 ✓; 100%/0-unjustified target → Task 4 ✓
- §7 proof wires → Task 7 ✓
- §8 daily surfacing + drop unattributed → Task 8 ✓
- §9 observe-only/no-schema-break (daily_run_status untouched) → preserved (no task modifies it) ✓
- §10 test matrix (12 cases) → Tasks 1,2,3,4,5,6,7 ✓
- §11 health pairing → Task 8 ✓

**Placeholder scan:** Task 2's provisional classification table is explicitly provisional with a grep method + a deterministic target test (Task 4) that fails until it's right — not a placeholder. `deprecated_candidate` only if genuinely dead. No "TODO/handle-edge-cases".

**Type/name consistency:** `CONSUMER_STATUSES` (Task 1) used in Tasks 2,3,4; status-dict keys `classified`/`unjustified_debt`/`justified_no_consumer`/`by_consumer_status`/`debt_target_met` defined in Task 3, consumed by Tasks 4 and 8; `consumer_status` enum values consistent across YAML (Task 2), validator (Task 3), tests (Tasks 1–4,7). The old `unattributed` key is removed in Task 3 and its last consumer (daily skill) updated in Task 8 — no dangling reference.
