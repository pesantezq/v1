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

# --- Cache freshness policy -------------------------------------------------
# Plausibility and freshness are INDEPENDENT gates. A >=400-row list can stay
# structurally plausible while becoming materially wrong, so a last-good cache
# must also prove it is current enough to trust.
#
# CACHE_FRESH_MAX_DAYS deliberately equals degraded_mode.DEFAULT_STALE_DAYS (7),
# the repo's existing cache-staleness convention, so the scanner does not invent
# a second freshness vocabulary.
#
# CACHE_USABLE_MAX_DAYS (30) is a POLICY DEFAULT, not an empirically validated
# constant. Rationale: S&P 500 membership turns over a handful of names per
# quarter, so a month-old list is still approximately right, while beyond that
# the drift compounds and both live sources having failed for a month is itself
# the real incident. Adjust here if the operator's tolerance differs — nothing
# downstream hardcodes these numbers.
CACHE_FRESH_MAX_DAYS = 7
CACHE_USABLE_MAX_DAYS = 30

FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_EXPIRED = "expired"
FRESHNESS_UNKNOWN = "unknown"

DEFAULT_CACHE_PATH = Path("data/universe/sp500_constituents.json")


class ConstituentSourceError(RuntimeError):
    """No source could produce a plausible constituent list.

    Raised instead of returning an empty or short list so that callers cannot
    mistake "I could not determine the universe" for "the universe is small".
    """


@dataclass(frozen=True)
class ConstituentResolution:
    """Resolved constituents plus the provenance needed to judge them.

    ``freshness``/``age_days``/``fetched_at`` are additive (added 2026-08-03) and
    default to a live-read shape, so pre-existing callers that only read
    ``rows``/``source``/``degraded`` keep working unchanged.
    """

    rows: list[dict[str, Any]]
    source: str          # "fmp" | "free_scrape" | "cache"
    degraded: bool       # True when served from cache (stale, not live)
    detail: str = ""
    freshness: str = FRESHNESS_FRESH
    age_days: float | None = 0.0
    fetched_at: str | None = None

    @property
    def symbols(self) -> list[str]:
        return sorted(str(r["symbol"]) for r in self.rows if r.get("symbol"))

    @property
    def count(self) -> int:
        return len(self.rows)

    def as_payload(self) -> dict[str, Any]:
        """JSON-serializable provenance for transport to artifacts/oversight.

        Calculated once here and carried outward — consumers must not recompute
        freshness from a file mtime or re-derive counts from the row list.
        """
        return {
            "source": self.source,
            "count": self.count,
            "fetched_at": self.fetched_at,
            "age_days": self.age_days,
            "freshness": self.freshness,
            "degraded": self.degraded,
            "detail": self.detail,
            "plausibility_floor": MIN_PLAUSIBLE_CONSTITUENTS,
            "fresh_max_days": CACHE_FRESH_MAX_DAYS,
            "usable_max_days": CACHE_USABLE_MAX_DAYS,
        }


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO timestamp to tz-aware UTC. Returns None when undeterminable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    # Timestamps written before tz-awareness was enforced are treated as UTC
    # rather than rejected — they are still usable evidence of age.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def classify_cache_freshness(
    fetched_at: Any, now: Any,
) -> tuple[str, float | None]:
    """Classify a cache timestamp into (freshness_state, age_days).

    Returns ``(FRESHNESS_UNKNOWN, None)`` — never ``fresh``, and never age 0 —
    when the timestamp is missing, unparseable, or in the future. An unknown age
    must not read as current: that is how a stale universe stays trusted. A
    future timestamp is treated as unknown rather than fresh because clock skew
    or hand editing cannot certify currency.
    """
    stamp = _parse_ts(fetched_at)
    reference = _parse_ts(now) or datetime.now(timezone.utc)
    if stamp is None:
        return FRESHNESS_UNKNOWN, None

    age_days = (reference - stamp).total_seconds() / 86400.0
    if age_days < 0:
        return FRESHNESS_UNKNOWN, None
    if age_days <= CACHE_FRESH_MAX_DAYS:
        return FRESHNESS_FRESH, age_days
    if age_days <= CACHE_USABLE_MAX_DAYS:
        return FRESHNESS_STALE, age_days
    return FRESHNESS_EXPIRED, age_days


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

    A corrupt cache is treated as absent, never as an empty universe. Kept for
    backward compatibility; ``read_cache_entry`` also returns the timestamp.
    """
    rows, detail, _ = read_cache_entry(path)
    return rows, detail


def read_cache_entry(path: Path | str) -> tuple[list[dict[str, Any]], str, Any]:
    """Read the cache as ``(rows, detail, fetched_at)``.

    Freshness is judged from the RECORDED ``fetched_at``, never from the file's
    mtime — a touched, rsynced, or restored file would otherwise look new while
    its contents are months old. Existence is not evidence of currency.
    """
    path = Path(path)
    if not path.exists():
        return [], "cache absent", None
    try:
        payload = json.loads(path.read_text())
    except (OSError, IsADirectoryError, ValueError) as exc:
        return [], f"cache unreadable: {exc}", None
    if not isinstance(payload, dict):
        return [], "cache is not an object", None
    rows = payload.get("constituents")
    if not isinstance(rows, list):
        return [], "cache missing 'constituents' list", None
    fetched_at = payload.get("fetched_at")
    return rows, f"cached {fetched_at or 'unknown date'}", fetched_at


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def resolve_constituents(
    *,
    client: Any = None,
    cache_path: Path | str = DEFAULT_CACHE_PATH,
    fetcher: Optional[Callable[[], list[dict[str, Any]]]] = None,
    ttl_days: int = 7,
    now: str | None = None,
) -> ConstituentResolution:
    """Resolve the S&P 500 membership list from the first plausible source.

    Order: FMP (still correct where the plan allows it) → free scrape → last-good
    cache. Each candidate must clear ``validate_constituents``; a source that
    returns a short list is skipped rather than accepted.

    Raises ``ConstituentSourceError`` when nothing plausible is available.
    """
    fetcher = fetcher or fetch_from_wikipedia
    stamp = now or datetime.now(timezone.utc).isoformat()
    attempts: list[str] = []

    # 1. FMP — free where the subscription still covers it. A live read is fresh
    #    by definition; the freshness policy gates the FALLBACK, never this.
    if client is not None:
        try:
            rows = validate_constituents(client.get_sp500_constituents(ttl_days=ttl_days))
        except ConstituentSourceError as exc:
            attempts.append(f"fmp: {exc}")
        except Exception as exc:
            attempts.append(f"fmp: {exc}")
        else:
            _try_write_cache(cache_path, rows, "fmp")
            return ConstituentResolution(
                rows, "fmp", False, "live FMP constituents",
                freshness=FRESHNESS_FRESH, age_days=0.0, fetched_at=stamp)

    # 2. Free public source.
    try:
        rows = validate_constituents(fetcher())
    except ConstituentSourceError as exc:
        attempts.append(f"free_scrape: {exc}")
    except Exception as exc:
        attempts.append(f"free_scrape: {exc}")
    else:
        _try_write_cache(cache_path, rows, "free_scrape")
        return ConstituentResolution(
            rows, "free_scrape", False, "live free source",
            freshness=FRESHNESS_FRESH, age_days=0.0, fetched_at=stamp)

    # 3. Last-good cache — a degraded read by definition, and now also subject to
    #    an explicit freshness gate. Plausibility and freshness are independent:
    #    both must pass. The six cache outcomes (absent / unreadable /
    #    implausible / unknown-age / stale / expired) stay distinguishable in
    #    `attempts` so the raised error names which one actually happened.
    cached, detail, fetched_at = read_cache_entry(cache_path)
    freshness, age_days = classify_cache_freshness(fetched_at, stamp)
    try:
        rows = validate_constituents(cached)
    except ConstituentSourceError as exc:
        attempts.append(f"cache: {exc}" if cached else f"cache: {detail}")
    else:
        if freshness == FRESHNESS_UNKNOWN:
            attempts.append(
                "cache: age unknown (missing/unparseable/future fetched_at) — "
                "cannot certify currency, refusing to serve")
        elif freshness == FRESHNESS_EXPIRED:
            attempts.append(
                f"cache: expired ({age_days:.1f}d > {CACHE_USABLE_MAX_DAYS}d max usable age)")
        else:
            note = (f"{detail}; {freshness} ({age_days:.1f}d old)")
            logger.warning(
                "sp500 constituents: live sources unavailable, serving %d cached rows (%s)",
                len(rows), note)
            return ConstituentResolution(
                rows, "cache", True, note,
                freshness=freshness, age_days=age_days, fetched_at=fetched_at)

    raise ConstituentSourceError(
        "no plausible, current S&P 500 constituent source available; tried "
        + "; ".join(attempts)
    )


def _try_write_cache(path: Path | str, rows: list[dict[str, Any]], source: str) -> None:
    """Refresh the last-good cache; a write failure must not fail the read."""
    try:
        write_cache(path, rows, source=source)
    except OSError as exc:
        logger.warning("sp500 constituents: cache write failed (%s): %s", path, exc)
