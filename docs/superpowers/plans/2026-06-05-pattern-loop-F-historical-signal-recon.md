# Sub-project F — Historical Signal Reconstruction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (or subagent-driven-development). Steps use checkbox (`- [ ]`).

**Goal:** Reconstruct a multi-year historical signal set from the 5y OHLCV archive, point-in-time and look-ahead-audited, so the existing loop matures its OOS window now and (when the audit is clean) E auto-apply runs full-auto on real-but-proxy evidence.

**Architecture:** A pure per-ticker reconstructor recomputes pattern families (`STRONG_MOVE_UP/DOWN`, `VOLUME_SPIKE`) from trailing OHLCV using `config.json event_thresholds`; a truncation-equality audit proves no future data leaked; reconstructed signals are written as snapshot-compatible `outputs/backtest/recon/<date>/watchlist_signals.json` consumed by the existing `run_loop --history`; `auto_apply` gains one fail-closed precondition that reconstructed evidence may drive an apply only when the audit is clean.

**Tech Stack:** Python 3.12 stdlib; pytest; reuses `portfolio_automation.historical_backfill`, `backtesting.signal_sources`, `backtesting.walk_forward`, `backtesting.direction_resolution`.

**Spec:** `docs/superpowers/specs/2026-06-05-pattern-loop-F-historical-signal-recon-design.md`

**Conventions:** branch off `main` (`git checkout -b feature/pattern-loop-F main`); interpreter `/opt/stockbot/.venv/bin/python`; tests fully offline (no FMP/network); never touch `config/signal_registry.yaml`.

**Archive row shape** (`outputs/backtest/historical/<TICKER>_5y.json` → `payload["rows"]`): each row `{date, open, high, low, close, adjClose, volume, change, changePercent}`, **newest-first** (sort ascending before walking).

---

### Task F1: per-ticker reconstructor `reconstruct_signals`

**Files:** Create `backtesting/historical_signal_recon.py`; Test `tests/test_historical_signal_recon.py`

- [ ] **Step 1: failing tests**

```python
from backtesting.historical_signal_recon import reconstruct_signals


def _row(d, close, volume=1_000_000):
    return {"date": d, "close": close, "volume": volume}


def test_strong_move_up_emitted_on_threshold_breach():
    rows = [_row("2026-01-02", 100.0), _row("2026-01-03", 104.0)]  # +4% >= 3%
    sigs = reconstruct_signals("AAA", rows)
    assert len(sigs) == 1
    s = sigs[0]
    assert s["ticker"] == "AAA" and s["scan_time"] == "2026-01-03"
    assert "price_move" in s["alert_basis"]
    assert s["pattern"] == "STRONG_MOVE"
    assert s["direction"] == "up"
    assert s["signal_score"] is None and s["source"] == "historical_reconstruction"


def test_strong_move_down_direction():
    rows = [_row("2026-01-02", 100.0), _row("2026-01-03", 96.0)]  # -4%
    s = reconstruct_signals("AAA", rows)[0]
    assert s["direction"] == "down"


def test_sub_threshold_emits_nothing():
    rows = [_row("2026-01-02", 100.0), _row("2026-01-03", 101.0)]  # +1% < 3%
    assert reconstruct_signals("AAA", rows) == []


def test_volume_spike_emitted():
    rows = [_row(f"2026-01-{d:02d}", 100.0, volume=1_000_000) for d in range(2, 22)]
    rows.append(_row("2026-01-22", 100.5, volume=3_000_000))  # 3x avg, price flat
    sigs = reconstruct_signals("AAA", rows, vol_window=20)
    spike = [s for s in sigs if "volume_spike" in s["alert_basis"]]
    assert spike and spike[-1]["scan_time"] == "2026-01-22"


def test_newest_first_input_is_sorted():
    rows = [_row("2026-01-03", 104.0), _row("2026-01-02", 100.0)]  # reversed
    assert reconstruct_signals("AAA", rows)[0]["scan_time"] == "2026-01-03"


def test_today_guard_excludes_future_dates():
    rows = [_row("2026-01-02", 100.0), _row("2026-01-03", 104.0), _row("2026-01-04", 110.0)]
    sigs = reconstruct_signals("AAA", rows, today="2026-01-03")
    assert all(s["scan_time"] <= "2026-01-03" for s in sigs)


def test_empty_and_short_series_no_raise():
    assert reconstruct_signals("AAA", []) == []
    assert reconstruct_signals("AAA", [_row("2026-01-02", 100.0)]) == []
```

- [ ] **Step 2:** Run → FAIL (ImportError).
  `/opt/stockbot/.venv/bin/python -m pytest tests/test_historical_signal_recon.py -q`

- [ ] **Step 3: implement**

```python
"""
Historical signal reconstruction  (additive | advisory-only | observe-only)

Pattern-Improvement Loop — sub-project F. Recomputes pattern-family signals
(STRONG_MOVE_UP/DOWN, VOLUME_SPIKE) point-in-time from archived OHLCV so the
walk-forward OOS window can mature without waiting for live signal history.

Look-ahead-safe BY CONSTRUCTION: each emitted date uses only rows at or before it
(price move from the prior close; volume vs the trailing window; an optional
`today` hard-stop). signal_score/confidence are deferred (emitted None). Outcomes
(forward returns) are computed downstream by the backtester and are future by
definition — that is the label, not leakage. Pure/total; never raises.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backtesting.signal_sources import _map_basis, _representative_pattern

_OBSERVE_ONLY = True
_SOURCE = "historical_reconstruction"


def _sorted_series(rows: list[dict]) -> list[dict]:
    clean = [r for r in rows if r.get("date") and r.get("close") is not None]
    return sorted(clean, key=lambda r: str(r["date"])[:10])


def reconstruct_signals(
    ticker: str,
    rows: list[dict],
    *,
    strong_move_pct: float = 3.0,
    volume_spike_factor: float = 2.0,
    vol_window: int = 20,
    today: str | None = None,
) -> list[dict]:
    """Reconstruct pattern-family signals for one ticker from its OHLCV rows
    (any order). Returns a list of harness signal dicts dated point-in-time."""
    series = _sorted_series(rows)
    out: list[dict] = []
    for i in range(1, len(series)):
        d = str(series[i]["date"])[:10]
        if today is not None and d > today:
            break
        try:
            prev_close = float(series[i - 1]["close"])
            close = float(series[i]["close"])
        except (TypeError, ValueError):
            continue
        if prev_close <= 0:
            continue
        ret_pct = (close - prev_close) / prev_close * 100.0

        basis: list[str] = []
        direction = "up" if ret_pct >= 0 else "down"
        if abs(ret_pct) >= strong_move_pct:
            basis.append("price_move")

        window = series[max(0, i - vol_window):i]
        vols = [float(r["volume"]) for r in window
                if r.get("volume") not in (None, "")]
        avg_vol = sum(vols) / len(vols) if vols else 0.0
        try:
            vol = float(series[i].get("volume") or 0.0)
        except (TypeError, ValueError):
            vol = 0.0
        if avg_vol > 0 and vol / avg_vol >= volume_spike_factor:
            basis.append("volume_spike")

        if not basis:
            continue
        patterns = _map_basis(basis)
        out.append({
            "ticker": str(ticker).upper(),
            "scan_time": d,
            "alert_basis": basis,
            "pattern": _representative_pattern(patterns),
            "patterns": patterns,
            "direction": direction,
            "signal_score": None,
            "confidence_score": None,
            "price_change_pct": round(ret_pct, 4),
            "source": _SOURCE,
        })
    return out
```

- [ ] **Step 4:** Run → PASS. `py_compile`. 
- [ ] **Step 5: commit**
  `git add backtesting/historical_signal_recon.py tests/test_historical_signal_recon.py && git commit -m "feat(backtesting): F1 point-in-time per-ticker signal reconstructor"`

---

### Task F2: universe reconstruction `reconstruct_universe`

**Files:** Modify `backtesting/historical_signal_recon.py`; Test same file.

- [ ] **Step 1: failing test** (append)

```python
import json
from pathlib import Path
from backtesting.historical_signal_recon import reconstruct_universe


def _archive(dirpath: Path, ticker: str, rows: list[dict]):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / f"{ticker}_5y.json").write_text(json.dumps({"symbol": ticker, "rows": rows}))


def test_reconstruct_universe_writes_snapshots(tmp_path):
    arch = tmp_path / "historical"
    _archive(arch, "AAA", [{"date": "2026-01-02", "close": 100.0, "volume": 1_000_000},
                           {"date": "2026-01-03", "close": 104.0, "volume": 1_000_000}])
    recon = tmp_path / "recon"
    summary = reconstruct_universe(str(arch), str(recon))
    assert summary["status"] == "ok"
    assert summary["signals_total"] >= 1
    snap = recon / "2026-01-03" / "watchlist_signals.json"
    assert snap.exists()
    doc = json.loads(snap.read_text())
    assert doc["results"][0]["ticker"] == "AAA"


def test_reconstruct_universe_no_archive(tmp_path):
    summary = reconstruct_universe(str(tmp_path / "nope"), str(tmp_path / "recon"))
    assert summary["status"] == "no_prices"
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: implement** (append to module)

```python
def reconstruct_universe(
    archive_dir: str,
    recon_dir: str,
    *,
    strong_move_pct: float = 3.0,
    volume_spike_factor: float = 2.0,
    vol_window: int = 20,
    today: str | None = None,
) -> dict[str, Any]:
    """Reconstruct signals for every <TICKER>_5y.json in archive_dir and write
    snapshot-compatible recon_dir/<date>/watchlist_signals.json files (the shape
    signal_sources.load_historical_signal_snapshots reads). Never raises."""
    adir = Path(archive_dir)
    archives = sorted(adir.glob("*_5y.json")) if adir.is_dir() else []
    if not archives:
        return {"observe_only": _OBSERVE_ONLY, "status": "no_prices",
                "tickers": 0, "signals_total": 0, "archive_dir": archive_dir}

    by_date: dict[str, list[dict]] = {}
    tickers = 0
    for arc in archives:
        try:
            payload = json.loads(arc.read_text(encoding="utf-8"))
            ticker = str(payload.get("symbol") or arc.stem.split("_")[0])
            rows = payload.get("rows") or []
            sigs = reconstruct_signals(
                ticker, rows, strong_move_pct=strong_move_pct,
                volume_spike_factor=volume_spike_factor, vol_window=vol_window, today=today)
        except Exception:
            continue
        tickers += 1
        for s in sigs:
            by_date.setdefault(s["scan_time"], []).append(s)

    rdir = Path(recon_dir)
    signals_total = 0
    for d, sigs in by_date.items():
        out_dir = rdir / d
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "watchlist_signals.json").write_text(
            json.dumps({"results": sigs, "source": _SOURCE}), encoding="utf-8")
        signals_total += len(sigs)

    dates = sorted(by_date)
    span = 0
    if dates:
        from datetime import date as _date
        span = _date.fromisoformat(dates[-1]).toordinal() - _date.fromisoformat(dates[0]).toordinal()
    return {"observe_only": _OBSERVE_ONLY, "status": "ok", "tickers": tickers,
            "signals_total": signals_total, "dates": len(dates), "span_days": span,
            "recon_dir": recon_dir}
```

- [ ] **Step 4:** Run → PASS. `py_compile`.
- [ ] **Step 5: commit** `feat(backtesting): F2 universe reconstruction → snapshot-compatible recon dir`

---

### Task F3: look-ahead audit `assert_no_lookahead`

**Files:** Modify `backtesting/historical_signal_recon.py`; Test `tests/test_lookahead_audit.py`

- [ ] **Step 1: failing tests** — the critical safety test injects a future-peek.

```python
import json
from pathlib import Path
from backtesting.historical_signal_recon import assert_no_lookahead, reconstruct_signals


def _series():
    return [{"date": f"2026-02-{d:02d}", "close": 100.0 + d, "volume": 1_000_000}
            for d in range(1, 25)]


def test_clean_reconstructor_passes_audit():
    rep = assert_no_lookahead({"AAA": _series()})
    assert rep["look_ahead_clean"] is True
    assert rep["mismatches"] == []


def test_future_peek_is_caught():
    # A leaky reconstructor that uses the NEXT day's close → must be flagged.
    def leaky(ticker, rows, **kw):
        sigs = reconstruct_signals(ticker, rows, **kw)
        s = _sorted = sorted(rows, key=lambda r: r["date"])
        # tag a signal using a future row to simulate leakage
        if len(s) >= 2:
            sigs.append({"ticker": ticker, "scan_time": s[0]["date"],
                         "alert_basis": ["price_move"], "pattern": "STRONG_MOVE",
                         "patterns": ["STRONG_MOVE"], "direction": "up",
                         "signal_score": None, "confidence_score": None,
                         "future_close": s[-1]["close"], "source": "historical_reconstruction"})
        return sigs
    rep = assert_no_lookahead({"AAA": _series()}, reconstructor=leaky, sample=3)
    assert rep["look_ahead_clean"] is False
    assert rep["mismatches"]
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: implement** (append). The invariant: signals for date D from the FULL series, filtered to D, must equal signals from the series TRUNCATED at D.

```python
def _signals_for_date(sigs: list[dict], d: str) -> list[dict]:
    # Compare on the load-bearing fields only (ignore incidental ordering).
    keyed = [{k: s.get(k) for k in ("ticker", "scan_time", "alert_basis", "pattern",
                                    "patterns", "direction")}
             for s in sigs if s.get("scan_time") == d]
    return sorted(keyed, key=lambda s: (s["ticker"], str(s["pattern"])))


def assert_no_lookahead(
    series_by_ticker: dict[str, list[dict]],
    *,
    reconstructor=reconstruct_signals,
    sample: int = 10,
    **recon_kw,
) -> dict[str, Any]:
    """Prove the reconstructor uses only data <= D: for a sample of dates D, the
    signals it emits for D from the FULL series must equal those from the series
    truncated at D. Any mismatch ⇒ look-ahead leakage. Never raises."""
    mismatches: list[dict] = []
    dates_checked = 0
    for ticker, rows in series_by_ticker.items():
        series = _sorted_series(rows)
        dates = [str(r["date"])[:10] for r in series]
        if len(dates) < 2:
            continue
        # sample evenly across the interior dates
        idxs = sorted(set(range(1, len(dates), max(1, len(dates) // max(sample, 1)))))
        full = reconstructor(ticker, rows, **recon_kw)
        for i in idxs:
            d = dates[i]
            truncated = reconstructor(ticker, series[: i + 1], **recon_kw)
            dates_checked += 1
            if _signals_for_date(full, d) != _signals_for_date(truncated, d):
                mismatches.append({"ticker": ticker, "date": d})
    return {"observe_only": _OBSERVE_ONLY,
            "look_ahead_clean": not mismatches,
            "dates_checked": dates_checked,
            "mismatches": mismatches}


def write_reconstruction_audit(report: dict, base_dir: str = "outputs") -> str:
    from portfolio_automation.data_governance import OutputNamespace, safe_write_json
    return str(safe_write_json(OutputNamespace.HISTORICAL, "reconstruction_audit.json",
                               report, base_dir=base_dir))
```

Note the future-peek test's `leaky` adds a `future_close` field but the comparison keys ignore it; to make the test meaningful, the leaky variant must change a COMPARED field based on future data. Adjust the leaky fn to set `direction` from the LAST row's close vs the date's close (a future-dependent compared field) so truncation changes it. (Fix the test fixture accordingly before Step 2.)

- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: commit** `feat(backtesting): F3 truncation-equality look-ahead audit`

---

### Task F4: reconstructed history matures the OOS window

**Files:** Test `tests/test_recon_matures_window.py`

- [ ] **Step 1: failing test** (no production code — validates the integration contract)

```python
import json
from datetime import date, timedelta
from pathlib import Path

from backtesting.historical_signal_recon import reconstruct_universe
from backtesting.signal_sources import load_historical_signal_snapshots
from backtesting.walk_forward import oos_window_status


def _multiyear_archive(dirpath: Path, ticker: str, start: date, days: int):
    dirpath.mkdir(parents=True, exist_ok=True)
    rows, price = [], 100.0
    for i in range(days):
        d = start + timedelta(days=i)
        price *= 1.04 if i % 7 == 0 else 0.999  # periodic +4% → STRONG_MOVE
        rows.append({"date": d.isoformat(), "close": round(price, 2), "volume": 1_000_000})
    (dirpath / f"{ticker}_5y.json").write_text(json.dumps({"symbol": ticker, "rows": rows}))


def test_reconstruction_matures_oos_window(tmp_path):
    arch = tmp_path / "historical"
    _multiyear_archive(arch, "AAA", date(2024, 1, 1), 500)  # > 315-day span
    recon = tmp_path / "recon"
    reconstruct_universe(str(arch), str(recon))
    signals = load_historical_signal_snapshots(str(recon))
    assert len(signals) > 0
    ow = oos_window_status(signals, today=date(2026, 6, 5))
    assert ow["folds_possible"] is True
    assert ow["calendar_days_observed"] >= 315
```

- [ ] **Step 2:** Run → it should PASS once F1/F2 exist (this is an integration assertion). If it fails, fix the reconstructor/loader contract until green.
- [ ] **Step 3:** (no impl) — if green, proceed.
- [ ] **Step 4: commit** `test(backtesting): F4 reconstructed history matures the OOS window`

---

### Task F5: auto_apply reconstruction-unverified gate

**Files:** Modify `backtesting/auto_apply.py`; Test `tests/test_auto_apply.py`

- [ ] **Step 1: failing tests** (append to test_auto_apply.py; reuse the `env` fixture + `_approve`)

```python
def test_reconstructed_evidence_blocked_when_audit_not_clean(env):
    out = _call(env, evidence_source="historical_reconstruction",
                reconstruction_audit={"look_ahead_clean": False}, approver=_approve)
    assert out["status"] == "reconstruction_unverified"
    assert _registry_unchanged(env)


def test_reconstructed_evidence_proceeds_when_audit_clean(env):
    out = _call(env, evidence_source="historical_reconstruction",
                reconstruction_audit={"look_ahead_clean": True}, approver=_approve)
    assert out["status"] == "applied"
```

- [ ] **Step 2:** Run → FAIL (unexpected kwargs).
- [ ] **Step 3: implement** — add params + a gate AFTER G2 (oos maturity), BEFORE G3:

In `maybe_auto_apply` signature add:
```python
    evidence_source: str | None = None,
    reconstruction_audit: dict | None = None,
```
After the G2 block insert:
```python
        # G2b — reconstructed evidence requires a clean look-ahead audit (fail-closed).
        if evidence_source == "historical_reconstruction":
            if not (reconstruction_audit or {}).get("look_ahead_clean"):
                return _result("reconstruction_unverified", now_iso=now_iso,
                               base_dir=base_dir, write=write)
```

- [ ] **Step 4:** Run → PASS (full `tests/test_auto_apply.py`).
- [ ] **Step 5: commit** `feat(backtesting): F5 auto_apply reconstruction-unverified fail-closed gate`

---

### Task F6: health surface + skill read + runner script + docs + full suite

**Files:** Modify `backtesting/backtest_health.py`, `.claude/commands/pattern-loop-analysis.md`; Create `scripts/pattern_loop_reconstruct.sh`, `docs/PATTERN_LOOP_RECONSTRUCTION.md`; Modify `docs/CHANGELOG_DECISIONS.md`, `.agent/project_state.yaml`; Test `tests/test_backtest_health.py`.

- [ ] **Step 1: failing health test** (append)

```python
def test_reconstruction_audit_surfaced(tmp_path):
    bt = tmp_path / "backtest"
    _write_results(bt, generated_at=_NOW.isoformat(), evaluated=120,
                   regimes=["risk_on", "neutral"], slope=0.3)
    prop = tmp_path / "policy" / "signal_weight_proposals.json"
    _write_proposals(prop, proposed_count=1)
    audit = tmp_path / "backtest" / "reconstruction_audit.json"
    _write_json(audit, {"look_ahead_clean": False, "mismatches": [{"ticker": "AAA"}]})
    out = assess_backtest_health(backtest_dir=str(bt), proposals_path=str(prop), now=_NOW,
                                 reconstruction_audit_path=str(audit))
    assert out["details"]["reconstruction"]["look_ahead_clean"] is False
    assert "reconstruction_lookahead_dirty" in out["flags"]  # RED
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: implement** in `assess_backtest_health`: add param `reconstruction_audit_path: str = "outputs/backtest/reconstruction_audit.json"`; read it; `details["reconstruction"] = audit`; if it exists and `look_ahead_clean is False` → `red.append("reconstruction_lookahead_dirty")`. Absent tolerated. (Update the `_assess` helper to point this path at the tmp tree, like the D/E paths, so existing tests stay isolated.)

- [ ] **Step 4:** Run → PASS (full `tests/test_backtest_health.py`).

- [ ] **Step 5: runner script** `scripts/pattern_loop_reconstruct.sh` (mirror `pattern_loop_recheck.sh`: PATH/HOME, dotenv parser, venv python). Body runs:
```
"${PYTHON_BIN}" - <<'PY'
from portfolio_automation.historical_backfill import run_historical_backfill
from backtesting.historical_signal_recon import reconstruct_universe, assert_no_lookahead, write_reconstruction_audit
import json
run_historical_backfill(root=".")                       # populate 5y price archive
summary = reconstruct_universe("outputs/backtest/historical", "outputs/backtest/recon")
print("recon:", json.dumps(summary))
# audit on the archives actually used
import glob
series = {}
for f in glob.glob("outputs/backtest/historical/*_5y.json"):
    d = json.load(open(f)); series[d.get("symbol","?")] = d.get("rows", [])
rep = assert_no_lookahead(series, sample=8)
write_reconstruction_audit(rep)
print("lookahead_clean:", rep["look_ahead_clean"])
PY
"${PYTHON_BIN}" -m backtesting.run_loop --history outputs/backtest/recon --live
```
`chmod +x`; `bash -n` syntax check.

- [ ] **Step 6: docs** — `docs/PATTERN_LOOP_RECONSTRUCTION.md` (what F does, the look-ahead audit invariant, the recon→audit→run_loop flow, the auto-apply gate, the runner). CHANGELOG entry (area: evaluation; reconstructed evidence; look-ahead audit gate; auto-apply may act on reconstructed evidence when clean — operator-approved). `.agent/project_state.yaml` note (next_official_step unchanged).

- [ ] **Step 7: full suite** `/opt/stockbot/.venv/bin/python -m pytest -q` → all pass.
- [ ] **Step 8: commit** `feat(backtesting): F6 reconstruction health surface + skill + runner + docs`

---

## Self-Review

**Spec coverage:** archive reuse → F6 script; reconstructor → F1; universe/snapshots → F2; look-ahead audit → F3 (+ the future-peek catch test); run_loop integration → F4 contract test + F6 script (`--history recon`); E gate → F5; health/skill/docs → F6. All spec sections covered.

**Placeholder scan:** none — every code step has concrete code. One step (F3) explicitly flags that the leaky-fixture must mutate a *compared* field (direction from a future row) for the test to be meaningful; that's a real instruction, fixed before running.

**Type/name consistency:** `reconstruct_signals(ticker, rows, *, strong_move_pct, volume_spike_factor, vol_window, today)` is used identically in F2/F3/F4. `reconstruct_universe(archive_dir, recon_dir, ...)`. `assert_no_lookahead(series_by_ticker, *, reconstructor, sample, **recon_kw) -> {look_ahead_clean, dates_checked, mismatches}`. Emitted signal keys (`ticker, scan_time, alert_basis, pattern, patterns, direction, signal_score, confidence_score, price_change_pct, source`) are consistent across reconstructor, audit comparison, and the snapshot writer. auto_apply params `evidence_source` + `reconstruction_audit`; status `reconstruction_unverified`; health flag `reconstruction_lookahead_dirty`; audit field `look_ahead_clean` — consistent end-to-end.

## Production boundary (operator go-ahead)
Running the reconstruction + enabling auto-apply on it is the activation step: requires populating the 5y archive (FMP), a clean look-ahead audit, `config.json backtesting.auto_apply.enabled=true`, and no kill-switch. Per the operator decision, once the audit is clean auto-apply proceeds full-auto — so enabling is the deliberate, gated act.
