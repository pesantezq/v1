"""Deterministic VS-002 readiness evaluator over a VALIDATED snapshot.

It answers one question -- is the frozen evidence sufficient to run VS-002 --
and deliberately cannot answer any other. It computes no beta, no excess return,
no IC and no verdict about the signal. Producing a result before the VS-002
question is frozen is the contamination the whole phase exists to prevent, so
that capability is absent by construction rather than by discipline.

``experimental_noncanonical``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from portfolio_automation.vs002_evidence import contracts as C
from portfolio_automation.vs002_evidence.consumer import ValidatedSnapshot

READY = "VS002_READY"
NOT_READY = "VS002_NOT_READY"


@dataclass(frozen=True)
class Readiness:
    status: str
    reasons: tuple[str, ...]
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reasons": list(self.reasons),
                "detail": self.detail}


def evaluate(snap: ValidatedSnapshot) -> Readiness:
    reasons: list[str] = []
    m = snap.manifest

    # ---- signal evidence -------------------------------------------------
    scored = [s for s in snap.signals if s.get("signal_score") is not None]
    if not scored:
        reasons.append(
            "no recorded signal_score present; VS-002 H2 tests ranking and "
            "cannot run on reconstructed or absent scores")

    cohorts = int(m.get("non_overlapping_cohort_count") or 0)
    if cohorts < C.MIN_COHORTS:
        reasons.append(
            f"{cohorts} non-overlapping {C.HORIZON_DAYS}-day cohorts; "
            f">= {C.MIN_COHORTS} required")

    if not m.get("signal_evidence_cutoff"):
        reasons.append("signal evidence cutoff is not frozen")

    # ---- universe --------------------------------------------------------
    eligible = set(m.get("eligible_universe") or [])
    excluded = dict(m.get("excluded_symbols") or {})
    if C.BENCHMARK not in {r["symbol"] for r in snap.returns}:
        reasons.append(f"benchmark {C.BENCHMARK} absent from the return panel")
    for sym, why in excluded.items():
        if str(C.MIN_PRIOR_SESSIONS) not in why and "no matured signal" not in why \
                and "no price archive" not in why:
            reasons.append(
                f"{sym} excluded for a reason outside the universal rule: {why}")

    # Legacy return-panel history is only a fallback: when a
    # dividend-adjusted bar panel is present it is the authoritative
    # VS-002 risk evidence, so legacy short-history / thin-overlap /
    # archive-close convention insufficiency must NOT veto readiness.
    if not snap.has_bars:
        # ---- historical returns, per eligible signal --------------------------
        bench_dates = {r["session_date"] for r in snap.returns_for(C.BENCHMARK)}
        short_history: list[str] = []
        thin_overlap: list[str] = []
        leaked: list[str] = []

        for sym in sorted(eligible):
            sym_returns = snap.returns_for(sym)
            sym_dates = {r["session_date"] for r in sym_returns}
            for sig in snap.signals_for(sym):
                boundary = sig["signal_time"][:10]
                prior = snap.returns_before(sym, boundary)
                if len(prior) < C.MIN_PRIOR_SESSIONS:
                    short_history.append(f"{sym}@{boundary}:{len(prior)}")
                    continue
                window = {r["session_date"] for r in prior[-C.MIN_PRIOR_SESSIONS:]}
                joint = len(window & bench_dates)
                if joint < C.MIN_JOINT_OBSERVATIONS:
                    thin_overlap.append(f"{sym}@{boundary}:{joint}")
                # Strict pre-signal boundary. A same-day or later session inside the
                # estimation window is the leak this experiment must not contain.
                if any(d >= boundary for d in window):
                    leaked.append(f"{sym}@{boundary}")

        if short_history:
            reasons.append(
                f"{len(short_history)} signal(s) lack a {C.MIN_PRIOR_SESSIONS}-session "
                f"lookback, e.g. {short_history[:3]}")
        if thin_overlap:
            reasons.append(
                f"{len(thin_overlap)} signal(s) have < {C.MIN_JOINT_OBSERVATIONS} "
                f"aligned stock/benchmark observations, e.g. {thin_overlap[:3]}")
        if leaked:
            reasons.append(
                f"{len(leaked)} signal(s) would admit a session at or after the "
                f"signal boundary into beta estimation, e.g. {leaked[:3]}")

    # ---- the dividend-adjusted bar panel: the risk evidence itself --------
    if not snap.has_bars:
        reasons.append(
            "no dividend-adjusted bar panel — the historical risk evidence "
            "VS-002 is blocked on is absent from this package")
    else:
        bench_bar_dates = {b["session_date"] for b in snap.bars_for(C.BENCHMARK)}
        if not bench_bar_dates:
            reasons.append(f"benchmark {C.BENCHMARK} absent from the bar panel")
        bar_eligible = set(m.get("bar_eligible_universe") or [])
        if not bar_eligible:
            reasons.append("no symbol is bar-eligible under the universal rule")
        for sym, why in dict(m.get("bar_excluded_symbols") or {}).items():
            if str(C.MIN_PRIOR_SESSIONS) not in why                     and "no matured signal" not in why:
                reasons.append(
                    f"{sym} bar-excluded for a reason outside the universal "
                    f"rule: {why}")

        bar_short: list[str] = []
        bar_thin: list[str] = []
        bar_leaked: list[str] = []
        for sym in sorted(bar_eligible):
            for sig in snap.signals_for(sym):
                boundary = sig["signal_time"][:10]
                prior = snap.bars_before(sym, boundary)
                if len(prior) < C.MIN_PRIOR_SESSIONS:
                    bar_short.append(f"{sym}@{boundary}:{len(prior)}")
                    continue
                window = [b["session_date"]
                          for b in prior[-C.MIN_PRIOR_SESSIONS:]]
                joint = len(set(window) & bench_bar_dates)
                if joint < C.MIN_JOINT_OBSERVATIONS:
                    bar_thin.append(f"{sym}@{boundary}:{joint}")
                # Strict structural boundary — the signal-date bar itself is
                # the leak the frozen design names.
                if any(d >= boundary for d in window):
                    bar_leaked.append(f"{sym}@{boundary}")
        if bar_short:
            reasons.append(
                f"{len(bar_short)} signal(s) lack a {C.MIN_PRIOR_SESSIONS}-"
                f"session adjusted-bar lookback, e.g. {bar_short[:3]}")
        if bar_thin:
            reasons.append(
                f"{len(bar_thin)} signal(s) have < {C.MIN_JOINT_OBSERVATIONS} "
                f"joint stock/benchmark bar sessions, e.g. {bar_thin[:3]}")
        if bar_leaked:
            reasons.append(
                f"{len(bar_leaked)} signal(s) would admit a bar at or after "
                f"the signal boundary into beta estimation, e.g. {bar_leaked[:3]}")

        if m.get("adjusted_pit_certification") != C.ADJUSTED_PIT_CERTIFICATION:
            reasons.append(
                "manifest lacks the narrow adjusted-bar PIT certification")
        if m.get("adjusted_beta_convention") != C.ADJUSTED_BETA_CONVENTION:
            reasons.append(
                "adjusted beta convention is not the declared dividend-"
                "adjusted convention")

    # ---- ordering + convention ------------------------------------------
    for sym in sorted(eligible | {C.BENCHMARK}):
        dates = [r["session_date"] for r in snap.returns_for(sym)]
        if dates != sorted(dates):
            reasons.append(f"{sym} return series is not chronologically ordered")
            break

    # These certify the LEGACY archive-close return surface. In adjusted-bar
    # mode that surface is not built, so the adjusted convention/PIT (checked
    # in the bar block above) governs and these must not add NOT_READY.
    if not snap.has_bars:
        if m.get("pit_certification") != C.PIT_CERTIFICATION:
            reasons.append("manifest lacks the narrow PIT certification for returns")
        if m.get("beta_convention") != C.BETA_CONVENTION:
            reasons.append("beta convention is not the declared archive-close convention")

    detail = {
        "eligible_symbols": sorted(eligible),
        "excluded_symbols": excluded,
        "cohorts": cohorts,
        "signal_rows": len(snap.signals),
        "scored_signal_rows": len(scored),
        "return_rows": len(snap.returns),
        "cutoff": m.get("signal_evidence_cutoff"),
        "min_prior_sessions": C.MIN_PRIOR_SESSIONS,
        "min_joint_observations": C.MIN_JOINT_OBSERVATIONS,
        "min_cohorts": C.MIN_COHORTS,
        "bar_rows": len(snap.bars),
        "bar_eligible_symbols": sorted(m.get("bar_eligible_universe") or []),
        "risk_free_rate_7d": snap.risk_free_rate_7d,
    }
    return Readiness(status=(READY if not reasons else NOT_READY),
                     reasons=tuple(reasons), detail=detail)
