# Quant Watch Probes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-managing ledger of sub-RED quant concerns ("watch probes") that auto-registers when a known condition fires, re-checks each run, and auto-archives on resolution — surfaced through the daily skill.

**Architecture:** A pure-function module (`portfolio_automation/quant_watch_probes.py`) holds 3 deterministic detectors + paired evaluators; a JSON ledger (`data/quant_watch_ledger.json`) holds active + archived probes; an observe-only status artifact (`outputs/latest/quant_watch_status.json`) is the consumer snapshot. A new `/quant-watch-analysis` skill drives the module each run (no production-pipeline changes); `daily-tool-analysis` delegates to it and folds one heartbeat line.

**Tech Stack:** Python 3 (stdlib only: `json`, `pathlib`, `datetime`), pytest. Reuses `portfolio_automation.data_governance.safe_write_json` + `OutputNamespace.LATEST`.

**Spec:** `docs/superpowers/specs/2026-06-08-quant-watch-probes-design.md`

**Branch:** `feat/quant-watch-probes` (already created; spec already committed there).

**Design refinement vs spec:** the spec's `resolve_when`/`escalate_when` machine-spec fields are replaced by **detector-paired evaluator functions** (`_EVALUATORS[detector_id]`). Reason: D1's resolution metric (`delta_vs_prior_pp`) is *computed*, not a raw artifact field, so a generic JSON-path spec can't express it. Each detector and its evaluator recompute from the same source, guaranteeing consistency. Probes still carry a human `resolve_hint` string for the audit trail. Intent (machine-checkable auto-retire) is preserved.

---

## File Structure

| File | Responsibility |
|---|---|
| `portfolio_automation/quant_watch_probes.py` (create) | Constants, JSON helpers, prior-gauge selection, 3 detectors, 3 paired evaluators, `detect`/`evaluate`/`update_ledger`/`render_status`/`overall_status`, `load_ledger`/`run_quant_watch` |
| `data/quant_watch_ledger.json` (created at runtime) | `{schema_version, active:[], archive:[]}` |
| `outputs/latest/quant_watch_status.json` (created at runtime) | observe-only heartbeat snapshot |
| `.claude/commands/quant-watch-analysis.md` (create) | The skill: Steps 1–5 |
| `.claude/commands/daily-tool-analysis.md` (modify) | Step 1 artifact-read entry + sub-check delegate + Step 4 body line |
| `docs/quant_watch_probes.md` (create) | Module documentation (closes a doc-coverage gap proactively) |
| `tests/test_quant_watch_probes.py` (create) | Unit + integration tests |

---

## Task 1: Module scaffold — constants, JSON helpers, ledger I/O

**Files:**
- Create: `portfolio_automation/quant_watch_probes.py`
- Test: `tests/test_quant_watch_probes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_quant_watch_probes.py
import json
from pathlib import Path

from portfolio_automation import quant_watch_probes as qwp


def test_empty_ledger_shape():
    led = qwp._empty_ledger()
    assert led == {"schema_version": "1", "active": [], "archive": []}


def test_load_ledger_missing_returns_empty(tmp_path):
    led = qwp.load_ledger(tmp_path / "nope.json")
    assert led == qwp._empty_ledger()


def test_load_ledger_corrupt_resets_to_empty(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text("{not valid json", encoding="utf-8")
    led = qwp.load_ledger(p)
    assert led == qwp._empty_ledger()


def test_load_ledger_backfills_missing_keys(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"active": [{"id": "x"}]}), encoding="utf-8")
    led = qwp.load_ledger(p)
    assert led["schema_version"] == "1"
    assert led["active"] == [{"id": "x"}]
    assert led["archive"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q`
Expected: FAIL — `ModuleNotFoundError`/`AttributeError` (module/functions not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# portfolio_automation/quant_watch_probes.py
"""quant_watch_probes — observe-only ledger of sub-RED quant concerns.

Auto-registers a "watch probe" when a deterministic quant condition fires below
the daily-tool-analysis RED trip-wires, re-checks each open probe every run, and
auto-archives it on resolution / scope-change / escalation. Companion to
applied_fix_verifier (which tracks applied fixes); this tracks open concerns.

Observe-only: mutates ONLY its ledger (data/quant_watch_ledger.json) and its
status artifact (outputs/latest/quant_watch_status.json). Never touches
decision / score / allocation / portfolio state. See
docs/superpowers/specs/2026-06-08-quant-watch-probes-design.md.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json

# ── status levels ───────────────────────────────────────────────────────────
GREEN, AMBER, RED = "green", "amber", "red"

# ── transition statuses ─────────────────────────────────────────────────────
ACTIVE, RESOLVED, ESCALATED = "active", "resolved", "escalated"

# ── detector ids ────────────────────────────────────────────────────────────
DETECTOR_PRIOR_GAUGE = "prior_gauge_underperformance"
DETECTOR_NEG_RETURN = "negative_mean_return_persistence"
DETECTOR_SECTOR_DRAG = "sector_drag"
DETECTOR_MANUAL = "manual"

# ── thresholds (module constants; config-overridable later) ─────────────────
MIN_RESOLVED_1D = 30           # min resolved sample before a probe may fire
PRIOR_GAUGE_FIRE_PP = -10.0    # fire D1 when current-fp <= prior gauge by this pp
PRIOR_GAUGE_RESOLVE_PP = -2.0  # resolve D1 when delta recovers to >= this pp
PRETRACKER_RED_GATE_PP = 10.0  # daily RED gate (|delta vs pre_tracker| >= this)
SECTOR_MIN_N = 30              # min n_samples for a sector:* loser to fire D3
MAX_PROBE_AGE_DAYS = 60        # TTL: stale probe auto-expires
MAX_OBSERVATIONS = 14          # cap per-probe observation trail
MAX_ARCHIVE = 200             # cap archive length (FIFO roll-off)

_LEDGER_REL = "data/quant_watch_ledger.json"
_STATUS_REL = "quant_watch_status.json"  # under outputs/latest/


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_ledger() -> dict:
    return {"schema_version": "1", "active": [], "archive": []}


def load_ledger(path: str | Path) -> dict:
    """Load the ledger; return an empty default if missing or corrupt.
    Backfills missing top-level keys so callers can rely on the shape."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
        if not isinstance(data, dict):
            return _empty_ledger()
        data.setdefault("schema_version", "1")
        data.setdefault("active", [])
        data.setdefault("archive", [])
        if not isinstance(data["active"], list) or not isinstance(data["archive"], list):
            return _empty_ledger()
        return data
    except FileNotFoundError:
        return _empty_ledger()
    except Exception:
        return _empty_ledger()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Compile + commit**

```bash
python3 -m py_compile portfolio_automation/quant_watch_probes.py
git add portfolio_automation/quant_watch_probes.py tests/test_quant_watch_probes.py
git commit -m "feat(quant-watch): module scaffold — constants + ledger I/O"
```

---

## Task 2: Prior-gauge selection helper

**Files:**
- Modify: `portfolio_automation/quant_watch_probes.py`
- Test: `tests/test_quant_watch_probes.py`

- [ ] **Step 1: Write the failing test**

```python
def test_select_prior_gauge_picks_latest_non_current_non_pretracker():
    by_fp = {
        "CUR": {"last_signal_time": "2026-06-08T09:00:00", "hit_rate_1d": 0.45},
        "OLDGAUGE": {"last_signal_time": "2026-05-29T09:00:00", "hit_rate_1d": 0.69},
        "OLDERGAUGE": {"last_signal_time": "2026-05-20T09:00:00", "hit_rate_1d": 0.55},
        "pre_tracker_unknown": {"last_signal_time": "2026-05-19T01:00:00", "hit_rate_1d": 0.40},
    }
    fp, entry = qwp._select_prior_gauge(by_fp, "CUR")
    assert fp == "OLDGAUGE"
    assert entry["hit_rate_1d"] == 0.69


def test_select_prior_gauge_none_when_only_current_and_pretracker():
    by_fp = {
        "CUR": {"last_signal_time": "2026-06-08T09:00:00"},
        "pre_tracker_unknown": {"last_signal_time": "2026-05-19T01:00:00"},
    }
    fp, entry = qwp._select_prior_gauge(by_fp, "CUR")
    assert fp is None and entry is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k select_prior_gauge`
Expected: FAIL — `AttributeError: module ... has no attribute '_select_prior_gauge'`.

- [ ] **Step 3: Write minimal implementation** (append to module)

```python
def _select_prior_gauge(
    by_fp: dict, current_fp: str | None,
    pretracker_label: str = "pre_tracker_unknown",
) -> tuple[str | None, dict | None]:
    """Return (fp, entry) of the gauge era immediately preceding the current
    one: the by_fingerprint entry that is neither current nor pre_tracker, with
    the latest last_signal_time. (None, None) if no such entry."""
    candidates = [
        (k, v) for k, v in (by_fp or {}).items()
        if k not in (current_fp, pretracker_label) and isinstance(v, dict)
    ]
    if not candidates:
        return None, None
    fp, entry = max(candidates, key=lambda kv: kv[1].get("last_signal_time") or "")
    return fp, entry
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k select_prior_gauge`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/quant_watch_probes.py tests/test_quant_watch_probes.py
git commit -m "feat(quant-watch): prior-gauge selection helper"
```

---

## Task 3: Transition builders + age helper

**Files:**
- Modify: `portfolio_automation/quant_watch_probes.py`
- Test: `tests/test_quant_watch_probes.py`

These tiny constructors are shared by every evaluator (Tasks 5–7), so define them once.

- [ ] **Step 1: Write the failing test**

```python
def test_transition_builders_shape():
    probe = {"id": "d:scope"}
    now = "2026-06-08T09:00:00+00:00"
    a = qwp._active(probe, "still bad", now, {"run": "2026-06-08", "v": 1})
    assert a == {"id": "d:scope", "status": "active", "detail": "still bad",
                 "observation": {"run": "2026-06-08", "v": 1}}
    r = qwp._resolved(probe, "recovered", "delta +1pp", now)
    assert r["status"] == "resolved" and r["resolution"] == "recovered"
    assert r["resolved_at"] == now
    e = qwp._escalated(probe, "crossed gate", now)
    assert e["status"] == "escalated" and e["resolution"] == "escalated_to_red"


def test_age_days():
    assert qwp._age_days("2026-06-01T00:00:00+00:00", "2026-06-08T00:00:00+00:00") == 7
    assert qwp._age_days(None, "2026-06-08T00:00:00+00:00") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k "transition_builders or age_days"`
Expected: FAIL — attributes not defined.

- [ ] **Step 3: Write minimal implementation** (append)

```python
def _active(probe: dict, detail: str, now_iso: str, observation: dict | None) -> dict:
    return {"id": probe.get("id"), "status": ACTIVE, "detail": detail,
            "observation": observation}


def _resolved(probe: dict, resolution: str, detail: str, now_iso: str) -> dict:
    return {"id": probe.get("id"), "status": RESOLVED, "resolution": resolution,
            "detail": detail, "resolved_at": now_iso, "observation": None}


def _escalated(probe: dict, detail: str, now_iso: str) -> dict:
    return {"id": probe.get("id"), "status": ESCALATED, "resolution": "escalated_to_red",
            "detail": detail, "resolved_at": now_iso, "observation": None}


def _age_days(created_at: str | None, now_iso: str) -> int:
    if not created_at:
        return 0
    try:
        c = datetime.fromisoformat(created_at)
        n = datetime.fromisoformat(now_iso)
        return (n - c).days
    except Exception:
        return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k "transition_builders or age_days"`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/quant_watch_probes.py tests/test_quant_watch_probes.py
git commit -m "feat(quant-watch): transition builders + age helper"
```

---

## Task 4: Detector D1 + evaluator — prior_gauge_underperformance (flagship)

**Files:**
- Modify: `portfolio_automation/quant_watch_probes.py`
- Test: `tests/test_quant_watch_probes.py`

- [ ] **Step 1: Write the failing test**

```python
def _retune_fixture(cur_hr=0.4489, prior_hr=0.6894, pre_hr=0.4062,
                    resolved=176, mean_ret=-1.18, current_fp="d95e"):
    return {
        "current_fingerprint": current_fp,
        "outcome_attribution": {
            "pre_tracker_label": "pre_tracker_unknown",
            "by_fingerprint": {
                current_fp: {"resolved_1d": resolved, "hit_rate_1d": cur_hr,
                             "mean_return_1d": mean_ret,
                             "last_signal_time": "2026-06-08T09:00:00"},
                "f60e": {"resolved_1d": 264, "hit_rate_1d": prior_hr,
                         "last_signal_time": "2026-05-29T09:00:00"},
                "pre_tracker_unknown": {"resolved_1d": 352, "hit_rate_1d": pre_hr,
                                        "last_signal_time": "2026-05-19T01:00:00"},
            },
        },
    }


def test_d1_fires_on_prior_gauge_underperformance():
    probe = qwp.detect_prior_gauge_underperformance(
        _retune_fixture(), "2026-06-08T09:00:00+00:00", "test-run")
    assert probe is not None
    assert probe["id"] == "prior_gauge_underperformance:d95e"
    assert probe["detector"] == qwp.DETECTOR_PRIOR_GAUGE
    assert probe["scope_key"] == "d95e"
    assert probe["lens"] == "quant"
    assert "vs prior gauge" in probe["concern"]
    assert probe["trigger_snapshot"]["delta_vs_prior_pp"] == -24.1


def test_d1_quiet_when_within_resolve_band():
    # current 0.68 vs prior 0.69 → delta -1pp, above the -10 fire gate
    probe = qwp.detect_prior_gauge_underperformance(
        _retune_fixture(cur_hr=0.68), "2026-06-08T09:00:00+00:00", "r")
    assert probe is None


def test_d1_quiet_when_daily_red_would_own_it():
    # delta vs pre_tracker is large (|0.30-0.55|=25pp >= 10) → daily RED owns it
    probe = qwp.detect_prior_gauge_underperformance(
        _retune_fixture(cur_hr=0.30, pre_hr=0.55), "2026-06-08T09:00:00+00:00", "r")
    assert probe is None


def test_d1_quiet_below_min_sample():
    probe = qwp.detect_prior_gauge_underperformance(
        _retune_fixture(resolved=10), "2026-06-08T09:00:00+00:00", "r")
    assert probe is None


def test_d1_eval_resolves_on_scope_change():
    probe = qwp.detect_prior_gauge_underperformance(
        _retune_fixture(), "2026-06-08T09:00:00+00:00", "r")
    # current fingerprint is now something else
    t = qwp._eval_prior_gauge(probe, _retune_fixture(current_fp="NEWFP"),
                              None, "NEWFP", "2026-06-20T09:00:00+00:00")
    assert t["status"] == "resolved" and t["resolution"] == "scope_changed"


def test_d1_eval_resolves_on_recovery():
    probe = qwp.detect_prior_gauge_underperformance(
        _retune_fixture(), "2026-06-08T09:00:00+00:00", "r")
    recovered = _retune_fixture(cur_hr=0.68)  # delta vs prior -1pp >= -2
    t = qwp._eval_prior_gauge(probe, recovered, None, "d95e",
                              "2026-06-20T09:00:00+00:00")
    assert t["status"] == "resolved" and t["resolution"] == "recovered"


def test_d1_eval_escalates_when_crosses_daily_red_gate():
    probe = qwp.detect_prior_gauge_underperformance(
        _retune_fixture(), "2026-06-08T09:00:00+00:00", "r")
    worse = _retune_fixture(cur_hr=0.30, pre_hr=0.55)  # |delta vs pre|=25pp
    t = qwp._eval_prior_gauge(probe, worse, None, "d95e",
                              "2026-06-20T09:00:00+00:00")
    assert t["status"] == "escalated"


def test_d1_eval_stays_active_when_still_bad():
    probe = qwp.detect_prior_gauge_underperformance(
        _retune_fixture(), "2026-06-08T09:00:00+00:00", "r")
    t = qwp._eval_prior_gauge(probe, _retune_fixture(), None, "d95e",
                              "2026-06-09T09:00:00+00:00")
    assert t["status"] == "active"
    assert t["observation"]["delta_vs_prior_pp"] == -24.1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k d1`
Expected: FAIL — `detect_prior_gauge_underperformance` / `_eval_prior_gauge` not defined.

- [ ] **Step 3: Write minimal implementation** (append)

```python
def _pre_tracker_entry(retune: dict) -> dict:
    attr = (retune or {}).get("outcome_attribution") or {}
    by_fp = attr.get("by_fingerprint") or {}
    return by_fp.get(attr.get("pre_tracker_label") or "pre_tracker_unknown") or {}


def detect_prior_gauge_underperformance(
    retune: dict, now_iso: str, created_run: str,
) -> dict | None:
    attr = (retune or {}).get("outcome_attribution") or {}
    by_fp = attr.get("by_fingerprint") or {}
    current_fp = (retune or {}).get("current_fingerprint")
    cur = by_fp.get(current_fp) if current_fp else None
    if not isinstance(cur, dict):
        return None
    resolved = cur.get("resolved_1d") or 0
    if resolved < MIN_RESOLVED_1D:
        return None
    prior_fp, prior = _select_prior_gauge(by_fp, current_fp)
    if not prior:
        return None
    cur_hr, prior_hr = cur.get("hit_rate_1d"), prior.get("hit_rate_1d")
    if cur_hr is None or prior_hr is None:
        return None
    delta_prior = round((cur_hr - prior_hr) * 100, 1)
    if delta_prior > PRIOR_GAUGE_FIRE_PP:
        return None
    pre_hr = _pre_tracker_entry(retune).get("hit_rate_1d")
    delta_pre = round((cur_hr - pre_hr) * 100, 1) if pre_hr is not None else None
    if delta_pre is not None and abs(delta_pre) >= PRETRACKER_RED_GATE_PP:
        return None  # daily RED owns it — not our band
    return {
        "id": f"{DETECTOR_PRIOR_GAUGE}:{current_fp}",
        "detector": DETECTOR_PRIOR_GAUGE,
        "lens": "quant",
        "scope_key": current_fp,
        "created_at": now_iso,
        "created_run": created_run,
        "severity": "amber",
        "concern": (
            f"current-fp {current_fp[:8]} {delta_prior:+.1f}pp vs prior gauge "
            f"{prior_fp[:8]} at n={resolved}, mean_return_1d "
            f"{cur.get('mean_return_1d', 0):.2f}"
        ),
        "trigger_snapshot": {
            "current_hit_rate_1d": cur_hr, "prior_hit_rate_1d": prior_hr,
            "delta_vs_prior_pp": delta_prior, "delta_vs_pretracker_pp": delta_pre,
            "resolved_1d": resolved, "mean_return_1d": cur.get("mean_return_1d"),
            "prior_fp": prior_fp,
        },
        "resolve_hint": f"delta vs prior gauge recovers to >= {PRIOR_GAUGE_RESOLVE_PP}pp, "
                        f"fingerprint changes, or sample collapses",
        "last_evaluated_at": now_iso,
        "observations": [{"run": now_iso[:10], "delta_vs_prior_pp": delta_prior}],
    }


def _eval_prior_gauge(probe, retune, efficacy, current_fp, now_iso) -> dict:
    scope = probe.get("scope_key")
    if current_fp and scope != current_fp:
        return _resolved(probe, "scope_changed", f"current fp now {str(current_fp)[:8]}", now_iso)
    by_fp = ((retune or {}).get("outcome_attribution") or {}).get("by_fingerprint") or {}
    cur = by_fp.get(scope)
    if not isinstance(cur, dict):
        return _resolved(probe, "scope_changed", "fingerprint no longer present", now_iso)
    resolved = cur.get("resolved_1d") or 0
    if resolved == 0:
        return _resolved(probe, "sample_collapsed", "resolved_1d == 0", now_iso)
    cur_hr = cur.get("hit_rate_1d")
    pre_hr = _pre_tracker_entry(retune).get("hit_rate_1d")
    # escalate BEFORE resolve — a worsening probe must not silently resolve
    if cur_hr is not None and pre_hr is not None:
        delta_pre = round((cur_hr - pre_hr) * 100, 1)
        if abs(delta_pre) >= PRETRACKER_RED_GATE_PP and resolved >= MIN_RESOLVED_1D:
            return _escalated(
                probe, f"crossed daily RED gate: {delta_pre:+.1f}pp vs pre_tracker "
                       f"at n={resolved}", now_iso)
    if _age_days(probe.get("created_at"), now_iso) >= MAX_PROBE_AGE_DAYS:
        return _resolved(probe, "ttl_expired", f"age >= {MAX_PROBE_AGE_DAYS}d", now_iso)
    prior_fp, prior = _select_prior_gauge(by_fp, scope)
    if not prior or prior.get("hit_rate_1d") is None or cur_hr is None:
        return _resolved(probe, "scope_changed", "no prior gauge to compare", now_iso)
    delta_prior = round((cur_hr - prior["hit_rate_1d"]) * 100, 1)
    if delta_prior >= PRIOR_GAUGE_RESOLVE_PP:
        return _resolved(probe, "recovered",
                         f"delta vs prior {delta_prior:+.1f}pp >= {PRIOR_GAUGE_RESOLVE_PP}", now_iso)
    return _active(probe, f"delta vs prior {delta_prior:+.1f}pp", now_iso,
                   {"run": now_iso[:10], "delta_vs_prior_pp": delta_prior})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k d1`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
python3 -m py_compile portfolio_automation/quant_watch_probes.py
git add portfolio_automation/quant_watch_probes.py tests/test_quant_watch_probes.py
git commit -m "feat(quant-watch): D1 prior-gauge-underperformance detector + evaluator"
```

---

## Task 5: Detector D2 + evaluator — negative_mean_return_persistence

**Files:**
- Modify: `portfolio_automation/quant_watch_probes.py`
- Test: `tests/test_quant_watch_probes.py`

- [ ] **Step 1: Write the failing test**

```python
def test_d2_fires_on_negative_mean_return():
    probe = qwp.detect_negative_mean_return_persistence(
        _retune_fixture(mean_ret=-1.18), "2026-06-08T09:00:00+00:00", "r")
    assert probe is not None
    assert probe["id"] == "negative_mean_return_persistence:d95e"
    assert probe["trigger_snapshot"]["mean_return_1d"] == -1.18


def test_d2_quiet_when_positive():
    probe = qwp.detect_negative_mean_return_persistence(
        _retune_fixture(mean_ret=0.5), "2026-06-08T09:00:00+00:00", "r")
    assert probe is None


def test_d2_quiet_below_min_sample():
    probe = qwp.detect_negative_mean_return_persistence(
        _retune_fixture(mean_ret=-1.0, resolved=5), "2026-06-08T09:00:00+00:00", "r")
    assert probe is None


def test_d2_eval_resolves_when_return_recovers():
    probe = qwp.detect_negative_mean_return_persistence(
        _retune_fixture(mean_ret=-1.18), "2026-06-08T09:00:00+00:00", "r")
    t = qwp._eval_neg_return(probe, _retune_fixture(mean_ret=0.2), None, "d95e",
                             "2026-06-20T09:00:00+00:00")
    assert t["status"] == "resolved" and t["resolution"] == "recovered"


def test_d2_eval_stays_active_when_still_negative():
    probe = qwp.detect_negative_mean_return_persistence(
        _retune_fixture(mean_ret=-1.18), "2026-06-08T09:00:00+00:00", "r")
    t = qwp._eval_neg_return(probe, _retune_fixture(mean_ret=-0.9), None, "d95e",
                             "2026-06-09T09:00:00+00:00")
    assert t["status"] == "active"
    assert t["observation"]["mean_return_1d"] == -0.9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k d2`
Expected: FAIL — functions not defined.

- [ ] **Step 3: Write minimal implementation** (append)

```python
def detect_negative_mean_return_persistence(
    retune: dict, now_iso: str, created_run: str,
) -> dict | None:
    by_fp = ((retune or {}).get("outcome_attribution") or {}).get("by_fingerprint") or {}
    current_fp = (retune or {}).get("current_fingerprint")
    cur = by_fp.get(current_fp) if current_fp else None
    if not isinstance(cur, dict):
        return None
    resolved = cur.get("resolved_1d") or 0
    mean_ret = cur.get("mean_return_1d")
    if resolved < MIN_RESOLVED_1D or mean_ret is None or mean_ret >= 0:
        return None
    return {
        "id": f"{DETECTOR_NEG_RETURN}:{current_fp}",
        "detector": DETECTOR_NEG_RETURN,
        "lens": "quant",
        "scope_key": current_fp,
        "created_at": now_iso,
        "created_run": created_run,
        "severity": "amber",
        "concern": (f"current-fp {current_fp[:8]} mean_return_1d {mean_ret:.2f} "
                    f"(< 0) at n={resolved}"),
        "trigger_snapshot": {"mean_return_1d": mean_ret, "resolved_1d": resolved},
        "resolve_hint": "mean_return_1d recovers to >= 0, or fingerprint changes",
        "last_evaluated_at": now_iso,
        "observations": [{"run": now_iso[:10], "mean_return_1d": mean_ret}],
    }


def _eval_neg_return(probe, retune, efficacy, current_fp, now_iso) -> dict:
    scope = probe.get("scope_key")
    if current_fp and scope != current_fp:
        return _resolved(probe, "scope_changed", f"current fp now {str(current_fp)[:8]}", now_iso)
    by_fp = ((retune or {}).get("outcome_attribution") or {}).get("by_fingerprint") or {}
    cur = by_fp.get(scope)
    if not isinstance(cur, dict):
        return _resolved(probe, "scope_changed", "fingerprint no longer present", now_iso)
    resolved = cur.get("resolved_1d") or 0
    if resolved == 0:
        return _resolved(probe, "sample_collapsed", "resolved_1d == 0", now_iso)
    if _age_days(probe.get("created_at"), now_iso) >= MAX_PROBE_AGE_DAYS:
        return _resolved(probe, "ttl_expired", f"age >= {MAX_PROBE_AGE_DAYS}d", now_iso)
    mean_ret = cur.get("mean_return_1d")
    if mean_ret is not None and mean_ret >= 0:
        return _resolved(probe, "recovered", f"mean_return_1d {mean_ret:.2f} >= 0", now_iso)
    return _active(probe, f"mean_return_1d {mean_ret:.2f}", now_iso,
                   {"run": now_iso[:10], "mean_return_1d": mean_ret})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k d2`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
python3 -m py_compile portfolio_automation/quant_watch_probes.py
git add portfolio_automation/quant_watch_probes.py tests/test_quant_watch_probes.py
git commit -m "feat(quant-watch): D2 negative-mean-return-persistence detector + evaluator"
```

---

## Task 6: Detector D3 + evaluator — sector_drag

**Files:**
- Modify: `portfolio_automation/quant_watch_probes.py`
- Test: `tests/test_quant_watch_probes.py`

- [ ] **Step 1: Write the failing test**

```python
def _efficacy_fixture(sector="sector:Consumer_Cyclical", sig="loser", n=42,
                      vs_baseline=-37.67):
    return {"by_tag": {
        sector: {"significance": sig, "n_samples": n, "vs_baseline_pp": vs_baseline,
                 "hit_rate_1d": 0.07},
        "sector:Technology": {"significance": "winner", "n_samples": 77,
                              "vs_baseline_pp": 6.21, "hit_rate_1d": 0.51},
    }}


def test_d3_fires_on_sector_loser_at_min_n():
    probes = qwp.detect_sector_drag(_efficacy_fixture(), "2026-06-08T09:00:00+00:00", "r")
    assert len(probes) == 1
    assert probes[0]["id"] == "sector_drag:Consumer_Cyclical"
    assert probes[0]["scope_key"] == "Consumer_Cyclical"
    assert probes[0]["trigger_snapshot"]["vs_baseline_pp"] == -37.67


def test_d3_quiet_when_loser_below_min_n():
    probes = qwp.detect_sector_drag(_efficacy_fixture(n=12), "2026-06-08T09:00:00+00:00", "r")
    assert probes == []


def test_d3_quiet_when_no_loser():
    probes = qwp.detect_sector_drag(_efficacy_fixture(sig="neutral"),
                                    "2026-06-08T09:00:00+00:00", "r")
    assert probes == []


def test_d3_eval_resolves_when_no_longer_loser():
    probe = qwp.detect_sector_drag(_efficacy_fixture(), "2026-06-08T09:00:00+00:00", "r")[0]
    t = qwp._eval_sector_drag(probe, None, _efficacy_fixture(sig="neutral"), "d95e",
                              "2026-06-20T09:00:00+00:00")
    assert t["status"] == "resolved" and t["resolution"] == "recovered"


def test_d3_eval_resolves_when_tag_absent():
    probe = qwp.detect_sector_drag(_efficacy_fixture(), "2026-06-08T09:00:00+00:00", "r")[0]
    t = qwp._eval_sector_drag(probe, None, {"by_tag": {}}, "d95e",
                              "2026-06-20T09:00:00+00:00")
    assert t["status"] == "resolved"


def test_d3_eval_stays_active_when_still_loser():
    probe = qwp.detect_sector_drag(_efficacy_fixture(), "2026-06-08T09:00:00+00:00", "r")[0]
    t = qwp._eval_sector_drag(probe, None, _efficacy_fixture(), "d95e",
                              "2026-06-09T09:00:00+00:00")
    assert t["status"] == "active"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k d3`
Expected: FAIL — functions not defined.

- [ ] **Step 3: Write minimal implementation** (append)

```python
def detect_sector_drag(efficacy: dict, now_iso: str, created_run: str) -> list[dict]:
    by_tag = (efficacy or {}).get("by_tag") or {}
    probes: list[dict] = []
    for tag, row in by_tag.items():
        if not (isinstance(tag, str) and tag.startswith("sector:") and isinstance(row, dict)):
            continue
        if row.get("significance") != "loser" or (row.get("n_samples") or 0) < SECTOR_MIN_N:
            continue
        sector = tag.split("sector:", 1)[1]
        probes.append({
            "id": f"{DETECTOR_SECTOR_DRAG}:{sector}",
            "detector": DETECTOR_SECTOR_DRAG,
            "lens": "quant",
            "scope_key": sector,
            "created_at": now_iso,
            "created_run": created_run,
            "severity": "amber",
            "concern": (f"sector {sector} is a loser ({row.get('vs_baseline_pp')}pp vs "
                        f"baseline) at n={row.get('n_samples')}"),
            "trigger_snapshot": {"vs_baseline_pp": row.get("vs_baseline_pp"),
                                 "n_samples": row.get("n_samples"),
                                 "hit_rate_1d": row.get("hit_rate_1d")},
            "resolve_hint": "sector no longer flagged 'loser' or the tag disappears",
            "last_evaluated_at": now_iso,
            "observations": [{"run": now_iso[:10], "vs_baseline_pp": row.get("vs_baseline_pp")}],
        })
    return probes


def _eval_sector_drag(probe, retune, efficacy, current_fp, now_iso) -> dict:
    sector = probe.get("scope_key")
    by_tag = (efficacy or {}).get("by_tag") or {}
    row = by_tag.get(f"sector:{sector}")
    if not isinstance(row, dict):
        return _resolved(probe, "recovered", "sector tag absent", now_iso)
    if row.get("significance") != "loser":
        return _resolved(probe, "recovered",
                         f"sector no longer loser (now {row.get('significance')})", now_iso)
    if _age_days(probe.get("created_at"), now_iso) >= MAX_PROBE_AGE_DAYS:
        return _resolved(probe, "ttl_expired", f"age >= {MAX_PROBE_AGE_DAYS}d", now_iso)
    return _active(probe, f"still loser ({row.get('vs_baseline_pp')}pp)", now_iso,
                   {"run": now_iso[:10], "vs_baseline_pp": row.get("vs_baseline_pp")})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k d3`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
python3 -m py_compile portfolio_automation/quant_watch_probes.py
git add portfolio_automation/quant_watch_probes.py tests/test_quant_watch_probes.py
git commit -m "feat(quant-watch): D3 sector-drag detector + evaluator"
```

---

## Task 7: Aggregators — detect(), evaluate(), evaluator dispatch, idempotency

**Files:**
- Modify: `portfolio_automation/quant_watch_probes.py`
- Test: `tests/test_quant_watch_probes.py`

- [ ] **Step 1: Write the failing test**

```python
def test_detect_aggregates_and_dedupes_active():
    retune = _retune_fixture()
    efficacy = _efficacy_fixture()
    ledger = qwp._empty_ledger()
    new1 = qwp.detect(retune, efficacy, ledger, "2026-06-08T09:00:00+00:00", "r")
    ids = {p["id"] for p in new1}
    assert "prior_gauge_underperformance:d95e" in ids
    assert "negative_mean_return_persistence:d95e" in ids
    assert "sector_drag:Consumer_Cyclical" in ids
    # now mark them active; re-running detect yields no duplicates
    ledger["active"] = new1
    new2 = qwp.detect(retune, efficacy, ledger, "2026-06-09T09:00:00+00:00", "r")
    assert new2 == []


def test_evaluate_dispatches_per_detector_and_manual_stays_active():
    retune = _retune_fixture()
    efficacy = _efficacy_fixture()
    ledger = qwp._empty_ledger()
    ledger["active"] = qwp.detect(retune, efficacy, ledger, "2026-06-08T09:00:00+00:00", "r")
    ledger["active"].append({"id": "manual:foo", "detector": "manual",
                             "scope_key": "foo", "created_at": "2026-06-08T09:00:00+00:00"})
    transitions = qwp.evaluate(retune, efficacy, "d95e", ledger, "2026-06-09T09:00:00+00:00")
    by_id = {t["id"]: t for t in transitions}
    assert by_id["prior_gauge_underperformance:d95e"]["status"] == "active"
    assert by_id["manual:foo"]["status"] == "active"  # never auto-resolved
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k "detect_aggregates or evaluate_dispatches"`
Expected: FAIL — `detect` / `evaluate` not defined.

- [ ] **Step 3: Write minimal implementation** (append)

```python
_EVALUATORS = {
    DETECTOR_PRIOR_GAUGE: _eval_prior_gauge,
    DETECTOR_NEG_RETURN: _eval_neg_return,
    DETECTOR_SECTOR_DRAG: _eval_sector_drag,
}


def detect(retune, efficacy, ledger, now_iso, created_run) -> list[dict]:
    """Run every detector; return NEW probes whose id is not already active."""
    active_ids = {p.get("id") for p in (ledger.get("active") or [])}
    found: list[dict] = []
    p1 = detect_prior_gauge_underperformance(retune, now_iso, created_run)
    if p1:
        found.append(p1)
    p2 = detect_negative_mean_return_persistence(retune, now_iso, created_run)
    if p2:
        found.append(p2)
    found.extend(detect_sector_drag(efficacy, now_iso, created_run))
    return [p for p in found if p["id"] not in active_ids]


def evaluate(retune, efficacy, current_fp, ledger, now_iso) -> list[dict]:
    """Re-check each active probe; return one transition per probe. Probes whose
    detector has no evaluator (e.g. manual) stay active until cleared by hand."""
    out: list[dict] = []
    for probe in (ledger.get("active") or []):
        ev = _EVALUATORS.get(probe.get("detector"))
        if ev is None:
            out.append(_active(probe, "manual — operator clears", now_iso, None))
            continue
        try:
            out.append(ev(probe, retune, efficacy, current_fp, now_iso))
        except Exception as exc:  # never let one bad probe abort the run
            out.append(_active(probe, f"eval error: {exc}", now_iso, None))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k "detect_aggregates or evaluate_dispatches"`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
python3 -m py_compile portfolio_automation/quant_watch_probes.py
git add portfolio_automation/quant_watch_probes.py tests/test_quant_watch_probes.py
git commit -m "feat(quant-watch): detect/evaluate aggregators + evaluator dispatch"
```

---

## Task 8: update_ledger() — add new, archive resolved/escalated, cap observations/archive

**Files:**
- Modify: `portfolio_automation/quant_watch_probes.py`
- Test: `tests/test_quant_watch_probes.py`

- [ ] **Step 1: Write the failing test**

```python
def test_update_ledger_adds_new_and_archives_resolved():
    now = "2026-06-09T09:00:00+00:00"
    ledger = {"schema_version": "1", "active": [
        {"id": "a", "detector": "d", "created_at": "2026-06-01T09:00:00+00:00",
         "observations": []},
        {"id": "b", "detector": "d", "created_at": "2026-06-01T09:00:00+00:00",
         "observations": []},
    ], "archive": []}
    new_probes = [{"id": "c", "detector": "d", "created_at": now, "observations": []}]
    transitions = [
        qwp._active({"id": "a"}, "still bad", now, {"run": "2026-06-09", "v": 1}),
        qwp._resolved({"id": "b"}, "recovered", "ok now", now),
    ]
    out = qwp.update_ledger(ledger, new_probes, transitions, now)
    active_ids = {p["id"] for p in out["active"]}
    archive_ids = {p["id"] for p in out["archive"]}
    assert active_ids == {"a", "c"}          # b archived, c added
    assert archive_ids == {"b"}
    arch_b = out["archive"][0]
    assert arch_b["resolution"] == "recovered"
    assert arch_b["resolved_at"] == now
    assert arch_b["lifetime_days"] == 8
    # a got its observation appended + last_evaluated_at bumped
    a = next(p for p in out["active"] if p["id"] == "a")
    assert a["observations"][-1] == {"run": "2026-06-09", "v": 1}
    assert a["last_evaluated_at"] == now


def test_update_ledger_escalated_goes_to_archive_with_reason():
    now = "2026-06-09T09:00:00+00:00"
    ledger = {"schema_version": "1", "active": [
        {"id": "a", "detector": "d", "created_at": "2026-06-01T09:00:00+00:00",
         "observations": []}], "archive": []}
    transitions = [qwp._escalated({"id": "a"}, "crossed gate", now)]
    out = qwp.update_ledger(ledger, [], transitions, now)
    assert out["active"] == []
    assert out["archive"][0]["resolution"] == "escalated_to_red"


def test_update_ledger_caps_observations_and_archive():
    now = "2026-06-09T09:00:00+00:00"
    probe = {"id": "a", "detector": "d", "created_at": "2026-06-01T09:00:00+00:00",
             "observations": [{"run": f"d{i}"} for i in range(qwp.MAX_OBSERVATIONS)]}
    ledger = {"schema_version": "1", "active": [probe],
              "archive": [{"id": f"old{i}"} for i in range(qwp.MAX_ARCHIVE)]}
    transitions = [qwp._active({"id": "a"}, "x", now, {"run": "new"})]
    out = qwp.update_ledger(ledger, [], transitions, now)
    a = out["active"][0]
    assert len(a["observations"]) == qwp.MAX_OBSERVATIONS  # capped
    assert a["observations"][-1] == {"run": "new"}
    assert len(out["archive"]) == qwp.MAX_ARCHIVE          # capped (FIFO)


def test_update_ledger_does_not_mutate_input():
    now = "2026-06-09T09:00:00+00:00"
    ledger = {"schema_version": "1", "active": [
        {"id": "a", "detector": "d", "created_at": now, "observations": []}],
        "archive": []}
    transitions = [qwp._resolved({"id": "a"}, "recovered", "x", now)]
    qwp.update_ledger(ledger, [], transitions, now)
    assert ledger["active"][0]["id"] == "a"  # original untouched
    assert ledger["archive"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k update_ledger`
Expected: FAIL — `update_ledger` not defined.

- [ ] **Step 3: Write minimal implementation** (append)

```python
def update_ledger(ledger, new_probes, transitions, now_iso) -> dict:
    """Return a NEW ledger (input not mutated):
      - resolved/escalated probes move to archive with resolved_at + resolution
        + lifetime_days;
      - still-active probes get last_evaluated_at bumped and their observation
        appended (capped at MAX_OBSERVATIONS);
      - new_probes are appended to active;
      - archive is FIFO-capped at MAX_ARCHIVE."""
    import copy
    active_in = {p.get("id"): copy.deepcopy(p) for p in (ledger.get("active") or [])}
    archive = [copy.deepcopy(a) for a in (ledger.get("archive") or [])]
    by_id = {t.get("id"): t for t in transitions}

    new_active: list[dict] = []
    for pid, probe in active_in.items():
        t = by_id.get(pid)
        if t is None:
            new_active.append(probe)  # no transition (shouldn't happen) → keep
            continue
        if t["status"] in (RESOLVED, ESCALATED):
            probe["resolved_at"] = t.get("resolved_at", now_iso)
            probe["resolved_run"] = now_iso[:10]
            probe["resolution"] = t.get("resolution")
            probe["resolution_detail"] = t.get("detail")
            probe["lifetime_days"] = _age_days(probe.get("created_at"), now_iso)
            archive.append(probe)
        else:  # active
            probe["last_evaluated_at"] = now_iso
            obs = t.get("observation")
            if obs:
                trail = list(probe.get("observations") or [])
                trail.append(obs)
                probe["observations"] = trail[-MAX_OBSERVATIONS:]
            new_active.append(probe)

    for p in (new_probes or []):
        new_active.append(copy.deepcopy(p))

    archive = archive[-MAX_ARCHIVE:]
    return {"schema_version": "1", "active": new_active, "archive": archive}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k update_ledger`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
python3 -m py_compile portfolio_automation/quant_watch_probes.py
git add portfolio_automation/quant_watch_probes.py tests/test_quant_watch_probes.py
git commit -m "feat(quant-watch): update_ledger — archive-with-outcome + caps"
```

---

## Task 9: overall_status() + render_status() + ledger_liveness

**Files:**
- Modify: `portfolio_automation/quant_watch_probes.py`
- Test: `tests/test_quant_watch_probes.py`

- [ ] **Step 1: Write the failing test**

```python
def test_overall_status_mapping():
    assert qwp.overall_status({"active": []}, []) == qwp.GREEN
    assert qwp.overall_status({"active": [{"id": "a"}]}, []) == qwp.AMBER
    esc = [{"id": "a", "status": "escalated"}]
    assert qwp.overall_status({"active": []}, esc) == qwp.RED


def test_render_status_shape():
    now = "2026-06-09T09:00:00+00:00"
    ledger = {"schema_version": "1", "active": [
        {"id": "prior_gauge_underperformance:d95e", "detector": "prior_gauge_underperformance",
         "concern": "bad", "severity": "amber", "created_at": "2026-06-08T09:00:00+00:00",
         "observations": [{"run": "2026-06-09", "delta_vs_prior_pp": -24.1}]}],
        "archive": []}
    new_probes = [{"id": "prior_gauge_underperformance:d95e"}]
    transitions = [{"id": "x", "status": "resolved", "resolution": "recovered"},
                   {"id": "y", "status": "escalated", "resolution": "escalated_to_red"}]
    status = qwp.render_status(ledger, new_probes, transitions, now)
    assert status["observe_only"] is True
    assert status["source"] == "quant_watch_probes"
    assert status["overall_status"] == "red"   # an escalation this run
    assert status["active_count"] == 1
    assert status["registered_today"] == ["prior_gauge_underperformance:d95e"]
    assert status["resolved_today"] == [{"id": "x", "resolution": "recovered"}]
    assert status["escalated_today"] == [{"id": "y", "resolution": "escalated_to_red"}]
    assert status["ledger_liveness"]["status"] == "ok"
    assert status["active"][0]["age_days"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k "overall_status or render_status"`
Expected: FAIL — functions not defined.

- [ ] **Step 3: Write minimal implementation** (append)

```python
def overall_status(ledger, transitions) -> str:
    if any(t.get("status") == ESCALATED for t in (transitions or [])):
        return RED
    if ledger.get("active"):
        return AMBER
    return GREEN


def render_status(ledger, new_probes, transitions, now_iso) -> dict:
    active = ledger.get("active") or []
    new_ids = [p.get("id") for p in (new_probes or [])]
    resolved_today = [{"id": t.get("id"), "resolution": t.get("resolution")}
                      for t in (transitions or []) if t.get("status") == RESOLVED]
    escalated_today = [{"id": t.get("id"), "resolution": t.get("resolution")}
                       for t in (transitions or []) if t.get("status") == ESCALATED]
    # liveness: an active probe is "stale" if it has no observation this run
    stale = sum(1 for p in active
                if (p.get("last_evaluated_at") or p.get("created_at")) != now_iso
                and now_iso not in (p.get("last_evaluated_at") or ""))
    return {
        "generated_at": now_iso,
        "observe_only": True,
        "schema_version": "1",
        "source": "quant_watch_probes",
        "overall_status": overall_status(ledger, transitions),
        "active_count": len(active),
        "active": [{
            "id": p.get("id"), "detector": p.get("detector"),
            "concern": p.get("concern"), "severity": p.get("severity"),
            "age_days": _age_days(p.get("created_at"), now_iso),
            "last_observation": (p.get("observations") or [None])[-1],
        } for p in active],
        "registered_today": new_ids,
        "resolved_today": resolved_today,
        "escalated_today": escalated_today,
        "ledger_liveness": {"status": "ok" if stale == 0 else "warn",
                            "active_count": len(active), "stale_active": stale},
        "disclaimer": (
            "Observe-only quant watch ledger. Tracks sub-RED quant concerns; "
            "re-checks and auto-retires them. Does not modify portfolio, "
            "allocation, scoring, or decision state."),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k "overall_status or render_status"`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
python3 -m py_compile portfolio_automation/quant_watch_probes.py
git add portfolio_automation/quant_watch_probes.py tests/test_quant_watch_probes.py
git commit -m "feat(quant-watch): overall_status + render_status + ledger_liveness"
```

---

## Task 10: run_quant_watch() orchestrator + artifact/ledger writes

**Files:**
- Modify: `portfolio_automation/quant_watch_probes.py`
- Test: `tests/test_quant_watch_probes.py`

- [ ] **Step 1: Write the failing test**

```python
def test_run_quant_watch_end_to_end(tmp_path):
    # arrange artifacts under a fake root
    root = tmp_path
    (root / "outputs" / "latest").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "outputs" / "latest" / "retune_impact.json").write_text(
        json.dumps(_retune_fixture()), encoding="utf-8")
    (root / "outputs" / "latest" / "pattern_efficacy_monthly.json").write_text(
        json.dumps(_efficacy_fixture()), encoding="utf-8")

    result = qwp.run_quant_watch(root=root, now_iso="2026-06-08T09:00:00+00:00",
                                 created_run="test-run", write_files=True)

    assert result["overall_status"] == "amber"
    assert result["active_count"] == 3
    # ledger written
    led = json.loads((root / "data" / "quant_watch_ledger.json").read_text())
    assert len(led["active"]) == 3
    # status artifact written
    status = json.loads(
        (root / "outputs" / "latest" / "quant_watch_status.json").read_text())
    assert status["observe_only"] is True
    assert status["active_count"] == 3

    # second run same inputs → idempotent (no new probes, still 3 active)
    result2 = qwp.run_quant_watch(root=root, now_iso="2026-06-09T09:00:00+00:00",
                                  created_run="test-run", write_files=True)
    assert result2["registered_today"] == []
    assert result2["active_count"] == 3


def test_run_quant_watch_degrades_when_artifacts_missing(tmp_path):
    (tmp_path / "data").mkdir()
    result = qwp.run_quant_watch(root=tmp_path, now_iso="2026-06-08T09:00:00+00:00",
                                 created_run="r", write_files=False)
    assert result["overall_status"] == "green"
    assert result["active_count"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k run_quant_watch`
Expected: FAIL — `run_quant_watch` not defined.

- [ ] **Step 3: Write minimal implementation** (append)

```python
def write_ledger(path: str | Path, ledger: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ledger, indent=2, default=str), encoding="utf-8")


def run_quant_watch(*, root: str | Path = ".", now_iso: str | None = None,
                    created_run: str = "quant-watch-analysis",
                    write_files: bool = True) -> dict:
    """Load ledger + source artifacts → detect → evaluate → update → render.
    Writes the ledger and the status artifact when write_files. Returns the
    status dict. Never raises — degrades to an empty-but-valid status."""
    root_path = Path(root).resolve()
    now = now_iso or _now_iso()
    try:
        ledger = load_ledger(root_path / _LEDGER_REL)
        retune = _load_json(root_path / "outputs/latest/retune_impact.json") or {}
        efficacy = _load_json(root_path / "outputs/latest/pattern_efficacy_monthly.json") or {}
        current_fp = retune.get("current_fingerprint")

        transitions = evaluate(retune, efficacy, current_fp, ledger, now)
        new_probes = detect(retune, efficacy, ledger, now, created_run)
        status = render_status(ledger, new_probes, transitions, now)
        new_ledger = update_ledger(ledger, new_probes, transitions, now)

        if write_files:
            write_ledger(root_path / _LEDGER_REL, new_ledger)
            safe_write_json(OutputNamespace.LATEST, _STATUS_REL, status,
                            base_dir=root_path / "outputs")
        return status
    except Exception as exc:
        return {"generated_at": now, "observe_only": True, "source": "quant_watch_probes",
                "overall_status": GREEN, "active_count": 0, "active": [],
                "registered_today": [], "resolved_today": [], "escalated_today": [],
                "ledger_liveness": {"status": "warn", "error": str(exc)},
                "disclaimer": "Observe-only quant watch ledger (degraded)."}
```

Note: `render_status` is called on the *pre-update* ledger (so `active_count` reflects probes carried in plus new), while `update_ledger` produces the persisted state. For the post-run snapshot we want active_count to include the newly-registered probes and exclude those archived this run. Adjust `render_status` call to use the new ledger for `active`, but keep `registered_today`/`resolved_today`/`escalated_today` from this run's lists:

Replace the two lines in `run_quant_watch`:

```python
        status = render_status(ledger, new_probes, transitions, now)
        new_ledger = update_ledger(ledger, new_probes, transitions, now)
```

with:

```python
        new_ledger = update_ledger(ledger, new_probes, transitions, now)
        status = render_status(new_ledger, new_probes, transitions, now)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_quant_watch_probes.py -q -k run_quant_watch`
Expected: PASS (2 passed). If `active_count` assertion is off-by-the-archived-set, confirm the render_status-after-update_ledger ordering above is applied.

- [ ] **Step 5: Run the full module test file + commit**

```bash
python3 -m pytest tests/test_quant_watch_probes.py -q
# Expected: all green (≈ 34 tests)
git add portfolio_automation/quant_watch_probes.py tests/test_quant_watch_probes.py
git commit -m "feat(quant-watch): run_quant_watch orchestrator + ledger/artifact writes"
```

---

## Task 11: The `/quant-watch-analysis` skill

**Files:**
- Create: `.claude/commands/quant-watch-analysis.md`

No automated test (it's a prompt). Verified by a manual dry-run in Task 13.

- [ ] **Step 1: Write the skill file**

Create `.claude/commands/quant-watch-analysis.md` with this content:

````markdown
# Quant Watch Analysis

Operational function + health check of the quant-watch probe ledger: a
self-managing list of sub-RED quant concerns. Auto-registers a probe when a
deterministic quant condition fires below the daily-tool-analysis RED
trip-wires, re-checks each open probe, and auto-archives it on resolution.
On-demand; delegated to daily by `/daily-tool-analysis`. Working dir
`/opt/stockbot`.

Module of record: `portfolio_automation/quant_watch_probes.py`. Do NOT
re-derive detector/resolution logic in this prose — the module owns it.

---

## Step 1 — Run the loop (deterministic)

Run the module orchestrator. It loads the ledger + source artifacts, evaluates
open probes (escalate-before-resolve), detects new ones, archives the resolved,
and writes both the ledger (`data/quant_watch_ledger.json`) and the status
artifact (`outputs/latest/quant_watch_status.json`):

```bash
python3 -c "import json; from portfolio_automation.quant_watch_probes import run_quant_watch; print(json.dumps(run_quant_watch(root='.', created_run='quant-watch-analysis'), indent=2))"
```

Read the returned JSON: `overall_status`, `active_count`, `active[]`,
`registered_today`, `resolved_today`, `escalated_today`, `ledger_liveness`.

## Step 2 — Manual judgment layer (optional)

Skim today's `outputs/latest/daily_memo.md` + `retune_impact.json` +
`pattern_efficacy_monthly.json` for a *novel* quant concern NOT covered by the
three detectors (prior-gauge underperformance, negative mean-return, sector
drag). If you find one worth tracking, append a manual probe to the active
ledger so it persists across runs:

```bash
python3 -c "
import json
from portfolio_automation.quant_watch_probes import load_ledger, write_ledger, _now_iso
p='data/quant_watch_ledger.json'; led=load_ledger(p)
led['active'].append({
  'id':'manual:<short-slug>','detector':'manual','lens':'quant',
  'scope_key':'<slug>','created_at':_now_iso(),'created_run':'quant-watch-analysis',
  'severity':'amber','concern':'<one-line concern>',
  'trigger_snapshot':{},'resolve_hint':'<how an operator will know it cleared>',
  'observations':[]})
write_ledger(p, led); print('appended manual:<short-slug>')
"
```

Manual probes are NEVER auto-resolved — retire one only when you (or the
operator) judge it cleared, by removing it from `active` (optionally moving it
to `archive` with `resolution:'manual'`).

## Step 3 — Triage

- **GREEN** — `overall_status == "green"` (no active probes).
- **AMBER** — `overall_status == "amber"` (≥1 active probe; the sub-RED band).
- **RED** — `overall_status == "red"` (≥1 probe escalated this run; it crossed a
  daily RED gate). The escalation is, by construction, also a daily RED key —
  daily-tool-analysis owns the RED *response* + agent dispatch.

If `ledger_liveness.status == "warn"`, note the stale/empty-ledger condition.

## Step 4 — Heartbeat (emit every run)

Lead line:

`[GREEN|AMBER|RED] quant-watch YYYY-MM-DD: {active_count} active · {len(registered_today)} registered · {len(resolved_today)} resolved · {len(escalated_today)} escalated`

Then one line per active probe:
`- {detector}: {concern} (age {age_days}d, last {last_observation})`

And, when present:
`- resolved today: {id} ({resolution})`
`- ESCALATED today: {id} → now daily-RED-tracked; see daily-tool-analysis dispatch`

## Step 5 — Notes

The ledger + status artifact are already written by Step 1. Nothing else to
persist. The archive (`data/quant_watch_ledger.json:archive`) is the
retrospective trail consumed by the monthly/yearly tool-analysis skills.
````

- [ ] **Step 2: Smoke-test the orchestrator command actually runs**

Run (this is the exact command the skill's Step 1 uses):
```bash
cd /opt/stockbot && python3 -c "import json; from portfolio_automation.quant_watch_probes import run_quant_watch; print(json.dumps(run_quant_watch(root='.', created_run='smoke'), indent=2))"
```
Expected: valid JSON with `overall_status` ∈ {green, amber, red}. On the live VPS artifacts this should register `prior_gauge_underperformance:d95e3096443925b0` and `negative_mean_return_persistence:d95e3096443925b0` (both fire on today's data), so `active_count >= 2` and `overall_status == "amber"`.

- [ ] **Step 3: Inspect the written artifacts**

Run:
```bash
cd /opt/stockbot && cat outputs/latest/quant_watch_status.json && echo "--- LEDGER ---" && cat data/quant_watch_ledger.json
```
Expected: status artifact with `observe_only: true`; ledger with the registered probes under `active`.

- [ ] **Step 4: Commit**

```bash
git add .claude/commands/quant-watch-analysis.md data/quant_watch_ledger.json outputs/latest/quant_watch_status.json
git commit -m "feat(quant-watch): /quant-watch-analysis skill + first ledger/status"
```

---

## Task 12: Wire into `/daily-tool-analysis`

**Files:**
- Modify: `.claude/commands/daily-tool-analysis.md`

Three additive edits. Read the file first to get exact surrounding text.

- [ ] **Step 1: Add the artifact to Step 1's read list**

In `.claude/commands/daily-tool-analysis.md`, find the numbered artifact list in
"Step 1 — Load state + read artifacts" (ends at item 16,
`outputs/policy/auto_apply_audit.json`). Add a new item after it:

```markdown
17. `outputs/latest/quant_watch_status.json` → overall_status, active_count, active[] (concern + age_days), registered_today, resolved_today, escalated_today, ledger_liveness (added 2026-06-08; quant-watch probe ledger — sub-RED quant concern tracker)
```

- [ ] **Step 2: Add the sub-check delegation block**

Find the "### Pattern-Loop operational sub-check (delegate to `/pattern-loop-analysis`)"
section near the end of Step 3. Immediately AFTER that section, add:

```markdown
### Quant-watch operational sub-check (delegate to `/quant-watch-analysis`)

Run the `/quant-watch-analysis` skill's Step 1 backbone as the daily driver of
the quant-watch probe ledger (auto-register sub-RED quant concerns, re-check
open probes, auto-archive resolved ones). Do NOT re-derive detector logic here
— that skill + `portfolio_automation/quant_watch_probes.py` own it. Fold its
one-line heartbeat into the daily body (Step 4, item: "quant-watch: …").

Escalate the DAILY check to RED only on the quant-watch RED condition
(`escalated_today` non-empty). By construction an escalated probe has crossed a
daily RED gate (e.g. `|delta_hit_rate_pp| >= 10 at n>=30`), so the existing
daily RED logic + `portfolio-attribution-analyst` dispatch already own the
response — quant-watch adds continuity + same-run visibility, not a second RED
authority. The steady state (≥1 active AMBER probe, e.g. the prior-gauge
underperformance trap) is AMBER — report it, don't alert on it.
```

- [ ] **Step 3: Add the Step 4 body line**

In "Step 4 — Output", find body item `6d. Pattern-loop (always, ...)`. Add a
sibling item after it:

```markdown
6e. Quant-watch (always, from the sub-check above): `"Quant-watch: {overall_status} · {active_count} active ({top active probe concern}); {len(registered_today)}↑/{len(resolved_today)}↓/{len(escalated_today)} esc today"` — folds in the `/quant-watch-analysis` heartbeat. RED only when `escalated_today` is non-empty (which is already a daily RED key).
```

- [ ] **Step 4: Verify the edits read coherently**

Run:
```bash
cd /opt/stockbot && grep -n "quant_watch_status.json\|quant-watch\|Quant-watch" .claude/commands/daily-tool-analysis.md
```
Expected: 3+ hits (artifact read entry, sub-check section, Step 4 line).

- [ ] **Step 5: Commit**

```bash
git add .claude/commands/daily-tool-analysis.md
git commit -m "feat(quant-watch): wire quant-watch sub-check into daily-tool-analysis"
```

---

## Task 13: Module docs + full-suite validation

**Files:**
- Create: `docs/quant_watch_probes.md`

- [ ] **Step 1: Write the module doc**

Create `docs/quant_watch_probes.md`:

```markdown
# quant_watch_probes

Observe-only ledger of **sub-RED quant concerns** ("watch probes"). Companion to
`applied_fix_verifier`: that module tracks *applied fixes*; this tracks *open
concerns* that sit below the `daily-tool-analysis` RED trip-wires yet are worth
watching with continuity.

## Lifecycle

1. **Register** — a deterministic detector fires and a probe is added to
   `data/quant_watch_ledger.json:active`, keyed `detector_id:scope_key`
   (idempotent — re-running never duplicates).
2. **Re-check** — each run a paired evaluator recomputes from current artifacts
   and returns `active` / `resolved` / `escalated` (escalate is checked first).
3. **Retire** — resolved/escalated probes move to `archive` with `resolved_at`,
   `resolution`, and `lifetime_days`. Resolutions: `recovered`, `scope_changed`,
   `sample_collapsed`, `escalated_to_red`, `ttl_expired`, `manual`.

## Detectors (v1)

| id | source | fires when | resolves when | escalates when |
|---|---|---|---|---|
| `prior_gauge_underperformance` | `retune_impact.json` | current-fp ≤ −10pp vs prior gauge at n≥30 AND `|Δ vs pre_tracker|` < 10pp | Δ vs prior ≥ −2pp / fp change / n→0 | `|Δ vs pre_tracker|` ≥ 10pp at n≥30 (daily RED gate) |
| `negative_mean_return_persistence` | `retune_impact.json` | current-fp `mean_return_1d` < 0 at n≥30 | `mean_return_1d` ≥ 0 / fp change | — |
| `sector_drag` | `pattern_efficacy_monthly.json` | a `sector:*` tag is `loser` at n≥30 | no longer `loser` / tag absent | — |

Plus a **manual** probe path (`detector: "manual"`) for novel concerns; manual
probes are never auto-resolved.

## Artifacts

- `data/quant_watch_ledger.json` — state: `{schema_version, active[], archive[]}`.
- `outputs/latest/quant_watch_status.json` — observe-only heartbeat snapshot
  (`overall_status` green/amber/red, active[], registered/resolved/escalated
  _today, ledger_liveness).

## Status levels

`green` (no active probes) · `amber` (≥1 active) · `red` (≥1 escalated this
run). RED escalation is, by construction, also a daily RED key — daily owns the
RED response; this module adds continuity + visibility.

## Entry point

`run_quant_watch(root, now_iso=None, created_run=..., write_files=True)` — loads,
evaluates, detects, updates, writes, returns the status dict. Never raises.

## Consumers

- `/quant-watch-analysis` skill (daily, on-demand) — drives the loop + heartbeat.
- `/daily-tool-analysis` — delegates the sub-check + folds the heartbeat.
- Monthly/yearly tool-analysis — mine `archive[]` for retrospectives (follow-up).

Observe-only: mutates only its ledger + status artifact; never decision, score,
allocation, or portfolio state.
```

- [ ] **Step 2: Run the full test suite**

Run:
```bash
cd /opt/stockbot && python3 -m pytest -q
```
Expected: all pass (the new `tests/test_quant_watch_probes.py` ~34 tests included; no regressions in the existing suite).

- [ ] **Step 3: py_compile the touched module + a smoke import**

Run:
```bash
cd /opt/stockbot && python3 -m py_compile portfolio_automation/quant_watch_probes.py && python3 -c "import portfolio_automation.quant_watch_probes as q; print('ok', q.GREEN, q.AMBER, q.RED)"
```
Expected: `ok green amber red`.

- [ ] **Step 4: Commit**

```bash
git add docs/quant_watch_probes.md
git commit -m "docs(quant-watch): module documentation"
```

---

## Task 14: Update CHANGELOG + project state, open PR

**Files:**
- Modify: `docs/CHANGELOG_DECISIONS.md` (append an entry — match existing format)
- Modify: `.agent/project_state.yaml` (only if there's a natural slot; otherwise skip)

- [ ] **Step 1: Append a CHANGELOG entry**

Read the top of `docs/CHANGELOG_DECISIONS.md` for the format, then add a dated
entry summarizing: quant-watch probe ledger shipped (3 detectors + manual path,
archive-with-outcome, AMBER/RED-hybrid escalation, wired into daily). Reference
the spec + plan paths.

- [ ] **Step 2: Commit**

```bash
git add docs/CHANGELOG_DECISIONS.md
git commit -m "docs(quant-watch): changelog entry"
```

- [ ] **Step 3: Push branch + open PR (STOP for operator go-ahead before merge)**

```bash
git push -u origin feat/quant-watch-probes
gh pr create --title "feat: quant-watch probe ledger (sub-RED quant concern tracker)" \
  --body "$(cat <<'EOF'
Implements the self-managing quant-watch probe ledger per
docs/superpowers/specs/2026-06-08-quant-watch-probes-design.md.

- 3 deterministic detectors (prior-gauge underperformance, negative mean-return,
  sector drag) + manual judgment path
- archive-with-outcome retirement; AMBER/RED-hybrid escalation that defers the
  RED response to daily-tool-analysis via a shared threshold
- observe-only; no production-pipeline changes (skill-driven, rides the existing
  09:15 daily delegation)
- new /quant-watch-analysis skill + daily-tool-analysis sub-check wiring
- ~34 unit/integration tests; full suite green

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Per the operator's standing rule, **stop here and wait for explicit go-ahead
before merging to main** (production boundary). Provide the VPS validation block
below.

- [ ] **Step 4: VPS validation block (for the operator to run)**

```bash
cd /opt/stockbot
git fetch origin && git checkout feat/quant-watch-probes
python3 -m pytest -q tests/test_quant_watch_probes.py
python3 -m pytest -q
python3 -c "import json; from portfolio_automation.quant_watch_probes import run_quant_watch; print(json.dumps(run_quant_watch(root='.', created_run='vps-validate'), indent=2))"
cat outputs/latest/quant_watch_status.json
cat data/quant_watch_ledger.json
```

Expected: targeted + full suites green; orchestrator returns `overall_status: amber`
with the prior-gauge + negative-return probes active on live data.

---

## Self-Review

**Spec coverage:**
- §3 architecture (module + ledger + artifact + skill + daily hook) → Tasks 1–13 ✓
- §4 module API (`detect`/`evaluate`/`update_ledger`/`render_status`/`overall_status`/`load_ledger`/orchestrator) → Tasks 1,7,8,9,10 ✓
- §5 data model (active probe, resolved archive w/ lifetime_days, ledger shape, status artifact) → Tasks 4,8,9 ✓
- §6 three detectors + resolution kinds → Tasks 4,5,6 (resolution kinds realized as evaluator returns; `ttl_expired`, `scope_changed`, `sample_collapsed`, `recovered`, `escalated_to_red` all covered) ✓
- §7 AMBER/RED-hybrid escalation → Tasks 4 (escalate path), 9 (overall_status), 12 (daily handoff) ✓
- §8 skill steps + daily integration → Tasks 11,12 ✓
- §9 observe-only/namespace → Task 10 (`safe_write_json` LATEST; ledger in `data/`) ✓
- §10 test plan (10 cases) → covered across Tasks 1–10 (idempotency T7, manual-never-dropped T7, escalation T4, liveness T9, corrupt-ledger T1) ✓
- §11 analysis+health pairing → Task 12 ✓

**Placeholder scan:** No "TBD"/"handle edge cases"/"similar to". The skill file's
manual-probe snippet uses `<short-slug>`/`<one-line concern>` — these are
intentional operator fill-ins in a prompt template, not plan placeholders. ✓

**Type/name consistency:** `_active/_resolved/_escalated` (T3) used by all
evaluators (T4–6) and `update_ledger` (T8); `_select_prior_gauge` (T2) used by
D1 detector+evaluator (T4); `_EVALUATORS` keys = detector id constants (T7);
`render_status` called on the post-`update_ledger` ledger (T10 note). Status
constants `GREEN/AMBER/RED` lowercase throughout. ✓

**One spec deviation, intentional + documented:** machine-spec `resolve_when`/
`escalate_when` → detector-paired evaluators (see header). Captured in the spec
deviation note + module doc.
````
