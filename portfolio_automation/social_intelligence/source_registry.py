"""
Source governance registry for public-discussion sources.

Tracks, per source, the compliance metadata that gates collection: collection
method, allowed fields, rate limit, whether raw text may be stored, whether AI
processing is permitted, the terms-review date, and a compliance status.

The registry is the single place that encodes "what are we allowed to do with
this source" — connectors must consult it before persisting anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from portfolio_automation.social_intelligence.base import base_envelope


@dataclass(frozen=True)
class SourceGovernance:
    """Compliance descriptor for a single public-discussion source."""

    source_name: str
    source_type: str               # e.g. "forum", "social"
    collection_method: str         # e.g. "official_api_oauth", "public_json"
    allowed_fields: tuple[str, ...]
    rate_limit: str                # human-readable, e.g. "60 req/min (OAuth)"
    raw_text_storage_allowed: bool
    ai_processing_allowed: bool
    terms_review_date: str         # ISO date of last terms review
    compliance_status: str         # "approved" | "review_needed" | "blocked"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "source_type": self.source_type,
            "collection_method": self.collection_method,
            "allowed_fields": list(self.allowed_fields),
            "rate_limit": self.rate_limit,
            "raw_text_storage_allowed": self.raw_text_storage_allowed,
            "ai_processing_allowed": self.ai_processing_allowed,
            "terms_review_date": self.terms_review_date,
            "compliance_status": self.compliance_status,
            "notes": self.notes,
        }


# The minimal-field set the layer collects (matches RawPost).
_REDDIT_ALLOWED_FIELDS = (
    "post_id", "source", "community", "created_utc", "title_or_title_hash",
    "flair", "score", "comment_count", "upvote_ratio", "detected_tickers",
    "url", "author_hash", "collection_timestamp",
)

# Default registry. Conservative defaults: raw text NOT stored, AI processing of
# derived features only. Reddit via its official OAuth API.
DEFAULT_SOURCES: tuple[SourceGovernance, ...] = (
    SourceGovernance(
        source_name="reddit",
        source_type="forum",
        collection_method="official_api_oauth",
        allowed_fields=_REDDIT_ALLOWED_FIELDS,
        rate_limit="60 req/min (OAuth app)",
        raw_text_storage_allowed=False,   # store derived features only
        ai_processing_allowed=True,        # derived features may be AI-processed
        terms_review_date="2026-06-12",
        compliance_status="approved",
        notes=(
            "Official Reddit OAuth API. Do not train models on raw user content; "
            "raw bodies processed transiently for ticker extraction / DD scoring "
            "only. Feature-gated and disable-able."
        ),
    ),
)


def get_source(name: str, sources: tuple[SourceGovernance, ...] = DEFAULT_SOURCES) -> SourceGovernance | None:
    for s in sources:
        if s.source_name == name:
            return s
    return None


def is_field_allowed(source: SourceGovernance, field_name: str) -> bool:
    return field_name in source.allowed_fields


def build_source_compliance(
    *,
    run_id: str,
    run_mode: str,
    enabled_sources: list[str] | None = None,
    sources: tuple[SourceGovernance, ...] = DEFAULT_SOURCES,
    overall_status: str = "ok",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """
    Build the ``social_source_compliance.json`` payload.

    ``enabled_sources`` is the subset actually active this run (from config); a
    source present in the registry but not enabled is reported with
    ``active=false`` so the operator can see the full governed set.
    """
    enabled = set(enabled_sources or [s.source_name for s in sources])
    records = []
    review_needed = 0
    for s in sources:
        rec = s.to_dict()
        rec["active"] = s.source_name in enabled
        records.append(rec)
        if s.compliance_status != "approved":
            review_needed += 1

    env = base_envelope(
        run_id=run_id,
        run_mode=run_mode,
        source_status=overall_status,
        data_quality_status="ok" if review_needed == 0 else "degraded",
        warnings=warnings,
    )
    env.update({
        "total_sources": len(records),
        "active_sources": sum(1 for r in records if r["active"]),
        "review_needed_count": review_needed,
        "records": records,
    })
    return env
