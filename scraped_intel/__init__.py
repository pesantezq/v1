"""
scraped_intel — scraped evidence intelligence pipeline.

Provides a layered system for ingesting, normalising, and feature-engineering
non-API intelligence (SEC filings, RSS news, company IR pages) without
contaminating trusted hard-data fields.

Public API
----------
    from scraped_intel.pipeline import run_scraped_intel
    from scraped_intel.export import export_training_rows
    from scraped_intel.models import ScrapedRecord, SoftSignals, IntelBundle

Quick start
-----------
    bundles = run_scraped_intel(
        symbols=["NVDA", "AMD"],
        config=full_config["scraped_intel"],
        known_themes=["AI Infrastructure", "Semiconductors"],
    )
    # Attach to scan result row — never modify existing fields
    for row in scan_result["results"]:
        sym = row["ticker"]
        if sym in bundles:
            row["scraped_intel"] = bundles[sym].to_dict()
"""

from scraped_intel.models import IntelBundle, ScrapedRecord, SoftSignals
from scraped_intel.pipeline import run_scraped_intel

__all__ = [
    "ScrapedRecord",
    "SoftSignals",
    "IntelBundle",
    "run_scraped_intel",
]
