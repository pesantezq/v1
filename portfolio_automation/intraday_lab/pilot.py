"""Bounded historical pilot for the Intraday Lab. Research-only, HISTORICAL.

PURPOSE
=======

Prove that ONE chain — request → governed provider → immutable raw → normalize →
certified calendar → exact session reconciliation → immutable canonical →
research manifest → PIT features → verified provenance graph — works across
historical REGIMES, not just on the handful of recent days that happened to be
convenient when it was written.

Bounded by design. This is not a backfill: it samples a small number of short
windows chosen because each one can break the chain in a DIFFERENT way.

    normal sessions      the baseline 78-bar grid
    early closes         42-bar sessions the calendar must predict exactly
    holidays             days that must yield NO bars at all
    DST transitions      where a naive local-time grid gains or loses an hour
    volatile regimes     where bar counts and prices are least well behaved
    a rule change        Juneteenth, which became a market holiday in 2022

Every requested symbol-date is accounted for in the output, admitted or not.
Rejections are reported, never hidden: a correctly rejected session is the
control working, and a pilot that quietly dropped one would be worthless as
evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from portfolio_automation.intraday_lab import migration as MG
from portfolio_automation.intraday_lab import pipeline as PL
from portfolio_automation.intraday_lab import storage as ST
from portfolio_automation.intraday_lab.dataset import DatasetRequest

SCHEMA_VERSION = "1"

DEFAULT_SYMBOLS = ("SPY", "AAPL")


@dataclass(frozen=True)
class PilotWindow:
    label: str
    start: date
    end: date
    rationale: str


# Each window earns its place by testing something the others cannot.
PILOT_WINDOWS: tuple[PilotWindow, ...] = (
    PilotWindow("2017-independence", date(2017, 7, 3), date(2017, 7, 7),
                "earliest research year; 07-03 early close + 07-04 holiday, "
                "under EDT"),
    PilotWindow("2020-covid-vol", date(2020, 3, 9), date(2020, 3, 13),
                "peak COVID volatility, immediately after the spring DST "
                "transition on 03-08"),
    PilotWindow("2022-juneteenth", date(2022, 6, 17), date(2022, 6, 23),
                "Juneteenth's FIRST year as a market holiday — a calendar rule "
                "that started mid-history"),
    PilotWindow("2023-fall-dst", date(2023, 11, 1), date(2023, 11, 6),
                "spans the fall DST transition on 11-05; the UTC offset moves "
                "while the local session does not"),
    PilotWindow("2024-thanksgiving", date(2024, 11, 27), date(2024, 12, 2),
                "Thanksgiving holiday + the 11-29 early close, under EST"),
    PilotWindow("2025-thanksgiving", date(2025, 11, 26), date(2025, 12, 1),
                "the 2025-11-28 early close PROVEN by the Session 1 probe"),
    PilotWindow("2026-normal", date(2026, 8, 3), date(2026, 8, 7),
                "a plain recent week; the control case"),
)


def run_window(window: PilotWindow, provider, *, symbols=DEFAULT_SYMBOLS,
               root: str = ".") -> dict:
    """Run the full governed chain over one window and summarise the outcome."""
    request = DatasetRequest(symbols=tuple(sorted(symbols)), start=window.start,
                             end=window.end)
    try:
        out = PL.build_historical_research_dataset(request, provider, root=root)
    except Exception as exc:                     # a failure is evidence, not a crash
        return {"label": window.label, "status": "PIPELINE_ERROR",
                "rationale": window.rationale,
                "start": window.start.isoformat(), "end": window.end.isoformat(),
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    prov = out.get("provenance_verification") or {}
    by_status: dict[str, int] = {}
    for r in out["rejections"]["rejections"]:
        by_status[r["admission_status"]] = by_status.get(r["admission_status"], 0) + 1
    return {
        "label": window.label,
        "status": "OK",
        "rationale": window.rationale,
        "start": window.start.isoformat(), "end": window.end.isoformat(),
        "symbols": list(request.symbols),
        "requested_symbol_dates": out["requested_symbol_dates"],
        "sessions_reconciled": out["sessions_reconciled"],
        "sessions_admitted": out["sessions_admitted"],
        "sessions_rejected": out["sessions_rejected"],
        "sessions_not_trading": out["sessions_not_trading"],
        "rejection_breakdown": by_status,
        "bars_admitted": out["bars_admitted"],
        "feature_observations": out["feature_observations"],
        "dataset_fingerprint": out["dataset_fingerprint"],
        "manifest_fingerprint": out["manifest_fingerprint"],
        "feature_fingerprint": out["feature_fingerprint"],
        "raw_content_fingerprints": out["raw_content_fingerprints"],
        "provenance_verified": bool(prov.get("verified")),
        "provenance_current_era": bool(prov.get("current_era")),
        "provider_provenance": out.get("provider_provenance"),
        "strategy_validation_allowed": out["strategy_validation_allowed"],
    }


def run_pilot(provider, *, symbols=DEFAULT_SYMBOLS, root: str = ".",
              windows: tuple[PilotWindow, ...] = PILOT_WINDOWS) -> dict:
    """Run every window. Returns an aggregate the graduation gate can read."""
    results = [run_window(w, provider, symbols=symbols, root=root) for w in windows]
    ok = [r for r in results if r["status"] == "OK"]
    totals = {
        "windows": len(results),
        "windows_ok": len(ok),
        "windows_failed": len(results) - len(ok),
        "requested_symbol_dates": sum(r.get("requested_symbol_dates", 0) for r in ok),
        "sessions_reconciled": sum(r.get("sessions_reconciled", 0) for r in ok),
        "sessions_admitted": sum(r.get("sessions_admitted", 0) for r in ok),
        "sessions_rejected": sum(r.get("sessions_rejected", 0) for r in ok),
        "sessions_not_trading": sum(r.get("sessions_not_trading", 0) for r in ok),
        "bars_admitted": sum(r.get("bars_admitted", 0) for r in ok),
        "feature_observations": sum(r.get("feature_observations", 0) for r in ok),
    }
    breakdown: dict[str, int] = {}
    for r in ok:
        for k, v in r.get("rejection_breakdown", {}).items():
            breakdown[k] = breakdown.get(k, 0) + v

    # Every requested symbol-date must be accounted for exactly once. A pilot
    # that silently dropped one would prove the opposite of what it claims.
    accounted = all(
        r["sessions_reconciled"] == r["requested_symbol_dates"] for r in ok)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_module": "intraday_lab.pilot",
        "observe_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbols": list(sorted(symbols)),
        "totals": totals,
        "rejection_breakdown": breakdown,
        "every_requested_session_accounted_for": accounted,
        "all_windows_provenance_verified": all(r["provenance_verified"] for r in ok),
        "all_windows_current_era": all(r["provenance_current_era"] for r in ok),
        "windows": results,
        "active_corpus": MG.active_corpus(root=root),
        "strategy_validation_allowed": False,
    }


# The lab's run mode. Deliberately NOT `historical_replay`, despite the name
# fitting: that mode is `priority: low`, and the governor SKIPS low-priority
# calls when the monthly bandwidth guard trips — returning `[]`, which
# `fetch_status` classifies as NO_DATA and the reconciler then records as
# REJECTED_MISSING_BARS. That would be OUR refusal masquerading as absent market
# data, the exact conflation §16 exists to prevent, and it would be indelibly
# written into immutable research evidence.
#
# An undeclared mode resolves to {call_budget: 0 (uncapped), priority: medium},
# for which BOTH skip paths are unreachable: `should_skip` requires
# priority == "low", and `over_run_budget` returns False when the budget is 0.
# So an empty response can only ever mean the provider genuinely returned
# nothing. The governor's other protections are unaffected — the token bucket,
# the usage ledger and the kill-switch all still apply. Research volume is tiny
# (one call per symbol per window) against a flat-rate subscription.
INTRADAY_RESEARCH_RUN_MODE = "intraday_research"


def governed_fmp_provider(*, run_mode: str = INTRADAY_RESEARCH_RUN_MODE,
                          ttl_seconds: int = 24 * 3600):
    """The sanctioned FMP path.

    Goes through `data_budget.factory.governed_client`, the repo's single entry
    point, rather than constructing `FMPClient` directly — a direct construction
    bypasses the budget governor, the token bucket and the 20GB guard, and is
    refused by `tests/test_data_budget_no_direct_construction.py`. Research
    traffic has no business being the one exception to that rule.
    """
    from portfolio_automation.data_budget.factory import governed_client
    from portfolio_automation.intraday_lab.providers import GovernedFMPIntradayProvider

    return GovernedFMPIntradayProvider(governed_client(run_mode=run_mode),
                                       ttl_seconds=ttl_seconds)
