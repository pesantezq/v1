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
