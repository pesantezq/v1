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

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone

from portfolio_automation.intraday_lab import identity as ID
from portfolio_automation.intraday_lab import migration as MG
from portfolio_automation.intraday_lab import pipeline as PL
from portfolio_automation.intraday_lab import storage as ST
from portfolio_automation.intraday_lab.dataset import (
    DatasetRequest, _calendar_identity,
)

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
                "transition on 03-08; spans the 03-09 and 03-12 market-wide "
                "circuit-breaker days"),
    PilotWindow("2020-covid-halts", date(2020, 3, 16), date(2020, 3, 18),
                "the other two 2020 market-wide circuit-breaker days (03-16, "
                "03-18) — included so the halted-session population is "
                "measured rather than inferred from two examples"),
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


def planned_provider_calls(symbols, windows) -> int:
    """One call per symbol per window — the pilot's entire provider footprint."""
    return len(list(symbols)) * len(list(windows))


def budget_headroom(symbols=DEFAULT_SYMBOLS,
                    windows: tuple[PilotWindow, ...] = PILOT_WINDOWS,
                    *, run_mode: str = None) -> dict:
    """Check the run will fit inside its registered budget BEFORE calling out.

    Registering a real budget makes the governor able to refuse mid-run, and a
    governor refusal returns `[]` — which this lab's `fetch_status` reads as
    NO_DATA and then writes into IMMUTABLE evidence as REJECTED_MISSING_BARS.
    Pre-flighting converts that silent, permanent corruption into a loud refusal
    to start. It is the reason a real budget is safe to impose at all.
    """
    from portfolio_automation.data_budget.scheduler import (
        DEFAULT_RUN_MODES, RunModeScheduler,
    )

    mode = run_mode or INTRADAY_RESEARCH_RUN_MODE
    sched = RunModeScheduler(DEFAULT_RUN_MODES)
    planned = planned_provider_calls(symbols, windows)
    budget = sched.call_budget(mode)
    fits = budget == 0 or planned < budget
    return {"run_mode": mode, "planned_calls": planned, "call_budget": budget,
            "priority": sched.priority(mode), "fits": fits,
            "reason": None if fits else (
                f"pilot would issue {planned} provider calls against a "
                f"{budget}-call budget for run_mode {mode!r}; the governor would "
                f"skip the excess and those skips would be recorded as absent "
                f"market data")}


def run_pilot(provider, *, symbols=DEFAULT_SYMBOLS, root: str = ".",
              windows: tuple[PilotWindow, ...] = PILOT_WINDOWS) -> dict:
    """Run every window. Returns an aggregate the graduation gate can read."""
    head = budget_headroom(symbols, windows)
    if not head["fits"]:
        raise RuntimeError(head["reason"])
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
        # Stamped unconditionally; whether it is JUSTIFIED is decided by
        # check_graduation_protocol against the windows actually run. Claiming
        # the protocol does not confer it.
        "graduation_protocol_id": GRADUATION_PROTOCOL_ID,
        # The calendar the pilot ran under, so its evidence stays interpretable
        # after a calendar upgrade instead of silently re-reading today's.
        "calendar_identity": _calendar_identity(),
        "totals": totals,
        "rejection_breakdown": breakdown,
        "every_requested_session_accounted_for": accounted,
        "all_windows_provenance_verified": all(r["provenance_verified"] for r in ok),
        "all_windows_current_era": all(r["provenance_current_era"] for r in ok),
        "windows": results,
        "active_corpus": MG.active_corpus(root=root),
        "strategy_validation_allowed": False,
    }


# The lab's run mode, REGISTERED in data_budget.scheduler.DEFAULT_RUN_MODES.
#
# It is deliberately not `historical_replay`, despite the name fitting: that
# mode is `priority: low`, and the governor SKIPS low-priority calls when the
# bandwidth guard trips — returning `[]`, which `fetch_status` classifies as
# NO_DATA and the reconciler then writes into IMMUTABLE evidence as
# REJECTED_MISSING_BARS. That would record our own refusal as absent market
# data, permanently.
#
# An earlier version left this mode UNREGISTERED and relied on the unknown-mode
# fallback to supply medium priority. That worked, but depending on the default
# for absent keys is not a policy — any change to the fallback would silently
# rewrite the lab's governance. It now carries an explicit, intentional budget.
INTRADAY_RESEARCH_RUN_MODE = "intraday_research"


# ── Durable pilot evidence ─────────────────────────────────────────────────
# A graduation verdict that lives only in a caller's in-memory dict does not
# survive a process exit, a Claude session, or a reboot — the artifact on disk
# would still SAY ready while a fresh process recomputed LIMITED. So the pilot
# is persisted as a content-addressed immutable object and the verdict is
# always re-derived from it.

PILOT_IDENTITY_SCHEMA = "intraday_pilot_v1"

# ── The graduation protocol ────────────────────────────────────────────────
# A generic "valid pilot" is not graduation evidence. Without this, an operator
# could point graduation at a perfectly valid but far weaker pilot — one normal
# 2026 week — and it would verify, silently discarding the historical regimes
# Session 2 exists to certify. The protocol freezes the minimum evidence that
# must have been EXERCISED; it deliberately does not freeze the RESULTS, which
# stay measured (admissions and bar counts are outcomes, not requirements).
GRADUATION_PROTOCOL_ID = "INTRADAY_GRADUATION_PILOT_V1"
GRADUATION_REQUIRED_TIMEFRAME = "5min"
GRADUATION_REQUIRED_SYMBOLS: frozenset[str] = frozenset({"SPY", "AAPL"})
GRADUATION_REQUIRED_WINDOWS: dict[str, tuple[str, str]] = {
    "2017-independence": ("2017-07-03", "2017-07-07"),
    "2020-covid-vol":    ("2020-03-09", "2020-03-13"),
    "2020-covid-halts":  ("2020-03-16", "2020-03-18"),
    "2022-juneteenth":   ("2022-06-17", "2022-06-23"),
    "2023-fall-dst":     ("2023-11-01", "2023-11-06"),
    "2024-thanksgiving": ("2024-11-27", "2024-12-02"),
    "2025-thanksgiving": ("2025-11-26", "2025-12-01"),
    "2026-normal":       ("2026-08-03", "2026-08-07"),
}

# EXTRA-WINDOW POLICY: required ⊆ observed. A future pilot may ADD adversarial
# windows without minting a new protocol, because more evidence never weakens
# the standard. Required coverage can only ever shrink by an explicit, reviewable
# edit here — which, because the protocol id is part of pilot identity, also
# invalidates every pilot minted under the old one.
GRADUATION_EXTRA_WINDOW_POLICY = "required_subset_of_observed"

# Keys excluded from the persisted pilot object: they describe the world at the
# moment the pilot ran, not what the pilot IS. `active_corpus` in particular
# changes as unrelated datasets are added, which would make an unchanged pilot
# mint a new identity on every rebuild.
_NON_CONTENT_KEYS = frozenset({"generated_at", "active_corpus"})


def pilot_identity_payload(pilot: dict) -> dict:
    """The canonical projection that DEFINES a pilot's identity.

    Binds to research meaning — window definitions, symbols, provider identity,
    calendar identity, the resulting graph identities and the accounting
    outcome. Deliberately excludes when it ran and anything about the wider
    corpus.
    """
    windows = []
    for w in pilot.get("windows", []):
        windows.append({
            "label": w.get("label"), "status": w.get("status"),
            "start": w.get("start"), "end": w.get("end"),
            "rationale": w.get("rationale"), "symbols": w.get("symbols"),
            "requested_symbol_dates": w.get("requested_symbol_dates"),
            "sessions_reconciled": w.get("sessions_reconciled"),
            "sessions_admitted": w.get("sessions_admitted"),
            "sessions_rejected": w.get("sessions_rejected"),
            "sessions_not_trading": w.get("sessions_not_trading"),
            "rejection_breakdown": w.get("rejection_breakdown"),
            "bars_admitted": w.get("bars_admitted"),
            "feature_observations": w.get("feature_observations"),
            "dataset_fingerprint": w.get("dataset_fingerprint"),
            "manifest_fingerprint": w.get("manifest_fingerprint"),
            "feature_fingerprint": w.get("feature_fingerprint"),
            "raw_content_fingerprints": sorted(w.get("raw_content_fingerprints") or []),
            "provider_provenance": w.get("provider_provenance"),
        })
    windows.sort(key=lambda w: (w["label"] or "", w["start"] or ""))
    return {
        "schema": PILOT_IDENTITY_SCHEMA,
        "pilot_schema_version": SCHEMA_VERSION,
        # The protocol is part of research MEANING: the same results gathered
        # under a different (or absent) protocol are different evidence, so they
        # must not share an identity. Without this, a generic pilot could be
        # relabelled as graduation evidence after the fact.
        "graduation_protocol_id": pilot.get("graduation_protocol_id"),
        "symbols": sorted(pilot.get("symbols") or []),
        "calendar_identity": pilot.get("calendar_identity"),
        "windows": windows,
        "strategy_validation_allowed": pilot.get("strategy_validation_allowed", False),
    }


def _verified_window_timeframe(window: dict, *, root: str) -> str | None:
    """The timeframe a window's dataset was actually REQUESTED at.

    Read from the persisted request manifest inside the verified research
    graph, not from `provider_provenance`. Provider provenance describes
    ACQUISITION and is optional — the governed FMP provider does not even emit
    a `timeframe` key — so the previous check read `None` on every real window
    and passed vacuously. The dataset request is what defines research meaning.
    """
    mfp = window.get("manifest_fingerprint")
    if not mfp:
        return None
    req = ST.read_snapshot(ST.DATASET_MANIFESTS, mfp, "request_manifest.json",
                           root=root)
    if not isinstance(req, dict):
        return None
    return req.get("timeframe")


def check_graduation_protocol(pilot: dict, *, root: str = ".") -> dict:
    """Does this pilot exercise the minimum Session 2 certification scope?

    Deliberately SEPARATE from integrity. A smaller pilot can be a perfectly
    valid research object and still not be graduation evidence; calling it
    corrupt would be false, and calling it sufficient would be worse.
    """
    failures: list[str] = []
    if pilot.get("graduation_protocol_id") != GRADUATION_PROTOCOL_ID:
        failures.append(
            f"pilot declares protocol {pilot.get('graduation_protocol_id')!r}, "
            f"required {GRADUATION_PROTOCOL_ID!r}")
    symbols = set(pilot.get("symbols") or [])
    missing_syms = GRADUATION_REQUIRED_SYMBOLS - symbols
    if missing_syms:
        failures.append(f"missing required symbol(s): {sorted(missing_syms)}")

    observed = {w.get("label"): w for w in pilot.get("windows") or []}
    for label, (start, end) in sorted(GRADUATION_REQUIRED_WINDOWS.items()):
        w = observed.get(label)
        if w is None:
            failures.append(f"missing required window {label!r} ({start}..{end})")
            continue
        if (w.get("start"), w.get("end")) != (start, end):
            failures.append(
                f"window {label!r} covers {w.get('start')}..{w.get('end')}, "
                f"required {start}..{end}")
        wsyms = set(w.get("symbols") or [])
        if GRADUATION_REQUIRED_SYMBOLS - wsyms:
            failures.append(f"window {label!r} omits required symbol(s): "
                            f"{sorted(GRADUATION_REQUIRED_SYMBOLS - wsyms)}")
        # PROVEN from the verified graph, and absence is a FAILURE. Treating a
        # missing timeframe as equivalent to 5min is how a protocol claim
        # becomes decorative.
        tf = _verified_window_timeframe(w, root=root)
        if tf is None:
            failures.append(f"window {label!r} has no verifiable request "
                            f"timeframe in its dataset manifest")
        elif tf != GRADUATION_REQUIRED_TIMEFRAME:
            failures.append(f"window {label!r} was requested at {tf!r}, "
                            f"required {GRADUATION_REQUIRED_TIMEFRAME!r}")
    return {
        "satisfied": not failures,
        "protocol_id": GRADUATION_PROTOCOL_ID,
        "required_timeframe": GRADUATION_REQUIRED_TIMEFRAME,
        "required_symbols": sorted(GRADUATION_REQUIRED_SYMBOLS),
        "required_windows": {k: list(v) for k, v in
                             sorted(GRADUATION_REQUIRED_WINDOWS.items())},
        "extra_window_policy": GRADUATION_EXTRA_WINDOW_POLICY,
        "observed_window_count": len(observed),
        "failures": failures,
    }


def pilot_fingerprint(pilot: dict) -> str:
    return ST.content_hash(pilot_identity_payload(pilot))


def _recompute_totals(windows: list[dict]) -> dict:
    ok = [w for w in windows if w.get("status") == "OK"]
    return {
        "windows": len(windows),
        "windows_ok": len(ok),
        "windows_failed": len(windows) - len(ok),
        "requested_symbol_dates": sum(w.get("requested_symbol_dates", 0) for w in ok),
        "sessions_reconciled": sum(w.get("sessions_reconciled", 0) for w in ok),
        "sessions_admitted": sum(w.get("sessions_admitted", 0) for w in ok),
        "sessions_rejected": sum(w.get("sessions_rejected", 0) for w in ok),
        "sessions_not_trading": sum(w.get("sessions_not_trading", 0) for w in ok),
        "bars_admitted": sum(w.get("bars_admitted", 0) for w in ok),
        "feature_observations": sum(w.get("feature_observations", 0) for w in ok),
    }


def persist_pilot(pilot: dict, *, root: str = ".") -> str:
    """Freeze a pilot result as immutable content-addressed evidence."""
    body = {k: v for k, v in pilot.items() if k not in _NON_CONTENT_KEYS}
    fp = pilot_fingerprint(pilot)
    ST.write_snapshot(ST.PILOTS, fp, {
        "pilot.json": body,
        "pilot_manifest.json": {
            "schema_version": SCHEMA_VERSION,
            "storage_schema": ID.STORAGE_SCHEMA,
            "identity_schema": PILOT_IDENTITY_SCHEMA,
            "pilot_fingerprint": fp,
            "window_count": len(body.get("windows") or []),
            "symbols": sorted(body.get("symbols") or []),
            "manifest_fingerprints": sorted(
                w["manifest_fingerprint"] for w in body.get("windows") or []
                if w.get("manifest_fingerprint")),
            "totals": _recompute_totals(body.get("windows") or []),
        },
    }, root=root)
    # WHEN it ran is an event fact, never inside the content object.
    ST.write_snapshot(ST.PILOT_EVENTS, ST.content_hash(
        {"pilot_fingerprint": fp, "generated_at": pilot.get("generated_at")}),
        {"pilot_event.json": {
            "schema_version": SCHEMA_VERSION,
            "pilot_fingerprint": fp,
            "generated_at": pilot.get("generated_at"),
        }}, root=root)
    return fp


def verify_historical_pilot(fingerprint: str, *, root: str = ".") -> dict:
    """Verify persisted pilot evidence. Stored totals are never trusted.

    Everything the graduation gate relies on is recomputed here from the
    persisted window rows and re-verified against the persisted graphs — a
    pilot that merely SAYS it accounted for everything proves nothing.
    """
    def fail(reason: str, **extra) -> dict:
        return {"verified": False, "reason": reason,
                "pilot_fingerprint": fingerprint, **extra}

    body = ST.read_snapshot(ST.PILOTS, fingerprint, "pilot.json", root=root)
    man = ST.read_snapshot(ST.PILOTS, fingerprint, "pilot_manifest.json", root=root)
    if body is None or man is None:
        return fail("missing pilot.json or pilot_manifest.json")
    if man.get("pilot_fingerprint") != fingerprint:
        return fail("pilot manifest declares a different fingerprint")
    if pilot_fingerprint(body) != fingerprint:
        return fail("persisted pilot does not hash to its identity — modified")

    windows = body.get("windows") or []
    if not windows:
        return fail("pilot contains no windows")
    failed = [w.get("label") for w in windows if w.get("status") != "OK"]
    if failed:
        return fail(f"pilot windows did not complete: {failed}")

    # Totals are RECOMPUTED, never read.
    totals = _recompute_totals(windows)
    stored = body.get("totals") or {}
    mismatched = {k: (stored.get(k), v) for k, v in totals.items()
                  if k in stored and stored[k] != v}
    if mismatched:
        return fail(f"stored pilot totals do not recompute: {mismatched}")

    # Every requested symbol-date accounted for, per window.
    for w in windows:
        if w.get("sessions_reconciled") != w.get("requested_symbol_dates"):
            return fail(f"window {w.get('label')} reconciled "
                        f"{w.get('sessions_reconciled')} of "
                        f"{w.get('requested_symbol_dates')} requested items")

    # Every referenced graph must still verify AND be current-era — and so must
    # every referenced FEATURE object. Verifying only datasets while claiming
    # DATASET_FEATURE_FOUNDATION_READY meant a pilot-referenced feature snapshot
    # could be deleted outright and graduation stayed READY.
    graphs, window_reports = {}, []
    for w in windows:
        label = w.get("label")
        mfp = w.get("manifest_fingerprint")
        if not mfp:
            return fail(f"window {label} references no dataset manifest")
        v = ST.verify_dataset_provenance(mfp, root=root)
        graphs[mfp] = {"verified": v.get("verified"),
                       "current_era": v.get("current_era")}
        if not v.get("verified"):
            return fail(f"window {label} manifest {mfp} failed provenance "
                        f"verification: {v.get('reason')}", graphs=graphs)
        if not v.get("current_era"):
            return fail(f"window {label} manifest {mfp} is not current-era: "
                        f"{v.get('not_current_reason')}", graphs=graphs)
        if v.get("canonical_content_fingerprint") != w.get("dataset_fingerprint"):
            return fail(f"window {label} names a dataset its manifest "
                        f"does not reference")

        # Acquisition lineage. Strict here: a current graduation manifest claims
        # governed real-data acquisition, so absent build/acquisition evidence
        # must fail rather than pass by vacuous truth.
        acq = ST.verify_manifest_acquisitions(mfp, root=root, require_evidence=True)
        if not acq["verified"]:
            return fail(f"window {label} acquisition lineage failed: {acq['reason']}")

        ffp = w.get("feature_fingerprint")
        if not ffp:
            return fail(f"window {label} references no feature snapshot")
        fv = ST.verify_feature_snapshot(ffp, root=root)
        if not fv.get("verified"):
            return fail(f"window {label} feature snapshot {ffp} failed "
                        f"verification: {fv.get('reason')}")
        if fv.get("source_dataset_fingerprint") != w.get("dataset_fingerprint"):
            return fail(f"window {label} feature {ffp} binds to dataset "
                        f"{fv.get('source_dataset_fingerprint')}, not the "
                        f"window's {w.get('dataset_fingerprint')}")
        if fv.get("source_dataset_manifest_fingerprint") != mfp:
            return fail(f"window {label} feature {ffp} binds to manifest "
                        f"{fv.get('source_dataset_manifest_fingerprint')}, not "
                        f"the window's {mfp}")
        # The stored count is a claim; the verified object is the evidence.
        if fv.get("observation_count") != w.get("feature_observations"):
            return fail(f"window {label} claims {w.get('feature_observations')} "
                        f"feature observations but the verified snapshot holds "
                        f"{fv.get('observation_count')}")

        window_reports.append({
            "label": label,
            "dataset_fingerprint": w.get("dataset_fingerprint"),
            "manifest_fingerprint": mfp,
            "feature_fingerprint": ffp,
            "dataset_provenance_verified": True,
            "dataset_current_era": True,
            "acquisition_evidence_verified": True,
            "acquisition_events": acq["acquisitions"],
            "feature_verified": True,
            "feature_dataset_binding_verified": True,
            "feature_manifest_binding_verified": True,
            "feature_observation_count_verified": True,
            "feature_observations": fv.get("observation_count"),
        })

    if body.get("strategy_validation_allowed") is not False:
        return fail("pilot does not assert strategy_validation_allowed=false")

    protocol = check_graduation_protocol(body, root=root)
    return {
        # INTEGRITY. A structurally sound pilot — true even for a pilot too
        # small to graduate, which is why the two are reported separately.
        "verified": True, "reason": None,
        "pilot_integrity_valid": True,
        "graduation_protocol_satisfied": protocol["satisfied"],
        "graduation_protocol": protocol,
        "pilot_fingerprint": fingerprint,
        "graduation_protocol_id": body.get("graduation_protocol_id"),
        "totals": totals,
        "window_count": len(windows),
        "symbols": sorted(body.get("symbols") or []),
        "manifest_fingerprints": sorted(graphs),
        "graphs": graphs,
        "window_verification": window_reports,
        "calendar_identity": body.get("calendar_identity"),
        "every_requested_session_accounted_for": True,
        "all_windows_provenance_verified": True,
        "all_windows_current_era": True,
        "all_windows_features_verified": True,
        "windows": windows,
        "strategy_validation_allowed": False,
    }


def set_graduation_evidence(fingerprint: str, *, root: str = ".") -> dict:
    """Point Session 2 graduation at an immutable pilot object.

    The pointer is the ONLY mutable element of the store, and it never copies
    the evidence it names. Selecting evidence by "newest directory" or file
    mtime would make the verdict depend on filesystem incidentals rather than
    an explicit, reviewable decision.
    """
    v = verify_historical_pilot(fingerprint, root=root)
    if not v["verified"]:
        raise ValueError(f"refusing to point graduation at unverifiable pilot "
                         f"{fingerprint}: {v['reason']}")
    if not v.get("graduation_protocol_satisfied"):
        # Integrity and sufficiency are different questions. This pilot may be a
        # perfectly sound research object; it simply does not exercise the
        # evidence Session 2 certifies, and pointing graduation at it would
        # silently lower the standard.
        raise ValueError(
            f"pilot {fingerprint} is structurally valid but does NOT satisfy "
            f"{GRADUATION_PROTOCOL_ID}: "
            f"{v['graduation_protocol']['failures']}")
    path = ST.intraday_root(root) / ST.GRADUATION_POINTER
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_module": "intraday_lab.pilot",
        "observe_only": True,
        "pilot_fingerprint": fingerprint,
        "pilot_identity_schema": PILOT_IDENTITY_SCHEMA,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "note": "Immutable pointer target; graduation is always RE-VERIFIED "
                "from this object, never cached.",
    }
    path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    return payload


def load_graduation_evidence(*, root: str = ".") -> dict:
    """Locate and verify the durable graduation evidence. No provider calls.

    Returns `{"pilot": <verified pilot>, ...}` or a reason it is unavailable.
    Never re-runs the pilot: a missing pointer is a governance fact to report,
    not a licence to spend provider budget re-manufacturing evidence.
    """
    path = ST.intraday_root(root) / ST.GRADUATION_POINTER
    if not path.exists():
        return {"available": False, "pilot": None,
                "reason": "no graduation evidence pointer on disk "
                          f"({ST.GRADUATION_POINTER})"}
    try:
        pointer = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": False, "pilot": None,
                "reason": f"graduation pointer unreadable: {type(exc).__name__}"}
    fp = pointer.get("pilot_fingerprint")
    if not fp:
        return {"available": False, "pilot": None,
                "reason": "graduation pointer names no pilot_fingerprint"}
    v = verify_historical_pilot(fp, root=root)
    if not v["verified"]:
        return {"available": False, "pilot": None, "pilot_fingerprint": fp,
                "pilot_integrity_valid": False,
                "graduation_protocol_satisfied": False,
                "reason": f"graduation evidence {fp} failed verification: "
                          f"{v['reason']}"}
    # A POINTER IS SELECTION, NOT AUTHORITY.
    #
    # The pointer is deliberately the one mutable element of the store, so the
    # gate must never assume it was written by the approved setter. An operator
    # or an interrupted process can point it at any pilot; `set_graduation_
    # evidence` refused an insufficient one, but this READ path did not, and a
    # hand-written pointer to a one-window pilot produced READY / SESSION_3_GO.
    # Every dereference therefore re-enforces the same admission contract.
    if not v.get("graduation_protocol_satisfied"):
        return {"available": False, "pilot": None, "pilot_fingerprint": fp,
                # Not corrupt — sound research evidence that is simply not
                # sufficient. Conflating the two would be its own falsehood.
                "pilot_integrity_valid": True,
                "graduation_protocol_satisfied": False,
                "graduation_protocol": v.get("graduation_protocol"),
                "reason": f"pilot {fp} is valid research evidence but does not "
                          f"satisfy the graduation protocol "
                          f"{GRADUATION_PROTOCOL_ID}: "
                          f"{(v.get('graduation_protocol') or {}).get('failures')}"}
    return {"available": True, "pilot": v, "pilot_fingerprint": fp,
            "pilot_integrity_valid": True,
            "graduation_protocol_satisfied": True,
            "pointer": pointer, "reason": None}


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
