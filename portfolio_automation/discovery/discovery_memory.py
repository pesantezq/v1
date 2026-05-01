"""
Persistent sandbox memory for discovery candidates.

Stores candidate history in outputs/sandbox/discovery/discovery_memory.json.
Memory file is written by the reports layer, not directly by this module.

Tolerates: missing file, empty file, corrupt JSON, missing optional fields.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.discovery.candidate_promotion_engine import (
    CandidateStatus,
    DiscoveryCandidate,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Memory entry
# ---------------------------------------------------------------------------

@dataclass
class MemoryEntry:
    """Persisted record for one ticker across multiple discovery runs."""
    ticker: str
    first_seen: str          # ISO timestamp
    last_seen: str           # ISO timestamp
    mention_count: int
    source_count: int
    seen_runs: int
    status: str              # CandidateStatus value
    last_score: float
    last_event_type: str
    rejected_reason: str | None = None
    discovery_only: bool = True
    sandbox_only: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> MemoryEntry:
        return cls(
            ticker=str(data.get("ticker", "")),
            first_seen=str(data.get("first_seen", "")),
            last_seen=str(data.get("last_seen", "")),
            mention_count=int(data.get("mention_count", 0)),
            source_count=int(data.get("source_count", 0)),
            seen_runs=int(data.get("seen_runs", 0)),
            status=str(data.get("status", CandidateStatus.DISCOVERED.value)),
            last_score=float(data.get("last_score", 0.0)),
            last_event_type=str(data.get("last_event_type", "unknown")),
            rejected_reason=data.get("rejected_reason"),
            discovery_only=bool(data.get("discovery_only", True)),
            sandbox_only=bool(data.get("sandbox_only", True)),
        )


# ---------------------------------------------------------------------------
# Memory store
# ---------------------------------------------------------------------------

class DiscoveryMemory:
    """
    In-memory store for discovery candidate history, backed by a JSON file.

    Call :meth:`load` to read existing memory, then :meth:`update` after each
    discovery run, then retrieve the updated dict via :meth:`to_dict` for
    serialization by the reports layer.

    The file is never written directly by this class — the reports layer
    owns all file I/O to preserve data governance boundaries.
    """

    def __init__(self) -> None:
        self._entries: dict[str, MemoryEntry] = {}

    # ------------------------------------------------------------------
    # I/O helpers (called by the reports layer)
    # ------------------------------------------------------------------

    @classmethod
    def load_from_path(cls, path: Path | str) -> "DiscoveryMemory":
        """
        Load memory from *path*. Tolerates missing file, empty file, and
        corrupt JSON — returns an empty :class:`DiscoveryMemory` on any error.
        """
        mem = cls()
        p = Path(path)
        if not p.exists():
            return mem
        try:
            raw = p.read_text(encoding="utf-8").strip()
            if not raw:
                return mem
            data = json.loads(raw)
            entries = data.get("entries") if isinstance(data, dict) else data
            if isinstance(entries, list):
                for item in entries:
                    if not isinstance(item, dict) or not item.get("ticker"):
                        continue
                    try:
                        entry = MemoryEntry.from_dict(item)
                        mem._entries[entry.ticker] = entry
                    except (TypeError, ValueError):
                        pass
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.warning("discovery_memory: could not load %s (non-fatal): %s", path, exc)
        return mem

    def to_dict(self) -> dict:
        """Return the full memory payload for JSON serialization."""
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "discovery_only": True,
            "sandbox_only": True,
            "entry_count": len(self._entries),
            "entries": [e.to_dict() for e in self._entries.values()],
        }

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update(
        self,
        candidates: list[DiscoveryCandidate],
        *,
        now: datetime | None = None,
    ) -> None:
        """
        Merge *candidates* into the in-memory store.

        - ``first_seen`` is preserved for returning tickers.
        - ``last_seen``, ``mention_count``, ``source_count``, ``seen_runs``,
          ``status``, ``last_score``, ``last_event_type`` are always updated.
        """
        ts = (now or datetime.now(timezone.utc)).isoformat()
        for cand in candidates:
            existing = self._entries.get(cand.ticker)
            if existing is None:
                self._entries[cand.ticker] = MemoryEntry(
                    ticker=cand.ticker,
                    first_seen=cand.first_seen or ts,
                    last_seen=cand.last_seen or ts,
                    mention_count=cand.mention_count,
                    source_count=cand.unique_source_count,
                    seen_runs=1,
                    status=cand.status.value,
                    last_score=cand.score,
                    last_event_type=cand.event_type.value,
                    rejected_reason=cand.rejection_reason,
                )
            else:
                existing.last_seen = cand.last_seen or ts
                existing.mention_count += cand.mention_count
                existing.source_count = max(existing.source_count, cand.unique_source_count)
                existing.seen_runs += 1
                existing.status = cand.status.value
                existing.last_score = cand.score
                existing.last_event_type = cand.event_type.value
                existing.rejected_reason = cand.rejection_reason

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get(self, ticker: str) -> MemoryEntry | None:
        return self._entries.get(ticker)

    def all(self) -> list[MemoryEntry]:
        return list(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)
