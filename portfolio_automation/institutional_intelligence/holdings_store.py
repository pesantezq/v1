"""
Point-in-time holdings store (SQLite).

Stores 13F filings + parsed holdings with a strict point-in-time contract:

  * Append-only raw filing records, idempotent by accession number.
  * Public-availability (``filed_at``) and quarter-end (``report_period``) are
    stored SEPARATELY. ``filed_at`` — never the quarter-end — is the earliest
    signal-availability time.
  * Amendments (13F-HR/A) supersede the prior effective filing for the same
    (cik, report_period) DETERMINISTICALLY, but the superseded rows are kept and
    remain auditable (``is_effective`` + ``superseded_by``).
  * Raw reported values are preserved verbatim; normalized/derived fields are a
    separate concern (never mutated back into the raw rows).
  * Content hashes are recorded; a re-import of an unchanged accession is a no-op.
  * Every filing import runs inside a single transaction.

Anti-look-ahead: ``effective_*_asof(cik, report_period, as_of)`` considers only
filings with ``filed_at <= as_of`` — an amendment is invisible before it is
filed, and a quarter's data is invisible before its filing became public.

Mirrors the repo SQLite convention (module-level DDL, idempotent construction,
context-managed writes, upsert-based idempotence) used by
``crowd_intelligence/capability_store.py`` and ``data_budget/usage_ledger.py``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .schemas import ParsedFiling, ParsedHolding

_DDL = """
CREATE TABLE IF NOT EXISTS institutional_filings (
    accession TEXT PRIMARY KEY,
    cik TEXT NOT NULL,
    form_type TEXT NOT NULL,
    filed_at TEXT NOT NULL,            -- public availability (ISO date)
    report_period TEXT,               -- quarter-end (ISO date), stored separately
    is_amendment INTEGER NOT NULL DEFAULT 0,
    is_notice INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT,
    is_effective INTEGER NOT NULL DEFAULT 1,
    superseded_by TEXT,               -- accession of the amendment that supersedes
    holdings_count INTEGER NOT NULL DEFAULT 0,
    parse_warnings TEXT,
    ingested_at REAL
);
CREATE INDEX IF NOT EXISTS idx_filings_cik_period
    ON institutional_filings(cik, report_period);

CREATE TABLE IF NOT EXISTS institutional_holdings (
    accession TEXT NOT NULL,
    cusip TEXT NOT NULL,
    class_title TEXT NOT NULL DEFAULT '',
    put_call TEXT NOT NULL DEFAULT 'none',
    issuer_name TEXT,
    figi TEXT,
    raw_value REAL,                    -- as filed (units resolved downstream)
    shares_or_principal REAL,
    share_principal_type TEXT,
    investment_discretion TEXT,
    voting_sole REAL,
    voting_shared REAL,
    voting_none REAL,
    other_managers TEXT,
    PRIMARY KEY (accession, cusip, class_title, put_call)
);
CREATE INDEX IF NOT EXISTS idx_holdings_accession
    ON institutional_holdings(accession);
"""


@dataclass(frozen=True)
class FilingRow:
    accession: str
    cik: str
    form_type: str
    filed_at: date
    report_period: date | None
    is_amendment: bool
    is_notice: bool
    content_hash: str | None
    is_effective: bool
    superseded_by: str | None
    holdings_count: int


class HoldingsStore:
    def __init__(self, db_path: str | Path = "data/institutional_intelligence.db",
                 *, clock_fn=None) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        import time
        self._clock = clock_fn or time.time
        with sqlite3.connect(self._path) as cx:
            cx.executescript(_DDL)

    # -- import ---------------------------------------------------------
    def import_filing(
        self,
        *,
        cik: str,
        accession: str,
        form_type: str,
        filed_at: date,
        report_period: date | None,
        is_amendment: bool,
        parsed: ParsedFiling,
        content_hash: str | None,
    ) -> str:
        """Import one filing + its holdings inside a transaction.

        Returns "inserted" | "unchanged" | "restated". Idempotent: re-importing
        the same accession with the same content_hash is a no-op ("unchanged").
        """
        with sqlite3.connect(self._path) as cx:
            cx.execute("BEGIN")
            try:
                existing = cx.execute(
                    "SELECT content_hash FROM institutional_filings WHERE accession=?",
                    (accession,),
                ).fetchone()
                if existing is not None and existing[0] == content_hash and content_hash is not None:
                    cx.execute("COMMIT")
                    return "unchanged"

                status = "restated" if existing is not None else "inserted"

                cx.execute("DELETE FROM institutional_holdings WHERE accession=?", (accession,))
                cx.execute(
                    "INSERT OR REPLACE INTO institutional_filings "
                    "(accession, cik, form_type, filed_at, report_period, is_amendment, "
                    " is_notice, content_hash, is_effective, superseded_by, holdings_count, "
                    " parse_warnings, ingested_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        accession, str(cik).zfill(10), form_type, filed_at.isoformat(),
                        report_period.isoformat() if report_period else None,
                        1 if is_amendment else 0, 1 if parsed.is_notice else 0,
                        content_hash, 1, None, parsed.holdings_count,
                        ",".join(parsed.parse_warnings), self._clock(),
                    ),
                )
                for h in parsed.holdings:
                    cx.execute(
                        "INSERT OR REPLACE INTO institutional_holdings "
                        "(accession, cusip, class_title, put_call, issuer_name, figi, "
                        " raw_value, shares_or_principal, share_principal_type, "
                        " investment_discretion, voting_sole, voting_shared, voting_none, "
                        " other_managers) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            accession, h.cusip, h.class_title, h.put_call, h.issuer_name,
                            h.figi, h.value, h.shares_or_principal, h.share_principal_type,
                            h.investment_discretion, h.voting_sole, h.voting_shared,
                            h.voting_none, ",".join(h.other_managers),
                        ),
                    )
                # Recompute effective flags for this (cik, report_period).
                self._recompute_effective(cx, str(cik).zfill(10), report_period)
                cx.execute("COMMIT")
                return status
            except Exception:
                cx.execute("ROLLBACK")
                raise

    def _recompute_effective(self, cx, cik: str, report_period: date | None) -> None:
        """Deterministically pick the effective filing for (cik, period).

        Effective = the filing with the latest ``filed_at`` (amendment wins once
        filed); ties broken by accession. All others are marked superseded_by
        the effective accession but retained for audit.
        """
        if report_period is None:
            return
        rows = cx.execute(
            "SELECT accession, filed_at FROM institutional_filings "
            "WHERE cik=? AND report_period=? ORDER BY filed_at DESC, accession DESC",
            (cik, report_period.isoformat()),
        ).fetchall()
        if not rows:
            return
        effective_accession = rows[0][0]
        for accession, _ in rows:
            if accession == effective_accession:
                cx.execute(
                    "UPDATE institutional_filings SET is_effective=1, superseded_by=NULL "
                    "WHERE accession=?", (accession,))
            else:
                cx.execute(
                    "UPDATE institutional_filings SET is_effective=0, superseded_by=? "
                    "WHERE accession=?", (effective_accession, accession))

    # -- queries --------------------------------------------------------
    def _row(self, r) -> FilingRow:
        return FilingRow(
            accession=r["accession"], cik=r["cik"], form_type=r["form_type"],
            filed_at=date.fromisoformat(r["filed_at"]),
            report_period=date.fromisoformat(r["report_period"]) if r["report_period"] else None,
            is_amendment=bool(r["is_amendment"]), is_notice=bool(r["is_notice"]),
            content_hash=r["content_hash"], is_effective=bool(r["is_effective"]),
            superseded_by=r["superseded_by"], holdings_count=r["holdings_count"],
        )

    def get_filing(self, accession: str) -> FilingRow | None:
        with sqlite3.connect(self._path) as cx:
            cx.row_factory = sqlite3.Row
            r = cx.execute("SELECT * FROM institutional_filings WHERE accession=?",
                           (accession,)).fetchone()
        return self._row(r) if r else None

    def all_filings(self, cik: str | None = None) -> list[FilingRow]:
        with sqlite3.connect(self._path) as cx:
            cx.row_factory = sqlite3.Row
            if cik is not None:
                rows = cx.execute("SELECT * FROM institutional_filings WHERE cik=? "
                                  "ORDER BY filed_at", (str(cik).zfill(10),)).fetchall()
            else:
                rows = cx.execute("SELECT * FROM institutional_filings ORDER BY filed_at").fetchall()
        return [self._row(r) for r in rows]

    def effective_filing_asof(self, cik: str, report_period: date,
                              as_of: date) -> FilingRow | None:
        """The effective filing for (cik, period) using ONLY filings publicly
        available on/before ``as_of`` (anti-look-ahead). The latest such
        ``filed_at`` wins — an amendment filed after ``as_of`` is invisible."""
        with sqlite3.connect(self._path) as cx:
            cx.row_factory = sqlite3.Row
            r = cx.execute(
                "SELECT * FROM institutional_filings WHERE cik=? AND report_period=? "
                "AND filed_at<=? ORDER BY filed_at DESC, accession DESC LIMIT 1",
                (str(cik).zfill(10), report_period.isoformat(), as_of.isoformat()),
            ).fetchone()
        return self._row(r) if r else None

    def holdings_for(self, accession: str) -> list[ParsedHolding]:
        with sqlite3.connect(self._path) as cx:
            cx.row_factory = sqlite3.Row
            rows = cx.execute("SELECT * FROM institutional_holdings WHERE accession=?",
                              (accession,)).fetchall()
        out: list[ParsedHolding] = []
        for r in rows:
            out.append(ParsedHolding(
                issuer_name=r["issuer_name"] or "", class_title=r["class_title"],
                cusip=r["cusip"], value=r["raw_value"],
                shares_or_principal=r["shares_or_principal"],
                share_principal_type=r["share_principal_type"],
                put_call=r["put_call"], figi=r["figi"],
                investment_discretion=r["investment_discretion"],
                voting_sole=r["voting_sole"], voting_shared=r["voting_shared"],
                voting_none=r["voting_none"],
                other_managers=tuple(x for x in (r["other_managers"] or "").split(",") if x),
            ))
        return out

    def effective_holdings_asof(self, cik: str, report_period: date,
                                as_of: date) -> list[ParsedHolding]:
        filing = self.effective_filing_asof(cik, report_period, as_of)
        return self.holdings_for(filing.accession) if filing else []

    def is_available_on(self, filing: FilingRow, as_of: date) -> bool:
        """A filing's signal is available only on/after its public filing date."""
        return as_of >= filing.filed_at
