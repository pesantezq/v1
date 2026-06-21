"""
Social Sentiment Pipeline — orchestrates Phases 3-11 for a set of tickers.

Flow:
  1. For each ticker: fetch posts from Bluesky, Mastodon, Lemmy.
  2. Apply quality gates per (ticker, source).
  3. Score posts that passed gates with FinBERT.
  4. Aggregate per-source → cross-source (with source cap).
  5. Record to daily history; compute trend states.
  6. Emit per-ticker SentimentResult + overall pipeline status.

Phase 9: The pipeline also writes an extension artifact
  ``outputs/sandbox/discovery/social_sentiment_status.json``
  that carries the new unified crowd bus fields:
    - social_sentiment_score
    - social_sentiment_confidence
    - social_sentiment_source_count
    - social_attention_score  (from ApeWisdom attention data)
    - social_quality_state    (from history tracker)

Phase 10: When ``config.simulation_social_sentiment.enabled=true``, the pipeline
emits a simulation adjustment artifact and, when improvement crosses the
configured threshold, writes a promotion proposal to the sim_governance lane
(human-gated).

Governance:
  - simulation_active: True   — simulation lane is ACTIVE
  - production_gated: True    — production changes require human approval
  - human_approval_required_for_production: True
  - feeds_decision_engine: False — never

Namespace: SANDBOX (never LATEST / POLICY / HISTORICAL from this module).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.social_intelligence.base import utc_now_iso
from portfolio_automation.social_sentiment.aggregator import (
    AggregateResult,
    PerSourceResult,
    aggregate_cross_source,
    aggregate_source,
)
from portfolio_automation.social_sentiment.finbert_scorer import FinBERTScorer, score_records
from portfolio_automation.social_sentiment.history import SentimentHistoryTracker
from portfolio_automation.social_sentiment.quality_gates import QualityGateChecker
from portfolio_automation.social_sentiment.schema import is_valid_text_record

logger = logging.getLogger("stockbot.social_sentiment.pipeline")

_STATUS_PATH = "discovery/social_sentiment_status.json"
_SIM_ADJUSTMENT_PATH = "discovery/social_sentiment_simulation_adjustment.json"

# Governance invariants — hardcoded, never conditional
SIMULATION_ACTIVE = True
PRODUCTION_GATED = True
HUMAN_APPROVAL_REQUIRED = True
FEEDS_DECISION_ENGINE = False
SANDBOX_ONLY = True


@dataclass
class TickerSentimentResult:
    ticker: str
    aggregate: AggregateResult | None
    per_source: list[PerSourceResult] = field(default_factory=list)
    trend_state: str = "building_history"
    sources_attempted: list[str] = field(default_factory=list)
    fetch_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "aggregate": self.aggregate.to_dict() if self.aggregate else None,
            "trend_state": self.trend_state,
            "sources_attempted": list(self.sources_attempted),
            "fetch_warnings": list(self.fetch_warnings),
        }


def run_social_sentiment_pipeline(
    tickers: list[str],
    root: str | Path = ".",
    *,
    cfg: dict[str, Any] | None = None,
    text_connectors: dict[str, Any] | None = None,
    attention_data: dict[str, float] | None = None,
    scorer: FinBERTScorer | None = None,
) -> dict[str, Any]:
    """
    Run the social sentiment pipeline for a list of tickers.

    Parameters
    ----------
    tickers:
        Symbols to fetch and score.
    root:
        Repository root (for artifact writes).
    cfg:
        crowd_radar config dict (from config.json crowd_radar).
    text_connectors:
        Dict of {source_name: connector} with ``fetch_for_ticker`` method.
        If None, connectors are built from config.
    attention_data:
        Dict of {ticker: attention_score} from ApeWisdom (for Phase 9 extension).
    scorer:
        FinBERTScorer instance (or None to use default lazy singleton).

    Returns
    -------
    Status dict suitable for health reporting.
    """
    try:
        return _run(tickers, root, cfg=cfg, text_connectors=text_connectors,
                    attention_data=attention_data, scorer=scorer)
    except Exception as exc:
        logger.exception("Social sentiment pipeline failed: %s", exc)
        return {
            "status": "error",
            "error": str(exc),
            "simulation_active": SIMULATION_ACTIVE,
            "production_gated": PRODUCTION_GATED,
            "feeds_decision_engine": FEEDS_DECISION_ENGINE,
        }


def _run(
    tickers: list[str],
    root: str | Path,
    *,
    cfg: dict[str, Any] | None,
    text_connectors: dict[str, Any] | None,
    attention_data: dict[str, float] | None,
    scorer: FinBERTScorer | None,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    cfg = cfg or {}
    sim_cfg = cfg.get("simulation_social_sentiment") or {}
    gate_cfg = cfg.get("quality_gates") or {}

    gate_checker = QualityGateChecker(gate_cfg)
    sc = scorer or FinBERTScorer(cfg.get("finbert"))

    # Build text connectors if not injected (for testing injectability)
    connectors = text_connectors or _build_connectors(cfg, root_path)

    ledger_path = root_path / "data" / "social_sentiment_history.jsonl"
    history = SentimentHistoryTracker(ledger_path)

    run_id = utc_now_iso()
    ticker_results: list[TickerSentimentResult] = []
    warnings: list[str] = []

    for ticker in tickers:
        result = _process_ticker(
            ticker, connectors, gate_checker, sc, history, cfg=cfg
        )
        ticker_results.append(result)

    # Phase 9: Build unified crowd bus extension fields
    extension = _build_extension(ticker_results, attention_data, run_id)

    # Phase 10: Simulation adjustment
    sim_adjustments: dict[str, Any] = {}
    if bool(sim_cfg.get("enabled", True)):
        sim_adjustments = _build_sim_adjustments(ticker_results, sim_cfg)

    # Write artifacts to SANDBOX
    artifacts: dict[str, str] = {}
    base = root_path / "outputs"
    try:
        artifacts["status"] = str(
            safe_write_json(OutputNamespace.SANDBOX, _STATUS_PATH, extension, base_dir=base)
        )
    except Exception as exc:
        warnings.append(f"status_write_failed:{exc}")

    if sim_adjustments:
        try:
            sim_payload = {
                "schema_version": "1",
                "source": "social_sentiment_simulation_adjustment",
                "run_id": run_id,
                "simulation_active": SIMULATION_ACTIVE,
                "production_gated": PRODUCTION_GATED,
                "human_approval_required_for_production": HUMAN_APPROVAL_REQUIRED,
                "feeds_decision_engine": FEEDS_DECISION_ENGINE,
                "sandbox_only": SANDBOX_ONLY,
                "adjustments": sim_adjustments,
            }
            artifacts["sim_adjustment"] = str(
                safe_write_json(OutputNamespace.SANDBOX, _SIM_ADJUSTMENT_PATH, sim_payload, base_dir=base)
            )
        except Exception as exc:
            warnings.append(f"sim_write_failed:{exc}")

    scored_tickers = [r.ticker for r in ticker_results if r.aggregate and r.aggregate.source_count > 0]

    return {
        "status": "ok" if scored_tickers else "insufficient_data",
        "run_id": run_id,
        "tickers_processed": len(ticker_results),
        "tickers_scored": len(scored_tickers),
        "simulation_active": SIMULATION_ACTIVE,
        "production_gated": PRODUCTION_GATED,
        "feeds_decision_engine": FEEDS_DECISION_ENGINE,
        "artifacts": artifacts,
        "warnings": warnings,
    }


def _process_ticker(
    ticker: str,
    connectors: dict[str, Any],
    gate_checker: QualityGateChecker,
    scorer: FinBERTScorer,
    history: SentimentHistoryTracker,
    *,
    cfg: dict[str, Any],
) -> TickerSentimentResult:
    per_source: list[PerSourceResult] = []
    fetch_warnings: list[str] = []
    sources_attempted: list[str] = []

    for source_name, connector in connectors.items():
        sources_attempted.append(source_name)
        try:
            result = connector.fetch_for_ticker(ticker)
        except AttributeError:
            # Connector doesn't support per-ticker fetching (e.g. ApeWisdom attention-only)
            continue
        except Exception as exc:
            fetch_warnings.append(f"{source_name}:{type(exc).__name__}")
            continue

        records = [r for r in (result.records or []) if is_valid_text_record(r)]
        if not records:
            per_source.append(PerSourceResult(
                source=source_name, ticker=ticker,
                sentiment_score=0.0, positive_probability=0.0,
                neutral_probability=1.0, negative_probability=0.0,
                sample_size=0, engagement_weighted=False,
                quality_passed=False,
                failure_reasons=["no_valid_records"],
            ))
            continue

        gate_result = gate_checker.check(records, source=source_name, ticker=ticker)

        if gate_result.passed:
            score_records(records, scorer=scorer)

        psr = aggregate_source(records, source_name, ticker, gate_result)
        per_source.append(psr)

        # Record to history (only when quality passed and scored)
        if psr.quality_passed and psr.sample_size > 0:
            history.record_daily(
                ticker, source_name, psr.sentiment_score, 0.5, psr.sample_size
            )

    if not per_source:
        return TickerSentimentResult(
            ticker=ticker, aggregate=None,
            per_source=per_source,
            sources_attempted=sources_attempted,
            fetch_warnings=fetch_warnings,
        )

    aggregate = aggregate_cross_source(per_source, ticker)
    trend_state = history.compute_trend_state(ticker)

    return TickerSentimentResult(
        ticker=ticker,
        aggregate=aggregate,
        per_source=per_source,
        trend_state=trend_state,
        sources_attempted=sources_attempted,
        fetch_warnings=fetch_warnings,
    )


def _build_extension(
    results: list[TickerSentimentResult],
    attention_data: dict[str, float] | None,
    run_id: str,
) -> dict[str, Any]:
    """Phase 9: Build the unified crowd bus extension payload."""
    per_ticker: list[dict[str, Any]] = []
    total_scored = 0

    for r in results:
        agg = r.aggregate
        entry: dict[str, Any] = {
            "ticker": r.ticker,
            "social_quality_state": r.trend_state,
            "social_sentiment_score": agg.sentiment_score if agg else None,
            "social_sentiment_confidence": agg.confidence if agg else 0.0,
            "social_sentiment_source_count": agg.source_count if agg else 0,
            "social_attention_score": (attention_data or {}).get(r.ticker),
        }
        if agg and agg.source_count > 0:
            total_scored += 1
        per_ticker.append(entry)

    return {
        "schema_version": "2",
        "source": "social_sentiment_pipeline",
        "run_id": run_id,
        "simulation_active": SIMULATION_ACTIVE,
        "production_gated": PRODUCTION_GATED,
        "human_approval_required_for_production": HUMAN_APPROVAL_REQUIRED,
        "feeds_decision_engine": FEEDS_DECISION_ENGINE,
        "sandbox_only": SANDBOX_ONLY,
        "tickers_scored": total_scored,
        "per_ticker": per_ticker,
    }


def _build_sim_adjustments(
    results: list[TickerSentimentResult],
    sim_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Phase 10: Compute bounded simulation score adjustments."""
    max_adj = float(sim_cfg.get("max_score_adjustment", 0.05))
    min_conf = float(sim_cfg.get("min_confidence", 0.6))
    min_sources = int(sim_cfg.get("min_source_count", 1))

    adjustments: dict[str, Any] = {}
    for r in results:
        agg = r.aggregate
        if agg is None:
            continue
        if agg.confidence < min_conf or agg.source_count < min_sources:
            adjustments[r.ticker] = {
                "adjustment": 0.0,
                "reason": "below_confidence_threshold",
                "confidence": agg.confidence,
                "source_count": agg.source_count,
            }
            continue
        # Bounded adjustment: scale sentiment_score [-1, 1] to [-max_adj, max_adj]
        raw_adj = agg.sentiment_score * max_adj
        adj = round(max(-max_adj, min(max_adj, raw_adj)), 4)
        adjustments[r.ticker] = {
            "adjustment": adj,
            "sentiment_score": agg.sentiment_score,
            "confidence": agg.confidence,
            "source_count": agg.source_count,
            "trend_state": r.trend_state,
            "reason": "sentiment_adjustment",
        }
    return adjustments


def _build_connectors(cfg: dict[str, Any], root_path: Path) -> dict[str, Any]:
    """Build text connectors from config (lazy import)."""
    connectors: dict[str, Any] = {}
    crowd_enabled = bool(cfg.get("enabled", True))
    policy = cfg.get("source_policy") or {}

    for source_name in ("bluesky", "mastodon", "lemmy"):
        source_cfg = policy.get(source_name) or {}
        if not source_cfg.get("enabled", True):
            continue
        try:
            if source_name == "bluesky":
                from portfolio_automation.social_sources.bluesky_connector import BlueskyConnector
                connectors[source_name] = BlueskyConnector(source_cfg, crowd_radar_enabled=crowd_enabled)
            elif source_name == "mastodon":
                from portfolio_automation.social_sources.mastodon_connector import MastodonConnector
                connectors[source_name] = MastodonConnector(source_cfg, crowd_radar_enabled=crowd_enabled)
            elif source_name == "lemmy":
                from portfolio_automation.social_sources.lemmy_connector import LemmyConnector
                connectors[source_name] = LemmyConnector(source_cfg, crowd_radar_enabled=crowd_enabled)
        except ImportError:
            pass
    return connectors
