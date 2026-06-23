from __future__ import annotations

import csv
import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from watchlist_scanner.cache_manager import CacheManager
from watchlist_scanner.outcome_evaluator import (
    _load_next_available_close,
    load_next_available_close,
)
from watchlist_scanner.state import WatchlistStateStore

logger = logging.getLogger("watchlist_scanner.performance_feedback")

DEFAULT_WINDOWS = (1, 3, 7)
PRIMARY_WINDOW_DAYS = 3


def _safe_iso(raw: Any) -> str | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).isoformat()
    except (TypeError, ValueError):
        return None


def _prediction_intent_for_row(row: dict[str, Any]) -> str:
    # Current watchlist signals are opportunity-seeking and therefore treated
    # as "up" intent. This is stored explicitly so the system can support
    # future bi-directional signals without changing the schema.
    return "up"


def _signal_key(row: dict[str, Any], signal_time: str) -> str:
    ticker = str(row.get("ticker") or "").upper()
    source = str(row.get("watchlist_source") or "static")
    return f"{ticker}|{source}|{signal_time}"


def _window_columns(window_days: int) -> tuple[str, str]:
    suffix = f"{window_days}d"
    return (f"outcome_return_{suffix}", f"outcome_success_{suffix}")


def record_scan_signals(
    scan_result: dict[str, Any],
    *,
    db_path: str | Path = "data/portfolio.db",
) -> dict[str, int]:
    store = WatchlistStateStore(db_path)
    signal_time = _safe_iso(scan_result.get("generated_at")) or datetime.now().isoformat()
    regime = scan_result.get("market_regime") if isinstance(scan_result.get("market_regime"), dict) else {}
    # Defense-in-depth: recording signals while the regime is unset means every
    # row falls back to the constant ("neutral", 0.0, "limited") triple, the
    # degeneracy that collapsed signal_outcomes.csv. Surface it loudly so a
    # producer-ordering re-regression cannot hide behind the silent fallback.
    if not regime and (scan_result.get("results") or []):
        logger.warning(
            "record_scan_signals: market_regime is empty while recording %d "
            "signal(s); regime tags will fall back to ('neutral', 0.0, "
            "'limited'). Ensure detect_market_regime runs BEFORE "
            "run_signal_feedback_cycle.",
            len(scan_result.get("results") or []),
        )
    tracked = 0
    skipped = 0

    for row in scan_result.get("results", []) or []:
        ticker = str(row.get("ticker") or "").upper()
        price = row.get("price")
        if not ticker or price is None:
            skipped += 1
            continue
        signal_score = float(row.get("signal_score") or 0.0)
        confidence_score = float(row.get("confidence_score") or 0.0)
        effective_score = float(row.get("effective_score") or 0.0)
        created = store.record_signal_feedback(
            signal_key=_signal_key(row, signal_time),
            ticker=ticker,
            signal_time=signal_time,
            watchlist_source=str(row.get("watchlist_source") or "static"),
            signal_score=signal_score,
            confidence_score=confidence_score,
            effective_score=effective_score,
            conviction_score=float(row.get("conviction_score") or 0.0) if row.get("conviction_score") is not None else None,
            conviction_band=str(row.get("conviction_band") or "") or None,
            normalized_allocation=float(row.get("normalized_allocation") or 0.0) if row.get("normalized_allocation") is not None else None,
            price_at_signal=float(price),
            prediction_intent=_prediction_intent_for_row(row),
            data_mode=str(row.get("data_mode") or scan_result.get("data_mode") or "live"),
            degraded_mode=bool(row.get("data_mode") == "fallback" or scan_result.get("degraded_mode")),
            regime_label=str(row.get("regime_label") or regime.get("regime_label") or "neutral"),
            regime_confidence=float(row.get("regime_confidence") or regime.get("regime_confidence") or 0.0),
            regime_data_quality=str(row.get("regime_data_quality") or regime.get("regime_data_quality") or "limited"),
            theme_alignment_score=float(row["theme_alignment_score"]) if row.get("theme_alignment_score") is not None else None,
            theme_top_name=str(row["theme_top_name"]) if row.get("theme_top_name") else None,
            theme_type=str(row["theme_type"]) if row.get("theme_type") else None,
            portfolio_fit_score=float(row["portfolio_fit_score"]) if row.get("portfolio_fit_score") is not None else None,
            portfolio_fit_label=str(row["portfolio_fit_label"]) if row.get("portfolio_fit_label") else None,
            final_rank_score=float(row["final_rank_score"]) if row.get("final_rank_score") is not None else None,
            augmented_signal_score=float(row["augmented_signal_score"]) if row.get("augmented_signal_score") is not None else None,
        )
        if created is None:
            skipped += 1
            continue
        tracked += 1

    return {"tracked": tracked, "skipped": skipped}


def _load_fmp_budget() -> int | None:
    """Read fmp_daily_calls_budget from config.json for the FMP price-fetch fallback.

    Returns the configured int verbatim — INCLUDING 0, which FMPClient treats as
    "no daily cap" (FMPClient.would_exceed: ``budget <= 0`` disables the cap).
    Returns None only when the key is absent or on any error, so the caller falls
    back to FMPClient's built-in default. Coalescing an explicit 0 to None here
    silently re-imposed the legacy 230-call cap on outcome resolution.
    """
    try:
        cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
        limits = (cfg.get("api_limits") or {}) if isinstance(cfg, dict) else {}
        if "fmp_daily_calls_budget" not in limits:
            return None
        return int(limits["fmp_daily_calls_budget"])
    except Exception:
        return None


def evaluate_pending_signal_feedback(
    *,
    db_path: str | Path = "data/portfolio.db",
    cache_dir: str | Path = "data/watchlist_cache",
    as_of: datetime | None = None,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
    limit: int = 5000,
    fmp_client: Any = None,
) -> dict[str, Any]:
    now = as_of or datetime.now()
    as_of_date = now.date()
    store = WatchlistStateStore(db_path)
    cache = CacheManager(cache_dir=cache_dir)
    summary: dict[str, Any] = {"as_of": now.isoformat(), "by_window": {}}

    # Best-effort FMP client for the price-cache fallback. The legacy
    # resolver relied on AV TIME_SERIES_DAILY payloads which the FMP-primary
    # pipeline doesn't populate; without an FMP fallback, every signal whose
    # ticker lacks an AV cache entry is bucketed "missing_price". Caller can
    # still pass fmp_client=None to keep the strict AV-only behavior.
    if fmp_client is None:
        try:
            from portfolio_automation.data_budget.factory import governed_client
            fmp_client = governed_client("daily")
        except Exception as exc:
            logger.debug("evaluate_pending_signal_feedback: no FMP fallback (%s)", exc)
            fmp_client = None

    # Per-cron in-process cache for FMP historical-prices data. Collect the
    # union of unique tickers across all windows, fetch each ONCE with
    # ttl_days=0, and reuse for every (window, ticker) pair below. Drops FMP
    # consumption from ~60 calls/cron (3 windows × 20 unique tickers) to ~20.
    historical_cache: dict[str, list[dict]] = {}
    if fmp_client is not None:
        unique_tickers: set[str] = set()
        try:
            for window_days in windows:
                _pending = store.list_pending_signal_feedback(window_days=window_days, limit=limit)
                for r in _pending:
                    t = str(r.get("ticker") or "").upper().strip()
                    if t:
                        unique_tickers.add(t)
        except Exception as exc:
            logger.debug("historical prefetch: pending enumeration failed (%s)", exc)
        prefetch_failures = 0
        for ticker in unique_tickers:
            try:
                rows = fmp_client.get_historical_prices(ticker, years=1, ttl_days=0)
                if isinstance(rows, list) and rows:
                    historical_cache[ticker] = rows
            except Exception as exc:
                prefetch_failures += 1
                logger.debug("historical prefetch failed for %s: %s", ticker, exc)
        logger.info(
            "evaluate_pending_signal_feedback: prefetched %d/%d unique tickers "
            "(%d failures); each ticker fetched once and reused across %d windows",
            len(historical_cache), len(unique_tickers), prefetch_failures, len(windows),
        )

    for window_days in windows:
        pending_rows = store.list_pending_signal_feedback(window_days=window_days, limit=limit)
        evaluated = 0
        not_due = 0
        missing_price = 0
        invalid_baseline = 0
        skipped = 0

        for row in pending_rows:
            feedback_id = int(row.get("id") or 0)
            ticker = str(row.get("ticker") or "").upper()
            signal_time_raw = row.get("signal_time")
            baseline_price = row.get("price_at_signal")
            if not feedback_id or not ticker or not signal_time_raw:
                skipped += 1
                continue
            try:
                signal_time = datetime.fromisoformat(str(signal_time_raw))
            except (TypeError, ValueError):
                skipped += 1
                continue

            due_date = signal_time.date().fromordinal(signal_time.date().toordinal() + window_days)
            if as_of_date < due_date:
                not_due += 1
                continue
            if baseline_price is None or float(baseline_price) <= 0:
                invalid_baseline += 1
                continue

            next_close = load_next_available_close(
                cache, ticker, due_date, as_of_date,
                fmp_client=fmp_client,
                historical_cache=historical_cache,
            )
            if next_close is None:
                missing_price += 1
                continue

            eval_date, evaluation_price = next_close
            baseline = float(baseline_price)
            return_pct = round(((evaluation_price - baseline) / baseline) * 100.0, 2)
            intent = str(row.get("prediction_intent") or "up")
            direction_correct = return_pct > 0 if intent == "up" else return_pct < 0
            resolved = store.resolve_signal_feedback(
                feedback_id,
                window_days=window_days,
                outcome_price=evaluation_price,
                return_pct=return_pct,
                outcome_success=direction_correct,
                direction_correct=direction_correct,
                evaluated_at=datetime.combine(eval_date, datetime.min.time()).isoformat(),
            )
            if resolved is not None:
                evaluated += 1

        summary["by_window"][f"{window_days}d"] = {
            "evaluated": evaluated,
            "not_due": not_due,
            "missing_price": missing_price,
            "invalid_baseline": invalid_baseline,
            "skipped": skipped,
        }

    return summary


def _pearson_correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return None
    return round(num / (den_x * den_y), 3)


def _historical_performance_score(avg_return_pct: float, win_rate: float, signal_count: int) -> float:
    avg_component = max(0.0, min(1.0, (avg_return_pct + 5.0) / 10.0))
    sample_factor = min(1.0, signal_count / 5.0)
    score = (0.55 * win_rate) + (0.45 * avg_component)
    score *= 0.5 + (0.5 * sample_factor)
    return round(max(0.0, min(1.0, score)), 3)


def _signal_reliability(score: float, signal_count: int) -> str:
    if signal_count < 3:
        return "unproven"
    if score >= 0.67:
        return "strong"
    if score <= 0.40:
        return "weak"
    return "mixed"


def _success_rate(rows: list[dict[str, Any]], success_col: str) -> float | None:
    if not rows:
        return None
    return round(sum(1 for row in rows if int(row.get(success_col) or 0) == 1) / len(rows), 3)


def _avg_metric(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(row.get(key) or 0.0) for row in rows if row.get(key) is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 3)


_MIN_SAMPLE_SIZE = 10

_THEME_ALIGNMENT_BUCKETS: list[tuple[str, float, float]] = [
    ("none", -0.001, 0.0),
    ("weak", 0.0, 0.3),
    ("moderate", 0.3, 0.7),
    ("strong", 0.7, 1.01),
]

_PORTFOLIO_FIT_BUCKETS: list[tuple[str, float, float]] = [
    ("poor", -0.001, 0.35),
    ("neutral", 0.35, 0.55),
    ("good", 0.55, 0.75),
    ("strong", 0.75, 1.01),
]


def _enrichment_bucket_stats(
    bucket_rows: list[dict[str, Any]],
    *,
    primary_window_days: int,
) -> dict[str, Any]:
    return_col = f"outcome_return_{primary_window_days}d"
    success_col = f"outcome_success_{primary_window_days}d"
    resolved = [r for r in bucket_rows if r.get(return_col) is not None]
    total = len(bucket_rows)
    n = len(resolved)
    if not n:
        return {
            "count": total,
            "resolved": 0,
            "avg_return": None,
            "hit_rate": None,
            "low_sample_warning": total < _MIN_SAMPLE_SIZE,
        }
    avg_return = round(sum(float(r.get(return_col) or 0.0) for r in resolved) / n, 3)
    hit_rate = round(sum(1 for r in resolved if int(r.get(success_col) or 0) == 1) / n, 3)
    return {
        "count": total,
        "resolved": n,
        "avg_return": avg_return,
        "hit_rate": hit_rate,
        "low_sample_warning": n < _MIN_SAMPLE_SIZE,
    }


def build_theme_alignment_performance(
    rows: list[dict[str, Any]],
    *,
    primary_window_days: int = PRIMARY_WINDOW_DAYS,
) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name, _, _ in _THEME_ALIGNMENT_BUCKETS}
    for row in rows:
        raw = row.get("theme_alignment_score")
        score = float(raw) if raw is not None else None
        for name, lo, hi in _THEME_ALIGNMENT_BUCKETS:
            if score is None or score == 0.0:
                if name == "none":
                    buckets[name].append(row)
                    break
            elif lo < score <= hi:
                buckets[name].append(row)
                break
        else:
            buckets["none"].append(row)
    return {
        "generated_at": datetime.now().isoformat(),
        "primary_window_days": primary_window_days,
        "total": len(rows),
        "buckets": {
            name: _enrichment_bucket_stats(group, primary_window_days=primary_window_days)
            for name, group in buckets.items()
        },
    }


def build_portfolio_fit_performance(
    rows: list[dict[str, Any]],
    *,
    primary_window_days: int = PRIMARY_WINDOW_DAYS,
) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name, _, _ in _PORTFOLIO_FIT_BUCKETS}
    for row in rows:
        raw = row.get("portfolio_fit_score")
        label = str(row.get("portfolio_fit_label") or "").lower()
        if raw is not None:
            score = float(raw)
            for name, lo, hi in _PORTFOLIO_FIT_BUCKETS:
                if lo < score <= hi or (name == "poor" and score <= 0.35):
                    buckets[name].append(row)
                    break
        elif label in buckets:
            buckets[label].append(row)
        else:
            buckets["neutral"].append(row)
    return {
        "generated_at": datetime.now().isoformat(),
        "primary_window_days": primary_window_days,
        "total": len(rows),
        "buckets": {
            name: _enrichment_bucket_stats(group, primary_window_days=primary_window_days)
            for name, group in buckets.items()
        },
    }


def build_final_rank_performance(
    rows: list[dict[str, Any]],
    *,
    primary_window_days: int = PRIMARY_WINDOW_DAYS,
) -> dict[str, Any]:
    return_col = f"outcome_return_{primary_window_days}d"
    direction_col = f"direction_correct_{primary_window_days}d"
    scored = [r for r in rows if r.get("final_rank_score") is not None]
    if not scored:
        return {
            "generated_at": datetime.now().isoformat(),
            "primary_window_days": primary_window_days,
            "total": len(rows),
            "scored": 0,
            "quartiles": {},
        }
    sorted_rows = sorted(scored, key=lambda r: float(r.get("final_rank_score") or 0.0), reverse=True)
    n = len(sorted_rows)
    q_size = max(1, n // 4)
    quartile_groups = {
        "Q1": sorted_rows[:q_size],
        "Q2": sorted_rows[q_size: 2 * q_size],
        "Q3": sorted_rows[2 * q_size: 3 * q_size],
        "Q4": sorted_rows[3 * q_size:],
    }
    quartile_stats: dict[str, Any] = {}
    for q_name, group in quartile_groups.items():
        if not group:
            quartile_stats[q_name] = {"count": 0, "resolved": 0, "avg_final_rank_score": None, "avg_return": None, "direction_correct_rate": None, "low_sample_warning": True}
            continue
        resolved = [r for r in group if r.get(return_col) is not None]
        avg_rank = round(sum(float(r.get("final_rank_score") or 0.0) for r in group) / len(group), 4)
        avg_return = round(sum(float(r.get(return_col) or 0.0) for r in resolved) / len(resolved), 3) if resolved else None
        dir_rate = round(sum(1 for r in resolved if int(r.get(direction_col) or 0) == 1) / len(resolved), 3) if resolved else None
        quartile_stats[q_name] = {
            "count": len(group),
            "resolved": len(resolved),
            "avg_final_rank_score": avg_rank,
            "avg_return": avg_return,
            "direction_correct_rate": dir_rate,
            "low_sample_warning": len(resolved) < _MIN_SAMPLE_SIZE,
        }
    return {
        "generated_at": datetime.now().isoformat(),
        "primary_window_days": primary_window_days,
        "total": len(rows),
        "scored": len(scored),
        "quartiles": quartile_stats,
    }


def build_theme_type_performance(
    rows: list[dict[str, Any]],
    *,
    primary_window_days: int = PRIMARY_WINDOW_DAYS,
) -> dict[str, Any]:
    by_type: dict[str, list[dict[str, Any]]] = {"classified": [], "emerging": [], "none": []}
    for row in rows:
        t = str(row.get("theme_type") or "").lower()
        if t in by_type:
            by_type[t].append(row)
        else:
            by_type["none"].append(row)
    return {
        "generated_at": datetime.now().isoformat(),
        "primary_window_days": primary_window_days,
        "total": len(rows),
        "by_type": {
            t: _enrichment_bucket_stats(group, primary_window_days=primary_window_days)
            for t, group in by_type.items()
        },
    }


def build_regime_performance_summary(
    rows: list[dict[str, Any]],
    *,
    primary_window_days: int = PRIMARY_WINDOW_DAYS,
) -> dict[str, Any]:
    primary_return_col, primary_success_col = _window_columns(primary_window_days)
    resolved_rows = [row for row in rows if row.get(primary_return_col) is not None]
    by_regime: dict[str, Any] = {}

    regime_groups: dict[str, list[dict[str, Any]]] = {}
    for row in resolved_rows:
        regime_groups.setdefault(str(row.get("regime_label") or "neutral"), []).append(row)

    for regime_label, group in sorted(regime_groups.items()):
        conviction_groups: dict[str, list[dict[str, Any]]] = {}
        for row in group:
            conviction_groups.setdefault(str(row.get("conviction_band") or "unknown"), []).append(row)

        conviction_summary: dict[str, Any] = {}
        for band, band_rows in sorted(conviction_groups.items()):
            conviction_summary[band] = {
                "signal_count": len(band_rows),
                "win_rate": _success_rate(band_rows, primary_success_col),
                "avg_return_pct": _avg_metric(band_rows, primary_return_col),
                "avg_conviction_score": _avg_metric(band_rows, "conviction_score"),
                "avg_holding_outcome_pct": round(
                    sum(
                        float(row.get(primary_return_col) or 0.0) * float(row.get("normalized_allocation") or 0.0)
                        for row in band_rows
                    ) / len(band_rows),
                    4,
                ) if band_rows else None,
            }

        best_band = None
        worst_band = None
        sortable_bands = [
            (band, stats) for band, stats in conviction_summary.items()
            if stats.get("avg_return_pct") is not None
        ]
        if sortable_bands:
            best_band = max(sortable_bands, key=lambda item: float(item[1].get("avg_return_pct") or 0.0))[0]
            worst_band = min(sortable_bands, key=lambda item: float(item[1].get("avg_return_pct") or 0.0))[0]

        degraded_rows = [row for row in group if bool(row.get("degraded_mode"))]
        normal_rows = [row for row in group if not bool(row.get("degraded_mode"))]
        regime_confidences = [float(row.get("regime_confidence") or 0.0) for row in group if row.get("regime_confidence") is not None]

        by_regime[regime_label] = {
            "total_signals": len(group),
            "win_rate": _success_rate(group, primary_success_col),
            "avg_return_pct": _avg_metric(group, primary_return_col),
            "avg_signal_score": _avg_metric(group, "signal_score"),
            "avg_conviction_score": _avg_metric(group, "conviction_score"),
            "avg_holding_outcome_pct": round(
                sum(
                    float(row.get(primary_return_col) or 0.0) * float(row.get("normalized_allocation") or 0.0)
                    for row in group
                ) / len(group),
                4,
            ) if group else None,
            "avg_regime_confidence": round(sum(regime_confidences) / len(regime_confidences), 3) if regime_confidences else None,
            "conviction_bands": conviction_summary,
            "best_conviction_band": best_band,
            "worst_conviction_band": worst_band,
            "degraded_mode_success_rate": _success_rate(degraded_rows, primary_success_col),
            "normal_mode_success_rate": _success_rate(normal_rows, primary_success_col),
            "degraded_data_impact_note": (
                "degraded data present in this regime sample"
                if degraded_rows else "no degraded data in this regime sample"
            ),
        }

    confidence_buckets: dict[str, list[dict[str, Any]]] = {"high": [], "medium": [], "low": []}
    for row in resolved_rows:
        confidence = float(row.get("confidence_score") or 0.0)
        if confidence >= 0.80:
            confidence_buckets["high"].append(row)
        elif confidence >= 0.65:
            confidence_buckets["medium"].append(row)
        else:
            confidence_buckets["low"].append(row)

    return {
        "generated_at": datetime.now().isoformat(),
        "primary_window_days": primary_window_days,
        "resolved_signals": len(resolved_rows),
        "by_regime": by_regime,
        "observability": {
            "regime_vs_success_correlation": {
                regime: metrics.get("win_rate")
                for regime, metrics in by_regime.items()
            },
            "degraded_mode_vs_regime_reliability": {
                regime: {
                    "avg_regime_confidence": metrics.get("avg_regime_confidence"),
                    "degraded_mode_success_rate": metrics.get("degraded_mode_success_rate"),
                }
                for regime, metrics in by_regime.items()
            },
            "confidence_vs_outcome_within_regime": {
                regime: {
                    bucket: _success_rate(bucket_rows, primary_success_col)
                    for bucket, bucket_rows in {
                        bucket: [
                            row for row in rows_for_regime
                            if (
                                (bucket == "high" and float(row.get("confidence_score") or 0.0) >= 0.80)
                                or (bucket == "medium" and 0.65 <= float(row.get("confidence_score") or 0.0) < 0.80)
                                or (bucket == "low" and float(row.get("confidence_score") or 0.0) < 0.65)
                            )
                        ]
                        for bucket in ("high", "medium", "low")
                        for rows_for_regime in [regime_groups.get(regime, [])]
                    }.items()
                }
                for regime in by_regime.keys()
            },
        },
    }


def _render_regime_performance_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Regime Performance",
        "",
        f"Generated: {summary.get('generated_at', '')}  ",
        f"Resolved signals: **{int(summary.get('resolved_signals') or 0)}**  ",
        "",
    ]
    by_regime = dict(summary.get("by_regime") or {})
    if not by_regime:
        lines.append("No resolved regime-tagged outcomes yet.")
        return "\n".join(lines)

    for regime, metrics in by_regime.items():
        lines += [
            f"## {regime}",
            "",
            f"- Total signals: **{int(metrics.get('total_signals') or 0)}**",
            f"- Win rate: **{float(metrics.get('win_rate') or 0.0):.1%}**" if metrics.get("win_rate") is not None else "- Win rate: n/a",
            f"- Avg return: **{float(metrics.get('avg_return_pct') or 0.0):+.2f}%**" if metrics.get("avg_return_pct") is not None else "- Avg return: n/a",
            f"- Avg signal score: **{float(metrics.get('avg_signal_score') or 0.0):.2f}**" if metrics.get("avg_signal_score") is not None else "- Avg signal score: n/a",
            f"- Avg conviction score: **{float(metrics.get('avg_conviction_score') or 0.0):.2f}**" if metrics.get("avg_conviction_score") is not None else "- Avg conviction score: n/a",
            f"- Avg holding outcome: **{float(metrics.get('avg_holding_outcome_pct') or 0.0):+.2f}%**" if metrics.get("avg_holding_outcome_pct") is not None else "- Avg holding outcome: n/a",
            f"- Best conviction band: `{metrics.get('best_conviction_band') or 'n/a'}`",
            f"- Worst conviction band: `{metrics.get('worst_conviction_band') or 'n/a'}`",
            f"- Degraded data impact: {metrics.get('degraded_data_impact_note') or 'n/a'}",
            "",
        ]
    return "\n".join(lines)


def build_signal_performance_summary(
    rows: list[dict[str, Any]],
    *,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
    primary_window_days: int = PRIMARY_WINDOW_DAYS,
    feedback_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    feedback_cfg = feedback_config if isinstance(feedback_config, dict) else {}
    primary_return_col, primary_success_col = _window_columns(primary_window_days)
    summary: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(),
        "windows": list(windows),
        "primary_window_days": primary_window_days,
        "tracked_signals": len(rows),
        "resolved_signals": 0,
        "by_window": {},
        "by_ticker": {},
        "historically_strong_tickers": [],
        "low_reliability_tickers": [],
        "global_metrics": {},
        "future_activation": {
            "enabled": bool(feedback_cfg.get("adaptive_feedback_enabled", False)),
            "mode": "observe_only",
            "prepared_hooks": [
                "historical_weighting",
                "dynamic_cooldown",
                "signal_dampening",
            ],
        },
    }

    primary_rows = [row for row in rows if row.get(primary_return_col) is not None]
    summary["resolved_signals"] = len(primary_rows)

    for window_days in windows:
        return_col, success_col = _window_columns(window_days)
        resolved = [row for row in rows if row.get(return_col) is not None]
        avg_return = round(
            sum(float(row.get(return_col) or 0.0) for row in resolved) / len(resolved),
            3,
        ) if resolved else 0.0
        win_rate = round(
            sum(1 for row in resolved if int(row.get(success_col) or 0) == 1) / len(resolved),
            3,
        ) if resolved else 0.0
        summary["by_window"][f"{window_days}d"] = {
            "resolved_count": len(resolved),
            "avg_return_pct": avg_return,
            "win_rate": win_rate,
        }

    ticker_groups: dict[str, list[dict[str, Any]]] = {}
    for row in primary_rows:
        ticker_groups.setdefault(str(row.get("ticker") or "").upper(), []).append(row)

    for ticker, group in ticker_groups.items():
        signal_count = len(group)
        avg_return = round(
            sum(float(row.get(primary_return_col) or 0.0) for row in group) / signal_count,
            3,
        )
        win_rate = round(
            sum(1 for row in group if int(row.get(primary_success_col) or 0) == 1) / signal_count,
            3,
        )
        conf_corr = _pearson_correlation(
            [float(row.get("confidence_score") or 0.0) for row in group],
            [float(int(row.get(primary_success_col) or 0)) for row in group],
        )
        score = _historical_performance_score(avg_return, win_rate, signal_count)
        reliability = _signal_reliability(score, signal_count)
        summary["by_ticker"][ticker] = {
            "avg_return_pct": avg_return,
            "win_rate": win_rate,
            "signal_count": signal_count,
            "confidence_accuracy_correlation": conf_corr,
            "historical_performance_score": score,
            "signal_reliability": reliability,
        }

    sorted_tickers = sorted(
        summary["by_ticker"].items(),
        key=lambda item: (
            float(item[1].get("historical_performance_score") or 0.0),
            float(item[1].get("avg_return_pct") or 0.0),
        ),
        reverse=True,
    )
    summary["historically_strong_tickers"] = [
        {"ticker": ticker, **stats}
        for ticker, stats in sorted_tickers
        if stats.get("signal_reliability") == "strong"
    ][:5]
    summary["low_reliability_tickers"] = [
        {"ticker": ticker, **stats}
        for ticker, stats in sorted(
            summary["by_ticker"].items(),
            key=lambda item: (
                float(item[1].get("historical_performance_score") or 0.0),
                float(item[1].get("avg_return_pct") or 0.0),
            ),
        )
        if stats.get("signal_reliability") == "weak"
    ][:5]

    def _success_rate(filtered: list[dict[str, Any]]) -> float | None:
        if not filtered:
            return None
        return round(sum(1 for row in filtered if int(row.get(primary_success_col) or 0) == 1) / len(filtered), 3)

    high_conf = [row for row in primary_rows if float(row.get("confidence_score") or 0.0) >= 0.80]
    low_conf = [row for row in primary_rows if float(row.get("confidence_score") or 0.0) < 0.65]
    degraded = [row for row in primary_rows if bool(row.get("degraded_mode"))]
    normal = [row for row in primary_rows if not bool(row.get("degraded_mode"))]
    regime_summary = build_regime_performance_summary(
        rows,
        primary_window_days=primary_window_days,
    )
    summary["global_metrics"] = {
        "high_confidence_success_rate": _success_rate(high_conf),
        "low_confidence_success_rate": _success_rate(low_conf),
        "degraded_mode_success_rate": _success_rate(degraded),
        "normal_mode_success_rate": _success_rate(normal),
    }
    summary["regime_performance"] = regime_summary
    summary["theme_alignment_performance"] = build_theme_alignment_performance(
        rows, primary_window_days=primary_window_days
    )
    summary["portfolio_fit_performance"] = build_portfolio_fit_performance(
        rows, primary_window_days=primary_window_days
    )
    summary["final_rank_performance"] = build_final_rank_performance(
        rows, primary_window_days=primary_window_days
    )
    summary["theme_type_performance"] = build_theme_type_performance(
        rows, primary_window_days=primary_window_days
    )
    return summary


def annotate_scan_result_with_performance(
    scan_result: dict[str, Any],
    performance_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    ticker_stats = (
        performance_summary.get("by_ticker", {})
        if isinstance(performance_summary, dict)
        else {}
    )
    for row in scan_result.get("results", []) or []:
        stats = ticker_stats.get(str(row.get("ticker") or "").upper())
        if stats:
            row["historical_performance_score"] = stats.get("historical_performance_score")
            row["signal_reliability"] = stats.get("signal_reliability", "unproven")
        else:
            row["historical_performance_score"] = None
            row["signal_reliability"] = "unproven"
    for row in scan_result.get("alerts", []) or []:
        stats = ticker_stats.get(str(row.get("ticker") or "").upper())
        if stats:
            row["historical_performance_score"] = stats.get("historical_performance_score")
            row["signal_reliability"] = stats.get("signal_reliability", "unproven")
        else:
            row["historical_performance_score"] = None
            row["signal_reliability"] = "unproven"
    return scan_result


def _write_signal_outcomes_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "ticker",
        "signal_time",
        "watchlist_source",
        "signal_score",
        "confidence_score",
        "effective_score",
        "conviction_score",
        "conviction_band",
        "normalized_allocation",
        "price_at_signal",
        "prediction_intent",
        "data_mode",
        "degraded_mode",
        "regime_label",
        "regime_confidence",
        "regime_data_quality",
        "theme_alignment_score",
        "theme_top_name",
        "theme_type",
        "portfolio_fit_score",
        "portfolio_fit_label",
        "final_rank_score",
        "augmented_signal_score",
        "outcome_return_1d",
        "outcome_success_1d",
        "direction_correct_1d",
        "outcome_price_1d",
        "evaluated_at_1d",
        "outcome_return_3d",
        "outcome_success_3d",
        "direction_correct_3d",
        "outcome_price_3d",
        "evaluated_at_3d",
        "outcome_return_7d",
        "outcome_success_7d",
        "direction_correct_7d",
        "outcome_price_7d",
        "evaluated_at_7d",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def generate_signal_performance_reports(
    *,
    db_path: str | Path = "data/portfolio.db",
    output_dir: str | Path = "outputs/performance",
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
    primary_window_days: int = PRIMARY_WINDOW_DAYS,
    feedback_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store = WatchlistStateStore(db_path)
    rows = store.list_signal_feedback(limit=10000)
    summary = build_signal_performance_summary(
        rows,
        windows=windows,
        primary_window_days=primary_window_days,
        feedback_config=feedback_config,
    )
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "signal_outcomes.csv"
    json_path = out_dir / "performance_summary.json"
    _write_signal_outcomes_csv(csv_path, rows)
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    logger.info("Signal performance reports written: %s, %s", csv_path, json_path)
    return {
        "summary": summary,
        "paths": {
            "csv_path": str(csv_path),
            "json_path": str(json_path),
        },
    }


def generate_regime_performance_reports(
    *,
    db_path: str | Path = "data/portfolio.db",
    output_dir: str | Path = "outputs/regime",
    primary_window_days: int = PRIMARY_WINDOW_DAYS,
) -> dict[str, Any]:
    store = WatchlistStateStore(db_path)
    rows = store.list_signal_feedback(limit=10000)
    summary = build_regime_performance_summary(
        rows,
        primary_window_days=primary_window_days,
    )
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "regime_performance.json"
    md_path = out_dir / "regime_performance.md"
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    md_path.write_text(_render_regime_performance_markdown(summary), encoding="utf-8")
    logger.info("Regime performance reports written: %s, %s", json_path, md_path)
    return {
        "summary": summary,
        "paths": {
            "json_path": str(json_path),
            "markdown_path": str(md_path),
        },
    }


def run_signal_feedback_cycle(
    scan_result: dict[str, Any],
    *,
    db_path: str | Path = "data/portfolio.db",
    cache_dir: str | Path = "data/watchlist_cache",
    output_dir: str | Path = "outputs/performance",
    dry_run: bool = False,
    feedback_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    feedback_cfg = feedback_config if isinstance(feedback_config, dict) else {}
    windows = tuple(int(w) for w in feedback_cfg.get("windows", DEFAULT_WINDOWS))
    primary_window_days = int(feedback_cfg.get("primary_window_days", PRIMARY_WINDOW_DAYS))
    regime_output_dir = Path(output_dir).parent / "regime"

    tracked_summary = {"tracked": 0, "skipped": 0}
    evaluation_summary = {"by_window": {}}
    if not dry_run:
        tracked_summary = record_scan_signals(scan_result, db_path=db_path)
        evaluation_summary = evaluate_pending_signal_feedback(
            db_path=db_path,
            cache_dir=cache_dir,
            windows=windows,
        )
        report = generate_signal_performance_reports(
            db_path=db_path,
            output_dir=output_dir,
            windows=windows,
            primary_window_days=primary_window_days,
            feedback_config=feedback_cfg,
        )
        regime_report = generate_regime_performance_reports(
            db_path=db_path,
            output_dir=regime_output_dir,
            primary_window_days=primary_window_days,
        )
    else:
        store = WatchlistStateStore(db_path)
        rows = store.list_signal_feedback(limit=10000)
        summary = build_signal_performance_summary(
            rows,
            windows=windows,
            primary_window_days=primary_window_days,
            feedback_config=feedback_cfg,
        )
        report = {
            "summary": summary,
            "paths": {
                "csv_path": str(Path(output_dir) / "signal_outcomes.csv"),
                "json_path": str(Path(output_dir) / "performance_summary.json"),
            },
        }
        regime_summary = build_regime_performance_summary(
            rows,
            primary_window_days=primary_window_days,
        )
        regime_report = {
            "summary": regime_summary,
            "paths": {
                "json_path": str(regime_output_dir / "regime_performance.json"),
                "markdown_path": str(regime_output_dir / "regime_performance.md"),
            },
        }

    scan_result["performance_feedback"] = {
        "tracked_signals": tracked_summary.get("tracked", 0),
        "tracked_skipped": tracked_summary.get("skipped", 0),
        "resolved_signals": int(report["summary"].get("resolved_signals", 0) or 0),
        "primary_window_days": primary_window_days,
        "paths": report["paths"],
        "regime_paths": regime_report["paths"],
        "evaluation": evaluation_summary,
        "future_activation": report["summary"].get("future_activation", {}),
    }
    scan_result.setdefault("scan_summary", {})
    scan_result["scan_summary"]["performance_tracked_signals"] = tracked_summary.get("tracked", 0)
    scan_result["scan_summary"]["performance_resolved_signals"] = int(report["summary"].get("resolved_signals", 0) or 0)
    annotate_scan_result_with_performance(scan_result, report["summary"])
    return report
