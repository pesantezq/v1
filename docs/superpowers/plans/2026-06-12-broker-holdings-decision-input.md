# Part B — Broker Holdings as Decision-Core Input — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`). EXECUTE IN AN ISOLATED GIT WORKTREE (concurrent `portfolio-sim` sessions active on `main`); rebase onto `origin/main` before merging. Run Python via `.venv/bin/python3`.

**Goal:** Feed the live Schwab snapshot (holdings shares + cash) into the decision pipeline as a runtime overlay at the config boundary — Schwab-preferred, config fallback on stale/missing — without editing `decision_engine.py` or any scoring math.

**Architecture:** New `broker_overlaid_portfolio()` (config-dict level) in `holdings_resolver.py` + a `Config`-object applier; called at the two pipeline entry points (`watchlist_scanner/__main__.py` dict path, `main.py` Config-object path) so downstream `portfolio_context` carries broker holdings. Gated by `portfolio.broker_aware.enabled`. Runtime-only (no `config.json` write).

**Tech Stack:** Python; existing `resolve_holdings`, `utils.Config`/`Holding`, `watchlist_scanner`/`main.py`. pytest.

**Conventions:** Additive; never edit `decision_engine.py` scoring/recommendation logic; never write `config.json`; never trade. TDD. Commit per task on the worktree branch. Preserve `config/signal_registry.yaml default_weight: 0.4947`.

**Key safety property:** Part A already synced `config.json` == Schwab. So with the overlay ON today, the overlaid holdings equal config → the pipeline output (`decision_plan`) must be UNCHANGED vs overlay-OFF. Use that as the regression check; real divergence only appears when Schwab later differs from config.

---

## File Structure
- **Modify** `portfolio_automation/holdings_resolver.py` — add `broker_overlaid_portfolio(portfolio_block, root, now=None)` (dict→dict) + `_broker_merged_holdings(...)` core (Task 1).
- **Modify** `watchlist_scanner/__main__.py` (~513) — overlay the dict path (Task 2).
- **Modify** `main.py` (after `load_config`/`validate_config`, before holdings objects ~682) — overlay the `Config` object (Task 3).
- **Modify** `.claude/commands/daily-tool-analysis.md` + telemetry (Task 4).
- Tests: `tests/test_broker_overlay.py` (new), plus a main.py-level integration test.

---

## Task 1: `broker_overlaid_portfolio` helper (dict level)

**Files:** Modify `portfolio_automation/holdings_resolver.py`; Test `tests/test_broker_overlay.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_broker_overlay.py`:

```python
import json
from datetime import datetime, timezone
from pathlib import Path
from portfolio_automation import holdings_resolver as hr


def _setup(tmp, positions, enabled=True):
    L = tmp / "outputs" / "latest"; L.mkdir(parents=True)
    (tmp / "config.json").write_text(json.dumps({"portfolio": {"broker_aware": {"enabled": enabled}}}))
    (L / "schwab_positions.json").write_text(json.dumps({"positions": positions}))
    (L / "schwab_portfolio_snapshot.json").write_text(json.dumps(
        {"snapshot_timestamp": datetime.now(timezone.utc).isoformat(), "totals": {"cash": 150.6}}))


_BLOCK = {"holdings": [
    {"symbol": "QQQ", "shares": 6, "target_weight": 0.35, "asset_class": "us_equity",
     "is_leveraged": False, "leverage_factor": 1},
    {"symbol": "NASA", "shares": 14, "target_weight": 0.10, "asset_class": "us_equity",
     "is_leveraged": False, "leverage_factor": 1},
    {"symbol": "VFH", "shares": 0, "target_weight": 0.15, "asset_class": "us_equity_sector",
     "is_leveraged": False, "leverage_factor": 1},
], "cash_available": 464.16, "target_cash_weight": 0.05}


def test_overlay_broker_preferred_preserves_metadata(tmp_path):
    _setup(tmp_path, [{"symbol": "QQQ", "quantity": 6, "market_value": 4200.0, "average_cost": 700.0},
                      {"symbol": "NASA", "quantity": 15, "market_value": 300.0, "average_cost": 20.0}])
    out = hr.broker_overlaid_portfolio(_BLOCK, tmp_path)
    assert out["holdings_source"] == "broker"
    by = {h["symbol"]: h for h in out["holdings"]}
    assert by["NASA"]["shares"] == 15                      # broker shares
    assert by["NASA"]["target_weight"] == 0.10             # config metadata preserved
    assert "VFH" in by and by["VFH"]["shares"] == 0        # config-only 0-share target kept
    assert out["cash_available"] == 150.6                  # broker cash


def test_overlay_adds_broker_only_symbol_with_defaults(tmp_path):
    _setup(tmp_path, [{"symbol": "QQQ", "quantity": 6, "market_value": 4200.0, "average_cost": 700.0},
                      {"symbol": "CHAT", "quantity": 4, "market_value": 100.0, "average_cost": 24.0}])
    out = hr.broker_overlaid_portfolio(_BLOCK, tmp_path)
    by = {h["symbol"]: h for h in out["holdings"]}
    assert by["CHAT"]["shares"] == 4 and by["CHAT"]["target_weight"] == 0.0
    assert by["CHAT"]["asset_class"] == "us_equity"


def test_overlay_config_fallback_when_disabled(tmp_path):
    _setup(tmp_path, [{"symbol": "QQQ", "quantity": 6}], enabled=False)
    out = hr.broker_overlaid_portfolio(_BLOCK, tmp_path)
    assert out["holdings_source"] == "config"
    assert out["holdings"] == _BLOCK["holdings"] and out["cash_available"] == 464.16


def test_overlay_config_fallback_when_stale(tmp_path):
    L = tmp_path / "outputs" / "latest"; L.mkdir(parents=True)
    (tmp_path / "config.json").write_text(json.dumps({"portfolio": {"broker_aware": {"enabled": True}}}))
    (L / "schwab_positions.json").write_text(json.dumps({"positions": [{"symbol": "QQQ", "quantity": 6}]}))
    (L / "schwab_portfolio_snapshot.json").write_text(json.dumps(
        {"snapshot_timestamp": "2020-01-01T00:00:00+00:00", "totals": {"cash": 0}}))  # stale
    out = hr.broker_overlaid_portfolio(_BLOCK, tmp_path)
    assert out["holdings_source"] == "config"


def test_overlay_never_raises_on_garbage(tmp_path):
    out = hr.broker_overlaid_portfolio({"holdings": "bad"}, tmp_path)
    assert out["holdings_source"] in ("config", "broker")
```

- [ ] **Step 2: Run — expect FAIL** (`.venv/bin/python3 -m pytest -q tests/test_broker_overlay.py`) — no `broker_overlaid_portfolio`.

- [ ] **Step 3: Implement** — append to `portfolio_automation/holdings_resolver.py`:

```python
_OVERLAY_DEFAULTS = {"target_weight": 0.0, "asset_class": "us_equity",
                     "is_leveraged": False, "leverage_factor": 1}


def broker_overlaid_portfolio(portfolio_block: dict, root: Path | str,
                              now: "datetime | None" = None) -> dict:
    """Return a COPY of the config portfolio block with holdings shares + cash
    overlaid from the live broker snapshot (Schwab-preferred), preserving config
    per-symbol strategy metadata. Config fallback on stale/missing/disabled.
    Runtime-only — never writes config.json. Never raises.

    Adds observe fields: holdings_source ('broker'|'config'), confidence_modifier.
    """
    block = dict(portfolio_block or {})
    cfg_holdings = block.get("holdings") if isinstance(block.get("holdings"), list) else []
    try:
        res = resolve_holdings(Path(root), now=now)
    except Exception:
        res = {"holdings_source": "config", "confidence_modifier": 1.0}
    if res.get("holdings_source") != "broker":
        block["holdings_source"] = "config"
        block["confidence_modifier"] = res.get("confidence_modifier", 1.0)
        return block
    # broker path: merge broker shares over config, preserving config metadata
    by_sym = {str(h.get("symbol", "")).upper(): dict(h) for h in cfg_holdings if isinstance(h, dict)}
    merged: list[dict] = []
    broker_syms = set()
    for bh in res.get("holdings", []) or []:
        sym = str(bh.get("symbol", "")).upper()
        if not sym:
            continue
        broker_syms.add(sym)
        base = by_sym.get(sym, {"symbol": sym, **_OVERLAY_DEFAULTS})
        base = dict(base)
        base["symbol"] = sym
        base["shares"] = bh.get("quantity")
        for k, v in _OVERLAY_DEFAULTS.items():
            base.setdefault(k, v)
        merged.append(base)
    # keep config-only entries (e.g. 0-share allocation targets) not held at broker
    for sym, h in by_sym.items():
        if sym not in broker_syms:
            merged.append(h)
    block["holdings"] = merged
    block["cash_available"] = res.get("cash", block.get("cash_available"))
    block["holdings_source"] = "broker"
    block["confidence_modifier"] = res.get("confidence_modifier", 1.0)
    return block
```

(Ensure `from datetime import datetime` is importable for the annotation — it is already imported at module top.)

- [ ] **Step 4: Run — expect PASS (5)**. Also `.venv/bin/python3 -m pytest -q -k holdings_resolver`.

- [ ] **Step 5: Commit** (worktree branch): `git add portfolio_automation/holdings_resolver.py tests/test_broker_overlay.py && git commit -m "feat(broker-overlay): broker_overlaid_portfolio helper (Schwab-preferred holdings, config fallback)"`

---

## Task 2: Wire the CLI/standalone path (`__main__.py`)

**Files:** Modify `watchlist_scanner/__main__.py` (~513); Test `tests/test_broker_overlay.py`

- [ ] **Step 1: Add test**

Append to `tests/test_broker_overlay.py`:

```python
def test_main_module_imports_overlay():
    src = Path("watchlist_scanner/__main__.py").read_text(encoding="utf-8")
    assert "broker_overlaid_portfolio" in src
```

- [ ] **Step 2: Run — expect FAIL** (`-k main_module_imports_overlay`).

- [ ] **Step 3: Implement** — in `watchlist_scanner/__main__.py`, add import near the top:
```python
from portfolio_automation.holdings_resolver import broker_overlaid_portfolio
```
Then change the `portfolio_context=full_cfg.get("portfolio")` argument (~line 513) to:
```python
            portfolio_context=broker_overlaid_portfolio(full_cfg.get("portfolio") or {}, Path(".")),
```
(Confirm `Path` is imported in that file; if not, add `from pathlib import Path`.)

- [ ] **Step 4: Run** `-k main_module_imports_overlay` (PASS) + `.venv/bin/python3 -m py_compile watchlist_scanner/__main__.py` + `.venv/bin/python3 -m pytest -q -k "watchlist or scanner_run" -x` (no new failures; note pre-existing).

- [ ] **Step 5: Commit**: `git add watchlist_scanner/__main__.py tests/test_broker_overlay.py && git commit -m "feat(broker-overlay): wire overlay into watchlist_scanner CLI portfolio_context"`

---

## Task 3: Wire the live pipeline (`main.py` Config object)

**Files:** Modify `main.py`; Test `tests/test_broker_overlay.py`

- [ ] **Step 1: Add a Config-applier to the helper** (holdings_resolver.py) — append:

```python
def apply_broker_overlay_to_config(config, root: Path | str, now=None):
    """Overlay broker holdings/cash onto a utils.Config object in place (runtime).
    Rebuilds config.holdings as the same Holding type. Never raises; returns config."""
    try:
        block = {"holdings": [{"symbol": getattr(h, "symbol", None), "shares": getattr(h, "shares", None),
                               "target_weight": getattr(h, "target_weight", 0.0),
                               "asset_class": getattr(h, "asset_class", "us_equity"),
                               "is_leveraged": getattr(h, "is_leveraged", False),
                               "leverage_factor": getattr(h, "leverage_factor", 1)}
                              for h in getattr(config, "holdings", []) or []],
                 "cash_available": getattr(config, "cash_available", 0.0)}
        overlaid = broker_overlaid_portfolio(block, root, now=now)
        if overlaid.get("holdings_source") != "broker":
            return config
        HoldingCls = type(config.holdings[0]) if getattr(config, "holdings", None) else None
        if HoldingCls is None:
            return config
        new_holdings = []
        for h in overlaid["holdings"]:
            new_holdings.append(HoldingCls(symbol=h["symbol"], shares=h.get("shares") or 0,
                                           target_weight=h.get("target_weight", 0.0),
                                           asset_class=h.get("asset_class", "us_equity"),
                                           is_leveraged=h.get("is_leveraged", False),
                                           leverage_factor=h.get("leverage_factor", 1)))
        config.holdings = new_holdings
        config.cash_available = overlaid.get("cash_available", config.cash_available)
    except Exception:
        return config
    return config
```

> NOTE: READ `utils.py:280-289` first to confirm the `Holding(...)` constructor's exact kwargs and match them. If `Holding` has more/other required fields, mirror that constructor exactly. If the kwargs differ, adjust this builder — the test in Step 2 will catch a mismatch.

- [ ] **Step 2: Add test**

Append to `tests/test_broker_overlay.py`:

```python
def test_apply_overlay_to_config_object(tmp_path, monkeypatch):
    from utils import Config
    import portfolio_automation.holdings_resolver as hrmod
    # fake a broker result so we don't need real artifacts
    monkeypatch.setattr(hrmod, "resolve_holdings", lambda root, now=None: {
        "holdings_source": "broker", "confidence_modifier": 1.0, "cash": 150.6,
        "holdings": [{"symbol": "NASA", "quantity": 15}, {"symbol": "QQQ", "quantity": 6}]})
    from utils import load_config
    cfg = load_config("config.json")          # real config (already Schwab-synced from Part A)
    before_syms = {h.symbol for h in cfg.holdings}
    cfg2 = hrmod.apply_broker_overlay_to_config(cfg, ".")
    by = {h.symbol: h for h in cfg2.holdings}
    assert by["NASA"].shares == 15            # overlaid from (faked) broker
    assert by["QQQ"].target_weight == 0.35    # config metadata preserved
```

- [ ] **Step 3: Run — expect FAIL** (`-k apply_overlay_to_config`), then it passes once Step 1 lands. Fix the `Holding(...)` kwargs if the test errors on construction.

- [ ] **Step 4: Wire main.py** — READ `main.py` around `config = load_config(...)` (the daily-run entry; NOT the `__main__` arg-parse block at 2956 — find the function that runs the pipeline, where `config` is loaded then holdings objects built ~682). Immediately AFTER `validate_config(config)` and BEFORE the holdings objects are built, insert:
```python
        from portfolio_automation.holdings_resolver import apply_broker_overlay_to_config
        config = apply_broker_overlay_to_config(config, ".")
```
(Place it so every downstream consumer — priced holdings objects at ~682, the `portfolio_context` at ~1569, decision_plan — sees the overlaid config. Gated internally by `broker_aware.enabled`; off → returns config unchanged.)

- [ ] **Step 5: Run** `.venv/bin/python3 -m pytest -q tests/test_broker_overlay.py` (all pass) + `.venv/bin/python3 -m py_compile main.py`.

- [ ] **Step 6: Commit**: `git add main.py portfolio_automation/holdings_resolver.py tests/test_broker_overlay.py && git commit -m "feat(broker-overlay): overlay broker holdings onto Config in the live pipeline"`

---

## Task 4: Telemetry + daily health coverage

**Files:** Modify `.claude/commands/daily-tool-analysis.md`; Test `tests/test_broker_overlay.py`

- [ ] **Step 1: Telemetry** — the overlaid block carries `holdings_source`/`confidence_modifier`. In `main.py` right after the overlay call (Task 3 Step 4), record it observably:
```python
        try:
            import json as _json
            _hs = getattr(config, "holdings_source", None)
        except Exception:
            _hs = None
```
Simpler + robust: have `apply_broker_overlay_to_config` also write a tiny observe artifact. Append to that function (before `return config`, inside the success path) a non-fatal write:
```python
        try:
            from portfolio_automation.data_governance import OutputNamespace, safe_write_json
            safe_write_json(OutputNamespace.LATEST, "decision_holdings_source.json",
                            {"observe_only": True, "holdings_source": overlaid.get("holdings_source"),
                             "confidence_modifier": overlaid.get("confidence_modifier"),
                             "generated_at": (now.isoformat() if now else None)},
                            base_dir="outputs")
        except Exception:
            pass
```

- [ ] **Step 2: Test the artifact** — append to `tests/test_broker_overlay.py`:
```python
def test_overlay_writes_holdings_source_artifact(tmp_path, monkeypatch):
    import portfolio_automation.holdings_resolver as hrmod
    from utils import load_config
    monkeypatch.setattr(hrmod, "resolve_holdings", lambda root, now=None: {
        "holdings_source": "broker", "confidence_modifier": 1.0, "cash": 150.6,
        "holdings": [{"symbol": "QQQ", "quantity": 6}]})
    monkeypatch.chdir(tmp_path)
    # minimal config with one holding so Holding type is discoverable
    import json, shutil, os
    shutil.copy("/opt/stockbot/config.json", tmp_path / "config.json")
    cfg = load_config(str(tmp_path / "config.json"))
    hrmod.apply_broker_overlay_to_config(cfg, str(tmp_path))
    p = tmp_path / "outputs" / "latest" / "decision_holdings_source.json"
    assert p.exists() and json.loads(p.read_text())["holdings_source"] == "broker"
```

- [ ] **Step 3: Run** `-k holdings_source_artifact` (PASS).

- [ ] **Step 4: daily-tool-analysis** — add to artifacts-read: `outputs/latest/decision_holdings_source.json` → `holdings_source`, `confidence_modifier`. Add AMBER bullet:
```markdown
- `decision_on_config_while_broker_ok` = `broker_aware` enabled AND `broker_sync_status.overall_status == ok` AND `decision_holdings_source.holdings_source == "config"` (the decision run fell back to config holdings despite Schwab being live — check broker snapshot freshness). Advisory; never RED.
```

- [ ] **Step 5: Commit**: `git add .claude/commands/daily-tool-analysis.md portfolio_automation/holdings_resolver.py tests/test_broker_overlay.py && git commit -m "feat(broker-overlay): decision holdings_source telemetry + daily health coverage"`

---

## Task 5: Regression (decision_plan unchanged today) + finalize

**Files:** none (verification + merge)

- [ ] **Step 1: No-regression proof** — since Part A made config == Schwab, the overlay must not change today's pipeline output. From the worktree, run a dry pipeline pass with the overlay and confirm `decision_plan` holdings reflect the same 5 symbols (NASA 15, QQQ 6, GLD 4, QLD 8, CHAT 4) and no error:
```bash
.venv/bin/python3 -c "
from pathlib import Path
from utils import load_config, validate_config
from portfolio_automation.holdings_resolver import apply_broker_overlay_to_config
import os; os.chdir('/opt/stockbot')
c = load_config('config.json'); validate_config(c)
c2 = apply_broker_overlay_to_config(c, '.')
print('source:', getattr(c2,'holdings_source','?'))
print('holdings:', sorted((h.symbol, h.shares) for h in c2.holdings))
"
```
Expect `source: broker` and the 5 Schwab symbols (+ VFH/VXUS 0-share targets) — matching config.

- [ ] **Step 2: Full suite** — `.venv/bin/python3 -m pytest -q` — PASS except the 3 known pre-existing failures; **no new failures** (decision_engine/portfolio_fit/postprocess tests stay green — they get the same context shape).

- [ ] **Step 3: signal_registry guard** — `grep -q "default_weight: 0.4947" config/signal_registry.yaml || git checkout -- config/signal_registry.yaml`.

- [ ] **Step 4: Finalize (controller)** — rebase the worktree branch onto `origin/main`, ff-merge into main, push, remove worktree.

- [ ] **Step 5: Restart + test** — `sudo systemctl restart stockbot-dashboard.service`; provide a test link.

---

## Notes for the implementer
- Run all Python via `.venv/bin/python3`.
- **NEVER edit `decision_engine.py`** or scoring/recommendation math. Only the holdings INPUT changes.
- **NEVER write `config.json`** — the overlay is runtime/in-memory only.
- If the `Holding(...)` constructor kwargs differ from the assumed `symbol/shares/target_weight/asset_class/is_leveraged/leverage_factor`, mirror the real ones from `utils.py:280-289`.
- The `broker_aware.enabled` flag (already true) gates everything via `resolve_holdings`; flipping it false reverts to config instantly.
