"""
Phase 7: Anti-manipulation quality gates for social sentiment aggregation.

Gates are applied per (ticker, source, time_window) before any sentiment is
computed. A batch that fails a gate contributes NO sentiment to aggregation —
it is quarantined with explicit failure reasons.

Gates (all configurable, defaults match the spec):
  - MIN_POSTS         : min 10 posts (default)
  - MIN_UNIQUE_AUTHORS: min 6 unique author hashes (default)
  - MAX_AUTHOR_CONC   : max 0.20 fraction from any single author (default)
  - MAX_DUPLICATE_RATIO: max 0.35 duplicate post fraction (default)
  - MAX_SPAM_RATIO    : max 0.40 estimated spam fraction (default)
  - MAX_AGE_HOURS     : max 24h post age window (default)

The ``QualityGateResult`` carries:
  - passed: bool
  - failure_reasons: list[str]  (empty on pass)
  - stats: dict[str, Any]       (computed metrics for audit trail)
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class QualityGateResult:
    passed: bool
    failure_reasons: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failure_reasons": list(self.failure_reasons),
            "stats": dict(self.stats),
        }


# Gate thresholds — override via config dict
_DEFAULTS = {
    "min_posts": 10,
    "min_unique_authors": 6,
    "max_author_concentration": 0.20,
    "max_duplicate_ratio": 0.35,
    "max_spam_ratio": 0.40,
    "max_age_hours": 24.0,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_created_at(s: str) -> datetime | None:
    if not s:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
    ):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc) if s.endswith("Z") \
                else datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _text_fingerprint(text: str) -> str:
    """Short fingerprint for near-duplicate detection."""
    normalized = " ".join(text.lower().split())[:200]
    return hashlib.md5(normalized.encode()).hexdigest()[:8]  # noqa: S324 — for dedup only, not security


def _is_likely_spam(text: str) -> bool:
    """Heuristic: very short text, all-caps, or repeated punctuation."""
    if len(text) < 20:
        return True
    if sum(1 for c in text if c.isupper()) / max(len(text), 1) > 0.7:
        return True
    if text.count("!") + text.count("?") > 5:
        return True
    return False


class QualityGateChecker:
    """
    Applies all quality gates to a batch of records for one (ticker, source).

    Records that survive all gates are returned in ``QualityGateResult.stats``
    alongside computed metrics for audit.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._min_posts = int(cfg.get("min_posts", _DEFAULTS["min_posts"]))
        self._min_unique_authors = int(cfg.get("min_unique_authors", _DEFAULTS["min_unique_authors"]))
        self._max_author_conc = float(cfg.get("max_author_concentration", _DEFAULTS["max_author_concentration"]))
        self._max_dup_ratio = float(cfg.get("max_duplicate_ratio", _DEFAULTS["max_duplicate_ratio"]))
        self._max_spam_ratio = float(cfg.get("max_spam_ratio", _DEFAULTS["max_spam_ratio"]))
        self._max_age_hours = float(cfg.get("max_age_hours", _DEFAULTS["max_age_hours"]))

    def check(
        self,
        records: list[dict[str, Any]],
        *,
        source: str = "",
        ticker: str = "",
    ) -> QualityGateResult:
        """
        Run all quality gates on a list of records.

        Returns a QualityGateResult with passed=True only if every gate clears.
        """
        now = _utc_now()
        failures: list[str] = []
        n = len(records)

        if n == 0:
            return QualityGateResult(
                passed=False,
                failure_reasons=["no_records"],
                stats={"n": 0, "source": source, "ticker": ticker},
            )

        # Gate 1: minimum post count
        if n < self._min_posts:
            failures.append(f"too_few_posts:{n}<{self._min_posts}")

        # Gate 2: minimum unique authors
        author_hashes = [r.get("author_hash", "") for r in records if r.get("author_hash")]
        unique_authors = len(set(author_hashes))
        if unique_authors < self._min_unique_authors:
            failures.append(f"too_few_authors:{unique_authors}<{self._min_unique_authors}")

        # Gate 3: single-author concentration
        author_concentration = 0.0
        top_author = ""
        if author_hashes:
            from collections import Counter
            author_counts = Counter(author_hashes)
            top_author, top_count = author_counts.most_common(1)[0]
            author_concentration = top_count / n
            if author_concentration > self._max_author_conc:
                failures.append(
                    f"high_author_concentration:{author_concentration:.2f}>{self._max_author_conc}"
                )

        # Gate 4: duplicate ratio
        texts = [str(r.get("text") or "") for r in records]
        fingerprints = [_text_fingerprint(t) for t in texts]
        unique_fps = len(set(fingerprints))
        dup_ratio = 1.0 - unique_fps / n if n > 0 else 0.0
        if dup_ratio > self._max_dup_ratio:
            failures.append(f"high_duplicate_ratio:{dup_ratio:.2f}>{self._max_dup_ratio}")

        # Gate 5: spam ratio
        spam_count = sum(1 for t in texts if _is_likely_spam(t))
        spam_ratio = spam_count / n if n > 0 else 0.0
        if spam_ratio > self._max_spam_ratio:
            failures.append(f"high_spam_ratio:{spam_ratio:.2f}>{self._max_spam_ratio}")

        # Gate 6: max age
        ages_hours: list[float] = []
        old_count = 0
        for r in records:
            ts = _parse_created_at(str(r.get("created_at") or ""))
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (now - ts).total_seconds() / 3600.0
            ages_hours.append(age)
            if age > self._max_age_hours:
                old_count += 1
        old_ratio = old_count / n if n > 0 else 0.0
        if old_ratio > 0.5:  # more than half the posts are too old
            failures.append(f"too_old:{old_ratio:.0%}_older_than_{self._max_age_hours}h")

        stats: dict[str, Any] = {
            "source": source,
            "ticker": ticker,
            "n": n,
            "unique_authors": unique_authors,
            "author_concentration": round(author_concentration, 4),
            "top_author_hash": top_author[:6] if top_author else "",
            "duplicate_ratio": round(dup_ratio, 4),
            "spam_ratio": round(spam_ratio, 4),
            "old_ratio": round(old_ratio, 4),
            "mean_age_hours": round(sum(ages_hours) / len(ages_hours), 2) if ages_hours else None,
        }

        return QualityGateResult(
            passed=len(failures) == 0,
            failure_reasons=failures,
            stats=stats,
        )
