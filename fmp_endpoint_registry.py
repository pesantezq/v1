"""
FMP Endpoint Registry — Starter-plan-safe stable API contract.

This is the machine-readable source of truth.
The companion document docs/fmp_endpoint_inventory.md is the human-readable
equivalent; keep them in sync when adding or retiring endpoints.

Classifications:
  core_stable_ok  — implemented via stable/, required or used by daily scanner
  legacy_optional — v3/v4 path kept for backward compat; NOT used by daily scanner
  premium_optional — v4 bulk endpoint; Starter plan does not include it
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

REGISTRY: dict[str, dict] = {
    # ── P0: Core market data (daily scanner) ─────────────────────────────────
    "quote": {
        "endpoint":      "/stable/quote",
        "per_symbol":    True,
        "starter_safe":  True,
        "priority":      "P0",
        "required_daily": True,
        "classification": "core_stable_ok",
        "usage": "price, 1d change, volume, SMA50",
    },
    "profile": {
        "endpoint":      "/stable/profile",
        "per_symbol":    True,
        "starter_safe":  True,
        "priority":      "P0",
        "required_daily": True,
        "classification": "core_stable_ok",
        "usage": "sector, industry, market cap, beta",
    },
    "historical_prices": {
        "endpoint":      "/stable/historical-price-eod/full",
        "per_symbol":    True,
        "starter_safe":  True,
        "priority":      "P0",
        "required_daily": True,
        "classification": "core_stable_ok",
        "usage": "SMA20, 5d change, volume avg",
    },
    "stock_news": {
        "endpoint":      "/stable/news/stock",
        "per_symbol":    False,
        "starter_safe":  True,
        "priority":      "P0",
        "required_daily": True,
        "classification": "core_stable_ok",
        "usage": "news headlines, sentiment proxy",
    },
    # ── P0: Fundamentals ──────────────────────────────────────────────────────
    "ratios": {
        "endpoint":      "/stable/ratios",
        "per_symbol":    True,
        "starter_safe":  True,
        "priority":      "P0",
        "required_daily": True,
        "classification": "core_stable_ok",
        "usage": "profit margin, ROE, debt ratios, dividend yield",
    },
    "key_metrics": {
        "endpoint":      "/stable/key-metrics",
        "per_symbol":    True,
        "starter_safe":  True,
        "priority":      "P0",
        "required_daily": False,
        "classification": "core_stable_ok",
        "usage": "PE, FCF yield, ROE, revenue growth",
    },
    "income_statement": {
        "endpoint":      "/stable/income-statement",
        "per_symbol":    True,
        "starter_safe":  True,
        "priority":      "P0",
        "required_daily": False,
        "classification": "core_stable_ok",
        "usage": "revenue, gross profit, net income, EPS",
    },
    # ── P1: Extended fundamentals ─────────────────────────────────────────────
    "balance_sheet": {
        "endpoint":      "/stable/balance-sheet-statement",
        "per_symbol":    True,
        "starter_safe":  True,
        "priority":      "P1",
        "required_daily": False,
        "classification": "core_stable_ok",
        "usage": "total debt, cash, equity, working capital",
    },
    "cashflow_statement": {
        "endpoint":      "/stable/cashflow-statement",
        "per_symbol":    True,
        "starter_safe":  True,
        "priority":      "P1",
        "required_daily": False,
        "classification": "core_stable_ok",
        "usage": "free cash flow, capex, operating CF",
    },
    "financial_growth": {
        "endpoint":      "/stable/financial-growth",
        "per_symbol":    True,
        "starter_safe":  True,
        "priority":      "P1",
        "required_daily": False,
        "classification": "core_stable_ok",
        "usage": "revenue/EPS/FCF growth rates (revenueGrowth field; verified HTTP 200 on Starter)",
    },
    # ── P2: Quality / reference ───────────────────────────────────────────────
    "ratings_snapshot": {
        "endpoint":      "/stable/ratings-snapshot",
        "per_symbol":    True,
        "starter_safe":  True,
        "priority":      "P2",
        "required_daily": False,
        "classification": "core_stable_ok",
        "usage": "analyst rating, DCF vs market price",
    },
    "historical_ratings": {
        "endpoint":      "/stable/historical-ratings",
        "per_symbol":    True,
        "starter_safe":  True,
        "priority":      "P2",
        "required_daily": False,
        "classification": "core_stable_ok",
        "usage": "rating trend over time",
    },
    "available_sectors": {
        "endpoint":      "/stable/available-sectors",
        "per_symbol":    False,
        "starter_safe":  True,
        "priority":      "P2",
        "required_daily": False,
        "classification": "core_stable_ok",
        "usage": "canonical sector list for mapping",
    },
    "available_industries": {
        "endpoint":      "/stable/available-industries",
        "per_symbol":    False,
        "starter_safe":  True,
        "priority":      "P2",
        "required_daily": False,
        "classification": "core_stable_ok",
        "usage": "canonical industry list for mapping",
    },
    # ── P3: Optional bulk (must NOT be required by daily scanner) ─────────────
    "bulk_key_metrics_ttm": {
        "endpoint":      "/stable/key-metrics-ttm-bulk",
        "per_symbol":    False,
        "starter_safe":  False,
        "priority":      "P3",
        "required_daily": False,
        "classification": "premium_optional",
        "usage": "optional bulk acceleration — not for core pipeline",
    },
    "bulk_ratios_ttm": {
        "endpoint":      "/stable/ratios-ttm-bulk",
        "per_symbol":    False,
        "starter_safe":  False,
        "priority":      "P3",
        "required_daily": False,
        "classification": "premium_optional",
        "usage": "optional bulk acceleration — not for core pipeline",
    },
    # ── P3: Social sentiment (paid Starter+ entitlement; PROBE-ONLY) ───────────
    # Crowd Radar no-extra-cost policy: NOT starter-safe, never required, only
    # ever entitlement-probed against the existing key. v4 legacy fallback noted.
    "social_sentiment": {
        "endpoint":      "/stable/historical/social-sentiment",
        "legacy_endpoint": "/api/v4/historical/social-sentiment",
        "per_symbol":    True,
        "starter_safe":  False,
        "priority":      "P3",
        "required_daily": False,
        "classification": "premium_optional",
        "usage": "Crowd Radar social-sentiment entitlement probe only — not core pipeline",
    },
}

# ---------------------------------------------------------------------------
# Legacy endpoints (NOT in stable registry — tracked for compliance)
# ---------------------------------------------------------------------------

LEGACY_ENDPOINTS: dict[str, str] = {
    "v3/sp500_constituent":     "legacy_optional",  # no confirmed stable equivalent
    "v3/profile/{batch}":       "legacy_optional",  # universe pipeline; not daily scanner
    "v3/key-metrics/{sym}":     "legacy_optional",  # get_fundamentals_v3 fallback only
    "v3/financial-growth/{sym}": "legacy_optional", # get_fundamentals_v3 fallback only
    "v4/profile/all":           "premium_optional",
    "v4/key-metrics-bulk":      "premium_optional",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_registry() -> dict[str, dict]:
    return REGISTRY


def get_core_daily_required() -> list[str]:
    """Registry keys that the daily scanner must successfully fetch."""
    return [k for k, v in REGISTRY.items() if v.get("required_daily")]


def get_stable_path(registry_key: str) -> str | None:
    """Return the stable endpoint path (without /stable/ prefix) for a key."""
    spec = REGISTRY.get(registry_key)
    if not spec:
        return None
    ep = spec["endpoint"]               # e.g. "/stable/quote"
    return ep.removeprefix("/stable/")  # e.g. "quote"
