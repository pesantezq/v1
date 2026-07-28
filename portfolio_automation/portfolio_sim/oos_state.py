"""
Explicit out-of-sample (OOS) validation states for Strategy Lab tactics.

WS2 (see ``.superpowers/audit/ws-02-03-oos-selection.md``) confirmed that the
historical ``still_works_oos: bool | None`` field conflated "never tested"
(``None``) with "tested and did not fail" — and that the health assessor's
``is False`` check on that field read absence-of-failure as presence-of-
validity. This module replaces the boolean as the *source of truth* with an
explicit state enum plus a structured, non-fabricated evidence record. The
legacy boolean is now DERIVED from the state (``legacy_still_works_oos``);
nothing computes it independently any more, so the two can never drift apart.

States
------
``OOS_NOT_TESTED``   Tactic was never passed through ``walk_forward()`` at
                     all — no entry in ``walk_forward_results.json``.
``OOS_DATA_BLOCKED`` A walk-forward attempt happened but was blocked before
                     any fold could run (``no_params`` / ``insufficient_data``).
                     Distinct from ``OOS_NOT_TESTED``: the attempt occurred.
``OOS_INSUFFICIENT`` Folds ran but too few of them (``< MIN_FOLDS_FOR_SUFFICIENCY``)
                     to treat the aggregate as evidence rather than noise.
``OOS_MIXED``        Enough folds ran, but the result is not a clean pass:
                     either the sign/hit-rate signal straddles the pass bar,
                     or a single fold's magnitude dominates the aggregate
                     (``one_fold_controls_result``), which makes an otherwise
                     passing aggregate fragile rather than supported.
``OOS_SUPPORTED``    Enough folds, positive mean OOS excess return, majority
                     hit rate, and no single fold dominating the result.
``OOS_FAILED``       Enough folds, non-positive mean OOS excess return AND a
                     minority hit rate.

Nothing in this module changes ``strategy_score``, ``overfit``, or the raw
``still_works_oos`` value written by ``walk_forward()`` — it only classifies
and reports on evidence that already exists (plus the small number of
genuinely-additive fields ``walk_forward.py`` now also returns, e.g.
``one_fold_controls_result``, ``distinct_test_dates``).
"""
from __future__ import annotations

from enum import Enum
from typing import Any

# Below this fold count, an OOS aggregate is noise, not evidence. 4 folds is a
# deliberately low bar (chosen so it can be crossed by extending the existing
# walk-forward to more tactics without a data redesign) — it is a floor, not a
# claim of statistical power.
MIN_FOLDS_FOR_SUFFICIENCY = 4

# If a single fold's |oos_excess| accounts for more than this share of the sum
# of |oos_excess| across all folds, the aggregate is fold-dominated: treat an
# otherwise-passing result as fragile (OOS_MIXED), not supported.
ONE_FOLD_DOMINANCE_SHARE = 0.5

_DATA_BLOCKED_STATUSES = ("no_params", "insufficient_data")


class OOSState(str, Enum):
    OOS_NOT_TESTED = "OOS_NOT_TESTED"
    OOS_INSUFFICIENT = "OOS_INSUFFICIENT"
    OOS_MIXED = "OOS_MIXED"
    OOS_SUPPORTED = "OOS_SUPPORTED"
    OOS_FAILED = "OOS_FAILED"
    OOS_DATA_BLOCKED = "OOS_DATA_BLOCKED"


_PASSING_STATES = {OOSState.OOS_SUPPORTED}
_FAILING_STATES = {OOSState.OOS_FAILED}


def legacy_still_works_oos(state: OOSState) -> bool | None:
    """Derive the legacy tri-state boolean FROM the state — never the reverse.

    ``OOS_SUPPORTED`` -> True, ``OOS_FAILED`` -> False, everything else
    (not tested / insufficient / mixed / data-blocked) -> None, matching the
    only two concrete values ``walk_forward()`` has ever produced plus the
    ``None`` the other 25/26 tactics have always carried.
    """
    if state in _PASSING_STATES:
        return True
    if state in _FAILING_STATES:
        return False
    return None


def classify_oos_state(wf_entry: dict[str, Any] | None) -> OOSState:
    """Classify one tactic's walk-forward entry (or absence of one) into a state."""
    if not isinstance(wf_entry, dict):
        return OOSState.OOS_NOT_TESTED
    wf_status = wf_entry.get("status")
    if wf_status in _DATA_BLOCKED_STATUSES:
        return OOSState.OOS_DATA_BLOCKED
    if wf_status != "ok":
        return OOSState.OOS_NOT_TESTED

    splits = wf_entry.get("splits") or 0
    oos_mean = wf_entry.get("oos_mean_excess")
    oos_hit = wf_entry.get("oos_hit_rate")
    if splits < MIN_FOLDS_FOR_SUFFICIENCY or oos_mean is None or oos_hit is None:
        return OOSState.OOS_INSUFFICIENT

    one_fold_dominates = bool(wf_entry.get("one_fold_controls_result"))
    passes = oos_mean > 0 and oos_hit >= 0.5
    fails = oos_mean <= 0 and oos_hit < 0.5
    if one_fold_dominates:
        return OOSState.OOS_MIXED
    if passes:
        return OOSState.OOS_SUPPORTED
    if fails:
        return OOSState.OOS_FAILED
    return OOSState.OOS_MIXED


def build_oos_evidence(tactic_id: str, wf_entry: dict[str, Any] | None) -> dict[str, Any]:
    """Structured, non-fabricated OOS evidence record for one tactic.

    Fields that are not genuinely computed anywhere in the Strategy Lab today
    are recorded as ``None`` with the state carrying the reason, rather than
    omitted (silent omission is how the original bug hid) or guessed
    (fabrication is explicitly disallowed by the WS2 spec).
    """
    state = classify_oos_state(wf_entry)
    wf = wf_entry if isinstance(wf_entry, dict) else {}
    tested = wf.get("status") == "ok"
    return {
        "tactic_id": tactic_id,
        "state": state.value,
        "folds": wf.get("splits") if tested else None,
        "fold_construction": (
            "rolling_24m_train_window,_contiguous_nonoverlapping_3m_test_windows"
            if tested else None
        ),
        "train_period_months": wf.get("train_months") if tested else None,
        "test_period_months": wf.get("test_months") if tested else None,
        # Confirmed by audit: walk_forward.py has no embargo/purge gap between
        # train end and test start. "none" is itself a finding, not a null.
        "embargo_purge_rule": "none" if tested else None,
        "distinct_test_dates": wf.get("distinct_test_dates") if tested else None,
        "distinct_test_weeks": wf.get("distinct_test_weeks") if tested else None,
        # walk_forward.py carries no regime classification at all (audit §WS2.2).
        "distinct_regimes": None,
        "benchmark_comparison": "vs_SPY_per_fold" if tested else None,
        "oos_return": wf.get("oos_mean_return") if tested else None,
        "oos_excess_return": wf.get("oos_mean_excess") if tested else None,
        "oos_drawdown": wf.get("oos_mean_drawdown") if tested else None,
        "in_sample_to_oos_degradation": wf.get("is_oos_gap") if tested else None,
        # No CI (Wilson/bootstrap/t-interval) is computed anywhere in
        # portfolio_sim/walk_forward.py — confirmed absent, not merely unread.
        "confidence_interval": None,
        "survives_costs": False if tested else None,
        "tax_note": "gross_until_cost_model" if tested else None,
        "one_fold_controls_result": wf.get("one_fold_controls_result") if tested else None,
        "legacy_still_works_oos": legacy_still_works_oos(state),
    }
