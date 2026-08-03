"""S&P 500 constituent resolution with a free source and a fail-closed chain.

Why this module exists
---------------------
On 2026-08-03 a live probe found that FMP had retired the v3 legacy API for this
key: ``/api/v3/sp500_constituent`` returns HTTP 403 ("only available for legacy
users who have valid subscriptions prior August 31, 2025"), and the modern
``/stable/sp500-constituent`` returns 402 Restricted on the current plan. There
was no cached constituents file on disk.

``universe/sp500.py`` was a thin wrapper with no fallback, so
``SP500Universe.get_symbols()`` raised — and with it ``CandidateScanner.full_scan()``,
the ONLY scanner path that can add a symbol to the watchlist. ``weekly_refresh()``
is monotone (it re-filters cached rows and keeps survivors), so the universe could
only shrink. It had decayed to 3 symbols, and both observed drops were the
*transient* 200-DMA trend filter — a temporary market condition made permanent.

Per-symbol ``stable/profile``, ``stable/key-metrics`` and ``stable/quote`` all
still work, so the only missing input was the membership list. This module
supplies it from a free source, caches the last good copy, and — importantly —
refuses to hand back a short list.

Fail-closed contract
--------------------
Every source is validated, not merely awaited. A call that *succeeds* while
returning 3 rows is the shape of the original bug, so ``validate_constituents``
enforces a plausibility floor and the resolver moves to the next source. When no
source can produce a plausible list, ``resolve_constituents`` raises
``ConstituentSourceError`` rather than returning ``[]`` — an empty list would flow
into ``full_scan`` and read downstream as "healthy, just empty", which is exactly
how a 3-symbol scanner passed every guard for two months.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger("universe.sp500_constituents")

# Free, no-key source. Parsed with the stdlib HTML parser — deliberately no new
# dependency (bs4/lxml are not installed and adding one needs operator sign-off).
WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_CONSTITUENTS_TABLE_ID = "constituents"
_USER_AGENT = "stockbot-universe/1.0 (advisory-only portfolio automation)"
_HTTP_TIMEOUT_SECONDS = 20

# The S&P 500 holds ~500 names (503 share classes as of 2026). A parse that
# yields far fewer means the page layout moved, not that the index shrank.
# The floor is what turns "the fetch returned" into "the fetch is usable".
MIN_PLAUSIBLE_CONSTITUENTS = 400

DEFAULT_CACHE_PATH = Path("data/universe/sp500_constituents.json")


class ConstituentSourceError(RuntimeError):
    """No source could produce a plausible constituent list.

    Raised instead of returning an empty or short list so that callers cannot
    mistake "I could not determine the universe" for "the universe is small".
    """


@dataclass(frozen=True)
class ConstituentResolution:
    """Resolved constituents plus the provenance needed to judge them."""

    rows: list[dict[str, Any]]
    source: str          # "fmp" | "free_scrape" | "cache"
    degraded: bool       # True when served from cache (stale, not live)
    detail: str = ""

    @property
    def symbols(self) -> list[str]:
        return sorted(str(r["symbol"]) for r in self.rows if r.get("symbol"))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def normalize_symbol(raw: str) -> str:
    """Normalize a scraped ticker to the dialect every FMP endpoint expects.

    Wikipedia writes class shares with a dot (``BRK.B``); FMP uses a dash
    (``BRK-B``). A symbol carried in the wrong dialect fails every downstream
    quote lookup silently, so this is not cosmetic.
    """
    return (raw or "").strip().upper().replace(".", "-")


class _ConstituentTableParser(HTMLParser):
    """Extract rows from the single table whose id is ``constituents``.

    The page also carries a "changes" table (recent additions/removals);
    scraping it would inject non-constituents into the universe, so table
    identity is matched explicitly rather than by position.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._in_table = False
        self._depth = 0            # nested-table guard
        self._in_cell = False
        self._cell: list[str] = []
        self._row: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrd = dict(attrs)
        if tag == "table":
            if self._in_table:
                self._depth += 1
            elif attrd.get("id") == _CONSTITUENTS_TABLE_ID:
                self._in_table = True
            return
        if not self._in_table:
            return
        if tag == "tr":
            self._row = []
        elif tag == "td":
            self._in_cell = True
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if not self._in_table:
            return
        if tag == "table":
            if self._depth:
                self._depth -= 1
            else:
                self._in_table = False
            return
        if tag == "td" and self._in_cell:
            self._in_cell = False
            self._row.append("".join(self._cell).strip())
        elif tag == "tr":
            # Header rows use <th> and so produce no cells — skipped here.
            if self._row:
                self.rows.append(self._row)
            self._row = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell.append(data)


def parse_constituents_html(html: str) -> list[dict[str, Any]]:
    """Parse constituent rows out of the S&P 500 list page.

    Returns ``[]`` on unparseable input; the plausibility floor in
    ``validate_constituents`` is what turns that into a hard failure. Column
    order on the page is Symbol, Security, GICS Sector, GICS Sub-Industry.
    """
    if not html:
        return []
    parser = _ConstituentTableParser()
    try:
        parser.feed(html)
    except Exception as exc:  # malformed markup — treat as no data
        logger.warning("sp500 constituents: HTML parse failed: %s", exc)
        return []

    out: list[dict[str, Any]] = []
    for cells in parser.rows:
        if len(cells) < 3:
            continue
        symbol = normalize_symbol(cells[0])
        if not symbol:
            continue
        out.append({
            "symbol": symbol,
            "name": cells[1].strip(),
            "sector": cells[2].strip(),
            "subSector": cells[3].strip() if len(cells) > 3 else "",
        })
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_constituents(rows: Any) -> list[dict[str, Any]]:
    """Return de-duplicated rows, or raise if the list is not plausible.

    Applied to EVERY source, including FMP. "The call succeeded" is not
    evidence the result is usable — a 3-row response is the shape of the bug
    this module exists to fix.
    """
    if not isinstance(rows, Iterable) or isinstance(rows, (str, bytes)):
        raise ConstituentSourceError(f"constituents payload is not a list: {type(rows).__name__}")

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = normalize_symbol(str(row.get("symbol") or ""))
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        merged = dict(row)
        merged["symbol"] = symbol
        deduped.append(merged)

    if len(deduped) < MIN_PLAUSIBLE_CONSTITUENTS:
        raise ConstituentSourceError(
            f"only {len(deduped)} distinct constituents resolved, below the "
            f"plausibility floor of {MIN_PLAUSIBLE_CONSTITUENTS} — refusing to "
            "publish a short universe"
        )
    return deduped


# ---------------------------------------------------------------------------
# Free source
# ---------------------------------------------------------------------------

def fetch_from_wikipedia(url: str = WIKIPEDIA_URL) -> list[dict[str, Any]]:
    """Fetch and parse the constituent table from the free public source."""
    import requests  # already a repo dependency; imported late to keep tests light

    resp = requests.get(
        url, timeout=_HTTP_TIMEOUT_SECONDS, headers={"User-Agent": _USER_AGENT}
    )
    resp.raise_for_status()
    return parse_constituents_html(resp.text)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def write_cache(path: Path | str, rows: list[dict[str, Any]], *, source: str) -> None:
    """Persist a last-good constituent list (atomically)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "count": len(rows),
        "constituents": rows,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(path)


def read_cache(path: Path | str) -> tuple[list[dict[str, Any]], str]:
    """Read the cached list. Returns ``([], detail)`` when unusable.

    A corrupt cache is treated as absent, never as an empty universe.
    """
    path = Path(path)
    if not path.exists():
        return [], "cache absent"
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return [], f"cache unreadable: {exc}"
    rows = payload.get("constituents")
    if not isinstance(rows, list):
        return [], "cache missing 'constituents' list"
    return rows, f"cached {payload.get('fetched_at') or 'unknown date'}"


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def resolve_constituents(
    *,
    client: Any = None,
    cache_path: Path | str = DEFAULT_CACHE_PATH,
    fetcher: Optional[Callable[[], list[dict[str, Any]]]] = None,
    ttl_days: int = 7,
) -> ConstituentResolution:
    """Resolve the S&P 500 membership list from the first plausible source.

    Order: FMP (still correct where the plan allows it) → free scrape → last-good
    cache. Each candidate must clear ``validate_constituents``; a source that
    returns a short list is skipped rather than accepted.

    Raises ``ConstituentSourceError`` when nothing plausible is available.
    """
    fetcher = fetcher or fetch_from_wikipedia
    attempts: list[str] = []

    # 1. FMP — free where the subscription still covers it.
    if client is not None:
        try:
            rows = validate_constituents(client.get_sp500_constituents(ttl_days=ttl_days))
        except ConstituentSourceError as exc:
            attempts.append(f"fmp: {exc}")
        except Exception as exc:
            attempts.append(f"fmp: {exc}")
        else:
            _try_write_cache(cache_path, rows, "fmp")
            return ConstituentResolution(rows, "fmp", False, "live FMP constituents")

    # 2. Free public source.
    try:
        rows = validate_constituents(fetcher())
    except ConstituentSourceError as exc:
        attempts.append(f"free_scrape: {exc}")
    except Exception as exc:
        attempts.append(f"free_scrape: {exc}")
    else:
        _try_write_cache(cache_path, rows, "free_scrape")
        return ConstituentResolution(rows, "free_scrape", False, "live free source")

    # 3. Last-good cache — usable, but a degraded read by definition.
    cached, detail = read_cache(cache_path)
    try:
        rows = validate_constituents(cached)
    except ConstituentSourceError as exc:
        attempts.append(f"cache: {exc}" if cached else f"cache: {detail}")
    else:
        logger.warning(
            "sp500 constituents: live sources unavailable, serving %s (%s)",
            f"{len(rows)} cached rows", detail,
        )
        return ConstituentResolution(rows, "cache", True, detail)

    raise ConstituentSourceError(
        "no plausible S&P 500 constituent source available; tried "
        + "; ".join(attempts)
    )


def _try_write_cache(path: Path | str, rows: list[dict[str, Any]], source: str) -> None:
    """Refresh the last-good cache; a write failure must not fail the read."""
    try:
        write_cache(path, rows, source=source)
    except OSError as exc:
        logger.warning("sp500 constituents: cache write failed (%s): %s", path, exc)
