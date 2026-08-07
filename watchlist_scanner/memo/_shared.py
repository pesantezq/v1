"""Primitives shared by the split-out memo renderers.

These live here rather than in ``daily_memo`` so extracted modules can use them
without importing their former home, which would be circular.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("watchlist_scanner.daily_memo")

_SEP = "=" * 48
_LINE = "-" * 48


def _safe_load(path: Path) -> dict[str, Any]:
    """Load a JSON object, degrading to ``{}`` rather than raising.

    The memo is a read-only consumer of artifacts it does not own, so a missing
    or malformed input must narrow what the memo can say, never break the run.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("daily_memo: could not load %s — %s", path, exc)
        return {}


def _flt(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# Defense-in-depth: these strings must never appear as a DECISION in memo
# output. The memo is advisory-only and the discovery lane is sandbox-only, so
# a rendered "buy"/"sell" would misrepresent the system's authority regardless
# of which renderer produced it.
_FORBIDDEN_MEMO_DECISIONS: frozenset[str] = frozenset(
    {"buy", "sell", "actionable", "promoted", "validated"}
)
