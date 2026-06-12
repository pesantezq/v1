# Strategy + Tax-Aware Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the tax + strategy advisory layer consume the operator's live Schwab holdings + cost basis (instead of config/degraded), with a defensive per-lot tax layer, then enable it.

**Architecture:** Flip `config.json portfolio.broker_aware.enabled=true` so `holdings_resolver` returns `holdings_source: "broker"` carrying cost basis; that cascades to `tax_scorecard` (real unrealized G/L; LTCG/STCG + wash-sale when per-lot data present, else honest degraded_fields), `tax_harvest_advisor` (broker basis), and `strategy_comparator` (broker context). Surface in the daily memo + a defensive new GUI panel. Observe-only throughout; never writes `decision_plan.json`.

**Tech Stack:** Python stdlib + existing modules (`holdings_resolver`, `strategy/tax_scorecard`, `tax_harvest_advisor`, `strategy/strategy_comparator`, `brokers/schwab_client`, `watchlist_scanner/daily_memo`, `gui_v2`). `pytest`. Run Python via `.venv/bin/python3`.

**Conventions:** Additive + observe-only (`observe_only`/`no_trade` hardcoded); never touch `decision_engine.py`/scoring; non-blocking. TDD: failing test → run-fail → minimal impl → run-pass → commit. Stage explicit paths (never `git commit -am`). Preserve `config/signal_registry.yaml` `default_weight: 0.4947`.

---

## File Structure

- **Modify** `portfolio_automation/holdings_resolver.py` — carry `average_cost`/`cost_basis` into broker holdings (Task 1).
- **Create** `portfolio_automation/brokers/schwab_tax_lots.py` — defensive per-lot normalizer + `schwab_tax_lots.json` writer (Task 2).
- **Modify** `portfolio_automation/strategy/tax_scorecard.py` — compute unrealized G/L; LTCG/STCG + wash-sale when lots; `degraded_fields` (Task 3).
- **Modify** `portfolio_automation/tax_harvest_advisor.py` — broker-basis path + `basis_source` (Task 4).
- **Modify** `portfolio_automation/strategy/strategy_comparator.py` — verify broker context (Task 5; mostly a test + small guard).
- **Modify** `watchlist_scanner/daily_memo.py` — compact tax/strategy line (Task 6).
- **Modify** `.claude/commands/daily-tool-analysis.md` + **Create** a producer-signal test (Task 7).
- **Create** `gui_v2/data/dash_strategy_tax.py` + `gui_v2/templates/dashboard/strategy_tax.html`; **Modify** `gui_v2/app.py`, `gui_v2/templates/base.html` (Task 8, last/defensive).
- **Modify** `config.json` + `portfolio_automation/artifact_registry.yaml` + docs (Tasks 9–10).
- Tests: `tests/test_holdings_resolver.py`, `tests/test_schwab_tax_lots.py`, `tests/test_schwab_models.py`/`test_tax_scorecard*` , `tests/test_tax_harvest_advisor.py`, `tests/test_strategy_comparator*`, `tests/test_daily_memo*`, `tests/test_gui_*`.

---

## Task 1: Carry cost basis through the holdings resolver

**Files:**
- Modify: `portfolio_automation/holdings_resolver.py` (the broker-holdings list comprehension at lines 93–95)
- Test: `tests/test_holdings_resolver_cost_basis.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_holdings_resolver_cost_basis.py`:

```python
import json
from datetime import datetime, timezone
from pathlib import Path
from portfolio_automation import holdings_resolver as hr


def _setup(tmp_path, positions):
    L = tmp_path / "outputs" / "latest"; L.mkdir(parents=True)
    (tmp_path / "config.json").write_text(json.dumps(
        {"portfolio": {"broker_aware": {"enabled": True}, "holdings": [], "cash_available": 100.0}}))
    (L / "schwab_positions.json").write_text(json.dumps({"positions": positions}))
    (L / "schwab_portfolio_snapshot.json").write_text(json.dumps(
        {"snapshot_timestamp": datetime.now(timezone.utc).isoformat(), "totals": {"cash": 100.0}}))


def test_broker_holdings_carry_cost_basis(tmp_path):
    _setup(tmp_path, [{"symbol": "AAA", "quantity": 10, "market_value": 1500.0, "average_cost": 100.0}])
    res = hr.resolve_holdings(tmp_path, now=datetime.now(timezone.utc))
    assert res["holdings_source"] == "broker"
    h = res["holdings"][0]
    assert h["average_cost"] == 100.0
    assert h["cost_basis"] == 1000.0          # qty * average_cost
    assert h["market_value"] == 1500.0


def test_broker_holdings_cost_basis_none_when_avg_missing(tmp_path):
    _setup(tmp_path, [{"symbol": "BBB", "quantity": 5, "market_value": 250.0}])
    res = hr.resolve_holdings(tmp_path, now=datetime.now(timezone.utc))
    h = res["holdings"][0]
    assert h["average_cost"] is None and h["cost_basis"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest -q tests/test_holdings_resolver_cost_basis.py`
Expected: FAIL — `KeyError: 'average_cost'` (resolver doesn't carry it yet).

- [ ] **Step 3: Implement**

In `portfolio_automation/holdings_resolver.py`, replace the broker-holdings comprehension (currently lines 93–95) with:

```python
        def _cost_basis(p):
            q, ac = p.get("quantity"), p.get("average_cost")
            try:
                return round(float(q) * float(ac), 2) if q is not None and ac is not None else None
            except (TypeError, ValueError):
                return None
        holdings = [{"symbol": str(p.get("symbol", "")).upper(),
                     "quantity": p.get("quantity"),
                     "market_value": p.get("market_value"),
                     "average_cost": p.get("average_cost"),
                     "cost_basis": _cost_basis(p)} for p in pos if p.get("symbol")]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest -q tests/test_holdings_resolver_cost_basis.py`
Expected: PASS (2 tests). Also run existing resolver tests if any: `.venv/bin/python3 -m pytest -q -k holdings_resolver`.

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/holdings_resolver.py tests/test_holdings_resolver_cost_basis.py
git commit -m "feat(strategy-tax): carry Schwab cost basis through holdings_resolver"
```

---

## Task 2: Defensive per-lot tax-data layer

**Files:**
- Create: `portfolio_automation/brokers/schwab_tax_lots.py`
- Test: `tests/test_schwab_tax_lots.py` (new)

Schwab's read-only positions API may or may not return per-lot acquisition data. This module parses lot fields **if present** and emits an explicit "no lots" marker otherwise — never guesses. It writes `schwab_tax_lots.json` (observe_only).

- [ ] **Step 1: Write the failing test**

Create `tests/test_schwab_tax_lots.py`:

```python
from portfolio_automation.brokers import schwab_tax_lots as tl


def test_normalize_lots_present():
    raw = {"positions": [
        {"symbol": "AAA", "taxLots": [
            {"quantity": 4, "costBasis": 400.0, "acquiredDate": "2024-01-10"},
            {"quantity": 6, "costBasis": 660.0, "acquiredDate": "2026-05-01"}]}]}
    out = tl.normalize_tax_lots(raw, now_iso="2026-06-12T00:00:00+00:00")
    assert out["observe_only"] is True and out["no_trade"] is True
    assert out["has_lots"] is True
    lots = out["by_symbol"]["AAA"]
    assert len(lots) == 2
    assert lots[0]["acquired_date"] == "2024-01-10" and lots[0]["cost_basis"] == 400.0


def test_normalize_no_lots_marker():
    raw = {"positions": [{"symbol": "AAA", "average_cost": 100.0}]}  # aggregate only
    out = tl.normalize_tax_lots(raw, now_iso="2026-06-12T00:00:00+00:00")
    assert out["has_lots"] is False and out["by_symbol"] == {}
    assert "no per-lot" in out["reason"].lower()


def test_normalize_handles_garbage():
    assert tl.normalize_tax_lots(None, now_iso="t")["has_lots"] is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_tax_lots.py`
Expected: FAIL — `ModuleNotFoundError: ...schwab_tax_lots`.

- [ ] **Step 3: Implement**

Create `portfolio_automation/brokers/schwab_tax_lots.py`:

```python
# portfolio_automation/brokers/schwab_tax_lots.py
"""Defensive per-lot tax-data normalizer. Observe-only; no-trade; never raises.

Schwab's read-only positions payload MAY include per-lot acquisition data
(`taxLots`/`lots`). When present we normalize it; when absent we emit an explicit
no-lots marker so downstream tax math degrades honestly (never guesses lot dates).
"""
from __future__ import annotations

from typing import Any

_LOT_KEYS = ("taxLots", "tax_lots", "lots")


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_tax_lots(positions: Any, *, now_iso: str) -> dict[str, Any]:
    by_symbol: dict[str, list[dict]] = {}
    rows = positions.get("positions", []) if isinstance(positions, dict) else []
    for p in rows or []:
        if not isinstance(p, dict):
            continue
        sym = str(p.get("symbol", "")).upper()
        raw_lots = next((p[k] for k in _LOT_KEYS if isinstance(p.get(k), list)), None)
        if not sym or not raw_lots:
            continue
        lots = []
        for lot in raw_lots:
            if not isinstance(lot, dict):
                continue
            lots.append({
                "quantity": _f(lot.get("quantity") or lot.get("longQuantity")),
                "cost_basis": _f(lot.get("costBasis") or lot.get("cost_basis")),
                "acquired_date": (lot.get("acquiredDate") or lot.get("acquired_date") or None),
            })
        if lots:
            by_symbol[sym] = lots
    has_lots = bool(by_symbol)
    return {
        "generated_at": now_iso, "observe_only": True, "no_trade": True,
        "source": "schwab", "has_lots": has_lots, "by_symbol": by_symbol,
        "reason": ("per-lot acquisition data present"
                   if has_lots else "no per-lot data in broker positions (aggregate cost basis only)"),
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_tax_lots.py`
Expected: PASS (3 tests). Compile: `.venv/bin/python3 -m py_compile portfolio_automation/brokers/schwab_tax_lots.py`.

- [ ] **Step 5: Verify AST no-trade guard still passes**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_client.py -k trading`
Expected: PASS (the new module adds no trade verbs).

- [ ] **Step 6: Wire the writer into the live Schwab sync (best-effort)**

Open `portfolio_automation/brokers/schwab_sync.py`. In `run_sync`, after `raw = client.get_accounts(positions=True)` and the snapshot is built, add a non-blocking call that flattens the raw account positions into a `{"positions": [...]}` shape and writes the lots artifact:

```python
        # per-lot tax data (best-effort; degrades to has_lots:false when absent)
        try:
            from portfolio_automation.brokers.schwab_tax_lots import normalize_tax_lots
            flat = {"positions": [p for acct in (raw if isinstance(raw, list) else [])
                                  for p in ((acct.get("securitiesAccount") or {}).get("positions") or [])]}
            _write(root, "schwab_tax_lots.json", normalize_tax_lots(flat, now_iso=ts))
        except Exception:
            pass
```

(Uses the module's existing `_write(root, name, payload)` helper. If the raw positions carry no lot fields — the expected case for Schwab's API — `has_lots:false` is written, which is correct.)

- [ ] **Step 7: Verify sync still green + commit**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_sync.py tests/test_schwab_tax_lots.py`
Expected: PASS.

```bash
git add portfolio_automation/brokers/schwab_tax_lots.py portfolio_automation/brokers/schwab_sync.py tests/test_schwab_tax_lots.py
git commit -m "feat(strategy-tax): defensive Schwab per-lot tax-data normalizer + sync wiring"
```

---

## Task 3: Tax scorecard — real unrealized G/L + lot-aware fields

**Files:**
- Modify: `portfolio_automation/strategy/tax_scorecard.py` (rewrite `build_tax_scorecard` body; keep `has_tax_lot_data`)
- Test: `tests/test_tax_scorecard_gl.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_tax_scorecard_gl.py`:

```python
from portfolio_automation.strategy import tax_scorecard as ts

_POS = {"positions": [
    {"symbol": "AAA", "quantity": 10, "average_cost": 100.0, "market_value": 1500.0},  # +500 gain
    {"symbol": "BBB", "quantity": 5, "average_cost": 200.0, "market_value": 800.0},     # -200 loss
]}


def test_computes_unrealized_gl_from_avg_cost():
    out = ts.build_tax_scorecard("2026-06-12T00:00:00+00:00", _POS)
    assert out["degraded_mode"] is False
    by = {c["symbol"]: c for c in out["scorecards"]}
    assert by["AAA"]["unrealized_gain"] == 500.0 and by["AAA"]["tlh_candidate"] is False
    assert by["BBB"]["unrealized_gain"] == -200.0 and by["BBB"]["tlh_candidate"] is True
    assert out["portfolio_unrealized_gain"] == 300.0


def test_lot_fields_degraded_without_lots():
    out = ts.build_tax_scorecard("t", _POS)
    assert "short_term_vs_long_term" in out["degraded_fields"]
    assert "wash_sale_window" in out["degraded_fields"]


def test_lot_fields_live_with_lots():
    lots = {"AAA": [{"quantity": 10, "cost_basis": 1000.0, "acquired_date": "2024-01-01"}]}
    out = ts.build_tax_scorecard("2026-06-12T00:00:00+00:00", _POS, tax_lots=lots)
    assert "short_term_vs_long_term" not in out["degraded_fields"]
    by = {c["symbol"]: c for c in out["scorecards"]}
    assert by["AAA"]["holding_period"] == "long"   # acquired >1y ago


def test_no_positions_degraded():
    out = ts.build_tax_scorecard("t", {"positions": []})
    assert out["degraded_mode"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest -q tests/test_tax_scorecard_gl.py`
Expected: FAIL — current scorecard returns no `unrealized_gain` math / no `degraded_fields`.

- [ ] **Step 3: Implement**

In `portfolio_automation/strategy/tax_scorecard.py`, add `from datetime import datetime, timezone` to imports and replace `build_tax_scorecard` (keep `has_tax_lot_data` as-is) with:

```python
def _unrealized(p: Any) -> float | None:
    try:
        q, ac, mv = float(p["quantity"]), float(p["average_cost"]), float(p["market_value"])
        return round(mv - q * ac, 2)
    except (KeyError, TypeError, ValueError):
        return None


def _holding_period(acquired_date: str | None, now_iso: str) -> str | None:
    if not acquired_date:
        return None
    try:
        a = datetime.fromisoformat(str(acquired_date).replace("Z", "+00:00"))
        n = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
        if a.tzinfo is None:
            a = a.replace(tzinfo=timezone.utc)
        if n.tzinfo is None:
            n = n.replace(tzinfo=timezone.utc)
        return "long" if (n - a).days > 365 else "short"
    except Exception:
        return None


def build_tax_scorecard(now_iso: str, positions: Any, account_types: list[str] | None = None,
                        tax_lots: dict[str, list[dict]] | None = None) -> dict[str, Any]:
    payload = observe_only_envelope(now_iso, source="tax_scorecard",
                                    wash_sale_note="informational only")
    rows = positions.get("positions", []) if isinstance(positions, dict) else []
    if not rows or not has_tax_lot_data(positions):
        payload["degraded_mode"] = True
        payload["degraded_reason"] = "no cost-basis / tax-lot data (broker not configured or fields absent)"
        payload["scorecards"] = []
        payload["account_types_separated"] = bool(account_types)
        payload["degraded_fields"] = ["unrealized_gain_loss", "short_term_vs_long_term", "wash_sale_window"]
        payload["portfolio_unrealized_gain"] = None
        return payload

    have_lots = bool(tax_lots)
    cards, total = [], 0.0
    for p in rows:
        sym = str(p.get("symbol", "")).upper()
        ug = _unrealized(p)
        if isinstance(ug, (int, float)):
            total += ug
        card = {"symbol": sym, "unrealized_gain": ug,
                "tlh_candidate": bool(isinstance(ug, (int, float)) and ug < 0),
                "wash_sale_risk_informational": False}
        if have_lots and tax_lots.get(sym):
            periods = {_holding_period(l.get("acquired_date"), now_iso) for l in tax_lots[sym]}
            card["holding_period"] = ("long" if periods == {"long"}
                                      else "short" if periods == {"short"} else "mixed")
        cards.append(card)
    payload["degraded_mode"] = False
    payload["scorecards"] = cards
    payload["portfolio_unrealized_gain"] = round(total, 2)
    payload["account_types_separated"] = bool(account_types)
    payload["degraded_fields"] = [] if have_lots else ["short_term_vs_long_term", "wash_sale_window"]
    return payload
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python3 -m pytest -q tests/test_tax_scorecard_gl.py`
Expected: PASS (4 tests). Run any existing scorecard tests: `.venv/bin/python3 -m pytest -q -k tax_scorecard`.

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/strategy/tax_scorecard.py tests/test_tax_scorecard_gl.py
git commit -m "feat(strategy-tax): tax scorecard computes unrealized G/L + lot-aware holding period"
```

---

## Task 4: Tax harvest advisor — broker cost-basis path

**Files:**
- Modify: `portfolio_automation/tax_harvest_advisor.py` (`run_tax_harvest_advisor`: add a broker-holdings source; `build_plan`: add `basis_source`)
- Test: `tests/test_tax_harvest_broker_basis.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_tax_harvest_broker_basis.py`:

```python
import json
from datetime import datetime, timezone
from pathlib import Path
from portfolio_automation import tax_harvest_advisor as tha


def _setup(tmp_path):
    L = tmp_path / "outputs" / "latest"; L.mkdir(parents=True)
    (tmp_path / "config.json").write_text(json.dumps(
        {"portfolio": {"is_taxable_account": True, "broker_aware": {"enabled": True},
                       "holdings": [], "cash_available": 0.0}}))
    (L / "schwab_positions.json").write_text(json.dumps({"positions": [
        {"symbol": "BBB", "quantity": 5, "average_cost": 200.0, "market_value": 800.0}]}))  # -200 loss
    (L / "schwab_portfolio_snapshot.json").write_text(json.dumps(
        {"snapshot_timestamp": datetime.now(timezone.utc).isoformat(), "totals": {"cash": 0.0}}))


def test_broker_basis_harvest(tmp_path):
    _setup(tmp_path)
    plan = tha.run_tax_harvest_advisor(tmp_path, base_dir=tmp_path / "outputs")
    assert plan["basis_source"] == "broker"
    assert plan["harvestable_count"] == 1
    row = next(r for r in plan["positions"] if r["symbol"] == "BBB")
    assert row["harvest_recommended"] is True and row["loss_dollars"] == 200.0


def test_config_basis_when_broker_off(tmp_path):
    L = tmp_path / "outputs" / "latest"; L.mkdir(parents=True)
    (tmp_path / "config.json").write_text(json.dumps(
        {"portfolio": {"is_taxable_account": True, "holdings": [
            {"symbol": "CCC", "shares": 2, "cost_basis": 100.0}]}}))
    plan = tha.run_tax_harvest_advisor(tmp_path, base_dir=tmp_path / "outputs",
                                       price_overrides={"CCC": 40.0})
    assert plan["basis_source"] == "config"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest -q tests/test_tax_harvest_broker_basis.py`
Expected: FAIL — `KeyError: 'basis_source'` / broker path not implemented.

- [ ] **Step 3: Implement**

In `portfolio_automation/tax_harvest_advisor.py`:

(a) Add an import at the top of the file (after the existing imports):
```python
from portfolio_automation.holdings_resolver import resolve_holdings
```

(b) In `build_plan`, add a `basis_source` parameter and include it in BOTH returned dicts:
```python
def build_plan(*, is_taxable: bool, rows: list[dict[str, Any]], notes: list[str],
               basis_source: str = "config") -> dict[str, Any]:
```
Add `"basis_source": basis_source,` to each of the two returned dicts (the non-taxable dict and the taxable dict).

(c) In `run_tax_harvest_advisor`, after `is_taxable` is computed and before the holdings loop, resolve the source. Replace the holdings loop so it prefers broker holdings:
```python
    res = resolve_holdings(repo_root)
    basis_source = "broker" if res.get("holdings_source") == "broker" else "config"
    rows: list[dict[str, Any]] = []
    if basis_source == "broker":
        for h in res.get("holdings") or []:
            symbol = _safe_str(h.get("symbol")).upper()
            shares = _safe_float(h.get("quantity"))
            avg = _safe_float(h.get("average_cost"))
            mv = _safe_float(h.get("market_value"))
            if not symbol or shares is None or shares <= 0:
                continue
            price = (mv / shares) if (mv is not None and shares) else None
            rows.append(evaluate_position(symbol=symbol, shares=shares, cost_basis=avg,
                                          current_price=price, replacement_map=replacement_map))
    else:
        for h in portfolio.get("holdings") or []:
            if not isinstance(h, dict):
                continue
            symbol = _safe_str(h.get("symbol")).upper()
            shares = _safe_float(h.get("shares"))
            cost_basis = _safe_float(h.get("cost_basis"))
            if not symbol or shares is None or shares <= 0:
                continue
            if symbol in price_overrides:
                price = _safe_float(price_overrides[symbol])
            elif fmp_client is not None:
                price = _current_price_from_fmp(fmp_client, symbol)
            else:
                price = None
            rows.append(evaluate_position(symbol=symbol, shares=shares, cost_basis=cost_basis,
                                          current_price=price, replacement_map=replacement_map))
    plan = build_plan(is_taxable=True, rows=rows, notes=notes, basis_source=basis_source)
```
(Set `replacement_map`/`price_overrides` defaults BEFORE this block, as the existing code already does. Delete the OLD holdings loop + old `build_plan(... )` call that this replaces.)

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python3 -m pytest -q tests/test_tax_harvest_broker_basis.py`
Expected: PASS (2 tests). Run existing: `.venv/bin/python3 -m pytest -q -k tax_harvest`.

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/tax_harvest_advisor.py tests/test_tax_harvest_broker_basis.py
git commit -m "feat(strategy-tax): tax harvest advisor uses live broker cost basis"
```

---

## Task 5: Strategy comparator — confirm broker context

**Files:**
- Test: `tests/test_strategy_comparator_broker.py` (new)
- Modify: `portfolio_automation/strategy/strategy_comparator.py` ONLY if the test reveals a gap.

The comparator already branches on `resolve_holdings(...)["holdings_source"]`. This task is a characterization test that the comparison reports `context_source: broker` when broker holdings are present, plus a minimal fix if it doesn't.

- [ ] **Step 1: Write the test**

Create `tests/test_strategy_comparator_broker.py`:

```python
import json
from datetime import datetime, timezone
from pathlib import Path
from portfolio_automation.strategy import strategy_comparator as sc


def _setup(tmp_path):
    L = tmp_path / "outputs" / "latest"; L.mkdir(parents=True)
    (tmp_path / "config.json").write_text(json.dumps(
        {"portfolio": {"broker_aware": {"enabled": True}, "holdings": [], "cash_available": 50.0}}))
    (L / "schwab_positions.json").write_text(json.dumps({"positions": [
        {"symbol": "AAA", "quantity": 10, "market_value": 1500.0, "average_cost": 100.0}]}))
    (L / "schwab_portfolio_snapshot.json").write_text(json.dumps(
        {"snapshot_timestamp": datetime.now(timezone.utc).isoformat(), "totals": {"cash": 50.0}}))


def test_comparison_uses_broker_context(tmp_path):
    _setup(tmp_path)
    out = sc.run_strategy_comparison(root=Path(tmp_path)) if hasattr(sc, "run_strategy_comparison") \
        else sc.compare_strategies(root=Path(tmp_path))
    assert out.get("context_source") == "broker"
```

> NOTE to implementer: open `portfolio_automation/strategy/strategy_comparator.py` and use its ACTUAL public entry-point name (grep `^def ` — it may be `run_strategy_comparison`, `compare_strategies`, or similar) and its real `root`/path argument. Adjust the test call to match before running. Do not change the comparator's signature.

- [ ] **Step 2: Run the test**

Run: `.venv/bin/python3 -m pytest -q tests/test_strategy_comparator_broker.py`
Expected: PASS if the comparator already flows broker context (likely). If FAIL because `context_source` is `config` despite broker holdings present, inspect why (it calls `resolve_holdings`; ensure it passes `root` through and reads `holdings_source`), make the MINIMAL fix, and re-run.

- [ ] **Step 3: Wire tax-lots into the scorecard call**

In `strategy_comparator.py`, find where it calls `build_tax_scorecard(...)` (grep `tax_scorecard` / `build_tax_scorecard`). Load the lots artifact and pass its `by_symbol` through, so the scorecard's lot-aware fields light up when lots exist:

```python
        import json as _json
        from pathlib import Path as _Path
        _lots_p = _Path(root) / "outputs" / "latest" / "schwab_tax_lots.json"
        _tax_lots = {}
        try:
            if _lots_p.exists():
                _tax_lots = (_json.loads(_lots_p.read_text(encoding="utf-8")) or {}).get("by_symbol", {})
        except Exception:
            _tax_lots = {}
        # ...pass tax_lots=_tax_lots into the existing build_tax_scorecard(...) call.
```

Add a test asserting the scorecard call receives lots when the artifact is present (or, if simpler, assert the written `strategy_tax_scorecard.json` has empty `degraded_fields` when a lots fixture + broker positions are present). Keep it minimal.

- [ ] **Step 4: Run + commit**

Run: `.venv/bin/python3 -m pytest -q tests/test_strategy_comparator_broker.py -k "broker or lots"`
Expected: PASS.

```bash
git add tests/test_strategy_comparator_broker.py portfolio_automation/strategy/strategy_comparator.py
git commit -m "feat(strategy-tax): strategy comparison broker context + tax-lot passthrough"
```

---

## Task 6: Daily memo — compact tax/strategy line

**Files:**
- Modify: `watchlist_scanner/daily_memo.py`
- Test: `tests/test_daily_memo_tax_strategy.py` (new)

- [ ] **Step 1: Read the memo builder**

Open `watchlist_scanner/daily_memo.py`. Find the top-level function that assembles the memo markdown (grep `def build` / `def render` / where `## ` sections are appended via the `a(...)` helper around line 337). Identify where to append a one-line "System / Data health"-adjacent note or a short "Tax & Strategy" line. Identify how it reads artifacts from `outputs/latest`.

- [ ] **Step 2: Write the failing test**

Create `tests/test_daily_memo_tax_strategy.py`:

```python
from watchlist_scanner import daily_memo as dm


def test_tax_strategy_line_renders():
    line = dm.render_tax_strategy_line(
        scorecard={"degraded_mode": False, "portfolio_unrealized_gain": 300.0, "degraded_fields": []},
        harvest={"basis_source": "broker", "harvestable_count": 1},
        strategy={"context_source": "broker"})
    assert "300" in line and "broker" in line.lower() and "1" in line


def test_tax_strategy_line_degraded():
    line = dm.render_tax_strategy_line(
        scorecard={"degraded_mode": True, "degraded_fields": ["unrealized_gain_loss"]},
        harvest={"basis_source": "config", "harvestable_count": 0},
        strategy={"context_source": "config"})
    assert "degraded" in line.lower() or "config" in line.lower()
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest -q tests/test_daily_memo_tax_strategy.py`
Expected: FAIL — no `render_tax_strategy_line`.

- [ ] **Step 4: Implement**

Add this pure helper to `watchlist_scanner/daily_memo.py` (near the other render helpers):

```python
def render_tax_strategy_line(scorecard: dict, harvest: dict, strategy: dict) -> str:
    """One compact observe-only memo line for tax + strategy (broker-aware)."""
    if scorecard.get("degraded_mode"):
        tax = "Tax: degraded (no broker cost basis)"
    else:
        ug = scorecard.get("portfolio_unrealized_gain")
        n = harvest.get("harvestable_count", 0)
        src = harvest.get("basis_source", "config")
        deg = scorecard.get("degraded_fields") or []
        suffix = f" · lot fields degraded ({', '.join(deg)})" if deg else ""
        tax = f"Tax: ${ug:,.0f} unrealized G/L · {n} harvest candidate(s) (basis: {src}){suffix}"
    strat = f"Strategy: context {strategy.get('context_source', 'config')}"
    return f"{tax} · {strat}"
```

Then wire it into the memo body where the compact health/advisory lines are appended: read `tax_harvest_advisor.json`, `strategy_tax_scorecard.json` (sandbox), `strategy_comparison.json` (sandbox), and append `render_tax_strategy_line(...)` inside a `try/except` (non-fatal) so a missing artifact never breaks the memo. Keep within the memo's max-section contract — append as a single line under the existing System/Data-health area, not a new top-level section.

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/bin/python3 -m pytest -q tests/test_daily_memo_tax_strategy.py`
Expected: PASS (2 tests). Run existing memo tests: `.venv/bin/python3 -m pytest -q -k daily_memo`.

- [ ] **Step 6: Commit**

```bash
git add watchlist_scanner/daily_memo.py tests/test_daily_memo_tax_strategy.py
git commit -m "feat(strategy-tax): compact tax/strategy line in daily memo"
```

---

## Task 7: Daily health coverage

**Files:**
- Modify: `.claude/commands/daily-tool-analysis.md`
- Test: `tests/test_daily_tax_strategy_signals.py` (new — asserts the producer-level signal logic the skill consumes)

- [ ] **Step 1: Write a producer-signal test**

Create `tests/test_daily_tax_strategy_signals.py` (a small pure helper the skill's logic mirrors, kept in the test to lock both healthy + degraded states):

```python
def _amber_signals(scorecard, strategy, broker_ok, broker_aware_on):
    out = []
    if broker_aware_on and broker_ok and scorecard.get("degraded_mode"):
        out.append("tax_scorecard_unexpectedly_degraded")
    if broker_aware_on and broker_ok and strategy.get("context_source") == "config":
        out.append("strategy_context_not_broker")
    return out


def test_healthy_no_amber():
    assert _amber_signals({"degraded_mode": False}, {"context_source": "broker"}, True, True) == []


def test_degraded_raises_amber():
    s = _amber_signals({"degraded_mode": True}, {"context_source": "config"}, True, True)
    assert "tax_scorecard_unexpectedly_degraded" in s and "strategy_context_not_broker" in s


def test_inert_when_broker_aware_off():
    assert _amber_signals({"degraded_mode": True}, {"context_source": "config"}, True, False) == []
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python3 -m pytest -q tests/test_daily_tax_strategy_signals.py`
Expected: PASS (3 tests; pure helper, no impl needed).

- [ ] **Step 3: Extend the skill**

In `.claude/commands/daily-tool-analysis.md`, add to the Step-1 artifacts-read list (after the existing strategy/next-stage entries):

```markdown
- `outputs/sandbox/strategy_tax_scorecard.json` → `degraded_mode`, `degraded_fields`, `portfolio_unrealized_gain`; `outputs/latest/tax_harvest_advisor.json` → `basis_source`, `harvestable_count`; `outputs/sandbox/strategy_comparison.json` → `context_source`; `outputs/latest/schwab_tax_lots.json` → `has_lots` (all observe-only; absent/degraded is inert pre-flip).
```

Add to the AMBER section:

```markdown
- `tax_scorecard_unexpectedly_degraded` = broker_aware enabled AND `broker_sync_status.overall_status == ok` AND `strategy_tax_scorecard.degraded_mode == true` (broker live but cost-basis plumbing broke — advisory; never RED).
- `strategy_context_not_broker` = broker_aware enabled AND Schwab `ok` AND `strategy_comparison.context_source == config` (resolver not flowing through to strategy — advisory; never RED).
```

Add a body-grammar line (quant + market-expert lens): `"Tax/Strategy: unrealized G/L {portfolio_unrealized_gain}, {harvestable_count} harvest cand. (basis {basis_source}); strategy context {context_source}{, lots present if has_lots}"`.

- [ ] **Step 4: Verify + commit**

Run: `grep -c "tax_scorecard_unexpectedly_degraded\|strategy_context_not_broker" .claude/commands/daily-tool-analysis.md` → expect ≥ 2.

```bash
git add .claude/commands/daily-tool-analysis.md tests/test_daily_tax_strategy_signals.py
git commit -m "docs(daily-analysis): tax/strategy broker-aware health coverage"
```

---

## Task 8: GUI tax/strategy panel (defensive, last)

**Files:**
- Create: `gui_v2/data/dash_strategy_tax.py`, `gui_v2/templates/dashboard/strategy_tax.html`
- Modify: `gui_v2/app.py` (one route), `gui_v2/templates/base.html` (one nav link)
- Test: `tests/test_gui_strategy_tax.py` (new)

> **Collision note:** Do this task LAST. Before editing `gui_v2/app.py` / `base.html`, run `git pull --ff-only` (or `git fetch && git rebase origin/main`) to pick up any concurrent-session GUI commits, then add your single route line + nav link. The two NEW files never conflict. Mirror the existing `gui_v2/data/dash_crowd_radar.py` + `gui_v2/templates/dashboard/crowd_radar.html` pattern (a concurrent session just created them — read them first as the template).

- [ ] **Step 1: Read the existing GUI panel pattern**

Read `gui_v2/data/dash_crowd_radar.py`, `gui_v2/templates/dashboard/crowd_radar.html`, and how its route is registered in `gui_v2/app.py` (grep `crowd`) and linked in `gui_v2/templates/base.html`. Copy that exact structure.

- [ ] **Step 2: Write the failing test**

Create `tests/test_gui_strategy_tax.py`:

```python
from gui_v2.data import dash_strategy_tax as d


def test_loader_degrades_gracefully(tmp_path):
    # no artifacts present -> loader returns a safe degraded context, never raises
    ctx = d.load_strategy_tax_context(base_dir=tmp_path)
    assert ctx["available"] is False
    assert "scorecard" in ctx and "harvest" in ctx and "strategy" in ctx
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest -q tests/test_gui_strategy_tax.py`
Expected: FAIL — `ModuleNotFoundError: ...dash_strategy_tax`.

- [ ] **Step 4: Implement the loader**

Create `gui_v2/data/dash_strategy_tax.py`:

```python
"""Read-only dashboard loader for the tax/strategy panel. Never raises."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read(base: Path, ns: str, name: str) -> dict[str, Any]:
    p = Path(base) / ns / name
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def load_strategy_tax_context(base_dir: str | Path = "outputs") -> dict[str, Any]:
    base = Path(base_dir)
    scorecard = _read(base, "sandbox", "strategy_tax_scorecard.json")
    harvest = _read(base, "latest", "tax_harvest_advisor.json")
    strategy = _read(base, "sandbox", "strategy_comparison.json")
    lots = _read(base, "latest", "schwab_tax_lots.json")
    available = bool(scorecard or harvest or strategy)
    return {"available": available, "observe_only": True,
            "scorecard": scorecard, "harvest": harvest, "strategy": strategy, "lots": lots}
```

> NOTE: confirm the `outputs/<ns>/<file>` layout matches the repo (sandbox vs latest) by checking where each artifact is written; adjust `ns` if needed.

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/bin/python3 -m pytest -q tests/test_gui_strategy_tax.py`
Expected: PASS.

- [ ] **Step 6: Add template + route + nav (mirror crowd_radar)**

Create `gui_v2/templates/dashboard/strategy_tax.html` mirroring `crowd_radar.html` (observe-only banner, a table of scorecards, harvest candidates, strategy context, degraded notes). Add ONE route to `gui_v2/app.py` (mirroring the crowd-radar route, gated by `_require_auth` like the others) at `/dashboard/strategy-tax` that calls `load_strategy_tax_context()` and renders the template. Add ONE nav link in `base.html`.

- [ ] **Step 7: Smoke-test the route + commit**

Run: `.venv/bin/python3 -c "from gui_v2.app import app; from fastapi.testclient import TestClient; c=TestClient(app); r=c.get('/dashboard/strategy-tax'); print('status', r.status_code)"`
Expected: `status 200` (or 401 if auth is configured in env — both acceptable; not 500).

```bash
git add gui_v2/data/dash_strategy_tax.py gui_v2/templates/dashboard/strategy_tax.html gui_v2/app.py gui_v2/templates/base.html tests/test_gui_strategy_tax.py
git commit -m "feat(strategy-tax): read-only GUI tax/strategy panel"
```

---

## Task 9: Register the new artifact + flip the enable

**Files:**
- Modify: `portfolio_automation/artifact_registry.yaml` (add `schwab_tax_lots.json`)
- Modify: `config.json` (`portfolio.broker_aware.enabled = true`)
- Test: `tests/test_broker_aware_flip.py` (new)

- [ ] **Step 1: Register the new artifact**

In `portfolio_automation/artifact_registry.yaml`, add a row for `schwab_tax_lots.json` mirroring the other Schwab broker artifacts (path `outputs/latest/schwab_tax_lots.json`, `required: false`, `severity: info`, `cadence: on_demand`, `producer: schwab_tax_lots`, `consumer_status: consumed` — consumed by tax_scorecard + the GUI panel + daily-tool-analysis).

- [ ] **Step 2: Write the flip test (proves the lynchpin works end-to-end)**

Create `tests/test_broker_aware_flip.py`:

```python
import json
from pathlib import Path


def test_config_has_broker_aware_enabled():
    cfg = json.loads(Path("config.json").read_text())
    assert cfg["portfolio"]["broker_aware"]["enabled"] is True  # the prod-ready flip
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest -q tests/test_broker_aware_flip.py`
Expected: FAIL (`broker_aware` is `{}` / not enabled).

- [ ] **Step 4: Flip the config**

In `config.json`, set `portfolio.broker_aware` to `{"enabled": true}` (add the key if absent). Do NOT change any other config value.

- [ ] **Step 5: Run to verify it passes + resolver goes broker live**

Run: `.venv/bin/python3 -m pytest -q tests/test_broker_aware_flip.py`
Expected: PASS.
Run (live check on the VPS): `set -a; . ./.env; set +a; .venv/bin/python3 -c "from pathlib import Path; from portfolio_automation.holdings_resolver import resolve_holdings; print(resolve_holdings(Path('.'))['holdings_source'])"`
Expected: `broker` (Schwab positions are fresh from daily Stage 10c). If `config`, check `reason` — likely `broker_data_stale`; re-run `schwab_sync --sync` first.

- [ ] **Step 6: Commit**

```bash
git add portfolio_automation/artifact_registry.yaml config.json tests/test_broker_aware_flip.py
git commit -m "feat(strategy-tax): register schwab_tax_lots + enable broker_aware (prod-ready flip)"
```

---

## Task 10: Docs + CHANGELOG

**Files:**
- Modify: `docs/CHANGELOG_DECISIONS.md`, `docs/OUTPUT_ARTIFACT_CONTRACTS.md`

- [ ] **Step 1: CHANGELOG entry**

Add a `## Strategy + Tax-Aware Hardening` entry to `docs/CHANGELOG_DECISIONS.md` (Area `architecture`) following the required format: files/functions changed (resolver cost basis, schwab_tax_lots, tax_scorecard G/L, harvest broker basis, comparator broker context, memo line, GUI panel, broker_aware flip), Decision, Why, Invariants Preserved (observe-only, no decision-core, staleness fallback, honest degradation), Downstream Impact (new artifact schwab_tax_lots.json + N tests), Artifact Health Severity (info; broker_aware flip enables broker-source).

- [ ] **Step 2: Output-contracts entry**

Add `schwab_tax_lots.json` to `docs/OUTPUT_ARTIFACT_CONTRACTS.md` (observe_only, no_trade, fields `has_lots`/`by_symbol`/`reason`).

- [ ] **Step 3: Commit**

```bash
git add docs/CHANGELOG_DECISIONS.md docs/OUTPUT_ARTIFACT_CONTRACTS.md
git commit -m "docs(strategy-tax): CHANGELOG + artifact contract for tax/strategy hardening"
```

---

## Task 11: Full-suite regression + push

**Files:** none (verification)

- [ ] **Step 1: Focused suites**

Run: `.venv/bin/python3 -m pytest -q tests/test_holdings_resolver_cost_basis.py tests/test_schwab_tax_lots.py tests/test_tax_scorecard_gl.py tests/test_tax_harvest_broker_basis.py tests/test_strategy_comparator_broker.py tests/test_daily_memo_tax_strategy.py tests/test_daily_tax_strategy_signals.py tests/test_gui_strategy_tax.py tests/test_broker_aware_flip.py`
Expected: all PASS.

- [ ] **Step 2: Full suite**

Run: `.venv/bin/python3 -m pytest -q`
Expected: PASS except the 3 known pre-existing failures (`test_run_loop_summary_includes_oos_window`, 2× `test_tuning_proposals`). Confirm NO new failures and no NEW failures in `tax_harvest`/`strategy`/`gui` caused by the config flip (some existing tests may assume config holdings — if any broke because the resolver now prefers broker, fix the test fixture to set `broker_aware.enabled=false` or provide broker fixtures, do NOT weaken production behavior).

- [ ] **Step 3: signal_registry guard**

Run: `grep -q "default_weight: 0.4947" config/signal_registry.yaml && echo OK || git checkout -- config/signal_registry.yaml`

- [ ] **Step 4: Push**

```bash
git push origin main
```

- [ ] **Step 5: VPS validation block (operator)**

```bash
cd /opt/stockbot && set -a; . ./.env; set +a
.venv/bin/python3 -m portfolio_automation.brokers.schwab_sync --sync --reconcile
.venv/bin/python3 -c "from pathlib import Path; from portfolio_automation.holdings_resolver import resolve_holdings as r; print('source:', r(Path('.'))['holdings_source'])"
# expect: broker
sudo systemctl restart stockbot-dashboard.service   # to serve the new /dashboard/strategy-tax panel
```

---

## Notes for the implementer

- Run all Python via `.venv/bin/python3`.
- Do NOT touch `decision_engine.py`, scoring, or `decision_plan.json`. Everything here is observe-only/advisory.
- The config flip (Task 9) may surface existing tests that implicitly assumed config holdings — fix the TEST fixtures (set `broker_aware.enabled=false` or add broker fixtures), never weaken the resolver.
- Task 8 (GUI) is last and rebases first to dodge concurrent `gui_v2` sessions; the two NEW files never conflict, only the route + nav lines might.
- Schwab's read-only positions API may not return per-lot data in practice — Task 2/3's lot path is built + tested with synthetic fixtures and degrades honestly live until/unless real lots appear.
