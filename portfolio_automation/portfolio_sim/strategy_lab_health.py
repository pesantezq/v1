"""
Strategy Lab health assessor — confirms the research lab ran and is producing
trustworthy output. Read-only; returns GREEN/AMBER/RED with reasons. Consumed by
the /strategy-lab-analysis skill and monthly-tool-analysis.

WS4 roll-up policy (see .superpowers/audit/ws-02-03-oos-selection.md)
----------------------------------------------------------------------
Health used to be a single collapsed verdict. The audit found the largest
false-GREEN in the system living inside it: `failing_oos` was computed with
an `is False` identity check, so 25/26 untested tactics (`still_works_oos:
null`) were silently treated the same as "tested and passed," and
`walk_forward_present` was merely a file-existence check. The result: GREEN
with the literal reason "no failing-OOS tactic surfaced" — which reads as
"nothing failed" when the true state was "almost nothing was ever tested."

This module now assesses NINE independent dimensions instead of collapsing
everything into one signal:

    runtime_health          — did the lab actually run and produce fresh,
                              parseable output (not "file exists").
    artifact_completeness   — are the 4 expected lab artifacts present.
    documentation_coverage  — Strategy Documentation Requirement
                              (`coverage_complete` / `undocumented`).
    data_admissibility      — price-panel / factor-data gaps that would taint
                              every tactic's numbers equally.
    statistical_sufficiency — fold-count / sample-size sufficiency across the
                              leaderboard, not per-tactic in isolation.
    oos_validity            — THE FIX. Explicit OOS-state based (see
                              oos_state.py): "no tactic surfaced as failed"
                              is no longer treated as "OOS-valid." At least
                              one tactic must reach OOS_SUPPORTED for this
                              dimension to be GREEN.
    ranking_credibility     — does the top-ranked tactic actually carry OOS
                              support, or is the leaderboard ordering resting
                              on untested tactics (WS3 selection-bias finding).
    governance_compliance   — observe_only/sandbox_only/no_trade invariants +
                              active-strategy-selection staleness.
    presentation_consistency— does the legacy `still_works_oos` field on each
                              row still agree with the state it is derived
                              from (a drift here means a consumer is lying).

Roll-up policy (fail-closed, documented so it cannot silently drift):
  1. Overall RED   if ANY dimension is RED.
  2. Overall AMBER if not RED and ANY dimension is AMBER.
  3. Overall GREEN only if ALL dimensions are GREEN.
  4. A dimension may only report GREEN if it carries a non-empty `evidence`
     list (positive proof, not merely "no errors seen") — enforced in code
     by `_dim()`, which downgrades a would-be GREEN with empty evidence to
     AMBER rather than trust the caller.
  5. Corollaries this policy is required to satisfy (WS4 spec):
       - documentation complete + insufficient OOS evidence => not fully GREEN
         (statistical_sufficiency/oos_validity AMBER drags the roll-up down
         even though documentation_coverage itself is GREEN).
       - no failing-OOS tactic + no OOS_SUPPORTED tactic => oos_validity AMBER,
         never GREEN (the headline fix).
       - fresh artifacts + an unparsable `created_at` => runtime_health RED.
       - cron succeeded + zero/empty meaningful output => runtime_health RED
         (`looks_fresh_but_empty`).

Gate (WS4 "C" — reversible, default ON)
----------------------------------------------------------------------
The stricter roll-up above ships behind `STRICT_ROLLUP_GATE`, default ON,
because the entire point of this change is that GREEN should mean
trustworthy — shipping it default-OFF would knowingly preserve the false
GREEN this module exists to fix. Disable it (temporarily, for rollback) via
EITHER:
  - config.json: `portfolio_sim.strategy_lab.health.strict_oos_rollup_enabled = false`
  - kill-switch file: `config/strategy_lab_strict_health.DISABLED` (any content)
  - env var: `STOCKBOT_STRATEGY_LAB_STRICT_HEALTH_DISABLED=1`
When disabled, `assess_strategy_lab_health` reproduces the PRE-WS4 verdict
exactly (same `status`/`reasons`/`signals` the old single-check algorithm
would have produced) — see `_assess_legacy`. This is intentionally the ONLY
supported rollback path; it does not partially relax individual dimensions.

Expected consequence of the default-ON gate, confirmed against the live repo
(2026-07-28): Strategy Lab health moves from GREEN to AMBER, because 25/26
tactics are untested (`oos_validity` has zero OOS_SUPPORTED tactics). This is
the INTENDED result of the fix, not a regression — do not tune thresholds to
avoid it.

RED   = the lab is broken or surfacing untrustworthy results.
AMBER = degraded but non-fatal (disabled, stale, factor data missing,
        OOS-failing tactic, or — under the strict gate — insufficient/absent
        credible OOS evidence).
GREEN = ran, populated, documented, AND at least one tactic carries positive,
        sufficient, undominated OOS evidence with nothing surfaced as failed.

WS14 regime-concentration downgrade (.superpowers/audit/ws-04-05-14-18-health.md)
----------------------------------------------------------------------
The WS14 audit found that NO assessor anywhere read
`outputs/regime/regime_performance.json`'s per-regime breakdown into a
validity verdict — a strategy's evidence could be 98.8% one regime label and
still read as generally validated. This module now consults
`portfolio_automation.regime_coverage.assess_regime_coverage()` and, when it
reports `REGIME_CONCENTRATED` or `RISK_OFF_UNPROVEN` (real regime data
present, not merely absent/thin), downgrades `ranking_credibility` and
`oos_validity` with a stated reason: a GREEN becomes AMBER, and an
already-non-GREEN dimension gets the caveat appended to its reasons either
way. `REGIME_DATA_INSUFFICIENT` alone (no regime artifact yet, or too thin a
window) never triggers a downgrade — absence of evidence is not evidence of
concentration, and doing so would falsely penalize the small-fixture tests
that predate this artifact entirely. This is DISTINCT from the 2026-06-23
neutral-collapse guard (`semantic_liveness.detect_single_value_collapse`,
which explicitly whitelists a single `"neutral"` label as a legitimately calm
window): that guard catches a producer-ordering bug collapsing the regime
column to ONE value; this module measures SHARE of evidence across however
many distinct labels exist and fires even with 2-3 labels present. Confirmed
live (2026-07-28): `REGIME_CONCENTRATED` + `RISK_OFF_UNPROVEN` both fire —
do not tune thresholds to avoid this.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.portfolio_sim.oos_state import OOSState, build_oos_evidence
from portfolio_automation.regime_coverage import (
    INSUFFICIENCY_MISSING_FIELDS,
    REGIME_CONCENTRATED, RISK_OFF_UNPROVEN, assess_regime_coverage,
)

_SANDBOX = ("outputs", "sandbox")

KILL_SWITCH_FILE = "config/strategy_lab_strict_health.DISABLED"
KILL_SWITCH_ENV = "STOCKBOT_STRATEGY_LAB_STRICT_HEALTH_DISABLED"
_LEGACY_GREEN_REASON = "lab healthy: ran, populated, documented, no failing-OOS tactic surfaced"

_UNTESTED_STATES = (OOSState.OOS_NOT_TESTED.value, OOSState.OOS_DATA_BLOCKED.value)

_KNOWN_LIMITATIONS = [
    "walk_forward validation runs for only 1 hardcoded tactic today "
    "(run_strategy_lab.py:_walk_forward_results); the other ~25 tactics on the "
    "leaderboard have never been OOS-tested (state OOS_NOT_TESTED).",
    "No multiple-comparison correction (Holm/Bonferroni/deflated Sharpe/PBO/"
    "bootstrap rank stability) is applied across the leaderboard's tactics "
    "(WS3 audit finding) — the ranking itself carries selection bias.",
    "No confidence interval is computed for any OOS excess-return estimate.",
    "Reported returns are gross of transaction costs and taxes "
    "(tax_note: gross_until_cost_model) for in-sample and OOS results alike.",
    "walk_forward.py applies no embargo/purge gap between a fold's train end "
    "and test start.",
]


# --------------------------------------------------------------------------
# artifact loading
# --------------------------------------------------------------------------

def _load(root: Path, name: str) -> dict[str, Any] | None:
    try:
        return json.loads(root.joinpath(*_SANDBOX, name).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _age_hours(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() / 3600.0
    except Exception:
        return None


def _load_path(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def check_active_strategy_selection(root: str | Path = ".") -> tuple[list[str], dict[str, Any]]:
    """Surface the operator-approved active strategy and its liveness.

    Returns ``(reasons, signals)``. An ``active_strategy_id`` that no longer
    appears in the current ``strategy_review_queue.json`` is a stale selection
    (AMBER). No selection, or a selection still in the queue, is clean.
    """
    root = Path(root)
    reasons: list[str] = []
    signals: dict[str, Any] = {}

    sel = _load_path(root / "outputs" / "policy" / "active_strategy_selection.json") or {}
    active = sel.get("active_strategy_id")
    signals["active_strategy_id"] = active

    dpath = root / "outputs" / "policy" / "strategy_decisions.jsonl"
    try:
        signals["strategy_decisions_count"] = sum(
            1 for ln in dpath.read_text(encoding="utf-8").splitlines() if ln.strip())
    except Exception:
        signals["strategy_decisions_count"] = 0

    if active:
        q = _load_path(root / "outputs" / "latest" / "strategy_review_queue.json") or {}
        ids = {r.get("strategy_id") for r in (q.get("queue") or [])}
        if active not in ids:
            reasons.append(
                f"stale_active_strategy_selection: '{active}' not in current review queue")
    return reasons, signals


# --------------------------------------------------------------------------
# gate
# --------------------------------------------------------------------------

def strict_rollup_gate(root: str | Path = ".") -> tuple[bool, str]:
    """Resolve the WS4 strict-rollup gate. Default ON. Returns (enabled, source)."""
    root = Path(root)
    if (root / KILL_SWITCH_FILE).exists():
        return False, "kill_switch_file"
    if os.environ.get(KILL_SWITCH_ENV) == "1":
        return False, "env_kill_switch"
    try:
        raw = json.loads((root / "config.json").read_text(encoding="utf-8", errors="replace"))
    except Exception:
        raw = {}
    health_cfg = ((raw.get("portfolio_sim") or {}).get("strategy_lab") or {}).get("health") or {}
    if "strict_oos_rollup_enabled" in health_cfg:
        return bool(health_cfg["strict_oos_rollup_enabled"]), "config"
    return True, "default_on"


# --------------------------------------------------------------------------
# legacy (pre-WS4) algorithm — kept byte-for-byte so the gate's OFF path can
# reproduce the historical verdict exactly, bug included.
# --------------------------------------------------------------------------

def _assess_legacy(lb, cat, wf, factor, root: Path, now: datetime) -> dict[str, Any]:
    reasons: list[str] = []
    signals: dict[str, Any] = {}

    status_val = lb.get("status")
    signals["lab_status"] = status_val

    rows = lb.get("leaderboard") or []
    signals["tactic_count"] = len(rows)
    age = _age_hours(lb.get("created_at"), now)
    signals["age_hours"] = round(age, 1) if age is not None else None

    if status_val == "ok" and not rows:
        reasons.append("looks_fresh_but_empty: status ok but zero tactics scored")
    if status_val == "insufficient_data":
        reasons.append("insufficient_data: price panel/history too thin")
    if age is not None and age > 24 * 8:
        reasons.append(f"stale: leaderboard {age/24:.1f}d old (weekly cadence)")

    coverage = (cat or {}).get("coverage_complete")
    signals["coverage_complete"] = coverage
    if cat is not None and coverage is False:
        reasons.append(f"undocumented_tactics: {(cat or {}).get('undocumented')}")

    signals["walk_forward_present"] = wf is not None
    failing_oos = [r["tactic_id"] for r in rows if r.get("still_works_oos") is False]
    signals["failing_oos"] = failing_oos
    if failing_oos:
        reasons.append(f"tactic(s) surfaced with still_works_oos=false: {failing_oos[:5]}")

    signals["factor_data_available"] = bool((factor or {}).get("factor_data_available"))
    if factor is not None and not signals["factor_data_available"]:
        reasons.append("factor_data_unavailable (run scripts/fetch_factor_data.sh)")

    sel_reasons, sel_signals = check_active_strategy_selection(root)
    signals.update(sel_signals)
    reasons.extend(sel_reasons)

    red = any("looks_fresh_but_empty" in r for r in reasons)
    if red:
        status = "RED"
    elif reasons:
        status = "AMBER"
    else:
        status = "GREEN"
        reasons.append(_LEGACY_GREEN_REASON)

    if rows:
        top = rows[0]
        signals["top_tactic"] = top.get("name")
        signals["top_score"] = top.get("strategy_score")
        signals["top_excess_vs_spy"] = top.get("mean_excess_vs_spy")
    return {"status": status, "reasons": reasons, "signals": signals}


# --------------------------------------------------------------------------
# WS4 strict dimensions
# --------------------------------------------------------------------------

def _dim(status: str, evidence: list[str], reasons: list[str] | None = None) -> dict[str, Any]:
    """Build one dimension result. A GREEN with no positive evidence is not a
    thing this function can produce — it downgrades to AMBER instead."""
    reasons = list(reasons or [])
    evidence = list(evidence or [])
    if status == "GREEN" and not evidence:
        status = "AMBER"
        reasons.append("no_positive_evidence_for_green (downgraded from GREEN)")
    return {"status": status, "evidence": evidence, "reasons": reasons}


def _dim_runtime_health(lb, now: datetime) -> dict[str, Any]:
    status_val = lb.get("status")
    created_at = lb.get("created_at")
    age = _age_hours(created_at, now)
    rows = lb.get("leaderboard") or []
    if created_at and age is None:
        return _dim("RED", [], [f"invalid_timestamp: created_at={created_at!r} unparsable"])
    if status_val == "ok" and not rows:
        return _dim("RED", [], ["looks_fresh_but_empty: status ok but zero tactics scored"])
    if status_val == "insufficient_data":
        return _dim("AMBER", [], ["insufficient_data: price panel/history too thin"])
    if age is not None and age > 24 * 8:
        return _dim("AMBER", [], [f"stale: leaderboard {age/24:.1f}d old (weekly cadence)"])
    if status_val == "ok" and rows:
        return _dim("GREEN", [f"leaderboard status=ok, {len(rows)} tactics scored at {created_at}"])
    return _dim("AMBER", [], [f"unexpected_lab_status:{status_val}"])


def _dim_artifact_completeness(lb, cat, wf, factor) -> dict[str, Any]:
    present = {"leaderboard": lb is not None, "catalog": cat is not None,
               "walk_forward": wf is not None, "factor": factor is not None}
    missing = [k for k, v in present.items() if not v]
    if not missing:
        return _dim("GREEN", [f"all 4 lab artifacts present: {sorted(present)}"])
    if "leaderboard" in missing:
        return _dim("RED", [], ["leaderboard_missing"])
    return _dim("AMBER", [], [f"artifacts_missing:{missing}"])


def _dim_documentation_coverage(cat) -> dict[str, Any]:
    if cat is None:
        return _dim("AMBER", [], ["catalog_absent"])
    coverage = cat.get("coverage_complete")
    undocumented = cat.get("undocumented") or []
    if coverage is True and not undocumented:
        return _dim("GREEN", ["coverage_complete=true, 0 undocumented tactics"])
    if coverage is False or undocumented:
        return _dim("AMBER", [], [f"undocumented_tactics:{undocumented}"])
    return _dim("AMBER", [], ["coverage_complete_unknown"])


def _dim_data_admissibility(lb, factor) -> dict[str, Any]:
    warnings = (lb or {}).get("warnings") or []
    missing_price = [w for w in warnings if str(w).startswith("missing_price_history")]
    factor_avail = bool((factor or {}).get("factor_data_available")) if factor is not None else None
    reasons: list[str] = []
    if missing_price:
        reasons.append(f"missing_price_history_warning:{missing_price}")
    if factor is not None and not factor_avail:
        reasons.append("factor_data_unavailable (run scripts/fetch_factor_data.sh)")
    if reasons:
        return _dim("AMBER", [], reasons)
    ev = "no missing-price-history warnings"
    if factor_avail:
        ev += "; factor data available"
    return _dim("GREEN", [ev])


def _oos_evidence_by_tactic(rows: list[dict], wf_results: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        tid = r.get("tactic_id")
        if not tid:
            continue
        existing = r.get("oos_evidence")
        if isinstance(existing, dict) and existing.get("state"):
            out[tid] = existing
        else:
            out[tid] = build_oos_evidence(tid, wf_results.get(tid))
    return out


def _dim_statistical_sufficiency(rows: list[dict], oos_by_tactic: dict[str, dict]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return _dim("AMBER", [], ["no_tactics_to_assess"])
    sufficient = [
        tid for tid, ev in oos_by_tactic.items()
        if ev["state"] not in _UNTESTED_STATES and ev["state"] != OOSState.OOS_INSUFFICIENT.value
    ]
    if len(sufficient) / n >= 0.5:
        return _dim("GREEN", [f"{len(sufficient)}/{n} tactics have sufficient-fold OOS evidence"])
    return _dim("AMBER", [], [
        f"only {len(sufficient)}/{n} tactics have sufficient-fold OOS evidence "
        "(walk-forward is wired to 1 hardcoded tactic today)"])


def _dim_oos_validity(rows: list[dict], oos_by_tactic: dict[str, dict]) -> dict[str, Any]:
    """THE headline fix: `failing_oos == []` no longer implies GREEN. GREEN
    requires at least one tactic to reach OOS_SUPPORTED, with zero surfaced
    as OOS_FAILED."""
    failed = [tid for tid, ev in oos_by_tactic.items() if ev["state"] == OOSState.OOS_FAILED.value]
    supported = [tid for tid, ev in oos_by_tactic.items() if ev["state"] == OOSState.OOS_SUPPORTED.value]
    mixed = [tid for tid, ev in oos_by_tactic.items() if ev["state"] == OOSState.OOS_MIXED.value]
    if failed:
        return _dim("AMBER", [], [f"tactic(s) failed OOS validation (OOS_FAILED): {failed[:5]}"])
    if not supported:
        untested_n = sum(1 for ev in oos_by_tactic.values() if ev["state"] in _UNTESTED_STATES)
        insufficient_n = sum(1 for ev in oos_by_tactic.values()
                             if ev["state"] == OOSState.OOS_INSUFFICIENT.value)
        return _dim("AMBER", [], [
            "no_credible_oos_test: zero tactics reached OOS_SUPPORTED "
            f"(OOS_NOT_TESTED/OOS_DATA_BLOCKED={untested_n}, OOS_INSUFFICIENT={insufficient_n}, "
            f"OOS_MIXED={len(mixed)}/{len(rows)}) — absence of OOS failure is NOT evidence of "
            "OOS validity"])
    evidence = [f"{tid}: OOS_SUPPORTED" for tid in supported]
    if mixed:
        return _dim("AMBER", evidence, [f"tactic(s) OOS_MIXED (fragile/fold-dominated pass): {mixed[:5]}"])
    return _dim("GREEN", evidence)


def _dim_ranking_credibility(rows: list[dict], oos_by_tactic: dict[str, dict]) -> dict[str, Any]:
    if not rows:
        return _dim("AMBER", [], ["no_leaderboard_rows"])
    top = rows[0]
    top_id = top.get("tactic_id")
    top_state = oos_by_tactic.get(top_id, {}).get("state")
    if top_state == OOSState.OOS_SUPPORTED.value:
        return _dim("GREEN", [f"top-ranked tactic '{top.get('name')}' is OOS_SUPPORTED"])
    return _dim("AMBER", [], [
        f"top-ranked tactic '{top.get('name')}' (tactic_id={top_id}) has OOS state "
        f"{top_state} — ranking is not yet corrected for OOS evidence or selection bias "
        "(see .superpowers/audit/ws-02-03-oos-selection.md WS3)"])


def _dim_governance_compliance(lb, sel_reasons: list[str]) -> dict[str, Any]:
    flags = {k: lb.get(k) for k in ("observe_only", "sandbox_only", "no_trade")}
    # Explicit False is a confirmed breach (RED); absence just means the artifact
    # didn't carry the field (AMBER, unconfirmed) — absence is not fabricated
    # into either a pass or a breach.
    breached = [k for k, v in flags.items() if v is False]
    missing = [k for k, v in flags.items() if v is None]
    if breached:
        return _dim("RED", [], [f"governance_invariant_breach: {breached} explicitly false"])
    if missing:
        return _dim("AMBER", [], [f"governance_invariant_unconfirmed: {missing} absent from artifact"])
    if sel_reasons:
        return _dim("AMBER", [], list(sel_reasons))
    return _dim("GREEN", ["observe_only=sandbox_only=no_trade=true; no stale active-strategy selection"])


def _dim_presentation_consistency(rows: list[dict], oos_by_tactic: dict[str, dict]) -> dict[str, Any]:
    mismatches = []
    for r in rows:
        tid = r.get("tactic_id")
        ev = oos_by_tactic.get(tid, {})
        if r.get("still_works_oos") != ev.get("legacy_still_works_oos"):
            mismatches.append(tid)
    if mismatches:
        return _dim("AMBER", [], [f"still_works_oos field disagrees with derived OOS state for: {mismatches[:5]}"])
    return _dim("GREEN", [f"still_works_oos agrees with derived OOS state for all {len(rows)} tactics"])


def _roll_up(dims: dict[str, dict[str, Any]]) -> str:
    statuses = {d["status"] for d in dims.values()}
    if "RED" in statuses:
        return "RED"
    if "AMBER" in statuses:
        return "AMBER"
    return "GREEN"


_REGIME_CONCENTRATION_DOWNGRADE_STATES = {REGIME_CONCENTRATED, RISK_OFF_UNPROVEN}
_REGIME_DOWNGRADED_DIMENSIONS = ("ranking_credibility", "oos_validity")


def _load_regime_coverage(root: Path) -> dict[str, Any]:
    perf = _load_path(root / "outputs" / "regime" / "regime_performance.json")
    return assess_regime_coverage(perf)


def _apply_regime_concentration_downgrade(
    dims: dict[str, dict[str, Any]], regime_coverage: dict[str, Any],
) -> None:
    """WS14 — mutate `dims` in place: if regime evidence is concentrated or
    risk-off remains unproven, downgrade ranking_credibility/oos_validity
    with a stated reason. No-op when regime data is merely absent/thin
    (REGIME_DATA_INSUFFICIENT with `too_few_resolved`) — absence of evidence
    must not read as evidence of concentration.

    B4 correction: an UNREADABLE artifact is not a thin one. When the assessor
    reports `insufficiency_kind == missing_derived_fields`, resolved evidence
    exists and cannot be read (typically an on-disk artifact predating the
    producer's enrichment fields). That is an instrumentation failure, and it
    must cost the same downgrade — otherwise a stale artifact silently buys the
    credibility it never earned, which is the failure mode this whole
    workstream exists to close."""
    states = set(regime_coverage.get("states") or [])
    unreadable = regime_coverage.get("insufficiency_kind") == INSUFFICIENCY_MISSING_FIELDS
    if not (states & _REGIME_CONCENTRATION_DOWNGRADE_STATES) and not unreadable:
        return
    if unreadable:
        reason = (
            "regime_coverage_unreadable: "
            f"{'; '.join(regime_coverage.get('reasons') or [])} — regime evidence "
            "exists but cannot be read, so regime diversification is unverified; "
            "this dimension cannot read as generally validated"
        )
    else:
        reason = (
            f"regime_concentration ({', '.join(sorted(states & _REGIME_CONCENTRATION_DOWNGRADE_STATES))}): "
            f"{'; '.join(regime_coverage.get('reasons') or [])} — evidence is not "
            "diversified across market regimes; this dimension cannot read as "
            "generally validated"
        )
    for name in _REGIME_DOWNGRADED_DIMENSIONS:
        dim = dims.get(name)
        if dim is None:
            continue
        if dim["status"] == "GREEN":
            dim["status"] = "AMBER"
        dim["reasons"] = list(dim.get("reasons") or []) + [reason]


def _assess_strict(lb, cat, wf, factor, root: Path, now: datetime, legacy: dict[str, Any]) -> dict[str, Any]:
    rows = lb.get("leaderboard") or []
    wf_results = (wf or {}).get("results") or {}
    oos_by_tactic = _oos_evidence_by_tactic(rows, wf_results)
    sel_reasons, _sel_signals = check_active_strategy_selection(root)

    dims = {
        "runtime_health": _dim_runtime_health(lb, now),
        "artifact_completeness": _dim_artifact_completeness(lb, cat, wf, factor),
        "documentation_coverage": _dim_documentation_coverage(cat),
        "data_admissibility": _dim_data_admissibility(lb, factor),
        "statistical_sufficiency": _dim_statistical_sufficiency(rows, oos_by_tactic),
        "oos_validity": _dim_oos_validity(rows, oos_by_tactic),
        "ranking_credibility": _dim_ranking_credibility(rows, oos_by_tactic),
        "governance_compliance": _dim_governance_compliance(lb, sel_reasons),
        "presentation_consistency": _dim_presentation_consistency(rows, oos_by_tactic),
    }
    regime_coverage = _load_regime_coverage(root)
    _apply_regime_concentration_downgrade(dims, regime_coverage)
    overall = _roll_up(dims)

    if overall == "GREEN":
        reasons = [f"lab healthy under strict OOS rollup: all 9 dimensions GREEN "
                   f"({len(rows)} tactics, oos_validity evidence: "
                   f"{'; '.join(dims['oos_validity']['evidence'])})"]
        blocking_reasons: list[str] = []
    else:
        # blocking_reasons = every reason from every non-GREEN dimension
        blocking_reasons = [
            f"{name}: {reason}"
            for name, d in dims.items() if d["status"] != "GREEN"
            for reason in d["reasons"]
        ]
        reasons = list(blocking_reasons)

    state_counts: dict[str, int] = {}
    for ev in oos_by_tactic.values():
        state_counts[ev["state"]] = state_counts.get(ev["state"], 0) + 1

    signals = dict(legacy["signals"])
    signals["oos_state_counts"] = state_counts
    signals["dimension_status"] = {name: d["status"] for name, d in dims.items()}
    signals["regime_coverage"] = {
        "states": regime_coverage.get("states"),
        "primary_state": regime_coverage.get("primary_state"),
        "resolved_signals": regime_coverage.get("resolved_signals"),
        # B4: `assessable`/`insufficiency_kind` distinguish "no regime evidence
        # yet" from "evidence present but unreadable" — the latter downgrades.
        "assessable": regime_coverage.get("assessable"),
        "insufficiency_kind": regime_coverage.get("insufficiency_kind"),
        "primary_window_days": regime_coverage.get("primary_window_days"),
        "concentration": regime_coverage.get("concentration"),
        "risk_off": regime_coverage.get("risk_off"),
    }

    return {
        "status": overall,
        "reasons": reasons,
        "signals": signals,
        "dimensions": dims,
        "known_limitations": list(_KNOWN_LIMITATIONS),
        "blocking_reasons": blocking_reasons,
        "legacy_status": legacy["status"],
    }


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def assess_strategy_lab_health(root: str | Path = ".", now: datetime | None = None) -> dict[str, Any]:
    """Return the Strategy Lab health verdict.

    Always returns (at minimum) ``{status, reasons[], signals{}}`` for backward
    compatibility with existing consumers (daily/monthly-tool-analysis,
    strategy-lab-analysis skill, GUI). When the WS4 strict-rollup gate is ON
    (default), also returns ``dimensions``, ``known_limitations``,
    ``blocking_reasons``, and ``legacy_status`` alongside.
    """
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    gate_enabled, gate_source = strict_rollup_gate(root)
    gate_info = {"strict_oos_rollup_enabled": gate_enabled, "source": gate_source}

    lb = _load(root, "strategy_leaderboard.json")
    cat = _load(root, "research_strategy_catalog.json")
    wf = _load(root, "walk_forward_results.json")
    factor = _load(root, "factor_exposure_report.json")

    if lb is None:
        return {"status": "AMBER", "reasons": ["leaderboard_absent (lab not yet run / disabled)"],
                "signals": {"present": False}, "gate": gate_info}

    if lb.get("status") == "disabled":
        return {"status": "AMBER", "reasons": ["strategy_lab_disabled (inert steady state)"],
                "signals": {"present": True, "lab_status": "disabled"}, "gate": gate_info}

    legacy = _assess_legacy(lb, cat, wf, factor, root, now)
    if not gate_enabled:
        legacy["gate"] = gate_info
        return legacy

    strict = _assess_strict(lb, cat, wf, factor, root, now, legacy)
    strict["gate"] = gate_info
    return strict
