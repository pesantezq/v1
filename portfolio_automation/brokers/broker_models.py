# portfolio_automation/brokers/broker_models.py
"""Pure normalization + safety helpers for broker data. No network, no secrets,
no trade logic. Observe-only."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_SECRET_KEYS = ("access_token", "refresh_token", "client_secret", "code", "id_token", "Authorization")
_KEY_ALT = "|".join(re.escape(k) for k in _SECRET_KEYS)
# Match an optionally-quoted secret key, a : or = separator, then the value, which
# may be: a double/single-quoted string, an auth scheme + token (Bearer/Basic <tok>),
# or a bare token. Over-redaction is acceptable; under-redaction (a leak) is not.
_SECRET_RE = re.compile(
    r"""(?xi)
    ( ["']? \b (?:""" + _KEY_ALT + r""") \b ["']? \s* [:=] \s* )
    ( "[^"]*" | '[^']*' | (?:bearer|basic)\s+[^\s,;}"']+ | [^\s,;}"'&]+ )
    """
)


def redact(text: Any) -> str:
    """Scrub token/secret/code VALUES from any text (incl. JSON / dict-repr / Bearer
    headers) before logging or persisting. Over-redaction is fine; leaks are not."""
    if text is None:
        return ""
    return _SECRET_RE.sub(lambda m: m.group(1) + "<redacted>", str(text))


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
