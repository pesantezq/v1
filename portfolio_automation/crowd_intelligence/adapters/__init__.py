"""Crowd-intelligence category adapters (observe-only).

Each adapter exposes ``run(symbol, *, client, usable, shared) -> CategoryResult``
and a ``CATEGORY`` / ``ENDPOINT_IDS`` declaration. All FMP access goes through the
governed client's ``get_json`` — never raw HTTP. Adapters skip endpoints whose
capability status is not usable and degrade to a neutral/empty result.
"""
from __future__ import annotations

from typing import Any, Callable

# A small shared helper every adapter uses to call one registry endpoint through
# the governed client, tolerating any failure (returns []).
def fetch_endpoint(client: Any, reg_entry: dict | None, *, symbol: str | None = None,
                   today: str | None = None) -> Any:
    if not isinstance(reg_entry, dict):
        return []  # unknown/missing registry entry — fail safe, never crash
    from datetime import datetime, timezone
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    params = {}
    for k, v in (reg_entry.get("params_template") or {}).items():
        if isinstance(v, str) and "{symbol}" in v:
            if symbol is None:
                return []  # per-symbol endpoint called without a symbol
            params[k] = v.replace("{symbol}", symbol)
        elif isinstance(v, str) and "{today}" in v:
            params[k] = v.replace("{today}", today)
        else:
            params[k] = v
    try:
        return client.get_json(reg_entry["path"], params,
                               ttl_seconds=int(reg_entry.get("ttl_seconds") or 3600))
    except Exception:
        return []


def usable_endpoint_ids(capabilities: dict, candidate_ids: list[str]) -> tuple[list[str], list[str]]:
    """Split candidate endpoint_ids into (usable, disabled) using the Phase-1
    capability map. Usable = AVAILABLE or EMPTY_OK. Missing capability (probe not
    run) → optimistically usable, but recorded so the builder can warn."""
    status_by_id = {r.get("endpoint_id"): r.get("status") for r in (capabilities.get("records") or [])}
    usable, disabled = [], []
    for eid in candidate_ids:
        st = status_by_id.get(eid)
        if st in ("AVAILABLE", "EMPTY_OK", None):  # None = not probed -> optimistic
            usable.append(eid)
        else:
            disabled.append(eid)
    return usable, disabled
