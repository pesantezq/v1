"""
SEC EDGAR adapter — fetches recent 8-K, 10-Q, 10-K filings for a ticker.

Data source
-----------
Uses the SEC EDGAR public JSON API (no API key required, rate limit: 10 req/s).

  CIK lookup:   https://www.sec.gov/files/company_tickers.json
  Submissions:  https://data.sec.gov/submissions/CIK{cik:010d}.json

Only filing *metadata* is captured in this first version (form type, date,
company name, accession number, index URL).  Full text extraction is a planned
next step and would be layered on top without changing this model.

Separation guarantee
--------------------
parse_quality = 1.0 for well-formed SEC metadata (regulatory filings are the
highest-quality evidence source).  source_weight = 1.0 in provenance.py.
These fields only affect scraped_confidence, never signal_score.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from scraped_intel.base import SourceAdapter
from scraped_intel.models import ScrapedRecord

logger = logging.getLogger("scraped_intel.sec_filings")

# EDGAR rate limit: 10 req/s; we stay conservative
_REQUEST_DELAY_S = 0.15
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_FILING_INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no_dashes}/"
    "{acc_no_dashes}-index.htm"
)
_RELEVANT_FORMS = {"8-K", "10-Q", "10-K"}
_HEADERS = {
    "User-Agent": "StockBot/1.0 portfolio-research@example.com",
    "Accept": "application/json",
}


def _fetch_json(url: str, timeout: int = 10) -> Optional[dict]:
    """GET url → parsed JSON dict, or None on any error."""
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.warning("SEC HTTP %s for %s", exc.code, url)
    except Exception as exc:
        logger.warning("SEC fetch failed for %s: %s", url, exc)
    return None


class SECFilingsAdapter(SourceAdapter):
    """
    Fetches recent SEC filings (8-K, 10-Q, 10-K) for a given ticker.

    Cache strategy
    --------------
    company_tickers.json is cached for 7 days (CIK mapping is stable).
    Per-company submissions are cached for 24 hours.
    Both use the shared scraped_cache directory as simple JSON files.
    """

    source_type = "sec_filing"
    domain = "sec.gov"
    source_weight = 1.0   # regulatory filings = highest-trust source

    def __init__(self, cache_dir: str = "data/scraped_cache") -> None:
        super().__init__(cache_dir=cache_dir)
        self._cache_path = Path(cache_dir)
        self._cache_path.mkdir(parents=True, exist_ok=True)
        self._ticker_cik: Optional[dict[str, int]] = None  # lazy-loaded

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch(self, symbol: str, lookback_days: int = 30) -> list[ScrapedRecord]:
        """Return recent SEC filings for `symbol` within `lookback_days`."""
        cik = self._resolve_cik(symbol)
        if cik is None:
            logger.debug("SECFilingsAdapter: no CIK found for %s", symbol)
            return []

        submissions = self._get_submissions(cik)
        if not submissions:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        return self._parse_filings(symbol, cik, submissions, cutoff)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_cik(self, symbol: str) -> Optional[int]:
        """Look up the SEC CIK number for a ticker symbol."""
        if self._ticker_cik is None:
            self._ticker_cik = self._load_ticker_cik_map()
        return self._ticker_cik.get(symbol.upper())

    def _load_ticker_cik_map(self) -> dict[str, int]:
        """Load (or refresh from cache) the full ticker → CIK mapping."""
        cache_file = self._cache_path / "sec_company_tickers.json"
        seven_days = 7 * 24 * 3600

        if cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < seven_days:
                try:
                    raw = json.loads(cache_file.read_text(encoding="utf-8"))
                    return self._parse_tickers_json(raw)
                except Exception:
                    pass

        time.sleep(_REQUEST_DELAY_S)
        data = _fetch_json(_TICKERS_URL)
        if data:
            try:
                cache_file.write_text(json.dumps(data), encoding="utf-8")
            except Exception:
                pass
            return self._parse_tickers_json(data)
        return {}

    @staticmethod
    def _parse_tickers_json(data: dict) -> dict[str, int]:
        """Convert company_tickers.json → {TICKER: cik_int} mapping."""
        result: dict[str, int] = {}
        for entry in data.values():
            ticker = str(entry.get("ticker") or "").upper().strip()
            cik = entry.get("cik_str")
            if ticker and cik:
                result[ticker] = int(cik)
        return result

    def _get_submissions(self, cik: int) -> Optional[dict]:
        """Fetch EDGAR company submissions (with 24h cache)."""
        cache_file = self._cache_path / f"sec_submissions_{cik}.json"
        one_day = 86400

        if cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < one_day:
                try:
                    return json.loads(cache_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

        time.sleep(_REQUEST_DELAY_S)
        url = _SUBMISSIONS_URL.format(cik=cik)
        data = _fetch_json(url)
        if data:
            try:
                cache_file.write_text(json.dumps(data), encoding="utf-8")
            except Exception:
                pass
        return data

    def _parse_filings(
        self,
        symbol: str,
        cik: int,
        submissions: dict,
        cutoff: datetime,
    ) -> list[ScrapedRecord]:
        """Extract recent relevant filings from the submissions JSON."""
        filings_section: dict[str, Any] = submissions.get("filings", {})
        recent: dict[str, Any] = filings_section.get("recent", {})

        form_types  = recent.get("form", [])
        filed_dates = recent.get("filingDate", [])
        acc_nums    = recent.get("accessionNumber", [])
        descriptions = recent.get("primaryDocDescription", [])
        company_name = submissions.get("name", symbol)

        records: list[ScrapedRecord] = []
        collected_at = self.now_iso()

        for i, form in enumerate(form_types):
            if form not in _RELEVANT_FORMS:
                continue
            try:
                filed_str = filed_dates[i] if i < len(filed_dates) else ""
                if not filed_str:
                    continue
                filed_dt = datetime.fromisoformat(filed_str).replace(tzinfo=timezone.utc)
                if filed_dt < cutoff:
                    break  # filings are newest-first; once past cutoff, stop

                acc = acc_nums[i] if i < len(acc_nums) else ""
                acc_dashes = acc  # e.g. "0001234567-25-000123"
                acc_nodash = acc.replace("-", "")
                desc = descriptions[i] if i < len(descriptions) else ""
                title = f"{company_name} — {form} ({filed_str})"
                if desc:
                    title += f": {desc}"

                url = _FILING_INDEX_URL.format(cik=cik, acc_no_dashes=acc_nodash)
                pub_iso = f"{filed_str}T00:00:00Z"
                record_id = ScrapedRecord.make_record_id(url, title, filed_str)

                records.append(ScrapedRecord(
                    symbol=symbol,
                    source_type=self.source_type,
                    domain=self.domain,
                    url=url,
                    published_at=pub_iso,
                    collected_at=collected_at,
                    title=title,
                    excerpt="",   # filing text not fetched in this version
                    extraction_status="ok",
                    parse_quality=1.0,   # SEC metadata is fully structured
                    themes=[],           # no theme classification on bare metadata
                    sentiment=None,      # no sentiment on bare metadata
                    recency_hours=self.recency_hours(pub_iso),
                    record_id=record_id,
                    extra={
                        "form_type":        form,
                        "cik":              cik,
                        "accession_number": acc_dashes,
                        "company_name":     company_name,
                    },
                ))
            except (IndexError, ValueError, TypeError) as exc:
                logger.debug("SECFilingsAdapter: parse error at index %d: %s", i, exc)

        logger.debug(
            "SECFilingsAdapter: %d %s filings for %s (lookback covers %d)",
            len(records), "/".join(sorted(_RELEVANT_FORMS)), symbol, len(form_types),
        )
        return records
