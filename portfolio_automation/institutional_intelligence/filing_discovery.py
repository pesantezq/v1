"""
Filing discovery — turn a manager CIK into a list of 13F :class:`FilingRef`s
using the governed SEC client's submissions history.

Point-in-time contract: the signal timestamp is the filing's public
availability (``filingDate``), never the quarter-end (``reportDate``). Notices
(13F-NT) are recognized but flagged ``is_holdings=False`` — a notice is NOT a
holdings filing and carries no information table.
"""

from __future__ import annotations

import json
from datetime import date

from .schemas import ALL_13F_FORMS, AMENDMENT_FORMS, FilingRef
from .sec_client import GovernedSECClient


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


def discover_filings(client: GovernedSECClient, cik: str | int) -> list[FilingRef]:
    """Return all 13F filings for ``cik`` (holdings + notices, incl. amendments).

    Degrades to ``[]`` when the client is disabled / the submissions response is
    absent or malformed — never raises.
    """
    resp = client.fetch_submissions(cik)
    if not resp.ok or not resp.body:
        return []
    try:
        data = json.loads(resp.body)
    except (json.JSONDecodeError, TypeError):
        return []
    return parse_submissions(data)


def parse_submissions(data: dict) -> list[FilingRef]:
    """Pure: extract 13F FilingRefs from an EDGAR submissions JSON mapping.

    Handles the ``filings.recent`` column-oriented arrays defensively; tolerates
    missing/ragged columns.
    """
    if not isinstance(data, dict):
        return []
    cik_raw = data.get("cik")
    cik = str(cik_raw).zfill(10) if cik_raw is not None else ""

    recent = (((data.get("filings") or {}).get("recent")) or {})
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    filing_dates = recent.get("filingDate") or []
    report_dates = recent.get("reportDate") or []
    primary_docs = recent.get("primaryDocument") or []

    n = min(len(forms), len(accessions), len(filing_dates))
    out: list[FilingRef] = []
    for i in range(n):
        form = str(forms[i]).strip()
        if form not in ALL_13F_FORMS:
            continue
        filed_at = _parse_date(filing_dates[i])
        if filed_at is None:
            # Without a public availability date we cannot honor the
            # point-in-time contract — skip rather than guess.
            continue
        report_period = _parse_date(report_dates[i]) if i < len(report_dates) else None
        primary_doc = primary_docs[i] if i < len(primary_docs) else None
        out.append(FilingRef(
            cik=cik,
            accession=str(accessions[i]).strip(),
            form_type=form,
            filed_at=filed_at,
            report_period=report_period,
            primary_doc=primary_doc,
            is_amendment=form in AMENDMENT_FORMS,
        ))
    return out


def latest_holdings_filing(filings: list[FilingRef]) -> FilingRef | None:
    """The most recent HOLDINGS filing (13F-HR/A) by filing date, or None.

    Notices are excluded — they carry no information table.
    """
    holdings = [f for f in filings if f.is_holdings]
    if not holdings:
        return None
    return max(holdings, key=lambda f: f.filed_at)
