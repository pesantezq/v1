"""Canonical EvidenceSnapshots for the dividend-adjusted bar panel.

Kept separate from the consumer on purpose: the consumer PARSES and refuses;
this module maps parsed bars into the canonical Northstar evidence plane and
runs them through the EXISTING EvidenceGateway. No gateway rule is duplicated
here, and none is weakened.

THE TWO PIT TRUTHS (operator ruling B1), never conflated:

1. POSSESSION. Every snapshot's ``known_at`` is its ``retrieved_at`` under the
   one sanctioned conservative derivation
   (``PointInTime.with_conservative_known_at``). A backfilled bar retrieved
   today is therefore REFUSED by the gateway for any ``as_of`` before
   retrieval. That refusal is the gateway working, and
   :func:`lookahead_probe` records it as evidence in the VS-001 refusal-ledger
   style rather than treating it as an obstacle.

2. STRUCTURAL WINDOW SAFETY. Whether a bar may enter a beta estimation window
   is a SESSION-DATE question — strictly before the signal date — answered by
   :func:`beta_window_bars` on the dates the sessions actually carry. No
   historical ``known_at`` is fabricated to make backfilled bars "admissible"
   at historical instants, and the gateway's ``known_at <= as_of`` rule is
   never bypassed to get there.

``experimental_noncanonical`` (the package); the snapshots themselves are
canonical 0B contracts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from portfolio_automation.evidence_gateway.admission import AdmissionDecision, admit
from portfolio_automation.northstar.evidence import EvidenceRef, EvidenceSnapshot
from portfolio_automation.northstar.pit import PointInTime
from portfolio_automation.northstar.provenance import Provenance

from portfolio_automation.vs002_evidence import contracts as C

#: ETFs in the frozen universe; everything else is a single name. Entity typing
#: is descriptive metadata for the canonical snapshot, not a behavioural fork.
_ETF_SYMBOLS = frozenset({"SPY", "QQQ", "IWM", "XLE", "XLF", "XLK"})


def _entity_type(symbol: str) -> str:
    return "etf" if symbol.upper() in _ETF_SYMBOLS else "symbol"


def bar_snapshot(bar: Mapping[str, Any], *, retrieved_at: datetime,
                 raw_response_digest: str) -> EvidenceSnapshot:
    """One bar -> one canonical EvidenceSnapshot.

    ``observed_at``/``published_at`` are deliberately ABSENT: the provider does
    not report when the bar occurred as a timestamp or when it became public,
    and the kernel's no-fabricated-time rule means absence is represented, not
    invented. ``known_at`` is derived via the ONE sanctioned conservative rule.
    """
    descriptor = C.data_source_descriptor()
    pit = PointInTime(retrieved_at=retrieved_at).with_conservative_known_at()
    provenance = Provenance(
        producer_id="adapter.fmp_historical_price_eod_dividend_adjusted",
        producer_type="source_adapter",
        recorded_at=retrieved_at,
        source_id=descriptor.source_id,
        transformation_id=(
            f"normalize_bars:{C.SOURCE_DATASET}:{raw_response_digest}"),
    )
    return EvidenceSnapshot(
        source_id=descriptor.source_id,
        entity_id=str(bar["symbol"]).upper(),
        entity_type=_entity_type(str(bar["symbol"])),
        evidence_type=C.EVIDENCE_TYPE_BAR,
        pit=pit,
        provenance=provenance,
        payload={
            "symbol": str(bar["symbol"]).upper(),
            "session_date": str(bar["session_date"]),
            "close": float(bar["close"]),
            "adj_close": float(bar["adj_close"]),
            "volume": int(bar["volume"]),
        },
    )


def panel_snapshots(bars: Sequence[Mapping[str, Any]], *,
                    retrieved_at: datetime,
                    raw_digests: Mapping[str, str]) -> list[EvidenceSnapshot]:
    """Snapshots for a whole validated panel, deterministic order."""
    ordered = sorted(bars, key=lambda b: (str(b["symbol"]), str(b["session_date"])))
    return [
        bar_snapshot(
            b, retrieved_at=retrieved_at,
            raw_response_digest=str(raw_digests.get(str(b["symbol"]), "")))
        for b in ordered
    ]


def panel_refs(snapshots: Iterable[EvidenceSnapshot]) -> list[EvidenceRef]:
    """The refs an ExperimentSpec embeds — identity plus integrity, no copies."""
    return [s.ref() for s in snapshots]


def admit_panel(snapshots: Sequence[EvidenceSnapshot], as_of: datetime,
                ) -> list[AdmissionDecision]:
    """Every snapshot through the EXISTING gateway. Nothing added, nothing
    bypassed: refusals come back as decisions, exactly as the gateway made
    them."""
    return [admit(s, as_of, ref=s.ref()) for s in snapshots]


@dataclass(frozen=True)
class LookaheadProbe:
    """The refusal ledger: proof the possession-PIT rule held, in the VS-001
    style (440/440 refused). ``passed`` is True only when EVERY snapshot was
    refused at an ``as_of`` strictly before its ``known_at`` — a single
    admission would mean the gateway leaked."""

    as_of: str
    total: int
    refused: int

    @property
    def passed(self) -> bool:
        return self.total > 0 and self.refused == self.total

    def to_dict(self) -> dict[str, Any]:
        return {"as_of": self.as_of, "total": self.total,
                "refused": self.refused, "passed": self.passed}


def lookahead_probe(snapshots: Sequence[EvidenceSnapshot],
                    as_of: datetime) -> LookaheadProbe:
    """Assert the gateway refuses possession-lookahead. The caller supplies an
    ``as_of`` known to precede retrieval (e.g. the earliest signal time); every
    backfilled snapshot must come back refused."""
    decisions = admit_panel(snapshots, as_of)
    refused = sum(1 for d in decisions if not d.admitted)
    return LookaheadProbe(as_of=as_of.isoformat(), total=len(decisions),
                          refused=refused)


def beta_window_bars(bars: Sequence[Mapping[str, Any]], *,
                     signal_date: str) -> list[Mapping[str, Any]]:
    """STRUCTURAL window safety: sessions STRICTLY before the signal date.

    The signal-date bar itself is excluded — contemporaneous evidence is the
    leak the frozen design names — and so is everything after it. Callers take
    the trailing ``MIN_PRIOR_SESSIONS`` of what this returns.
    """
    boundary = str(signal_date)[:10]
    return [b for b in bars if str(b["session_date"]) < boundary]
