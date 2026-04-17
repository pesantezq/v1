"""
Cache manager for the watchlist scanner.

Each entry is stored as a separate JSON file:
    data/watchlist_cache/<safe_key>.json
    → { "stored_at": "<ISO>", "data": <payload> }

TTL is checked on read; stale files are replaced on the next write.
A separate call_counter.json tracks daily API usage and resets at midnight.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("watchlist_scanner.cache")


class CacheManager:
    """
    Simple JSON disk cache with per-call TTL checking.

    Args:
        cache_dir: Directory to store cache files (created if absent).
    """

    def __init__(self, cache_dir: str | Path = "data/watchlist_cache") -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._counter_path = self._dir / "call_counter.json"

    # ── Cache read / write ─────────────────────────────────────────────────

    def _path(self, key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
        return self._dir / f"{safe[:80]}.json"

    def get(self, key: str, ttl_seconds: int) -> Optional[Any]:
        """Return cached data if it exists and is within TTL; else None."""
        p = self._path(key)
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            stored_at = datetime.fromisoformat(d["stored_at"])
            age = (datetime.now() - stored_at).total_seconds()
            if age > ttl_seconds:
                logger.debug("Cache expired for %r (age=%.0fs, ttl=%ds)", key, age, ttl_seconds)
                return None
            logger.debug("Cache hit for %r (age=%.0fs)", key, age)
            return d["data"]
        except Exception as exc:
            logger.warning("Cache read failed for %r: %s", key, exc)
            return None

    def get_stale(self, key: str) -> Optional[Any]:
        """Return cached data regardless of age (budget-exceeded fallback)."""
        p = self._path(key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))["data"]
        except Exception:
            return None

    def get_age_seconds(self, key: str) -> Optional[float]:
        """
        Return age in seconds of a cache entry, regardless of TTL.
        Returns None if the file does not exist or cannot be read.
        """
        p = self._path(key)
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            stored_at = datetime.fromisoformat(d["stored_at"])
            return (datetime.now() - stored_at).total_seconds()
        except Exception:
            return None

    def set(self, key: str, data: Any) -> None:
        """Write data to cache with current timestamp."""
        p = self._path(key)
        try:
            p.write_text(
                json.dumps(
                    {"stored_at": datetime.now().isoformat(), "data": data},
                    ensure_ascii=False,
                    default=str,
                ),
                encoding="utf-8",
            )
            logger.debug("Cache set for %r", key)
        except OSError as exc:
            logger.warning("Cache write failed for %r: %s", key, exc)

    def is_valid(self, key: str, ttl_seconds: int) -> bool:
        """Return True if a valid (non-expired) cache entry exists."""
        return self.get(key, ttl_seconds) is not None

    # ── Daily call counter ─────────────────────────────────────────────────

    def _load_counter(self) -> dict:
        today = date.today().isoformat()
        if self._counter_path.exists():
            try:
                d = json.loads(self._counter_path.read_text(encoding="utf-8"))
                if d.get("date") == today:
                    return d
            except Exception:
                pass
        return {"date": today, "count": 0}

    def _save_counter(self, d: dict) -> None:
        try:
            self._counter_path.write_text(json.dumps(d), encoding="utf-8")
        except OSError:
            pass

    @property
    def calls_today(self) -> int:
        return self._load_counter().get("count", 0)

    def increment_calls(self, n: int = 1) -> int:
        d = self._load_counter()
        d["count"] += n
        self._save_counter(d)
        return d["count"]

    def would_exceed(self, max_calls: int, additional: int = 1) -> bool:
        return self.calls_today + additional > max_calls
