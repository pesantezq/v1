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
