"""
theme_discovery — keyword-driven market theme detection from news feeds.

Pipeline:
    collector  → collect_articles()       # fetch + deduplicate RSS items
    extractor  → extract(articles)        # score articles against themes + find tickers
    scorer     → score(groups, top_n)     # rank into ThemeOpportunity objects

Run:
    python -m theme_discovery
    python -m theme_discovery --top-n 15 --dry-run
"""
