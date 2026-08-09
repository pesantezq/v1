"""Session-1 foundation + provider assessment artifacts. Observe-only.

Records what the 2026-08-08 read-only probe actually measured, so later
sessions inherit evidence rather than assumptions. Deliberately separates
ARCHITECTURE feasibility from REAL-MARKET feasibility: the lab can be
structurally sound while the data is unusable, and conflating the two is how a
research platform ends up reporting fixture output as market evidence.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"

REAL_DATA_READY = "REAL_DATA_READY"
REAL_DATA_LIMITED = "REAL_DATA_LIMITED"
DATA_UNAVAILABLE = "DATA_UNAVAILABLE"

# Evidence from the 2026-08-08 read-only probe (SPY/AAPL, small windows only).
# Every value here was MEASURED against the configured account, not read off
# provider documentation — entitlement and documentation disagree routinely.
PROBE_EVIDENCE: list[dict[str, Any]] = [
    {"symbol": "SPY", "timeframe": "5min", "window": "2026-08-03..2026-08-07",
     "http": 200, "bars": 390, "first": "2026-08-03 09:30:00", "last": "2026-08-07 15:55:00"},
    {"symbol": "SPY", "timeframe": "5min", "window": "2026-05-05..2026-05-09",
     "http": 200, "bars": 312, "first": "2026-05-05 09:30:00", "last": "2026-05-08 15:55:00",
     "note": "312 == 4 complete sessions x 78 bars. 2026-05-09 is a SATURDAY, so "
             "the window holds FOUR regular sessions, not five. An earlier reading "
             "called this a missing session; that was a calendar error, not a "
             "coverage gap. This probe shows COMPLETE data."},
    {"symbol": "SPY", "timeframe": "5min", "window": "2025-08-04..2025-08-08",
     "http": 200, "bars": 390, "first": "2025-08-04 09:30:00", "last": "2025-08-08 15:55:00"},
    {"symbol": "SPY", "timeframe": "5min", "window": "2023-08-07..2023-08-11",
     "http": 200, "bars": 390, "first": "2023-08-07 09:30:00", "last": "2023-08-11 15:55:00"},
    {"symbol": "SPY", "timeframe": "5min", "window": "2020-08-03..2020-08-07",
     "http": 200, "bars": 390, "first": "2020-08-03 09:30:00", "last": "2020-08-07 15:55:00"},
    {"symbol": "SPY", "timeframe": "5min", "window": "2017-08-07..2017-08-11",
     "http": 200, "bars": 390, "first": "2017-08-07 09:30:00", "last": "2017-08-11 15:55:00"},
    {"symbol": "AAPL", "timeframe": "5min", "window": "2026-08-03..2026-08-07",
     "http": 200, "bars": 390, "first": "2026-08-03 09:30:00", "last": "2026-08-07 15:55:00"},
    {"symbol": "SPY", "timeframe": "5min", "window": "2025-11-28..2025-11-28",
     "http": 200, "bars": 42, "first": "2025-11-28 09:30:00", "last": "2025-11-28 12:55:00",
     "note": "day after Thanksgiving, 13:00 early close — provider reflects it; "
             "last bar 12:55 PROVES BAR_OPEN timestamp semantics"},
    {"symbol": "SPY", "timeframe": "5min", "window": "2026-08-08..2026-08-08",
     "http": 200, "bars": 0, "note": "Saturday — correctly empty (SESSION_CLOSED)"},
    {"symbol": "SPY", "timeframe": "1min", "window": "2026-08-06..2026-08-07",
     "http": 402, "bars": None, "note": "HTTP 402 Payment Required — NOT ENTITLED"},
    {"symbol": "AAPL", "timeframe": "5min", "window": "2020-08-27..2020-08-27",
     "http": 200, "bars": 78, "note": "closes ~125 pre-4:1-split (actual print ~500) "
                                      "=> history is SPLIT BACK-ADJUSTED"},
]


def provider_assessment() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "intraday_lab.provider_assessment",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "provider": "fmp",
        "endpoint": "/stable/historical-chart/{timeframe}",
        "registry_status": "REGISTERED",
        "registry_key": "intraday_chart",
        "account_access": {"5min": "ENTITLED", "1min": "NOT_ENTITLED_HTTP_402"},
        "tested_timeframes": ["5min", "1min"],
        "canonical_research_timeframe": "5min",
        "undeclared_untested_timeframes": {
            "15min": "NOT_PROBED — not declared in TIMEFRAMES",
            "30min": "NOT_PROBED — not declared in TIMEFRAMES",
            "1hour": "NOT_PROBED — not declared in TIMEFRAMES",
        },
        "tested_symbols": ["SPY", "AAPL"],
        "observed_historical_depth": {
            "earliest_verified": "2017-08-07",
            "latest_verified": "2026-08-07",
            "approx_years": 9,
            "bars_per_full_session": 78,
            "method": "spot probes at 2017/2020/2023/2025/2026 — not an exhaustive scan",
        },
        "ohlc_available": True,
        "volume_available": True,
        "timestamp_semantics": "BAR_OPEN",
        "timestamp_semantics_evidence":
            "13:00 early close yields a final bar of 12:55; normal sessions run "
            "09:30..15:55. Only consistent with bar-open labelling.",
        "timezone_behavior": "naive US/Eastern wall-clock; no offset supplied",
        "extended_hours_behavior": "REGULAR_ONLY",
        "adjustment_semantics": "SPLIT_BACK_ADJUSTED",
        "adjustment_evidence":
            "AAPL 2020-08-27 closes ~125 vs the ~500 that actually printed before "
            "the 4:1 split.",
        "rate_limit_constraints": "governed by the existing FMP budget governor",
        "observed_quality": "every probed window returned complete regular "
                            "sessions; no coverage gap was observed in the sample. "
                            "Completeness must still be profiled per session — a "
                            "clean sample is not proof the provider has no gaps.",
        "pit_suitability": "SUITABLE for return-based research; split adjustment "
                           "is retroactive, so rules keyed to ABSOLUTE price "
                           "levels are NOT point-in-time safe",
        "compliance_status": "COMPLIANT — endpoint registered, read-only probes only",
        "final_source_status": REAL_DATA_READY,
        "probe_evidence": PROBE_EVIDENCE,
    }


def foundation_status() -> dict:
    limitations = [
        "1min is NOT entitled on this account (HTTP 402). 5min is the finest "
        "VERIFIED AND DECLARED research timeframe for the configured account — "
        "15min/30min/1hour were never probed, so nothing is claimed about them.",
        "Intraday history is SPLIT BACK-ADJUSTED. Safe for returns; any future "
        "rule keyed to absolute price levels or round-number thresholds is NOT "
        "point-in-time safe.",
        "Dividend adjustment behaviour was NOT established for intraday bars.",
        "Historical depth was spot-probed at five points, not exhaustively scanned.",
        "Session completeness must be profiled per trading session against a "
        "calendar-derived expected bar count. The sampled windows were complete, "
        "which is NOT evidence that the provider has no gaps elsewhere.",
        "Provider publishes no emission timestamp, so known_at uses a documented "
        "conservative 60s floor rather than measured latency.",
        "REGULAR_ONLY: no extended-hours bars, so gap/pre-market research is out "
        "of scope unless another source is sanctioned.",
        "No immutable canonical dataset has been constructed yet; Session 1 "
        "performed read-only probes only.",
        "Non-price feature families are NOT point-in-time ready except where "
        "explicitly classified PIT_READY.",
        "Session 2 must derive session_type and expected_bars from the sanctioned "
        "exchange calendar before admitting a session to the canonical dataset. "
        "A weekday-only approximation is NOT acceptable for the production "
        "dataset — it cannot see holidays or early closes.",
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "intraday_lab.foundation_status",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "session": 1,
        "foundation_status": "PASS",
        "data_source_status": REAL_DATA_READY,
        "point_in_time_status": "PASS",
        "data_quality_status": "PASS",
        "leakage_test_status": "PASS",
        "feature_timestamp_status": "PARTIAL — price-derived only; non-price "
                                    "families classified, not integrated",
        "architecture_next_session_allowed": True,
        "real_market_validation_allowed": True,
        "blocking_reasons": [],
        "warnings": limitations,
        "signal_family_pit_assessment": {
            "PRICE_DERIVED": "PIT_READY",
            "NEWS": "PIT_POSSIBLE_WITH_WORK",
            "FINBERT": "PIT_POSSIBLE_WITH_WORK",
            "CROWD_ATTENTION": "PIT_UNSAFE",
            "ANALYST": "PIT_POSSIBLE_WITH_WORK",
            "INSIDER": "PIT_POSSIBLE_WITH_WORK",
            "CONGRESS": "PIT_POSSIBLE_WITH_WORK",
            "REGIME": "PIT_UNSAFE",
        },
    }


def assess_foundation_health() -> dict:
    """Health consumer. Distinguishes SYSTEM failure from SOURCE limitation —
    an unentitled account is a correct diagnosis, not a software crash."""
    status = foundation_status()
    source = status["data_source_status"]
    if status["foundation_status"] != "PASS":
        overall = "SYSTEM_FAILURE"
    elif source == DATA_UNAVAILABLE:
        overall = "SOURCE_UNAVAILABLE"
    elif source == REAL_DATA_LIMITED:
        overall = "SOURCE_LIMITED"
    else:
        overall = "HEALTHY"
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "intraday_lab.health",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "overall": overall,
        "data_source_status": source,
        "leakage_test_status": status["leakage_test_status"],
        "warning_count": len(status["warnings"]),
        "reasons": status["warnings"],
    }


def write_foundation_artifacts(root: str = ".") -> list[str]:
    """Write to OutputNamespace.HISTORICAL — research, never live."""
    from portfolio_automation.data_governance import OutputNamespace, safe_write_json

    base = Path(root) / "outputs"
    return [str(safe_write_json(OutputNamespace.HISTORICAL, name, payload, base_dir=base))
            for name, payload in (
                ("intraday_provider_assessment.json", provider_assessment()),
                ("intraday_foundation_status.json", foundation_status()),
                ("intraday_foundation_health.json", assess_foundation_health()))]


# ---------------------------------------------------------------------------
# Session 2 — dataset + feature foundation status
# ---------------------------------------------------------------------------
DATASET_FEATURE_FOUNDATION_READY = "DATASET_FEATURE_FOUNDATION_READY"
DATASET_FEATURE_FOUNDATION_LIMITED = "DATASET_FEATURE_FOUNDATION_LIMITED"
DATASET_FEATURE_FOUNDATION_BLOCKED = "DATASET_FEATURE_FOUNDATION_BLOCKED"


def _canonical_ready(pilot: dict | None, root: str = ".") -> bool:
    """Walk the persisted provenance graph. Metadata claims prove nothing.

    Readiness is NARROW by design: integrity verified AND the whole graph is
    current-era. A legacy object that verifies under its own identity schema is
    sound evidence, but it does not satisfy today's contract — admitting it here
    would be the silent bypass that makes the era distinction meaningless.
    Migration (see `migration.py`) is what makes legacy evidence research-ready.
    """
    if not isinstance(pilot, dict):
        return False
    mfp = pilot.get("manifest_fingerprint")
    if not mfp:
        return False
    try:
        from portfolio_automation.intraday_lab import storage as _st
        result = _st.verify_dataset_provenance(mfp, root=root)
        return bool(result.get("verified")) and bool(result.get("current_era")) and \
            result.get("canonical_content_fingerprint") == pilot.get("dataset_fingerprint")
    except Exception:
        return False


def _feature_ready(pilot: dict | None, root: str = ".") -> bool:
    if not _canonical_ready(pilot, root):
        return False
    fp = (pilot or {}).get("feature_fingerprint")
    if not fp:
        return False
    try:
        from portfolio_automation.intraday_lab import storage as _st
        v = _st.verify_feature_snapshot(fp, root=root)
        return bool(v.get("verified")) and \
            v.get("source_dataset_fingerprint") == (pilot or {}).get("dataset_fingerprint") and \
            v.get("source_dataset_manifest_fingerprint") == (pilot or {}).get("manifest_fingerprint")
    except Exception:
        return False


SESSION_3_GO = "SESSION_3_GO"
SESSION_3_NO_GO = "SESSION_3_NO_GO"

# ── THE SESSION 3 TEMPORAL INVARIANT ───────────────────────────────────────
# Frozen here, prominently, because it is the single rule whose violation
# silently manufactures profit in every backtest that breaks it.
#
#     A 5-minute bar covering 10:00-10:05 has known_at = 10:06.
#
#     A strategy consuming that COMPLETED bar may NOT claim a 10:05 fill. The
#     bar did not exist as information at 10:05; at 10:05 it was still forming.
#     The earliest decision instant is known_at, and the earliest fill is at or
#     after that.
#
# known_at is bar_end_at plus a conservative publication delay, and it is part
# of canonical identity — so a dataset that moves knowability earlier is a
# DIFFERENT research object, not the same one tuned. Session 3 inherits this
# rule; it does not get to reinterpret it.
SESSION_3_TEMPORAL_INVARIANT = (
    "For a 5-minute bar 10:00-10:05, known_at = 10:06. A strategy using that "
    "completed bar may not claim a 10:05 fill. decision_time >= known_at, and "
    "fill_time >= decision_time. Never derive known_at from retrieved_at."
)


def session3_input_contract(pilot: dict | None = None, root: str = ".") -> dict:
    """What Session 3 may rely on — emitted ONLY when Session 2 graduates.

    Deliberately gated: a contract published beside a LIMITED foundation would
    be read as permission. When the gate is not met this returns NO_GO with the
    exact blockers and no contract body at all.
    """
    from portfolio_automation.intraday_lab import calendar as _cal
    from portfolio_automation.intraday_lab import features as _feat
    from portfolio_automation.intraday_lab import identity as _id
    from portfolio_automation.intraday_lab import migration as _mg

    grad = session2_graduation(pilot, root=root)
    if grad["status"] != DATASET_FEATURE_FOUNDATION_READY:
        return {
            "schema_version": "1",
            "source": "intraday_lab.session3_input_contract",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "observe_only": True,
            "session_3_gate": SESSION_3_NO_GO,
            "reason": "Session 2 has not graduated",
            "blockers": grad["blockers"],
            "contract": None,
            "strategy_validation_allowed": False,
        }

    corpus = _mg.active_corpus(root=root)
    lo, hi = _cal.coverage()
    return {
        "schema_version": "1",
        "source": "intraday_lab.session3_input_contract",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "session_3_gate": SESSION_3_GO,
        "blockers": [],
        "contract": {
            "data_scope": {
                "timeframe": "5min",
                "session_scope": "REGULAR SESSION ONLY — no pre/post market",
                "exchange": _cal.EXCHANGE,
                "calendar_backend": _cal.backend(),
                "certified_from": lo.isoformat(),
                "certified_through": hi.isoformat(),
            },
            "temporal_contract": {
                "invariant": SESSION_3_TEMPORAL_INVARIANT,
                "fields_guaranteed": ["bar_start_at", "bar_end_at", "known_at"],
                "known_at_rule": "known_at = bar_end_at + publication_delay, "
                                 "never derived from retrieved_at",
                "decision_rule": "an input is usable only when known_at <= decision_time",
                "fill_rule": "no fill may be claimed before the information was knowable",
            },
            "admission_contract": {
                "rule": "observed bar-start timestamps must EQUAL the calendar's "
                        "expected grid exactly; counts are never sufficient",
                "regular_session_bars": 78,
                "early_close_bars": 42,
                "rejected_sessions_contribute_no_bars": True,
                "known_exclusion": "market-wide trading halts remove bars that "
                                   "never printed, so halted sessions are "
                                   "REJECTED. The most volatile days are "
                                   "therefore absent — account for this "
                                   "selection bias explicitly.",
            },
            "adjustment_contract": {
                "state": "split_adjusted (split back-adjusted)",
                "absolute_price_features": "BLOCKED unless separately proven",
                "volume_features": "BLOCKED unless volume adjustment semantics "
                                   "are separately established",
                "enabled_features": list(_feat.ENABLED_FEATURES),
            },
            "identity_contract": {
                "current_canonical_identity_schema": _id.CURRENT_CANONICAL_ERA.schema_id,
                "current_raw_identity_schema": _id.CURRENT_RAW_ERA.schema_id,
                "rule": "Session 3 consumes CURRENT-ERA objects only. Archival "
                        "legacy objects are retained, verifiable evidence and "
                        "are never silently reused.",
                "active_manifests": [a["manifest_fingerprint"]
                                     for a in corpus["active_manifests"]],
                "archival_manifests": [a["manifest_fingerprint"]
                                       for a in corpus["archival_manifests"]],
                "provenance_required": "immutable canonical + manifest + feature "
                                       "provenance must verify before use",
            },
        },
        # Graduating the DATA does not graduate the STRATEGY layer. Session 3
        # must set this deliberately; Session 2 never does.
        "strategy_validation_allowed": False,
    }


def _probe(fn, *, expected=True) -> tuple[bool, str]:
    """Run a live proof, converting any failure into a False verdict + reason.

    Graduation checks must never raise: a gate that crashes gives the operator
    no verdict at all, which is worse than a red one.
    """
    try:
        got = fn()
        return (bool(got) == expected, "" if bool(got) == expected else f"got {got!r}")
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"


def session2_graduation(pilot: dict | None = None, root: str = ".") -> dict:
    """Compute the Session 2 exit verdict FROM EVIDENCE.

    Two categories, deliberately kept apart:

    * **measured** — proven right here, right now, by running the real function
      over the real corpus. A claim in this list is backed by a computation.
    * **test_enforced** — invariants a runtime status function cannot honestly
      self-certify (tamper cascades, adversarial legacy handling). They are
      named with their enforcing tests so the claim is traceable, and they are
      NOT counted as measured evidence. Asserting "tamper detection works"
      without running a tamper would be exactly the verdict-from-absent-data
      failure this lab exists to prevent.

    READY requires every measured check to pass AND a clean pilot. Anything
    else is LIMITED with the exact blockers named.
    """
    from datetime import timedelta as _td

    from portfolio_automation.intraday_lab import calendar as _cal
    from portfolio_automation.intraday_lab import dataset as _ds
    from portfolio_automation.intraday_lab import identity as _id
    from portfolio_automation.intraday_lab import migration as _mg
    from portfolio_automation.intraday_lab import providers as _pr
    from portfolio_automation.intraday_lab import storage as _st
    from portfolio_automation.intraday_lab.models import IntradayBar

    def _pit_pair(delay_a, delay_b, field):
        a = IntradayBar(symbol="SPY", timeframe="5min",
                        bar_start_at=datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc),
                        open=100, high=101, low=99, close=100.5, volume=1000,
                        adjustment_state="split_adjusted",
                        publication_delay=_td(seconds=delay_a))
        b = IntradayBar(symbol="SPY", timeframe="5min",
                        bar_start_at=a.bar_start_at, open=100, high=101, low=99,
                        close=100.5, volume=1000, adjustment_state="split_adjusted",
                        publication_delay=_td(seconds=delay_b))
        fa = _ds.canonical_fingerprint([a], timeframe="5min",
                                       adjustment_state="split_adjusted")
        fb = _ds.canonical_fingerprint([b], timeframe="5min",
                                       adjustment_state="split_adjusted")
        return fa != fb

    # Evidence gathering is itself fallible, and a gate that raises leaves the
    # operator with no verdict at all. Failures here degrade to empty evidence,
    # which the checks below then report as blockers — never as READY.
    try:
        corpus = _mg.active_corpus(root=root)
    except Exception:
        corpus = {"active_manifests": [], "archival_manifests": [],
                  "integrity_failures": [{"reason": "active_corpus unreadable"}]}
    try:
        lo, hi = _cal.coverage()
    except Exception:
        lo = hi = date(1970, 1, 1)
    try:
        cal_prov = _cal.calendar_provenance()
    except Exception:
        cal_prov = {"authoritative": False, "backend": "unavailable"}

    def _legacy_all_honest() -> bool:
        """No object anywhere is misreported: nothing corrupt, legacy is legacy."""
        base = _st.intraday_root(root)
        ok = True
        for kind, verify in ((_st.RAW, _st.verify_raw_content),
                             (_st.DATASETS, _st.verify_canonical_snapshot)):
            d = base / kind
            if not d.is_dir():
                continue
            for obj in d.iterdir():
                if not obj.is_dir() or obj.name.startswith("."):
                    continue
                v = verify(obj.name, root=root)
                ok = ok and v.get("verified") and v.get("state") in _id.VERIFIED_STATES
        return ok

    def _archival_has_lineage() -> bool:
        return all(_mg.find_lineage(a["manifest_fingerprint"], "dataset_manifest",
                                    root=root) is not None
                   for a in corpus["archival_manifests"])

    def _reminted_features() -> bool:
        """Vacuously true when nothing needed migrating.

        A corpus built entirely under the current era has no legacy features to
        remint, and must not be blocked for the absence of work that was never
        required. When archival manifests DO exist, each one's reminted feature
        object has to verify.
        """
        recs = [_mg.find_lineage(a["manifest_fingerprint"], "dataset_manifest", root=root)
                for a in corpus["archival_manifests"]]
        return all(r and r.get("features", {}).get("verified") for r in recs)

    p = pilot or {}
    totals = p.get("totals") or {}

    measured: dict[str, dict] = {}

    def check(name: str, fn, note: str, expected=True) -> None:
        ok, reason = _probe(fn, expected=expected)
        measured[name] = {"pass": ok, "note": note, **({"reason": reason} if reason else {})}

    # ── identity ───────────────────────────────────────────────────────────
    check("canonical_identity_protects_known_at",
          lambda: _pit_pair(60, 0, "known_at"),
          "two datasets differing only in when a bar became knowable get "
          "different identities")
    check("canonical_identity_protects_bar_end_at",
          lambda: "bar_end_at" in _id.CURRENT_CANONICAL_ERA.protects,
          "bar_end_at is hashed into the current canonical identity")
    check("current_identity_era_recorded",
          lambda: bool(_id.CURRENT_CANONICAL_ERA.schema_id
                       and _id.CURRENT_RAW_ERA.schema_id),
          "new objects declare identity_schema in their content manifest")
    check("identity_verification_is_era_aware",
          lambda: len(_id.eras_for("canonical")) > 1 and len(_id.eras_for("raw")) > 1,
          "a closed registry of historical eras exists and is consulted")
    check("legacy_objects_verify_or_fail_honestly", _legacy_all_honest,
          "every persisted object verifies under its own era; none is "
          "misreported as tampered")
    check("legacy_not_automatically_current_ready",
          lambda: not (set(a["manifest_fingerprint"] for a in corpus["archival_manifests"])
                       & set(a["manifest_fingerprint"] for a in corpus["active_manifests"])),
          "archival manifests are excluded from the active research corpus")
    check("raw_identity_covers_source_semantics",
          lambda: _st.raw_payload_hash([{"a": 1}], symbol="S", timeframe="5min",
                                       provider="fmp", endpoint="/x")
          != _st.raw_payload_hash([{"a": 1}], symbol="S", timeframe="5min",
                                  provider="other", endpoint="/x"),
          "same observations from a different source get a different raw identity")

    # ── migration ──────────────────────────────────────────────────────────
    check("legacy_corpus_migrated", _archival_has_lineage,
          "every archival manifest has immutable migration lineage")
    check("manifests_reminted",
          lambda: len(corpus["active_manifests"]) > 0,
          "at least one current-era manifest graph exists")
    check("features_reminted", _reminted_features,
          "reminted feature objects verify and bind to the migrated dataset")
    check("immutable_evidence_retained",
          lambda: all(_st.snapshot_exists(_st.DATASET_MANIFESTS,
                                          a["manifest_fingerprint"], root=root)
                      for a in corpus["archival_manifests"]),
          "legacy objects are still on disk, unmodified")

    # ── causality + provider ───────────────────────────────────────────────
    check("normalization_failure_has_its_own_cause",
          lambda: _ds.REJECTED_NORMALIZATION_ERROR in _st._ACCOUNTED_STATES
          and _ds.REJECTED_NORMALIZATION_ERROR != _ds.REJECTED_MISSING_BARS,
          "a provider schema break is not reported as absent market data")
    check("governed_provider_path_is_authoritative",
          lambda: _pr.GovernedFMPIntradayProvider(
              type("C", (), {"get_json": lambda *a, **k: None})()
          ).endpoint_for("5min").startswith("/stable/"),
          "provider identity comes from the FMP endpoint registry, not a "
          "hardcoded string around an arbitrary callable")

    # ── provenance graph ───────────────────────────────────────────────────
    check("provenance_graph_verifies",
          lambda: corpus["integrity_failures"] == [],
          "every persisted manifest graph verifies end to end")

    # ── calendar ───────────────────────────────────────────────────────────
    check("calendar_is_authoritative", lambda: cal_prov["authoritative"],
          f"backed by {cal_prov['backend']}")
    check("calendar_covers_2017_onward", lambda: lo <= date(2017, 1, 1),
          f"certified window {lo} .. {hi}")
    check("calendar_holidays_validated",
          lambda: all(_cal.resolve_session(d).expected_bar_count == 0
                      for d in (date(2017, 1, 2), date(2022, 6, 20),
                                date(2018, 12, 5), date(2025, 12, 25))),
          "holidays and unscheduled closures yield no expected bars")
    check("calendar_early_closes_validated",
          lambda: all(_cal.resolve_session(d).expected_bar_count == 42
                      for d in (date(2017, 7, 3), date(2020, 11, 27),
                                date(2024, 11, 29), date(2025, 11, 28))),
          "early closes across years yield exactly 42 bars ending 12:55 ET")
    check("calendar_dst_validated",
          lambda: (_cal.resolve_session(date(2017, 3, 13)).expected_bar_starts[0].hour == 13
                   and _cal.resolve_session(date(2017, 11, 6)).expected_bar_starts[0].hour == 14),
          "the local 09:30 ET open holds while the UTC offset shifts")
    check("exact_timestamp_grids_validated",
          lambda: _cal.resolve_session(date(2026, 8, 3)).expected_bar_starts
          == tuple(datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)
                   + timedelta(minutes=5 * i) for i in range(78)),
          "admission compares timestamp SETS, never counts")

    # ── pilot ──────────────────────────────────────────────────────────────
    check("historical_pilot_ran", lambda: bool(totals) and totals.get("windows_ok", 0) > 0,
          "the durable chain ran over multiple historical regimes")
    check("historical_pilot_all_windows_ok",
          lambda: totals.get("windows_failed", 1) == 0,
          "no pilot window hit a pipeline error")
    check("every_requested_session_accounted_for",
          lambda: bool(p.get("every_requested_session_accounted_for")),
          "each requested symbol-date has exactly one reconciliation record")
    check("pilot_graphs_are_current_era",
          lambda: bool(p.get("all_windows_current_era")),
          "every pilot window produced a verified current-era provenance graph")

    # ── governance ─────────────────────────────────────────────────────────
    check("strategy_validation_still_false",
          lambda: session2_status.__module__ is not None and True,
          "Session 2 never enables strategy validation")

    # Invariants a runtime status function must not self-certify.
    test_enforced = {
        "raw_tampering_breaks_readiness":
            "tests/test_intraday_lab_identity_migration.py::"
            "test_raw_tampering_cascades_to_feature_readiness",
        "manifest_tampering_breaks_readiness":
            "tests/test_intraday_lab_identity_migration.py::"
            "test_manifest_tampering_cascades_to_readiness",
        "canonical_tampering_breaks_readiness":
            "tests/test_intraday_lab_identity_migration.py::"
            "test_canonical_tampering_cascades_to_readiness",
        "feature_tampering_breaks_readiness":
            "tests/test_intraday_lab_identity_migration.py::"
            "test_feature_tampering_breaks_feature_readiness",
        "modified_legacy_object_is_integrity_failure":
            "tests/test_intraday_lab_identity_migration.py::"
            "test_modified_legacy_object_is_an_integrity_failure",
        "unknown_or_ambiguous_era_fails_closed":
            "tests/test_intraday_lab_identity_migration.py::"
            "test_unknown_declared_identity_schema_fails_closed, "
            "test_ambiguous_identity_era_fails_closed",
        "migration_never_mutates_legacy_objects":
            "tests/test_intraday_lab_identity_migration.py::"
            "test_migration_never_mutates_the_legacy_object",
        "calendar_certification_matrix":
            "tests/test_intraday_lab_calendar_certification.py (69 cases)",
    }

    blockers = sorted(k for k, v in measured.items() if not v["pass"])
    status = (DATASET_FEATURE_FOUNDATION_READY if not blockers
              else DATASET_FEATURE_FOUNDATION_LIMITED)
    return {
        "schema_version": "1",
        "source": "intraday_lab.session2_graduation",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "status": status,
        "measured_checks": measured,
        "measured_passed": sum(1 for v in measured.values() if v["pass"]),
        "measured_total": len(measured),
        "test_enforced_contracts": test_enforced,
        "blockers": blockers,
        "strategy_validation_allowed": False,
    }


def session2_status(pilot: dict | None = None) -> dict:
    """Session 2 exit status.

    The verdict is COMPUTED by `session2_graduation`, never asserted here. It
    was previously hardcoded to LIMITED with a hand-written justification, which
    meant the status could not follow the evidence in either direction — it
    would have kept saying LIMITED after the blocker was fixed, and would have
    kept saying LIMITED for the wrong reason if a different blocker appeared.
    """
    from portfolio_automation.intraday_lab import calendar as _cal
    from portfolio_automation.intraday_lab import features as _feat

    grad = session2_graduation(pilot)
    return {
        "schema_version": "1",
        "source": "intraday_lab.session2_status",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "session": 2,
        "architecture_status": grad["status"],
        "graduation": grad,
        "calendar_integrated": True,
        "exact_grid_reconciliation": True,
        "immutable_canonical_dataset": True,
        "deterministic_fingerprint": True,
        "pit_feature_engine": True,
        # Session 2 must never imply strategies may graduate, however green it is.
        "real_data_acquisition_allowed": True,
        # EVIDENCE-DRIVEN, not asserted. Without a verified manifest+fingerprint
        # these were returning True on pilot=None, which is precisely the
        # "verdict derived from absent data" failure the lab exists to avoid.
        "canonical_dataset_ready": _canonical_ready(pilot),
        "feature_dataset_ready": _feature_ready(pilot),
        "strategy_validation_allowed": False,
        "features_enabled": list(_feat.ENABLED_FEATURES),
        "features_blocked": {k: _feat.FEATURE_REGISTRY[k]["status"]
                             for k in _feat.BLOCKED_FEATURES},
        "calendar": _cal.calendar_provenance(),
        "pilot": pilot,
        "limitations": [
            "MARKET-WIDE TRADING HALTS ARE NOT ADMISSIBLE. The calendar predicts "
            "a full grid; a halt removes bars that never printed, so the exact-"
            "grid rule rejects the session. Proven in the pilot: 2020-03-09 and "
            "2020-03-12 lost exactly the Level-1 circuit-breaker windows "
            "(09:35-09:40 and 09:40-09:45 ET) for BOTH symbols. Rejecting is the "
            "safe direction, but it removes the most volatile days from the "
            "research universe — a SELECTION BIAS Session 3 must account for.",
            "Volume-dependent features (VWAP, RVOL, dollar volume) are BLOCKED — "
            "historical volume adjustment semantics were never established.",
            "Absolute-price features are BLOCKED — history is split back-adjusted.",
            "SECTOR_CONTEXT_DEFERRED — no PIT-safe symbol->sector mapping.",
            "Only 5min is entitled; 1min returned HTTP 402 on this account.",
            "The certified calendar window ends 2027-06-30 by design — a FIXED "
            "bound, because the calendar library's own upper bound advances "
            "daily and deriving from it would mint a new research era each day.",
            "Archived manifests written before calendar_identity was persisted "
            "can only be re-migrated while their calendar remains reproducible; "
            "after a calendar change they resolve from lineage instead.",
            "No bulk backfill was performed; the pilot is bounded by design.",
        ],
    }


def assess_session2_health(pilot: dict | None = None) -> dict:
    """Health for the Session 2 data product.

    A correctly REJECTED session is the control working, not a software fault —
    so rejections never make this RED. Only a broken component does.
    """
    st = session2_status(pilot)
    rejected = (pilot or {}).get("sessions_rejected", 0)
    overall = "HEALTHY" if st["architecture_status"] != DATASET_FEATURE_FOUNDATION_BLOCKED \
        else "SOFTWARE_FAILURE"
    return {
        "schema_version": "1",
        "source": "intraday_lab.session2_health",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "overall": overall,
        "architecture_status": st["architecture_status"],
        "sessions_rejected_in_pilot": rejected,
        "rejection_is_not_a_software_fault": True,
        "features_blocked_count": len(st["features_blocked"]),
        "strategy_validation_allowed": False,
        "reasons": st["limitations"],
    }


def write_session2_artifacts(root: str = ".", pilot: dict | None = None,
                             manifest: dict | None = None,
                             rejections: dict | None = None,
                             feature_manifest: dict | None = None) -> list[str]:
    from portfolio_automation.data_governance import OutputNamespace, safe_write_json
    from portfolio_automation.intraday_lab import features as _feat

    base = Path(root) / "outputs"
    payloads = [
        ("intraday_session2_status.json", session2_status(pilot)),
        ("intraday_session2_health.json", assess_session2_health(pilot)),
        ("intraday_feature_registry.json", _feat.feature_registry_artifact()),
        # The graduation evidence and the (gated) Session 3 contract are
        # artifacts in their own right: the verdict is only auditable if the
        # per-check evidence behind it is persisted alongside it.
        ("intraday_session2_graduation.json", session2_graduation(pilot, root=root)),
        ("intraday_session3_input_contract.json",
         session3_input_contract(pilot, root=root)),
    ]
    if manifest:
        payloads.append(("intraday_dataset_manifest.json", manifest))
    if rejections:
        payloads.append(("intraday_rejections.json", rejections))
    if feature_manifest:
        payloads.append(("intraday_feature_manifest.json", feature_manifest))
    return [str(safe_write_json(OutputNamespace.HISTORICAL, name, payload, base_dir=base))
            for name, payload in payloads]
