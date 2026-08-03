# portfolio_automation/regime_coverage.py
"""regime_coverage — WS14 regime-concentration validity assessor (observe-only).

.superpowers/audit/ws-04-05-14-18-health.md (WS14) found that
``outputs/regime/regime_performance.json`` is purely observational: no
assessor anywhere (backtest_health, strategy_lab_health, quant_watch_probes)
reads its ``by_regime`` breakdown into a validity verdict. Confirmed live
(2026-07-28): 2211/2238 resolved signals (98.8%) are ``neutral``; only 27
(1.2%) are ``high_volatility``; return-weighted concentration is ~96%. The
2026-06-23 neutral-collapse guard (``semantic_liveness.py``'s
``detect_single_value_collapse``) catches a DIFFERENT failure — a
producer-ordering bug that collapses the column to a single value — and
explicitly whitelists ``"neutral"`` as a legitimate single state. It cannot
catch (and was never meant to catch) today's real, non-degenerate two-or-
three-label distribution that is nonetheless massively concentrated. This
module is the distributional-skew check that guard structurally cannot be.

Contract
--------
Pure function ``assess_regime_coverage(regime_perf)`` over the ALREADY
enriched ``by_regime`` shape produced by
``watchlist_scanner.performance_feedback.build_regime_performance_summary``
(count / effective_signals / return / excess_return / hit_rate / drawdown /
uncertainty / share_of_evidence / return_weighted_share per regime — added
alongside this module). Returns one or more of the four explicit states:

    REGIME_COVERAGE_BALANCED   — no single regime dominates evidence AND
                                 risk_off carries a sufficient effective
                                 sample. The only state that does NOT
                                 downgrade a claimed edge's credibility.
    REGIME_CONCENTRATED        — a single regime holds >= CONCENTRATION_
                                 SHARE_THRESHOLD of resolved evidence, by
                                 count share or |return-weighted share|.
    RISK_OFF_UNPROVEN          — the risk_off regime label is absent, or has
                                 fewer than MIN_REGIME_EFFECTIVE_N full-
                                 quality observations. A strategy's behavior
                                 in a genuine risk-off regime remains
                                 unproven regardless of how much neutral-
                                 regime evidence exists.
    REGIME_DATA_INSUFFICIENT  — fewer than MIN_RESOLVED_TOTAL resolved
                                 signals exist at all; too thin to assess
                                 coverage one way or the other. This state
                                 alone does NOT trigger a downgrade
                                 elsewhere (absence of evidence is not
                                 evidence of concentration).

States are not mutually exclusive at the data-sufficient tier — a window can
be simultaneously REGIME_CONCENTRATED and RISK_OFF_UNPROVEN (this is, in
fact, today's real state — the fix does not tune toward a friendlier label).

B4 correction (2026-07-29, docs/reliability-program/2026-07-28-final-report.md
addendum "Sent back for correction"). Two ways this assessor derived a
plausible verdict from ABSENT data, both now closed:

1. ``share_of_evidence`` is the only input to the concentration verdict, and a
   MISSING value was coerced to 0.0 by ``float(None or 0.0)``. A 98.8%-neutral
   window therefore read as perfectly balanced, ``REGIME_CONCENTRATED``
   structurally could not fire, and ``max()`` over the all-zeros dict named the
   SMALLEST regime (high_volatility, n=27) as the concentration leader at 0.0%.
   Now fail-closed: absent required fields → ``REGIME_DATA_INSUFFICIENT`` with
   ``insufficiency_kind == INSUFFICIENCY_MISSING_FIELDS``, no leader named.
   ``insufficiency_kind`` matters to the consumer — a thin window costs no
   credibility, an UNREADABLE one does (see ``strategy_lab_health``).

2. ``by_regime`` covers only rows resolved at the primary window, so a regime
   label observed but not yet matured is absent from it — and the old code
   reported that absence as "never observed in resolved evidence". Live
   2026-07-28 that claim was false: 108 ``risk_off`` rows existed (2026-07-25..27,
   all ``regime_data_quality=full``), 54 resolved at 1d, ZERO at the 3d primary
   window, so the verdict would have flipped to "proven" purely on maturation.
   The producer now emits an additive ``regime_census`` (observed vs resolved
   per label over ALL rows) and ``absence_kind`` distinguishes
   never_observed / immature / indeterminate / inconsistent.

Note on the two neutral-share figures in the program's write-ups: 2211/2238 =
98.79% at the 3-day primary window and 2211/2292 = 96.47% at the 1-day window
are BOTH correct. They differ by resolution horizon, not by any artifact
omission — the addendum's "corrected claim" mis-attributed the gap.

Consumer: ``portfolio_automation.portfolio_sim.strategy_lab_health`` reads
this assessor's ``states`` to downgrade its ``ranking_credibility`` and
``oos_validity`` dimensions with a stated reason (WS14 item 3) — a strategy
whose evidence is ~99% one regime must not read as generally validated.

Observe-only: never mutates decisions, scores, or portfolio state. Writes
only its own status artifact under OutputNamespace.LATEST.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json

__all__ = [
    "REGIME_COVERAGE_BALANCED", "REGIME_CONCENTRATED", "RISK_OFF_UNPROVEN",
    "REGIME_DATA_INSUFFICIENT", "assess_regime_coverage", "run_regime_coverage",
    "INSUFFICIENCY_TOO_FEW_RESOLVED", "INSUFFICIENCY_MISSING_FIELDS",
    "ABSENCE_NEVER_OBSERVED", "ABSENCE_IMMATURE", "ABSENCE_INDETERMINATE",
    "ABSENCE_INCONSISTENT",
]

REGIME_COVERAGE_BALANCED = "REGIME_COVERAGE_BALANCED"
REGIME_CONCENTRATED = "REGIME_CONCENTRATED"
RISK_OFF_UNPROVEN = "RISK_OFF_UNPROVEN"
REGIME_DATA_INSUFFICIENT = "REGIME_DATA_INSUFFICIENT"

# Below this many total resolved signals, the window is too thin to assess
# coverage at all (neither "balanced" nor "concentrated" is a supportable
# claim) — mirrors the existing semantic_liveness / quant_watch_probes
# min_sample convention used elsewhere in this codebase.
MIN_RESOLVED_TOTAL = 30

# A regime needs at least this many FULL-quality (not degraded/limited/
# partial) observations before its evidence counts as sufficient to prove
# behavior in that regime.
MIN_REGIME_EFFECTIVE_N = 30

# A single regime holding this share (by count OR by |return-weighted share|)
# of resolved evidence is "concentrated" — any claimed edge is really that
# one regime's performance wearing an all-regime label.
CONCENTRATION_SHARE_THRESHOLD = 0.80

RISK_OFF_LABEL = "risk_off"

# Fields this assessor cannot substitute a default for. `share_of_evidence` is
# the ONLY input to the concentration verdict; coercing a missing value to 0.0
# (as this module did before the B4 correction) imputes the BEST case from
# missing data — a 98.8%-concentrated window read as perfectly balanced,
# because `float(None or 0.0)` is 0.0. The program's standing decision is
# "never impute a missing component": a window whose derived fields are absent
# is not assessable, and must say so rather than return a verdict.
_REQUIRED_REGIME_FIELDS = ("share_of_evidence",)

# Why a regime window is not assessable. These are NOT interchangeable:
# `too_few_resolved` means there is genuinely no evidence yet (absence of
# evidence is not evidence of concentration, so it costs no credibility
# downstream); `missing_derived_fields` means the evidence EXISTS but the
# artifact cannot be read — an instrumentation failure that must not buy a
# free pass. See `strategy_lab_health._apply_regime_concentration_downgrade`.
INSUFFICIENCY_TOO_FEW_RESOLVED = "too_few_resolved"
INSUFFICIENCY_MISSING_FIELDS = "missing_derived_fields"

# How a risk_off absence from `by_regime` is explained. `by_regime` covers only
# rows RESOLVED at the primary window, so absence has three possible meanings
# and the pre-correction code asserted the strongest one unconditionally.
ABSENCE_NEVER_OBSERVED = "never_observed"   # census proves zero rows, ever
ABSENCE_IMMATURE = "immature"               # observed, none matured at window
ABSENCE_INDETERMINATE = "indeterminate"     # no census: cannot distinguish
ABSENCE_INCONSISTENT = "inconsistent"       # census says resolved, by_regime disagrees

_STATUS_REL = "regime_coverage_status.json"  # under outputs/latest/

_DISCLAIMER = (
    "Observe-only regime-coverage evidence-sufficiency check (WS14). Measures "
    "SHARE of resolved evidence per regime label, not label cardinality — "
    "distinct from the semantic_liveness neutral-collapse guard, which only "
    "catches a producer-ordering bug collapsing the column to one value. "
    "Never mutates decisions, scores, allocation, or portfolio state. A "
    "concentrated/unproven verdict is an evidence-sufficiency caveat on a "
    "claimed edge, not a market-timing signal."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _missing_required_fields(by_regime: dict[str, Any]) -> list[str]:
    """``["neutral.share_of_evidence", ...]`` for every regime missing a field
    the verdict depends on. Empty list means the window is assessable."""
    missing: list[str] = []
    for label, metrics in sorted(by_regime.items()):
        for field in _REQUIRED_REGIME_FIELDS:
            if (metrics or {}).get(field) is None:
                missing.append(f"{label}.{field}")
    return missing


def _classify_risk_off(
    by_regime: dict[str, Any], census_observed: dict[str, Any],
) -> tuple[str | None, int | None, int | None]:
    """Return ``(absence_kind, observed, resolved_at_primary_window)`` for
    risk_off. ``absence_kind`` is None when risk_off IS present in the resolved
    breakdown. Without a census the honest answer is ABSENCE_INDETERMINATE —
    never a claim that the label was never observed."""
    entry = census_observed.get(RISK_OFF_LABEL) if census_observed else None
    observed = int((entry or {}).get("observed") or 0) if entry is not None else None
    resolved = int((entry or {}).get("resolved") or 0) if entry is not None else None

    if RISK_OFF_LABEL in by_regime:
        return None, observed, resolved
    if not census_observed:
        return ABSENCE_INDETERMINATE, None, None
    if entry is None or not observed:
        return ABSENCE_NEVER_OBSERVED, observed or 0, resolved or 0
    if not resolved:
        return ABSENCE_IMMATURE, observed, resolved or 0
    return ABSENCE_INCONSISTENT, observed, resolved


def _risk_off_absence_reason(
    absence_kind: str, observed: int | None, primary_window_days: Any,
) -> str:
    window = f"{primary_window_days}-day" if primary_window_days else "primary"
    if absence_kind == ABSENCE_NEVER_OBSERVED:
        return (
            "risk_off regime label never observed in any tracked signal (resolved "
            "or not) — strategy/gauge behavior in a genuine risk-off regime is "
            "completely unproven")
    if absence_kind == ABSENCE_IMMATURE:
        return (
            f"risk_off observed {observed} time(s) but 0 resolved at the {window} "
            "primary window (immature, NOT unobserved) — behavior in a genuine "
            "risk-off regime remains unproven until those outcomes mature")
    if absence_kind == ABSENCE_INCONSISTENT:
        return (
            f"risk_off census reports resolved rows at the {window} window but "
            "by_regime carries no risk_off entry — artifact internally "
            "inconsistent; risk-off validity cannot be claimed")
    return (
        f"risk_off absent from resolved evidence at the {window} window and this "
        "artifact carries no regime_census, so never-observed cannot be "
        "distinguished from not-yet-matured — risk-off validity unproven")


def assess_regime_coverage(
    regime_perf: dict[str, Any] | None,
    *,
    min_resolved_total: int = MIN_RESOLVED_TOTAL,
    min_regime_effective_n: int = MIN_REGIME_EFFECTIVE_N,
    concentration_share_threshold: float = CONCENTRATION_SHARE_THRESHOLD,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Assess regime-coverage concentration from a
    ``build_regime_performance_summary``-shaped dict. Pure; never raises on
    malformed input (degrades to REGIME_DATA_INSUFFICIENT)."""
    now = now_iso or _now_iso()
    by_regime = dict((regime_perf or {}).get("by_regime") or {})
    resolved_total = (regime_perf or {}).get("resolved_signals")
    if not isinstance(resolved_total, (int, float)):
        resolved_total = sum(int((m or {}).get("total_signals") or 0) for m in by_regime.values())
    resolved_total = int(resolved_total or 0)

    primary_window_days = (regime_perf or {}).get("primary_window_days")
    census = dict((regime_perf or {}).get("regime_census") or {})
    census_observed = dict(census.get("observed") or {})
    if census.get("primary_window_days") is not None:
        primary_window_days = census.get("primary_window_days")
    absence_kind, risk_off_observed, risk_off_resolved = _classify_risk_off(
        by_regime, census_observed)

    def _risk_off_block(present: bool, effective: int) -> dict[str, Any]:
        return {
            "present": present, "effective_signals": effective,
            "min_required": min_regime_effective_n,
            "observed": risk_off_observed,
            "resolved_at_primary_window": risk_off_resolved,
            "absence_kind": absence_kind,
        }

    def _not_assessable(kind: str, reasons: list[str]) -> dict[str, Any]:
        return {
            "generated_at": now, "observe_only": True, "schema_version": "2",
            "source": "regime_coverage",
            "resolved_signals": resolved_total,
            "primary_window_days": primary_window_days,
            "by_regime": {}, "concentration": {},
            "risk_off": _risk_off_block(RISK_OFF_LABEL in by_regime, 0),
            "states": [REGIME_DATA_INSUFFICIENT], "primary_state": REGIME_DATA_INSUFFICIENT,
            "assessable": False, "insufficiency_kind": kind,
            "reasons": reasons, "disclaimer": _DISCLAIMER,
        }

    if not by_regime or resolved_total < min_resolved_total:
        return _not_assessable(INSUFFICIENCY_TOO_FEW_RESOLVED, [
            f"only {resolved_total} resolved signal(s) across {len(by_regime)} regime "
            f"label(s) (< {min_resolved_total}) — too thin to assess regime coverage "
            "either way"
        ])

    # Fail closed on an artifact whose derived fields are absent. This is NOT a
    # thin window — the evidence exists (resolved_total signals) and cannot be
    # read, typically because the on-disk artifact predates the producer's WS14
    # enrichment fields. Returning a verdict here would mean imputing one.
    missing = _missing_required_fields(by_regime)
    if missing:
        return _not_assessable(INSUFFICIENCY_MISSING_FIELDS, [
            f"{resolved_total} resolved signal(s) present but required field(s) "
            f"absent: {', '.join(missing)} — cannot assess regime concentration "
            "without imputing them; regenerate outputs/regime/regime_performance.json "
            "with the current producer"
        ])

    states: list[str] = []
    reasons: list[str] = []

    count_shares = {r: float((m or {}).get("share_of_evidence") or 0.0) for r, m in by_regime.items()}
    rw_shares = {r: (m or {}).get("return_weighted_share") for r, m in by_regime.items()
                if (m or {}).get("return_weighted_share") is not None}

    max_count_regime = max(count_shares, key=lambda k: count_shares[k]) if count_shares else None
    max_count_share = count_shares.get(max_count_regime) if max_count_regime is not None else None
    max_rw_regime = max(rw_shares, key=lambda k: abs(rw_shares[k])) if rw_shares else None
    max_rw_share = rw_shares.get(max_rw_regime) if max_rw_regime is not None else None

    # `return_weighted_share` is a SIGNED attribution ratio against the NET total, so
    # with mixed-sign contributions it is unbounded — performance_feedback.py:625-634
    # says so explicitly ("not a bounded probability; do not treat it as one"). Using
    # abs() of it as a concentration trigger did treat it as one: an unbounded value
    # compared against a threshold that is by construction <= 1.0, so a BALANCED book
    # whose regime contributions cancel fires REGIME_CONCENTRATED for reasons that have
    # nothing to do with concentration.
    #
    # Trigger on an abs-normalised share instead — |contribution| / sum|contribution| —
    # which is genuinely bounded [0, 1] and means "how much of the total return MOVEMENT
    # this regime accounts for". On 2026-08-03 that is neutral 499.07/654.78 = 76.2%
    # (below threshold) while the count arm is 94.31% (above), so today's verdict is
    # unchanged — but it is now reached for a stated, bounded reason.
    abs_total = sum(abs(v) for v in rw_shares.values()) or None
    rw_abs_shares = ({r: abs(v) / abs_total for r, v in rw_shares.items()}
                     if abs_total else {})
    max_rw_abs_regime = (max(rw_abs_shares, key=lambda k: rw_abs_shares[k])
                         if rw_abs_shares else None)
    max_rw_abs_share = (rw_abs_shares.get(max_rw_abs_regime)
                        if max_rw_abs_regime is not None else None)

    concentrated = (
        (max_count_share is not None and max_count_share >= concentration_share_threshold)
        or (max_rw_abs_share is not None
            and max_rw_abs_share >= concentration_share_threshold)
    )
    if concentrated:
        states.append(REGIME_CONCENTRATED)
        if max_rw_abs_share is not None:
            reasons.append(
                f"regime '{max_count_regime}' holds {max_count_share:.1%} of resolved "
                f"evidence by count ({max_rw_abs_share:.1%} of total return movement "
                f"via '{max_rw_abs_regime}') — any claimed edge is concentrated in one "
                f"regime")
        else:
            reasons.append(
                f"regime '{max_count_regime}' holds {max_count_share:.1%} of resolved "
                "evidence by count — any claimed edge is concentrated in one regime")

    risk_off_metrics = by_regime.get(RISK_OFF_LABEL)
    risk_off_present = risk_off_metrics is not None
    risk_off_effective = int((risk_off_metrics or {}).get("effective_signals") or 0)
    if not risk_off_present:
        states.append(RISK_OFF_UNPROVEN)
        reasons.append(_risk_off_absence_reason(
            absence_kind or ABSENCE_INDETERMINATE, risk_off_observed, primary_window_days))
    elif risk_off_effective < min_regime_effective_n:
        states.append(RISK_OFF_UNPROVEN)
        reasons.append(
            f"risk_off effective_signals={risk_off_effective} < {min_regime_effective_n} "
            "— insufficient full-quality evidence to claim risk-off validity")

    if not states:
        states.append(REGIME_COVERAGE_BALANCED)
        reasons.append(
            "no single regime dominates resolved evidence and risk_off carries a "
            "sufficient effective sample")

    priority = [REGIME_DATA_INSUFFICIENT, RISK_OFF_UNPROVEN, REGIME_CONCENTRATED, REGIME_COVERAGE_BALANCED]
    primary_state = next(s for s in priority if s in states)

    return {
        "generated_at": now, "observe_only": True, "schema_version": "2",
        "source": "regime_coverage",
        "resolved_signals": resolved_total,
        "primary_window_days": primary_window_days,
        "assessable": True, "insufficiency_kind": None,
        "by_regime": {
            r: {
                "total_signals": (m or {}).get("total_signals"),
                "effective_signals": (m or {}).get("effective_signals"),
                "avg_return_pct": (m or {}).get("avg_return_pct"),
                "excess_return_pct": (m or {}).get("excess_return_pct"),
                "win_rate": (m or {}).get("win_rate"),
                "drawdown_pct": (m or {}).get("drawdown_pct"),
                "hit_rate_uncertainty_pp": (m or {}).get("hit_rate_uncertainty_pp"),
                "share_of_evidence": (m or {}).get("share_of_evidence"),
                "return_weighted_share": (m or {}).get("return_weighted_share"),
            }
            for r, m in by_regime.items()
        },
        "concentration": {
            "max_share_regime": max_count_regime, "max_share": max_count_share,
            # SIGNED attribution ratio vs the NET total — UNBOUNDED (can exceed 1.0 or
            # go negative when regime contributions have mixed signs). Kept for
            # backward compatibility and for reading direction of attribution; NOT the
            # concentration trigger. Do not render it as a percentage of anything.
            "return_weighted_max_share_regime": max_rw_regime,
            "return_weighted_max_share": max_rw_share,
            # Abs-normalised share of total return MOVEMENT — bounded [0, 1]. This is
            # what the REGIME_CONCENTRATED return-weighted arm actually tests.
            "return_weighted_abs_share_regime": max_rw_abs_regime,
            "return_weighted_abs_share": max_rw_abs_share,
            "threshold": concentration_share_threshold,
        },
        "risk_off": _risk_off_block(risk_off_present, risk_off_effective),
        "states": states,
        "primary_state": primary_state,
        "reasons": reasons,
        "disclaimer": _DISCLAIMER,
    }


def run_regime_coverage(*, root: str | Path = ".", now_iso: str | None = None,
                        write_files: bool = True) -> dict[str, Any]:
    """Load ``outputs/regime/regime_performance.json`` → assess → (optionally)
    write ``outputs/latest/regime_coverage_status.json``. Never raises —
    degrades to a REGIME_DATA_INSUFFICIENT-shaped status on any error."""
    root_path = Path(root).resolve()
    now = now_iso or _now_iso()
    try:
        perf_path = root_path / "outputs" / "regime" / "regime_performance.json"
        regime_perf = json.loads(perf_path.read_text(encoding="utf-8")) if perf_path.exists() else None
        status = assess_regime_coverage(regime_perf, now_iso=now)
        if write_files:
            safe_write_json(OutputNamespace.LATEST, _STATUS_REL, status,
                            base_dir=root_path / "outputs")
        return status
    except Exception as exc:
        return {
            "generated_at": now, "observe_only": True, "schema_version": "2",
            "source": "regime_coverage", "resolved_signals": 0,
            "primary_window_days": None,
            "by_regime": {}, "concentration": {},
            "risk_off": {
                "present": False, "effective_signals": 0,
                "min_required": MIN_REGIME_EFFECTIVE_N,
                "observed": None, "resolved_at_primary_window": None,
                "absence_kind": ABSENCE_INDETERMINATE,
            },
            "states": [REGIME_DATA_INSUFFICIENT], "primary_state": REGIME_DATA_INSUFFICIENT,
            "assessable": False, "insufficiency_kind": INSUFFICIENCY_TOO_FEW_RESOLVED,
            "reasons": [f"error computing regime coverage: {exc}"],
            "disclaimer": _DISCLAIMER,
        }
