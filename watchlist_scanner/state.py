from __future__ import annotations

from pathlib import Path
from typing import Any

from state_store import PortfolioStateStore


class WatchlistStateStore:
    """
    Thin watchlist-specific adapter over the shared PortfolioStateStore.

    This keeps the existing database schema and persistence semantics intact
    while giving watchlist code a narrower, watchlist-oriented API surface.
    """

    def __init__(self, db_path: str | Path = "data/portfolio.db") -> None:
        self._store = PortfolioStateStore(Path(db_path))

    def should_suppress_alert(
        self,
        fingerprint: str,
        cooldown_days: int,
        severity: str = "",
        state_hash: str = "",
    ) -> bool:
        return self._store.should_suppress_alert(
            fingerprint,
            cooldown_days=cooldown_days,
            severity=severity,
            state_hash=state_hash,
        )

    def touch_alert_state(
        self,
        fingerprint: str,
        severity: str = "",
        state_hash: str = "",
        alert_tier: str = "",
        reason_code: str = "",
        last_signal_score: float | None = None,
        last_confidence_score: float | None = None,
        last_action_taken: str = "",
    ) -> None:
        self._store.upsert_alert_event(
            fingerprint,
            severity=severity,
            state_hash=state_hash,
            alert_tier=alert_tier,
            reason_code=reason_code,
            last_signal_score=last_signal_score,
            last_confidence_score=last_confidence_score,
            last_action_taken=last_action_taken,
        )

    def get_alert_state(self, fingerprint: str) -> dict[str, Any] | None:
        return self._store.get_alert_event(fingerprint)

    def get_alert_lifecycle(
        self,
        fingerprint: str,
        state_hash: str = "",
    ) -> dict[str, Any] | None:
        return self._store.get_watchlist_alert_outcome(
            fingerprint,
            state_hash=state_hash,
        )

    def get_or_create_alert_lifecycle(
        self,
        fingerprint: str,
        state_hash: str,
        alert_data: dict[str, Any],
        evaluation_window: str = "1d,3d,5d,10d",
    ) -> dict[str, Any]:
        return self._store.record_watchlist_alert_surface(
            fingerprint,
            state_hash,
            alert_data,
            evaluation_window=evaluation_window,
        )

    def record_alert_surface(
        self,
        fingerprint: str,
        state_hash: str,
        alert_data: dict[str, Any],
        evaluation_window: str = "1d,3d,5d,10d",
    ) -> dict[str, Any]:
        return self.get_or_create_alert_lifecycle(
            fingerprint,
            state_hash,
            alert_data,
            evaluation_window=evaluation_window,
        )

    def mark_alert_notified(self, fingerprint: str) -> None:
        self._store.record_alert_emailed(fingerprint)

    def list_alert_lifecycles(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._store.get_watchlist_alert_outcomes(limit=limit)

    def list_pending_alert_lifecycles(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._store.get_pending_watchlist_alert_outcomes(limit=limit)

    def resolve_alert_lifecycle(
        self,
        outcome_id: int,
        *,
        evaluation_price: float,
        return_pct: float,
        evaluated_at: str | None = None,
        outcome_label: str,
        outcome_status: str = "resolved_1d",
    ) -> dict[str, Any] | None:
        return self._store.resolve_watchlist_alert_outcome(
            outcome_id,
            evaluation_price=evaluation_price,
            return_pct=return_pct,
            evaluated_at=evaluated_at,
            outcome_label=outcome_label,
            outcome_status=outcome_status,
        )

    def record_signal_feedback(
        self,
        *,
        signal_key: str,
        ticker: str,
        signal_time: str,
        watchlist_source: str = "static",
        signal_score: float | None = None,
        confidence_score: float | None = None,
        effective_score: float | None = None,
        conviction_score: float | None = None,
        conviction_band: str | None = None,
        normalized_allocation: float | None = None,
        price_at_signal: float | None = None,
        prediction_intent: str = "up",
        data_mode: str = "live",
        degraded_mode: bool = False,
        regime_label: str = "neutral",
        regime_confidence: float | None = None,
        regime_data_quality: str = "limited",
        theme_alignment_score: float | None = None,
        theme_top_name: str | None = None,
        theme_type: str | None = None,
        portfolio_fit_score: float | None = None,
        portfolio_fit_label: str | None = None,
        final_rank_score: float | None = None,
        augmented_signal_score: float | None = None,
    ) -> dict[str, Any] | None:
        return self._store.record_watchlist_signal_feedback(
            signal_key=signal_key,
            ticker=ticker,
            signal_time=signal_time,
            watchlist_source=watchlist_source,
            signal_score=signal_score,
            confidence_score=confidence_score,
            effective_score=effective_score,
            conviction_score=conviction_score,
            conviction_band=conviction_band,
            normalized_allocation=normalized_allocation,
            price_at_signal=price_at_signal,
            prediction_intent=prediction_intent,
            data_mode=data_mode,
            degraded_mode=degraded_mode,
            regime_label=regime_label,
            regime_confidence=regime_confidence,
            regime_data_quality=regime_data_quality,
            theme_alignment_score=theme_alignment_score,
            theme_top_name=theme_top_name,
            theme_type=theme_type,
            portfolio_fit_score=portfolio_fit_score,
            portfolio_fit_label=portfolio_fit_label,
            final_rank_score=final_rank_score,
            augmented_signal_score=augmented_signal_score,
        )

    def list_signal_feedback(
        self,
        *,
        ticker: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return self._store.get_watchlist_signal_feedback(ticker=ticker, limit=limit)

    def list_pending_signal_feedback(
        self,
        *,
        window_days: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return self._store.get_pending_watchlist_signal_feedback(window_days=window_days, limit=limit)

    def resolve_signal_feedback(
        self,
        feedback_id: int,
        *,
        window_days: int,
        outcome_price: float,
        return_pct: float,
        outcome_success: bool,
        direction_correct: bool,
        evaluated_at: str | None = None,
    ) -> dict[str, Any] | None:
        return self._store.resolve_watchlist_signal_feedback(
            feedback_id,
            window_days=window_days,
            outcome_price=outcome_price,
            return_pct=return_pct,
            outcome_success=outcome_success,
            direction_correct=direction_correct,
            evaluated_at=evaluated_at,
        )
