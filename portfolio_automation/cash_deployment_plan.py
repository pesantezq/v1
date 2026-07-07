"""
Cash Deployment Plan — observe-only advisor for excess cash + recurring
monthly contributions.

Reads:
    config.json                                — monthly_contribution,
                                                  target_cash_weight, cash_available
    outputs/latest/decision_plan.json          — ranked BUY/SCALE decisions
    outputs/latest/system_decision_summary.json (optional) — data health

Writes:
    outputs/latest/cash_deployment_plan.json
    outputs/latest/cash_deployment_plan.md

Hard guarantees:
    - observe_only=True hardcoded in every artifact.
    - Never mutates decision_plan or any score.
    - Never deploys when degraded_mode is true.
    - Caps suggested per-position deployment at allocation_engine
      max_position_cap (0.15).
    - Preserves a safety floor of 5% cash (matches config cash_reserve).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger("stockbot.portfolio_automation.cash_deployment_plan")

# ---------------------------------------------------------------------------
# Constants — mirror allocation_engine defaults; documented in spec.
# ---------------------------------------------------------------------------

_MAX_POSITION_PCT = 0.12          # mirrors allocation_engine max_position_cap (revert 2026-06-26)
_SAFETY_FLOOR_PCT = 0.05          # never deploy below this cash level
_DEFAULT_TARGET_CASH = 0.05       # fallback when config.target_cash_weight missing

# Glide-in excess cash + weekly deployment pacing (spec 2026-07-07).
# Deploy the monthly contribution PLUS a capped slice of idle excess cash each
# cycle, paced weekly. glide_fraction=0 + cadence="monthly" reproduces legacy.
_DEFAULT_EXCESS_CASH_GLIDE_FRACTION = 0.25   # fraction of idle excess deployed per cycle
_DEFAULT_DEPLOY_CADENCE = "weekly"           # weekly | monthly | daily
_VALID_DEPLOY_CADENCES = frozenset({"weekly", "monthly", "daily"})

# --- Monthly capital envelope: position-sizing bands (config-overridable) ---
# Canonical reserve is portfolio.target_cash_weight (NOT redefined here).
# These bands live under config.daily_memo_capital.*; values below are fallbacks.
_STARTER_POSITION_PCT = 0.005          # ~0.50% of portfolio
_STANDARD_POSITION_PCT = 0.01          # ~1.00% of portfolio
_MAX_NEW_POSITION_PCT_PER_CYCLE = 0.015  # ~1.50% of portfolio per contribution cycle
_THEME_CAP_PCT_OF_NET_INVESTABLE = 0.40  # max one theme's share of net investable
_EXTENDED_SESSION_PCT = 8.0            # session move at/above this -> extended entry

# Append-only contribution-cycle deployment ledger (POLICY namespace).
_LEDGER_FILENAME = "monthly_deployment_ledger.jsonl"

# Precise funding statuses (memo-facing). FUNDED_* are deployed; the rest explain
# why a recommendation was not (fully) funded today.
STATUS_FUNDED_STARTER = "FUNDED_STARTER"
STATUS_FUNDED_STANDARD = "FUNDED_STANDARD"
STATUS_DEFERRED_BY_MONTHLY_BUDGET = "DEFERRED_BY_MONTHLY_BUDGET"
STATUS_DEFERRED_BY_WEEKLY_PACING = "DEFERRED_BY_WEEKLY_PACING"
STATUS_DEFERRED_BY_THEME_CAP = "DEFERRED_BY_THEME_CAP"
STATUS_DEFERRED_BY_POSITION_CAP = "DEFERRED_BY_POSITION_CAP"
STATUS_HELD_FOR_PULLBACK = "HELD_FOR_PULLBACK"
STATUS_BLOCKED_BY_CASH = "BLOCKED_BY_CASH"
STATUS_INSUFFICIENT_CAPITAL_DATA = "INSUFFICIENT_CAPITAL_DATA"

# Conviction-band sizing multipliers (mirror conviction.py DEFAULT_SIZING)
_BAND_MULTIPLIERS = {
    "high_conviction": 1.00,
    "normal":          0.50,
    "starter":         0.25,
    "observe":         0.00,
    "defer":           0.00,
}

# Decisions eligible for capital deployment from this layer
_ELIGIBLE_DECISIONS = frozenset({"BUY", "SCALE"})

# Max ranked decisions we'll consider for deployment in one cycle
_MAX_DECISIONS = 10

# ---------------------------------------------------------------------------
# Safe helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        result = float(v)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


def _load_json_safe(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Pure planning logic
# ---------------------------------------------------------------------------


def compute_available_cash(
    *,
    portfolio_value: float,
    cash_available: float,
    target_cash_pct: float,
    monthly_contribution: float,
    safety_floor_pct: float = _SAFETY_FLOOR_PCT,
) -> dict[str, Any]:
    """
    Return the deployable cash budget given the current state.

    Components:
      current_cash_pct  = cash / portfolio_value
      excess_cash_pct   = max(0, current_cash_pct - target_cash_pct)
      total_deployable_pct = excess_cash_pct        (0 when below the safety floor)
      total_deployable_amount = total_deployable_pct * portfolio_value

    Deposited-contribution model (operator-confirmed 2026-07-07): the monthly
    contribution is transferred into the brokerage, so it is already part of
    ``cash_available`` and must NOT be added on top of the excess (doing so
    double-counts it). ``incoming_pct`` is retained for display only. This keeps
    ``total_deployable_amount`` consistent with the monthly envelope's
    ``deployable_cash`` (both = cash above the reserve target).

    If portfolio_value <= 0 the plan reports zero deployable; safe for new
    accounts with no holdings yet.
    """
    if portfolio_value <= 0:
        return {
            "portfolio_value": portfolio_value,
            "cash_available": cash_available,
            "current_cash_pct": 0.0,
            "target_cash_pct": target_cash_pct,
            "excess_cash_pct": 0.0,
            "incoming_pct": 0.0,
            "total_deployable_pct": 0.0,
            "total_deployable_amount": 0.0,
            "below_safety_floor": False,
        }

    current_cash_pct = cash_available / portfolio_value
    excess_cash_pct = max(0.0, current_cash_pct - target_cash_pct)
    # Deposited-contribution model: the contribution is already inside
    # cash_available, so it is NOT added on top of the excess. Retained for
    # display/transparency only (see docstring).
    incoming_pct = monthly_contribution / portfolio_value if monthly_contribution > 0 else 0.0

    below_safety_floor = current_cash_pct < safety_floor_pct
    if below_safety_floor:
        # Below the reserve floor: all cash (contribution included) is needed to
        # restore the floor, so nothing is deployable this cycle.
        total_deployable_pct = 0.0
    else:
        total_deployable_pct = excess_cash_pct

    return {
        "portfolio_value": round(portfolio_value, 2),
        "cash_available": round(cash_available, 2),
        "current_cash_pct": round(current_cash_pct, 4),
        "target_cash_pct": round(target_cash_pct, 4),
        "excess_cash_pct": round(excess_cash_pct, 4),
        "incoming_pct": round(incoming_pct, 4),
        "total_deployable_pct": round(total_deployable_pct, 4),
        "total_deployable_amount": round(total_deployable_pct * portfolio_value, 2),
        "below_safety_floor": below_safety_floor,
    }


def capital_config(cfg: dict[str, Any] | None) -> dict[str, float]:
    """Resolve the position-sizing bands + theme cap from config.daily_memo_capital,
    falling back to documented defaults. The reserve floor is intentionally NOT
    read here — it stays canonical at portfolio.target_cash_weight.
    """
    block = {}
    if isinstance(cfg, dict):
        block = cfg.get("daily_memo_capital") or {}
    return {
        "starter_position_pct": _safe_float(block.get("starter_position_pct")) or _STARTER_POSITION_PCT,
        "standard_position_pct": _safe_float(block.get("standard_position_pct")) or _STANDARD_POSITION_PCT,
        "max_new_position_pct_per_cycle": _safe_float(block.get("max_new_position_pct_per_cycle")) or _MAX_NEW_POSITION_PCT_PER_CYCLE,
        "theme_cap_pct_of_net_investable": _safe_float(block.get("theme_cap_pct_of_net_investable")) or _THEME_CAP_PCT_OF_NET_INVESTABLE,
    }


def contribution_cycle(as_of: date) -> tuple[str, str, str]:
    """Calendar-month contribution cycle. Returns (cycle_id, start_iso, end_iso)."""
    start = as_of.replace(day=1)
    nxt = start.replace(year=start.year + 1, month=1) if start.month == 12 \
        else start.replace(month=start.month + 1)
    end = nxt - timedelta(days=1)
    return start.strftime("%Y-%m"), start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# Weekly deployment pacing helpers (ISO-week based; no market-calendar dep)
# ---------------------------------------------------------------------------

def _iso_week_key(d: date) -> tuple[int, int]:
    """(iso_year, iso_week) — stable weekly bucket that survives year boundaries."""
    iso = d.isocalendar()
    return (iso[0], iso[1])


def iso_weeks_remaining_in_cycle(today: date, cycle_end: date) -> int:
    """Distinct ISO weeks spanning today..cycle_end inclusive (always >= 1).

    Bounded by the ~31-day cycle, so the day-walk is cheap. Guards a past
    cycle_end (returns 1) so the weekly tranche never divides by zero.
    """
    if cycle_end < today:
        return 1
    weeks: set[tuple[int, int]] = set()
    d = today
    while d <= cycle_end:
        weeks.add(_iso_week_key(d))
        d += timedelta(days=1)
    return max(1, len(weeks))


def deployed_in_iso_week(
    rows: list[dict[str, Any]], cycle_id: str, today: date
) -> float:
    """Capital funded on prior dates within *today*'s ISO week (excludes today).

    Derived from the existing append-only cycle ledger (last-wins per date) — no
    new state file. 'Before today' is the correct basis for sizing today's
    tranche, since today's own funding is appended after this read.
    """
    wk = _iso_week_key(today)
    today_iso = today.isoformat()
    total = 0.0
    for d_iso, amt in _cycle_deployed_by_date(rows, cycle_id).items():
        if d_iso >= today_iso:
            continue
        try:
            dd = date.fromisoformat(d_iso)
        except ValueError:
            continue
        if _iso_week_key(dd) == wk:
            total += amt
    return round(total, 2)


def weekday_days_remaining_in_week(today: date) -> int:
    """Weekday (Mon–Fri) days from today through Sunday, inclusive (always >= 1).

    Used by the `daily` cadence to spread the week's remaining budget over the
    trading days left, with no market-holiday calendar dependency (a holiday
    simply leaves that day's slice undeployed and self-corrects into later days).
    """
    count = 0
    for offset in range(0, 7 - today.isoweekday() + 1):
        if (today + timedelta(days=offset)).isoweekday() <= 5:
            count += 1
    return max(1, count)


def compute_weekly_pacing(
    *,
    cycle_net_investable: float,
    deployed_before_today: float | None,
    deployed_this_week_before_today: float,
    weeks_remaining_in_cycle: int,
    weekday_days_remaining: int,
    deploy_cadence: str,
    monthly_history_status: str,
) -> dict[str, Any]:
    """Resolve today's paced budget + the reporting block for the memo.

    Returns a dict with the effective ``budget_today`` (the cap passed to the
    allocator), the cycle-level remaining (for deferral-reason classification),
    and the display fields (weekly_tranche, weekly_remaining, ...).

    Semantics (see spec §B): the weekly tranche is a *live residual*, not a fixed
    allowance — because it is recomputed from the undeployed cycle budget each
    run it drifts down within a week and re-levels at the next ISO week, still
    totalling ``cycle_net_investable`` across the cycle. Never over-deploys.
    """
    cadence = deploy_cadence if deploy_cadence in _VALID_DEPLOY_CADENCES else _DEFAULT_DEPLOY_CADENCE

    # History unavailable → cannot trust the ledger; fall back to the full cycle
    # budget with no weekly sub-cap (never over-deploy on missing history).
    if monthly_history_status == "unavailable":
        return {
            "deploy_cadence": cadence,
            "budget_today": None,          # allocator uses full net_investable
            "cycle_remaining": None,
            "weeks_remaining_in_cycle": weeks_remaining_in_cycle,
            "weekly_tranche": None,
            "deployed_this_week": None,
            "weekly_remaining": None,
            "daily_budget": None,
            "note": "monthly_history_status unavailable — weekly pacing disabled this run",
        }

    before = deployed_before_today or 0.0
    cycle_remaining = round(max(0.0, cycle_net_investable - before), 2)

    if cadence == "monthly":
        # Whole cycle budget available any day; no weekly sub-cap.
        return {
            "deploy_cadence": cadence,
            "budget_today": cycle_remaining,
            "cycle_remaining": cycle_remaining,
            "weeks_remaining_in_cycle": weeks_remaining_in_cycle,
            "weekly_tranche": cycle_remaining,
            "deployed_this_week": deployed_this_week_before_today,
            "weekly_remaining": cycle_remaining,
            "daily_budget": None,
            "note": None,
        }

    weekly_tranche = round(cycle_remaining / max(1, weeks_remaining_in_cycle), 2)
    weekly_remaining = round(max(0.0, weekly_tranche - deployed_this_week_before_today), 2)

    if cadence == "daily":
        daily_budget = round(weekly_remaining / max(1, weekday_days_remaining), 2)
        budget_today = daily_budget
    else:  # weekly
        daily_budget = None
        budget_today = weekly_remaining

    return {
        "deploy_cadence": cadence,
        "budget_today": budget_today,
        "cycle_remaining": cycle_remaining,
        "weeks_remaining_in_cycle": weeks_remaining_in_cycle,
        "weekly_tranche": weekly_tranche,
        "deployed_this_week": deployed_this_week_before_today,
        "weekly_remaining": weekly_remaining,
        "daily_budget": daily_budget,
        "note": None,
    }


_SESSION_MOVE_RE = re.compile(r"momentum:\s*([+-]?\d+(?:\.\d+)?)\s*%", re.IGNORECASE)


def _session_move_pct(reason: Any) -> float | None:
    """Extract the session return (%) from a decision reason like
    'momentum: +9.14% today'. Returns the signed percent or None.
    """
    if not isinstance(reason, str):
        return None
    m = _SESSION_MOVE_RE.search(reason)
    if not m:
        m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%\s*today", reason, re.IGNORECASE)
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Contribution-cycle deployment ledger (append-only; last-wins per date)
# ---------------------------------------------------------------------------

def _ledger_path(base_dir: Path | str) -> Path:
    return Path(base_dir) / "policy" / _LEDGER_FILENAME


def read_deployment_ledger(base_dir: Path | str) -> tuple[list[dict[str, Any]], str]:
    """Return (rows, read_status). read_status: 'ok' | 'absent' | 'unavailable'."""
    path = _ledger_path(base_dir)
    if not path.exists():
        return [], "absent"
    try:
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows, "ok"
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        return [], "unavailable"


def _cycle_deployed_by_date(rows: list[dict[str, Any]], cycle_id: str) -> dict[str, float]:
    """Last-wins funded amount per date within *cycle_id* (append-only friendly)."""
    by_date: dict[str, float] = {}
    for r in rows:
        if isinstance(r, dict) and str(r.get("cycle")) == cycle_id:
            by_date[str(r.get("date"))] = _safe_float(r.get("capital_funded")) or 0.0
    return by_date


def resolve_prior_deployment(
    base_dir: Path | str, cycle_id: str, cycle_start: str, today_iso: str
) -> tuple[float | None, str]:
    """Resolve capital deployed earlier in this cycle and an honest history status.

    Returns (deployed_before_today, monthly_history_status). Never silently
    assumes zero: 'unavailable' on read error, 'partial' when our ledger began
    after the cycle started (pre-tracking deployment is unknown), 'ok' otherwise.
    """
    rows, read_status = read_deployment_ledger(base_dir)
    if read_status == "unavailable":
        return None, "unavailable"
    by_date = _cycle_deployed_by_date(rows, cycle_id)
    before = round(sum(v for d, v in by_date.items() if d < today_iso), 2)
    cycle_dates = sorted(by_date)
    earliest = cycle_dates[0] if cycle_dates else today_iso
    if earliest > cycle_start and today_iso > cycle_start:
        # ledger began mid-cycle: deployment before `earliest` is untracked
        return before, "partial"
    return before, "ok"


def append_deployment_ledger(
    base_dir: Path | str, *, cycle_id: str, today_iso: str,
    capital_funded: float, run_id: str = "",
) -> None:
    """Append today's funded total (append-only; last-wins read makes it idempotent)."""
    path = _ledger_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "cycle": cycle_id,
        "date": today_iso,
        "capital_funded": round(capital_funded, 2),
        "run_id": run_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "no_trade": True,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Monthly capital envelope (decimal-safe, amount-based reserve formulas)
# ---------------------------------------------------------------------------

def compute_monthly_envelope(
    *,
    portfolio_value: float | None,
    cash_on_hand: float,
    monthly_contribution_gross: float,
    reserve_pct: float,
    deployed_before_today: float | None,
    capital_funded_today: float,
    cycle_id: str,
    cycle_start: str,
    cycle_end: str,
    monthly_history_status: str,
    excess_cash_glide_fraction: float = 0.0,
    weekly_pacing: dict[str, Any] | None = None,
    portfolio_value_source: str = "decision_plan.portfolio_context",
    contribution_source: str = "config.portfolio.monthly_contribution",
    cash_source: str = "config.portfolio.cash_available",
) -> dict[str, Any]:
    """Build the canonical monthly capital envelope. Amount-based (no pct round-trip).

    reserve_target    = reserve_pct * portfolio_value        (denominator = portfolio_value)
    reserve_shortfall = max(0, reserve_target - cash_on_hand)
    contribution_base = max(0, gross_contribution - reserve_shortfall)
    deployable_cash   = max(0, cash_on_hand - reserve_target)   # all touchable excess
    idle_excess       = max(0, deployable_cash - gross_contribution)  # dry powder beyond this month
    glide_slice       = idle_excess * excess_cash_glide_fraction
    net_investable    = contribution_base + glide_slice        # cycle deployable (glide-in)
    deployed_total    = deployed_before_today + capital_funded_today
    remaining         = max(0, net_investable - deployed_total)

    ``excess_cash_glide_fraction=0`` reproduces the contribution-only budget
    (back-compat). The contribution is deposited (already in cash_on_hand), so
    subtracting it from deployable_cash prevents double-counting the idle slice.
    """
    ts = datetime.now(timezone.utc).isoformat()
    if portfolio_value is None or portfolio_value <= 0:
        return {
            "status": STATUS_INSUFFICIENT_CAPITAL_DATA,
            "reason": "missing_or_zero_portfolio_value",
            "portfolio_value": portfolio_value,
            "monthly_contribution_gross": round(monthly_contribution_gross, 2),
            "cash_on_hand": round(cash_on_hand, 2),
            "cash_reserve_target_pct": round(reserve_pct, 4),
            "calculation_timestamp": ts,
            "monthly_history_status": monthly_history_status,
            "portfolio_value_source": portfolio_value_source,
            "contribution_source": contribution_source,
            "cash_source": cash_source,
        }

    reserve_target = round(reserve_pct * portfolio_value, 2)
    reserve_shortfall = round(max(0.0, reserve_target - cash_on_hand), 2)
    contribution_base = round(max(0.0, monthly_contribution_gross - reserve_shortfall), 2)

    # Glide-in a capped slice of idle excess cash beyond this month's contribution.
    glide_frac = max(0.0, min(1.0, excess_cash_glide_fraction))
    deployable_cash = round(max(0.0, cash_on_hand - reserve_target), 2)
    idle_excess = round(max(0.0, deployable_cash - monthly_contribution_gross), 2)
    glide_slice = round(idle_excess * glide_frac, 2)
    net_investable = round(contribution_base + glide_slice, 2)

    before = deployed_before_today if deployed_before_today is not None else 0.0
    deployed_total = round(before + (capital_funded_today or 0.0), 2)
    # When history is unavailable we cannot trust `remaining`; expose None.
    if monthly_history_status == "unavailable":
        remaining = None
        held_for_future = None
    else:
        remaining = round(max(0.0, net_investable - deployed_total), 2)
        held_for_future = remaining

    utilization = (
        round(100.0 * deployed_total / net_investable, 1)
        if net_investable > 0 else 0.0
    )

    return {
        "status": "ok",
        "portfolio_value": round(portfolio_value, 2),
        "monthly_contribution_gross": round(monthly_contribution_gross, 2),
        "cash_on_hand": round(cash_on_hand, 2),
        "cash_reserve_target_pct": round(reserve_pct, 4),
        "cash_reserve_target_amount": reserve_target,
        "cash_reserve_shortfall": reserve_shortfall,
        "deployable_cash": deployable_cash,
        "idle_excess": idle_excess,
        "excess_cash_glide_fraction": round(glide_frac, 4),
        "glide_slice": glide_slice,
        "monthly_contribution_net_investable_base": contribution_base,
        "monthly_contribution_net_investable": net_investable,
        "monthly_capital_deployed_before_today": deployed_before_today,
        "capital_funded_today": round(capital_funded_today or 0.0, 2),
        "monthly_capital_deployed_total": deployed_total,
        "monthly_capital_remaining": remaining,
        "capital_held_for_reserve": reserve_shortfall,
        "capital_held_for_future_entries": held_for_future,
        "monthly_utilization_pct": utilization,
        "contribution_cycle": cycle_id,
        "contribution_cycle_start": cycle_start,
        "contribution_cycle_end": cycle_end,
        "monthly_history_status": monthly_history_status,
        "weekly_pacing": weekly_pacing,
        "rollover_behavior": "no_rollover: undeployed net-investable is not carried forward; "
                             "it remains cash and contributes to next cycle's excess.",
        "calculation_timestamp": ts,
        "portfolio_value_source": portfolio_value_source,
        "contribution_source": contribution_source,
        "cash_source": cash_source,
    }


def rank_deployable_decisions(
    decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Return BUY/SCALE rows sorted by priority desc, capped at _MAX_DECISIONS.

    Non-eligible decisions (HOLD/WAIT/AVOID/SELL) are filtered out so the
    output focuses only on capital deployment candidates.
    """
    eligible = [
        d for d in (decisions or [])
        if isinstance(d, dict)
        and _safe_str(d.get("decision")).upper() in _ELIGIBLE_DECISIONS
    ]
    eligible.sort(
        key=lambda d: _safe_float(d.get("priority")) or 0.0,
        reverse=True,
    )
    return eligible[:_MAX_DECISIONS]


def _band_multiplier(band: str | None) -> float:
    if not band:
        return 0.50  # treat unknown as normal-band size
    return _BAND_MULTIPLIERS.get(_safe_str(band).lower(), 0.50)


def allocate_deployment(
    *,
    deployable_amount: float,
    portfolio_value: float,
    ranked_decisions: list[dict[str, Any]],
    max_position_pct: float = _MAX_POSITION_PCT,
) -> list[dict[str, Any]]:
    """
    Distribute *deployable_amount* across *ranked_decisions* respecting:
      - the decision's own recommended_allocation_pct ceiling
      - the per-position cap (max_position_pct of portfolio_value)
      - the conviction-band sizing multiplier
      - the running remaining budget

    Each decision either receives a positive allocation or appears with
    suggested_amount=0 and a `skipped_reason` string. No decision is
    dropped silently — caller can see why.
    """
    rows: list[dict[str, Any]] = []
    remaining = deployable_amount
    pos_cap_amount = max_position_pct * portfolio_value

    for d in ranked_decisions:
        symbol = _safe_str(d.get("symbol")).upper()
        decision = _safe_str(d.get("decision")).upper()
        priority = _safe_float(d.get("priority")) or 0.0
        recommended_pct = _safe_float(d.get("recommended_allocation_pct"))
        band = _safe_str((d.get("inputs_used") or {}).get("conviction_band") or
                         d.get("conviction_band"))

        if remaining <= 0:
            rows.append({
                "symbol": symbol,
                "decision": decision,
                "priority": priority,
                "suggested_amount": 0.0,
                "suggested_pct": 0.0,
                "skipped_reason": "budget exhausted",
            })
            continue

        # Start from the decision's own recommended allocation if provided,
        # otherwise fall back to band-derived sizing of 1% of portfolio.
        if recommended_pct is not None and recommended_pct > 0:
            base_amount = recommended_pct * portfolio_value
        else:
            base_amount = 0.01 * portfolio_value

        sized = base_amount * _band_multiplier(band)
        capped_amount = min(sized, pos_cap_amount, remaining)
        capped_amount = round(max(0.0, capped_amount), 2)

        if capped_amount <= 0:
            rows.append({
                "symbol": symbol,
                "decision": decision,
                "priority": priority,
                "suggested_amount": 0.0,
                "suggested_pct": 0.0,
                "skipped_reason": "sized to zero by band multiplier or cap",
            })
            continue

        rows.append({
            "symbol": symbol,
            "decision": decision,
            "priority": priority,
            "conviction_band": band or "unknown",
            "suggested_amount": capped_amount,
            "suggested_pct": round(capped_amount / portfolio_value, 4)
                             if portfolio_value > 0 else 0.0,
            "skipped_reason": None,
        })
        remaining -= capped_amount

    return rows


def allocate_within_envelope(
    *,
    monthly_capital_remaining_before_today: float | None,
    net_investable: float,
    portfolio_value: float,
    ranked_decisions: list[dict[str, Any]],
    bands: dict[str, float],
    sector_map: dict[str, str] | None = None,
    max_position_pct: float = _MAX_POSITION_PCT,
    cycle_remaining: float | None = None,
) -> list[dict[str, Any]]:
    """Allocate within TODAY's paced budget (weekly tranche, not the full net).

    ``monthly_capital_remaining_before_today`` is the effective budget cap for
    today (the weekly/daily tranche under paced cadence, or the cycle remaining
    under monthly cadence). ``cycle_remaining`` is the cycle-level budget left
    (before today) — when a name can't be funded because the *paced* budget is
    exhausted but cycle budget remains, it is deferred with the precise
    ``DEFERRED_BY_WEEKLY_PACING`` status (distinct from a cycle-exhausted
    ``DEFERRED_BY_MONTHLY_BUDGET``).

    Assigns a precise status per row, a tranche type, portfolio-relative sizing,
    and (for extended entries) a starter tranche with the rest held for pullback.
    """
    sector_map = sector_map or {}
    starter_amt = round(bands["starter_position_pct"] * portfolio_value, 2)
    standard_amt = round(bands["standard_position_pct"] * portfolio_value, 2)
    max_new_amt = round(bands["max_new_position_pct_per_cycle"] * portfolio_value, 2)
    theme_cap_amt = round(bands["theme_cap_pct_of_net_investable"] * net_investable, 2) \
        if net_investable > 0 else 0.0

    rows: list[dict[str, Any]] = []
    # No net-investable at all → genuinely cash-blocked.
    no_capital = net_investable <= 0
    # If history unavailable, treat the full net_investable as the budget but the
    # envelope block flags the cycle remaining as unknown (honest degrade).
    budget = (net_investable if monthly_capital_remaining_before_today is None
              else monthly_capital_remaining_before_today)
    theme_funded: dict[str, float] = {}
    funded_so_far = 0.0

    def _budget_deferral_status() -> str:
        # Cycle budget still has room but the paced (weekly/daily) budget is
        # spent → weekly-pacing deferral; otherwise the whole cycle is spent.
        room_basis = cycle_remaining if cycle_remaining is not None else budget
        cycle_room_left = (room_basis or 0.0) - funded_so_far
        return (STATUS_DEFERRED_BY_WEEKLY_PACING if cycle_room_left > 0.005
                else STATUS_DEFERRED_BY_MONTHLY_BUDGET)

    def _row(d, amount, status, tranche, held_pullback=0.0):
        sym = _safe_str(d.get("symbol")).upper()
        sector = sector_map.get(sym)
        return {
            "symbol": sym,
            "decision": _safe_str(d.get("decision")).upper(),
            "priority": _safe_float(d.get("priority")) or 0.0,
            "conviction_band": _safe_str((d.get("inputs_used") or {}).get("conviction_band")
                                         or d.get("conviction_band")) or "unknown",
            "suggested_amount": round(amount, 2),
            "suggested_pct": round(amount / portfolio_value, 4) if portfolio_value > 0 and amount > 0 else 0.0,
            "pct_of_portfolio": round(100.0 * amount / portfolio_value, 3) if portfolio_value > 0 and amount > 0 else 0.0,
            "pct_of_net_investable": round(100.0 * amount / net_investable, 1) if net_investable > 0 and amount > 0 else 0.0,
            "tranche_type": tranche,
            "status": status,
            "session_move_pct": _session_move_pct(d.get("reason") or d.get("decision_reason")),
            "entry_extended": tranche == "starter_extended",
            "held_for_pullback": round(held_pullback, 2),
            "sector": sector,
            # back-compat: legacy consumers read skipped_reason (None when funded)
            "skipped_reason": None if amount > 0 else status,
        }

    for d in ranked_decisions:
        if no_capital:
            rows.append(_row(d, 0.0, STATUS_BLOCKED_BY_CASH, "none"))
            continue
        if budget <= 0:
            rows.append(_row(d, 0.0, _budget_deferral_status(), "none"))
            continue

        session_move = _session_move_pct(d.get("reason") or d.get("decision_reason"))
        extended = session_move is not None and session_move >= _EXTENDED_SESSION_PCT
        if extended:
            target = min(starter_amt, max_new_amt)
            tranche = "starter_extended"
        else:
            target = min(standard_amt, max_new_amt)
            tranche = "standard"

        amount = min(target, budget)

        sym = _safe_str(d.get("symbol")).upper()
        sector = sector_map.get(sym)
        if sector and theme_cap_amt > 0:
            room = max(0.0, theme_cap_amt - theme_funded.get(sector, 0.0))
            if room <= 0:
                rows.append(_row(d, 0.0, STATUS_DEFERRED_BY_THEME_CAP, "none"))
                continue
            amount = min(amount, room)

        amount = round(max(0.0, amount), 2)
        if amount <= 0:
            rows.append(_row(d, 0.0, _budget_deferral_status(), "none"))
            continue

        status = STATUS_FUNDED_STARTER if extended or amount < standard_amt - 0.005 else STATUS_FUNDED_STANDARD
        held_pullback = round(max(0.0, standard_amt - amount), 2) if extended else 0.0
        rows.append(_row(d, amount, status, tranche, held_pullback))
        budget -= amount
        funded_so_far += amount
        if sector:
            theme_funded[sector] = theme_funded.get(sector, 0.0) + amount

    return rows


def _sector_map_from_cache(base_dir: Path | str, symbols: list[str]) -> dict[str, str]:
    """Best-effort symbol -> industry/sector from the read-only FMP profile cache.

    Prefers the finer `industry` (e.g. 'Semiconductors') over `sector`. Returns
    only symbols with a canonical classification; callers degrade honestly when
    coverage is incomplete. No network access; no name-based inference.
    """
    out: dict[str, str] = {}
    cache_dir = Path(base_dir).parent / "data" / "fmp_cache" if (Path(base_dir).name == "outputs") \
        else Path("data") / "fmp_cache"
    # The repo layout keeps fmp_cache at repo-root/data/fmp_cache regardless of
    # the outputs base_dir; try both repo-root and base_dir-relative.
    candidates = [Path("data") / "fmp_cache", cache_dir]
    for sym in symbols:
        for cdir in candidates:
            p = cdir / f"profile_stable_{sym}.json"
            if not p.exists():
                continue
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            rec = payload
            if isinstance(rec, dict) and "data" in rec:
                rec = rec["data"]
            if isinstance(rec, list) and rec:
                rec = rec[0]
            if isinstance(rec, dict):
                cls = _safe_str(rec.get("industry")) or _safe_str(rec.get("sector"))
                if cls:
                    out[sym] = cls
            break
    return out


def compute_concentration(
    deployment_rows: list[dict[str, Any]],
    *,
    net_investable: float,
    theme_cap_pct: float,
    sector_map: dict[str, str] | None,
) -> dict[str, Any]:
    """Funded capital grouped by sector/theme, with remaining theme-cap capacity.

    Degrades honestly when no canonical classification is available — never
    infers a theme from a ticker symbol.
    """
    funded = [r for r in deployment_rows if (r.get("suggested_amount") or 0) > 0]
    total_funded = round(sum(r.get("suggested_amount", 0.0) for r in funded), 2)
    if not sector_map or not any(r.get("sector") for r in funded):
        return {
            "available": False,
            "reason": "no_canonical_sector_theme_classification",
            "total_funded_today": total_funded,
        }
    theme_cap_amt = round(theme_cap_pct * net_investable, 2) if net_investable > 0 else 0.0
    by_theme: dict[str, float] = {}
    for r in funded:
        sec = r.get("sector")
        if sec:
            by_theme[sec] = round(by_theme.get(sec, 0.0) + r.get("suggested_amount", 0.0), 2)
    themes = []
    for sec, amt in sorted(by_theme.items(), key=lambda kv: kv[1], reverse=True):
        themes.append({
            "theme": sec,
            "funded_today": amt,
            "pct_of_today_funded": round(100.0 * amt / total_funded, 1) if total_funded > 0 else 0.0,
            "pct_of_net_investable": round(100.0 * amt / net_investable, 1) if net_investable > 0 else 0.0,
            "remaining_under_theme_cap": round(max(0.0, theme_cap_amt - amt), 2),
        })
    classified = sum(1 for r in funded if r.get("sector"))
    return {
        "available": True,
        "theme_cap_pct_of_net_investable": round(theme_cap_pct, 4),
        "theme_cap_amount": theme_cap_amt,
        "total_funded_today": total_funded,
        "classification_coverage": f"{classified}/{len(funded)}",
        "themes": themes,
    }


# ---------------------------------------------------------------------------
# Plan envelope
# ---------------------------------------------------------------------------


def build_plan(
    *,
    cash_summary: dict[str, Any],
    deployment_rows: list[dict[str, Any]],
    degraded_mode: bool,
    data_mode: str,
    notes: list[str],
    monthly_capital_envelope: dict[str, Any] | None = None,
    concentration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    total_deployed = round(
        sum(r.get("suggested_amount", 0.0) for r in deployment_rows), 2
    )
    plan = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "no_trade": True,
        "schema_version": "2",  # v2 adds monthly_capital_envelope + concentration
        "degraded_mode": bool(degraded_mode),
        "data_mode": data_mode or "unknown",
        "cash_summary": cash_summary,
        "deployment_rows": deployment_rows,
        "total_deployed_amount": total_deployed,
        "remaining_budget": round(
            max(0.0, cash_summary.get("total_deployable_amount", 0.0) - total_deployed),
            2,
        ),
        "summary_line": (
            f"Cash deployment: ${total_deployed:.2f} across "
            f"{sum(1 for r in deployment_rows if r.get('suggested_amount', 0) > 0)} "
            f"position(s); budget ${cash_summary.get('total_deployable_amount', 0.0):.2f}"
        ),
        "notes": list(notes),
    }
    if monthly_capital_envelope is not None:
        plan["monthly_capital_envelope"] = monthly_capital_envelope
    if concentration is not None:
        plan["concentration"] = concentration
    return plan


def _render_markdown(plan: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Cash Deployment Plan")
    lines.append("")
    lines.append(f"_Generated: {plan.get('generated_at')}_")
    lines.append("")
    lines.append("Observe-only. No trades are executed.")
    lines.append("")
    lines.append(plan.get("summary_line", ""))
    lines.append("")
    cs = plan.get("cash_summary", {})
    lines.append("## Cash budget")
    lines.append("")
    lines.append(f"- Portfolio value: ${cs.get('portfolio_value', 0):,.2f}")
    lines.append(f"- Cash available: ${cs.get('cash_available', 0):,.2f} "
                 f"({(cs.get('current_cash_pct') or 0):.1%})")
    lines.append(f"- Target cash %: {(cs.get('target_cash_pct') or 0):.1%}")
    lines.append(f"- Excess cash %: {(cs.get('excess_cash_pct') or 0):.1%}")
    lines.append(f"- Monthly contribution (deposited) %: {(cs.get('incoming_pct') or 0):.1%}")
    lines.append(f"- Deployable: ${(cs.get('total_deployable_amount') or 0):,.2f}")
    lines.append("")

    env = plan.get("monthly_capital_envelope") or {}
    pacing = env.get("weekly_pacing") or {}
    if env.get("status") == "ok" and pacing:
        wk_rem = pacing.get("weekly_remaining")
        lines.append("## Weekly Deployment")
        lines.append("")
        glide = env.get("glide_slice") or 0.0
        base = env.get("monthly_contribution_net_investable_base") or 0.0
        net = env.get("monthly_contribution_net_investable") or 0.0
        if pacing.get("deploy_cadence") != "monthly" and wk_rem is not None:
            lines.append(
                f"- This week ({pacing.get('deploy_cadence')}): "
                f"${(pacing.get('weekly_tranche') or 0):,.2f} target · "
                f"${(pacing.get('deployed_this_week') or 0):,.2f} deployed · "
                f"${wk_rem:,.2f} remaining this week"
            )
        lines.append(
            f"- Cycle: ${net:,.2f} net-investable "
            f"(${base:,.2f} contribution + ${glide:,.2f} glide) · "
            f"reserve ${(env.get('cash_reserve_target_amount') or 0):,.2f} protected"
        )
        if pacing.get("note"):
            lines.append(f"- {pacing.get('note')}")
        lines.append("")

    if plan.get("notes"):
        lines.append("## Notes")
        for n in plan["notes"]:
            lines.append(f"- {n}")
        lines.append("")
    lines.append("## Deployment plan")
    lines.append("")
    if not plan.get("deployment_rows"):
        lines.append("_No eligible BUY/SCALE decisions in current plan._")
    else:
        lines.append("| Symbol | Decision | Priority | Band | Amount | % | Note |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in plan["deployment_rows"]:
            amt = r.get("suggested_amount", 0)
            lines.append(
                "| {sym} | {dec} | {pri:.3f} | {band} | ${amt:,.2f} | {pct} | {note} |".format(
                    sym=r.get("symbol", "?"),
                    dec=r.get("decision", "?"),
                    pri=r.get("priority", 0) or 0,
                    band=r.get("conviction_band", "—"),
                    amt=amt,
                    pct=(f"{r.get('suggested_pct', 0):.1%}" if amt > 0 else "—"),
                    note=r.get("skipped_reason") or "",
                )
            )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


def _portfolio_value_from_context(
    decision_plan_payload: dict[str, Any],
    cfg: dict[str, Any],
) -> float:
    # 1) Preferred: top-level portfolio_context on the decision_plan envelope
    #    (main.py writes this since 2026-05-15).
    top_pc = decision_plan_payload.get("portfolio_context") or {}
    v = _safe_float(top_pc.get("total_portfolio_value"))
    if v and v > 0:
        return v
    # 2) Fallback: inputs_used.portfolio_context on any decision row (older runs)
    for d in decision_plan_payload.get("decisions") or []:
        if not isinstance(d, dict):
            continue
        iu = d.get("inputs_used") or {}
        pc = iu.get("portfolio_context") or {}
        v = _safe_float(pc.get("total_portfolio_value"))
        if v and v > 0:
            return v
    # 3) Last resort: cash-only fallback. Calling code treats current_cash_pct
    #    as 100% in this branch, which is correct given we have no other info.
    cash = _safe_float((cfg.get("portfolio") or {}).get("cash_available")) or 0.0
    return cash


# Broker snapshot considered fresh within this window (mirrors holdings_resolver).
_BROKER_STALE_AFTER_S = 24 * 3600


def _snapshot_fresh(snapshot: dict[str, Any], now: datetime) -> bool:
    ts = snapshot.get("snapshot_timestamp") or snapshot.get("generated_at")
    if not ts or not isinstance(ts, str):
        return False
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds() <= _BROKER_STALE_AFTER_S
    except ValueError:
        return False


def resolve_capital_basis(
    base_dir: Path,
    decision_plan_payload: dict[str, Any],
    cfg: dict[str, Any],
    now: datetime,
) -> tuple[float, float, str, str]:
    """Resolve (portfolio_value, cash_on_hand, pv_source, cash_source) for the
    advisory capital envelope, preferring the LIVE read-only Schwab snapshot.

    Precedence (read-only; never enables broker_aware, never feeds decision_plan):
      1. Schwab snapshot totals (authenticated + fresh) — totals.market_value is
         the TOTAL account value (securities + cash); totals.cash is cash on hand.
      2. decision_plan.portfolio_context (total_portfolio_value + cash) — the
         basis the live decision pipeline actually used.
      3. config.portfolio (cash_available).
    """
    latest = Path(base_dir) / "latest"
    snap = _load_json_safe(latest / "schwab_portfolio_snapshot.json")
    sync = _load_json_safe(latest / "broker_sync_status.json")
    totals = (snap.get("totals") or {}) if isinstance(snap, dict) else {}
    authed = bool(sync.get("authenticated")) and _safe_str(sync.get("overall_status")) == "ok"
    pv_b = _safe_float(totals.get("market_value"))
    cash_b = _safe_float(totals.get("cash"))
    if authed and _snapshot_fresh(snap, now) and pv_b and pv_b > 0 and cash_b is not None:
        return round(pv_b, 2), round(cash_b, 2), "schwab_snapshot", "schwab_snapshot"

    pc = decision_plan_payload.get("portfolio_context") or {}
    pv = _safe_float(pc.get("total_portfolio_value"))
    cash_ctx = _safe_float(pc.get("cash"))
    cfg_cash = _safe_float((cfg.get("portfolio") or {}).get("cash_available")) or 0.0
    if pv and pv > 0:
        if cash_ctx is not None:
            return round(pv, 2), round(cash_ctx, 2), "decision_plan.portfolio_context", "decision_plan.portfolio_context"
        return round(pv, 2), round(cfg_cash, 2), "decision_plan.portfolio_context", "config.portfolio.cash_available"
    return round(cfg_cash, 2), round(cfg_cash, 2), "config.portfolio.cash_available", "config.portfolio.cash_available"


def run_cash_deployment_plan(
    repo_root: Path | str,
    *,
    base_dir: Path | str = "outputs",
    as_of_date: date | None = None,
    run_id: str = "",
) -> dict[str, Any]:
    repo_root = Path(repo_root)
    base_dir = Path(base_dir)
    today = as_of_date or datetime.now(timezone.utc).date()
    today_iso = today.isoformat()

    cfg = _load_json_safe(repo_root / "config.json")
    decision_plan_path = base_dir / "latest" / "decision_plan.json"
    decision_plan_payload = _load_json_safe(decision_plan_path)
    system_summary = _load_json_safe(base_dir / "latest" / "system_decision_summary.json")

    portfolio_cfg = cfg.get("portfolio") or {}
    monthly_contribution = _safe_float(portfolio_cfg.get("monthly_contribution")) or 0.0
    target_cash_pct = _safe_float(portfolio_cfg.get("target_cash_weight")) or _DEFAULT_TARGET_CASH
    glide_fraction = portfolio_cfg.get("excess_cash_glide_fraction")
    glide_fraction = _DEFAULT_EXCESS_CASH_GLIDE_FRACTION if glide_fraction is None \
        else (_safe_float(glide_fraction) or 0.0)
    deploy_cadence = _safe_str(portfolio_cfg.get("deploy_cadence")).lower() or _DEFAULT_DEPLOY_CADENCE
    if deploy_cadence not in _VALID_DEPLOY_CADENCES:
        deploy_cadence = _DEFAULT_DEPLOY_CADENCE
    bands = capital_config(cfg)

    # Prefer the LIVE read-only Schwab balance for the advisory capital view, so
    # the memo + dashboard reflect the broker, not stale config. Read-only;
    # does not enable broker_aware or change the decision engine.
    portfolio_value, cash_available, pv_source, cash_source = resolve_capital_basis(
        base_dir, decision_plan_payload, cfg, datetime.now(timezone.utc)
    )

    # Legacy cash_summary block (kept for backward-compat consumers).
    cash_summary = compute_available_cash(
        portfolio_value=portfolio_value,
        cash_available=cash_available,
        target_cash_pct=target_cash_pct,
        monthly_contribution=monthly_contribution,
    )

    data_health = (system_summary.get("data_health") or {})
    degraded_mode = bool(data_health.get("degraded_mode", False))
    data_mode = _safe_str(data_health.get("data_mode")) or "unknown"

    # --- Monthly capital envelope -----------------------------------------
    cycle_id, cycle_start, cycle_end = contribution_cycle(today)
    before, history_status = resolve_prior_deployment(
        base_dir, cycle_id, cycle_start, today_iso
    )
    # Cycle net-investable = contribution (net of reserve shortfall) + glide slice
    # of idle excess cash (spec 2026-07-07). Mirrors compute_monthly_envelope.
    if portfolio_value and portfolio_value > 0:
        _reserve_target_amt = round(target_cash_pct * portfolio_value, 2)
        _reserve_shortfall = round(max(0.0, _reserve_target_amt - cash_available), 2)
        _contribution_base = round(max(0.0, monthly_contribution - _reserve_shortfall), 2)
        _deployable_cash = round(max(0.0, cash_available - _reserve_target_amt), 2)
        _idle_excess = round(max(0.0, _deployable_cash - monthly_contribution), 2)
        _glide_frac = max(0.0, min(1.0, glide_fraction))
        net_investable = round(_contribution_base + round(_idle_excess * _glide_frac, 2), 2)
    else:
        net_investable = 0.0

    # Weekly deployment pacing — derive today's paced budget from the cycle ledger
    # (no new state file). budget_today is the cap the allocator sizes against;
    # cycle_remaining distinguishes weekly-pacing deferral from cycle exhaustion.
    ledger_rows, _ledger_read_status = read_deployment_ledger(base_dir)
    try:
        _cycle_end_date = date.fromisoformat(cycle_end)
    except ValueError:
        _cycle_end_date = today
    pacing = compute_weekly_pacing(
        cycle_net_investable=net_investable,
        deployed_before_today=before,
        deployed_this_week_before_today=deployed_in_iso_week(ledger_rows, cycle_id, today),
        weeks_remaining_in_cycle=iso_weeks_remaining_in_cycle(today, _cycle_end_date),
        weekday_days_remaining=weekday_days_remaining_in_week(today),
        deploy_cadence=deploy_cadence,
        monthly_history_status=history_status,
    )
    budget_today = pacing["budget_today"]      # None when history unavailable → full net
    cycle_remaining = pacing["cycle_remaining"]

    notes: list[str] = []
    deployment_rows: list[dict[str, Any]] = []
    sector_map: dict[str, str] = {}

    if degraded_mode:
        notes.append("degraded_mode active — deployment suspended this cycle")
    elif portfolio_value <= 0:
        notes.append("portfolio_value unavailable — cannot size positions")
    elif net_investable <= 0:
        notes.append("no net-investable capital this cycle (contribution consumed by reserve restoration)")
        ranked = rank_deployable_decisions(decision_plan_payload.get("decisions") or [])
        deployment_rows = allocate_within_envelope(
            monthly_capital_remaining_before_today=budget_today,
            net_investable=net_investable, portfolio_value=portfolio_value,
            ranked_decisions=ranked, bands=bands, sector_map={},
            cycle_remaining=cycle_remaining,
        )
    else:
        ranked = rank_deployable_decisions(decision_plan_payload.get("decisions") or [])
        if not ranked:
            notes.append("no BUY/SCALE decisions in current decision_plan")
        symbols = [_safe_str(d.get("symbol")).upper() for d in ranked]
        sector_map = _sector_map_from_cache(base_dir, symbols)
        deployment_rows = allocate_within_envelope(
            monthly_capital_remaining_before_today=budget_today,
            net_investable=net_investable, portfolio_value=portfolio_value,
            ranked_decisions=ranked, bands=bands, sector_map=sector_map,
            cycle_remaining=cycle_remaining,
        )
        if history_status == "partial":
            notes.append("monthly deployment ledger initialized mid-cycle — prior deployment may be undercounted")
        if history_status == "unavailable":
            notes.append("monthly_history_status: unavailable — cycle remaining cannot be confirmed")

    capital_funded_today = round(
        sum(r.get("suggested_amount", 0.0) for r in deployment_rows), 2
    )

    # Record today's deployment (append-only; idempotent via last-wins read).
    if not degraded_mode and portfolio_value and portfolio_value > 0 and history_status != "unavailable":
        try:
            append_deployment_ledger(
                base_dir, cycle_id=cycle_id, today_iso=today_iso,
                capital_funded=capital_funded_today, run_id=run_id,
            )
        except OSError as exc:
            logger.warning("cash_deployment_plan: ledger append failed (non-fatal): %s", exc)

    envelope = compute_monthly_envelope(
        portfolio_value=portfolio_value if portfolio_value and portfolio_value > 0 else None,
        cash_on_hand=cash_available,
        monthly_contribution_gross=monthly_contribution,
        reserve_pct=target_cash_pct,
        deployed_before_today=before,
        capital_funded_today=capital_funded_today,
        cycle_id=cycle_id, cycle_start=cycle_start, cycle_end=cycle_end,
        monthly_history_status=history_status,
        excess_cash_glide_fraction=glide_fraction,
        weekly_pacing=pacing,
        portfolio_value_source=pv_source,
        cash_source=cash_source,
    )

    concentration = compute_concentration(
        deployment_rows,
        net_investable=net_investable,
        theme_cap_pct=bands["theme_cap_pct_of_net_investable"],
        sector_map=sector_map,
    )

    plan = build_plan(
        cash_summary=cash_summary,
        deployment_rows=deployment_rows,
        degraded_mode=degraded_mode,
        data_mode=data_mode,
        notes=notes,
        monthly_capital_envelope=envelope,
        concentration=concentration,
    )

    try:
        safe_write_json(
            OutputNamespace.LATEST,
            "cash_deployment_plan.json",
            plan,
            base_dir=base_dir,
        )
        safe_write_text(
            OutputNamespace.LATEST,
            "cash_deployment_plan.md",
            _render_markdown(plan),
            base_dir=base_dir,
        )
    except Exception as exc:
        logger.warning(
            "cash_deployment_plan: failed to write artifacts (non-fatal): %s", exc
        )

    return plan
