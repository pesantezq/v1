from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from watchlist_scanner.alert_filter import cooldown_decision
from watchlist_scanner.models import PortfolioContext, WatchlistRow, WatchlistScanResult
from watchlist_scanner.state import WatchlistStateStore

logger = logging.getLogger("watchlist_scanner")


def _make_alert_fingerprint(result: WatchlistRow) -> str:
    """
    Stable identity string for a watchlist alert.

    Uses ticker + watchlist_source + primary trigger type so the fingerprint
    is consistent across runs for the same underlying condition.
    Hard-codes the 3.0% price threshold to keep it stable if config changes.
    """
    ticker = result.get("ticker", "").upper()
    source = result.get("watchlist_source", "static")
    if result.get("volume_spike"):
        trigger = "volume_spike"
    elif abs(result.get("price_change_pct") or 0.0) >= 3.0:
        trigger = "price_move"
    else:
        trigger = "signal_score"
    return f"{ticker}|{source}|{trigger}"


def _make_alert_state_hash(result: WatchlistRow) -> str:
    """
    Content fingerprint for material-change detection.

    Buckets signal and confidence scores to the nearest 0.1 so minor
    noise does not break cooldown, but real moves (>= 0.1 delta) do.
    Also captures confidence_band, data_quality, and alert_priority so
    any meaningful quality shift breaks cooldown automatically.
    """
    signal_bucket = round(float(result.get("signal_score") or 0) * 10) / 10
    conf_bucket   = round(float(result.get("confidence_score") or 0) * 10) / 10
    raw = "|".join([
        str(signal_bucket),
        str(conf_bucket),
        result.get("confidence_band", ""),
        result.get("data_quality", ""),
        result.get("alert_priority", "normal"),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _apply_alert_cooldown(
    scan_result: WatchlistScanResult,
    db_path: str | Path = "data/portfolio.db",
    cooldown_days: int = 3,
    signals_config: dict | None = None,
) -> WatchlistScanResult:
    """
    Suppress unchanged alerts within the configured cooldown window.

    Reuses the shared alert_events table in PortfolioStateStore so repeat
    watchlist alerts do not keep surfacing every run with identical state.
    The alert_priority remains the intrinsic routing decision; the
    notification_status records whether the alert is newly surfaced or
    suppressed by cooldown for this run.
    """
    alerts = list(scan_result.get("alerts") or [])
    results = list(scan_result.get("results") or [])
    summary = scan_result.setdefault("scan_summary", {})
    summary.setdefault("alerts_cooldown_suppressed", 0)

    if cooldown_days <= 0 or not alerts:
        for row in results:
            row.setdefault("notification_status", "alerted" if row.get("alert_priority") else "not_alerting")
            row.setdefault("notification_reason", "")
        return scan_result

    store = WatchlistStateStore(Path(db_path))
    kept_alerts: list[WatchlistRow] = []
    suppressed_by_ticker: dict[str, str] = {}
    outcomes_by_ticker: dict[str, dict] = {}
    cooldown_meta_by_ticker: dict[str, dict[str, object]] = {}

    for alert in alerts:
        fingerprint = _make_alert_fingerprint(alert)
        state_hash = _make_alert_state_hash(alert)
        alert["alert_state_hash"] = state_hash
        severity = str(alert.get("alert_priority") or "")
        current_state = store.get_alert_state(fingerprint)
        cooldown_info = cooldown_decision(
            alert,
            current_state,
            signals_config,
        )
        suppress = not bool(cooldown_info["allowed"])
        store.touch_alert_state(
            fingerprint,
            severity=severity,
            state_hash=state_hash,
            alert_tier=str(alert.get("alert_tier") or ""),
            reason_code=str(alert.get("filter_reason_code") or ""),
            last_signal_score=float(alert.get("signal_score") or 0.0),
            last_confidence_score=float(alert.get("confidence_score") or 0.0),
            last_action_taken="cooldown_suppressed" if suppress else "alerted",
        )

        if suppress:
            outcome = store.get_alert_lifecycle(fingerprint, state_hash=state_hash)
            _attach_outcome_tracking(alert, outcome)
            ticker_key = str(alert.get("ticker") or "").upper()
            outcomes_by_ticker[ticker_key] = outcome or {}
            cooldown_label = int(cooldown_info.get("cooldown_applied_hours") or (cooldown_days * 24))
            reason = (
                f"cooldown-suppressed: unchanged {str(alert.get('alert_tier') or severity or 'alert')} "
                f"within {cooldown_label}h window"
            )
            alert["notification_status"] = "cooldown_suppressed"
            alert["notification_reason"] = reason
            alert["cooldown_applied_hours"] = cooldown_label
            alert["cooldown_override_reason"] = ""
            suppressed_by_ticker[ticker_key] = reason
            cooldown_meta_by_ticker[ticker_key] = {
                "cooldown_applied_hours": cooldown_label,
                "cooldown_override_reason": "",
            }
            continue

        alert["notification_status"] = "alerted"
        alert["notification_reason"] = ""
        alert["cooldown_applied_hours"] = cooldown_info.get("cooldown_applied_hours")
        alert["cooldown_override_reason"] = str(cooldown_info.get("override_reason") or "")
        outcome = store.record_alert_surface(
            fingerprint=fingerprint,
            state_hash=state_hash,
            alert_data=alert,
        )
        _attach_outcome_tracking(alert, outcome)
        ticker_key = str(alert.get("ticker") or "").upper()
        outcomes_by_ticker[ticker_key] = outcome
        cooldown_meta_by_ticker[ticker_key] = {
            "cooldown_applied_hours": alert.get("cooldown_applied_hours"),
            "cooldown_override_reason": alert.get("cooldown_override_reason", ""),
        }
        store.mark_alert_notified(fingerprint)
        kept_alerts.append(alert)

    for row in results:
        sym = str(row.get("ticker") or "").upper()
        if sym in outcomes_by_ticker:
            _attach_outcome_tracking(row, outcomes_by_ticker[sym])
        if sym in cooldown_meta_by_ticker:
            row["cooldown_applied_hours"] = cooldown_meta_by_ticker[sym].get("cooldown_applied_hours")
            row["cooldown_override_reason"] = str(cooldown_meta_by_ticker[sym].get("cooldown_override_reason") or "")
        if sym in suppressed_by_ticker:
            row["notification_status"] = "cooldown_suppressed"
            row["notification_reason"] = suppressed_by_ticker[sym]
        elif row.get("alert_priority") is not None:
            row["notification_status"] = "alerted"
            row["notification_reason"] = ""
        else:
            row["notification_status"] = "not_alerting"
            row["notification_reason"] = str(row.get("filter_reason") or "")

    summary["alerts_cooldown_suppressed"] = max(0, len(alerts) - len(kept_alerts))
    scan_result["alerts"] = kept_alerts
    return scan_result


def _action_filter_cfg(signals_config: dict[str, Any] | None) -> dict[str, float]:
    cfg = signals_config or {}
    action_cfg = cfg.get("action_filter") if isinstance(cfg.get("action_filter"), dict) else {}
    base_min_confidence = float(cfg.get("min_confidence_score", 0.50))
    return {
        "min_confidence_score": float(action_cfg.get("min_confidence_score", max(base_min_confidence, 0.55))),
        "min_degraded_confidence_score": float(
            action_cfg.get("min_degraded_confidence_score", max(base_min_confidence + 0.10, 0.65))
        ),
        "high_confidence_score": float(action_cfg.get("high_confidence_score", 0.85)),
        "strong_signal_score": float(action_cfg.get("strong_signal_score", 0.75)),
    }


def _annotate_signal_meta(
    row: WatchlistRow,
    *,
    data_health: dict[str, Any],
    prior_state: dict[str, Any] | None = None,
) -> None:
    signal_score = float(row.get("signal_score") or 0.0)
    confidence_score = float(row.get("confidence_score") or 0.0)
    penalty = float(data_health.get("degraded_confidence_penalty", 0.0) or 0.0)
    effective_score = signal_score * confidence_score
    if data_health.get("degraded_mode"):
        effective_score *= max(0.0, 1.0 - penalty)

    row["confidence_weight"] = round(confidence_score, 3)
    row["effective_score"] = round(max(0.0, effective_score), 3)
    row["cooldown_active"] = row.get("notification_status") == "cooldown_suppressed"
    row["cooldown_reason"] = str(row.get("notification_reason") or "") if row["cooldown_active"] else ""
    row.setdefault("action_suppressed", False)
    row.setdefault("action_suppression_reason", "")

    if prior_state:
        row["last_alert_timestamp"] = prior_state.get("last_emailed")
        row["last_action_taken"] = str(prior_state.get("last_action_taken") or "")
        previous_signal = prior_state.get("last_signal_score")
        row["recent_signal_strength"] = round(
            float(previous_signal) if previous_signal is not None else signal_score,
            3,
        )
    else:
        row["last_alert_timestamp"] = None
        row["last_action_taken"] = ""
        row["recent_signal_strength"] = round(signal_score, 3)

    row.setdefault("actionable_signal", row.get("notification_status") == "alerted")


def _confidence_action_decision(
    row: WatchlistRow,
    *,
    data_health: dict[str, Any],
    signals_config: dict[str, Any] | None = None,
) -> dict[str, str | bool]:
    cfg = _action_filter_cfg(signals_config)
    confidence_score = float(row.get("confidence_score") or 0.0)
    signal_score = float(row.get("signal_score") or 0.0)
    degraded_penalty = float(data_health.get("degraded_confidence_penalty", 0.0) or 0.0)
    degraded_confidence = max(0.0, confidence_score - degraded_penalty)

    if row.get("cooldown_override_reason"):
        return {"allowed": True, "reason": ""}

    if (
        confidence_score >= cfg["high_confidence_score"]
        and signal_score >= cfg["strong_signal_score"]
    ):
        return {"allowed": True, "reason": ""}

    if data_health.get("degraded_mode") and degraded_confidence < cfg["min_degraded_confidence_score"]:
        return {
            "allowed": False,
            "reason": (
                "confidence-aware action filter: degraded data mode lowered confidence "
                f"to {degraded_confidence:.2f}"
            ),
        }

    if confidence_score < cfg["min_confidence_score"]:
        return {
            "allowed": False,
            "reason": (
                "confidence-aware action filter: confidence "
                f"{confidence_score:.2f} below actionable threshold"
            ),
        }

    return {"allowed": True, "reason": ""}


def _apply_signal_meta_layer(
    scan_result: WatchlistScanResult,
    *,
    data_health: dict[str, Any],
    db_path: str | Path = "data/portfolio.db",
    signals_config: dict[str, Any] | None = None,
) -> WatchlistScanResult:
    """
    Add confidence-aware metadata and a lightweight output-only alert filter.

    This layer does not change base scores, ranking keys, or alert generation.
    It annotates rows with effective_score/cooldown metadata and can suppress
    final emitted alerts when confidence is too weak for actionability.
    """
    results = list(scan_result.get("results") or [])
    alerts = list(scan_result.get("alerts") or [])
    summary = scan_result.setdefault("scan_summary", {})
    summary.setdefault("alerts_action_suppressed", 0)
    summary.setdefault("signals_suppressed", 0)
    summary.setdefault("cooldown_hits", 0)

    store = WatchlistStateStore(Path(db_path))
    state_by_ticker: dict[str, dict[str, Any] | None] = {}
    for row in results:
        ticker = str(row.get("ticker") or "").upper()
        state = None
        if ticker:
            state = store.get_alert_state(_make_alert_fingerprint(row))
            state_by_ticker[ticker] = state
        _annotate_signal_meta(row, data_health=data_health, prior_state=state)

    kept_alerts: list[WatchlistRow] = []
    suppressed_by_ticker: dict[str, str] = {}
    for alert in alerts:
        ticker = str(alert.get("ticker") or "").upper()
        state = state_by_ticker.get(ticker)
        _annotate_signal_meta(alert, data_health=data_health, prior_state=state)
        if alert.get("alert_type") == "opportunity":
            alert["actionable_signal"] = False
            alert["action_suppressed"] = False
            alert["action_suppression_reason"] = ""
            kept_alerts.append(alert)
            continue
        decision = _confidence_action_decision(
            alert,
            data_health=data_health,
            signals_config=signals_config,
        )
        if bool(decision["allowed"]):
            alert["actionable_signal"] = True
            kept_alerts.append(alert)
            continue

        alert["actionable_signal"] = False
        alert["action_suppressed"] = True
        alert["action_suppression_reason"] = str(decision["reason"] or "")
        suppressed_by_ticker[ticker] = alert["action_suppression_reason"]

    for row in results:
        ticker = str(row.get("ticker") or "").upper()
        if row.get("notification_status") == "cooldown_suppressed":
            row["actionable_signal"] = False
            row["cooldown_active"] = True
            row["cooldown_reason"] = str(row.get("notification_reason") or "")
            continue
        if row.get("notification_status") == "fallback_opportunity":
            row["actionable_signal"] = False
            row["action_suppressed"] = False
            row["action_suppression_reason"] = ""
            row["cooldown_active"] = False
            row["cooldown_reason"] = ""
            continue
        if ticker in suppressed_by_ticker:
            row["actionable_signal"] = False
            row["action_suppressed"] = True
            row["action_suppression_reason"] = suppressed_by_ticker[ticker]
            row["cooldown_active"] = False
            row["cooldown_reason"] = ""
            continue
        row["actionable_signal"] = row.get("notification_status") == "alerted"

    summary["alerts_action_suppressed"] = len(suppressed_by_ticker)
    summary["cooldown_hits"] = sum(1 for row in results if row.get("cooldown_active"))
    summary["signals_suppressed"] = sum(
        1
        for row in results
        if row.get("cooldown_active") or row.get("action_suppressed")
    )
    scan_result["alerts"] = kept_alerts
    return scan_result


def _attach_outcome_tracking(row: WatchlistRow, outcome: dict | None) -> None:
    """Copy persisted lifecycle metadata onto an alert/result row."""
    if not outcome:
        return
    row["alert_event_id"] = outcome.get("id")
    row["surfaced_at"] = outcome.get("surfaced_at")
    row["baseline_price"] = outcome.get("baseline_price")
    row["evaluation_window"] = outcome.get("evaluation_window")
    row["outcome_status"] = outcome.get("outcome_status", "pending")
    row["outcome_pending"] = bool(outcome.get("outcome_pending", 1))


def _holding_value(holding: dict | object, key: str, default=None):
    if isinstance(holding, dict):
        return holding.get(key, default)
    return getattr(holding, key, default)


def _normalize_portfolio_context(
    portfolio_context: PortfolioContext | None,
    results: list[WatchlistRow],
) -> dict:
    portfolio_context = portfolio_context or {}
    holdings = list(portfolio_context.get("holdings") or [])
    holding_symbols = {
        str(_holding_value(h, "symbol", "") or "").upper()
        for h in holdings
        if _holding_value(h, "symbol", "")
    }
    core_symbols = {
        str(_holding_value(h, "symbol", "") or "").upper()
        for h in holdings
        if _holding_value(h, "symbol", "") and str(_holding_value(h, "asset_class", "") or "") != "speculative"
    }
    speculative_symbols = holding_symbols - core_symbols

    held_theme_counts = dict(portfolio_context.get("held_theme_counts") or {})
    held_sector_counts = dict(portfolio_context.get("held_sector_counts") or {})
    if not held_theme_counts or not held_sector_counts:
        for row in results:
            sym = str(row.get("ticker") or "").upper()
            if sym not in holding_symbols:
                continue
            for theme in row.get("themes") or []:
                held_theme_counts[theme] = int(held_theme_counts.get(theme, 0)) + 1
            sector = str((row.get("fundamentals") or {}).get("sector") or "").strip()
            if sector:
                held_sector_counts[sector] = int(held_sector_counts.get(sector, 0)) + 1

    return {
        "holding_symbols": holding_symbols,
        "core_symbols": core_symbols,
        "speculative_symbols": speculative_symbols,
        "available_cash": portfolio_context.get("cash_available"),
        "target_cash_weight": portfolio_context.get("target_cash_weight"),
        "held_theme_counts": held_theme_counts,
        "held_sector_counts": held_sector_counts,
    }


def _apply_portfolio_priority_overlay(
    scan_result: WatchlistScanResult,
    portfolio_context: PortfolioContext | None = None,
) -> WatchlistScanResult:
    """
    Annotate results with portfolio-aware ranking context.

    This overlay adjusts operator ordering only. It does not modify
    signal_score, confidence_score, or alert routing decisions.
    """
    results = list(scan_result.get("results") or [])
    normalized = _normalize_portfolio_context(portfolio_context, results)
    holding_symbols = normalized["holding_symbols"]
    core_symbols = normalized["core_symbols"]
    available_cash = normalized.get("available_cash")
    held_theme_counts = normalized["held_theme_counts"]
    held_sector_counts = normalized["held_sector_counts"]

    for row in results:
        ticker = str(row.get("ticker") or "").upper()
        themes = list(row.get("themes") or [])
        sector = str((row.get("fundamentals") or {}).get("sector") or "").strip()
        price = row.get("price")

        is_existing_holding = ticker in holding_symbols
        is_core_holding = ticker in core_symbols
        overlapping_themes = sorted({theme for theme in themes if theme in held_theme_counts}) if not is_existing_holding else []
        theme_overlap_penalty = min(2, len(overlapping_themes))
        sector_overlap_penalty = 0
        if not is_existing_holding and sector and sector in held_sector_counts and not overlapping_themes:
            sector_overlap_penalty = 1
        overlap_penalty = float(theme_overlap_penalty + sector_overlap_penalty)

        diversification_bonus = 0.0
        if (
            not is_existing_holding
            and (themes or sector)
            and not overlapping_themes
            and sector_overlap_penalty == 0
        ):
            diversification_bonus = 1.0

        existing_position_relevance_bonus = 2.0 if is_existing_holding else 0.0

        budget_fit = "unknown"
        budget_fit_score = 0.0
        if is_existing_holding:
            budget_fit = "held"
        elif available_cash is not None:
            cash = float(available_cash or 0.0)
            if cash <= 0:
                budget_fit = "poor"
                budget_fit_score = -2.0
            elif price is None or float(price) <= 0:
                budget_fit = "unknown"
            elif float(price) <= cash * 0.25:
                budget_fit = "good"
                budget_fit_score = 1.0
            elif float(price) <= cash:
                budget_fit = "tight"
            else:
                budget_fit = "poor"
                budget_fit_score = -1.0

        portfolio_priority = round(
            existing_position_relevance_bonus
            + diversification_bonus
            + budget_fit_score
            - overlap_penalty,
            2,
        )

        context_bits: list[str] = []
        if is_core_holding:
            context_bits.append("existing core holding")
        elif is_existing_holding:
            context_bits.append("existing holding")
        if overlapping_themes:
            context_bits.append(f"overlaps held themes: {', '.join(overlapping_themes)}")
        if sector_overlap_penalty:
            context_bits.append(f"overlaps held sector: {sector}")
        if diversification_bonus > 0:
            context_bits.append("adds diversification vs current holdings")
        context_bits.append(f"budget_fit={budget_fit}")

        rank_reason_bits: list[str] = []
        if existing_position_relevance_bonus:
            rank_reason_bits.append(f"existing_position+{existing_position_relevance_bonus:.0f}")
        if diversification_bonus:
            rank_reason_bits.append(f"diversification+{diversification_bonus:.0f}")
        if overlap_penalty:
            rank_reason_bits.append(f"overlap-{overlap_penalty:.0f}")
        if budget_fit_score:
            rank_reason_bits.append(f"budget{budget_fit_score:+.0f}")
        if not rank_reason_bits:
            rank_reason_bits.append("portfolio-neutral")

        row["portfolio_priority"] = portfolio_priority
        row["overlap_penalty"] = overlap_penalty
        row["diversification_bonus"] = diversification_bonus
        row["existing_position_relevance_bonus"] = existing_position_relevance_bonus
        row["budget_fit"] = budget_fit
        row["budget_fit_score"] = budget_fit_score
        row["exposure_context"] = "; ".join(context_bits) or "none"
        row["final_operator_rank_reason"] = ", ".join(rank_reason_bits)

    scan_result["results"] = results
    scan_result["portfolio_context_summary"] = {
        "holding_count": len(holding_symbols),
        "held_theme_counts": held_theme_counts,
        "held_sector_counts": held_sector_counts,
        "available_cash": available_cash,
    }
    return scan_result


def _operator_order_key(row: WatchlistRow) -> tuple:
    """
    Explainable post-routing ordering key for operator-facing outputs.

    Ordering is quality-first within the current operator relevance bucket:
    1. current relevance / actionability (`notification_status`)
    2. routed alert priority (`alert_priority`)
    3. promotion quality tier (`alert_quality_tier`)
    4. portfolio usefulness (`portfolio_priority`)
    5. confirmation strength (`confirmation_count`, `evidence_breadth`)
    6. confidence-adjusted conviction (`trusted_signal_score`)
    7. notable move magnitude, then raw `signal_score`
    """
    notification_rank = {
        "alerted": 2,
        "cooldown_suppressed": 1,
        "not_alerting": 0,
    }.get(str(row.get("notification_status") or "not_alerting"), 0)
    priority_rank = {
        "high": 3,
        "normal": 2,
        "watch": 1,
    }.get(str(row.get("alert_priority") or ""), 0)
    quality_rank = {
        "broad": 3,
        "confirmed": 2,
        "thin": 1,
        "none": 0,
    }.get(str(row.get("alert_quality_tier") or "none"), 0)
    tier_rank = {
        "high": 3,
        "medium": 2,
        "low": 1,
    }.get(str(row.get("alert_tier") or ""), 0)
    priority_score = float(row.get("priority_score") or 0.0)
    portfolio_priority = float(row.get("portfolio_priority") or 0.0)
    confirmation_count = int(row.get("confirmation_count") or 0)
    evidence_breadth = int(row.get("evidence_breadth") or 0)
    trusted_signal_score = float(row.get("trusted_signal_score") or 0.0)
    move_magnitude = abs(float(row.get("price_change_pct") or 0.0))
    signal_score = float(row.get("signal_score") or 0.0)
    ticker = str(row.get("ticker") or "")

    return (
        notification_rank,
        priority_rank,
        quality_rank,
        tier_rank,
        priority_score,
        portfolio_priority,
        confirmation_count,
        evidence_breadth,
        trusted_signal_score,
        move_magnitude,
        signal_score,
        ticker,
    )


def _apply_output_ordering(scan_result: WatchlistScanResult) -> WatchlistScanResult:
    """
    Apply one shared operator-facing order across results and alerts.

    This keeps JSON, CSV, and markdown outputs aligned with the same
    confirmation-aware ranking model after cooldown has already been applied.
    """
    results = list(scan_result.get("results") or [])
    alerts = list(scan_result.get("alerts") or [])

    results.sort(key=_operator_order_key, reverse=True)
    alerts.sort(key=_operator_order_key, reverse=True)

    for idx, row in enumerate(results, start=1):
        row["operator_rank"] = idx

    result_rank_by_ticker = {
        str(row.get("ticker") or "").upper(): int(row.get("operator_rank") or 0)
        for row in results
    }
    for row in alerts:
        row["operator_rank"] = result_rank_by_ticker.get(str(row.get("ticker") or "").upper(), 0)

    scan_result["results"] = results
    scan_result["alerts"] = alerts
    return scan_result
