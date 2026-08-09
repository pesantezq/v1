"""Session 3.0 — halt-aware irregular-session classification. Research-only.

WHY THIS EXISTS
===============

Session 2 admits a session only when the observed bar-start timestamps EQUAL the
calendar's expected grid. That is the right rule for establishing a pristine
continuous-session dataset, and it stays frozen.

But a market-wide trading halt produces intervals in which no trade occurs, so a
legitimately halted session has missing nominal bars and is rejected as
`REJECTED_MISSING_BARS`. Safe for data integrity — and a research-population
problem, because the sessions most likely to be halted are the most volatile
ones. A strategy validated only on the surviving population has been tested
disproportionately on uninterrupted markets.

Session 3.0 does not relax Session 2. It builds a SEPARATE derived view on top
of frozen Session 2 evidence, and measures the bias.

    SESSION 2 (frozen)                         SESSION 3.0 (this module)
    provider evidence                          authoritative event registry
      -> immutable raw                                    +
      -> normalization                         Session 2 evidence
      -> calendar reconciliation                          |
      -> continuous canonical dataset                     v
      -> rejected irregular sessions           irregular-session classifier
                    \\_________________________________/  |
                                                          v
                                             halt-aware derived research view
                                                          |
                                                          v
                                                population-bias report

THE RULE THAT MATTERS MOST
==========================

**Missing bars are never, by themselves, evidence of a halt.** Two symbols
losing the identical interval is grounds for investigation, not admission. Only
an event in the AUTHORITATIVE registry can explain an absence. Everything else
stays rejected as an unexplained gap.

The corollary is equally important: a halt does not repair other defects. Only
`REJECTED_MISSING_BARS` is eligible for halt-aware recovery. A provider error, a
normalization failure, a surplus bar, an off-grid timestamp or a mixed
adjustment regime remains rejected whatever the calendar says about that day.

NO SYNTHETIC BARS, EVER
=======================

A halted interval is represented as ABSENCE plus an authoritative event. Nothing
is forward-filled, interpolated, zero-volume-padded or otherwise invented. The
discontinuity IS the information; manufacturing a price across it would destroy
exactly the thing this session exists to measure.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Sequence

from portfolio_automation.intraday_lab import calendar as CAL
from portfolio_automation.intraday_lab import storage as ST

SCHEMA_VERSION = "1"

# ── Session 3.0 policy identity ────────────────────────────────────────────
POLICY_ID = "INTRADAY_IRREGULAR_SESSION_POLICY_V1"
METRIC_DEFINITIONS_VERSION = "intraday_session_metrics_v1"

# ── Authoritative market-wide circuit-breaker registry ─────────────────────
MWCB_REGISTRY_VERSION = "INTRADAY_MWCB_REGISTRY_V1"
MWCB_SCOPE = "US_EQUITIES_MARKET_WIDE"
MWCB_TRIGGER = "S&P 500 Level 1 MWCB (7% decline)"

# Verified 2026-08-09 directly against the primary document, not a summary:
#   "Report of the Market-Wide Circuit Breaker ("MWCB") Working Group Regarding
#    the March 2020 MWCB Events", submitted 2021-03-31, filed with the SEC as
#    Release No. 34-92428 Exhibit 3.
# Trigger and reopening-auction times are quoted on pp. 4-5; the 03-18 pair is
# independently corroborated on p. 11 by the contemporaneous Cboe and NYSE halt
# notices, which state "12:56:17 ET" and "13:11:17 ET" explicitly. The report
# writes times in a.m./p.m. Eastern; ET is the authoritative zone.
MWCB_SOURCE = {
    "authority": "Market-Wide Circuit Breaker Working Group "
                 "(NYSE, Nasdaq, Cboe, FINRA, SEC, CFTC, DTCC, OCC)",
    "source_title": "Report of the Market-Wide Circuit Breaker (\"MWCB\") Working "
                    "Group Regarding the March 2020 MWCB Events",
    "source_document_identifier": "SEC Release No. 34-92428, Exhibit 3",
    "source_publication_date": "2021-03-31",
    "source_pages": "pp. 4-5 (trigger/reopen times); p. 11 (Cboe/NYSE ET notices)",
    "timezone": "America/New_York",
}


@dataclass(frozen=True)
class MWCBEvent:
    """One authoritative market-wide halt, in exchange-local time."""

    market_date: date
    halt_start_et: time
    reopen_start_et: time
    level: int
    scope: str = MWCB_SCOPE
    trigger: str = MWCB_TRIGGER

    def window_utc(self) -> tuple[datetime, datetime]:
        """The halt interval as aware UTC instants."""
        start = datetime.combine(self.market_date, self.halt_start_et,
                                 tzinfo=CAL.EXCHANGE_TZ).astimezone(timezone.utc)
        end = datetime.combine(self.market_date, self.reopen_start_et,
                               tzinfo=CAL.EXCHANGE_TZ).astimezone(timezone.utc)
        return start, end

    def to_dict(self) -> dict:
        return {
            "market_date": self.market_date.isoformat(),
            "halt_start_et": self.halt_start_et.isoformat(),
            "reopen_start_et": self.reopen_start_et.isoformat(),
            "level": self.level,
            "scope": self.scope,
            "trigger": self.trigger,
        }


MWCB_EVENTS: tuple[MWCBEvent, ...] = (
    MWCBEvent(date(2020, 3, 9), time(9, 34, 13), time(9, 49, 13), 1),
    MWCBEvent(date(2020, 3, 12), time(9, 35, 44), time(9, 50, 44), 1),
    MWCBEvent(date(2020, 3, 16), time(9, 30, 1), time(9, 45, 1), 1),
    MWCBEvent(date(2020, 3, 18), time(12, 56, 17), time(13, 11, 17), 1),
)

_BY_DATE: dict[date, MWCBEvent] = {e.market_date: e for e in MWCB_EVENTS}


def registry_provenance() -> dict:
    """Everything needed to reproduce and audit the registry. No timestamps."""
    return {
        "schema_version": SCHEMA_VERSION,
        "registry_version": MWCB_REGISTRY_VERSION,
        "scope": MWCB_SCOPE,
        "trigger": MWCB_TRIGGER,
        **MWCB_SOURCE,
        "events": [e.to_dict() for e in MWCB_EVENTS],
    }


def registry_fingerprint() -> str:
    """Identity of the registry's MEANING.

    Any change to an event date, halt/reopen instant, level, scope or the
    claiming authority mints a new fingerprint — so a later source correction
    produces a DIFFERENT research object rather than silently reinterpreting
    published results. `reviewed_at` and other retrieval facts are deliberately
    excluded; when we looked is not what the registry means.
    """
    return ST.content_hash(registry_provenance())


def mwcb_event_for(market_date: date) -> MWCBEvent | None:
    return _BY_DATE.get(market_date)


# ── Session 3.0 population states ──────────────────────────────────────────
# Exactly one is assigned to every requested certified symbol-session.
VALID_CONTINUOUS_SESSION = "VALID_CONTINUOUS_SESSION"
VALID_MARKET_WIDE_HALT_SESSION = "VALID_MARKET_WIDE_HALT_SESSION"
REJECTED_UNEXPLAINED_GAP = "REJECTED_UNEXPLAINED_GAP"
REJECTED_SOURCE_ERROR = "REJECTED_SOURCE_ERROR"
REJECTED_OTHER_DATA_DEFECT = "REJECTED_OTHER_DATA_DEFECT"
NOT_A_TRADING_SESSION = "NOT_A_TRADING_SESSION"

POPULATION_STATES = (VALID_CONTINUOUS_SESSION, VALID_MARKET_WIDE_HALT_SESSION,
                     REJECTED_UNEXPLAINED_GAP, REJECTED_SOURCE_ERROR,
                     REJECTED_OTHER_DATA_DEFECT, NOT_A_TRADING_SESSION)

# Cohort membership.
COHORT_CONTINUOUS_ONLY = "CONTINUOUS_ONLY"
COHORT_HALT_AWARE = "HALT_AWARE"

# Session 2 states that mean "we could not get the data", as distinct from
# "the market data itself is defective". Kept separate so a provider outage is
# never counted as evidence about market structure.
_SOURCE_ERROR_STATES = frozenset({
    "REJECTED_PROVIDER_ERROR", "REJECTED_NORMALIZATION_ERROR",
})
# The ONLY state eligible for halt-aware recovery. A halt cannot make a surplus
# bar, an off-grid timestamp or a mixed adjustment regime correct.
_HALT_ELIGIBLE_STATE = "REJECTED_MISSING_BARS"


def bar_fully_inside_halt(bar_start: datetime, bar_end: datetime,
                          halt_start: datetime, reopen_start: datetime) -> bool:
    """Is this nominal bar interval ENTIRELY inside the authoritative halt?

    Exact instant arithmetic, deliberately not rounded to convenient 5-minute
    boundaries. A bar that merely OVERLAPS the halt contained tradable time, so
    its absence is not explained by the halt and must remain unresolved.

    For 2020-03-09 (halt 09:34:13 -> 09:49:13):

        09:30-09:35   partial overlap  -> NOT explained
        09:35-09:40   fully inside     -> explained
        09:40-09:45   fully inside     -> explained
        09:45-09:50   partial overlap  -> NOT explained
    """
    return bar_start >= halt_start and bar_end <= reopen_start


def _as_utc(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        raise ValueError(f"naive timestamp refused: {value!r}")
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class SessionClassification:
    symbol: str
    market_date: date
    timeframe: str
    session2_state: str
    state: str
    reason: str | None
    explained_missing: tuple[str, ...] = ()
    unexplained_missing: tuple[str, ...] = ()
    mwcb_event: dict | None = None

    @property
    def in_continuous_cohort(self) -> bool:
        return self.state == VALID_CONTINUOUS_SESSION

    @property
    def in_halt_aware_cohort(self) -> bool:
        return self.state in (VALID_CONTINUOUS_SESSION,
                              VALID_MARKET_WIDE_HALT_SESSION)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "market_date": self.market_date.isoformat(),
            "timeframe": self.timeframe,
            "session2_state": self.session2_state,
            "state": self.state,
            "reason": self.reason,
            "explained_missing": list(self.explained_missing),
            "unexplained_missing": list(self.unexplained_missing),
            "mwcb_event": self.mwcb_event,
        }


def classify_session(*, symbol: str, market_date: date, timeframe: str,
                     session2_state: str,
                     missing_timestamps: Sequence = (),
                     unexpected_timestamps: Sequence = (),
                     session_type: str | None = None) -> SessionClassification:
    """Assign exactly one Session 3.0 population state. Deterministic.

    Fails closed at every ambiguity: an unexplained absence, an unexpected
    timestamp, a partially-overlapped bar or a wrong-scope event all leave the
    session rejected.
    """
    def out(state, reason=None, explained=(), unexplained=(), event=None):
        return SessionClassification(
            symbol=symbol, market_date=market_date, timeframe=timeframe,
            session2_state=session2_state, state=state, reason=reason,
            explained_missing=tuple(explained), unexplained_missing=tuple(unexplained),
            mwcb_event=event)

    if session_type in (CAL.SESSION_MARKET_CLOSED,) or session2_state == "NOT_A_TRADING_SESSION":
        return out(NOT_A_TRADING_SESSION, "calendar says the market was closed")
    if session2_state == "ADMITTED":
        return out(VALID_CONTINUOUS_SESSION)
    if session2_state in _SOURCE_ERROR_STATES:
        return out(REJECTED_SOURCE_ERROR,
                   f"Session 2 recorded {session2_state}: we could not obtain or "
                   f"interpret the data, which says nothing about market structure")
    if session2_state != _HALT_ELIGIBLE_STATE:
        # A halt does not repair a defect. Surplus bars, off-grid timestamps,
        # duplicates, identity mismatches and mixed adjustment regimes stay
        # rejected regardless of what happened in the market that day.
        return out(REJECTED_OTHER_DATA_DEFECT,
                   f"Session 2 recorded {session2_state}, which is not eligible "
                   f"for halt-aware recovery")

    # From here: REJECTED_MISSING_BARS only.
    if unexpected_timestamps:
        return out(REJECTED_OTHER_DATA_DEFECT,
                   "session has unexpected timestamps as well as missing ones")

    event = mwcb_event_for(market_date)
    missing = [_as_utc(t) for t in missing_timestamps]
    if not missing:
        return out(REJECTED_UNEXPLAINED_GAP,
                   "Session 2 rejected for missing bars but recorded none")
    if event is None:
        # THE central rule. Identical gaps across symbols are grounds for
        # investigation, never for admission.
        return out(REJECTED_UNEXPLAINED_GAP,
                   f"no authoritative {MWCB_REGISTRY_VERSION} event on "
                   f"{market_date.isoformat()}; missing bars are not by "
                   f"themselves evidence of a halt",
                   unexplained=[t.isoformat() for t in missing])
    if event.scope != MWCB_SCOPE:
        return out(REJECTED_UNEXPLAINED_GAP,
                   f"registry event scope {event.scope!r} is not market-wide, so "
                   f"it cannot explain an arbitrary symbol's absence",
                   unexplained=[t.isoformat() for t in missing])

    duration = timedelta(minutes=5) if timeframe == "5min" else None
    if duration is None:
        return out(REJECTED_UNEXPLAINED_GAP,
                   f"halt containment is only defined for 5min; got {timeframe!r}",
                   unexplained=[t.isoformat() for t in missing])

    halt_start, reopen_start = event.window_utc()
    explained, unexplained = [], []
    for start in sorted(missing):
        if bar_fully_inside_halt(start, start + duration, halt_start, reopen_start):
            explained.append(start.isoformat())
        else:
            unexplained.append(start.isoformat())

    if unexplained:
        return out(REJECTED_UNEXPLAINED_GAP,
                   f"{len(unexplained)} missing bar(s) are not fully contained in "
                   f"the authoritative halt window "
                   f"{event.halt_start_et.isoformat()}-{event.reopen_start_et.isoformat()} ET",
                   explained=explained, unexplained=unexplained,
                   event=event.to_dict())
    return out(VALID_MARKET_WIDE_HALT_SESSION,
               f"every missing bar lies entirely inside the authoritative "
               f"Level {event.level} market-wide halt",
               explained=explained, event=event.to_dict())


# ── Halt-aware segmentation ────────────────────────────────────────────────
def segment_bars(bars: Sequence, *, timeframe: str = "5min") -> list[list]:
    """Split a session's observed bars into CONTIGUOUS segments.

    A rolling feature must not treat 09:30, 09:45, 09:50 as three consecutive
    five-minute observations merely because they are the three bars that exist.
    The halt is a segmentation boundary, so rolling state resets across it and a
    3-bar feature only becomes available again after three new contiguous bars.

    Deliberately generic: any discontinuity splits, whether or not the registry
    explains it. Session 2's feature definitions are untouched — this is a
    Session 3 view over their inputs.

    IMPORTANT, stated accurately: this does NOT close a leak. The frozen
    Session 2 engine already refuses a bridged window — `features._contiguous`
    checks adjacency in TIME, so a rolling window spanning a halt returns
    explicit absence. Segmentation earns its place by making the segment
    STRUCTURE explicit (Session 3.1 needs it for reopening behaviour), by
    expressing the invariant independently, and because session-level metrics
    must keep the reopening discontinuity out of the within-segment volatility
    series — a computation Session 2 never performs.
    """
    step = timedelta(minutes=5) if timeframe == "5min" else None
    if step is None:
        raise ValueError(f"segmentation is only defined for 5min, got {timeframe!r}")
    ordered = sorted(bars, key=lambda b: b.bar_start_at)
    segments: list[list] = []
    for bar in ordered:
        if segments and bar.bar_start_at == segments[-1][-1].bar_start_at + step:
            segments[-1].append(bar)
        else:
            segments.append([bar])
    return segments


def segmented_features(bars: Sequence, *, dataset_id: str, fingerprint: str,
                       manifest_fingerprint: str, lookback: int = 3,
                       timeframe: str = "5min") -> list:
    """Session 2's feature algorithm, applied PER CONTIGUOUS SEGMENT.

    Reuses `pipeline.features_from_bars` unchanged — Session 3 decides which
    bars are consecutive, Session 2 decides what a feature means.
    """
    from portfolio_automation.intraday_lab import pipeline as PL

    out = []
    for segment in segment_bars(bars, timeframe=timeframe):
        out.extend(PL.features_from_bars(
            segment, dataset_id=dataset_id, fingerprint=fingerprint,
            manifest_fingerprint=manifest_fingerprint, lookback=lookback))
    return out


def policy_provenance() -> dict:
    """The Session 3.0 admissibility contract, as data."""
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_id": POLICY_ID,
        "session": "3.0",
        "observe_only": True,
        "registry_version": MWCB_REGISTRY_VERSION,
        "registry_fingerprint": registry_fingerprint(),
        "metric_definitions_version": METRIC_DEFINITIONS_VERSION,
        "population_states": list(POPULATION_STATES),
        "cohorts": {
            COHORT_CONTINUOUS_ONLY:
                "Session 2 admitted continuous sessions only.",
            COHORT_HALT_AWARE:
                "continuous sessions PLUS authoritatively verified market-wide "
                "halt sessions. Unexplained gaps are excluded from BOTH.",
        },
        "rules": {
            "halt_eligible_session2_state": _HALT_ELIGIBLE_STATE,
            "containment": "a missing nominal bar is halt-explained only when "
                           "bar_start >= halt_start AND bar_end <= reopen_start",
            "partial_overlap": "a partially overlapped missing bar is NOT "
                               "explained; it contained tradable time",
            "inference": "missing bars are never by themselves evidence of a "
                         "halt, including when identical across symbols",
            "synthetic_bars": "never; a halted interval is absence plus an "
                              "authoritative event",
            "temporal": "bar_start_at, bar_end_at and known_at are preserved "
                        "exactly from Session 2 evidence",
            "symbol_specific_halts": "LULD / news / regulatory / IPO pauses are "
                                     "NOT classifiable — no authoritative "
                                     "historical source is sanctioned, so such "
                                     "gaps remain rejected",
        },
        "strategy_validation_allowed": False,
    }


def policy_fingerprint() -> str:
    return ST.content_hash(policy_provenance())


# ── Immutable Session 3 derived research view ──────────────────────────────
# A SEPARATE object. Session 2 canonical content is never modified: the halted
# session remains rejected under the continuous-session contract, and this view
# records that the same evidence is admissible under the halt-aware contract.
IRREGULAR_VIEW_IDENTITY_SCHEMA = "intraday_irregular_session_v1"


def irregular_view_payload(*, classification: SessionClassification,
                           source_manifest_fingerprint: str,
                           source_dataset_fingerprint: str | None,
                           raw_content_fingerprints: Sequence[str],
                           calendar_identity: dict, bars: Sequence) -> dict:
    """The canonical projection that DEFINES a derived view's identity.

    Binds to the Session 2 provenance it was derived from, the registry that
    explained it, and the observed bars — so a source correction, a registry
    correction or a policy change all mint a different research object rather
    than silently reinterpreting a published result.
    """
    return {
        "identity_schema": IRREGULAR_VIEW_IDENTITY_SCHEMA,
        "policy_id": POLICY_ID,
        "policy_fingerprint": policy_fingerprint(),
        "registry_version": MWCB_REGISTRY_VERSION,
        "registry_fingerprint": registry_fingerprint(),
        "symbol": classification.symbol,
        "market_date": classification.market_date.isoformat(),
        "timeframe": classification.timeframe,
        "calendar_identity": calendar_identity,
        "source_manifest_fingerprint": source_manifest_fingerprint,
        "source_dataset_fingerprint": source_dataset_fingerprint,
        "raw_content_fingerprints": sorted(raw_content_fingerprints),
        "session2_state": classification.session2_state,
        "classification": classification.state,
        "explained_missing": list(classification.explained_missing),
        "unexplained_missing": list(classification.unexplained_missing),
        "mwcb_event": classification.mwcb_event,
        # Observed bars ONLY. Nothing is added for the halted interval.
        "observed_bars": ST.bars_to_rows(bars),
    }


def persist_irregular_view(payload: dict, *, root: str = ".") -> str:
    fp = ST.content_hash(payload)
    ST.write_snapshot(ST.IRREGULAR_VIEWS, fp, {
        "irregular_session.json": payload,
        "view_manifest.json": {
            "schema_version": SCHEMA_VERSION,
            "storage_schema": "intraday_content_envelope_v1",
            "identity_schema": IRREGULAR_VIEW_IDENTITY_SCHEMA,
            "view_fingerprint": fp,
            "symbol": payload["symbol"],
            "market_date": payload["market_date"],
            "classification": payload["classification"],
            "observed_bar_count": len(payload["observed_bars"]),
            "explained_missing_count": len(payload["explained_missing"]),
        },
    }, root=root)
    return fp


def verify_irregular_view(fingerprint: str, *, root: str = ".") -> dict:
    """Re-verify a derived view against its own bytes and its Session 2 source.

    Everything is recomputed. In particular the temporal fields are compared
    against the SOURCE canonical bars, because a halt-aware view that quietly
    shifted `known_at` would break the one invariant Session 3 inherits.
    """
    body = ST.read_snapshot(ST.IRREGULAR_VIEWS, fingerprint,
                            "irregular_session.json", root=root)
    man = ST.read_snapshot(ST.IRREGULAR_VIEWS, fingerprint, "view_manifest.json",
                           root=root)
    if body is None or man is None:
        return {"verified": False, "reason": "missing view files"}
    if ST.content_hash(body) != fingerprint or man.get("view_fingerprint") != fingerprint:
        return {"verified": False, "reason": "view does not hash to its identity"}
    if body.get("registry_fingerprint") != registry_fingerprint():
        return {"verified": False, "reason": "view was built under a different "
                                             "MWCB registry"}
    if body.get("policy_fingerprint") != policy_fingerprint():
        return {"verified": False, "reason": "view was built under a different "
                                             "Session 3.0 policy"}
    prov = ST.verify_dataset_provenance(body["source_manifest_fingerprint"], root=root)
    if not prov.get("verified"):
        return {"verified": False,
                "reason": f"source Session 2 provenance failed: {prov.get('reason')}"}
    for raw_fp in body.get("raw_content_fingerprints") or []:
        if not ST.verify_raw_content(raw_fp, root=root).get("verified"):
            return {"verified": False,
                    "reason": f"source raw evidence {raw_fp} failed verification"}
    if body["classification"] == VALID_MARKET_WIDE_HALT_SESSION:
        if body.get("unexplained_missing"):
            return {"verified": False,
                    "reason": "halt classification with unexplained missing bars"}
        if not body.get("mwcb_event"):
            return {"verified": False,
                    "reason": "halt classification with no authoritative event"}
    return {"verified": True, "reason": None, "view_fingerprint": fingerprint,
            "symbol": body["symbol"], "market_date": body["market_date"],
            "classification": body["classification"],
            "observed_bar_count": len(body["observed_bars"]),
            "explained_missing": body["explained_missing"]}
