"""Memo cockpit — phone-readable daily memo view.

Parses ``outputs/latest/daily_memo.md`` into 6 named sections mapped from
the memo's ``##`` headers, then strips raw fingerprint hashes so the mobile
view stays clean.

Section mapping
---------------
Top Insight      ← "Today's Verdict" / "Top Insight"
Risk Focus       ← "Risk Delta" / "Risk Focus" / "Portfolio Pulse"
Portfolio Decisions ← "Top Decisions" / "Capital Actions" / "Top Movers"
Data Quality     ← "System / Data Health" / "What Changed" / "Advisor Stack"
Quant Notes      ← "Decision Hit Rate" / "What To Watch" / "Portfolio Growth" / "Discovery Research"
Watchlist Notes  ← "Watch list" / "Sandbox"

Unlabeled header content (date, frontmatter) is silently skipped.

SAFETY: No new buy/sell/execute/trade action controls are added here.
Decision lines from the memo pipeline are rendered as-is (text only).
"""
from __future__ import annotations

import html as _html_mod
import re
from pathlib import Path
from typing import Any

from gui_v2.data.shared import _read_json  # noqa: F401 (imported for symmetry; used only for JSON)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Regex: exactly 16 hex characters as a standalone token (fingerprint hashes).
# Strips tokens like ``f60e0b9d51bec808`` so the mobile memo stays clean.
_HEX_HASH_RE = re.compile(r"\b[0-9a-f]{16}\b", re.IGNORECASE)

# Inline-markdown conversion patterns (M2 fix).
# Applied AFTER HTML-escaping so markup delimiters are safe.
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")

# Mapping from normalised header text → section key
# Matching is case-insensitive, substring-based.
_HEADER_MAP: list[tuple[str, str]] = [
    # Top Insight
    ("today's verdict", "Top Insight"),
    ("top insight",     "Top Insight"),
    # Risk Focus
    ("risk delta",      "Risk Focus"),
    ("risk focus",      "Risk Focus"),
    ("portfolio pulse", "Risk Focus"),
    # Portfolio Decisions
    ("top decisions",          "Portfolio Decisions"),
    ("capital actions",        "Portfolio Decisions"),
    ("top movers",             "Portfolio Decisions"),
    # Data Quality
    ("system / data health",   "Data Quality"),
    ("system/data health",     "Data Quality"),
    ("what changed",           "Data Quality"),
    ("advisor stack",          "Data Quality"),
    # Quant Notes
    ("decision hit rate",      "Quant Notes"),
    ("what to watch",          "Quant Notes"),
    ("portfolio growth",       "Quant Notes"),
    ("discovery research",     "Quant Notes"),
    # Watchlist Notes
    ("watch list",             "Watchlist Notes"),
    ("watchlist",              "Watchlist Notes"),
    ("sandbox",                "Watchlist Notes"),
]

# Section display order
_SECTION_ORDER: list[str] = [
    "Top Insight",
    "Risk Focus",
    "Portfolio Decisions",
    "Data Quality",
    "Quant Notes",
    "Watchlist Notes",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _render_inline_md(text: str) -> str:
    """
    Convert a single line of memo text to safe HTML (M2 fix).

    Steps:
    1. HTML-escape the entire string (prevents XSS).
    2. Convert ``**x**`` → ``<strong>x</strong>`` (bold).
    3. Convert `` `x` `` → ``<code>x</code>`` (inline code).

    The result is marked safe for template rendering via ``| safe``.
    """
    escaped = _html_mod.escape(text)
    escaped = _BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    escaped = _CODE_RE.sub(r"<code>\1</code>", escaped)
    return escaped


def _map_header(header_text: str) -> str | None:
    """Return the section key for a ``##`` header, or None to skip it."""
    lower = header_text.strip().lower()
    for fragment, section in _HEADER_MAP:
        if fragment in lower:
            return section
    return None


def _strip_hashes(text: str) -> str:
    """Remove 16-hex-char fingerprint tokens from *text*."""
    return _HEX_HASH_RE.sub("[…]", text)


def _parse_memo(raw: str) -> dict[str, list[str]]:
    """
    Split *raw* on ``##`` headers and assign each block to a section.

    Returns a dict mapping section name → list of content lines (stripped).
    Sections with no matching header are absent from the dict.
    """
    sections: dict[str, list[str]] = {}
    current_section: str | None = None
    buffer: list[str] = []

    def _flush():
        if current_section and buffer:
            block = [ln for ln in buffer if ln.strip()]  # drop pure-blank lines
            if block:
                sections.setdefault(current_section, []).extend(block)

    for raw_line in raw.splitlines():
        # Strip fingerprint hashes from every line before storing
        line = _strip_hashes(raw_line)

        if line.startswith("## "):
            _flush()
            buffer = []
            header_text = line[3:].strip()
            current_section = _map_header(header_text)
        else:
            buffer.append(line)

    _flush()
    return sections


# ---------------------------------------------------------------------------
# Public collector
# ---------------------------------------------------------------------------

def collect_memo_view(root: Path) -> dict[str, Any]:
    """
    Persona collector for /dashboard/memo.

    Returns::

        {
          "sections":         [ {"title": str, "lines": [str]} … ],
          "memo_date":        str | None,
          "persona":          "memo",
          "source_artifacts": ["daily_memo.md"],
          "observe_only":     True,
          "empty":            bool,   # True when memo absent / empty
        }
    """
    root = Path(root)
    memo_path = root / "outputs" / "latest" / "daily_memo.md"

    # ---------- empty state ----------
    if not memo_path.exists():
        return {
            "sections": [],
            "memo_date": None,
            "persona": "memo",
            "source_artifacts": ["daily_memo.md"],
            "observe_only": True,
            "empty": True,
            "empty_message": "No memo yet — run the daily pipeline.",
        }

    raw = ""
    try:
        raw = memo_path.read_text(encoding="utf-8")
    except Exception:
        pass

    if not raw.strip():
        return {
            "sections": [],
            "memo_date": None,
            "persona": "memo",
            "source_artifacts": ["daily_memo.md"],
            "observe_only": True,
            "empty": True,
            "empty_message": "No memo yet — run the daily pipeline.",
        }

    # ---------- extract date from first line ----------
    memo_date: str | None = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("# ") and "—" in line:
            # e.g. "# Daily Investment Memo — 2026-06-08"
            memo_date = line.split("—", 1)[-1].strip()
            break
        if line.startswith("**Date:**"):
            memo_date = line.split(":", 1)[-1].strip()
            break

    # ---------- parse sections ----------
    parsed = _parse_memo(raw)

    # Build ordered sections list; include every named section even if empty.
    # Each section gets both raw `lines` (for template prefix-based branching)
    # and `rendered_lines` (HTML-escaped + inline-md converted, safe to | safe).
    sections: list[dict[str, Any]] = []
    for title in _SECTION_ORDER:
        lines = parsed.get(title, [])
        sections.append({
            "title": title,
            "lines": lines,
            "rendered_lines": [_render_inline_md(ln) for ln in lines],
        })

    return {
        "sections": sections,
        "memo_date": memo_date,
        "persona": "memo",
        "source_artifacts": ["daily_memo.md"],
        "observe_only": True,
        "empty": False,
        "empty_message": "",
    }
