import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.output_writers import (
    _write_alerts_csv,
    _write_signals_json,
    _write_summary_md,
)
from watchlist_scanner.postprocess import (
    _apply_alert_cooldown,
    _apply_output_ordering,
    _apply_portfolio_priority_overlay,
    _apply_signal_meta_layer,
    _make_alert_fingerprint,
    _make_alert_state_hash,
)
from watchlist_scanner.alert_filter import should_emit_alert
from watchlist_scanner.scanner import WatchlistScanner
from state_store import PortfolioStateStore


class _DummyCache:
    calls_today = 0


class _DummyAV:
    _max_calls = 20


def _scanner() -> WatchlistScanner:
    return WatchlistScanner(
        watchlist=[],
        cache=_DummyCache(),
        av_client=_DummyAV(),
    )


def _signals_config() -> dict:
    return {
        "min_signal_score": 0.50,
        "min_confidence_score": 0.50,
        "min_evidence_count": 2,
        "confidence_tiers": {
            "high": 0.80,
            "medium": 0.65,
            "low": 0.50,
        },
        "cooldown": {
            "high": 6,
            "medium": 24,
            "low": 72,
        },
    }


class TestAlertFilterLayer(unittest.TestCase):

    def test_high_confidence_alert_passes(self):
        decision = should_emit_alert(
            {
                "signal_score": 0.72,
                "confidence_score": 0.88,
                "routed_alert_priority": "high",
                "alert_priority": "high",
                "evidence_breadth": 1,
            },
            _signals_config(),
        )
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["tier"], "high")
        self.assertEqual(decision["reason_code"], "allowed_high")

    def test_medium_confidence_alert_fails_without_enough_evidence(self):
        decision = should_emit_alert(
            {
                "signal_score": 0.68,
                "confidence_score": 0.70,
                "routed_alert_priority": "watch",
                "alert_priority": "watch",
                "evidence_breadth": 1,
            },
            _signals_config(),
        )
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["tier"], "medium")
        self.assertEqual(decision["reason_code"], "insufficient_evidence")

    def test_medium_confidence_alert_passes_with_enough_evidence(self):
        decision = should_emit_alert(
            {
                "signal_score": 0.68,
                "confidence_score": 0.70,
                "routed_alert_priority": "watch",
                "alert_priority": "watch",
                "evidence_breadth": 2,
            },
            _signals_config(),
        )
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["tier"], "medium")
        self.assertEqual(decision["reason_code"], "allowed_medium")

    def test_low_confidence_alert_is_suppressed(self):
        decision = should_emit_alert(
            {
                "signal_score": 0.92,
                "confidence_score": 0.55,
                "routed_alert_priority": "watch",
                "alert_priority": "watch",
                "evidence_breadth": 3,
            },
            _signals_config(),
        )
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["tier"], "low")
        self.assertEqual(decision["reason_code"], "low_confidence_suppressed")


class TestAlertDecisionMetadata(unittest.TestCase):

    def test_medium_conf_signal_below_higher_bar_is_suppressed_with_reason(self):
        scanner = _scanner()
        decision = scanner._evaluate_alert_decision({
            "price_change_pct": 0.0,
            "volume_spike": False,
            "signal_score": 0.55,
            "avg_sentiment": 0.0,
            "confidence_score": 0.65,
        })
        self.assertIsNone(decision["priority"])
        self.assertEqual(decision["code"], "medium_conf_suppressed")
        self.assertIn("higher bar", decision["reason"])

    def test_low_conf_observable_without_confirmation_is_suppressed(self):
        scanner = _scanner()
        decision = scanner._evaluate_alert_decision({
            "price_change_pct": 4.2,
            "volume_spike": False,
            "signal_score": 0.20,
            "avg_sentiment": 0.0,
            "confidence_score": 0.40,
            "score_breakdown": {"technical_score": 0.0, "theme_news_score": 0.0},
            "above_sma20": False,
            "above_sma50": False,
        })
        self.assertIsNone(decision["priority"])
        self.assertEqual(decision["code"], "low_conf_observable_unconfirmed")
        self.assertIn("price_move", decision["basis"])

    def test_low_conf_large_observable_move_stays_watch(self):
        scanner = _scanner()
        decision = scanner._evaluate_alert_decision({
            "price_change_pct": 6.8,
            "volume_spike": False,
            "signal_score": 0.20,
            "avg_sentiment": 0.0,
            "confidence_score": 0.40,
            "score_breakdown": {"technical_score": 0.0, "theme_news_score": 0.0},
            "above_sma20": False,
            "above_sma50": False,
        })
        self.assertEqual(decision["priority"], "watch")
        self.assertEqual(decision["code"], "low_conf_observable_large_move")

    def test_high_conf_observable_without_confirmation_is_demoted(self):
        scanner = _scanner()
        decision = scanner._evaluate_alert_decision({
            "price_change_pct": 3.4,
            "volume_spike": False,
            "signal_score": 0.18,
            "avg_sentiment": 0.0,
            "confidence_score": 0.90,
            "score_breakdown": {"technical_score": 0.20, "theme_news_score": 0.0},
            "above_sma20": False,
            "above_sma50": False,
        })
        self.assertEqual(decision["priority"], "watch")
        self.assertEqual(decision["code"], "high_conf_observable_unconfirmed")
        self.assertEqual(decision["confirmation_summary"], "none")

    def test_medium_conf_observable_with_confirmation_stays_normal(self):
        scanner = _scanner()
        decision = scanner._evaluate_alert_decision({
            "price_change_pct": 3.3,
            "volume_spike": False,
            "signal_score": 0.32,
            "avg_sentiment": 0.0,
            "confidence_score": 0.68,
            "score_breakdown": {"technical_score": 0.62, "theme_news_score": 0.0},
            "above_sma20": True,
            "above_sma50": True,
        })
        self.assertEqual(decision["priority"], "normal")
        self.assertEqual(decision["code"], "medium_conf_observable_confirmed")
        self.assertIn("technical strength", decision["confirmation_summary"])

    def test_high_conf_observable_with_trusted_score_only_is_demoted(self):
        scanner = _scanner()
        decision = scanner._evaluate_alert_decision({
            "price_change_pct": 3.4,
            "volume_spike": False,
            "signal_score": 0.62,
            "avg_sentiment": 0.0,
            "confidence_score": 0.90,
            "score_breakdown": {
                "technical_score": 0.20,
                "theme_news_score": 0.0,
                "fundamental_context_score": 0.20,
            },
            "above_sma20": False,
            "above_sma50": False,
        })
        self.assertEqual(decision["priority"], "watch")
        self.assertEqual(decision["code"], "high_conf_observable_unconfirmed")
        self.assertEqual(decision["confirmation_summary"], "trusted score")
        self.assertIn("independent confirmation", decision["reason"])

    def test_medium_conf_observable_with_trusted_score_only_is_suppressed(self):
        scanner = _scanner()
        decision = scanner._evaluate_alert_decision({
            "price_change_pct": 3.2,
            "volume_spike": False,
            "signal_score": 0.60,
            "avg_sentiment": 0.0,
            "confidence_score": 0.68,
            "score_breakdown": {
                "technical_score": 0.0,
                "theme_news_score": 0.0,
                "fundamental_context_score": 0.20,
            },
            "above_sma20": False,
            "above_sma50": False,
        })
        self.assertIsNone(decision["priority"])
        self.assertEqual(decision["code"], "medium_conf_observable_unconfirmed")
        self.assertEqual(decision["confirmation_summary"], "trusted score")
        self.assertIn("no independent confirmation", decision["reason"])

    def test_high_signal_low_confidence_with_thin_evidence_is_suppressed(self):
        scanner = _scanner()
        decision = scanner._evaluate_alert_decision({
            "price_change_pct": 0.0,
            "volume_spike": False,
            "signal_score": 0.90,
            "avg_sentiment": 0.22,
            "news_count": 4,
            "confidence_score": 0.50,
            "data_quality": "fresh",
            "score_breakdown": {
                "technical_score": 0.0,
                "theme_news_score": 0.82,
                "fundamental_context_score": 0.20,
            },
            "above_sma20": False,
            "above_sma50": False,
        })
        self.assertIsNone(decision["priority"])
        self.assertEqual(decision["code"], "low_conf_exceptional_signal_thin")
        self.assertEqual(decision["evidence_breadth"], 1)
        self.assertEqual(decision["alert_quality_tier"], "thin")

    def test_high_confidence_weak_signal_stays_suppressed(self):
        scanner = _scanner()
        decision = scanner._evaluate_alert_decision({
            "price_change_pct": 0.0,
            "volume_spike": False,
            "signal_score": 0.22,
            "avg_sentiment": 0.0,
            "confidence_score": 0.92,
            "score_breakdown": {
                "technical_score": 0.15,
                "theme_news_score": 0.05,
                "fundamental_context_score": 0.35,
            },
        })
        self.assertIsNone(decision["priority"])
        self.assertEqual(decision["code"], "below_threshold")

    def test_theme_only_high_conf_signal_is_demoted_to_watch(self):
        scanner = _scanner()
        decision = scanner._evaluate_alert_decision({
            "price_change_pct": 0.0,
            "volume_spike": False,
            "signal_score": 0.64,
            "avg_sentiment": 0.20,
            "news_count": 3,
            "confidence_score": 0.90,
            "data_quality": "fresh",
            "score_breakdown": {
                "technical_score": 0.0,
                "theme_news_score": 0.74,
                "fundamental_context_score": 0.30,
            },
            "above_sma20": False,
            "above_sma50": False,
        })
        self.assertEqual(decision["priority"], "watch")
        self.assertEqual(decision["code"], "high_conf_standard_signal_thin")
        self.assertEqual(decision["evidence_categories"], ["news_theme"])
        self.assertEqual(decision["evidence_breadth"], 1)

    def test_multi_factor_high_conf_signal_promotes_to_high(self):
        scanner = _scanner()
        decision = scanner._evaluate_alert_decision({
            "price_change_pct": 0.0,
            "volume_spike": False,
            "signal_score": 0.74,
            "avg_sentiment": 0.18,
            "news_count": 4,
            "confidence_score": 0.91,
            "data_quality": "fresh",
            "score_breakdown": {
                "technical_score": 0.62,
                "theme_news_score": 0.48,
                "fundamental_context_score": 0.66,
            },
            "above_sma20": True,
            "above_sma50": True,
        })
        self.assertEqual(decision["priority"], "high")
        self.assertEqual(decision["code"], "high_conf_strong_signal_broad")
        self.assertEqual(decision["evidence_breadth"], 3)
        self.assertEqual(decision["alert_quality_tier"], "broad")

    def test_partial_data_downgrades_signal_quality(self):
        scanner = _scanner()
        decision = scanner._evaluate_alert_decision({
            "price_change_pct": 0.0,
            "volume_spike": False,
            "signal_score": 0.63,
            "avg_sentiment": 0.18,
            "news_count": 3,
            "confidence_score": 0.88,
            "data_quality": "partial",
            "score_breakdown": {
                "technical_score": 0.0,
                "theme_news_score": 0.52,
                "fundamental_context_score": 0.61,
            },
            "above_sma20": False,
            "above_sma50": False,
        })
        self.assertEqual(decision["priority"], "watch")
        self.assertEqual(decision["code"], "high_conf_standard_signal_thin")
        self.assertEqual(decision["evidence_breadth"], 2)
        self.assertEqual(decision["alert_quality_tier"], "thin")


class TestSummaryOutput(unittest.TestCase):

    def test_summary_includes_alert_decision_reason_and_basis(self):
        scan_result = {
            "run_date": "2026-04-13",
            "generated_at": "2026-04-13T12:00:00",
            "calls_used": 1,
            "scan_summary": {
                "scan_status": "ok",
                "symbols_fresh": 1,
                "symbols_cached": 0,
                "symbols_partial": 0,
                "symbols_budget_skipped": 0,
                "alerts_cooldown_suppressed": 0,
            },
            "results": [{
                "ticker": "AMD",
                "signal_score": 0.62,
                "data_quality": "fresh",
                "confidence_score": 0.65,
                "confidence_band": "medium",
                "watchlist_source": "static",
                "price": 100.0,
                "price_change_pct": 0.5,
                "above_sma20": True,
                "above_sma50": True,
                "volume_spike": False,
                "avg_sentiment": 0.10,
                "themes": ["AI"],
                "alert_priority": "watch",
                "alert_basis_summary": "signal_score",
                "alert_decision_reason": "medium confidence signal cleared the higher watch bar",
                "alert_confirmation_summary": "technical strength",
                "confirmation_count": 1,
                "alert_quality_tier": "confirmed",
                "evidence_breadth": 2,
                "evidence_categories": ["technical", "fundamentals"],
                "fundamentals": {"sector": "TECHNOLOGY", "market_cap": 1000000000.0, "pe_ratio": 25.0},
                "technicals": {"price_change_5d": 3.2, "above_sma20": True, "above_sma50": True},
                "news": {"headline_count": 2, "avg_sentiment": 0.10},
                "score_breakdown": {
                    "theme_news_score": 0.5,
                    "technical_score": 0.6,
                    "fundamental_context_score": 0.7,
                },
                "headline_examples": [],
            }],
            "alerts": [{
                "ticker": "AMD",
                "signal_score": 0.62,
                "data_quality": "fresh",
                "confidence_score": 0.65,
                "confidence_band": "medium",
                "watchlist_source": "static",
                "price": 100.0,
                "price_change_pct": 0.5,
                "above_sma20": True,
                "above_sma50": True,
                "volume_spike": False,
                "avg_sentiment": 0.10,
                "themes": ["AI"],
                "alert_priority": "watch",
                "alert_basis_summary": "signal_score",
                "alert_decision_reason": "medium confidence signal cleared the higher watch bar",
                "alert_confirmation_summary": "technical strength",
                "confirmation_count": 1,
                "alert_quality_tier": "confirmed",
                "evidence_breadth": 2,
                "evidence_categories": ["technical", "fundamentals"],
                "fundamentals": {"sector": "TECHNOLOGY", "market_cap": 1000000000.0, "pe_ratio": 25.0},
                "technicals": {"price_change_5d": 3.2, "above_sma20": True, "above_sma50": True},
                "news": {"headline_count": 2, "avg_sentiment": 0.10},
                "score_breakdown": {
                    "theme_news_score": 0.5,
                    "technical_score": 0.6,
                    "fundamental_context_score": 0.7,
                },
                "headline_examples": [],
            }],
        }

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            _write_summary_md(out_dir, scan_result)
            summary = (out_dir / "watchlist_summary.md").read_text(encoding="utf-8")

        self.assertIn("**Alerting:** watch via signal_score", summary)
        self.assertIn("**Confirmation:** technical strength", summary)
        self.assertIn(
            "**Promotion quality:** confirmed | breadth 2 | confirmations 1 | evidence count 2 | categories: technical, fundamentals",
            summary,
        )
        self.assertIn("medium confidence signal cleared the higher watch bar", summary)


class TestOutputOrdering(unittest.TestCase):

    def _scan_result(self):
        return {
            "run_date": "2026-04-13",
            "generated_at": "2026-04-13T12:00:00",
            "calls_used": 1,
            "scan_summary": {
                "scan_status": "ok",
                "symbols_fresh": 4,
                "symbols_cached": 0,
                "symbols_partial": 0,
                "symbols_budget_skipped": 0,
                "alerts_cooldown_suppressed": 1,
            },
            "results": [
                {
                    "ticker": "THIN",
                    "signal_score": 0.72,
                    "trusted_signal_score": 0.67,
                    "priority_score": 0.70,
                    "priority_explanation": "High confidence with evidence count 1",
                    "data_quality": "fresh",
                    "confidence_score": 0.88,
                    "confidence_band": "high",
                    "alert_tier": "high",
                    "notification_status": "alerted",
                    "alert_priority": "watch",
                    "alert_quality_tier": "thin",
                    "confirmation_count": 1,
                    "evidence_breadth": 1,
                    "watchlist_source": "static",
                    "price": 40.0,
                    "price_change_pct": 0.8,
                    "above_sma20": False,
                    "above_sma50": False,
                    "volume_spike": False,
                    "avg_sentiment": 0.20,
                    "themes": ["AI"],
                    "technicals": {},
                    "fundamentals": {"sector": "TECHNOLOGY"},
                    "score_breakdown": {"theme_news_score": 0.7, "technical_score": 0.1, "fundamental_context_score": 0.3},
                },
                {
                    "ticker": "COOL",
                    "signal_score": 0.91,
                    "trusted_signal_score": 0.84,
                    "priority_score": 0.88,
                    "priority_explanation": "High confidence + 3 reinforcing categories",
                    "data_quality": "fresh",
                    "confidence_score": 0.92,
                    "confidence_band": "high",
                    "alert_tier": "high",
                    "notification_status": "cooldown_suppressed",
                    "alert_priority": "high",
                    "alert_quality_tier": "broad",
                    "confirmation_count": 3,
                    "evidence_breadth": 3,
                    "watchlist_source": "static",
                    "price": 110.0,
                    "price_change_pct": 5.1,
                    "above_sma20": True,
                    "above_sma50": True,
                    "volume_spike": True,
                    "avg_sentiment": 0.30,
                    "themes": ["AI", "Semiconductors"],
                    "technicals": {"price_change_5d": 6.4},
                    "fundamentals": {"sector": "TECHNOLOGY"},
                    "score_breakdown": {"theme_news_score": 0.6, "technical_score": 0.8, "fundamental_context_score": 0.7},
                },
                {
                    "ticker": "MOVE",
                    "signal_score": 0.49,
                    "trusted_signal_score": 0.46,
                    "priority_score": 0.58,
                    "priority_explanation": "Medium confidence, evidence threshold met (0)",
                    "data_quality": "fresh",
                    "confidence_score": 0.78,
                    "confidence_band": "high",
                    "alert_tier": "medium",
                    "notification_status": "alerted",
                    "alert_priority": "normal",
                    "alert_quality_tier": "thin",
                    "confirmation_count": 0,
                    "evidence_breadth": 0,
                    "watchlist_source": "static",
                    "price": 25.0,
                    "price_change_pct": 7.2,
                    "above_sma20": False,
                    "above_sma50": False,
                    "volume_spike": False,
                    "avg_sentiment": 0.0,
                    "themes": [],
                    "technicals": {"price_change_5d": 8.0},
                    "fundamentals": {"sector": "INDUSTRIALS"},
                    "score_breakdown": {"theme_news_score": 0.0, "technical_score": 0.2, "fundamental_context_score": 0.3},
                },
                {
                    "ticker": "BROAD",
                    "signal_score": 0.69,
                    "trusted_signal_score": 0.65,
                    "priority_score": 0.81,
                    "priority_explanation": "High confidence + 3 reinforcing categories",
                    "data_quality": "fresh",
                    "confidence_score": 0.87,
                    "confidence_band": "high",
                    "alert_tier": "high",
                    "notification_status": "alerted",
                    "alert_priority": "high",
                    "alert_quality_tier": "broad",
                    "confirmation_count": 3,
                    "evidence_breadth": 3,
                    "watchlist_source": "static",
                    "price": 95.0,
                    "price_change_pct": 2.4,
                    "above_sma20": True,
                    "above_sma50": True,
                    "volume_spike": True,
                    "avg_sentiment": 0.22,
                    "themes": ["AI", "Semiconductors"],
                    "technicals": {"price_change_5d": 4.5},
                    "fundamentals": {"sector": "TECHNOLOGY"},
                    "score_breakdown": {"theme_news_score": 0.5, "technical_score": 0.7, "fundamental_context_score": 0.7},
                },
            ],
            "alerts": [],
        }

    def test_apply_output_ordering_prioritizes_quality_and_actionability(self):
        scan_result = self._scan_result()
        scan_result["alerts"] = [
            scan_result["results"][0],
            scan_result["results"][1],
            scan_result["results"][2],
            scan_result["results"][3],
        ]

        ordered = _apply_output_ordering(scan_result)
        self.assertEqual(
            [r["ticker"] for r in ordered["results"]],
            ["BROAD", "MOVE", "THIN", "COOL"],
        )
        self.assertEqual(
            [a["ticker"] for a in ordered["alerts"]],
            ["BROAD", "MOVE", "THIN", "COOL"],
        )
        self.assertEqual(ordered["results"][0]["operator_rank"], 1)
        self.assertEqual(ordered["results"][-1]["notification_status"], "cooldown_suppressed")

    def test_ranking_sorts_allowed_alerts_by_priority_score_with_quality_context(self):
        scan_result = self._scan_result()
        scan_result["alerts"] = [
            scan_result["results"][0],
            scan_result["results"][2],
            scan_result["results"][3],
        ]
        ordered = _apply_output_ordering(scan_result)
        self.assertEqual([a["ticker"] for a in ordered["alerts"]], ["BROAD", "MOVE", "THIN"])

    def test_output_files_preserve_shared_ordering(self):
        scan_result = self._scan_result()
        scan_result["data_mode"] = "fallback"
        scan_result["degraded_mode"] = True
        scan_result["degraded_reason"] = "cache_only"
        scan_result["alerts"] = [
            scan_result["results"][0],
            scan_result["results"][1],
            scan_result["results"][2],
            scan_result["results"][3],
        ]
        scan_result = _apply_signal_meta_layer(
            scan_result,
            data_health={
                "degraded_mode": True,
                "degraded_reason": "cache_only",
                "data_sources_used": ["cache"],
                "data_mode": "fallback",
                "degraded_confidence_penalty": 0.20,
            },
            signals_config=_signals_config(),
        )
        ordered = _apply_output_ordering(scan_result)

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            _write_signals_json(out_dir, ordered)
            _write_alerts_csv(out_dir, ordered["alerts"])
            _write_summary_md(out_dir, ordered)

            signals = json.loads((out_dir / "watchlist_signals.json").read_text(encoding="utf-8"))
            self.assertEqual(signals["data_mode"], "fallback")
            self.assertTrue(signals["degraded_mode"])
            self.assertEqual([r["ticker"] for r in signals["results"]], ["BROAD", "MOVE", "THIN", "COOL"])
            self.assertIn("effective_score", signals["results"][0])
            self.assertIn("confidence_weight", signals["results"][0])

            with open(out_dir / "watchlist_alerts.csv", newline="", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual([r["ticker"] for r in rows], [r["ticker"] for r in ordered["alerts"]])
            self.assertEqual(rows[0]["operator_rank"], "1")
            self.assertIn("effective_score", rows[0])
            self.assertIn("cooldown_active", rows[0])

            summary = (out_dir / "watchlist_summary.md").read_text(encoding="utf-8")
            for row in ordered["alerts"]:
                self.assertIn(f"### #{row['operator_rank']} {row['ticker']}", summary)
            self.assertLess(summary.index("| 1 | BROAD |"), summary.index("| 2 | MOVE |"))
            self.assertLess(summary.index("| 2 | MOVE |"), summary.index("| 3 | THIN |"))

    def test_alerts_csv_is_truncated_to_header_when_no_alerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            stale_path = out_dir / "watchlist_alerts.csv"
            stale_path.write_text("ticker\nOLD\n", encoding="utf-8")

            _write_alerts_csv(out_dir, [])

            with open(stale_path, newline="", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))

            self.assertEqual(rows, [])
            self.assertGreater(stale_path.stat().st_size, 0)


class TestPortfolioPriorityOverlay(unittest.TestCase):

    def _base_scan_result(self):
        return {
            "run_date": "2026-04-13",
            "generated_at": "2026-04-13T12:00:00",
            "calls_used": 1,
            "scan_summary": {"scan_status": "ok"},
            "results": [
                {
                    "ticker": "QQQ",
                    "signal_score": 0.52,
                    "trusted_signal_score": 0.50,
                    "data_quality": "fresh",
                    "confidence_score": 0.90,
                    "confidence_band": "high",
                    "notification_status": "not_alerting",
                    "alert_priority": None,
                    "alert_quality_tier": "confirmed",
                    "confirmation_count": 1,
                    "evidence_breadth": 2,
                    "watchlist_source": "static",
                    "price": 500.0,
                    "themes": ["AI"],
                    "fundamentals": {"sector": "TECHNOLOGY"},
                },
                {
                    "ticker": "AI2",
                    "signal_score": 0.73,
                    "trusted_signal_score": 0.70,
                    "data_quality": "fresh",
                    "confidence_score": 0.88,
                    "confidence_band": "high",
                    "notification_status": "alerted",
                    "alert_priority": "high",
                    "alert_quality_tier": "confirmed",
                    "confirmation_count": 2,
                    "evidence_breadth": 2,
                    "watchlist_source": "static",
                    "price": 140.0,
                    "themes": ["AI"],
                    "fundamentals": {"sector": "TECHNOLOGY"},
                },
                {
                    "ticker": "DEF",
                    "signal_score": 0.71,
                    "trusted_signal_score": 0.69,
                    "data_quality": "fresh",
                    "confidence_score": 0.87,
                    "confidence_band": "high",
                    "notification_status": "alerted",
                    "alert_priority": "high",
                    "alert_quality_tier": "confirmed",
                    "confirmation_count": 2,
                    "evidence_breadth": 2,
                    "watchlist_source": "static",
                    "price": 90.0,
                    "themes": ["Defense"],
                    "fundamentals": {"sector": "INDUSTRIALS"},
                },
                {
                    "ticker": "CASHX",
                    "signal_score": 0.76,
                    "trusted_signal_score": 0.73,
                    "data_quality": "fresh",
                    "confidence_score": 0.89,
                    "confidence_band": "high",
                    "notification_status": "alerted",
                    "alert_priority": "high",
                    "alert_quality_tier": "confirmed",
                    "confirmation_count": 2,
                    "evidence_breadth": 2,
                    "watchlist_source": "extended_theme",
                    "price": 700.0,
                    "themes": ["Crypto"],
                    "fundamentals": {"sector": "FINANCIAL SERVICES"},
                },
                {
                    "ticker": "NASA",
                    "signal_score": 0.66,
                    "trusted_signal_score": 0.63,
                    "data_quality": "fresh",
                    "confidence_score": 0.86,
                    "confidence_band": "high",
                    "notification_status": "alerted",
                    "alert_priority": "normal",
                    "alert_quality_tier": "confirmed",
                    "confirmation_count": 2,
                    "evidence_breadth": 2,
                    "watchlist_source": "static",
                    "price": 20.0,
                    "themes": ["Space"],
                    "fundamentals": {"sector": "INDUSTRIALS"},
                },
            ],
            "alerts": [],
        }

    def test_theme_concentration_penalty_and_diversification_bonus_reorder_candidates(self):
        scan_result = self._base_scan_result()
        portfolio_context = {
            "holdings": [
                {"symbol": "QQQ", "asset_class": "us_equity"},
            ],
            "cash_available": 500.0,
        }
        scan_result["alerts"] = scan_result["results"][1:]
        enriched = _apply_portfolio_priority_overlay(scan_result, portfolio_context=portfolio_context)
        ordered = _apply_output_ordering(enriched)

        self.assertGreater(
            next(r for r in ordered["results"] if r["ticker"] == "DEF")["portfolio_priority"],
            next(r for r in ordered["results"] if r["ticker"] == "AI2")["portfolio_priority"],
        )
        alert_order = [r["ticker"] for r in ordered["alerts"]]
        self.assertLess(alert_order.index("DEF"), alert_order.index("AI2"))

    def test_budget_unfriendly_candidate_is_deprioritized(self):
        scan_result = self._base_scan_result()
        portfolio_context = {
            "holdings": [{"symbol": "QQQ", "asset_class": "us_equity"}],
            "cash_available": 120.0,
        }
        scan_result["alerts"] = scan_result["results"][1:4]
        ordered = _apply_output_ordering(
            _apply_portfolio_priority_overlay(scan_result, portfolio_context=portfolio_context)
        )
        cashx = next(r for r in ordered["results"] if r["ticker"] == "CASHX")
        deff = next(r for r in ordered["results"] if r["ticker"] == "DEF")
        self.assertEqual(cashx["budget_fit"], "poor")
        self.assertLess(cashx["portfolio_priority"], deff["portfolio_priority"])
        self.assertLess(
            [r["ticker"] for r in ordered["alerts"]].index("CASHX"),
            len(ordered["alerts"]),
        )
        self.assertGreater(
            [r["ticker"] for r in ordered["alerts"]].index("CASHX"),
            [r["ticker"] for r in ordered["alerts"]].index("DEF"),
        )

    def test_existing_holding_relevance_bonus_is_visible(self):
        scan_result = self._base_scan_result()
        portfolio_context = {
            "holdings": [{"symbol": "NASA", "asset_class": "us_equity"}],
            "cash_available": 200.0,
        }
        scan_result["alerts"] = [scan_result["results"][4], scan_result["results"][2]]
        ordered = _apply_output_ordering(
            _apply_portfolio_priority_overlay(scan_result, portfolio_context=portfolio_context)
        )
        nasa = next(r for r in ordered["results"] if r["ticker"] == "NASA")
        self.assertEqual(nasa["budget_fit"], "held")
        self.assertGreater(nasa["portfolio_priority"], 0)
        self.assertIn("existing_position", nasa["final_operator_rank_reason"])

    def test_confirmation_quality_ordering_still_wins_under_portfolio_overlay(self):
        scan_result = self._base_scan_result()
        broad = {
            "ticker": "BROAD",
            "signal_score": 0.68,
            "trusted_signal_score": 0.65,
            "data_quality": "fresh",
            "confidence_score": 0.88,
            "confidence_band": "high",
            "notification_status": "alerted",
            "alert_priority": "high",
            "alert_quality_tier": "broad",
            "confirmation_count": 3,
            "evidence_breadth": 3,
            "watchlist_source": "static",
            "price": 80.0,
            "themes": ["AI"],
            "fundamentals": {"sector": "TECHNOLOGY"},
        }
        diversified = {
            "ticker": "DIVERSE",
            "signal_score": 0.69,
            "trusted_signal_score": 0.66,
            "data_quality": "fresh",
            "confidence_score": 0.88,
            "confidence_band": "high",
            "notification_status": "alerted",
            "alert_priority": "high",
            "alert_quality_tier": "confirmed",
            "confirmation_count": 2,
            "evidence_breadth": 2,
            "watchlist_source": "static",
            "price": 60.0,
            "themes": ["Defense"],
            "fundamentals": {"sector": "INDUSTRIALS"},
        }
        scan_result["results"] = [scan_result["results"][0], broad, diversified]
        scan_result["alerts"] = [broad, diversified]
        portfolio_context = {
            "holdings": [{"symbol": "QQQ", "asset_class": "us_equity"}],
            "cash_available": 500.0,
        }
        ordered = _apply_output_ordering(
            _apply_portfolio_priority_overlay(scan_result, portfolio_context=portfolio_context)
        )
        self.assertEqual([r["ticker"] for r in ordered["alerts"]], ["BROAD", "DIVERSE"])


class TestAlertCooldown(unittest.TestCase):

    def _scan_result(self):
        alert = {
            "ticker": "AMD",
            "signal_score": 0.62,
            "data_quality": "fresh",
            "confidence_score": 0.91,
            "confidence_band": "high",
            "watchlist_source": "static",
            "price": 100.0,
            "price_change_pct": 4.2,
            "above_sma20": True,
            "above_sma50": True,
            "volume_spike": False,
            "avg_sentiment": 0.10,
            "themes": ["AI"],
            "alert_priority": "high",
            "routed_alert_priority": "high",
            "alert_tier": "high",
            "filter_allowed": True,
            "filter_reason": "high-confidence alert allowed immediately",
            "filter_reason_code": "allowed_high",
            "filtered_reason": "",
            "alert_basis_summary": "price_move, signal_score",
            "alert_decision_reason": "high confidence plus observable trigger",
            "alert_quality_tier": "broad",
            "confirmation_count": 3,
            "evidence_count": 3,
            "evidence_breadth": 3,
            "portfolio_priority": 1.0,
            "overlap_penalty": 0.0,
            "diversification_bonus": 1.0,
            "existing_position_relevance_bonus": 0.0,
            "budget_fit": "good",
            "priority_score": 0.82,
            "priority_explanation": "High confidence + 3 reinforcing categories",
            "fundamentals": {"sector": "TECHNOLOGY", "market_cap": 1000000000.0, "pe_ratio": 25.0},
            "technicals": {"price_change_5d": 3.2, "above_sma20": True, "above_sma50": True},
            "news": {"headline_count": 2, "avg_sentiment": 0.10},
            "score_breakdown": {
                "theme_news_score": 0.5,
                "technical_score": 0.6,
                "fundamental_context_score": 0.7,
            },
            "headline_examples": [],
        }
        result_row = dict(alert)
        return {
            "run_date": "2026-04-13",
            "generated_at": "2026-04-13T12:00:00",
            "calls_used": 1,
            "scan_summary": {
                "scan_status": "ok",
                "symbols_fresh": 1,
                "symbols_cached": 0,
                "symbols_partial": 0,
                "symbols_budget_skipped": 0,
            },
            "results": [result_row],
            "alerts": [alert],
        }

    def test_second_identical_alert_is_cooldown_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "portfolio.db"

            first = _apply_alert_cooldown(
                self._scan_result(),
                db_path=db_path,
                cooldown_days=3,
                signals_config=_signals_config(),
            )
            self.assertEqual(len(first["alerts"]), 1)
            self.assertEqual(first["scan_summary"]["alerts_cooldown_suppressed"], 0)
            self.assertEqual(first["results"][0]["notification_status"], "alerted")
            self.assertIsNotNone(first["results"][0].get("alert_event_id"))
            self.assertEqual(first["results"][0].get("outcome_status"), "pending")
            self.assertEqual(first["results"][0].get("cooldown_applied_hours"), 6)

            second = _apply_alert_cooldown(
                self._scan_result(),
                db_path=db_path,
                cooldown_days=3,
                signals_config=_signals_config(),
            )
            self.assertEqual(len(second["alerts"]), 0)
            self.assertEqual(second["scan_summary"]["alerts_cooldown_suppressed"], 1)
            self.assertEqual(second["results"][0]["notification_status"], "cooldown_suppressed")
            self.assertIn("cooldown-suppressed", second["results"][0]["notification_reason"])
            self.assertEqual(
                first["results"][0].get("alert_event_id"),
                second["results"][0].get("alert_event_id"),
            )

            store = PortfolioStateStore(db_path)
            rows = store.get_watchlist_alert_outcomes()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["ticker"], "AMD")
            self.assertEqual(rows[0]["alert_quality_tier"], "broad")
            self.assertEqual(rows[0]["portfolio_priority"], 1.0)

    def test_state_change_breaks_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "portfolio.db"

            first = _apply_alert_cooldown(
                self._scan_result(),
                db_path=db_path,
                cooldown_days=3,
                signals_config=_signals_config(),
            )
            changed = self._scan_result()
            changed["alerts"][0]["confidence_score"] = 0.55
            changed["alerts"][0]["confidence_band"] = "medium"
            changed["alerts"][0]["alert_tier"] = "low"
            changed["results"][0]["confidence_score"] = 0.55
            changed["results"][0]["confidence_band"] = "medium"
            changed["results"][0]["alert_tier"] = "low"

            rerun = _apply_alert_cooldown(
                changed,
                db_path=db_path,
                cooldown_days=3,
                signals_config=_signals_config(),
            )
            self.assertEqual(len(rerun["alerts"]), 1)
            self.assertEqual(rerun["scan_summary"]["alerts_cooldown_suppressed"], 0)
            self.assertEqual(rerun["results"][0]["notification_status"], "alerted")
            self.assertNotEqual(
                first["results"][0].get("alert_event_id"),
                rerun["results"][0].get("alert_event_id"),
            )

            store = PortfolioStateStore(db_path)
            rows = store.get_watchlist_alert_outcomes(limit=10)
            self.assertEqual(len(rows), 2)
            latest = rows[0]
            self.assertEqual(latest["confirmation_count"], 3)
            self.assertEqual(latest["evidence_breadth"], 3)
            self.assertEqual(latest["outcome_status"], "pending")

    def test_tier_upgrade_overrides_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "portfolio.db"
            scan_result = self._scan_result()
            alert = scan_result["alerts"][0]
            fingerprint = _make_alert_fingerprint(alert)
            state_hash = _make_alert_state_hash(alert)

            store = PortfolioStateStore(db_path)
            store.upsert_alert_event(
                fingerprint,
                severity="normal",
                state_hash=state_hash,
                alert_tier="medium",
                reason_code="allowed_medium",
            )
            store.record_alert_emailed(fingerprint)

            rerun = _apply_alert_cooldown(
                scan_result,
                db_path=db_path,
                cooldown_days=3,
                signals_config=_signals_config(),
            )
            self.assertEqual(len(rerun["alerts"]), 1)
            self.assertEqual(rerun["results"][0]["notification_status"], "alerted")
            self.assertIn("tier medium -> high", rerun["results"][0].get("cooldown_override_reason", ""))


class TestSignalMetaLayer(unittest.TestCase):

    @staticmethod
    def _data_health(*, degraded: bool = False, penalty: float = 0.0) -> dict:
        return {
            "degraded_mode": degraded,
            "degraded_reason": "fmp_403" if degraded else None,
            "data_sources_used": ["fallback"] if degraded else ["alphavantage"],
            "data_mode": "fallback" if degraded else "live",
            "degraded_confidence_penalty": penalty,
        }

    def _scan_result(self) -> dict:
        alert = {
            "ticker": "AMD",
            "signal_score": 0.80,
            "confidence_score": 0.90,
            "confidence_band": "high",
            "notification_status": "alerted",
            "notification_reason": "",
            "alert_priority": "high",
            "alert_tier": "high",
            "watchlist_source": "static",
            "filter_reason_code": "allowed_high",
        }
        return {
            "run_date": "2026-04-14",
            "generated_at": "2026-04-14T12:00:00",
            "calls_used": 1,
            "scan_summary": {"scan_status": "ok"},
            "results": [dict(alert)],
            "alerts": [alert],
        }

    def test_effective_score_uses_confidence_weight(self):
        enriched = _apply_signal_meta_layer(
            self._scan_result(),
            data_health=self._data_health(),
            signals_config=_signals_config(),
        )
        row = enriched["results"][0]
        self.assertAlmostEqual(row["confidence_weight"], 0.90)
        self.assertAlmostEqual(row["effective_score"], 0.72)

    def test_degraded_penalty_reduces_effective_score(self):
        enriched = _apply_signal_meta_layer(
            self._scan_result(),
            data_health=self._data_health(degraded=True, penalty=0.25),
            signals_config=_signals_config(),
        )
        row = enriched["results"][0]
        self.assertAlmostEqual(row["effective_score"], 0.54)

    def test_second_identical_alert_records_cooldown_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "portfolio.db"
            _apply_alert_cooldown(
                self._scan_result(),
                db_path=db_path,
                cooldown_days=3,
                signals_config=_signals_config(),
            )
            second = _apply_alert_cooldown(
                self._scan_result(),
                db_path=db_path,
                cooldown_days=3,
                signals_config=_signals_config(),
            )
            enriched = _apply_signal_meta_layer(
                second,
                data_health=self._data_health(),
                db_path=db_path,
                signals_config=_signals_config(),
            )
            row = enriched["results"][0]
            self.assertTrue(row["cooldown_active"])
            self.assertIn("cooldown-suppressed", row["cooldown_reason"])
            self.assertEqual(row["last_action_taken"], "cooldown_suppressed")
            self.assertGreater(enriched["scan_summary"]["cooldown_hits"], 0)

    def test_high_confidence_signal_can_bypass_degraded_action_filter(self):
        enriched = _apply_signal_meta_layer(
            self._scan_result(),
            data_health=self._data_health(degraded=True, penalty=0.30),
            signals_config={
                **_signals_config(),
                "action_filter": {
                    "min_degraded_confidence_score": 0.70,
                    "high_confidence_score": 0.85,
                    "strong_signal_score": 0.75,
                },
            },
        )
        self.assertEqual(len(enriched["alerts"]), 1)
        self.assertTrue(enriched["results"][0]["actionable_signal"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
