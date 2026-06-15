"""Pure FMP endpoint capability classifier + probe driver (observe-only).

No network or filesystem here — ``http_get_status`` is injected so the logic is
fully unit-testable. The CLI in ``scripts/probe_fmp_crowd_endpoints.py`` supplies
a real urllib status-returning callable and persists the results.
"""
from __future__ import annotations

from typing import Any, Callable

# Capability statuses (the spec's vocabulary).
AVAILABLE = "AVAILABLE"          # 200 + non-empty, expected-ish shape
EMPTY_OK = "EMPTY_OK"            # 200 + empty list — endpoint exists, no data for probe symbol
PLAN_LOCKED = "PLAN_LOCKED"      # 402/403 or FMP 200 "Error Message" gate
AUTH_ERROR = "AUTH_ERROR"        # 401 (bad/absent key)
NOT_FOUND = "NOT_FOUND"          # 404 — path wrong or retired
RATE_LIMITED = "RATE_LIMITED"    # 429
SCHEMA_CHANGED = "SCHEMA_CHANGED"  # 200 but unusable / unexpected shape
NETWORK_ERROR = "NETWORK_ERROR"  # transport failure / status <= 0
SKIPPED_CAP = "SKIPPED_CAP"      # not probed — hard call cap reached (coverage honesty)

ALL_STATUSES = (
    AVAILABLE, EMPTY_OK, PLAN_LOCKED, AUTH_ERROR, NOT_FOUND,
    RATE_LIMITED, SCHEMA_CHANGED, NETWORK_ERROR, SKIPPED_CAP,
)


def classify(http_status: int | None, body: Any, error: str | None,
             expected_fields: list[str] | None = None) -> str:
    """Map one probe outcome to a capability status. Pure."""
    if error or http_status is None or http_status <= 0:
        return NETWORK_ERROR
    if http_status == 401:
        return AUTH_ERROR
    if http_status in (402, 403):
        return PLAN_LOCKED
    if http_status == 404:
        return NOT_FOUND
    if http_status == 429:
        return RATE_LIMITED
    if http_status == 200:
        # FMP signals plan/legacy gates as 200 + {"Error Message": ...}.
        if isinstance(body, dict) and "Error Message" in body:
            return PLAN_LOCKED
        if isinstance(body, list):
            if not body:
                return EMPTY_OK
            first = body[0]
            if expected_fields and isinstance(first, dict) and not any(f in first for f in expected_fields):
                return SCHEMA_CHANGED
            return AVAILABLE
        if isinstance(body, dict) and body:
            if expected_fields and not any(f in body for f in expected_fields):
                return SCHEMA_CHANGED
            return AVAILABLE
        # 200 but None / str / empty dict — usable shape not present.
        return SCHEMA_CHANGED
    if 400 <= http_status < 500:
        return PLAN_LOCKED   # conservative: other 4xx read as forbidden/locked
    return NETWORK_ERROR


def _sample_fields(body: Any) -> list[str]:
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return sorted(body[0].keys())[:12]
    if isinstance(body, dict):
        return sorted(body.keys())[:12]
    return []


def _resolve_params(params_template: dict | None, symbol: str) -> dict:
    out: dict[str, Any] = {}
    for k, v in (params_template or {}).items():
        out[k] = v.replace("{symbol}", symbol) if isinstance(v, str) else v
    return out


def probe_all(entries: list[dict],
              http_get_status: Callable[[str, dict], tuple[int | None, Any, str]],
              *, max_calls: int = 80, symbol: str = "AAPL",
              now_iso: str = "") -> list[dict]:
    """Probe each entry once. Never raises; one endpoint failing never aborts
    the run. ``http_get_status(path, params) -> (status_code, body, message)``."""
    results: list[dict] = []
    calls = 0
    for e in entries:
        eid = e.get("endpoint_id")
        base = {"endpoint_id": eid, "category": e.get("category"),
                "last_checked_at": now_iso}
        if calls >= max_calls:
            results.append({**base, "status": SKIPPED_CAP, "http_status": None,
                            "response_bytes": 0, "sample_fields": [],
                            "error_summary": "probe call cap reached"})
            continue
        params = _resolve_params(e.get("params_template"), symbol)
        calls += 1
        try:
            status_code, body, message = http_get_status(e["path"], params)
            status = classify(status_code, body, None, e.get("expected_fields"))
            results.append({**base, "status": status, "http_status": status_code,
                            "response_bytes": len(str(body)) if body is not None else 0,
                            "sample_fields": _sample_fields(body),
                            "error_summary": (message or "")[:200]})
        except Exception as exc:  # one endpoint must never sink the probe
            results.append({**base, "status": NETWORK_ERROR, "http_status": None,
                            "response_bytes": 0, "sample_fields": [],
                            "error_summary": f"{type(exc).__name__}: {exc}"[:200]})
    return results


def summarize_by_status(results: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
    return counts
