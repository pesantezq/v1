# ============================================================
# FMP ENDPOINT REGISTRY (STARTER PLAN + STABLE SAFE)
# ============================================================

FMP_ENDPOINTS = {

    # =========================
    # CORE MARKET DATA (P0)
    # =========================
    "quote": {
        "endpoint": "/stable/quote",
        "method": "GET",
        "params": ["symbol"],
        "per_symbol": True,
        "starter_safe": True,
        "priority": "P0",
        "usage": "price, change, volume",
    },

    "profile": {
        "endpoint": "/stable/profile",
        "method": "GET",
        "params": ["symbol"],
        "per_symbol": True,
        "starter_safe": True,
        "priority": "P0",
        "usage": "sector, industry, market cap",
    },

    "historical_prices": {
        "endpoint": "/stable/historical-price-eod/full",
        "method": "GET",
        "params": ["symbol"],
        "per_symbol": True,
        "starter_safe": True,
        "priority": "P0",
        "usage": "SMA, returns, volume averages",
    },

    "historical_prices_dividend_adjusted": {
        "endpoint": "/stable/historical-price-eod/dividend-adjusted",
        "method": "GET",
        "params": ["symbol", "from"],
        "per_symbol": True,
        "starter_safe": "unverified",
        "priority": "P2",
        "usage": "VS-002 historical risk evidence ONLY (bounded Northstar 0C). Adjustment semantics carried by the endpoint identity. Entitlement/response shape UNVERIFIED — one bounded metered verification is deferred to the production evidence-build mission. Fail closed; never fall back to /full, the mutable archive, or another vendor.",
    },

    "stock_news": {
        "endpoint": "/stable/news/stock",
        "method": "GET",
        "params": ["tickers"],
        "per_symbol": False,
        "starter_safe": True,
        "priority": "P0",
        "usage": "news, sentiment, catalysts",
    },

    # =========================
    # FUNDAMENTALS (P0)
    # =========================
    "income_statement": {
        "endpoint": "/stable/income-statement",
        "method": "GET",
        "params": ["symbol"],
        "per_symbol": True,
        "starter_safe": True,
        "priority": "P0",
        "usage": "revenue, net income, EPS",
    },

    "ratios": {
        "endpoint": "/stable/ratios",
        "method": "GET",
        "params": ["symbol"],
        "per_symbol": True,
        "starter_safe": True,
        "priority": "P0",
        "usage": "margins, ROE, debt ratios",
    },

    "key_metrics": {
        "endpoint": "/stable/key-metrics",
        "method": "GET",
        "params": ["symbol"],
        "per_symbol": True,
        "starter_safe": True,
        "priority": "P0",
        "usage": "valuation, FCF, PE",
    },

    # =========================
    # EXTENDED FUNDAMENTALS (P1)
    # =========================
    "balance_sheet": {
        "endpoint": "/stable/balance-sheet-statement",
        "method": "GET",
        "params": ["symbol"],
        "per_symbol": True,
        "starter_safe": True,
        "priority": "P1",
        "usage": "debt, cash, equity",
    },

    "cashflow_statement": {
        "endpoint": "/stable/cashflow-statement",
        "method": "GET",
        "params": ["symbol"],
        "per_symbol": True,
        "starter_safe": True,
        "priority": "P1",
        "usage": "free cash flow, capex",
    },

    "financial_growth": {
        "endpoint": "/stable/financial-growth",
        "method": "GET",
        "params": ["symbol"],
        "per_symbol": True,
        "starter_safe": True,
        "priority": "P1",
        "usage": "revenue/EPS growth (revenueGrowth field)",
    },

    # =========================
    # QUALITY / VALIDATION (P2)
    # =========================
    "ratings_snapshot": {
        "endpoint": "/stable/ratings-snapshot",
        "method": "GET",
        "params": ["symbol"],
        "per_symbol": True,
        "starter_safe": True,
        "priority": "P2",
        "usage": "quick quality + valuation sanity check",
    },

    "historical_ratings": {
        "endpoint": "/stable/historical-ratings",
        "method": "GET",
        "params": ["symbol"],
        "per_symbol": True,
        "starter_safe": True,
        "priority": "P2",
        "usage": "rating trend",
    },

    # =========================
    # REFERENCE DATA (P2)
    # =========================
    "available_sectors": {
        "endpoint": "/stable/available-sectors",
        "method": "GET",
        "params": [],
        "per_symbol": False,
        "starter_safe": True,
        "priority": "P2",
        "usage": "sector mapping",
    },

    "available_industries": {
        "endpoint": "/stable/available-industries",
        "method": "GET",
        "params": [],
        "per_symbol": False,
        "starter_safe": True,
        "priority": "P2",
        "usage": "industry mapping",
    },

    # =========================
    # OPTIONAL / DO NOT USE IN CORE PIPELINE
    # =========================
    "bulk_key_metrics_ttm": {
        "endpoint": "/stable/key-metrics-ttm-bulk",
        "starter_safe": False,  # depends on plan
        "priority": "P3",
        "usage": "optional acceleration only",
    },

    "bulk_ratios_ttm": {
        "endpoint": "/stable/ratios-ttm-bulk",
        "starter_safe": False,
        "priority": "P3",
        "usage": "optional acceleration only",
    },
}