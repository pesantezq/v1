"""Session 3.0 — research-population audit and selection-bias measurement.

The graduation pilot deliberately oversampled March 2020, so its rejection rate
is NOT a population prevalence estimate. This module measures the real one over
SPY and AAPL across the certified calendar window, assigns every requested
certified symbol-session exactly one population state, and compares the two
research cohorts on descriptive market statistics.

WHAT IT DOES NOT DO
===================

No strategy, no signal, no entry/exit, no fill, no P&L. The metrics here are
descriptive properties of a market session — the kind of thing you compute to
decide WHICH SESSIONS a future strategy should be judged on, before any strategy
exists. Four historical MWCB dates are important tail observations, not a sample
supporting inferential claims, and nothing here reports significance.

ONE ACCOUNTING RULE
===================

    sum(population states) == requested certified trading symbol-sessions

A year whose acquisition failed is reported as REJECTED_SOURCE_ERROR and stays
in the denominator. Silently dropping it would flatter every rate on the page.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from portfolio_automation.intraday_lab import calendar as CAL
from portfolio_automation.intraday_lab import irregular_sessions as IR
from portfolio_automation.intraday_lab import pipeline as PL
from portfolio_automation.intraday_lab import storage as ST
from portfolio_automation.intraday_lab.dataset import (
    DatasetRequest, build_canonical_dataset, _calendar_identity,
)

SCHEMA_VERSION = "1"
AUDIT_UNIVERSE = ("AAPL", "SPY")
AUDIT_START = date(2017, 1, 1)
AUDIT_END = date(2026, 8, 7)


# MEASURED provider constraint, not an assumption. `/stable/historical-chart/5min`
# returns at most ~432 rows (~6 regular sessions) for ANY requested window, always
# the tail ending at the window end. Verified 2026-08-09: a full-year, a quarter
# and a month request all returned the identical 6 days.
#
# This is why the audit is a STRATIFIED SAMPLE and not a census. A census of
# 2017..2026 for two symbols is ~2,412 sessions per symbol, i.e. ~800+ provider
# calls, far outside the registered 40-call intraday_research budget. Requesting
# that budget is an operator decision, not one this audit may take for itself.
#
# The consequence matters more than the number: a wide request SILENTLY returns
# only the tail, and the frozen Session 2 chain then records every un-returned
# day as REJECTED_MISSING_BARS. That output is CORRECT — the pipeline invented
# nothing — but reading it as market structure would manufacture exactly the
# fabricated gap evidence this session exists to prevent.
PROVIDER_MAX_SESSIONS_PER_CALL = 6
SAMPLE_WINDOW_SESSIONS = 5


def yearly_chunks(start: date = AUDIT_START, end: date = AUDIT_END
                  ) -> list[tuple[int, date, date]]:
    """Deterministic year chunks. Retained for callers that want whole years.

    NOT used by the sampled audit: see PROVIDER_MAX_SESSIONS_PER_CALL.
    """
    out = []
    for year in range(start.year, end.year + 1):
        lo = max(start, date(year, 1, 1))
        hi = min(end, date(year, 12, 31))
        if lo <= hi:
            out.append((year, lo, hi))
    return out


def certified_sessions_in(start: date, end: date) -> list[date]:
    """Certified trading dates — from the calendar, needing no provider call."""
    return [s.market_date for s in CAL.sessions_in_range(start, end)
            if s.session_type in (CAL.SESSION_REGULAR, CAL.SESSION_EARLY_CLOSE)]


def mwcb_prevalence(start: date = AUDIT_START, end: date = AUDIT_END,
                    symbols=AUDIT_UNIVERSE) -> dict:
    """EXACT market-wide-halt prevalence. No sampling, no provider calls.

    The registry is COMPLETE for this window: there were exactly four Level 1
    market-wide circuit breakers in US equities between 2017 and 2026, all in
    March 2020. So the halt-session share of the population is a closed-form
    fact about the calendar and the registry — the only quantity in this audit
    that needs no data acquisition at all.
    """
    sessions = certified_sessions_in(start, end)
    halt_dates = [e.market_date for e in IR.MWCB_EVENTS
                  if start <= e.market_date <= end and e.market_date in set(sessions)]
    n_sym = len(symbols)
    return {
        "certified_trading_dates": len(sessions),
        "certified_symbol_sessions": len(sessions) * n_sym,
        "mwcb_dates": [d.isoformat() for d in sorted(halt_dates)],
        "mwcb_symbol_sessions": len(halt_dates) * n_sym,
        "mwcb_share_of_symbol_sessions_pct": round(
            100.0 * len(halt_dates) * n_sym / (len(sessions) * n_sym), 5)
        if sessions else 0.0,
        "registry_complete_for_window": True,
        "basis": "MWCB registry is complete for 2017-2026; halt prevalence is "
                 "computed from the certified calendar, not sampled",
    }


def sample_windows(start: date = AUDIT_START, end: date = AUDIT_END, *,
                   per_year: int = 1) -> list[tuple[int, date, date]]:
    """Deterministic provider-compatible sample windows.

    Fixed, reproducible anchors rather than random dates, so two runs over
    identical evidence reproduce the same research object. The anchor MONTH
    rotates by year so a one-window-per-year budget still spreads across
    seasons instead of sampling ten consecutive Februaries.

    One window per year keeps the run inside the registered 40-call budget:
    2 anchors/year would need 42 calls and the pre-flight correctly refuses it.
    """
    out = []
    rotation = [(2, 15), (5, 15), (9, 15), (11, 15)]
    for year in range(start.year, end.year + 1):
        anchors = [rotation[(year + i) % len(rotation)] for i in range(per_year)]
        # A partial final year can rotate onto a month past the coverage end.
        # Fall back to any anchor that fits rather than dropping the year: a
        # silently absent year is a coverage hole nobody would notice.
        if all(date(year, mo, day) > end or date(year, mo, day) < start
               for mo, day in anchors):
            anchors = [a for a in rotation
                       if start <= date(year, a[0], a[1]) <= end][:per_year]
        for mo, day in sorted(set(anchors)):
            anchor = date(year, mo, day)
            if not (start <= anchor <= end):
                continue
            sess = [d for d in certified_sessions_in(
                anchor, min(end, anchor + timedelta(days=20)))]
            if len(sess) < SAMPLE_WINDOW_SESSIONS:
                continue
            win = sess[:SAMPLE_WINDOW_SESSIONS]
            out.append((year, win[0], win[-1]))
    return out


def planned_calls(symbols=AUDIT_UNIVERSE, chunks=None) -> int:
    return len(symbols) * len(chunks if chunks is not None else yearly_chunks())


def audit_budget_headroom(symbols=AUDIT_UNIVERSE, chunks=None) -> dict:
    """Refuse before starting if the run cannot complete inside governance.

    A mid-run governor refusal returns `[]`, which the frozen pipeline reads as
    NO_DATA and records as REJECTED_MISSING_BARS — fabricated market-gap
    evidence, in exactly the audit whose subject is market gaps. Pre-flighting
    is what makes that impossible rather than unlikely.
    """
    from portfolio_automation.data_budget.scheduler import (
        DEFAULT_RUN_MODES, RunModeScheduler,
    )
    from portfolio_automation.intraday_lab.pilot import INTRADAY_RESEARCH_RUN_MODE

    sched = RunModeScheduler(DEFAULT_RUN_MODES)
    planned = planned_calls(symbols, chunks)
    budget = sched.call_budget(INTRADAY_RESEARCH_RUN_MODE)
    fits = budget == 0 or planned < budget
    return {"run_mode": INTRADAY_RESEARCH_RUN_MODE, "planned_calls": planned,
            "call_budget": budget, "priority": sched.priority(INTRADAY_RESEARCH_RUN_MODE),
            "fits": fits,
            "reason": None if fits else
            (f"audit would issue {planned} provider calls against a {budget}-call "
             f"budget; the governor would skip the excess and those skips would "
             f"be recorded as absent market data")}


# ── Descriptive session metrics (definitions frozen by METRIC_DEFINITIONS_VERSION)
@dataclass(frozen=True)
class SessionMetrics:
    symbol: str
    market_date: date
    state: str
    bars: int
    segments: int
    open_close_return: float
    abs_open_close_return: float
    range_pct: float
    max_up_excursion_pct: float
    max_down_excursion_pct: float
    largest_step_return: float
    within_segment_realized_vol: float
    discontinuity_return: float | None
    halt_minutes: float | None

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["market_date"] = self.market_date.isoformat()
        return d


def session_metrics(symbol: str, market_date: date, bars, state: str,
                    *, event: dict | None = None) -> SessionMetrics | None:
    """Descriptive statistics for one session's OBSERVED bars.

    Definitions, all on observed bars only — nothing is imputed across a gap:

      open_close_return   last close / first open - 1
      range_pct           (max high - min low) / first open
      *_excursion_pct     extreme high/low relative to the first open
      largest_step_return biggest close-to-close move between CONSECUTIVE
                          observed bars WITHIN a segment
      within_segment_realized_vol
                          stdev of intra-segment 5-minute log-ish simple returns.
                          Cross-segment steps are EXCLUDED: a 20-minute gap is
                          not a 5-minute return, and folding it in would inflate
                          a volatility number that claims equal spacing.
      discontinuity_return
                          last pre-gap close -> first post-gap open, reported
                          SEPARATELY and never mixed into the vol series.
    """
    segments = IR.segment_bars(bars)
    flat = [b for seg in segments for b in seg]
    if not flat:
        return None
    first_open = flat[0].open
    last_close = flat[-1].close
    if first_open <= 0:
        return None

    highs = [b.high for b in flat]
    lows = [b.low for b in flat]
    intra: list[float] = []
    largest_step = 0.0
    for seg in segments:
        for i in range(1, len(seg)):
            prev, cur = seg[i - 1].close, seg[i].close
            if prev > 0:
                r = cur / prev - 1.0
                intra.append(r)
                largest_step = max(largest_step, abs(r))

    disc = None
    halt_minutes = None
    if len(segments) > 1:
        pre_close = segments[0][-1].close
        post_open = segments[1][0].open
        if pre_close > 0:
            disc = post_open / pre_close - 1.0
        gap = segments[1][0].bar_start_at - segments[0][-1].bar_end_at
        halt_minutes = gap.total_seconds() / 60.0

    return SessionMetrics(
        symbol=symbol, market_date=market_date, state=state,
        bars=len(flat), segments=len(segments),
        open_close_return=last_close / first_open - 1.0,
        abs_open_close_return=abs(last_close / first_open - 1.0),
        range_pct=(max(highs) - min(lows)) / first_open,
        max_up_excursion_pct=max(highs) / first_open - 1.0,
        max_down_excursion_pct=min(lows) / first_open - 1.0,
        largest_step_return=largest_step,
        within_segment_realized_vol=(statistics.pstdev(intra) if len(intra) > 1 else 0.0),
        discontinuity_return=disc,
        halt_minutes=halt_minutes,
    )


def _percentile_rank(values: list[float], x: float) -> float:
    """Fraction of `values` strictly below x, as a percentage."""
    if not values:
        return float("nan")
    return 100.0 * sum(1 for v in values if v < x) / len(values)


def audit_chunk(symbol: str, year: int, start: date, end: date, provider, *,
                root: str = ".") -> dict:
    """Classify every certified session for one symbol-year.

    Runs the FROZEN Session 2 chain (acquire -> reconcile). Raw evidence is
    persisted immutably; the full canonical snapshot is not, because the audit
    needs the reconciliation verdicts rather than another copy of the bars.
    """
    request = DatasetRequest(symbols=(symbol,), start=start, end=end)
    try:
        acq = PL.acquire(request, provider, root=root)
    except Exception as exc:
        return {"symbol": symbol, "year": year, "status": "SOURCE_ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                "classifications": [], "metrics": []}

    ds = build_canonical_dataset(
        acq["bars_by_date"], request=request,
        provider_failures=acq["provider_failures"],
        normalization_failures=acq["normalization_failures"])

    classifications, metrics = [], []
    session_records: list[dict] = []
    view_fps: list[str] = []
    mfp = None
    try:
        built = PL.build_historical_research_dataset(request, provider, root=root)
        mfp = built["manifest_fingerprint"]
    except Exception as exc:
        return {"symbol": symbol, "year": year, "status": "SOURCE_ERROR",
                "error": f"manifest persistence failed: {type(exc).__name__}",
                "classifications": [], "metrics": []}
    expected_raw = IR.expected_raw_lineage(mfp, symbol, request.timeframe, root=root)

    for rec in ds.reconciliations:
        session = CAL.resolve_session(rec.market_date)
        if session.session_type not in (CAL.SESSION_REGULAR, CAL.SESSION_EARLY_CLOSE):
            continue                                   # not a certified session
        c = IR.classify_session(
            symbol=rec.symbol, market_date=rec.market_date, timeframe=rec.timeframe,
            session2_state=rec.admission_status,
            missing_timestamps=rec.missing_timestamps,
            unexpected_timestamps=rec.unexpected_timestamps,
            session_type=session.session_type)
        classifications.append(c)
        record = {
            "market_date": rec.market_date.isoformat(),
            "session2_state": rec.admission_status,
            "session3_classification": c.state,
            "explained_missing": list(c.explained_missing),
            "unexplained_missing": list(c.unexplained_missing),
        }
        if c.in_halt_aware_cohort:
            # Metrics are computed from the SOURCE-reconstructed bars so the
            # verifier can recompute the identical numbers without a refetch.
            bars = IR.reconstruct_observed_bars(symbol, rec.market_date,
                                                request.timeframe, expected_raw,
                                                root=root)
            m = session_metrics(symbol, rec.market_date, bars, c.state,
                                event=c.mwcb_event)
            if m:
                metrics.append(m)
                record["metrics"] = m.to_dict()
        if c.state == IR.VALID_MARKET_WIDE_HALT_SESSION:
            payload = IR.irregular_view_payload(
                classification=c, source_manifest_fingerprint=mfp,
                source_dataset_fingerprint=built["dataset_fingerprint"],
                raw_content_fingerprints=expected_raw,
                calendar_identity=_calendar_identity(),
                bars=IR.reconstruct_observed_bars(symbol, rec.market_date,
                                                  request.timeframe, expected_raw,
                                                  root=root))
            vfp = IR.persist_irregular_view(payload, root=root)
            record["irregular_view_fingerprint"] = vfp
            view_fps.append(vfp)
        session_records.append(record)

    chunk = {
        "schema_version": SCHEMA_VERSION,
        "policy_fingerprint": IR.policy_fingerprint(),
        "registry_fingerprint": IR.registry_fingerprint(),
        "metric_definitions_version": IR.METRIC_DEFINITIONS_VERSION,
        "symbol": symbol, "start": start.isoformat(), "end": end.isoformat(),
        "timeframe": request.timeframe,
        "used_session2_manifest": mfp,
        "used_raw_content": expected_raw,
        "used_irregular_views": sorted(view_fps),
        "sessions": session_records,
    }
    return {"symbol": symbol, "year": year, "status": "OK",
            "classifications": classifications, "metrics": metrics,
            "chunk_fingerprint": persist_population_chunk(chunk, root=root),
            "provider_calls": len(acq["acquisitions"])}


def mwcb_windows() -> list[tuple[int, date, date]]:
    """Provider-compatible windows covering every registry event and a control.

    Includes 2020-03-17 — an extremely volatile session with NO circuit breaker —
    so the audit demonstrates on real data that volatility alone never produces a
    halt classification.
    """
    return [(2020, date(2020, 3, 9), date(2020, 3, 13)),
            (2020, date(2020, 3, 16), date(2020, 3, 18))]


def run_population_audit(provider, *, symbols=AUDIT_UNIVERSE, root: str = ".",
                         chunks=None) -> dict:
    """The Session 3.0 audit: EXACT halt prevalence + a SAMPLED continuous cohort.

    Two different epistemic statuses, deliberately not blended:

      * halt prevalence is EXACT — the registry is complete for the window, so it
        is computed from the certified calendar with no acquisition at all;
      * the continuous-cohort distribution is a STRATIFIED SAMPLE, because the
        provider returns at most ~6 sessions per call and a census would need
        ~800 calls against a 40-call registered budget.

    The sample supports the tail-position question ("are excluded sessions in the
    tails?"). It does NOT support an unexplained-gap prevalence estimate, and
    none is reported.
    """
    chunks = chunks if chunks is not None else (sample_windows() + mwcb_windows())
    head = audit_budget_headroom(symbols, chunks)
    if not head["fits"]:
        raise RuntimeError(head["reason"])

    per_chunk, all_class, all_metrics, chunk_fps = [], [], [], []
    source_error_years = []
    calls = 0
    for symbol in sorted(symbols):
        for year, lo, hi in chunks:
            out = audit_chunk(symbol, year, lo, hi, provider, root=root)
            calls += out.get("provider_calls", 0)
            if out["status"] != "OK":
                # The year still has certified sessions; they are counted as
                # source errors so the denominator never quietly shrinks.
                n = len([s for s in CAL.sessions_in_range(lo, hi)
                         if s.session_type in (CAL.SESSION_REGULAR,
                                               CAL.SESSION_EARLY_CLOSE)])
                source_error_years.append(
                    {"symbol": symbol, "year": year, "sessions": n,
                     "error": out.get("error")})
                for s in CAL.sessions_in_range(lo, hi):
                    if s.session_type in (CAL.SESSION_REGULAR, CAL.SESSION_EARLY_CLOSE):
                        all_class.append(IR.SessionClassification(
                            symbol=symbol, market_date=s.market_date,
                            timeframe="5min", session2_state="ACQUISITION_FAILED",
                            state=IR.REJECTED_SOURCE_ERROR,
                            reason=out.get("error")))
                per_chunk.append({"symbol": symbol, "year": year,
                                  "status": "SOURCE_ERROR", "sessions": n})
                continue
            all_class.extend(out["classifications"])
            all_metrics.extend(out["metrics"])
            counts = _count_states(out["classifications"])
            chunk_fps.append(out["chunk_fingerprint"])
            per_chunk.append({"symbol": symbol, "year": year, "status": "OK",
                              "sessions": len(out["classifications"]),
                              "chunk_fingerprint": out["chunk_fingerprint"],
                              **counts})

    counts = _count_states(all_class)
    requested = sum(c["sessions"] for c in per_chunk)
    accounted = sum(counts.values())
    prevalence = mwcb_prevalence(symbols=symbols)

    cont = [m for m in all_metrics if m.state == IR.VALID_CONTINUOUS_SESSION]
    halt = [m for m in all_metrics if m.state == IR.VALID_MARKET_WIDE_HALT_SESSION]
    return {
        "schema_version": SCHEMA_VERSION,
        "source_module": "intraday_lab.population_audit",
        "observe_only": True,
        "session": "3.0",
        "policy_id": IR.POLICY_ID,
        "policy_fingerprint": IR.policy_fingerprint(),
        "registry_version": IR.MWCB_REGISTRY_VERSION,
        "registry_fingerprint": IR.registry_fingerprint(),
        "metric_definitions_version": IR.METRIC_DEFINITIONS_VERSION,
        "calendar_identity": _calendar_identity(),
        "symbols": sorted(symbols),
        "date_coverage": {"start": chunks[0][1].isoformat(),
                          "end": chunks[-1][2].isoformat()},
        "provider_calls": calls,
        "coverage_kind": "STRATIFIED_SAMPLE",
        # PopulationAudit generated_from -> PopulationChunk
        "generated_from_chunks": sorted(chunk_fps),
        "coverage_note": (
            "The continuous cohort is a deterministic stratified sample "
            f"({SAMPLE_WINDOW_SESSIONS}-session windows, fixed anchors), NOT a "
            "census. The provider returns at most ~"
            f"{PROVIDER_MAX_SESSIONS_PER_CALL} sessions per call, so a full "
            "2017-2026 census of two symbols needs ~800 calls against a 40-call "
            "registered budget. Unexplained-gap PREVALENCE is therefore NOT "
            "estimated here and must not be inferred from these counts."),
        "exact_mwcb_prevalence": prevalence,
        "requested_certified_symbol_sessions": requested,
        "accounted_symbol_sessions": accounted,
        "accounting_exact": requested == accounted,
        "counts": counts,
        "rates": {k: (round(100.0 * v / accounted, 4) if accounted else 0.0)
                  for k, v in counts.items()},
        "source_error_chunks": source_error_years,
        "per_chunk": per_chunk,
        "cohorts": {
            IR.COHORT_CONTINUOUS_ONLY: len(cont),
            IR.COHORT_HALT_AWARE: len(cont) + len(halt),
        },
        "comparison": compare_cohorts(cont, halt),
        "halt_sessions": [m.to_dict() for m in halt],
        "strategy_validation_allowed": False,
    }


def _count_states(classifications) -> dict:
    counts = {s: 0 for s in IR.POPULATION_STATES}
    for c in classifications:
        counts[c.state] = counts.get(c.state, 0) + 1
    return {k: v for k, v in counts.items() if k != IR.NOT_A_TRADING_SESSION}


def compare_cohorts(continuous: list[SessionMetrics],
                    halt: list[SessionMetrics]) -> dict:
    """Descriptive comparison plus where the halt sessions SIT in the continuous
    distribution. Percentile rank is the honest question at N=8: not 'is this
    significant' but 'is the excluded set in the tail'."""
    fields = ("abs_open_close_return", "range_pct", "max_down_excursion_pct",
              "largest_step_return", "within_segment_realized_vol")
    out = {"n_continuous": len(continuous), "n_halt": len(halt), "metrics": {}}
    for f in fields:
        cvals = [getattr(m, f) for m in continuous]
        hvals = [getattr(m, f) for m in halt]
        entry = {
            "continuous_median": round(statistics.median(cvals), 6) if cvals else None,
            "continuous_p99": (round(sorted(cvals)[int(0.99 * (len(cvals) - 1))], 6)
                               if cvals else None),
            "halt_median": round(statistics.median(hvals), 6) if hvals else None,
            "halt_values": [round(v, 6) for v in sorted(hvals)],
            "halt_percentile_ranks_in_continuous": [
                round(_percentile_rank(cvals, v), 3) for v in sorted(hvals)],
        }
        out["metrics"][f] = entry
    out["discontinuity_returns"] = sorted(
        round(m.discontinuity_return, 6) for m in halt
        if m.discontinuity_return is not None)
    out["halt_minutes"] = sorted(
        round(m.halt_minutes, 2) for m in halt if m.halt_minutes is not None)
    out["note"] = ("N=8 halt symbol-sessions across 4 dates. These are important "
                   "TAIL OBSERVATIONS, not a sample supporting inferential "
                   "statistics. No significance is claimed or implied.")
    return out


# ── Session 3.0 status + artifacts ─────────────────────────────────────────
SESSION_3_0_POLICY_READY = "SESSION_3_0_POLICY_READY"
SESSION_3_0_LIMITED = "SESSION_3_0_LIMITED"
SESSION_3_0_BLOCKED = "SESSION_3_0_BLOCKED"
SESSION_3_1_GO = "SESSION_3_1_GO"
SESSION_3_1_NO_GO = "SESSION_3_1_NO_GO"

# The chosen research-population policy. Recorded as data so a future strategy
# result can name the population contract it was produced under.
POLICY_CONTINUOUS_PRIMARY = "CONTINUOUS_PRIMARY_HALT_ROBUSTNESS"
POLICY_HALT_AWARE_PRIMARY = "HALT_AWARE_PRIMARY_CONTINUOUS_COMPARISON"
POLICY_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"


def session3_0_status(audit: dict | None = None, *, root: str = ".") -> dict:
    """Session 3.0 completion, derived from DURABLE evidence.

    The `audit` argument is retained for display/compatibility and can never
    grant authority: a fabricated dictionary satisfying the old checks returned
    POLICY_READY / SESSION_3_1_GO with zero blockers, even after the rendered
    population JSON was deleted. Authority now comes only from a content-
    addressed population audit reachable through the explicit pointer and
    re-verified on every call.

    Defence in depth: the gate measures its own critical invariants rather than
    inheriting them from the loader, so a future loader regression cannot
    silently widen what graduates.
    """
    from portfolio_automation.intraday_lab import foundation as FD

    try:
        evidence = load_session3_graduation_evidence(root=root)
    except Exception as exc:
        evidence = {"available": False, "audit": None, "integrity_valid": False,
                    "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
    verified = (evidence.get("audit") or {})
    counts = verified.get("counts") or {}
    comp = verified.get("comparison") or {}

    s2 = FD.session2_graduation(root=root)

    def _graduated_halt_views_bound_and_verified() -> bool:
        """The REAL invariant, replacing a vacuous `count >= 0` comparison.

        Every graduated VALID_MARKET_WIDE_HALT_SESSION must have a bound
        irregular view that verifies from source AND claims zero unexplained
        intervals. A global "no unexplained gaps anywhere" test would be wrong:
        unexplained-gap sessions may legitimately exist in the sampled
        population — they are simply excluded from both cohorts.

        Scanning every view on disk and treating an empty directory as success
        was also wrong: it passes when the very views the audit depends on are
        missing. The referenced views are what must exist.
        """
        halt_claimed = counts.get(IR.VALID_MARKET_WIDE_HALT_SESSION, 0)
        views = verified.get("irregular_views") or []
        if halt_claimed <= 0:
            return not views                 # nothing claimed, nothing required
        if len(views) != halt_claimed:
            return False                     # a claim with no bound evidence
        for vfp in views:
            v = IR.verify_irregular_view(vfp, root=root)
            if not v.get("verified"):
                return False
            body = ST.read_snapshot(ST.IRREGULAR_VIEWS, vfp,
                                    "irregular_session.json", root=root) or {}
            if body.get("unexplained_missing"):
                return False
        return True

    def _control_date_is_not_a_halt() -> bool:
        """2020-03-17 was extremely volatile and had NO circuit breaker."""
        return IR.mwcb_event_for(date(2020, 3, 17)) is None

    def _all_mwcb_dates_present() -> bool:
        got = set((verified.get("exact_mwcb_prevalence") or {}).get("mwcb_dates") or [])
        return got == {e.market_date.isoformat() for e in IR.MWCB_EVENTS}

    checks = {
        "session2_gate_ready":
            s2["status"] == FD.DATASET_FEATURE_FOUNDATION_READY,
        "population_evidence_available": bool(evidence.get("available")),
        "population_evidence_content_verifies": bool(verified),
        "policy_fingerprint_matches":
            (verified.get("audit") or {}).get("policy_fingerprint") == IR.policy_fingerprint(),
        "registry_fingerprint_matches":
            (verified.get("audit") or {}).get("registry_fingerprint") == IR.registry_fingerprint(),
        "population_accounting_exact": bool(verified.get("accounting_exact")),
        "all_required_mwcb_dates_present": _all_mwcb_dates_present(),
        "mwcb_sessions_classified":
            counts.get(IR.VALID_MARKET_WIDE_HALT_SESSION, 0) > 0,
        "graduated_halt_views_bound_and_verified":
            _graduated_halt_views_bound_and_verified(),
        "aggregates_rebuilt_from_child_evidence":
            bool(verified.get("generated_from_chunks")),
        "control_date_remains_non_halt": _control_date_is_not_a_halt(),
        "no_source_errors": counts.get(IR.REJECTED_SOURCE_ERROR, 0) == 0,
        "cohort_comparison_produced":
            comp.get("n_continuous", 0) > 0 and comp.get("n_halt", 0) > 0,
        "exact_mwcb_prevalence_computed": bool(
            (verified.get("exact_mwcb_prevalence") or {}).get("registry_complete_for_window")),
        "strategy_validation_stays_false":
            (verified.get("audit") or {}).get("strategy_validation_allowed") is False,
    }
    blockers = sorted(k for k, v in checks.items() if not v)
    status = SESSION_3_0_POLICY_READY if not blockers else SESSION_3_0_LIMITED
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "intraday_lab.population_audit.session3_0_status",
        "observe_only": True,
        "session": "3.0",
        "status": status,
        "session_3_1_gate": SESSION_3_1_GO if not blockers else SESSION_3_1_NO_GO,
        "measured_checks": checks,
        "measured_passed": sum(1 for v in checks.values() if v),
        "measured_total": len(checks),
        "test_enforced_contracts": {
            "fabricated_caller_audit_cannot_graduate":
                "tests/test_intraday_lab_session3_durable.py::"
                "test_a_fabricated_caller_audit_cannot_grant_graduation",
            "derived_view_source_binding":
                "tests/test_intraday_lab_session3_durable.py (8 adversarial "
                "derivation cases)",
            "fresh_process_recovery":
                "tests/test_intraday_lab_session3_durable.py::"
                "test_graduation_survives_a_fresh_process",
        },
        "blockers": blockers,
        "population_fingerprint": evidence.get("population_fingerprint"),
        "evidence_integrity_valid": bool(evidence.get("integrity_valid")),
        "evidence_reason": evidence.get("reason"),
        "session2_status": s2["status"],
        "research_population_policy": POLICY_HALT_AWARE_PRIMARY,
        "policy_rationale": (
            "Authoritative market-wide halt sessions sit in the upper tail of "
            "intraday range, largest observed step and within-segment realized "
            "volatility, so excluding them removes precisely the regimes a "
            "strategy most needs to survive. They are admissible only under a "
            "registry-backed, fully-explained-absence contract, so admitting "
            "them costs no data integrity. BOTH cohorts are reported because "
            "the halt cohort is tiny (N=8) and execution during a halt is "
            "prohibited."),
        "strategy_validation_allowed": False,
    }


def render_population_md(audit: dict, status: dict) -> str:
    c = audit["comparison"]
    p = audit["exact_mwcb_prevalence"]
    lines = [
        "# Intraday Strategy Lab — Session 3.0 Research-Population Audit",
        "",
        f"**{status['status']}** · **{status['session_3_1_gate']}** · research-only "
        f"· `strategy_validation_allowed = false`",
        "",
        f"Policy `{audit['policy_id']}` · registry `{audit['registry_version']}` "
        f"(`{audit['registry_fingerprint'][:12]}`) · metrics "
        f"`{audit['metric_definitions_version']}`",
        "",
        "## Exact market-wide-halt prevalence",
        "",
        "Computed from the certified calendar and the registry, with **no** "
        "provider calls — the MWCB registry is complete for this window.",
        "",
        f"| certified trading dates | {p['certified_trading_dates']} |",
        "|---|---|",
        f"| certified symbol-sessions ({len(audit['symbols'])} symbols) | "
        f"{p['certified_symbol_sessions']} |",
        f"| MWCB dates | {', '.join(p['mwcb_dates'])} |",
        f"| MWCB symbol-sessions | {p['mwcb_symbol_sessions']} "
        f"({p['mwcb_share_of_symbol_sessions_pct']}%) |",
        "",
        "## Sampled population",
        "",
        f"{audit['coverage_note']}",
        "",
        "| state | n | % of sample |",
        "|---|---:|---:|",
    ]
    for k, v in audit["counts"].items():
        lines.append(f"| `{k}` | {v} | {audit['rates'][k]}% |")
    lines += [
        "",
        f"Accounting exact: **{audit['accounting_exact']}** "
        f"({audit['accounted_symbol_sessions']} of "
        f"{audit['requested_certified_symbol_sessions']} requested)",
        "",
        "## Cohort comparison",
        "",
        f"`CONTINUOUS_ONLY` n={c['n_continuous']} · `HALT_AWARE` "
        f"n={c['n_continuous']}+{c['n_halt']}",
        "",
        "| metric | continuous median | continuous p99 | halt median | "
        "halt percentile ranks in continuous |",
        "|---|---:|---:|---:|---|",
    ]
    for f, e in c["metrics"].items():
        lines.append(
            f"| `{f}` | {e['continuous_median']} | {e['continuous_p99']} | "
            f"{e['halt_median']} | {e['halt_percentile_ranks_in_continuous']} |")
    lines += [
        "",
        f"Reopening discontinuity returns: `{c['discontinuity_returns']}`",
        f"Observed absent minutes per halt session: `{c['halt_minutes']}`",
        "",
        f"> {c['note']}",
        "",
        "## Policy decision",
        "",
        f"**{status['research_population_policy']}**",
        "",
        status["policy_rationale"],
        "",
        "## Limitations",
        "",
        "- Symbol-specific halts (LULD, news, regulatory, IPO pauses) are **not "
        "classifiable**: no authoritative historical source is sanctioned, so "
        "such gaps remain rejected.",
        "- Unexplained-gap **prevalence is not estimated** — the provider "
        f"returns at most ~{PROVIDER_MAX_SESSIONS_PER_CALL} sessions per call, "
        "so a census is outside the registered research budget.",
        "- The halt cohort is 8 symbol-sessions on 4 dates. Tail observations, "
        "not an inferential sample.",
        "- Execution during an authoritative halt is prohibited (Session 3.2).",
    ]
    return "\n".join(lines) + "\n"


def write_session3_artifacts(audit: dict, status: dict, *, root: str = ".") -> list[str]:
    """Deterministic Session 3.0 artifacts under the HISTORICAL research area."""
    base = ST.intraday_root(root) / "session3"
    base.mkdir(parents=True, exist_ok=True)
    import json as _json

    written = []
    for name, payload in (("irregular_session_policy.json", IR.policy_provenance()),
                          ("irregular_session_registry.json", IR.registry_provenance()),
                          ("irregular_session_population.json",
                           {**audit, "session3_0_status": status})):
        path = base / name
        path.write_text(_json.dumps(payload, indent=1, sort_keys=True, default=str),
                        encoding="utf-8")
        written.append(str(path))
    md = base / "irregular_session_population.md"
    md.write_text(render_population_md(audit, status), encoding="utf-8")
    written.append(str(md))
    return written


# ── Durable Session 3.0 graduation evidence ────────────────────────────────
# The gate previously accepted a caller-supplied audit dictionary. A fabricated
# dict satisfying the checks returned SESSION_3_0_POLICY_READY / SESSION_3_1_GO
# with zero blockers — even with the rendered population JSON deleted. That is
# the same failure class Session 2 removed: a verdict from caller claims rather
# than from durable evidence.
# v2 (2026-08-09): identity now binds the CHILD evidence fingerprints. Under v1
# the aggregates were unfalsifiable — a fabricated audit claiming 100 halt
# sessions (against a registry with exactly four MWCB dates) verified, was
# accepted by the setter and graduated with zero blockers. v1 objects are
# preserved and reported archival under their historical contract, never
# silently reinterpreted, following the Session 2 identity-era precedent.
POPULATION_IDENTITY_SCHEMA = "intraday_session3_population_v2"
POPULATION_SCHEMA_HISTORY = ("intraday_session3_population_v1",
                             "intraday_session3_population_v2")


def population_identity_payload(audit: dict) -> dict:
    """The canonical projection that DEFINES a population audit's identity.

    Binds to the policy, registry, metric definitions and calendar it was
    computed under, the universe and windows it covered, and the accounting and
    comparison results. Excludes generation time, runtime and machine — none of
    which is part of what the audit MEANS.
    """
    return {
        "schema": POPULATION_IDENTITY_SCHEMA,
        # The child evidence IS part of what the audit means.
        "generated_from_chunks": sorted(audit.get("generated_from_chunks") or []),
        "audit_schema_version": audit.get("schema_version"),
        "policy_id": audit.get("policy_id"),
        "policy_fingerprint": audit.get("policy_fingerprint"),
        "registry_version": audit.get("registry_version"),
        "registry_fingerprint": audit.get("registry_fingerprint"),
        "metric_definitions_version": audit.get("metric_definitions_version"),
        "calendar_identity": audit.get("calendar_identity"),
        "symbols": sorted(audit.get("symbols") or []),
        "date_coverage": audit.get("date_coverage"),
        "coverage_kind": audit.get("coverage_kind"),
        "per_chunk": audit.get("per_chunk"),
        "requested_certified_symbol_sessions":
            audit.get("requested_certified_symbol_sessions"),
        "accounted_symbol_sessions": audit.get("accounted_symbol_sessions"),
        "accounting_exact": audit.get("accounting_exact"),
        "counts": audit.get("counts"),
        "cohorts": audit.get("cohorts"),
        "comparison": audit.get("comparison"),
        "exact_mwcb_prevalence": audit.get("exact_mwcb_prevalence"),
        "halt_sessions": audit.get("halt_sessions"),
        "strategy_validation_allowed": audit.get("strategy_validation_allowed", False),
    }


def population_fingerprint(audit: dict) -> str:
    return ST.content_hash(population_identity_payload(audit))


_NON_CONTENT_AUDIT_KEYS = frozenset({"generated_at", "source_error_chunks"})


def persist_population_audit(audit: dict, *, root: str = ".") -> str:
    """Freeze a population audit as immutable content-addressed evidence."""
    body = {k: v for k, v in audit.items() if k not in _NON_CONTENT_AUDIT_KEYS}
    fp = population_fingerprint(audit)
    ST.write_snapshot(ST.SESSION3_POPULATION, fp, {
        "population_audit.json": body,
        "population_manifest.json": {
            "schema_version": SCHEMA_VERSION,
            "identity_schema": POPULATION_IDENTITY_SCHEMA,
            "population_fingerprint": fp,
            "policy_fingerprint": audit.get("policy_fingerprint"),
            "registry_fingerprint": audit.get("registry_fingerprint"),
            "symbols": sorted(audit.get("symbols") or []),
            "accounted_symbol_sessions": audit.get("accounted_symbol_sessions"),
        },
    }, root=root)
    return fp


def verify_population_audit(fingerprint: str, *, root: str = ".") -> dict:
    """Recompute everything the gate will rely on. Stored claims are not trusted."""
    def fail(reason: str, **extra) -> dict:
        return {"verified": False, "reason": reason,
                "population_fingerprint": fingerprint, **extra}

    body = ST.read_snapshot(ST.SESSION3_POPULATION, fingerprint,
                            "population_audit.json", root=root)
    man = ST.read_snapshot(ST.SESSION3_POPULATION, fingerprint,
                           "population_manifest.json", root=root)
    if body is None or man is None:
        return fail("missing population_audit or population_manifest")
    if man.get("population_fingerprint") != fingerprint:
        return fail("population manifest declares a different fingerprint")
    if population_fingerprint(body) != fingerprint:
        return fail("persisted audit does not hash to its identity — modified")
    declared = man.get("identity_schema")
    if declared not in POPULATION_SCHEMA_HISTORY:
        return fail(f"unknown population identity schema {declared!r}")
    if declared != POPULATION_IDENTITY_SCHEMA:
        return fail(f"audit was minted under {declared!r}; the current contract "
                    f"is {POPULATION_IDENTITY_SCHEMA!r} — archival only",
                    archival=True)

    # Identity of the contracts it was computed under must still be current.
    if body.get("policy_fingerprint") != IR.policy_fingerprint():
        return fail("audit was computed under a different Session 3.0 policy")
    if body.get("registry_fingerprint") != IR.registry_fingerprint():
        return fail("audit was computed under a different MWCB registry")
    if body.get("metric_definitions_version") != IR.METRIC_DEFINITIONS_VERSION:
        return fail("audit was computed under different metric definitions")

    # ── Aggregates are REBUILT from the immutable child evidence ──────────
    # Internal arithmetic consistency is not evidence of derivation: the whole
    # point of v2 is that the numbers must come from somewhere verifiable.
    chunk_fps = sorted(body.get("generated_from_chunks") or [])
    if not chunk_fps:
        return fail("audit references no population child evidence, so its "
                    "aggregates cannot be reconstructed")

    declared_chunks = sorted({c.get("chunk_fingerprint")
                              for c in (body.get("per_chunk") or [])
                              if c.get("chunk_fingerprint")})
    if declared_chunks and declared_chunks != chunk_fps:
        return fail("per_chunk rows and generated_from_chunks disagree about "
                    "which child evidence this audit summarises")

    rebuilt_counts: dict[str, int] = {}
    rebuilt_metrics, rebuilt_views, rebuilt_sessions = [], [], 0
    symbols_seen, chunk_results = set(), {}
    for cfp in chunk_fps:
        cv = verify_population_chunk(cfp, root=root)
        chunk_results[cfp] = cv.get("verified")
        if not cv["verified"]:
            return fail(f"population child {cfp} failed verification: "
                        f"{cv['reason']}", chunks=chunk_results)
        for k, v in cv["counts"].items():
            rebuilt_counts[k] = rebuilt_counts.get(k, 0) + v
        rebuilt_metrics.extend(cv["metrics"])
        rebuilt_views.extend(cv["irregular_views"])
        rebuilt_sessions += cv["session_count"]
        symbols_seen.add(cv["symbol"])

    counts = body.get("counts") or {}
    if {k: v for k, v in counts.items() if v} != {k: v for k, v in rebuilt_counts.items() if v}:
        return fail(f"stored population counts do not rebuild from child "
                    f"evidence (stored {counts}, rebuilt {rebuilt_counts})")

    requested = sum(c.get("sessions", 0) for c in body.get("per_chunk") or [])
    accounted = sum(counts.values())
    if requested != rebuilt_sessions:
        return fail(f"per_chunk session counts ({requested}) do not match the "
                    f"child evidence ({rebuilt_sessions})")
    if requested != body.get("requested_certified_symbol_sessions"):
        return fail("requested symbol-sessions do not recompute from per_chunk")
    if accounted != body.get("accounted_symbol_sessions"):
        return fail("accounted symbol-sessions do not recompute from counts")
    if requested != accounted or not body.get("accounting_exact"):
        return fail(f"population accounting is not exact: {accounted} accounted "
                    f"of {requested} requested")

    if sorted(body.get("symbols") or []) != sorted(symbols_seen):
        return fail("audit symbol universe does not match its child evidence")

    # Cohorts, halt-session list and the cohort comparison all rebuild.
    cont = [m for m in rebuilt_metrics if m["state"] == IR.VALID_CONTINUOUS_SESSION]
    halt = [m for m in rebuilt_metrics if m["state"] == IR.VALID_MARKET_WIDE_HALT_SESSION]
    if body.get("cohorts") != {IR.COHORT_CONTINUOUS_ONLY: len(cont),
                               IR.COHORT_HALT_AWARE: len(cont) + len(halt)}:
        return fail("stored cohort counts do not rebuild from child evidence")
    if sorted(body.get("halt_sessions") or [], key=lambda m: (m["symbol"], m["market_date"])) != \
            sorted(halt, key=lambda m: (m["symbol"], m["market_date"])):
        return fail("stored halt-session list does not rebuild from child evidence")

    rebuilt_cmp = compare_cohorts(
        [SessionMetrics(**{k: v for k, v in m.items() if k != "market_date"},
                        market_date=date.fromisoformat(m["market_date"])) for m in cont],
        [SessionMetrics(**{k: v for k, v in m.items() if k != "market_date"},
                        market_date=date.fromisoformat(m["market_date"])) for m in halt])
    if body.get("comparison") != rebuilt_cmp:
        return fail("stored cohort comparison does not rebuild from child evidence")

    # Exact MWCB prevalence is recomputed from the calendar + registry.
    exact = mwcb_prevalence(symbols=body.get("symbols") or AUDIT_UNIVERSE)
    if body.get("exact_mwcb_prevalence") != exact:
        return fail("exact MWCB prevalence does not recompute from the calendar "
                    "and registry")

    return {"verified": True, "reason": None,
            "population_fingerprint": fingerprint,
            "audit": body,
            "counts": counts,
            "requested_certified_symbol_sessions": requested,
            "accounted_symbol_sessions": accounted,
            "accounting_exact": True,
            "comparison": body.get("comparison") or {},
            "generated_from_chunks": chunk_fps,
            "irregular_views": sorted(rebuilt_views),
            "exact_mwcb_prevalence": exact,
            "symbols": body.get("symbols") or []}


def graduation_sufficiency(verified: dict) -> list[str]:
    """Why VERIFIED evidence is still not sufficient to graduate, if it isn't.

    Shared by the setter and the loader so they can never disagree about what
    qualifies. Integrity and sufficiency stay separate concepts: an audit with
    no halt sessions is authentic evidence that simply does not exercise what
    Session 3.0 certifies.
    """
    counts = verified.get("counts") or {}
    comp = verified.get("comparison") or {}
    out = []
    if counts.get(IR.VALID_MARKET_WIDE_HALT_SESSION, 0) <= 0:
        out.append("no market-wide halt sessions classified")
    if counts.get(IR.REJECTED_SOURCE_ERROR, 0) != 0:
        out.append("graduation evidence contains source errors")
    if not (comp.get("n_continuous", 0) > 0 and comp.get("n_halt", 0) > 0):
        out.append("cohort comparison is missing a cohort")
    return out


def set_session3_graduation_evidence(fingerprint: str, *, root: str = ".") -> dict:
    """Point Session 3.0 graduation at an immutable population audit."""
    v = verify_population_audit(fingerprint, root=root)
    if not v["verified"]:
        raise ValueError(f"refusing to point Session 3.0 graduation at "
                         f"unverifiable evidence {fingerprint}: {v['reason']}")
    short = graduation_sufficiency(v)
    if short:
        raise ValueError(f"population audit {fingerprint} is authentic but "
                         f"insufficient for Session 3.0 graduation: {short}")
    path = ST.intraday_root(root) / ST.SESSION3_GRADUATION_POINTER
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_module": "intraday_lab.population_audit",
        "observe_only": True,
        "population_fingerprint": fingerprint,
        "identity_schema": POPULATION_IDENTITY_SCHEMA,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "note": "Immutable pointer target; Session 3.0 graduation is always "
                "RE-VERIFIED from this object, never cached.",
    }
    path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    return payload


def load_session3_graduation_evidence(*, root: str = ".") -> dict:
    """Locate and re-verify durable Session 3.0 evidence. No provider calls.

    A pointer is SELECTION, not AUTHORITY: every dereference re-runs the whole
    admission contract, so a hand-written pointer cannot confer graduation.
    """
    path = ST.intraday_root(root) / ST.SESSION3_GRADUATION_POINTER
    if not path.exists():
        return {"available": False, "audit": None, "integrity_valid": False,
                "reason": f"no Session 3.0 graduation pointer on disk "
                          f"({ST.SESSION3_GRADUATION_POINTER})"}
    try:
        pointer = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": False, "audit": None, "integrity_valid": False,
                "reason": f"pointer unreadable: {type(exc).__name__}"}
    fp = pointer.get("population_fingerprint")
    if not fp:
        return {"available": False, "audit": None, "integrity_valid": False,
                "reason": "pointer names no population_fingerprint"}
    v = verify_population_audit(fp, root=root)
    if not v["verified"]:
        return {"available": False, "audit": None, "integrity_valid": False,
                "population_fingerprint": fp,
                "reason": f"population evidence {fp} failed verification: "
                          f"{v['reason']}"}

    # Integrity is established. SUFFICIENCY is a separate question, decided by
    # the SAME helper the setter uses.
    insufficient = graduation_sufficiency(v)
    if insufficient:
        return {"available": False, "audit": None, "integrity_valid": True,
                "population_fingerprint": fp, "insufficient": insufficient,
                "reason": f"population evidence {fp} is authentic but "
                          f"insufficient for graduation: {insufficient}"}
    return {"available": True, "audit": v, "integrity_valid": True,
            "population_fingerprint": fp, "pointer": pointer, "reason": None}


# ── Immutable population CHILD evidence ────────────────────────────────────
# A content hash proves an audit has not changed. It does not prove the audit
# was DERIVED. A wholly fabricated audit claiming 100 market-wide halt sessions
# — against a registry containing exactly four MWCB dates — verified, was
# accepted by the setter, and graduated with zero blockers, because nothing
# bound its aggregates to per-session evidence.
#
# So the audit now summarises immutable CHILD objects, and verification REBUILDS
# every aggregate from them.
#
# Minimal provenance vocabulary, deliberately concrete rather than a generic
# parent-reference framework:
#
#   PopulationAudit  generated_from ->  PopulationChunk
#   PopulationChunk  used           ->  Session 2 manifest / raw content
#   PopulationChunk  used           ->  IrregularView   (halt sessions only)
CHUNK_IDENTITY_SCHEMA = "intraday_session3_population_chunk_v1"


def chunk_identity_payload(chunk: dict) -> dict:
    """What DEFINES a population chunk. Excludes generation time."""
    return {
        "schema": CHUNK_IDENTITY_SCHEMA,
        "policy_fingerprint": chunk.get("policy_fingerprint"),
        "registry_fingerprint": chunk.get("registry_fingerprint"),
        "metric_definitions_version": chunk.get("metric_definitions_version"),
        "symbol": chunk.get("symbol"),
        "start": chunk.get("start"),
        "end": chunk.get("end"),
        "timeframe": chunk.get("timeframe"),
        "used_session2_manifest": chunk.get("used_session2_manifest"),
        "used_raw_content": sorted(chunk.get("used_raw_content") or []),
        "used_irregular_views": sorted(chunk.get("used_irregular_views") or []),
        "sessions": chunk.get("sessions"),
    }


def chunk_fingerprint(chunk: dict) -> str:
    return ST.content_hash(chunk_identity_payload(chunk))


def persist_population_chunk(chunk: dict, *, root: str = ".") -> str:
    fp = chunk_fingerprint(chunk)
    ST.write_snapshot(ST.SESSION3_POPULATION_CHUNKS, fp, {
        "chunk_evidence.json": chunk,
        "chunk_manifest.json": {
            "schema_version": SCHEMA_VERSION,
            "identity_schema": CHUNK_IDENTITY_SCHEMA,
            "chunk_fingerprint": fp,
            "symbol": chunk.get("symbol"),
            "start": chunk.get("start"), "end": chunk.get("end"),
            "session_count": len(chunk.get("sessions") or []),
            "used_session2_manifest": chunk.get("used_session2_manifest"),
        },
    }, root=root)
    return fp


def verify_population_chunk(fingerprint: str, *, root: str = ".") -> dict:
    """Recompute a chunk's every claim from persisted Session 2 evidence.

    Reuses the frozen classifier, the frozen normalizer and the same metric
    function the audit used — a second implementation of any of them would let
    the same inputs mean two different things.
    """
    def fail(reason: str, **extra) -> dict:
        return {"verified": False, "reason": reason,
                "chunk_fingerprint": fingerprint, **extra}

    body = ST.read_snapshot(ST.SESSION3_POPULATION_CHUNKS, fingerprint,
                            "chunk_evidence.json", root=root)
    man = ST.read_snapshot(ST.SESSION3_POPULATION_CHUNKS, fingerprint,
                           "chunk_manifest.json", root=root)
    if body is None or man is None:
        return fail("missing chunk evidence or manifest")
    if man.get("chunk_fingerprint") != fingerprint:
        return fail("chunk manifest declares a different fingerprint")
    if chunk_fingerprint(body) != fingerprint:
        return fail("chunk does not hash to its identity — modified")
    if body.get("policy_fingerprint") != IR.policy_fingerprint():
        return fail("chunk built under a different Session 3.0 policy")
    if body.get("registry_fingerprint") != IR.registry_fingerprint():
        return fail("chunk built under a different MWCB registry")
    if body.get("metric_definitions_version") != IR.METRIC_DEFINITIONS_VERSION:
        return fail("chunk built under different metric definitions")

    symbol = body.get("symbol")
    timeframe = body.get("timeframe")
    mfp = body.get("used_session2_manifest")
    prov = ST.verify_dataset_provenance(mfp, root=root) if mfp else {}
    if not prov.get("verified"):
        return fail(f"source Session 2 manifest failed: {prov.get('reason')}")

    req = ST.read_snapshot(ST.DATASET_MANIFESTS, mfp, "request_manifest.json",
                           root=root) or {}
    recon = ST.read_snapshot(ST.DATASET_MANIFESTS, mfp, "reconciliation.json",
                             root=root) or []

    # The requested window must be the one the manifest actually covers.
    if (req.get("start"), req.get("end")) != (body.get("start"), body.get("end")):
        return fail("chunk window does not match its source request manifest")
    if symbol not in (req.get("symbols") or []):
        return fail("chunk symbol is not in its source request manifest")
    if timeframe != req.get("timeframe"):
        return fail("chunk timeframe disagrees with its source request manifest")

    # Raw lineage is decided by the MANIFEST, never by the chunk.
    expected_raw = IR.expected_raw_lineage(mfp, symbol, timeframe, root=root)
    if sorted(body.get("used_raw_content") or []) != expected_raw:
        return fail("chunk raw evidence is not the acquisition lineage for this "
                    "symbol", expected_raw=expected_raw)

    by_date = {(r.get("symbol"), r.get("market_date")): r for r in recon}
    rebuilt_counts: dict[str, int] = {}
    rebuilt_metrics, rebuilt_views = [], []
    for rec in body.get("sessions") or []:
        md = rec.get("market_date")
        row = by_date.get((symbol, md))
        if row is None:
            return fail(f"no persisted reconciliation row for {symbol} {md}")
        if row.get("admission_status") != rec.get("session2_state"):
            return fail(f"{symbol} {md}: session2_state does not match the "
                        f"persisted reconciliation record")
        session = CAL.resolve_session(date.fromisoformat(md))
        recomputed = IR.classify_session(
            symbol=symbol, market_date=date.fromisoformat(md), timeframe=timeframe,
            session2_state=row.get("admission_status"),
            missing_timestamps=row.get("missing_timestamps") or [],
            unexpected_timestamps=row.get("unexpected_timestamps") or [],
            session_type=session.session_type)
        if recomputed.state != rec.get("session3_classification"):
            return fail(f"{symbol} {md}: classification does not recompute "
                        f"(stored {rec.get('session3_classification')!r}, "
                        f"recomputed {recomputed.state!r})")
        if list(recomputed.explained_missing) != list(rec.get("explained_missing") or []):
            return fail(f"{symbol} {md}: explained_missing does not recompute")
        if list(recomputed.unexplained_missing) != list(rec.get("unexplained_missing") or []):
            return fail(f"{symbol} {md}: unexplained_missing does not recompute")
        rebuilt_counts[recomputed.state] = rebuilt_counts.get(recomputed.state, 0) + 1

        # Halt sessions must bind to a verifying irregular view for THIS
        # symbol/date. A valid view for another session must not qualify.
        if recomputed.state == IR.VALID_MARKET_WIDE_HALT_SESSION:
            vfp = rec.get("irregular_view_fingerprint")
            if not vfp:
                return fail(f"{symbol} {md}: halt session has no bound irregular view")
            vv = IR.verify_irregular_view(vfp, root=root)
            if not vv.get("verified"):
                return fail(f"{symbol} {md}: bound irregular view {vfp} failed "
                            f"verification: {vv.get('reason')}")
            if vv.get("symbol") != symbol or vv.get("market_date") != md:
                return fail(f"{symbol} {md}: bound irregular view describes "
                            f"{vv.get('symbol')} {vv.get('market_date')}")
            rebuilt_views.append(vfp)
            if recomputed.unexplained_missing:
                return fail(f"{symbol} {md}: graduated halt session has "
                            f"unexplained missing intervals")

        # Metrics must recompute from the bars reconstructed from RAW evidence.
        stored_metrics = rec.get("metrics")
        if stored_metrics is not None:
            bars = IR.reconstruct_observed_bars(symbol, date.fromisoformat(md),
                                                timeframe, expected_raw, root=root)
            m = session_metrics(symbol, date.fromisoformat(md), bars, recomputed.state)
            if m is None or m.to_dict() != stored_metrics:
                return fail(f"{symbol} {md}: session metrics do not recompute "
                            f"from the source bars")
            rebuilt_metrics.append(stored_metrics)

    if sorted(body.get("used_irregular_views") or []) != sorted(rebuilt_views):
        return fail("chunk irregular-view references do not match the halt "
                    "sessions it actually contains")

    return {"verified": True, "reason": None, "chunk_fingerprint": fingerprint,
            "symbol": symbol, "start": body.get("start"), "end": body.get("end"),
            "counts": rebuilt_counts,
            "session_count": len(body.get("sessions") or []),
            "metrics": rebuilt_metrics,
            "irregular_views": sorted(rebuilt_views),
            "used_session2_manifest": mfp}
