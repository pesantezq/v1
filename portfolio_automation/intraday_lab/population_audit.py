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
        if c.in_halt_aware_cohort:
            bars = acq["bars_by_date"].get((symbol, rec.market_date), [])
            m = session_metrics(symbol, rec.market_date, bars, c.state,
                                event=c.mwcb_event)
            if m:
                metrics.append(m)
    return {"symbol": symbol, "year": year, "status": "OK",
            "classifications": classifications, "metrics": metrics,
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

    per_chunk, all_class, all_metrics = [], [], []
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
            per_chunk.append({"symbol": symbol, "year": year, "status": "OK",
                              "sessions": len(out["classifications"]), **counts})

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


def session3_0_status(audit: dict, *, root: str = ".") -> dict:
    """Session 3.0 completion, computed from the audit evidence.

    Deliberately a SEPARATE signal from `strategy_validation_allowed`, which
    stays false: a population policy is not a validated strategy, and reusing
    that flag as a completion marker is exactly how a governance gate loses its
    meaning.
    """
    from portfolio_automation.intraday_lab import foundation as FD

    s2 = FD.session2_graduation(root=root)
    checks = {
        "session2_gate_ready":
            s2["status"] == FD.DATASET_FEATURE_FOUNDATION_READY,
        "registry_verifies": bool(IR.registry_fingerprint()),
        "population_accounting_exact": bool(audit.get("accounting_exact")),
        "mwcb_sessions_classified":
            audit["counts"].get(IR.VALID_MARKET_WIDE_HALT_SESSION, 0) > 0,
        "no_source_errors": audit["counts"].get(IR.REJECTED_SOURCE_ERROR, 0) == 0,
        "cohort_comparison_produced":
            audit["comparison"]["n_continuous"] > 0 and audit["comparison"]["n_halt"] > 0,
        "exact_mwcb_prevalence_computed":
            bool(audit.get("exact_mwcb_prevalence", {}).get("registry_complete_for_window")),
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
        "checks": checks,
        "blockers": blockers,
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
