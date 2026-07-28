# Watchlist-Removal Producer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit human-gated `watchlist_remove` proposals for static universe members that are decayed on both accuracy and expectancy, so the production watchlist can shrink as well as grow.

**Architecture:** A fifth experiment in the sim-governance simulation lane reads `universe_sanitation`'s monthly ranks and proposes removals for symbols present in the *effective* production watchlist. Everything downstream (bundle → AI review → proposal → human approval → overlay applier) already exists and is enabled; this adds the missing producer plus the expectancy field it needs, and fixes the baseline loader that currently reads a nonexistent config key.

**Tech Stack:** Python 3.12, pytest, `.venv/bin/python`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-28-watchlist-remove-producer-design.md`

## Global Constraints

- Interpreter is `/opt/stockbot/.venv/bin/python`. Bare `python` is not on PATH.
- Observe-only except the sanctioned human-gated promotion path. Do NOT change `decision_engine.py`, scoring logic, `_TRACKED_KNOBS`, or any score semantics.
- **Authority invariant:** `PROPOSAL_WATCHLIST_REMOVE` must stay absent from `auto_approval._WATCHLIST_ELIGIBLE_TYPES`. Removals are always human-gated. Never add it.
- Do NOT hand-edit `config.json`'s watchlist. Removal happens via the overlay; config stays the historical baseline.
- `universe_sanitation._OBSERVE_ONLY` stays `True`.
- Null-safety rule: a missing `recent_hit_rate_1d` or `recent_mean_return_1d` is `None`, never coerced to `0.0`. The two coercions fail in opposite directions (a null hit rate reads as worst-possible; a null mean is not `< 0`).
- The simulation lane is non-fatal per experiment: any producer error degrades to `[]`, never raises.
- Commit with explicit paths (`git add <path>`), never `git commit -am` — the tree carries unrelated modified artifacts.
- Every commit message ends with:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
- Run the full suite before the final commit. Baseline is **9 pre-existing failures** (`tuning_proposals` x2, `run_loop`, `operator_control` x2, `operator_worker_runner` x2, `data_budget_governor`, `social_sentiment/quality_gates`). Anything beyond those 9 is a regression.
- The full suite mutates `config/signal_registry.yaml`. Back it up first and restore after:
  `cp config/signal_registry.yaml /tmp/sr.bak` … `cp /tmp/sr.bak config/signal_registry.yaml`

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `portfolio_automation/universe_sanitation.py` | universe ranking; gains the expectancy term | modify (~6 lines) |
| `portfolio_automation/sim_governance/simulation_lane.py` | baseline loader + the new experiment | modify (~70 lines) |
| `portfolio_automation/quant_watch_probes.py` | composition-break probe registration | modify (~35 lines) |
| `tests/test_universe_sanitation_mean_return.py` | Task 1 tests | create |
| `tests/test_sim_lane_effective_baseline.py` | Task 2 tests | create |
| `tests/test_watchlist_decay_removals.py` | Task 3 tests | create |
| `tests/test_universe_composition_break_probe.py` | Task 4 tests | create |
| `.claude/commands/daily-tool-analysis.md` | health check line + dispatch | modify |
| `.claude/agents/portfolio-discovery-health.md` | universe-decay audit duty | modify |

---

## Task 1: `universe_sanitation` emits `recent_mean_return_1d`

**Files:**
- Modify: `portfolio_automation/universe_sanitation.py:164-178` (bucket + accumulate, inside `_load_recent_signals` at `:145`), `:420-446` (emit, inside `_rank_candidates` at `:415`)
- Test: `tests/test_universe_sanitation_mean_return.py`

**Helper names (verified):** the stats loader is `_load_recent_signals(root, lookback_days)`; the row builder is `_rank_candidates(by_sym, root)` — argument order `(by_sym, root)`.

**Interfaces:**
- Consumes: nothing (first task).
- Produces: each row in `top100_*.json:candidates[]` gains key `recent_mean_return_1d: float | None` — the mean of `outcome_return_1d` over resolved rows in the lookback window, rounded to 4dp, or `None` when `recent_resolved_1d == 0`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_universe_sanitation_mean_return.py`:

```python
"""universe_sanitation: recent_mean_return_1d expectancy field (Task 1).

The watchlist-removal gate needs expectancy, not just accuracy: a symbol can
have a poor hit rate yet positive mean return (rare but large winners), which
is a legitimate profile rather than a decayed one.
"""
from __future__ import annotations

import csv
from pathlib import Path

from portfolio_automation.universe_sanitation import _load_recent_signals

_HEADER = [
    "ticker", "signal_time", "outcome_return_1d", "direction_correct_1d",
]


def _write_outcomes(tmp_path: Path, rows: list[tuple[str, str, str, str]]) -> Path:
    root = tmp_path
    d = root / "outputs" / "performance"
    d.mkdir(parents=True)
    with (d / "signal_outcomes.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(_HEADER)
        for r in rows:
            w.writerow(list(r))
    return root


def test_mean_return_is_arithmetic_mean_of_resolved_rows(tmp_path):
    root = _write_outcomes(tmp_path, [
        ("AAA", "2026-07-20T09:00:00", "1.00", "1"),
        ("AAA", "2026-07-21T09:00:00", "-3.00", "0"),
        ("AAA", "2026-07-22T09:00:00", "2.00", "1"),
    ])
    stats = _load_recent_signals(root, 3650)
    bucket = stats["AAA"]
    assert bucket["resolved_1d"] == 3
    # (1.00 - 3.00 + 2.00) / 3 == 0.0
    assert bucket["return_sum_1d"] == 0.0


def test_unresolved_rows_do_not_contribute_to_the_sum(tmp_path):
    root = _write_outcomes(tmp_path, [
        ("BBB", "2026-07-20T09:00:00", "4.00", "1"),
        ("BBB", "2026-07-21T09:00:00", "", "0"),
    ])
    stats = _load_recent_signals(root, 3650)
    assert stats["BBB"]["resolved_1d"] == 1
    assert stats["BBB"]["return_sum_1d"] == 4.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_universe_sanitation_mean_return.py -v`
Expected: FAIL with `KeyError: 'return_sum_1d'`.

The helper is `_load_recent_signals(root: Path, lookback_days: int)` at line 145
(verified). If the signature has since changed, run
`grep -n "def _load_recent_signals" portfolio_automation/universe_sanitation.py`
and adapt the call before proceeding.

- [ ] **Step 3: Accumulate the return sum**

In `portfolio_automation/universe_sanitation.py`, the bucket initializer currently reads:

```python
                bucket = out.setdefault(t, {
                    "count": 0, "resolved_1d": 0, "hits_1d": 0, "last_signal_time": None,
                })
```

Change it to:

```python
                bucket = out.setdefault(t, {
                    "count": 0, "resolved_1d": 0, "hits_1d": 0,
                    "return_sum_1d": 0.0, "last_signal_time": None,
                })
```

Then the resolved branch currently reads:

```python
                ret = row.get("outcome_return_1d") or ""
                if ret not in ("", None, "—"):
                    try:
                        _ = float(ret)
                        bucket["resolved_1d"] += 1
                        if row.get("direction_correct_1d") in ("1", "1.0", "True", "true"):
                            bucket["hits_1d"] += 1
                    except ValueError:
                        pass
```

Change `_ = float(ret)` to capture the value and add it to the sum:

```python
                ret = row.get("outcome_return_1d") or ""
                if ret not in ("", None, "—"):
                    try:
                        val = float(ret)
                        bucket["resolved_1d"] += 1
                        bucket["return_sum_1d"] += val
                        if row.get("direction_correct_1d") in ("1", "1.0", "True", "true"):
                            bucket["hits_1d"] += 1
                    except ValueError:
                        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_universe_sanitation_mean_return.py -v`
Expected: 2 passed.

- [ ] **Step 5: Write the failing test for the emitted field**

Append to `tests/test_universe_sanitation_mean_return.py`:

```python
def test_emitted_row_carries_mean_return(tmp_path, monkeypatch):
    """The artifact row exposes recent_mean_return_1d rounded to 4dp."""
    from portfolio_automation import universe_sanitation as US

    rec = {"sources": ["static"], "signal": {
        "count": 3, "resolved_1d": 3, "hits_1d": 1,
        "return_sum_1d": -0.9729, "last_signal_time": "2026-07-22T09:00:00",
    }}
    monkeypatch.setattr(US, "_load_sector", lambda root, sym: "Technology")
    # NOTE argument order: (by_sym, root) — not (root, by_sym).
    rows = US._rank_candidates({"RIOT": rec}, Path(tmp_path))

    row = rows[0]
    assert row["recent_resolved_1d"] == 3
    # -0.9729 / 3 == -0.3243
    assert row["recent_mean_return_1d"] == -0.3243


def test_mean_return_is_none_when_nothing_resolved(tmp_path, monkeypatch):
    """None, not 0.0 — a zero mean would read as 'not negative' to the gate."""
    from portfolio_automation import universe_sanitation as US

    rec = {"sources": ["static"], "signal": {
        "count": 2, "resolved_1d": 0, "hits_1d": 0,
        "return_sum_1d": 0.0, "last_signal_time": None,
    }}
    monkeypatch.setattr(US, "_load_sector", lambda root, sym: "Unknown")
    rows = US._rank_candidates({"LLY": rec}, Path(tmp_path))

    assert rows[0]["recent_resolved_1d"] == 0
    assert rows[0]["recent_mean_return_1d"] is None
    assert rows[0]["recent_hit_rate_1d"] is None
```

- [ ] **Step 6: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_universe_sanitation_mean_return.py -v`
Expected: the two new tests FAIL with `KeyError: 'recent_mean_return_1d'`.

The row builder is `_rank_candidates(by_sym: dict, root: Path)` at line 415
(verified — note the argument order is `(by_sym, root)`, which is easy to get
backwards). It sorts by score and assigns `rank`, so a single-entry dict returns
a one-row list.

- [ ] **Step 7: Emit the field**

In the row-building block (near line 424), the hit-rate line currently reads:

```python
        resolved = int(sig.get("resolved_1d") or 0)
        hit_rate = round(int(sig.get("hits_1d") or 0) / resolved, 4) if resolved else None
```

Add the mean directly beneath it:

```python
        resolved = int(sig.get("resolved_1d") or 0)
        hit_rate = round(int(sig.get("hits_1d") or 0) / resolved, 4) if resolved else None
        # Expectancy term consumed by the watchlist-removal gate. None (never 0.0)
        # when nothing resolved: a 0.0 mean would read as "not negative" and a
        # 0.0 hit rate as "worst possible", so the two coercions fail in
        # opposite directions.
        mean_return_1d = (
            round(float(sig.get("return_sum_1d") or 0.0) / resolved, 4) if resolved else None
        )
```

Then in the `rows.append({...})` dict, add the key immediately after
`"recent_hit_rate_1d": hit_rate,`:

```python
            "recent_mean_return_1d": mean_return_1d,
```

- [ ] **Step 8: Run to verify all four pass**

Run: `.venv/bin/python -m pytest tests/test_universe_sanitation_mean_return.py -v`
Expected: 4 passed.

- [ ] **Step 9: Verify against real data**

Run:

```bash
.venv/bin/python -c "
from pathlib import Path
from portfolio_automation.universe_sanitation import _load_recent_signals
s=_load_recent_signals(Path('.'), 30)
for t in ('RIOT','SMCI','MARA'):
    b=s.get(t) or {}
    assert 'return_sum_1d' in b, f'{t}: return_sum_1d MISSING — Step 3 did not take effect'
    r=b['resolved_1d']
    m=b['return_sum_1d']/r if r else None
    print(f'{t:5} resolved={r:3} mean={m:+.4f}' if m is not None else f'{t:5} resolved=0 mean=None')
"
```

Expected (approximately, over the trailing 30 days): RIOT mean **negative**, SMCI mean **positive**, MARA mean negative. The exact values drift daily; the *signs* are what Task 3 depends on. If RIOT is not negative or SMCI is not positive, STOP and report — the gate's premise no longer holds and the spec needs revisiting.

- [ ] **Step 10: Run the touched-area regression**

Run: `.venv/bin/python -m pytest tests/ -k "universe_sanitation or top100" -q`
Expected: all pass (no existing consumer asserts an exact row-key set).

- [ ] **Step 11: Commit**

```bash
git add portfolio_automation/universe_sanitation.py tests/test_universe_sanitation_mean_return.py
git commit -m "feat(universe): emit recent_mean_return_1d expectancy field

The watchlist-removal gate needs expectancy alongside accuracy: a low hit
rate with positive mean return is a legitimate high-variance profile, while
a low hit rate with negative mean return is decay.

Near-free — the resolved branch already computed float(ret) and discarded it.
Emits None (never 0.0) at zero resolved, matching recent_hit_rate_1d.

Additive and observe-only; no existing consumer changes behavior.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Baseline reads the effective runtime watchlist

**Files:**
- Modify: `portfolio_automation/sim_governance/simulation_lane.py:111-160` (`load_production_baseline`)
- Test: `tests/test_sim_lane_effective_baseline.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `load_production_baseline(root: Path) -> dict` where `baseline["watchlist"]` is now the **effective** runtime list — `config.json:watchlist_scanner.watchlist` + active extended-watchlist tickers + approved overlay ops — uppercased, de-duplicated, order-preserving (static first, then extended). Task 3 depends on this being non-empty and on applied removals being absent from it.

**Why:** the loader currently reads `config.json:portfolio.watchlist`, a key that does not exist, so `baseline["watchlist"] == []`. Verified pre-change.

- [ ] **Step 1: Write the failing test**

Create `tests/test_sim_lane_effective_baseline.py`:

```python
"""load_production_baseline: effective runtime watchlist (Task 2).

Before this change the loader read config.json:portfolio.watchlist — a key that
does not exist — so baseline["watchlist"] was always []. The removal producer
needs the list the scanner actually sees, and an applied removal must disappear
from it so the rule stops re-proposing (idempotence).
"""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.sim_governance.simulation_lane import load_production_baseline


def _write_config(root: Path, watchlist: list[str], *, extended_enabled: bool = False) -> None:
    cfg = {
        "watchlist_scanner": {"watchlist": watchlist},
        "extended_watchlist": {"enabled": extended_enabled, "db_path": "data/nonexistent.db"},
    }
    (root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def test_baseline_reads_watchlist_scanner_key(tmp_path):
    _write_config(tmp_path, ["AAPL", "riot", "QQQ"])

    baseline = load_production_baseline(tmp_path)

    assert baseline["watchlist"] == ["AAPL", "RIOT", "QQQ"], "uppercased, order preserved"


def test_baseline_is_empty_when_config_absent(tmp_path):
    """Tolerance is preserved: a missing config degrades, never raises."""
    baseline = load_production_baseline(tmp_path)
    assert baseline["watchlist"] == []


def test_baseline_tolerates_unreadable_extended_db(tmp_path):
    """A broken extended-watchlist DB must not break the whole lane."""
    _write_config(tmp_path, ["AAPL"], extended_enabled=True)

    baseline = load_production_baseline(tmp_path)

    assert baseline["watchlist"] == ["AAPL"], "static list survives an extended-DB failure"


def test_applied_removal_is_absent_from_baseline(tmp_path):
    """The idempotence property Task 3 relies on."""
    _write_config(tmp_path, ["AAPL", "RIOT"])
    ov = tmp_path / "outputs" / "latest"
    ov.mkdir(parents=True)
    (ov / "approved_watchlist_proposals.json").write_text(json.dumps({
        "schema": "approved_watchlist_proposals.v1",
        "feeds_production": True,
        "applied_proposal_ids": ["prop_test"],
        "ops": [{"proposal_id": "prop_test",
                 "change": {"op": "remove", "symbol": "RIOT"}}],
    }), encoding="utf-8")

    baseline = load_production_baseline(tmp_path)

    assert "RIOT" not in baseline["watchlist"]
    assert "AAPL" in baseline["watchlist"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_sim_lane_effective_baseline.py -v`
Expected: `test_baseline_reads_watchlist_scanner_key` and `test_applied_removal_is_absent_from_baseline` FAIL with `assert [] == ['AAPL', 'RIOT', 'QQQ']` — the loader returns an empty list. The two tolerance tests should already pass.

- [ ] **Step 3: Replace the watchlist block**

In `load_production_baseline`, this block:

```python
    # Production watchlist (config + extended). Best-effort.
    cfg = _read_json(root / "config.json") or {}
    pf = cfg.get("portfolio", {}) if isinstance(cfg, dict) else {}
    wl = pf.get("watchlist") if isinstance(pf, dict) else None
    if isinstance(wl, list):
        out["watchlist"] = [str(t).upper() for t in wl if t]
```

becomes:

```python
    # Production watchlist = the EFFECTIVE runtime list the scanner sees:
    # static config + active extended rows + approved overlay ops. Mirrors
    # watchlist_scanner/__main__.py:210-226. Reading the effective list (rather
    # than raw config) is what makes removal proposals idempotent: once a
    # removal is applied the symbol is gone from here, so the decay rule stops
    # re-proposing it. Each source degrades independently to empty.
    cfg = _read_json(root / "config.json") or {}
    ws = cfg.get("watchlist_scanner", {}) if isinstance(cfg, dict) else {}
    wl = ws.get("watchlist") if isinstance(ws, dict) else None
    static_wl = [str(t).upper() for t in wl if t] if isinstance(wl, list) else []

    extended: list[str] = []
    ew_cfg = cfg.get("extended_watchlist", {}) if isinstance(cfg, dict) else {}
    if isinstance(ew_cfg, dict) and ew_cfg.get("enabled", True):
        try:
            from watchlist_scanner.extended_watchlist import ExtendedWatchlist
            _ew = ExtendedWatchlist(
                db_path=ew_cfg.get("db_path", "data/portfolio.db"),
                ttl_days=int(ew_cfg.get("ttl_days", 7)),
                max_symbols=int(ew_cfg.get("max_symbols", 5)),
                confidence_threshold=float(ew_cfg.get("confidence_threshold", 0.80)),
            )
            extended = [str(t).upper() for t in _ew.get_active_tickers()]
        except Exception as exc:  # pragma: no cover - environment dependent
            logger.debug("simulation_lane: extended watchlist unavailable: %s", exc)

    _seen = set(static_wl)
    effective = list(static_wl) + [t for t in extended if t not in _seen]

    try:
        from portfolio_automation.sim_governance.production_overlays import (
            load_production_watchlist,
        )
        _ov = load_production_watchlist(
            effective, base_dir=str(root / "outputs"), enabled=True
        )
        effective = [str(t).upper() for t in _ov.get("watchlist", effective)]
    except Exception as exc:  # pragma: no cover - overlay is best-effort
        logger.debug("simulation_lane: watchlist overlay unavailable: %s", exc)

    out["watchlist"] = effective
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_sim_lane_effective_baseline.py -v`
Expected: 4 passed.

- [ ] **Step 5: Verify against real data and confirm zero blast radius**

Run:

```bash
.venv/bin/python -c "
from pathlib import Path
from portfolio_automation.sim_governance import simulation_lane as SL
b=SL.load_production_baseline(Path('.'))
print('effective watchlist:', len(b['watchlist']), 'RIOT present:', 'RIOT' in b['watchlist'])
for fn in SL.DEFAULT_EXPERIMENTS:
    print(f'  {fn.__name__:42} -> {len(fn(b))} candidate(s)')
"
```

Expected: 27 symbols, `RIOT present: True`, and the experiment counts unchanged from
before this task — `discovery_adds 0`, `rerank 0`, `crowd_context 46`, `flock 20`
(the advisory counts drift daily; what matters is that the two watchlist
experiments stay at 0 and the advisory ones are non-zero). The two watchlist
experiments remain dead because their inputs (`discovery_candidates`,
`watchlist_ranked`) are still unpopulated — that is expected and out of scope.

- [ ] **Step 6: Run the sim-governance regression**

Run: `.venv/bin/python -m pytest tests/test_sim_governance.py tests/test_sim_lane_effective_baseline.py -q`
Expected: all pass. If a test asserted an empty baseline watchlist, read it — a test that encoded the bug should be updated to the corrected expectation, and say so in the commit.

- [ ] **Step 7: Commit**

```bash
git add portfolio_automation/sim_governance/simulation_lane.py tests/test_sim_lane_effective_baseline.py
git commit -m "fix(sim-gov): baseline reads the effective runtime watchlist

load_production_baseline read config.json:portfolio.watchlist — a key that
does not exist. The real list is watchlist_scanner.watchlist, so
baseline[watchlist] was always []. Now assembles what the scanner actually
sees: static config + active extended rows + approved overlay ops, mirroring
watchlist_scanner/__main__.py:210-226.

This makes removal proposals idempotent: once a removal is applied the symbol
is absent from the baseline, so the decay rule stops re-proposing it.

Blast radius is zero on the other experiments: discovery_adds and rerank stay
dead (their inputs discovery_candidates / watchlist_ranked are still
unpopulated — separate follow-up), and crowd_context / flock never read
baseline[watchlist].

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: The decay-removal producer

**Files:**
- Modify: `portfolio_automation/sim_governance/simulation_lane.py` (add function after `experiment_watchlist_rerank`, register in `DEFAULT_EXPERIMENTS`)
- Test: `tests/test_watchlist_decay_removals.py`

**Interfaces:**
- Consumes: `recent_mean_return_1d` from Task 1; `baseline["watchlist"]` (effective list) from Task 2.
- Produces: `experiment_watchlist_decay_removals(baseline: dict) -> list[S.SimulationCandidate]`, registered in `DEFAULT_EXPERIMENTS`. Each candidate carries `proposal_type=S.PROPOSAL_WATCHLIST_REMOVE`, `workflow=S.WORKFLOW_WATCHLIST`, and `proposed_production_change={"op": "remove", "symbol": SYM}` — the shape `production_overlays.apply_approved_watchlist` already consumes at line 76.
- New baseline key: `baseline["universe_ranks"]` — the `candidates` list from `outputs/latest/top100_monthly.json`, loaded by `load_production_baseline`.

**Gate:** `recent_resolved_1d >= 30 AND recent_hit_rate_1d < 0.40 AND recent_mean_return_1d < 0 AND symbol in effective baseline`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_watchlist_decay_removals.py`:

```python
"""experiment_watchlist_decay_removals: the decay gate (Task 3).

Gate: recent_resolved_1d >= 30
  AND recent_hit_rate_1d < 0.40      (decayed accuracy)
  AND recent_mean_return_1d < 0      (negative expectancy)
  AND symbol present in the effective baseline watchlist

Both metric conditions are required. Accuracy alone cannot isolate decay:
SMCI has a LOWER hit rate than RIOT but POSITIVE expectancy (rare, large
winners), which is a legitimate profile rather than decay.
"""
from __future__ import annotations

import pytest

from portfolio_automation.sim_governance import schemas as S
from portfolio_automation.sim_governance.simulation_lane import (
    experiment_watchlist_decay_removals,
)


def _rank(symbol: str, *, resolved: int, hit: float | None, mean: float | None) -> dict:
    return {
        "symbol": symbol,
        "recent_resolved_1d": resolved,
        "recent_hit_rate_1d": hit,
        "recent_mean_return_1d": mean,
        "rank": 29,
        "score": 0.231,
        "sector": "Technology",
    }


def _baseline(ranks: list[dict], watchlist: list[str] | None = None) -> dict:
    return {
        "watchlist": watchlist if watchlist is not None else [r["symbol"] for r in ranks],
        "universe_ranks": ranks,
    }


def test_decayed_symbol_is_proposed_for_removal():
    ranks = [_rank("RIOT", resolved=34, hit=0.3235, mean=-0.324)]

    out = experiment_watchlist_decay_removals(_baseline(ranks))

    assert len(out) == 1
    c = out[0]
    assert c.symbol == "RIOT"
    assert c.proposal_type == S.PROPOSAL_WATCHLIST_REMOVE
    assert c.workflow == S.WORKFLOW_WATCHLIST
    assert c.proposed_production_change == {"op": "remove", "symbol": "RIOT"}


def test_positive_expectancy_is_not_removed_despite_worse_hit_rate():
    """SMCI's real profile: lowest hit rate in the universe, positive mean.

    This test fails if someone 'simplifies' the gate back to hit-rate-only.
    """
    ranks = [_rank("SMCI", resolved=34, hit=0.2647, mean=+0.072)]

    out = experiment_watchlist_decay_removals(_baseline(ranks))

    assert out == []


def test_hit_rate_above_threshold_is_not_removed_despite_worse_expectancy():
    """MARA's real profile: expectancy worse than RIOT, hit rate above the gate."""
    ranks = [_rank("MARA", resolved=34, hit=0.4118, mean=-0.487)]

    out = experiment_watchlist_decay_removals(_baseline(ranks))

    assert out == []


@pytest.mark.parametrize("resolved,expected", [(29, 0), (30, 1)])
def test_sample_size_boundary(resolved, expected):
    ranks = [_rank("AAA", resolved=resolved, hit=0.30, mean=-0.5)]
    assert len(experiment_watchlist_decay_removals(_baseline(ranks))) == expected


@pytest.mark.parametrize("hit,expected", [(0.399, 1), (0.400, 0), (0.401, 0)])
def test_hit_rate_boundary_is_strict_less_than(hit, expected):
    ranks = [_rank("AAA", resolved=34, hit=hit, mean=-0.5)]
    assert len(experiment_watchlist_decay_removals(_baseline(ranks))) == expected


@pytest.mark.parametrize("mean,expected", [(-0.001, 1), (0.0, 0), (0.001, 0)])
def test_mean_return_boundary_is_strict_less_than_zero(mean, expected):
    ranks = [_rank("AAA", resolved=34, hit=0.30, mean=mean)]
    assert len(experiment_watchlist_decay_removals(_baseline(ranks))) == expected


@pytest.mark.parametrize("hit,mean", [(None, -0.5), (0.30, None), (None, None)])
def test_null_metrics_are_skipped_not_coerced(hit, mean):
    """n >= 30 deliberately satisfied, so this fails if the code leans on the n guard.

    A null->0.0 coercion breaks in opposite directions: a 0.0 hit rate reads as
    worst-possible (would remove), a 0.0 mean is not < 0 (would keep).
    """
    ranks = [_rank("AAA", resolved=34, hit=hit, mean=mean)]
    assert experiment_watchlist_decay_removals(_baseline(ranks)) == []


def test_symbol_absent_from_baseline_is_not_proposed():
    """Idempotence: an already-removed symbol is not re-proposed."""
    ranks = [_rank("RIOT", resolved=34, hit=0.3235, mean=-0.324)]

    out = experiment_watchlist_decay_removals(_baseline(ranks, watchlist=["AAPL"]))

    assert out == []


def test_missing_universe_ranks_returns_empty():
    assert experiment_watchlist_decay_removals({"watchlist": ["RIOT"]}) == []


def test_malformed_ranks_do_not_raise():
    bad = {"watchlist": ["RIOT"], "universe_ranks": [
        {"symbol": "RIOT", "recent_resolved_1d": "not-a-number",
         "recent_hit_rate_1d": 0.3, "recent_mean_return_1d": -0.3},
        "not-a-dict",
    ]}
    assert experiment_watchlist_decay_removals(bad) == []


def test_removal_is_never_auto_approvable():
    """Authority invariant: removals stay human-gated by construction."""
    from portfolio_automation.sim_governance.auto_approval import _WATCHLIST_ELIGIBLE_TYPES

    assert S.PROPOSAL_WATCHLIST_REMOVE not in _WATCHLIST_ELIGIBLE_TYPES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_watchlist_decay_removals.py -v`
Expected: FAIL at import — `ImportError: cannot import name 'experiment_watchlist_decay_removals'`. (`test_removal_is_never_auto_approvable` fails only because of the module-level import; it will pass once the function exists.)

- [ ] **Step 3: Load the ranks into the baseline**

In `load_production_baseline`, immediately before `return out`, add:

```python
    # Universe-sanitation monthly ranks — the decay evidence for removals. The
    # MONTHLY cadence is required: build_top100_daily uses lookback_days=1, which
    # zeroes the recent-hit-rate weight, so the daily artifact cannot discriminate.
    ranks = _read_json(root / "outputs" / "latest" / "top100_monthly.json")
    out["universe_ranks"] = (
        ranks.get("candidates") or [] if isinstance(ranks, dict) else []
    )
```

- [ ] **Step 4: Write the producer**

Insert after `experiment_watchlist_rerank` (before `experiment_advisory_crowd_context`):

```python
# Decay gate thresholds. Both metric conditions are required — accuracy alone
# cannot separate decay from a legitimate high-variance profile (a low hit rate
# with positive expectancy means rare but large winners).
_DECAY_MIN_RESOLVED = 30
_DECAY_MAX_HIT_RATE = 0.40
_UNIVERSE_RANK_EVIDENCE = "outputs/latest/top100_monthly.json"


def _decay_metric(row: dict, key: str) -> float | None:
    """Return a numeric metric, or None when absent/null/non-numeric.

    Never coerces to 0.0: a 0.0 hit rate would read as worst-possible (removing
    every no-history symbol) and a 0.0 mean return is not < 0 (keeping a symbol
    that should go). The two coercions fail in opposite directions, so a missing
    value must disqualify the row outright.
    """
    val = row.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def experiment_watchlist_decay_removals(baseline: dict) -> list[S.SimulationCandidate]:
    """Propose removing watchlist members decayed on BOTH accuracy and expectancy.

    Gate: recent_resolved_1d >= 30 AND recent_hit_rate_1d < 0.40
      AND recent_mean_return_1d < 0 AND the symbol is on the effective watchlist.

    Static config entries bypass every quality screen the FMP candidate path
    applies (min_mkt_cap / rev-growth / 200dma), so without this producer the
    universe can grow but never shrink. Removal stays human-gated: this emits a
    candidate, never an approval.
    """
    effective = {str(t).upper() for t in baseline.get("watchlist", []) or []}
    cands: list[S.SimulationCandidate] = []

    for row in baseline.get("universe_ranks", []) or []:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).upper()
        if not sym or sym not in effective:
            continue

        try:
            resolved = int(row.get("recent_resolved_1d") or 0)
        except (TypeError, ValueError):
            continue
        if resolved < _DECAY_MIN_RESOLVED:
            continue

        hit = _decay_metric(row, "recent_hit_rate_1d")
        mean = _decay_metric(row, "recent_mean_return_1d")
        if hit is None or mean is None:
            continue
        if not (hit < _DECAY_MAX_HIT_RATE and mean < 0):
            continue

        cid = S.make_candidate_id(
            S.PROPOSAL_WATCHLIST_REMOVE, sym, salt=f"{resolved}:{hit:.4f}:{mean:.4f}"
        )
        cands.append(S.SimulationCandidate(
            candidate_id=cid,
            workflow=S.WORKFLOW_WATCHLIST,
            proposal_type=S.PROPOSAL_WATCHLIST_REMOVE,
            symbol=sym,
            what_changed=f"Remove {sym} from the watchlist",
            why_changed=(
                f"Decayed on both accuracy and expectancy over the trailing "
                f"universe-sanitation window: hit rate {hit:.2%} (< 40%) with "
                f"mean 1d return {mean:+.3f}% (negative) across {resolved} "
                f"resolved signals. A low hit rate alone is not decay — negative "
                f"expectancy is what distinguishes it from a high-variance profile."
            ),
            source_evidence=[_UNIVERSE_RANK_EVIDENCE],
            production_baseline=sym,
            simulated_value=None,
            risk_impact="medium",
            confidence=round(min(1.0, (_DECAY_MAX_HIT_RATE - hit) / _DECAY_MAX_HIT_RATE + 0.5), 4),
            data_quality="ok",
            ready_for_production_review=True,
            proposed_production_change={"op": "remove", "symbol": sym},
            metadata={
                "recent_resolved_1d": resolved,
                "recent_hit_rate_1d": hit,
                "recent_mean_return_1d": mean,
                "universe_rank": row.get("rank"),
                "sector": row.get("sector"),
            },
        ))
    return cands
```

- [ ] **Step 5: Register the experiment**

`DEFAULT_EXPERIMENTS` currently reads:

```python
DEFAULT_EXPERIMENTS: list[Experiment] = [
    experiment_watchlist_discovery_adds,
    experiment_watchlist_rerank,
    experiment_advisory_crowd_context,
    experiment_flock_intelligence,
]
```

becomes:

```python
DEFAULT_EXPERIMENTS: list[Experiment] = [
    experiment_watchlist_discovery_adds,
    experiment_watchlist_rerank,
    experiment_watchlist_decay_removals,
    experiment_advisory_crowd_context,
    experiment_flock_intelligence,
]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_watchlist_decay_removals.py -v`
Expected: all pass (16 including parametrized cases).

- [ ] **Step 7: Verify against real data — RIOT only**

Run:

```bash
.venv/bin/python -c "
from pathlib import Path
from portfolio_automation.sim_governance import simulation_lane as SL
b=SL.load_production_baseline(Path('.'))
out=SL.experiment_watchlist_decay_removals(b)
print('proposed removals:', [c.symbol for c in out])
for c in out:
    print(' ', c.symbol, c.proposed_production_change, c.metadata)
"
```

Expected: exactly `['RIOT']`. If SMCI or MARA appears, STOP — re-check the two
metric conditions against the Task 1 verification output before continuing.

- [ ] **Step 8: Verify the simulated view shrinks**

Run:

```bash
.venv/bin/python -c "
from pathlib import Path
from portfolio_automation.sim_governance import simulation_lane as SL
b=SL.load_production_baseline(Path('.'))
cands=SL.experiment_watchlist_decay_removals(b)
views=SL.materialize_simulated_views(b, cands)
print('baseline size :', len(b['watchlist']), 'RIOT in:', 'RIOT' in b['watchlist'])
print('simulated size:', len(views['simulated_watchlist']), 'RIOT in:', 'RIOT' in views['simulated_watchlist'])
"
```

Expected: baseline 27 with RIOT present; simulated 26 with RIOT **absent**
(`materialize_simulated_views:413` already handles `op == "remove"`).

- [ ] **Step 9: Run the sim-governance regression**

Run: `.venv/bin/python -m pytest tests/test_sim_governance.py tests/test_watchlist_decay_removals.py tests/test_sim_lane_effective_baseline.py -q`
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add portfolio_automation/sim_governance/simulation_lane.py tests/test_watchlist_decay_removals.py
git commit -m "feat(sim-gov): watchlist decay-removal producer

Adds experiment_watchlist_decay_removals, the first functioning watchlist
experiment. PROPOSAL_WATCHLIST_REMOVE had a schema, rollback text, applier and
live wiring (apply_watchlist_overlay=true) but no producer, so the universe
could grow but never shrink.

Gate: recent_resolved_1d >= 30 AND recent_hit_rate_1d < 0.40 AND
recent_mean_return_1d < 0. Both metric conditions are required — SMCI has a
LOWER hit rate than RIOT but positive expectancy (rare large winners), which
is a legitimate profile, not decay. Verified on real data: proposes RIOT only.

Null metrics are skipped, never coerced to 0.0 — the two coercions fail in
opposite directions (0.0 hit rate reads worst-possible; 0.0 mean is not < 0).

Removal stays human-gated: PROPOSAL_WATCHLIST_REMOVE is absent from
auto_approval._WATCHLIST_ELIGIBLE_TYPES, pinned by a regression test.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Composition-break probe on applied removal

**Files:**
- Modify: `portfolio_automation/quant_watch_probes.py` (add `register_universe_composition_break`)
- Test: `tests/test_universe_composition_break_probe.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (operates on the ledger + a symbol).
- Produces: `register_universe_composition_break(root: str | Path, symbol: str, fingerprint: str, now_iso: str, *, created_run: str = "watchlist_removal", sample_share: float | None = None) -> dict` returning `{"registered": bool, "probe_id": str, "reason": str}`. Idempotent per `(symbol, fingerprint)`.

**Why:** the watchlist is not in `_TRACKED_KNOBS`, so a removal does not mint a new gauge fingerprint. Pre- and post-removal samples pool under the same fingerprint and its hit rate drifts upward by the removed sample share (RIOT ≈ 3.75% of 881 resolved) purely from composition — indistinguishable from genuine improvement. The probe makes any later attribution read self-warn.

- [ ] **Step 1: Write the failing test**

Create `tests/test_universe_composition_break_probe.py`:

```python
"""Universe composition-break probe (Task 4).

The watchlist is not part of the gauge fingerprint (_TRACKED_KNOBS), so removing
a symbol does NOT mint a new fingerprint. Pre- and post-removal samples pool
under the same fingerprint and its hit rate drifts up by the removed sample
share, indistinguishable from real improvement. This probe records the break.
"""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.quant_watch_probes import (
    load_ledger,
    register_universe_composition_break,
)

_NOW = "2026-07-28T14:00:00+00:00"


def _ledger_path(root: Path) -> Path:
    p = root / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p / "quant_watch_ledger.json"


def test_registers_a_probe_for_an_applied_removal(tmp_path):
    _ledger_path(tmp_path).write_text(json.dumps({"active": [], "archive": []}), encoding="utf-8")

    res = register_universe_composition_break(
        tmp_path, "RIOT", "5687885c755dd6c9", _NOW, sample_share=0.0375
    )

    assert res["registered"] is True
    led = load_ledger(_ledger_path(tmp_path))
    probe = next(p for p in led["active"] if p["id"] == res["probe_id"])
    assert probe["detector"] == "manual"
    assert probe["severity"] == "amber"
    assert "RIOT" in probe["concern"]
    assert "5687885c755dd6c9" in probe["concern"]
    assert probe["trigger_snapshot"]["symbol"] == "RIOT"
    assert probe["trigger_snapshot"]["sample_share"] == 0.0375


def test_is_idempotent_for_the_same_symbol_and_fingerprint(tmp_path):
    _ledger_path(tmp_path).write_text(json.dumps({"active": [], "archive": []}), encoding="utf-8")

    first = register_universe_composition_break(tmp_path, "RIOT", "fp_a", _NOW)
    second = register_universe_composition_break(tmp_path, "RIOT", "fp_a", _NOW)

    assert first["registered"] is True
    assert second["registered"] is False
    assert second["reason"] == "already_registered"
    led = load_ledger(_ledger_path(tmp_path))
    assert sum(1 for p in led["active"] if p["id"] == first["probe_id"]) == 1


def test_a_second_symbol_registers_its_own_probe(tmp_path):
    """MARA is expected to qualify later; each removal gets its own record."""
    _ledger_path(tmp_path).write_text(json.dumps({"active": [], "archive": []}), encoding="utf-8")

    a = register_universe_composition_break(tmp_path, "RIOT", "fp_a", _NOW)
    b = register_universe_composition_break(tmp_path, "MARA", "fp_a", _NOW)

    assert a["probe_id"] != b["probe_id"]
    led = load_ledger(_ledger_path(tmp_path))
    assert len(led["active"]) == 2


def test_missing_ledger_does_not_raise(tmp_path):
    res = register_universe_composition_break(tmp_path, "RIOT", "fp_a", _NOW)
    assert res["registered"] in (True, False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_universe_composition_break_probe.py -v`
Expected: FAIL at import — `ImportError: cannot import name 'register_universe_composition_break'`.

- [ ] **Step 3: Implement the registrar**

Append to `portfolio_automation/quant_watch_probes.py`:

```python
def register_universe_composition_break(
    root: str | Path,
    symbol: str,
    fingerprint: str,
    now_iso: str,
    *,
    created_run: str = "watchlist_removal",
    sample_share: float | None = None,
) -> dict:
    """Record that a watchlist removal changed the resolved-sample composition.

    The watchlist is NOT part of the gauge fingerprint (retune_impact_tracker
    _TRACKED_KNOBS), so a removal does not mint a new fingerprint: pre- and
    post-removal samples pool under the same one and its hit rate / mean return
    drift upward by roughly the removed sample share purely from composition
    change — indistinguishable from a genuine gauge improvement.

    Registering a probe makes any later attribution read of that fingerprint
    self-warn and split the window. Idempotent per (symbol, fingerprint).

    Returns {"registered": bool, "probe_id": str, "reason": str}.
    """
    sym = str(symbol).upper()
    probe_id = f"manual:universe_composition_break_{sym}_{fingerprint}"
    path = Path(root) / "data" / "quant_watch_ledger.json"

    try:
        led = load_ledger(path)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("quant_watch: composition-break ledger read failed: %s", exc)
        return {"registered": False, "probe_id": probe_id, "reason": f"ledger_error: {exc}"}

    active = led.setdefault("active", [])
    if any(p.get("id") == probe_id for p in active):
        return {"registered": False, "probe_id": probe_id, "reason": "already_registered"}

    share_txt = f" ({sample_share:.2%} of resolved samples)" if sample_share is not None else ""
    active.append({
        "id": probe_id,
        "detector": "manual",
        "lens": "quant",
        "scope_key": f"universe_composition_break_{sym}",
        "created_at": now_iso,
        "created_run": created_run,
        "severity": "amber",
        "concern": (
            f"{sym} was removed from the watchlist on {now_iso[:10]}{share_txt}. "
            f"The watchlist is not part of the gauge fingerprint, so gauge "
            f"{fingerprint} pools pre- and post-removal samples: its hit rate and "
            f"mean return will drift upward from composition change alone, which "
            f"is NOT evidence of gauge improvement. Attribution reads for "
            f"{fingerprint} must split the window at this date."
        ),
        "trigger_snapshot": {
            "symbol": sym,
            "fingerprint": fingerprint,
            "removed_at": now_iso,
            "sample_share": sample_share,
        },
        "resolve_hint": (
            "Retire once a NEW gauge fingerprint has accumulated its own samples "
            "post-removal, so attribution no longer spans the composition break."
        ),
        "observations": [],
    })

    try:
        write_ledger(path, led)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("quant_watch: composition-break ledger write failed: %s", exc)
        return {"registered": False, "probe_id": probe_id, "reason": f"write_error: {exc}"}

    return {"registered": True, "probe_id": probe_id, "reason": "ok"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_universe_composition_break_probe.py -v`
Expected: 4 passed.

If `write_ledger` or `logger` is not defined at module scope, check with
`grep -n "^def write_ledger\|^logger" portfolio_automation/quant_watch_probes.py`
and use the real names.

- [ ] **Step 5: Run the quant-watch regression**

Run: `.venv/bin/python -m pytest tests/ -k "quant_watch" -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add portfolio_automation/quant_watch_probes.py tests/test_universe_composition_break_probe.py
git commit -m "feat(quant-watch): universe composition-break probe

The watchlist is not in _TRACKED_KNOBS, so removing a symbol does not mint a
new gauge fingerprint. Pre- and post-removal samples pool under the same
fingerprint and its hit rate drifts upward by the removed sample share (RIOT
~3.75% of the current gauge's 881 resolved rows) purely from composition —
indistinguishable from genuine improvement.

register_universe_composition_break records the break so later attribution
reads self-warn and split the window. Idempotent per (symbol, fingerprint), so
each future removal gets its own record.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Health-check pairing and docs

**Files:**
- Modify: `.claude/commands/daily-tool-analysis.md` (artifacts read, §6n body line, dispatch trigger)
- Modify: `.claude/agents/portfolio-discovery-health.md` (universe-decay audit duty)
- Modify: `docs/superpowers/specs/2026-07-28-watchlist-remove-producer-design.md` (mark implemented)

**Interfaces:**
- Consumes: the producer from Task 3 and the probe from Task 4.
- Produces: no code interfaces — operator-facing surface only.

**Why:** CLAUDE.md's Analysis + Health Coverage Requirement — a feature without a paired check is incomplete. Cadence is daily (the sim lane is Stage 10e), so the owning skill is `daily-tool-analysis`; the lens is developer + process-analyst.

- [ ] **Step 1: Read the current §6n sim-gov section**

Run: `grep -n "6n. Sim-governance" -A 12 .claude/commands/daily-tool-analysis.md`

Note the exact heartbeat-line format so the addition matches its grammar.

- [ ] **Step 2: Add the removal line to §6n**

In `.claude/commands/daily-tool-analysis.md`, immediately after the `6n2. Sim-gov backlog review` block, insert:

```markdown
6n4. Watchlist decay-removals (always when `sim_gov_ran`; else omit): `"Watchlist-decay: {n} removal candidate(s){: <csv of symbols> if any} · composition-break probes {k} active"` — the decay-removal producer (`simulation_lane.experiment_watchlist_decay_removals`, added 2026-07-28). Gate is `recent_resolved_1d >= 30 AND recent_hit_rate_1d < 0.40 AND recent_mean_return_1d < 0` over `top100_monthly.json`; it proposes only symbols on the EFFECTIVE watchlist, so an applied removal self-suppresses. Zero candidates is the healthy steady state (report, don't alert). **AMBER** when: a removal candidate has been pending human approval > 7 days (operator decision-queue aging), OR `recent_mean_return_1d` is missing from `top100_monthly.json` while `recent_hit_rate_1d` is present (the Task-1 expectancy field regressed → the gate silently proposes nothing) → dispatch `portfolio-discovery-health`. **RED** only if a removal appears in `production_application_state.applied` with no matching valid human approval (contract breach), or if `PROPOSAL_WATCHLIST_REMOVE` ever appears in `auto_approval._WATCHLIST_ELIGIBLE_TYPES` (authority breach) → escalate immediately. A removal is NEVER auto-approvable by construction. Source: `outputs/sandbox/sim_governance/simulation_candidates.json` + `outputs/latest/top100_monthly.json` + `data/quant_watch_ledger.json`.
```

- [ ] **Step 3: Add the content-liveness dispatch trigger**

In the `portfolio-discovery-health IF any of:` list, append:

```markdown
- `top100_monthly.json:candidates[]` rows carry `recent_hit_rate_1d` but are MISSING `recent_mean_return_1d` (the decay-removal gate's expectancy term regressed — the gate fails closed and silently proposes nothing, so the universe stops shrinking with no error). Pass the affected row count so the agent can confirm whether `universe_sanitation` stopped emitting the field or the artifact is stale.
```

- [ ] **Step 4: Add the audit duty to the agent**

In `.claude/agents/portfolio-discovery-health.md`, add to its audit list:

```markdown
- **Universe decay-removal health.** Confirm `top100_monthly.json:candidates[]` carries BOTH `recent_hit_rate_1d` and `recent_mean_return_1d` (the decay gate needs both; a missing expectancy field makes the gate fail closed and silently propose nothing). Verify any pending `watchlist_remove` proposal against its evidence, and check that applied removals carry a valid human approval — a removal can never be auto-approved. Report a symbol whose metrics have recovered above the gate while a removal proposal is still pending, since that proposal is now stale evidence.
```

- [ ] **Step 5: Mark the spec implemented**

In `docs/superpowers/specs/2026-07-28-watchlist-remove-producer-design.md`, change the status line:

```markdown
**Status:** approved (operator, 2026-07-28)
```

to:

```markdown
**Status:** implemented 2026-07-28 (plan: `docs/superpowers/plans/2026-07-28-watchlist-remove-producer.md`)
```

- [ ] **Step 6: Verify the docs reference only real symbols**

Run:

```bash
.venv/bin/python -c "
from portfolio_automation.sim_governance import simulation_lane as SL
from portfolio_automation.sim_governance.auto_approval import _WATCHLIST_ELIGIBLE_TYPES
from portfolio_automation.sim_governance import schemas as S
from portfolio_automation.quant_watch_probes import register_universe_composition_break
assert hasattr(SL, 'experiment_watchlist_decay_removals')
assert SL.experiment_watchlist_decay_removals in SL.DEFAULT_EXPERIMENTS
assert S.PROPOSAL_WATCHLIST_REMOVE not in _WATCHLIST_ELIGIBLE_TYPES
print('all symbols referenced by the docs exist; authority invariant holds')
"
```

Expected: the confirmation line, no assertion error.

- [ ] **Step 7: Back up the registry, then run the full suite**

```bash
cp config/signal_registry.yaml /tmp/sr.bak
.venv/bin/python -m pytest -q 2>&1 | tail -15
diff -q /tmp/sr.bak config/signal_registry.yaml || cp /tmp/sr.bak config/signal_registry.yaml
```

Expected: `9 failed, <N> passed`, and the 9 are exactly the documented pre-existing set (`tuning_proposals` x2, `run_loop`, `operator_control` x2, `operator_worker_runner` x2, `data_budget_governor`, `social_sentiment/quality_gates`). Any other failure is a regression — fix before committing.

- [ ] **Step 8: Commit**

```bash
git add .claude/commands/daily-tool-analysis.md .claude/agents/portfolio-discovery-health.md docs/superpowers/specs/2026-07-28-watchlist-remove-producer-design.md
git commit -m "docs(health): pair watchlist decay-removals with a daily check

CLAUDE.md Analysis + Health Coverage Requirement: the producer runs daily
(sim lane Stage 10e), so daily-tool-analysis owns the check.

Adds §6n4 heartbeat, a content-liveness dispatch trigger for the case where
recent_mean_return_1d goes missing (the gate then fails closed and silently
proposes nothing — no error, universe just stops shrinking), and the matching
audit duty on portfolio-discovery-health.

RED reserved for the two contract breaches: an applied removal with no valid
human approval, or PROPOSAL_WATCHLIST_REMOVE becoming auto-approvable.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Post-implementation

Not part of any task — operator decisions:

1. **Approving the RIOT removal.** The producer only proposes. Approval is human-gated via `promotion_approvals.record_approval(<proposal_id>, "approve", "pesantez", <now>, base_dir="/opt/stockbot/outputs")`. Note `base_dir` must be `<root>/outputs`; a repo-root path is refused by the guard.
2. **Timing.** The spec recommends approving just after the next retune so the composition break lands on a fingerprint boundary, which is cleaner than annotating a mid-window break.
3. **Applying.** `production_application.apply_approved_proposals(now, base_dir="/opt/stockbot/outputs")` — or wait for the next daily cron.
4. **Register the probe** after a successful apply (Task 4 provides the function; wiring it into the apply path automatically is deliberately out of scope, since the apply is an operator action).

## Follow-ups (out of scope, from the spec)

1. Wire `discovery_candidates` to revive `experiment_watchlist_discovery_adds`.
2. Wire `watchlist_ranked` to revive `experiment_watchlist_rerank`.
3. Advisory report: static seats failing the live `min_mkt_cap` $5B screen (gives MARA a principled exit — its cap is $4.62B).
4. `build_top100_daily(lookback_days=1)` zeroes the `_W_RECENT_HITRATE` term, so all tickers tie and the daily artifact cannot discriminate.
5. Modernize the stale fixture in `tests/test_gui_dashboard_memo.py:83,87`.
