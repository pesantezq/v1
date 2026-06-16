"""
Theme Engine package.

Harvests public RSS headlines, detects durable investing themes via the
configured LLM provider (OpenAI primary, Anthropic fallback), maps themes to
S&P 500 tickers, and produces a small confidence-weighted score boost for the
candidate scanner.

Entry point: py -m theme_engine --mode daily|weekly|monthly
"""
