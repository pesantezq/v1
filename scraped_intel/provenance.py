"""
Scraped Intelligence — provenance and confidence scoring.

Every soft signal must be traceable to its evidence.  This module assigns
quality weights to sources and aggregates them into a single scraped_confidence
score for a set of ScrapedRecords.

Design
------
scraped_confidence is a number in [0, 1] measuring how trustworthy the
scraped evidence is.  It is NOT a measure of investment attractiveness —
that is signal_score (a hard-data field untouched here).

Formula:
    weighted_parse_quality = mean(parse_quality × source_weight) per record
    count_bonus = min(1.0, record_count / COUNT_SATURATION)
    scraped_confidence = 0.70 × weighted_parse_quality + 0.30 × count_bonus

The two components:
  - Weighted parse quality captures source authority and extraction fidelity.
  - Count bonus rewards having multiple independent records (diversity).

Contamination guard
-------------------
scraped_confidence is only written to SoftSignals.scraped_confidence and
IntelBundle.to_dict()["scraped_confidence"].  It is never merged into the
scanner's confidence_score field.
"""

from __future__ import annotations

from scraped_intel.models import ScrapedRecord

# Source domain → reliability weight in [0, 1]
# Add entries here as new adapters or feeds are added.
_DOMAIN_WEIGHTS: dict[str, float] = {
    "sec.gov":            1.00,   # regulatory filings
    "edgar.sec.gov":      1.00,
    "reuters.com":        0.85,
    "bloomberg.com":      0.85,
    "ft.com":             0.85,
    "wsj.com":            0.80,
    "barrons.com":        0.80,
    "cnbc.com":           0.70,
    "marketwatch.com":    0.70,
    "benzinga.com":       0.65,
    "finance.yahoo.com":  0.60,
    "seekingalpha.com":   0.55,
    "motleyfool.com":     0.50,
    "rss":                0.45,   # generic / unknown RSS feed
    "unknown":            0.35,
}
_DEFAULT_DOMAIN_WEIGHT: float = 0.45

# How many records before count_bonus saturates at 1.0
_COUNT_SATURATION: int = 8


def domain_weight(domain: str) -> float:
    """Return the quality weight for a source domain."""
    domain = (domain or "").lower().lstrip("www.")
    # Exact match first, then suffix match (e.g. "finance.yahoo.com" → "yahoo.com")
    if domain in _DOMAIN_WEIGHTS:
        return _DOMAIN_WEIGHTS[domain]
    for key, w in _DOMAIN_WEIGHTS.items():
        if domain.endswith(key):
            return w
    return _DEFAULT_DOMAIN_WEIGHT


def compute_scraped_confidence(records: list[ScrapedRecord]) -> float:
    """
    Compute overall scraped confidence for a set of ScrapedRecords.

    Returns 0.0 if no records are provided.
    """
    if not records:
        return 0.0

    # Weighted parse quality per record
    weighted_quals = [
        r.parse_quality * domain_weight(r.domain)
        for r in records
    ]
    mean_quality = sum(weighted_quals) / len(weighted_quals)

    # Count bonus
    count_bonus = min(1.0, len(records) / _COUNT_SATURATION)

    confidence = 0.70 * mean_quality + 0.30 * count_bonus
    return round(min(1.0, confidence), 4)


def build_provenance_summary(records: list[ScrapedRecord]) -> dict:
    """
    Return a human-readable provenance summary dict for debugging and logging.

    Not used in scoring — purely informational.
    """
    if not records:
        return {"record_count": 0, "sources": [], "scraped_confidence": 0.0}

    source_breakdown: dict[str, dict] = {}
    for r in records:
        d = r.domain or "unknown"
        if d not in source_breakdown:
            source_breakdown[d] = {
                "count": 0,
                "domain_weight": domain_weight(d),
                "avg_parse_quality": 0.0,
                "_qual_sum": 0.0,
            }
        source_breakdown[d]["count"] += 1
        source_breakdown[d]["_qual_sum"] += r.parse_quality

    for entry in source_breakdown.values():
        n = entry["count"]
        entry["avg_parse_quality"] = round(entry["_qual_sum"] / n, 3)
        del entry["_qual_sum"]

    return {
        "record_count":       len(records),
        "scraped_confidence": compute_scraped_confidence(records),
        "sources": [
            {
                "domain":        d,
                "count":         v["count"],
                "weight":        v["domain_weight"],
                "avg_parse_q":   v["avg_parse_quality"],
            }
            for d, v in sorted(
                source_breakdown.items(),
                key=lambda x: -x[1]["count"],
            )
        ],
    }
