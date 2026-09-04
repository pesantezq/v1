"""Adapt recorded signal outcomes into Northstar EvidenceSnapshots.

THE ONE THING THIS MODULE EXISTS TO GET RIGHT.

Each CSV row secretly contains two facts that became knowable at DIFFERENT
times: what the scanner saw (known at ``signal_time``) and what happened next
(known at ``evaluated_at_7d``). Loading the row as a single record would make
the outcome look knowable at signal time, which is precisely the leak the
Evidence Plane exists to prevent -- and it would leak silently, because the
resulting numbers look plausible.

So one row becomes TWO snapshots with two different ``known_at`` values, and
the outcome snapshot is refused by the gateway at any ``as_of`` before its
resolution instant. The refusal is machine-checked, not asserted in a comment.

``experimental_noncanonical``.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from portfolio_automation.northstar.evidence import EvidenceSnapshot
from portfolio_automation.northstar.pit import PointInTime
from portfolio_automation.northstar.provenance import (
    PRODUCER_SOURCE_ADAPTER, Provenance)
from portfolio_automation.northstar.sources import DataSourceDescriptor

SIGNAL_EVIDENCE_TYPE = "watchlist_signal"
OUTCOME_EVIDENCE_TYPE = "signal_outcome_7d"

#: ETFs in the recorded watchlist. Entity type is part of snapshot identity, so
#: it is derived from a declared set rather than guessed per row.
_ETF_TICKERS = frozenset({"SPY", "QQQ", "IWM", "XLE", "XLF", "XLK", "CHAT", "NASA"})

ADAPTER_ID = "vertical_slice.signal_outcomes_adapter"
ADAPTER_VERSION = "v1"


def source_descriptor() -> DataSourceDescriptor:
    """The recorded scanner output, described honestly.

    ``pit_capability='reconstructable'`` and not ``native_pit``: the file does
    not ship a point-in-time index, but every row carries the instants needed to
    rebuild one, which is a weaker and more accurate claim."""
    return DataSourceDescriptor(
        provider="stockbot",
        dataset="watchlist_signal_outcomes",
        source_type="market_data",
        access_class="file",
        rights_class="internal_use",
        cost_class="free",
        pit_capability="reconstructable",
        historical_capability="limited",
        status="active",
        notes=("Recorded live by watchlist_scanner.performance_feedback. Signal "
               "and outcome instants are separate fields, so point-in-time "
               "structure is reconstructable from the file itself."))


def _parse_ts(raw: str) -> Optional[datetime]:
    """Timestamps are naive in the file but are UTC by upstream convention.

    Attaching UTC here is a declared adapter decision, not a silent coercion:
    the gateway refuses naive datetimes outright, so the choice has to be made
    somewhere and is made once, visibly, at the boundary."""
    text = (raw or "").strip()
    if not text:
        return None
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def _entity_type(ticker: str) -> str:
    return "etf" if ticker in _ETF_TICKERS else "company"


@dataclass(frozen=True)
class RowEvidence:
    """The two snapshots one CSV row really contains, plus the row's keys."""

    ticker: str
    scan_time: datetime
    signal: EvidenceSnapshot
    outcome: Optional[EvidenceSnapshot]
    outcome_known_at: Optional[datetime]


def _provenance(recorded_at: datetime, source_id: str) -> Provenance:
    return Provenance(
        producer_id=ADAPTER_ID,
        producer_type=PRODUCER_SOURCE_ADAPTER,
        recorded_at=recorded_at,
        code_version=ADAPTER_VERSION,
        source_id=source_id,
    )


def load_rows(csv_path: Path) -> list[dict[str, str]]:
    """utf-8-sig: the file carries a BOM, and reading it as plain utf-8 turns
    the first column name into '\\ufeffticker'."""
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def build_row_evidence(row: dict[str, str], *, descriptor: DataSourceDescriptor
                       ) -> Optional[RowEvidence]:
    """Two snapshots from one row. Returns None if the row has no usable signal."""
    ticker = (row.get("ticker") or "").strip()
    scan_time = _parse_ts(row.get("signal_time", ""))
    if not ticker or scan_time is None:
        return None

    source_id = descriptor.source_id
    entity_type = _entity_type(ticker)

    signal = EvidenceSnapshot(
        source_id=source_id,
        entity_id=ticker,
        entity_type=entity_type,
        evidence_type=SIGNAL_EVIDENCE_TYPE,
        pit=PointInTime(observed_at=scan_time, known_at=scan_time,
                        known_at_basis="source_reported"),
        provenance=_provenance(scan_time, source_id),
        payload={
            "ticker": ticker,
            "scan_time": scan_time.isoformat(),
            "signal_score": _float_or_none(row.get("signal_score")),
            "price_at_signal": _float_or_none(row.get("price_at_signal")),
            "prediction_intent": (row.get("prediction_intent") or "").strip(),
        },
    )

    outcome_known_at = _parse_ts(row.get("evaluated_at_7d", ""))
    ret = _float_or_none(row.get("outcome_return_7d"))
    outcome = None
    if outcome_known_at is not None and ret is not None:
        outcome = EvidenceSnapshot(
            source_id=source_id,
            entity_id=ticker,
            entity_type=entity_type,
            evidence_type=OUTCOME_EVIDENCE_TYPE,
            pit=PointInTime(observed_at=outcome_known_at,
                            known_at=outcome_known_at,
                            known_at_basis="source_reported"),
            provenance=_provenance(outcome_known_at, source_id),
            payload={
                "ticker": ticker,
                "scan_time": scan_time.isoformat(),
                "outcome_return_7d_pct": ret,
                "evaluated_at": outcome_known_at.isoformat(),
            },
        )
    return RowEvidence(ticker=ticker, scan_time=scan_time, signal=signal,
                       outcome=outcome, outcome_known_at=outcome_known_at)


def _float_or_none(raw: Any) -> Optional[float]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def iter_row_evidence(csv_path: Path) -> Iterator[RowEvidence]:
    descriptor = source_descriptor()
    for row in load_rows(csv_path):
        built = build_row_evidence(row, descriptor=descriptor)
        if built is not None:
            yield built
