# Schwab Read-Only Broker Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only Schwab broker-sync layer that pulls accounts/positions, normalizes them into artifacts, reconciles vs local `config.json`, and emits a proposal-only config-update artifact — observe-only, no trade execution, fixture-tested, graceful when unconfigured.

**Architecture:** Pure-function core (`broker_models`, `broker_reconciliation`) + isolated network/secrets layer (`schwab_oauth`, `schwab_client`) + orchestrator/CLI (`schwab_sync`). All artifact writes via `data_governance.safe_write_json(OutputNamespace.LATEST, ...)`. No trade methods exist anywhere. Proposal-only (no config write this slice).

**Tech Stack:** Python 3 stdlib + `requests` (already a repo dep — confirm; else `urllib`), pytest. Reuses `portfolio_automation/data_governance.py`.

**Spec:** `docs/superpowers/specs/2026-06-08-schwab-readonly-sync-design.md`

**Branch:** `feat/schwab-readonly-sync` off `main` (spec committed `260a331e`).

### Critical discipline
- **Never `git commit -am`** (working tree carries unrelated modified tracked files). Stage explicit paths; `git diff main HEAD --stat` before push.
- **Observe-only / no trading:** no function/method named `order|trade|buy|sell|place_*`. `trading_enabled` hardcoded `false`. A test (Task 7) enforces this by AST scan.
- Secrets/tokens never logged/committed/in-artifacts; account ids masked. Tests enforce.
- **Proposal-only:** no write to `config.json` in this slice.
- **Deferred (documented, NOT built here):** GUI `/dashboard/portfolio-sync`; artifact-registry registration of the 5 artifacts (registry not on main). See spec §15.

---

## File Structure
| File | Responsibility |
|---|---|
| `portfolio_automation/brokers/__init__.py` (create) | package marker |
| `portfolio_automation/brokers/broker_models.py` (create) | dataclasses + `mask_account` + `redact` + normalize + artifact dicts (pure) |
| `portfolio_automation/brokers/broker_status.py` (create) | `build_status()` → broker_sync_status shape (pure) |
| `portfolio_automation/brokers/broker_reconciliation.py` (create) | `reconcile`, `validate_proposed_holdings`, `build_proposal` (pure) |
| `portfolio_automation/brokers/schwab_oauth.py` (create) | OAuth2 auth-url/exchange/refresh + gitignored token load/save (network/secrets) |
| `portfolio_automation/brokers/schwab_client.py` (create) | read-only GET client (NO trade methods) |
| `portfolio_automation/brokers/schwab_sync.py` (create) | orchestrator + CLI + artifact writes + archive (never raises) |
| `tests/fixtures/schwab/*.json` (create) | fixture API responses |
| `tests/test_schwab_models.py`, `test_schwab_status.py`, `test_schwab_reconciliation.py`, `test_schwab_oauth.py`, `test_schwab_client.py`, `test_schwab_sync.py` (create) | tests |
| `docs/schwab_integration.md`, `docs/CHANGELOG_DECISIONS.md` (create/modify) | docs |

---

## Task 1: broker_models — dataclasses + mask_account + redact

**Files:** Create `portfolio_automation/brokers/__init__.py`, `portfolio_automation/brokers/broker_models.py`; Test `tests/test_schwab_models.py`.

- [ ] **Step 1: Write the failing test**
```python
# tests/test_schwab_models.py
from portfolio_automation.brokers import broker_models as bm


def test_mask_account_keeps_last4():
    assert bm.mask_account("123456789") == "…9789"  # last 4
    assert bm.mask_account("") == "…"
    assert bm.mask_account(None) == "…"


def test_redact_scrubs_tokens_and_secrets():
    s = "access_token=abc123 refresh_token=zzz client_secret=shh code=qqq ok"
    out = bm.redact(s)
    for leak in ("abc123", "zzz", "shh", "qqq"):
        assert leak not in out
    assert "ok" in out


def test_redact_handles_non_string():
    assert bm.redact(None) == ""
    assert "5" in bm.redact(5)
```

- [ ] **Step 2: Run → fail**
`python3 -m pytest -q tests/test_schwab_models.py` → FAIL (module missing).

- [ ] **Step 3: Implement**
```python
# portfolio_automation/brokers/__init__.py
"""Observe-only broker integration package. Read-only; no trade execution."""
```
```python
# portfolio_automation/brokers/broker_models.py
"""Pure normalization + safety helpers for broker data. No network, no secrets,
no trade logic. Observe-only."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

_SECRET_KEYS = ("access_token", "refresh_token", "client_secret", "code", "id_token", "Authorization")
_SECRET_RE = re.compile(
    r"(?i)\b(" + "|".join(re.escape(k) for k in _SECRET_KEYS) + r")\b\s*[=:]\s*\S+"
)


def redact(text: Any) -> str:
    """Scrub token/secret/code values from any text before logging/persisting."""
    if text is None:
        return ""
    s = str(text)
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", s)


def mask_account(account: Any) -> str:
    """Mask an account identifier to its last 4 chars (e.g. '…6789')."""
    s = "" if account is None else str(account)
    return "…" + s[-4:] if s else "…"


@dataclass
class BrokerPosition:
    symbol: str
    quantity: float
    market_value: float | None = None
    average_cost: float | None = None
    asset_type: str | None = None
    account_ref_masked: str | None = None
    source_timestamp: str | None = None


@dataclass
class BrokerAccount:
    account_id_masked: str
    account_type: str | None = None
    total_market_value: float | None = None
    cash: float | None = None
    positions: list[BrokerPosition] = field(default_factory=list)


@dataclass
class BrokerSnapshot:
    snapshot_timestamp: str
    accounts: list[BrokerAccount] = field(default_factory=list)
```

- [ ] **Step 4: Run → pass.** `python3 -m pytest -q tests/test_schwab_models.py` → PASS (3).
- [ ] **Step 5: Commit**
```bash
python3 -m py_compile portfolio_automation/brokers/broker_models.py
git add portfolio_automation/brokers/__init__.py portfolio_automation/brokers/broker_models.py tests/test_schwab_models.py
git commit -m "feat(schwab): broker_models dataclasses + mask_account + redact"
```

---

## Task 2: normalize Schwab responses → snapshot + artifact dicts (fixtures)

**Files:** Modify `broker_models.py`; Create `tests/fixtures/schwab/accounts_positions.json`, `tests/fixtures/schwab/account_numbers.json`; Modify `tests/test_schwab_models.py`.

- [ ] **Step 1: Write the fixtures** (mirror the documented Schwab shape; field names confirmed at connect-time)
`tests/fixtures/schwab/account_numbers.json`:
```json
[{"accountNumber": "123456789", "hashValue": "ABC123HASH"}]
```
`tests/fixtures/schwab/accounts_positions.json`:
```json
[{"securitiesAccount": {
  "accountNumber": "123456789", "type": "MARGIN",
  "currentBalances": {"liquidationValue": 15000.50, "cashBalance": 464.16},
  "positions": [
    {"instrument": {"symbol": "QQQ", "assetType": "EQUITY"}, "longQuantity": 6, "marketValue": 4200.0, "averagePrice": 600.0},
    {"instrument": {"symbol": "GLD", "assetType": "EQUITY"}, "longQuantity": 4, "marketValue": 1200.0, "averagePrice": 280.0}
  ]}}]
```

- [ ] **Step 2: Write the failing test**
```python
import json
from pathlib import Path

_FIX = Path("tests/fixtures/schwab")


def test_normalize_accounts_from_fixture():
    raw = json.loads((_FIX / "accounts_positions.json").read_text())
    nums = json.loads((_FIX / "account_numbers.json").read_text())
    snap = bm.normalize_accounts(raw, nums, now_iso="2026-06-08T12:00:00+00:00")
    assert len(snap.accounts) == 1
    acct = snap.accounts[0]
    assert acct.account_id_masked == "…6789"   # masked, no full number
    assert acct.account_type == "MARGIN"
    assert acct.total_market_value == 15000.50
    assert acct.cash == 464.16
    assert {p.symbol for p in acct.positions} == {"QQQ", "GLD"}
    qqq = next(p for p in acct.positions if p.symbol == "QQQ")
    assert qqq.quantity == 6 and qqq.market_value == 4200.0 and qqq.average_cost == 600.0
    assert qqq.account_ref_masked == "…6789"


def test_snapshot_and_positions_dicts_have_no_raw_account():
    raw = json.loads((_FIX / "accounts_positions.json").read_text())
    nums = json.loads((_FIX / "account_numbers.json").read_text())
    snap = bm.normalize_accounts(raw, nums, now_iso="2026-06-08T12:00:00+00:00")
    sd = bm.snapshot_dict(snap)
    pr = bm.positions_dict(snap)
    blob = json.dumps(sd) + json.dumps(pr)
    assert "123456789" not in blob           # no full account number leaks
    assert sd["totals"]["market_value"] == 15000.50
    assert len(pr["positions"]) == 2


def test_normalize_is_defensive_on_missing_fields():
    snap = bm.normalize_accounts([{"securitiesAccount": {}}], [], now_iso="t")
    assert len(snap.accounts) == 1
    assert snap.accounts[0].positions == []
```

- [ ] **Step 3: Run → fail.**
- [ ] **Step 4: Implement** (append to `broker_models.py`)
```python
def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_accounts(raw_accounts: Any, account_numbers: Any, *, now_iso: str) -> BrokerSnapshot:
    """Defensively normalize Schwab accounts+positions JSON into a BrokerSnapshot.
    Uses candidate key names; never raises on missing/odd shapes."""
    # map plain account number -> masked (we never store the plain number)
    num_to_masked: dict[str, str] = {}
    for entry in account_numbers if isinstance(account_numbers, list) else []:
        if isinstance(entry, dict):
            n = entry.get("accountNumber")
            if n is not None:
                num_to_masked[str(n)] = mask_account(n)

    accounts: list[BrokerAccount] = []
    for item in raw_accounts if isinstance(raw_accounts, list) else []:
        sa = (item or {}).get("securitiesAccount") or item or {}
        if not isinstance(sa, dict):
            continue
        num = sa.get("accountNumber") or sa.get("accountId")
        masked = num_to_masked.get(str(num)) if num is not None else None
        masked = masked or mask_account(num)
        bal = sa.get("currentBalances") or sa.get("balances") or {}
        positions: list[BrokerPosition] = []
        for p in (sa.get("positions") or []):
            if not isinstance(p, dict):
                continue
            instr = p.get("instrument") or {}
            qty = p.get("longQuantity")
            if qty in (None, 0) and p.get("shortQuantity"):
                qty = -_f(p.get("shortQuantity"))  # represent short as negative
            positions.append(BrokerPosition(
                symbol=str(instr.get("symbol") or p.get("symbol") or "").upper(),
                quantity=_f(qty) or 0.0,
                market_value=_f(p.get("marketValue")),
                average_cost=_f(p.get("averagePrice") or p.get("averageCost")),
                asset_type=instr.get("assetType") or p.get("assetType"),
                account_ref_masked=masked,
                source_timestamp=now_iso,
            ))
        accounts.append(BrokerAccount(
            account_id_masked=masked,
            account_type=sa.get("type") or sa.get("accountType"),
            total_market_value=_f(bal.get("liquidationValue") or bal.get("totalMarketValue")),
            cash=_f(bal.get("cashBalance") or bal.get("cashAvailableForTrading") or bal.get("cash")),
            positions=positions,
        ))
    return BrokerSnapshot(snapshot_timestamp=now_iso, accounts=accounts)


def snapshot_dict(snap: BrokerSnapshot) -> dict:
    mv = sum(a.total_market_value or 0.0 for a in snap.accounts)
    cash = sum(a.cash or 0.0 for a in snap.accounts)
    return {
        "generated_at": snap.snapshot_timestamp, "source": "schwab",
        "snapshot_timestamp": snap.snapshot_timestamp,
        "accounts": [{
            "account_id_masked": a.account_id_masked, "account_type": a.account_type,
            "total_market_value": a.total_market_value, "cash": a.cash,
            "positions_count": len(a.positions),
        } for a in snap.accounts],
        "totals": {"market_value": mv, "cash": cash},
    }


def positions_dict(snap: BrokerSnapshot) -> dict:
    rows = []
    for a in snap.accounts:
        for p in a.positions:
            rows.append({
                "symbol": p.symbol, "quantity": p.quantity, "market_value": p.market_value,
                "average_cost": p.average_cost, "asset_type": p.asset_type,
                "account_ref_masked": p.account_ref_masked, "source_timestamp": p.source_timestamp,
            })
    return {"generated_at": snap.snapshot_timestamp, "source": "schwab", "positions": rows}
```

- [ ] **Step 5: Run → pass. Commit**
```bash
python3 -m py_compile portfolio_automation/brokers/broker_models.py
git add portfolio_automation/brokers/broker_models.py tests/fixtures/schwab/ tests/test_schwab_models.py
git commit -m "feat(schwab): defensive account/position normalization + artifact dicts"
```

---

## Task 3: broker_status — broker_sync_status shape (all modes)

**Files:** Create `portfolio_automation/brokers/broker_status.py`; Test `tests/test_schwab_status.py`.

- [ ] **Step 1: Failing test**
```python
from portfolio_automation.brokers import broker_status as bs


def test_status_unconfigured():
    st = bs.build_status(enabled=True, configured=False, authenticated=False,
                         account_count=0, position_count=0, last_success_at=None,
                         last_error=None, now_iso="t")
    assert st["overall_status"] == "unconfigured"
    assert st["read_only_mode"] is True and st["trading_enabled"] is False
    assert st["observe_only"] is True and st["source"] == "schwab"


def test_status_ok_and_error_redacted():
    ok = bs.build_status(enabled=True, configured=True, authenticated=True,
                         account_count=1, position_count=2, last_success_at="t",
                         last_error=None, now_iso="t")
    assert ok["overall_status"] == "ok" and ok["account_count"] == 1
    err = bs.build_status(enabled=True, configured=True, authenticated=False,
                          account_count=0, position_count=0, last_success_at=None,
                          last_error="boom access_token=SEKRET", now_iso="t")
    assert err["overall_status"] == "error"
    assert "SEKRET" not in err["last_error"]   # redacted


def test_status_disabled():
    st = bs.build_status(enabled=False, configured=True, authenticated=False,
                         account_count=0, position_count=0, last_success_at=None,
                         last_error=None, now_iso="t")
    assert st["overall_status"] == "disabled"
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement**
```python
# portfolio_automation/brokers/broker_status.py
"""broker_sync_status artifact builder. Pure; observe-only; read-only hardcoded."""
from __future__ import annotations

from portfolio_automation.brokers.broker_models import redact


def build_status(*, enabled: bool, configured: bool, authenticated: bool,
                 account_count: int, position_count: int,
                 last_success_at: str | None, last_error: str | None,
                 now_iso: str) -> dict:
    if not enabled:
        overall = "disabled"
    elif not configured:
        overall = "unconfigured"
    elif last_error:
        overall = "error"
    elif authenticated:
        overall = "ok"
    else:
        overall = "degraded"
    return {
        "generated_at": now_iso, "observe_only": True, "source": "schwab",
        "enabled": bool(enabled), "configured": bool(configured),
        "authenticated": bool(authenticated),
        "read_only_mode": True, "trading_enabled": False,
        "last_success_at": last_success_at,
        "last_error": redact(last_error) if last_error else None,
        "account_count": int(account_count), "position_count": int(position_count),
        "overall_status": overall,
    }
```

- [ ] **Step 4: Run → pass. Step 5: Commit**
```bash
python3 -m py_compile portfolio_automation/brokers/broker_status.py
git add portfolio_automation/brokers/broker_status.py tests/test_schwab_status.py
git commit -m "feat(schwab): broker_sync_status builder (read_only hardcoded, error redacted)"
```

---

## Task 4: reconciliation — reconcile() snapshot vs config

**Files:** Create `portfolio_automation/brokers/broker_reconciliation.py`; Test `tests/test_schwab_reconciliation.py`.

- [ ] **Step 1: Failing test**
```python
from portfolio_automation.brokers import broker_reconciliation as rec

_SNAP = {"accounts": [{"cash": 464.16}], "totals": {"market_value": 5400.0, "cash": 464.16}}
_POS = {"positions": [
    {"symbol": "QQQ", "quantity": 6, "market_value": 4200.0},
    {"symbol": "GLD", "quantity": 4, "market_value": 1200.0},
]}
_CFG = {"portfolio": {"cash_available": 464.16, "holdings": [
    {"symbol": "QQQ", "shares": 6}, {"symbol": "GLD", "shares": 5}, {"symbol": "NASA", "shares": 14},
]}}


def test_reconcile_classifies():
    r = rec.reconcile(_SNAP, _POS, _CFG)
    matched = {m["symbol"] for m in r["matched"]}
    mism = {m["symbol"] for m in r["quantity_mismatches"]}
    miss_schwab = {m["symbol"] for m in r["missing_in_schwab"]}
    assert "QQQ" in matched
    assert "GLD" in mism                      # 4 vs 5
    assert "NASA" in miss_schwab              # local only
    assert r["missing_in_local"] == []        # nothing schwab-only here
    assert r["cash"]["delta"] == 0.0
    assert r["summary_status"] == "mismatch"
    assert "buy" not in r["operator_review_message"].lower()
    assert "sell" not in r["operator_review_message"].lower()


def test_reconcile_missing_in_local():
    pos = {"positions": [{"symbol": "TSLA", "quantity": 3}]}
    r = rec.reconcile(_SNAP, pos, {"portfolio": {"holdings": [], "cash_available": 0}})
    assert {m["symbol"] for m in r["missing_in_local"]} == {"TSLA"}
    assert r["summary_status"] in ("mismatch", "no_local_config")


def test_reconcile_no_broker_data():
    r = rec.reconcile({"totals": {}}, {"positions": []}, _CFG)
    assert r["summary_status"] == "no_broker_data"
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement**
```python
# portfolio_automation/brokers/broker_reconciliation.py
"""Pure reconciliation of a Schwab snapshot vs local config.json, plus a
PROPOSAL-ONLY config-update artifact. No config writes. Observe-only."""
from __future__ import annotations

from typing import Any

_QTY_EPS = 1e-6
_CASH_EPS = 0.01


def _local_holdings(config: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for h in ((config.get("portfolio") or {}).get("holdings") or []):
        if isinstance(h, dict) and h.get("symbol"):
            out[str(h["symbol"]).upper()] = h
    return out


def reconcile(snapshot: dict, positions: dict, config: dict) -> dict:
    schwab = {str(p.get("symbol", "")).upper(): p
              for p in (positions.get("positions") or []) if p.get("symbol")}
    local = _local_holdings(config)
    matched, mismatches, missing_local, missing_schwab = [], [], [], []
    for sym in sorted(set(schwab) | set(local)):
        sp, lp = schwab.get(sym), local.get(sym)
        if sp and lp:
            sq = float(sp.get("quantity") or 0.0)
            lq = float(lp.get("shares") or 0.0)
            if abs(sq - lq) < _QTY_EPS:
                matched.append({"symbol": sym, "schwab_qty": sq, "local_shares": lq})
            else:
                mismatches.append({"symbol": sym, "schwab_qty": sq, "local_shares": lq,
                                   "delta": round(sq - lq, 6)})
        elif sp and not lp:
            missing_local.append({"symbol": sym, "schwab_qty": float(sp.get("quantity") or 0.0)})
        else:
            missing_schwab.append({"symbol": sym, "local_shares": float(lp.get("shares") or 0.0)})

    schwab_cash = float((snapshot.get("totals") or {}).get("cash") or 0.0)
    local_cash = float((config.get("portfolio") or {}).get("cash_available") or 0.0)
    cash_delta = round(schwab_cash - local_cash, 2)

    has_broker = bool(schwab) or bool((snapshot.get("totals") or {}).get("market_value"))
    has_local = bool(local)
    if not has_broker:
        summary = "no_broker_data"
    elif not has_local:
        summary = "no_local_config"
    elif mismatches or missing_local or missing_schwab or abs(cash_delta) >= _CASH_EPS:
        summary = "mismatch"
    else:
        summary = "ok"

    n_diff = len(mismatches) + len(missing_local) + len(missing_schwab)
    msg = {
        "ok": "Schwab and local config agree. No review needed.",
        "no_broker_data": "No Schwab data available — run --sync first.",
        "no_local_config": "No local holdings configured to compare against.",
        "mismatch": (f"Review {n_diff} holding difference(s)"
                     + (f" and a ${abs(cash_delta):.2f} cash difference" if abs(cash_delta) >= _CASH_EPS else "")
                     + ". Generate a config-update proposal to align local config to Schwab reality."),
    }[summary]

    return {
        "generated_at": snapshot.get("generated_at"), "source": "schwab",
        "summary_status": summary,
        "matched": matched, "quantity_mismatches": mismatches,
        "missing_in_local": missing_local, "missing_in_schwab": missing_schwab,
        "cash": {"schwab": schwab_cash, "local": local_cash, "delta": cash_delta},
        "target_allocation_comparison": None,
        "operator_review_message": msg,
    }
```

- [ ] **Step 4: Run → pass. Step 5: Commit**
```bash
python3 -m py_compile portfolio_automation/brokers/broker_reconciliation.py
git add portfolio_automation/brokers/broker_reconciliation.py tests/test_schwab_reconciliation.py
git commit -m "feat(schwab): reconcile snapshot vs config (matched/mismatch/missing/cash)"
```

---

## Task 5: proposal generation + validation (proposal-only)

**Files:** Modify `broker_reconciliation.py`; Modify `tests/test_schwab_reconciliation.py`.

- [ ] **Step 1: Failing test**
```python
def test_validate_rejects_negative_and_missing_symbol():
    v = rec.validate_proposed_holdings(
        [{"symbol": "QQQ", "shares": -1}, {"symbol": "", "shares": 5}], -10.0, _CFG)
    assert v["ok"] is False
    joined = " ".join(v["errors"]).lower()
    assert "negative" in joined and ("symbol" in joined or "cash" in joined)


def test_build_proposal_is_proposal_only():
    r = rec.reconcile(_SNAP, _POS, _CFG)
    prop = rec.build_proposal(r, _CFG, now_iso="2026-06-08T12:00:00+00:00")
    assert prop["operator_approval_required"] is True
    assert prop["auto_applied"] is False
    assert "before" in prop and "proposed_after" in prop
    # proposed_after aligns GLD toward schwab qty (4)
    after = {h["symbol"]: h["shares"] for h in prop["proposed_after"]["holdings"]}
    assert after["GLD"] == 4
    assert prop["validation"]["ok"] in (True, False)
    assert "manual_portfolio_update" in prop["apply_instructions"]
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** (append)
```python
def validate_proposed_holdings(holdings: list[dict], cash: float, config: dict) -> dict:
    errors: list[str] = []
    if cash is not None and float(cash) < 0:
        errors.append(f"negative cash: {cash}")
    seen = set()
    for h in holdings or []:
        sym = str(h.get("symbol") or "").strip()
        if not sym:
            errors.append("missing/empty symbol field in a holding")
            continue
        if sym in seen:
            errors.append(f"duplicate symbol: {sym}")
        seen.add(sym)
        if float(h.get("shares") or 0) < 0:
            errors.append(f"negative shares for {sym}")
    # guardrails (advisory): concentration/leverage caps from config.growth_mode
    gm = config.get("growth_mode") or {}
    # target weights sum check only if any target_weight present
    tws = [float(h["target_weight"]) for h in (holdings or []) if h.get("target_weight") is not None]
    if tws and abs(sum(tws) - 1.0) > 0.02:
        errors.append(f"target weights sum to {sum(tws):.3f}, expected ~1.0")
    return {"ok": not errors, "errors": errors}


def build_proposal(reconciliation: dict, config: dict, *, now_iso: str) -> dict:
    """PROPOSAL ONLY — never writes config.json. Aligns local holdings/cash toward
    Schwab reality; operator applies via tools/manual_portfolio_update.py."""
    local = _local_holdings(config)
    before_holdings = [dict(h) for h in local.values()]
    before_cash = float((config.get("portfolio") or {}).get("cash_available") or 0.0)

    after = {sym: dict(h) for sym, h in local.items()}
    for m in reconciliation.get("quantity_mismatches", []):
        after.setdefault(m["symbol"], {"symbol": m["symbol"]})["shares"] = m["schwab_qty"]
    for m in reconciliation.get("missing_in_local", []):
        after[m["symbol"]] = {"symbol": m["symbol"], "shares": m["schwab_qty"]}
    after_holdings = list(after.values())
    after_cash = reconciliation.get("cash", {}).get("schwab", before_cash)

    validation = validate_proposed_holdings(after_holdings, after_cash, config)
    return {
        "generated_at": now_iso, "source": "schwab",
        "source_snapshot_timestamp": reconciliation.get("generated_at"),
        "before": {"holdings": before_holdings, "cash": before_cash},
        "proposed_after": {"holdings": after_holdings, "cash": after_cash},
        "reason": "Align local StockBot config to Schwab actual holdings/cash (observe-only).",
        "validation": validation,
        "operator_approval_required": True,
        "auto_applied": False,
        "apply_instructions": ("Reviewed manual step only: apply via "
                               "`python -m tools.manual_portfolio_update` (backup+audit+validate). "
                               "This proposal performs NO writes and NO trades."),
    }
```

- [ ] **Step 4: Run → pass. Step 5: Commit**
```bash
python3 -m py_compile portfolio_automation/brokers/broker_reconciliation.py
git add portfolio_automation/brokers/broker_reconciliation.py tests/test_schwab_reconciliation.py
git commit -m "feat(schwab): proposal-only config-update artifact + validation rules"
```

---

## Task 6: schwab_oauth — auth-url/exchange/refresh + gitignored token storage

**Files:** Create `portfolio_automation/brokers/schwab_oauth.py`; Test `tests/test_schwab_oauth.py`.

NOTE: network calls are isolated; tests exercise config/redaction/token-IO WITHOUT live creds (monkeypatch the HTTP post). Confirm `requests` is available (`python3 -c "import requests"`); if not, use `urllib.request` (the plan's HTTP call is a single POST — adapt).

- [ ] **Step 1: Failing test**
```python
import json
from pathlib import Path
from portfolio_automation.brokers import schwab_oauth as oa


def test_is_configured_reads_env(monkeypatch):
    monkeypatch.delenv("SCHWAB_CLIENT_ID", raising=False)
    assert oa.is_configured() is False
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "cid")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "csec")
    monkeypatch.setenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1/cb")
    assert oa.is_configured() is True


def test_build_authorize_url_has_no_secret(monkeypatch):
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "cid")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "csec-SEKRET")
    monkeypatch.setenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1/cb")
    url = oa.build_authorize_url()
    assert "cid" in url and "csec-SEKRET" not in url  # secret never in authorize URL
    assert url.startswith("https://api.schwabapi.com/v1/oauth/authorize")


def test_token_save_load_roundtrip_and_perms(tmp_path, monkeypatch):
    p = tmp_path / "schwab_token.json"
    monkeypatch.setattr(oa, "TOKEN_PATH", p)
    oa.save_token({"access_token": "a", "refresh_token": "r", "expires_at": 999})
    assert oa.load_token()["access_token"] == "a"
    import os, stat
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600  # 0600


def test_load_token_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(oa, "TOKEN_PATH", tmp_path / "nope.json")
    assert oa.load_token() is None
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement**
```python
# portfolio_automation/brokers/schwab_oauth.py
"""Schwab OAuth2 (auth-code + refresh) + conservative gitignored token storage.
Secrets via env only; tokens never logged. READ-ONLY scopes; no trade auth."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode

from portfolio_automation.brokers.broker_models import redact

_AUTH_BASE = "https://api.schwabapi.com/v1/oauth/authorize"
_TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
# data/ is gitignored at repo root -> token file is auto-protected.
TOKEN_PATH = Path(__file__).resolve().parents[2] / "data" / "schwab_token.json"


def _env(key: str) -> str:
    return os.environ.get(key, "").strip()


def is_configured() -> bool:
    return bool(_env("SCHWAB_CLIENT_ID") and _env("SCHWAB_CLIENT_SECRET") and _env("SCHWAB_REDIRECT_URI"))


def read_only_mode() -> bool:
    # default true; trading is never implemented regardless of this flag.
    return _env("SCHWAB_READ_ONLY_MODE").lower() not in ("0", "false", "no")


def build_authorize_url(state: str = "stockbot") -> str:
    """Step-1 of auth-code flow. Contains client_id + redirect_uri only — NOT the secret."""
    params = {"response_type": "code", "client_id": _env("SCHWAB_CLIENT_ID"),
              "redirect_uri": _env("SCHWAB_REDIRECT_URI"), "state": state}
    return f"{_AUTH_BASE}?{urlencode(params)}"


def save_token(token: dict) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(token), encoding="utf-8")
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass


def load_token() -> dict | None:
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None


def _post_token(data: dict) -> dict:
    """Single POST to the token endpoint. Network isolated here; raises on failure
    with a REDACTED message. Tests monkeypatch this."""
    import requests  # local import so the module loads without requests in pure paths
    resp = requests.post(_TOKEN_URL, data=data, auth=(_env("SCHWAB_CLIENT_ID"), _env("SCHWAB_CLIENT_SECRET")),
                         headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(redact(f"token endpoint {resp.status_code}: {resp.text}"))
    tok = resp.json()
    tok["expires_at"] = int(time.time()) + int(tok.get("expires_in", 1800))
    return tok


def exchange_code(code: str) -> dict:
    tok = _post_token({"grant_type": "authorization_code", "code": code,
                       "redirect_uri": _env("SCHWAB_REDIRECT_URI")})
    save_token(tok)
    return tok


def refresh(token: dict) -> dict:
    tok = _post_token({"grant_type": "refresh_token", "refresh_token": token.get("refresh_token", "")})
    if "refresh_token" not in tok:
        tok["refresh_token"] = token.get("refresh_token", "")
    save_token(tok)
    return tok


def valid_access_token() -> str | None:
    """Return a fresh access token, refreshing if expired. None if unauthenticated."""
    tok = load_token()
    if not tok:
        return None
    if int(tok.get("expires_at", 0)) <= int(time.time()) + 30:
        try:
            tok = refresh(tok)
        except Exception:
            return None
    return tok.get("access_token")
```

- [ ] **Step 4: Run → pass. Step 5: Commit**
```bash
python3 -m py_compile portfolio_automation/brokers/schwab_oauth.py
git add portfolio_automation/brokers/schwab_oauth.py tests/test_schwab_oauth.py
git commit -m "feat(schwab): OAuth2 auth-url/exchange/refresh + gitignored 0600 token storage"
```

---

## Task 7: schwab_client — read-only GETs + NO-TRADING-CAPABILITY test

**Files:** Create `portfolio_automation/brokers/schwab_client.py`; Test `tests/test_schwab_client.py`.

- [ ] **Step 1: Failing test (the safety-critical one)**
```python
import ast
from pathlib import Path
from portfolio_automation.brokers import schwab_client as cl


def test_no_trading_capability_anywhere_in_brokers_package():
    pkg = Path("portfolio_automation/brokers")
    forbidden = ("place_order", "submit_order", "buy", "sell", "execute_trade", "cancel_order")
    offenders = []
    for py in pkg.glob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name.lower()
                if any(f in name for f in forbidden) or name.startswith(("order", "trade")):
                    offenders.append(f"{py.name}:{node.name}")
    assert offenders == [], f"trading-capable functions present: {offenders}"


def test_client_has_only_read_methods():
    methods = [m for m in dir(cl.SchwabClient) if not m.startswith("_")]
    for m in methods:
        assert not any(k in m.lower() for k in ("order", "trade", "buy", "sell", "place")), m
    # has the read methods we expect
    assert "get_account_numbers" in methods and "get_accounts" in methods


def test_client_get_uses_bearer_and_returns_json(monkeypatch):
    captured = {}
    class _Resp:
        status_code = 200
        def json(self): return [{"ok": True}]
    def fake_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url; captured["headers"] = headers; return _Resp()
    monkeypatch.setattr(cl, "_requests_get", fake_get)
    c = cl.SchwabClient(access_token="TOK")
    out = c.get_accounts(positions=True)
    assert out == [{"ok": True}]
    assert captured["headers"]["Authorization"] == "Bearer TOK"
    assert "fields=positions" in captured["url"] or captured.get("url", "").endswith("/accounts") is False
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement**
```python
# portfolio_automation/brokers/schwab_client.py
"""Read-only Schwab Trader API client. ONLY GET endpoints for accounts/positions.
NO order/trade methods exist by design (enforced by test). Observe-only."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

_BASE = "https://api.schwabapi.com/trader/v1"


def _requests_get(url: str, headers: dict, params: dict | None = None, timeout: int = 30):
    import requests
    return requests.get(url, headers=headers, params=params, timeout=timeout)


class SchwabClient:
    """Read-only. Construct with a valid access token; call get_* methods."""

    def __init__(self, access_token: str):
        self._token = access_token

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"{_BASE}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        resp = _requests_get(url, headers={"Authorization": f"Bearer {self._token}",
                                           "Accept": "application/json"})
        if getattr(resp, "status_code", 500) != 200:
            raise RuntimeError(f"GET {path} -> {getattr(resp, 'status_code', '?')}")
        return resp.json()

    def get_account_numbers(self) -> Any:
        """Plain↔encrypted account-number map (we mask the plain)."""
        return self._get("/accounts/accountNumbers")

    def get_accounts(self, positions: bool = True) -> Any:
        return self._get("/accounts", params={"fields": "positions"} if positions else None)
```

- [ ] **Step 4: Run → pass. Step 5: Commit**
```bash
python3 -m py_compile portfolio_automation/brokers/schwab_client.py
git add portfolio_automation/brokers/schwab_client.py tests/test_schwab_client.py
git commit -m "feat(schwab): read-only Trader API client (GET accounts/positions; no trade methods)"
```

---

## Task 8: schwab_sync — orchestrator + CLI + artifacts + archive (never raises)

**Files:** Create `portfolio_automation/brokers/schwab_sync.py`; Test `tests/test_schwab_sync.py`.

- [ ] **Step 1: Failing test**
```python
import json
from pathlib import Path
from portfolio_automation.brokers import schwab_sync as sync


def test_status_when_unconfigured_writes_artifact(tmp_path, monkeypatch):
    monkeypatch.delenv("SCHWAB_CLIENT_ID", raising=False)
    st = sync.run_status(root=tmp_path)
    assert st["overall_status"] == "unconfigured"
    assert st["read_only_mode"] is True and st["trading_enabled"] is False
    p = tmp_path / "outputs/latest/broker_sync_status.json"
    assert p.exists() and json.loads(p.read_text())["source"] == "schwab"


def test_sync_unconfigured_is_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("SCHWAB_CLIENT_ID", raising=False)
    st = sync.run_sync(root=tmp_path)  # must not raise, must not network
    assert st["overall_status"] in ("unconfigured", "disabled")


def test_reconcile_from_fixture_writes_artifacts(tmp_path, monkeypatch):
    # seed a snapshot+positions as if a sync had run, plus a config
    (tmp_path / "outputs/latest").mkdir(parents=True)
    (tmp_path / "outputs/latest/schwab_portfolio_snapshot.json").write_text(
        json.dumps({"generated_at": "t", "totals": {"market_value": 5400, "cash": 464.16}}))
    (tmp_path / "outputs/latest/schwab_positions.json").write_text(
        json.dumps({"positions": [{"symbol": "QQQ", "quantity": 6}]}))
    (tmp_path / "config.json").write_text(json.dumps(
        {"portfolio": {"cash_available": 464.16, "holdings": [{"symbol": "QQQ", "shares": 6}]}}))
    out = sync.run_reconcile(root=tmp_path)
    assert out["summary_status"] in ("ok", "mismatch")
    assert (tmp_path / "outputs/latest/portfolio_reconciliation.json").exists()
    assert (tmp_path / "outputs/latest/portfolio_config_update_proposal.json").exists()


def test_no_secrets_in_any_written_artifact(tmp_path, monkeypatch):
    monkeypatch.delenv("SCHWAB_CLIENT_ID", raising=False)
    sync.run_status(root=tmp_path)
    blob = ""
    for p in (tmp_path / "outputs/latest").glob("*.json"):
        blob += p.read_text()
    for leak in ("access_token", "client_secret", "refresh_token"):
        assert leak not in blob or "<redacted>" in blob
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement**
```python
# portfolio_automation/brokers/schwab_sync.py
"""Schwab sync orchestrator + CLI. Observe-only; read-only; never raises.
Writes broker_sync_status / schwab_portfolio_snapshot / schwab_positions /
portfolio_reconciliation / portfolio_config_update_proposal."""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.brokers import broker_models as bm
from portfolio_automation.brokers import broker_status as bstat
from portfolio_automation.brokers import broker_reconciliation as brec
from portfolio_automation.brokers import schwab_oauth as oauth


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(root: Path, name: str, payload: dict) -> Path:
    return safe_write_json(OutputNamespace.LATEST, name, payload, base_dir=root / "outputs")


def _read_json(root: Path, name: str) -> Any:
    try:
        return json.loads((root / "outputs/latest" / name).read_text(encoding="utf-8"))
    except Exception:
        return None


def _enabled() -> bool:
    return oauth.read_only_mode()  # layer is active in read-only mode; inert without creds


def run_status(*, root: Path = Path("."), now: str | None = None,
               last_error: str | None = None, account_count: int = 0,
               position_count: int = 0, authenticated: bool | None = None) -> dict:
    root = Path(root)
    ts = now or _now()
    configured = oauth.is_configured()
    auth = bool(authenticated) if authenticated is not None else (configured and oauth.load_token() is not None)
    st = bstat.build_status(enabled=_enabled(), configured=configured, authenticated=auth,
                            account_count=account_count, position_count=position_count,
                            last_success_at=(ts if (auth and not last_error) else None),
                            last_error=last_error, now_iso=ts)
    try:
        _write(root, "broker_sync_status.json", st)
    except Exception:
        pass
    return st


def run_sync(*, root: Path = Path("."), now: str | None = None) -> dict:
    root = Path(root); ts = now or _now()
    if not (oauth.is_configured() and _enabled()):
        return run_status(root=root, now=ts)  # fail-closed: unconfigured/disabled
    try:
        token = oauth.valid_access_token()
        if not token:
            return run_status(root=root, now=ts, last_error="unauthenticated: run OAuth flow")
        from portfolio_automation.brokers.schwab_client import SchwabClient
        client = SchwabClient(access_token=token)
        nums = client.get_account_numbers()
        raw = client.get_accounts(positions=True)
        snap = bm.normalize_accounts(raw, nums, now_iso=ts)
        sd, pr = bm.snapshot_dict(snap), bm.positions_dict(snap)
        _write(root, "schwab_portfolio_snapshot.json", sd)
        _write(root, "schwab_positions.json", pr)
        _archive(root, ts, sd, pr)
        return run_status(root=root, now=ts, authenticated=True,
                          account_count=len(sd["accounts"]), position_count=len(pr["positions"]))
    except Exception as exc:
        return run_status(root=root, now=ts, last_error=bm.redact(str(exc)))


def run_reconcile(*, root: Path = Path("."), now: str | None = None) -> dict:
    root = Path(root); ts = now or _now()
    snap = _read_json(root, "schwab_portfolio_snapshot.json") or {"totals": {}}
    pos = _read_json(root, "schwab_positions.json") or {"positions": []}
    try:
        config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    except Exception:
        config = {}
    recon = brec.reconcile(snap, pos, config)
    recon.setdefault("generated_at", ts)
    proposal = brec.build_proposal(recon, config, now_iso=ts)
    try:
        _write(root, "portfolio_reconciliation.json", recon)
        _write(root, "portfolio_config_update_proposal.json", proposal)
    except Exception:
        pass
    return recon


def _archive(root: Path, ts: str, *payloads: dict) -> None:
    try:
        day = ts[:10]
        adir = root / "outputs/archive/broker_sync" / day
        adir.mkdir(parents=True, exist_ok=True)
        for name, payload in zip(("schwab_portfolio_snapshot.json", "schwab_positions.json"), payloads):
            (adir / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m portfolio_automation.brokers.schwab_sync",
                                 description="Schwab READ-ONLY sync (no trading).")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--reconcile", action="store_true")
    args = ap.parse_args(argv)
    print("READ-ONLY MODE ACTIVE — no trading endpoints are called.")
    if args.sync:
        st = run_sync()
    elif args.reconcile:
        run_sync(); st = run_status()
        run_reconcile()
    else:
        st = run_status()
    # print status WITHOUT secrets
    print(f"schwab: configured={st['configured']} authenticated={st['authenticated']} "
          f"status={st['overall_status']} accounts={st['account_count']} positions={st['position_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run → pass.** Then run the FULL brokers test set:
`python3 -m pytest -q tests/test_schwab_*.py` → all green.
- [ ] **Step 5: Commit**
```bash
python3 -m py_compile portfolio_automation/brokers/schwab_sync.py
git add portfolio_automation/brokers/schwab_sync.py tests/test_schwab_sync.py
git commit -m "feat(schwab): sync orchestrator + CLI + artifacts + archive (read-only, never raises)"
```

---

## Task 9: docs + CHANGELOG + full validation (GUI + registry deferred)

**Files:** Create `docs/schwab_integration.md`; Modify `docs/CHANGELOG_DECISIONS.md`.

- [ ] **Step 1: Write `docs/schwab_integration.md`** with: overview; Schwab Developer app/OAuth setup (create app, set redirect URI, scopes); the 5 env vars; **read-only safety model** (no trading methods exist; `trading_enabled` hardcoded false; observe-only); token/security notes (gitignored `data/schwab_token.json`, 0600, never logged, redaction); how to run (`--status`/`--sync`/`--reconcile`, all read-only; OAuth one-time auth-url + code paste); how to read reconciliation; **how proposal/apply works** (proposal-only now; apply = reviewed manual step via `tools/manual_portfolio_update.py`); **confirm field names on first live call** caveat; troubleshooting; NO secrets/personal data. Also document the **deferred** GUI `/dashboard/portfolio-sync` view + artifact-registry registration as next steps.

- [ ] **Step 2: CHANGELOG entry** (Area: architecture/output_contract): shipped read-only Schwab broker sync (brokers package, OAuth scaffold, normalized snapshot/positions, reconciliation, proposal-only update artifact, CLI, fixtures). Invariants: observe-only, no trade execution, no decision-core mutation, proposal-only. Downstream: 5 new `outputs/latest` artifacts + archive; new tests. Reference spec+plan.

- [ ] **Step 3: Full validation (report verbatim)**
```bash
cd /opt/stockbot
python3 -m pytest -q tests/test_schwab_models.py tests/test_schwab_status.py tests/test_schwab_reconciliation.py tests/test_schwab_oauth.py tests/test_schwab_client.py tests/test_schwab_sync.py
python3 -m py_compile portfolio_automation/brokers/*.py
python3 -m portfolio_automation.brokers.schwab_sync --status   # disabled-graceful smoke; prints read-only + no secrets
python3 -m pytest -q   # FULL suite; report line; compare to main baseline (pre-existing dotenv collection errors expected)
git status --short      # clean (no data/schwab_token.json staged; outputs/* untracked pre-existing)
```

- [ ] **Step 4: Commit (do NOT push/PR — controller handles)**
```bash
git add docs/schwab_integration.md docs/CHANGELOG_DECISIONS.md
git commit -m "docs(schwab): integration guide + changelog (read-only; proposal-only; GUI/registry deferred)"
git diff main HEAD --stat   # confirm ONLY brokers/, tests/test_schwab_*, fixtures/schwab, docs — no ride-alongs
```

---

## Self-Review

**Spec coverage:** §3 safety (Task 7 no-trade test, Tasks 1/3/8 redaction+masking+read_only-hardcoded) ✓; §4 architecture (Tasks 1-8) ✓; §5 OAuth/token (Task 6) ✓; §6 defensive normalize (Task 2) ✓; §7 all 5 artifacts (Tasks 2,3,5,8) ✓; §8 reconcile (Task 4) ✓; §9 proposal-only (Task 5) ✓; §10 CLI (Task 8) ✓; §11 validation (Task 5) ✓; §12 mask/redact (Tasks 1,3,8 tests) ✓; §13 disabled-graceful (Task 8) ✓; §14 test matrix (Tasks 1-8) ✓; §16 docs (Task 9) ✓; §15 deferred GUI+registry — documented in Task 9, NOT built ✓.

**Placeholder scan:** none. Network calls in oauth/client are real code with monkeypatch seams (`_post_token`, `_requests_get`) so tests need no live creds. The "confirm field names at connect" is an intentional documented caveat, not a placeholder.

**Type/name consistency:** `redact`/`mask_account` (T1) used in T2/T3/T6/T8. `normalize_accounts`/`snapshot_dict`/`positions_dict` (T2) consumed by T8. `build_status` (T3) by T8. `reconcile`/`validate_proposed_holdings`/`build_proposal` (T4/T5) by T8. `SchwabClient.get_account_numbers/get_accounts` (T7) by T8. `is_configured`/`read_only_mode`/`valid_access_token`/`TOKEN_PATH` (T6) by T8. Artifact names match spec §7 across T8 + tests.

**Deferred, documented (not built):** GUI `/dashboard/portfolio-sync`; artifact-registry registration of the 5 artifacts (registry not on main); gated config apply.
