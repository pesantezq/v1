"""Daily-memo decision-coherence reconciliation layer.

This module is an **advisory, observe-only** reconciliation step that runs after
the decision/portfolio producers and before memo rendering. It reads
already-produced artifacts under ``outputs/latest/`` (and the policy
``decision_outcomes.jsonl``) and emits a single reconciled diagnostic artifact,
``outputs/latest/memo_coherence.json``, that the memo renderer consumes.

It NEVER:
  * recomputes decisions or any protected score
    (``signal_score``/``confidence_score``/``effective_score``/
    ``conviction_score``/``final_rank_score``/``recommendation_score``/
    ``priority_score`` are read, never redefined),
  * mutates ``decision_plan.json`` or ``portfolio_snapshot.json``,
  * executes trades, makes network calls, or relaxes observe-only anywhere.

Design: pure functions in → dict out. The top-level ``run_memo_coherence`` wraps
everything in ``try/except`` and returns an honest degraded state on failure so
the daily pipeline stays non-blocking.

See ``docs/DAILY_MEMO_DECISION_COHERENCE_PLAN.md`` and
``docs/OUTPUT_ARTIFACT_CONTRACTS.md`` (``memo_coherence.json``).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

try:  # pragma: no cover - import guard for governance writers
    from portfolio_automation.data_governance import OutputNamespace, safe_write_json, safe_write_text
except Exception:  # pragma: no cover
    OutputNamespace = None  # type: ignore
    safe_write_json = None  # type: ignore
    safe_write_text = None  # type: ignore

try:  # pragma: no cover - sector mapping is optional context
    from portfolio_automation.sector_mapping import normalize_sector
except Exception:  # pragma: no cover
    def normalize_sector(ticker, raw_sector, *, is_etf=False, is_fund=False, unknown="Unknown"):  # type: ignore
        return raw_sector if isinstance(raw_sector, str) and raw_sector.strip() else unknown

SCHEMA_VERSION = "1"
SOURCE = "memo_coherence"

# --- Reused / documented conventions -----------------------------------------
# ±1.0% neutral band: this is the SAME convention used by
# watchlist_scanner/outcome_evaluator.py::_label_return for "flat" outcomes.
# We reuse it here (we do not invent a new threshold) so noise-level moves are
# classified neutral instead of correct/incorrect. Documented in the plan.
NEUTRAL_BAND_PCT = 1.0
# Entry "extension" threshold: a single-session move at/above this magnitude
# (positive) marks a momentum entry as extended (prefer starter/pullback).
ENTRY_EXTENDED_PCT = 8.0
# The well-known default-fallback priority assigned to market/watch decisions
# whose drivers are all zero (observed live: 19/47 decisions == 0.55).
DEFAULT_FALLBACK_PRIORITY = 0.55
# compute_priority weights (READ-ONLY mirror of decision_engine.compute_priority,
# used only to display the breakdown — never to recompute the stored value).
PRIORITY_WEIGHTS = {"conviction_score": 0.45, "signal_score": 0.35, "confidence_score": 0.20}

_CAPITAL_DECISIONS = {"BUY", "SCALE"}
_RISK_INCREASING = {"BUY", "SCALE"}
_CAUTIOUS_POSTURES = {"cautious", "defensive", "structural_risk", "action_required", "stale"}

_DEFAULT_ARTIFACTS = {
    "decision_plan": ("outputs", "latest", "decision_plan.json"),
    "system_decision_summary": ("outputs", "latest", "system_decision_summary.json"),
    "cash_deployment_plan": ("outputs", "latest", "cash_deployment_plan.json"),
    "risk_delta": ("outputs", "latest", "risk_delta.json"),
    "correlation_risk_advisor": ("outputs", "latest", "correlation_risk_advisor.json"),
    "kelly_sizing_advisor": ("outputs", "latest", "kelly_sizing_advisor.json"),
    "confidence_calibration": ("outputs", "latest", "confidence_calibration.json"),
    "unified_crowd": ("outputs", "latest", "unified_crowd_intelligence_status.json"),
    "portfolio_snapshot": ("outputs", "portfolio", "portfolio_snapshot.json"),
    "decision_outcomes": ("outputs", "policy", "decision_outcomes.jsonl"),
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    txt = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_today_move(reason: Any) -> Optional[float]:
    """Extract the session move (%) from a decision reason string.

    Reasons look like ``"momentum: +9.14% today, RS: near 52wk high ..."``.
    Returns the signed percent (e.g. 9.14) or None if not parseable.
    """
    if not isinstance(reason, str):
        return None
    m = re.search(r"momentum:\s*([+-]?\d+(?:\.\d+)?)\s*%", reason, re.IGNORECASE)
    if not m:
        m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%\s*today", reason, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# 1-2. load + freshness
# ---------------------------------------------------------------------------

def load_sources(root: Path, artifacts: Optional[dict[str, tuple]] = None) -> dict[str, Any]:
    """Load all source artifacts. Missing files map to None (honest absence)."""
    artifacts = artifacts or _DEFAULT_ARTIFACTS
    out: dict[str, Any] = {}
    for name, rel in artifacts.items():
        path = root.joinpath(*rel)
        if name == "decision_outcomes":
            out[name] = _load_jsonl(path)
        else:
            out[name] = _load_json(path)
    return out


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return rows


def build_freshness(sources: dict[str, Any], now: Optional[datetime] = None) -> dict[str, Any]:
    """Per-source generated_at, age, stale flag, and max skew across sources."""
    now = now or datetime.now(timezone.utc)
    per: dict[str, Any] = {}
    times: list[datetime] = []
    for name, payload in sources.items():
        if not isinstance(payload, dict):
            per[name] = {"present": bool(payload), "generated_at": None, "age_minutes": None, "fresh": None}
            continue
        dt = _parse_dt(payload.get("generated_at"))
        age = round((now - dt).total_seconds() / 60.0, 1) if dt else None
        if dt:
            times.append(dt)
        per[name] = {
            "present": True,
            "generated_at": payload.get("generated_at"),
            "age_minutes": age,
            "fresh": (age is not None and age <= 24 * 60),
        }
    skew = round((max(times) - min(times)).total_seconds() / 60.0, 1) if len(times) >= 2 else 0.0
    stale = [n for n, m in per.items() if m.get("fresh") is False]
    snapshot = max(times).isoformat() if times else None
    return {
        "per_source": per,
        "max_skew_minutes": skew,
        "stale_sources": stale,
        "snapshot_timestamp": snapshot,
    }


# ---------------------------------------------------------------------------
# 3. candidate decision records
# ---------------------------------------------------------------------------

def derive_presentation_state(
    decision: str,
    *,
    band: str,
    funded: bool,
    blocking_reason: Optional[str],
    entry_extended: bool,
    degraded: bool,
    sandbox: bool,
) -> str:
    """Map the PROTECTED decision (+ context) to a richer memo-layer state.

    The protected ``decision`` vocabulary {BUY,SCALE,HOLD,WAIT,AVOID,SELL} is
    never modified; this returns an additive presentation label only.
    """
    d = (decision or "").upper()
    band = (band or "").lower()
    if degraded:
        return "INSUFFICIENT_DATA"
    if sandbox:
        return "RESEARCH_ONLY"
    if d == "SELL":
        return "TRIM"
    if d == "HOLD":
        return "HOLD"
    if d in ("WAIT", "AVOID"):
        return "WATCH"
    if d in _CAPITAL_DECISIONS:
        if blocking_reason == "concentration":
            return "BLOCKED_BY_CONCENTRATION"
        if blocking_reason == "risk":
            return "BLOCKED_BY_RISK"
        if not funded:
            # extended unfunded names are pullback candidates; others cash-blocked
            return "ADD_ON_PULLBACK" if entry_extended else "BLOCKED_BY_CASH"
        # funded:
        if entry_extended:
            return "STARTER"  # never a full breakout entry on an extended move
        if d == "SCALE":
            return "ADD"
        if band in ("normal", "high_conviction"):
            return "BUY_NOW"
        return "STARTER"
    return "WATCH"


def _priority_breakdown(drivers: dict[str, Any], priority: float) -> dict[str, Any]:
    """Read-only breakdown of compute_priority's weighted contributions."""
    conv = _f(drivers.get("conviction_score"))
    sig = _f(drivers.get("signal_score"))
    conf = _f(drivers.get("confidence_score"))
    contributions = {
        "conviction": round(conv * PRIORITY_WEIGHTS["conviction_score"], 4),
        "signal": round(sig * PRIORITY_WEIGHTS["signal_score"], 4),
        "confidence": round(conf * PRIORITY_WEIGHTS["confidence_score"], 4),
    }
    recomputed = round(sum(contributions.values()), 4)
    drivers_all_zero = conv == 0.0 and sig == 0.0 and conf == 0.0
    # If the stored priority cannot be explained by the drivers, it is a
    # default/fallback (observed: market/watch decisions default to 0.55).
    is_default = drivers_all_zero and abs(_f(priority) - DEFAULT_FALLBACK_PRIORITY) < 1e-6
    if is_default:
        basis = "default_fallback"
    elif abs(recomputed - _f(priority)) < 1e-3:
        basis = "computed"
    else:
        basis = "adjusted"  # priority diverges from drivers for another reason
    return {
        "weights": dict(PRIORITY_WEIGHTS),
        "contributions": contributions,
        "recomputed": recomputed,
        "stored_priority": round(_f(priority), 4),
        "basis": basis,
    }


def build_candidates(
    sources: dict[str, Any],
    *,
    entry_extended_pct: float = ENTRY_EXTENDED_PCT,
) -> list[dict[str, Any]]:
    """Build one enriched record per decision_plan decision."""
    plan = sources.get("decision_plan") or {}
    decisions = plan.get("decisions") or []
    degraded_mode = bool(plan.get("portfolio_context", {}).get("degraded_mode")) if isinstance(plan.get("portfolio_context"), dict) else False

    out: list[dict[str, Any]] = []
    for d in decisions:
        if not isinstance(d, dict):
            continue
        decision = d.get("decision") or d.get("decision_type") or ""
        structured = d.get("decision_reason_structured") or {}
        drivers = structured.get("drivers") or {}
        band = structured.get("band") or d.get("conviction_band") or "unknown"
        priority = _f(d.get("priority"), _f(d.get("priority_score")))
        move = _parse_today_move(d.get("reason") or d.get("decision_reason"))
        entry_extended = move is not None and move >= entry_extended_pct
        risk_flags = d.get("risk_flags") or []
        degraded = degraded_mode or ("degraded_data" in risk_flags) or ("low_confidence" in risk_flags and _f(d.get("confidence")) < 0.3)
        out.append({
            "symbol": d.get("symbol"),
            "decision": decision,          # PROTECTED value, surfaced as-is
            "source": d.get("source"),
            "band": band,
            "strategy": structured.get("strategy"),
            "priority": round(priority, 4),
            "priority_breakdown": _priority_breakdown(drivers, priority),
            "confidence": round(_f(d.get("confidence")), 4),
            "conviction_score": round(_f(drivers.get("conviction_score")), 4),
            "risk_flags": list(risk_flags),
            "entry_move_pct": move,
            "entry_extended": entry_extended,
            # Explicit, unambiguous metric basis (not free prose). The decision
            # reason reports the current-session return ("momentum: +X% today").
            "entry_metric": ("session_return_pct" if move is not None else None),
            "entry_metric_value": move,
            "entry_metric_basis": ("current session return from decision_plan reason"
                                   if move is not None else None),
            "primary_thesis": (d.get("reason") or d.get("decision_reason") or "").strip()[:200] or None,
            "primary_risk": _primary_risk(risk_flags, entry_extended, move),
            "why": structured.get("why") or [],
            "_degraded": degraded,
            "is_existing_holding": bool((d.get("inputs_used") or {}).get("is_existing_holding")),
        })
    return out


def _primary_risk(risk_flags: list, entry_extended: bool, move: Optional[float]) -> Optional[str]:
    if entry_extended and move is not None:
        # Explicit metric basis (session return), not the ambiguous word "today".
        return f"Session move: +{move:.1f}% — entry risk elevated; prefer starter/pullback."
    if "degraded_data" in risk_flags:
        return "Degraded input data — lower conviction."
    if "low_confidence" in risk_flags:
        return "Low model confidence on this signal."
    if "cooldown_active" in risk_flags:
        return "Recently traded — cooldown active."
    if "weak_signal" in risk_flags:
        return "Weak underlying signal strength."
    return None


# ---------------------------------------------------------------------------
# 4. funding (funded vs unfunded; cash-on-hand vs incoming)
# ---------------------------------------------------------------------------

def compute_funding(sources: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Split capital decisions into funded vs unfunded using the EXISTING
    cash_deployment_plan (5% reserve, deployable budget). Reuses the existing
    policy — invents no new cash reserve.
    """
    cdp = sources.get("cash_deployment_plan")
    if not isinstance(cdp, dict):
        return {"available": False, "reason": "cash_deployment_plan_missing", "status": "degraded"}

    cash_summary = cdp.get("cash_summary") or {}
    rows = cdp.get("deployment_rows") or []
    envelope = cdp.get("monthly_capital_envelope") or {}
    concentration = cdp.get("concentration") or {}
    portfolio_value = _f(cash_summary.get("portfolio_value"))
    cash_available = _f(cash_summary.get("cash_available"))
    target_cash_pct = _f(cash_summary.get("target_cash_pct"), 0.05)
    total_deployable = _f(cash_summary.get("total_deployable_amount"))
    below_floor = bool(cash_summary.get("below_safety_floor"))

    reserve_amount = round(portfolio_value * target_cash_pct, 2)
    cash_on_hand_deployable = max(0.0, round(cash_available - reserve_amount, 2))
    incoming_deployable = max(0.0, round(total_deployable - cash_on_hand_deployable, 2))

    # index deployment rows by symbol
    row_by_symbol = {r.get("symbol"): r for r in rows if isinstance(r, dict)}

    capital_symbols = {c["symbol"] for c in candidates if (c.get("decision") or "").upper() in _CAPITAL_DECISIONS}

    funded: list[dict] = []
    blocked: list[dict] = []
    funded_capital = 0.0
    gross_known = 0.0
    cumulative = 0.0

    sorted_rows = sorted(
        [r for r in rows if isinstance(r, dict)],
        key=lambda r: _f(r.get("priority")),
        reverse=True,
    )
    for r in sorted_rows:
        amt = _f(r.get("suggested_amount"))
        sym = r.get("symbol")
        # Precise status from the envelope-aware allocator (falls back to legacy
        # skipped_reason string for older artifacts).
        status = r.get("status") or r.get("skipped_reason")
        gross_known += amt
        if amt <= 0:
            blocked.append({
                "symbol": sym, "requested_capital": amt,
                "blocking_reason": status or "zero_size",
                "status": status or "BLOCKED_BY_CASH",
            })
            continue
        cumulative += amt
        source_tag = "cash_on_hand" if cumulative <= cash_on_hand_deployable + 1e-6 else "incoming_contributions"
        funded_capital += amt
        funded.append({
            "symbol": sym,
            "funded_capital": round(amt, 2),
            "funding_source": source_tag,
            "priority": round(_f(r.get("priority")), 4),
            "conviction_band": r.get("conviction_band"),
            "status": status,
            "tranche_type": r.get("tranche_type"),
            "pct_of_portfolio": r.get("pct_of_portfolio"),
            "pct_of_net_investable": r.get("pct_of_net_investable"),
            "held_for_pullback": r.get("held_for_pullback"),
            "sector": r.get("sector"),
        })

    # capital decisions in the plan but NOT in deployment rows → ranked out / unfunded.
    # With a monthly envelope present this is a budget/rank deferral, not cash blockage.
    has_envelope = bool(envelope) and envelope.get("status") == "ok"
    unfunded_status = "DEFERRED_BY_MONTHLY_BUDGET" if has_envelope else "beyond_deployment_budget"
    deferred_unsized: list[dict] = []
    for sym in sorted(capital_symbols - set(row_by_symbol.keys()), key=lambda s: str(s)):
        deferred_unsized.append({
            "symbol": sym, "requested_capital": None,
            "blocking_reason": unfunded_status, "status": unfunded_status,
        })

    funded_capital = round(funded_capital, 2)
    gross_known = round(gross_known, 2)
    unfunded_capital = round(max(0.0, gross_known - funded_capital), 2)

    # Prefer the canonical monthly envelope's net-investable as the deployable
    # ceiling; fall back to the legacy total_deployable.
    max_deployable = _f(envelope.get("monthly_contribution_net_investable"), total_deployable) \
        if has_envelope else round(total_deployable, 2)

    result = {
        "available": True,
        "status": "ok" if not below_floor else "warning",
        "portfolio_value": round(portfolio_value, 2),
        "available_cash": round(cash_available, 2),
        "cash_reserve_pct": target_cash_pct,
        "cash_reserve_amount": reserve_amount,
        "max_deployable": round(max_deployable, 2),
        "deployable_from_cash": cash_on_hand_deployable,
        "deployable_from_incoming": incoming_deployable,
        "gross_recommended_sized": gross_known,
        "funded_capital": funded_capital,
        "unfunded_capital": unfunded_capital,
        "funded_count": len(funded),
        "blocked_count": len(blocked) + len(deferred_unsized),
        "capital_decision_count": len(capital_symbols),
        "below_safety_floor": below_floor,
        "funded_actions": funded,
        "blocked_actions": blocked + deferred_unsized,
        "note": (
            "Funded amounts include expected incoming contributions; "
            f"only ${cash_on_hand_deployable:.0f} is fundable from cash on hand."
            if incoming_deployable > 0 else None
        ),
    }
    # Surface the full monthly capital envelope + concentration for the memo.
    if envelope:
        result["monthly_envelope"] = envelope
    if concentration:
        result["concentration"] = concentration
    return result


def _blocking_reason_for(symbol: Any, funding: dict[str, Any]) -> Optional[str]:
    if not funding.get("available"):
        return None
    for b in funding.get("blocked_actions") or []:
        if b.get("symbol") == symbol:
            return b.get("blocking_reason")
    return None


def _funded_amount_for(symbol: Any, funding: dict[str, Any]) -> Optional[float]:
    if not funding.get("available"):
        return None
    for f in funding.get("funded_actions") or []:
        if f.get("symbol") == symbol:
            return f.get("funded_capital")
    return None


def _funding_source_for(symbol: Any, funding: dict[str, Any]) -> Optional[str]:
    if not funding.get("available"):
        return None
    for f in funding.get("funded_actions") or []:
        if f.get("symbol") == symbol:
            return f.get("funding_source")
    return None


def _precise_status_for(symbol: Any, funding: dict[str, Any]) -> Optional[str]:
    """Return the envelope-aware precise status for a symbol, if any."""
    if not funding.get("available"):
        return None
    for bucket in ("funded_actions", "blocked_actions"):
        for r in funding.get(bucket) or []:
            if r.get("symbol") == symbol and r.get("status"):
                return r.get("status")
    return None


def _funded_row_for(symbol: Any, funding: dict[str, Any]) -> dict[str, Any]:
    """Return the funding funded-row (per-position sizing fields) for a symbol."""
    if not funding.get("available"):
        return {}
    for r in funding.get("funded_actions") or []:
        if r.get("symbol") == symbol:
            return r
    return {}


# ---------------------------------------------------------------------------
# 3b. finalize action records (presentation_state needs funding)
# ---------------------------------------------------------------------------

def finalize_actions(candidates: list[dict[str, Any]], funding: dict[str, Any]) -> list[dict[str, Any]]:
    """Attach funding + presentation_state to each candidate; deterministic order."""
    out: list[dict[str, Any]] = []
    for c in candidates:
        sym = c.get("symbol")
        decision = (c.get("decision") or "").upper()
        is_capital = decision in _CAPITAL_DECISIONS
        funded_amt = _funded_amount_for(sym, funding) if is_capital else None
        funded = bool(funded_amt and funded_amt > 0)
        block_reason_raw = _blocking_reason_for(sym, funding) if is_capital else None
        # normalize block reason to a coarse class for presentation_state
        block_class = None
        if block_reason_raw:
            low = str(block_reason_raw).lower()
            if "concentr" in low:
                block_class = "concentration"
            elif "risk" in low:
                block_class = "risk"
            else:
                block_class = "cash"
        sandbox = (c.get("source") in {"sandbox", "discovery", "crowd"})
        # Prefer the precise monthly-envelope status when the capital layer
        # produced one (FUNDED_STARTER / DEFERRED_BY_MONTHLY_BUDGET / ...).
        precise = _precise_status_for(sym, funding) if is_capital else None
        if precise and not sandbox and not c.get("_degraded"):
            state = precise
        else:
            state = derive_presentation_state(
                decision,
                band=c.get("band") or "",
                funded=funded,
                blocking_reason=block_class,
                entry_extended=bool(c.get("entry_extended")),
                degraded=bool(c.get("_degraded")),
                sandbox=sandbox,
            )
        entry_ctx = "extended" if c.get("entry_extended") else ("normal" if c.get("entry_move_pct") is not None else "unknown")
        rec = {k: v for k, v in c.items() if not k.startswith("_")}
        frow = _funded_row_for(sym, funding) if funded else {}
        rec.update({
            "presentation_state": state,
            "requested_capital": funded_amt if funded else (None if not is_capital else 0.0),
            "funded_capital": funded_amt if funded else (0.0 if is_capital else None),
            "funding_source": _funding_source_for(sym, funding) if funded else None,
            # per-position sizing carried from the monthly-envelope funding row
            "pct_of_portfolio": frow.get("pct_of_portfolio"),
            "pct_of_net_investable": frow.get("pct_of_net_investable"),
            "tranche_type": frow.get("tranche_type"),
            "held_for_pullback": frow.get("held_for_pullback"),
            "blocking_reason": block_reason_raw,
            "entry_context": entry_ctx,
            "eligibility": "sandbox" if sandbox else "advisory",
            "priority_basis": c.get("priority_breakdown", {}).get("basis"),
        })
        out.append(rec)
    # deterministic ranking: priority desc, then tie-break
    out = apply_tie_break(out)
    return out


def apply_tie_break(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic ordering. Primary: priority desc. Tie-break (documented):
    entry momentum desc → confidence desc → symbol asc. Annotates tie groups.
    """
    def key(a: dict[str, Any]):
        return (
            -_f(a.get("priority")),
            -(_f(a.get("entry_move_pct")) if a.get("entry_move_pct") is not None else -999),
            -_f(a.get("confidence")),
            str(a.get("symbol") or ""),
        )

    ranked = sorted(actions, key=key)
    # annotate ties (same rounded priority)
    from collections import Counter
    counts = Counter(round(_f(a.get("priority")), 3) for a in ranked)
    for a in ranked:
        p = round(_f(a.get("priority")), 3)
        a["priority_tie_group_size"] = counts[p]
        a["priority_is_tied"] = counts[p] > 1
    return ranked


TIE_BREAK_RULE = "priority desc → today's momentum desc → confidence desc → symbol asc"


# ---------------------------------------------------------------------------
# 5. field reconciliation
# ---------------------------------------------------------------------------

def _posture_from_sources(sources: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive a deterministic posture label (does not replace the renderer's
    verdict; used for the contradiction guard + investor lead).
    """
    plan = sources.get("decision_plan") or {}
    decisions = plan.get("decisions") or []
    urgencies = [str(d.get("urgency") or "").lower() for d in decisions if isinstance(d, dict)]
    risk = sources.get("risk_delta") or {}
    risk_status = str(risk.get("overall_status") or "").lower()
    has_structural = any((d.get("source") == "structural") for d in decisions if isinstance(d, dict))
    if "critical" in urgencies or has_structural or risk_status == "breach":
        label = "action_required"
    elif risk_status == "near_cap":
        label = "cautious"
    elif any(u == "high" for u in urgencies):
        label = "cautious"
    else:
        label = "steady"
    return {"label": label, "risk_status": risk_status}


def reconcile_fields(sources: dict[str, Any], actions: list[dict[str, Any]], funding: dict[str, Any]) -> dict[str, Any]:
    """Select authoritative source per field; flag/explain differences."""
    summ = sources.get("system_decision_summary") or {}
    risk = sources.get("risk_delta") or {}
    corr = sources.get("correlation_risk_advisor") or {}
    cal = sources.get("confidence_calibration") or {}
    posture = _posture_from_sources(sources, actions)

    top_theme = summ.get("top_theme") or {}
    top_opp = summ.get("top_opportunity") or {}
    best_fit = summ.get("best_portfolio_fit") or {}
    conc = risk.get("concentration") or {}
    lev = risk.get("leverage") or {}

    def field(value, source, status="ok", note=None):
        return {"value": value, "source": source, "status": status, "note": note}

    fields = {
        "posture": field(posture["label"], "memo_coherence:_posture_from_sources"),
        "dominant_theme": field(top_theme.get("name"), "system_decision_summary.top_theme"),
        "top_opportunity": field(top_opp.get("ticker"), "system_decision_summary.top_opportunity"),
        "best_portfolio_fit": field(best_fit.get("ticker"), "system_decision_summary.best_portfolio_fit"),
        "top_decisions": field([a.get("symbol") for a in actions[:5]], "decision_plan(ranked)"),
        "risk_posture": field(risk.get("overall_status"), "risk_delta.overall_status"),
        "available_cash": field(funding.get("available_cash") if funding.get("available") else None, "cash_deployment_plan", "ok" if funding.get("available") else "degraded"),
        "recommended_deployment": field(funding.get("funded_capital") if funding.get("available") else None, "cash_deployment_plan"),
        "concentration": field(conc.get("top_position"), "risk_delta.concentration"),
        "leverage": field(lev.get("total_exposure"), "risk_delta.leverage"),
        "effective_independent_bets": field(corr.get("effective_independent_bets"), "correlation_risk_advisor"),
        "model_confidence": field(cal.get("overall_hit_rate"), "confidence_calibration", "ok" if not cal.get("insufficient_data") else "degraded"),
        "data_quality": field(("degraded" if (sources.get("decision_plan") or {}).get("portfolio_context", {}).get("degraded_mode") else "ok") if isinstance((sources.get("decision_plan") or {}).get("portfolio_context"), dict) else "unknown", "decision_plan.portfolio_context"),
        "sandbox_vs_actionable": field("advisory", "memo_coherence", note="Memo is advisory-only; crowd/strategy outputs are sandbox/production-gated."),
    }
    return fields


# ---------------------------------------------------------------------------
# 6. overlap / correlation context
# ---------------------------------------------------------------------------

def build_overlap(sources: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    """Group proposed BUYs into thesis clusters using the EXISTING correlation
    advisor + sector mapping. Honestly degrades when ETF constituents absent.
    """
    corr = sources.get("correlation_risk_advisor")
    proposed = [a for a in actions if (a.get("decision") or "").upper() in _CAPITAL_DECISIONS]
    proposed_syms = [a.get("symbol") for a in proposed]

    clusters: list[dict[str, Any]] = []
    high_pairs = []
    eff_bets = None
    flags = []
    if isinstance(corr, dict):
        eff_bets = corr.get("effective_independent_bets")
        flags = corr.get("overall_flags") or []
        high_pairs = corr.get("high_correlation_pairs") or []

    # cluster by high-correlation pairs that involve proposed symbols OR holdings
    pair_clusters = _union_find_clusters([
        tuple(p.get("pair")) for p in high_pairs if isinstance(p.get("pair"), list) and len(p.get("pair")) == 2
    ])
    for members in pair_clusters:
        overlap_with_proposed = sorted(set(members) & set(proposed_syms))
        clusters.append({
            "members": sorted(members),
            "basis": "correlation>0.85",
            "proposed_in_cluster": overlap_with_proposed,
            "multiple_proposed_same_thesis": len(overlap_with_proposed) >= 2,
        })

    # sector clustering of proposed names (issuer-normalized; degraded if no sector)
    sector_groups: dict[str, list[str]] = {}
    sector_coverage = 0
    for a in proposed:
        sym = a.get("symbol")
        sector = _proposed_sector(sym, sources)
        if sector and sector != "Unknown":
            sector_coverage += 1
            sector_groups.setdefault(sector, []).append(sym)
    for sector, syms in sector_groups.items():
        if len(syms) >= 2:
            clusters.append({
                "members": sorted(syms),
                "basis": f"shared_sector:{sector}",
                "proposed_in_cluster": sorted(syms),
                "multiple_proposed_same_thesis": True,
            })

    return {
        "available": isinstance(corr, dict),
        "etf_lookthrough_available": False,  # no constituent dataset; honest
        "etf_lookthrough_reason": "no_constituent_dataset",
        "effective_independent_bets": eff_bets,
        "high_correlation_pairs": high_pairs,
        "overall_flags": flags,
        "clusters": clusters,
        "proposed_symbols": proposed_syms,
        "sector_coverage": f"{sector_coverage}/{len(proposed)}" if proposed else "0/0",
    }


def _proposed_sector(symbol: Any, sources: dict[str, Any]) -> Optional[str]:
    """Best-effort sector for a proposed symbol from portfolio_snapshot, else None."""
    snap = sources.get("portfolio_snapshot") or {}
    holdings = snap.get("holdings") if isinstance(snap, dict) else None
    if isinstance(holdings, list):
        for h in holdings:
            if isinstance(h, dict) and h.get("symbol") == symbol:
                raw = h.get("sector")
                is_etf = bool(h.get("is_etf") or h.get("is_fund"))
                try:
                    return normalize_sector(symbol, raw, is_etf=is_etf, is_fund=is_etf)
                except Exception:
                    return raw if isinstance(raw, str) else None
    return None


def _union_find_clusters(pairs: Iterable[tuple]) -> list[set]:
    parent: dict[Any, Any] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    for a, b in pairs:
        union(a, b)
    groups: dict[Any, set] = {}
    for node in list(parent.keys()):
        groups.setdefault(find(node), set()).add(node)
    return [g for g in groups.values() if len(g) >= 2]


# ---------------------------------------------------------------------------
# 7. crowd narrative consistency
# ---------------------------------------------------------------------------

CROWD_DEFINITIONS = {
    "cross_source_confirmation": "Retail attention overlaps an independent FMP/institutional attention signal. NOT a classified buy state.",
    "retail_only_attention": "Retail mention spike with no institutional corroboration. Sandbox research only.",
    "divergent_attention": "Retail attention diverges from institutional attention.",
    "classified_crowd_state": "A crowd-knowledge state from the sandbox classifier (e.g. crowd_validation). Production-gated.",
    "insufficient_data": "Too few mentions / no history to classify.",
}


def build_crowd_narrative(sources: dict[str, Any]) -> dict[str, Any]:
    crowd = sources.get("unified_crowd")
    if not isinstance(crowd, dict):
        return {"available": False, "reason": "unified_crowd_missing", "production_eligible": False, "definitions": CROWD_DEFINITIONS}

    state_counts = crowd.get("state_counts") or {}

    def _names(key):
        rows = crowd.get(key) or []
        return [r.get("ticker") for r in rows if isinstance(r, dict) and r.get("ticker")][:8]

    return {
        "available": True,
        "overall_status": crowd.get("overall_status"),
        "cross_source_confirmed": _names("top_confirmed_attention"),
        "retail_only": _names("top_retail_only_attention"),
        "divergent": _names("top_divergent_attention"),
        "institutional_context_only": _names("top_institutional_context_only"),
        "classified_state_counts": state_counts,
        "insufficient_data_count": state_counts.get("insufficient_data"),
        "social_sentiment_status": crowd.get("social_sentiment_status"),
        "enabled_categories": crowd.get("enabled_categories"),
        "disabled_categories": crowd.get("disabled_categories"),
        # NOTE: confirmed_attention is cross-source ATTENTION overlap, not a
        # classified crowd-knowledge buy state. Surfaced distinctly.
        "any_classified_buy_state": False,
        "production_eligible": False,
        "feeds_decision_engine": bool(crowd.get("feeds_decision_engine", False)),
        "definitions": CROWD_DEFINITIONS,
    }


# ---------------------------------------------------------------------------
# 8. hit-rate neutral-band evaluation (memo presentation layer)
# ---------------------------------------------------------------------------

def evaluate_hit_rate(sources: dict[str, Any], *, neutral_band_pct: float = NEUTRAL_BAND_PCT) -> dict[str, Any]:
    """Re-evaluate resolved decision outcomes with an economically-meaningful
    neutral band. Moves within ±band% are NEUTRAL (excluded from correct/
    incorrect), not scored as hits/misses. Producer win-rate is NOT changed.
    """
    rows = sources.get("decision_outcomes") or []
    cal = sources.get("confidence_calibration") or {}

    resolved = [r for r in rows if isinstance(r, dict) and r.get("resolved") and r.get("return_pct") is not None]
    correct = incorrect = neutral = missing = 0
    for r in resolved:
        ret = r.get("return_pct")
        if ret is None:
            missing += 1
            continue
        # decision_outcomes.jsonl stores return_pct as a DECIMAL FRACTION
        # (e.g. 0.0105 == 1.05%), unlike signal_outcomes.csv which stores
        # an already-multiplied percent. Convert to percent for the band.
        ret_pct = _f(ret) * 100.0
        decision = str(r.get("decision") or "").upper()
        if abs(ret_pct) < neutral_band_pct:
            neutral += 1
            continue
        ret = ret_pct
        # directional intent: BUY/SCALE expect up; SELL/TRIM expect down
        up_expected = decision in {"BUY", "SCALE", "HOLD"}
        hit = (ret > 0) if up_expected else (ret < 0)
        if hit:
            correct += 1
        else:
            incorrect += 1
    decided = correct + incorrect
    directional_accuracy = round(100.0 * correct / decided, 1) if decided else None

    # also count unresolved / missing-price among all outcome rows
    unresolved = sum(1 for r in rows if isinstance(r, dict) and not r.get("resolved"))
    missing_price = sum(1 for r in rows if isinstance(r, dict) and r.get("resolved") and r.get("return_pct") is None)

    return {
        "available": bool(resolved),
        "method": "neutral_band",
        "neutral_band_pct": neutral_band_pct,
        "neutral_band_source": "outcome_evaluator._label_return (reused ±1% flat convention)",
        "horizon": "next-available-close after decision (per producer)",
        "price_source": "decision_outcomes.jsonl (FMP/AV close, producer-resolved)",
        "return_scale": "return_pct is a decimal fraction; converted to percent before banding",
        "resolved_count": len(resolved),
        "correct": correct,
        "incorrect": incorrect,
        "neutral": neutral,
        "directional_accuracy_pct": directional_accuracy,
        "unresolved_count": unresolved,
        "missing_price_count": missing_price,
        "raw_calibration_hit_rate": cal.get("overall_hit_rate"),
        "note": (
            "Directional accuracy EXCLUDES sub-1% noise moves (counted neutral). "
            "Raw calibration hit-rate (producer) shown for comparison; it counts "
            "any positive move as correct."
        ),
        "followup": "MAE/MFE, payoff ratio, cost-adjusted and benchmark-relative metrics are documented as follow-up in DAILY_MEMO_DECISION_COHERENCE_PLAN.md.",
    }


# ---------------------------------------------------------------------------
# 9. coherence guards
# ---------------------------------------------------------------------------

def run_guards(
    sources: dict[str, Any],
    actions: list[dict[str, Any]],
    funding: dict[str, Any],
    fields: dict[str, Any],
    overlap: dict[str, Any],
    crowd: dict[str, Any],
    freshness: dict[str, Any],
) -> list[dict[str, Any]]:
    """Deterministic coherence guards. Each returns an issue dict or is skipped.
    Guards NEVER raise and NEVER fabricate replacement values.
    """
    issues: list[dict[str, Any]] = []

    def add(gid, severity, message, resolved=False):
        issues.append({"id": gid, "severity": severity, "message": message, "resolved": resolved})

    posture = fields.get("posture", {}).get("value")
    risk_increasing = [a for a in actions if (a.get("decision") or "").upper() in _RISK_INCREASING]
    funded_risk = [a for a in risk_increasing if (a.get("funded_capital") or 0) > 0]

    # G1: cautious posture but risk-increasing actions
    if posture in _CAUTIOUS_POSTURES and risk_increasing:
        msg = (
            f"Posture '{posture}' with {len(risk_increasing)} risk-increasing action(s); "
            f"{len(funded_risk)} are funded (the rest are starter/blocked/deferred)."
        )
        add("verdict_conflicts_with_action_mix", "warning", msg, resolved=bool(len(funded_risk) <= max(1, len(risk_increasing)//3)))

    # G2: top opportunity missing from top decisions
    top_opp = fields.get("top_opportunity", {}).get("value")
    top_dec = fields.get("top_decisions", {}).get("value") or []
    if top_opp and top_opp not in top_dec:
        reason = _explain_missing(top_opp, actions, funding)
        add("top_opportunity_missing_from_top_decisions", "info", f"Top opportunity {top_opp} not in Top Decisions: {reason}", resolved=True)

    # G3: best fit not funded/actionable without explanation
    best_fit = fields.get("best_portfolio_fit", {}).get("value")
    funded_syms = {a.get("symbol") for a in actions if (a.get("funded_capital") or 0) > 0}
    if best_fit and best_fit not in funded_syms:
        reason = _explain_missing(best_fit, actions, funding)
        add("best_fit_missing", "info", f"Best portfolio fit {best_fit} not in funded actions: {reason}", resolved=True)

    # G4: dominant theme not represented in displayed actions
    theme = fields.get("dominant_theme", {}).get("value")
    theme_tickers = set((sources.get("system_decision_summary") or {}).get("top_theme", {}).get("tickers") or [])
    displayed = set(a.get("symbol") for a in actions[:5])
    if theme and theme_tickers and not (theme_tickers & displayed):
        add("dominant_theme_not_represented", "info", f"Dominant theme '{theme}' not represented in Top Decisions — treat as research context.", resolved=True)

    # G5: all priorities identical (plateau)
    prios = {round(_f(a.get("priority")), 3) for a in actions}
    default_count = sum(1 for a in actions if a.get("priority_basis") == "default_fallback")
    if actions and len(prios) == 1:
        add("all_priorities_identical", "warning", f"All {len(actions)} priorities == {next(iter(prios))}; tie-break applied ({TIE_BREAK_RULE}).")
    elif default_count >= max(3, len(actions) // 3):
        add("priority_default_plateau", "info", f"{default_count} decisions carry the default-fallback priority {DEFAULT_FALLBACK_PRIORITY}; tie-break applied.", resolved=True)

    # G6: recommended capital exceeds available capital
    if funding.get("available"):
        if funding.get("funded_capital", 0) > funding.get("max_deployable", 0) + 1e-6:
            add("recommended_exceeds_deployable", "warning", "Funded capital exceeds max deployable — check cash_deployment_plan.")
        if funding.get("below_safety_floor"):
            add("below_cash_safety_floor", "info", "Cash is below the 5% safety floor; funded entries rely on incoming contributions.", resolved=True)
    else:
        add("funding_unavailable", "degraded", "cash_deployment_plan unavailable — funding shown as degraded.")

    # G7: action counts reconcile (capital decisions == funded + blocked)
    if funding.get("available"):
        cap = funding.get("capital_decision_count", 0)
        reconciled = funding.get("funded_count", 0) + funding.get("blocked_count", 0)
        if cap and reconciled and abs(cap - reconciled) > max(1, cap // 4):
            add("action_counts_unreconciled", "info", f"Capital decisions={cap} vs funded+blocked={reconciled}; some sized only when ranked into the budget.", resolved=True)

    # G8: crowd states contradict data quality
    if crowd.get("available"):
        confirmed = len(crowd.get("cross_source_confirmed") or [])
        insufficient = crowd.get("insufficient_data_count") or 0
        if confirmed and insufficient and not crowd.get("any_classified_buy_state"):
            add("crowd_attention_vs_classified", "info", f"{confirmed} cross-source 'confirmed-attention' tickers coexist with {insufficient} 'insufficient_data' — these are different definitions (attention overlap vs classified state).", resolved=True)

    # G9: stale mixed with fresh
    if freshness.get("stale_sources"):
        add("stale_mixed_with_fresh", "warning", f"Stale source(s): {', '.join(freshness['stale_sources'])} (>24h).")

    # G10: model readiness insufficient for sizing
    cal = sources.get("confidence_calibration") or {}
    if cal.get("insufficient_data"):
        add("model_readiness_insufficient", "info", "Confidence calibration insufficient_data — sizing remains conservative/advisory.", resolved=True)

    # G11: missing portfolio prices reduce coverage
    cov = overlap.get("sector_coverage")
    if isinstance(cov, str) and cov.endswith("/0") is False and cov.startswith("0/"):
        add("portfolio_sector_coverage_low", "info", "No sector coverage for proposed names — overlap clustering degraded.", resolved=True)

    return issues


def _explain_missing(symbol: Any, actions: list[dict[str, Any]], funding: dict[str, Any]) -> str:
    for a in actions:
        if a.get("symbol") == symbol:
            state = a.get("presentation_state")
            if state and state.startswith("BLOCKED"):
                return f"blocked ({a.get('blocking_reason') or state})"
            if state in {"WATCH", "ADD_ON_PULLBACK"}:
                return f"present but ranked below top-5 (state={state})"
            return f"ranked below top-5 (priority={a.get('priority')})"
    # not in decision plan at all
    block = _blocking_reason_for(symbol, funding)
    if block:
        return f"not an actionable decision ({block})"
    return "not present in the decision plan (eligibility/ranking)"


# ---------------------------------------------------------------------------
# investor summary
# ---------------------------------------------------------------------------

def build_investor_summary(
    fields: dict[str, Any],
    funding: dict[str, Any],
    overlap: dict[str, Any],
    actions: list[dict[str, Any]],
    sources: dict[str, Any],
) -> dict[str, Any]:
    posture = fields.get("posture", {}).get("value") or "steady"
    theme = fields.get("dominant_theme", {}).get("value")
    cash = funding.get("available_cash") if funding.get("available") else None
    funded_n = funding.get("funded_count", 0) if funding.get("available") else 0
    blocked_n = funding.get("blocked_count", 0) if funding.get("available") else 0

    # main opportunity = top funded BUY_NOW/STARTER, else top opportunity field
    funded_actions = [a for a in actions if (a.get("funded_capital") or 0) > 0]
    main_opp = funded_actions[0].get("symbol") if funded_actions else fields.get("top_opportunity", {}).get("value")

    eff_bets = overlap.get("effective_independent_bets")
    overlap_flag = "low_effective_independent_bets" in (overlap.get("overall_flags") or [])
    main_risk = (
        f"Portfolio is highly correlated (effective independent bets ≈ {eff_bets}); incremental tech/growth overlap is the dominant risk."
        if overlap_flag else
        "Concentration within position caps; monitor single-name and sector exposure."
    )

    posture_word = {
        "action_required": "Action required",
        "cautious": "Cautious",
        "steady": "Steady",
    }.get(posture, posture.replace("_", " ").title())

    cash_clause = (
        f"Available cash is ${cash:,.0f}" + (" (below the 5% reserve)" if funding.get("below_safety_floor") else "")
        if cash is not None else "Cash data is degraded"
    )
    para = (
        f"{posture_word} and mostly hold. "
        + (f"Lead research theme is {theme}. " if theme else "")
        + f"{cash_clause}, so {funded_n} funded entr{'y' if funded_n == 1 else 'ies'} "
        + f"{'is' if funded_n == 1 else 'are'} eligible and {blocked_n} opportunit{'y' if blocked_n == 1 else 'ies'} "
        + f"{'is' if blocked_n == 1 else 'are'} deferred or blocked."
    )

    return {
        "posture_paragraph": para,
        "main_opportunity": main_opp,
        "main_risk": main_risk,
        "what_changed": (sources.get("system_decision_summary") or {}).get("changes", {}).get("changes", [])[:3],
    }


# ---------------------------------------------------------------------------
# top-level orchestration
# ---------------------------------------------------------------------------

def build_memo_coherence(sources: dict[str, Any], *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Pure builder: sources dict in → reconciled coherence dict out."""
    freshness = build_freshness(sources, now=now)
    candidates = build_candidates(sources)
    funding = compute_funding(sources, candidates)
    actions = finalize_actions(candidates, funding)
    fields = reconcile_fields(sources, actions, funding)
    overlap = build_overlap(sources, actions)
    crowd = build_crowd_narrative(sources)
    hit_rate = evaluate_hit_rate(sources)
    issues = run_guards(sources, actions, funding, fields, overlap, crowd, freshness)
    investor = build_investor_summary(fields, funding, overlap, actions, sources)

    unresolved = [i for i in issues if not i.get("resolved")]
    resolved = [i for i in issues if i.get("resolved")]
    if any(i["severity"] == "degraded" for i in unresolved):
        status = "degraded"
    elif unresolved:
        status = "warning"
    else:
        status = "ok"

    funded_actions = [a for a in actions if (a.get("funded_capital") or 0) > 0]
    deferred_actions = [
        a for a in actions
        if (a.get("decision") or "").upper() in _CAPITAL_DECISIONS and not ((a.get("funded_capital") or 0) > 0)
    ]

    return {
        "generated_at": _now_iso(),
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "observe_only": True,
        "no_trade": True,
        "snapshot_timestamp": freshness.get("snapshot_timestamp"),
        "source_artifacts": freshness.get("per_source"),
        "freshness": {k: freshness[k] for k in ("max_skew_minutes", "stale_sources", "snapshot_timestamp")},
        "reconciliation": {
            "status": status,
            "fields": fields,
            "issues": issues,
            "resolved_issues": resolved,
            "unresolved_issues": unresolved,
            "issue_count": len(issues),
            "unresolved_count": len(unresolved),
        },
        "funding": funding,
        "actions": actions,
        "funded_actions": funded_actions,
        "deferred_actions": deferred_actions,
        "ranking": {
            "tie_break_rule": TIE_BREAK_RULE,
            "default_fallback_priority": DEFAULT_FALLBACK_PRIORITY,
            "default_fallback_count": sum(1 for a in actions if a.get("priority_basis") == "default_fallback"),
            "distinct_priorities": len({round(_f(a.get("priority")), 4) for a in actions}),
            "total_actions": len(actions),
        },
        "overlap": overlap,
        "crowd": crowd,
        "hit_rate": hit_rate,
        "investor_summary": investor,
        "coherence_status": status,
    }


def render_memo_coherence_md(result: dict[str, Any]) -> str:
    """Operator-facing markdown appendix of the coherence diagnostics."""
    rec = result.get("reconciliation", {})
    lines = ["# Memo Coherence Diagnostics", ""]
    lines.append(f"- Status: **{rec.get('status', 'unknown')}** · generated {result.get('generated_at')}")
    lines.append(f"- Snapshot: {result.get('snapshot_timestamp')} · max source skew {result.get('freshness', {}).get('max_skew_minutes')} min")
    fund = result.get("funding", {})
    if fund.get("available"):
        lines += [
            "",
            "## Funding",
            f"- Available cash: ${fund.get('available_cash'):,.0f} (reserve {fund.get('cash_reserve_pct')})",
            f"- Max deployable: ${fund.get('max_deployable'):,.0f} (cash ${fund.get('deployable_from_cash'):,.0f} + incoming ${fund.get('deployable_from_incoming'):,.0f})",
            f"- Funded: {fund.get('funded_count')} actions / ${fund.get('funded_capital'):,.0f} · Blocked/deferred: {fund.get('blocked_count')}",
        ]
    issues = rec.get("issues", [])
    if issues:
        lines += ["", "## Coherence issues"]
        for i in issues:
            tag = "resolved" if i.get("resolved") else i.get("severity")
            lines.append(f"- [{tag}] {i.get('id')}: {i.get('message')}")
    hr = result.get("hit_rate", {})
    if hr.get("available"):
        lines += [
            "",
            "## Hit-rate (neutral-band)",
            f"- Directional accuracy: {hr.get('directional_accuracy_pct')}% "
            f"(correct {hr.get('correct')} / incorrect {hr.get('incorrect')} / neutral {hr.get('neutral')})",
            f"- Neutral band ±{hr.get('neutral_band_pct')}% · raw calibration {hr.get('raw_calibration_hit_rate')}",
        ]
    lines += ["", "_Advisory only — no trades executed. Production behavior remains human-gated._"]
    return "\n".join(lines)


def run_memo_coherence(root: Path | str = ".", *, write_files: bool = True) -> dict[str, Any]:
    """Top-level non-blocking entry point used by the daily pipeline."""
    try:
        root_path = Path(root)
        sources = load_sources(root_path)
        result = build_memo_coherence(sources)
        if write_files and safe_write_json is not None and OutputNamespace is not None:
            try:
                safe_write_json(OutputNamespace.LATEST, "memo_coherence.json", result, base_dir=str(root_path / "outputs"))
                safe_write_text(OutputNamespace.LATEST, "memo_coherence.md", render_memo_coherence_md(result), base_dir=str(root_path / "outputs"))
            except Exception as exc:  # pragma: no cover - write failures are non-fatal
                result["write_error"] = str(exc)
        return result
    except Exception as exc:  # pragma: no cover - top-level guard
        return {
            "generated_at": _now_iso(),
            "schema_version": SCHEMA_VERSION,
            "source": SOURCE,
            "observe_only": True,
            "no_trade": True,
            "status": "error",
            "coherence_status": "degraded",
            "error": str(exc),
        }


if __name__ == "__main__":  # pragma: no cover
    import sys
    out = run_memo_coherence(sys.argv[1] if len(sys.argv) > 1 else ".")
    print(json.dumps(out.get("reconciliation", {}).get("status"), indent=2))
