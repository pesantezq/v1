"""
Phase 4: Normalized social record schema with privacy-safe author hash.

Every text-producing connector (Bluesky, Mastodon, Lemmy) emits records
conforming to this schema. The schema is versioned (schema_version="2");
readers must be tolerant — use .get() with defaults for any field added in a
future version.

Attention-producing connectors (ApeWisdom) remain schema_version="1" with
source_type="attention" and do NOT carry sentiment fields.
"""
from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "2"
ATTENTION_SCHEMA_VERSION = "1"


def make_text_record(
    *,
    source: str,
    ticker: str,
    post_id_hash: str,
    author_hash: str,
    created_at: str,
    text: str,
    text_len: int,
    like_count: int = 0,
    reply_count: int = 0,
    repost_count: int = 0,
    engagement_score: float = 0.0,
    language: str = "en",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build a validated text record.

    Sentinel values for missing optional fields: empty string / 0 / 0.0.
    The ``sentiment_*`` fields are filled by the FinBERT scorer (Phase 6)
    AFTER this record is created; they are absent until scoring runs.
    """
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "source_type": "text",
        "ticker": ticker.upper(),
        "post_id_hash": str(post_id_hash)[:16],
        "author_hash": str(author_hash)[:12],
        "created_at": created_at,
        "text": str(text)[:500],
        "text_len": int(text_len),
        "like_count": max(0, int(like_count)),
        "reply_count": max(0, int(reply_count)),
        "repost_count": max(0, int(repost_count)),
        "engagement_score": round(max(0.0, min(1.0, float(engagement_score))), 4),
        "language": str(language or "en")[:8],
        # Sentiment fields — filled by scorer; absent until then.
        # "sentiment_score": float,        # -1.0 (neg) to +1.0 (pos)
        # "positive_probability": float,
        # "neutral_probability": float,
        # "negative_probability": float,
        # "label": str,                    # "positive" | "neutral" | "negative"
        # "scorer": str,                   # "finbert" | "scorer_unavailable"
        # "scorer_version": str,
    }
    if extra:
        # Allow source-specific extras (e.g. instance for Mastodon/Lemmy)
        for k, v in extra.items():
            if k not in record:
                record[k] = v
    return record


def is_valid_text_record(record: Any) -> bool:
    """
    Tolerant validator for text records.

    Returns True if the record has the minimum required fields to be processed.
    Unknown extra fields are accepted (forward-compatible readers).
    """
    if not isinstance(record, dict):
        return False
    required = {"source", "source_type", "ticker", "post_id_hash", "created_at", "text"}
    return required.issubset(record.keys()) and bool(record.get("ticker")) and bool(record.get("text"))


def is_sentiment_scored(record: dict[str, Any]) -> bool:
    """True if the record has been through the FinBERT scorer."""
    return "sentiment_score" in record and "scorer" in record


def attach_sentiment(
    record: dict[str, Any],
    *,
    sentiment_score: float,
    positive_probability: float,
    neutral_probability: float,
    negative_probability: float,
    label: str,
    scorer: str,
    scorer_version: str,
) -> dict[str, Any]:
    """Attach FinBERT sentiment fields to a record in-place and return it."""
    record["sentiment_score"] = round(float(sentiment_score), 4)
    record["positive_probability"] = round(float(positive_probability), 4)
    record["neutral_probability"] = round(float(neutral_probability), 4)
    record["negative_probability"] = round(float(negative_probability), 4)
    record["label"] = str(label)
    record["scorer"] = str(scorer)
    record["scorer_version"] = str(scorer_version)
    return record
