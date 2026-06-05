# Sub-project D — Feedback Proposers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** Two observe-only/proposes-only/owner-gated proposers (calibration correction + signal tagging) that turn the two live-run defects into bounded review artifacts under `outputs/policy/`, paired with monthly health checks. No scoring/registry/scanner mutation; apply stays OOS-gated.

**Architecture:** Pure producer modules `backtesting/calibration_proposer.py` + `backtesting/tagging_proposer.py`, each emitting a `{observe_only, proposed_only, ...}` dict + JSON/MD artifact via `safe_write_json/text(OutputNamespace.POLICY, ...)`, integrated non-blocking into `run_loop` after Step 4, surfaced by `backtest_health` + monthly skill.

**Tech Stack:** Python 3.12 stdlib + yaml; pytest. Interpreter `/opt/stockbot/.venv/bin/python`.

**Spec:** `docs/superpowers/specs/2026-06-05-pattern-loop-D-feedback-proposers-design.md`

---

### Task D1: calibration_correction_proposer

**Files:** Create `backtesting/calibration_proposer.py`; Test `tests/test_calibration_proposer.py`

- [ ] Test (inverted fixture → inverted:true, monotone suggested map, apply_gate oos_unconfirmed; well-calibrated → no proposal; thin band excluded; degraded → no raise).
- [ ] Run → fail (ImportError).
- [ ] Implement `propose_calibration_correction(results, *, min_band_n=20)` + `write_calibration_proposal(payload, base_dir="outputs")`.
- [ ] Run → pass; `py_compile`; commit.

`propose_calibration_correction` logic:
- `cal = results.get("calibration") or {}`; `buckets = cal.get("buckets") or []`; `slope = cal.get("calibration_slope")`.
- usable bands = buckets with `count >= min_band_n`; if `< 2` usable → `{status:"insufficient", inverted:False, bands:[], proposals_note:...}`.
- `inverted = (isinstance(slope,(int,float)) and slope < 0) or _spearman_decreasing(midpoints, hit_rates)`.
- monotone map: take usable bands in ascending band order, set `suggested = isotonic_nondecreasing([hit_rate/100 for each])` (simple pool-adjacent-violators).
- `oos = results.get("oos_window") or {}`; `apply_gate = "ready" if oos.get("folds_possible") else "oos_unconfirmed"`.
- return `{observe_only:True, proposed_only:True, advisory_only:True, generated_by:"backtesting.calibration_proposer", status:"ok", calibration_slope:slope, inverted, bands:[{band,n,empirical_hit_rate,suggested_calibrated_conf}], apply_gate, rationale}`.
- Wrap body in try/except → `{observe_only:True, status:"degraded", error:str(e)}`.

`write_calibration_proposal`: `safe_write_json(OutputNamespace.POLICY, "calibration_correction_proposal.json", payload, base_dir)` + `safe_write_text(... ".md", _md(payload) ...)`; return json Path.

### Task D2: signal_tagging_proposer

**Files:** Create `backtesting/tagging_proposer.py`; Test `tests/test_tagging_proposer.py`

- [ ] Test (60% empty alert_basis → untagged_pct≈0.6 + backfill proposal; SIGNAL_SCORE absent from registry → families_missing_registry_id + registry-entry proposal; fully-tagged → no proposals; empty → no raise).
- [ ] Run → fail.
- [ ] Implement `propose_tagging_fixes(signals, *, registry_path="config/signal_registry.yaml")` + `write_tagging_proposal(payload, base_dir="outputs")`.
- [ ] Run → pass; `py_compile`; commit.

`propose_tagging_fixes` logic (reuse `signal_sources._map_basis` + a local registry-id reader mirroring `tuning_proposals._load_registry_weights`):
- `total=len(signals)`; `untagged=sum(1 for s in signals if not isinstance(s.get("alert_basis"),(list,tuple)) or not s.get("alert_basis"))`.
- family_distribution: Counter over `_map_basis(s.get("alert_basis"))` representative families.
- registry_ids = set of registry signal_ids; STRONG_MOVE maps to UP/DOWN — treat family covered if any `signal_id` startswith family. `families_missing_registry_id` = mapped families (excl. UNKNOWN) with no covering registry id (→ SIGNAL_SCORE).
- proposals: for each missing family → `{kind:"registry_entry", signal_id:family, suggested_default_weight:0.0, rationale}`; if `untagged/total >= 0.10` → `{kind:"backfill_inference", rule:"alert_basis empty → infer ['signal_score'] when signal_score present; ['volume_spike'] when volume_ratio>=2", would_tag:<count with signal_score present>, rationale}`.
- return `{observe_only:True, proposed_only:True, advisory_only:True, generated_by:"backtesting.tagging_proposer", status:"ok", total, untagged_count, untagged_pct:round(untagged/total,4) if total else 0.0, family_distribution, families_missing_registry_id, proposals, rationale}`; try/except → degraded.

### Task D3: run_loop non-blocking integration

**Files:** Modify `backtesting/run_loop.py`; Test extend `tests/test_run_loop.py`

- [ ] Test: extend `TestRunLoopOosWindow` to assert `out` has `calibration_proposal` and `tagging_proposal` keys.
- [ ] Run → fail.
- [ ] Implement: import the two proposers; after `write_proposals(...)`, inside the existing try, add a nested non-blocking block:
```python
        cal_prop = tag_prop = None
        try:
            from backtesting.calibration_proposer import propose_calibration_correction, write_calibration_proposal
            from backtesting.tagging_proposer import propose_tagging_fixes, write_tagging_proposal
            cal_prop = propose_calibration_correction(poc)
            tag_prop = propose_tagging_fixes(signals, registry_path=registry_path)
            if write:
                write_calibration_proposal(cal_prop, base_dir=base_dir)
                write_tagging_proposal(tag_prop, base_dir=base_dir)
        except Exception:  # non-blocking feedback layer; never break the loop
            pass
```
  and add to the returned ok dict: `"calibration_proposal": cal_prop, "tagging_proposal": tag_prop,`.
- [ ] Run → pass; `py_compile`; commit.

### Task D4: backtest_health flags + monthly skill

**Files:** Modify `backtesting/backtest_health.py`, `.claude/commands/monthly-tool-analysis.md`; Test extend `tests/test_backtest_health.py`

- [ ] Test: degraded fixtures (calibration proposal with inverted:true; tagging proposal untagged_pct 0.6) → AMBER flags `calibration_correction_available`, `high_untagged_rate`; healthy → absent.
- [ ] Run → fail.
- [ ] Implement in `assess_backtest_health`: add params `calibration_proposal_path="outputs/policy/calibration_correction_proposal.json"`, `tagging_proposal_path="outputs/policy/signal_tagging_proposal.json"`; `_load_json` each; if calibration `inverted` true → `amber.append("calibration_correction_available")` + `details["calibration_inverted"]=True`; if tagging `untagged_pct >= 0.50` → `amber.append("high_untagged_rate")` + `details["untagged_pct"]`. Absent files tolerated.
- [ ] Monthly skill: add the 2 artifacts to artifacts-read (items 14,15), a Quant/Developer body line, dispatch note.
- [ ] Run → pass; `py_compile`; commit.

### Task D5: docs + full suite

- [ ] CHANGELOG_DECISIONS entry (area: evaluation; observe-only; apply OOS-gated; no scoring change).
- [ ] `.agent/project_state.yaml` one-line note (next_official_step unchanged).
- [ ] `/opt/stockbot/.venv/bin/python -m pytest -q` → all pass.
- [ ] Commit.

## Self-Review
- Spec D1→T1, D2→T2, integration→T3, health+skill→T4, docs→T5. Every new module tested. Apply stays OOS-gated/owner-gated; POLICY namespace. Names: `propose_calibration_correction`/`write_calibration_proposal`, `propose_tagging_fixes`/`write_tagging_proposal`, artifact keys `calibration_proposal`/`tagging_proposal`, flags `calibration_correction_available`/`high_untagged_rate` — consistent across producer, integration, health, tests.

## Production boundary (operator go-ahead)
Merge to main + activate. Same as Foundation; D is observe-only so activation is low-risk, but still gated per the working contract.
