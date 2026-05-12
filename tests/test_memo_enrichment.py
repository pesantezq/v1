"""
Tests for watchlist_scanner/memo_enrichment.py
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from watchlist_scanner.memo_enrichment import (
    compute_portfolio_growth,
    compute_top_movers,
    compute_decision_hit_rate,
    compute_what_to_watch,
    render_growth_text,
    render_growth_md,
    render_top_movers_text,
    render_top_movers_md,
    render_hit_rate_text,
    render_hit_rate_md,
    render_what_to_watch_text,
    render_what_to_watch_md,
    load_enrichment_data,
    build_enrichment,
    _SANDBOX_DISCLAIMER,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_snapshots_db(tmp_path: Path) -> Path:
    """Create a portfolio.db with the snapshots table and helper for inserts."""
    db = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE run_history (
            run_id TEXT PRIMARY KEY,
            user_id TEXT DEFAULT 'owner'
        )
    """)
    conn.execute("""
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            total_value REAL,
            cash REAL,
            max_drift REAL,
            drawdown_regime TEXT,
            recorded_at TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT 'owner'
        )
    """)
    conn.commit()
    conn.close()
    return db


def _insert_snapshot(
    db: Path, *, value: float, cash: float, recorded_at: str, user_id: str = "owner"
) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO snapshots (run_id, total_value, cash, recorded_at, user_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (f"run_{recorded_at}", value, cash, recorded_at, user_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1. compute_portfolio_growth
# ---------------------------------------------------------------------------

class TestPortfolioGrowth:
    def test_missing_db_returns_unavailable(self, tmp_path):
        out = compute_portfolio_growth(tmp_path / "nope.db")
        assert out["available"] is False
        assert out["reason"] == "db_missing"

    def test_empty_db_returns_unavailable(self, tmp_path):
        db = _make_snapshots_db(tmp_path)
        out = compute_portfolio_growth(db)
        assert out["available"] is False
        assert out["reason"] == "no_snapshots"

    def test_single_snapshot_no_deltas(self, tmp_path):
        db = _make_snapshots_db(tmp_path)
        _insert_snapshot(db, value=10000.0, cash=500.0, recorded_at="2026-05-12T10:00:00")
        out = compute_portfolio_growth(db, now=datetime(2026, 5, 12, 11, 0))
        assert out["available"] is True
        assert out["today_value"] == 10000.0
        assert out["today_cash"] == 500.0
        # No prior snapshots — all deltas None
        assert out["delta_day"] is None
        assert out["delta_week"] is None
        assert out["delta_month"] is None
        assert out["delta_ytd"] is None

    def test_growth_computed_correctly(self, tmp_path):
        db = _make_snapshots_db(tmp_path)
        # Day-old: $9500
        _insert_snapshot(db, value=9500.0, cash=400.0, recorded_at="2026-05-11T10:00:00")
        # Today: $10000 → +$500, +5.26%
        _insert_snapshot(db, value=10000.0, cash=500.0, recorded_at="2026-05-12T10:00:00")
        out = compute_portfolio_growth(db)
        assert out["available"] is True
        d_day = out["delta_day"]
        assert d_day is not None
        assert d_day[0] == 500.0
        assert abs(d_day[1] - 5.26) < 0.01

    def test_user_id_filter_works(self, tmp_path):
        db = _make_snapshots_db(tmp_path)
        _insert_snapshot(db, value=10000, cash=500, recorded_at="2026-05-12T10:00:00", user_id="owner")
        _insert_snapshot(db, value=999, cash=0, recorded_at="2026-05-12T11:00:00", user_id="other")
        out = compute_portfolio_growth(db, user_id="owner")
        assert out["today_value"] == 10000.0

    def test_handles_null_total_value(self, tmp_path):
        db = _make_snapshots_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO snapshots (run_id, total_value, cash, recorded_at, user_id) "
            "VALUES (?, NULL, ?, ?, ?)",
            ("run_null", 200.0, "2026-05-12T09:00:00", "owner"),
        )
        conn.commit()
        conn.close()
        _insert_snapshot(db, value=10000, cash=500, recorded_at="2026-05-12T10:00:00")
        out = compute_portfolio_growth(db)
        assert out["today_value"] == 10000.0  # NULL row skipped

    def test_corrupt_db_returns_unavailable(self, tmp_path):
        bad = tmp_path / "bad.db"
        bad.write_text("not a sqlite database")
        out = compute_portfolio_growth(bad)
        assert out["available"] is False
        assert out["reason"].startswith("error:")


# ---------------------------------------------------------------------------
# 2. compute_top_movers
# ---------------------------------------------------------------------------

class TestTopMovers:
    def test_no_holdings(self):
        out = compute_top_movers([], {})
        assert out["available"] is False
        assert out["reason"] == "no_holdings"

    def test_no_signals(self):
        out = compute_top_movers([{"symbol": "QQQ", "shares": 6}], {})
        assert out["available"] is False
        assert out["reason"] == "no_signals"

    def test_no_overlap_between_holdings_and_signals(self):
        out = compute_top_movers(
            [{"symbol": "QQQ", "shares": 6}],
            {"signals": [{"symbol": "AAPL", "price": 100, "price_change_1d": 1.0}]},
        )
        assert out["available"] is False
        assert out["reason"] == "no_price_data_for_held"
        assert out["total_held"] == 1

    def test_winners_losers_sorted(self):
        holdings = [
            {"symbol": "QQQ", "shares": 10},
            {"symbol": "GLD", "shares": 5},
            {"symbol": "VOO", "shares": 8},
        ]
        signals = {"signals": [
            {"symbol": "QQQ", "price": 500.0, "price_change_1d": 1.5},
            {"symbol": "GLD", "price": 200.0, "price_change_1d": -0.8},
            {"symbol": "VOO", "price": 450.0, "price_change_1d": 2.3},
        ]}
        out = compute_top_movers(holdings, signals, top_n=2)
        assert out["available"] is True
        assert out["total_held"] == 3
        assert out["total_covered"] == 3
        # Top up: VOO (+2.3%) then QQQ (+1.5%)
        assert out["winners"][0]["symbol"] == "VOO"
        assert out["winners"][1]["symbol"] == "QQQ"
        # Top down: GLD (-0.8%)
        assert out["losers"][0]["symbol"] == "GLD"

    def test_signal_with_technicals_nested(self):
        holdings = [{"symbol": "QQQ", "shares": 10}]
        signals = {"signals": [
            {
                "symbol": "QQQ",
                "technicals": {"price": 500.0, "price_change_1d": 2.0},
            }
        ]}
        out = compute_top_movers(holdings, signals, top_n=1)
        assert out["available"] is True
        assert out["winners"][0]["change_1d_pct"] == 2.0

    def test_skips_holdings_with_invalid_shares(self):
        holdings = [
            {"symbol": "QQQ", "shares": "bad"},
            {"symbol": "GLD", "shares": 5},
        ]
        signals = {"signals": [
            {"symbol": "QQQ", "price": 500.0, "price_change_1d": 1.0},
            {"symbol": "GLD", "price": 200.0, "price_change_1d": 0.5},
        ]}
        out = compute_top_movers(holdings, signals)
        assert out["total_covered"] == 1  # QQQ skipped (bad shares)

    def test_list_signals_shape_supported(self):
        holdings = [{"symbol": "QQQ", "shares": 10}]
        signals = [{"symbol": "QQQ", "price": 500.0, "price_change_1d": 1.0}]
        out = compute_top_movers(holdings, signals)
        assert out["available"] is True


# ---------------------------------------------------------------------------
# 3. compute_decision_hit_rate
# ---------------------------------------------------------------------------

class TestDecisionHitRate:
    def test_no_outcomes_no_calibration(self):
        out = compute_decision_hit_rate([], {})
        assert out["available"] is False

    def test_resolved_count_and_rate(self):
        now = datetime(2026, 5, 12, 12, 0)
        outcomes = [
            {"resolved": True, "direction_correct": True,
             "resolved_at": "2026-05-11T10:00:00",
             "symbol": "AAPL", "decision": "BUY", "return_pct": 2.5},
            {"resolved": True, "direction_correct": False,
             "resolved_at": "2026-05-10T10:00:00",
             "symbol": "TSLA", "decision": "BUY", "return_pct": -3.0},
            {"resolved": True, "direction_correct": True,
             "resolved_at": "2026-05-09T10:00:00",
             "symbol": "NVDA", "decision": "BUY", "return_pct": 4.0},
            # Unresolved — should be ignored
            {"resolved": False, "direction_correct": None,
             "resolved_at": None, "symbol": "GLD", "decision": "BUY"},
        ]
        out = compute_decision_hit_rate(outcomes, {}, now=now)
        assert out["resolved_count"] == 3
        assert out["correct_count"] == 2
        assert abs(out["hit_rate_pct"] - 66.67) < 0.1

    def test_recent_correct_and_missed_lists(self):
        now = datetime(2026, 5, 12, 12, 0)
        outcomes = [
            {"resolved": True, "direction_correct": True,
             "resolved_at": "2026-05-11T10:00:00",
             "symbol": "AAPL", "decision": "BUY", "return_pct": 2.5},
            {"resolved": True, "direction_correct": False,
             "resolved_at": "2026-05-11T11:00:00",
             "symbol": "TSLA", "decision": "BUY", "return_pct": -3.0},
        ]
        out = compute_decision_hit_rate(outcomes, {}, now=now, recent_days=7)
        assert len(out["recent_correct"]) == 1
        assert out["recent_correct"][0]["symbol"] == "AAPL"
        assert len(out["recent_missed"]) == 1
        assert out["recent_missed"][0]["symbol"] == "TSLA"

    def test_outside_window_ignored(self):
        now = datetime(2026, 5, 12, 12, 0)
        outcomes = [
            # 60 days ago — outside default 30-day window
            {"resolved": True, "direction_correct": True,
             "resolved_at": "2026-03-12T10:00:00",
             "symbol": "AAPL", "decision": "BUY"},
        ]
        out = compute_decision_hit_rate(outcomes, {}, now=now, window_days=30)
        assert out["resolved_count"] == 0

    def test_calibration_buckets_surfaced(self):
        cal = {
            "total_resolved": 25,
            "overall_hit_rate": 0.68,
            "confidence_buckets": {
                "high": {"hit_rate": 0.75, "count": 12, "avg_return": 1.5},
                "medium": {"hit_rate": 0.55, "count": 8},
                "low": {"hit_rate": 0.40, "count": 5},
            },
        }
        out = compute_decision_hit_rate([], cal)
        assert out["available"] is True
        assert "high" in out["bucket_hit_rates"]
        assert out["bucket_hit_rates"]["high"]["hit_rate"] == 0.75
        assert out["bucket_hit_rates"]["high"]["count"] == 12
        assert out["calibration_overall_hit_rate"] == 0.68

    def test_garbage_outcomes_ignored(self):
        out = compute_decision_hit_rate(
            [None, "bad", 42, {"resolved": False}, {"resolved": True, "direction_correct": None}],
            {},
        )
        assert out["resolved_count"] == 0


# ---------------------------------------------------------------------------
# 4. compute_what_to_watch
# ---------------------------------------------------------------------------

class TestWhatToWatch:
    def test_no_auto_promotion(self):
        out = compute_what_to_watch({}, {})
        assert out["available"] is False

    def test_monitor_candidates_surfaced(self):
        auto = {
            "decisions": [
                {"ticker": "NVDA", "proposed_status": "MONITOR", "evidence_score": 0.85,
                 "corroboration_score": 0.7, "news_relevance_score": 0.6,
                 "catalyst_flags": ["beat estimates"]},
                {"ticker": "AAPL", "proposed_status": "NEEDS_REVIEW", "evidence_score": 0.45},
                {"ticker": "TSLA", "proposed_status": "REJECTED", "evidence_score": 0.1},
            ]
        }
        out = compute_what_to_watch(auto, {})
        assert out["available"] is True
        assert out["monitor_count"] == 1
        assert out["needs_review_count"] == 1
        assert out["monitor_top"][0]["ticker"] == "NVDA"
        assert out["monitor_top"][0]["evidence_score"] == 0.85
        # REJECTED candidates not surfaced
        for entry in out["monitor_top"] + out["needs_review_top"]:
            assert entry["ticker"] != "TSLA"

    def test_news_context_attached(self):
        auto = {"decisions": [
            {"ticker": "NVDA", "proposed_status": "MONITOR", "evidence_score": 0.8},
        ]}
        news = {"ticker_contexts": [
            {"ticker": "NVDA", "evidence_strength": "strong",
             "context_effect": "catalyst_context"},
        ]}
        out = compute_what_to_watch(auto, news)
        e = out["monitor_top"][0]
        assert e["news_evidence_strength"] == "strong"
        assert e["news_context_effect"] == "catalyst_context"

    def test_safety_disclaimer_present(self):
        auto = {"decisions": [{"ticker": "X", "proposed_status": "MONITOR"}]}
        out = compute_what_to_watch(auto, {})
        assert _SANDBOX_DISCLAIMER in out["safety_disclaimer"]

    def test_top_n_respected(self):
        auto = {"decisions": [
            {"ticker": f"T{i}", "proposed_status": "MONITOR", "evidence_score": 0.5 + i*0.01}
            for i in range(10)
        ]}
        out = compute_what_to_watch(auto, {}, top_n=3)
        assert len(out["monitor_top"]) == 3
        # Top 3 by evidence_score descending: T9, T8, T7
        assert [e["ticker"] for e in out["monitor_top"]] == ["T9", "T8", "T7"]


# ---------------------------------------------------------------------------
# 5. Renderers — text variant
# ---------------------------------------------------------------------------

class TestTextRenderers:
    def test_growth_unavailable(self):
        out = render_growth_text({"available": False, "reason": "db_missing"})
        assert any("not yet available" in line for line in out)

    def test_growth_with_data(self):
        g = {
            "available": True,
            "today_value": 10000.0, "today_cash": 500.0,
            "delta_day": (500.0, 5.0),
            "delta_week": (1000.0, 10.0),
            "delta_month": None,
            "delta_ytd": (2000.0, 25.0),
        }
        out = render_growth_text(g)
        assert any("Total value" in line for line in out)
        assert any("+$500.00" in line for line in out)
        assert any("+5.00%" in line for line in out)

    def test_movers_unavailable_no_price_data(self):
        out = render_top_movers_text({
            "available": False, "reason": "no_price_data_for_held", "total_held": 6
        })
        assert any("6 held position" in line for line in out)

    def test_movers_winners_and_losers(self):
        m = {
            "available": True, "total_held": 3, "total_covered": 3,
            "winners": [{"symbol": "QQQ", "shares": 6, "change_1d_pct": 2.0, "change_1d_dollar": 60.0}],
            "losers": [{"symbol": "GLD", "shares": 4, "change_1d_pct": -1.0, "change_1d_dollar": -8.0}],
        }
        out = render_top_movers_text(m)
        assert any("QQQ" in line and "+2.00%" in line for line in out)
        assert any("GLD" in line and "-1.00%" in line for line in out)

    def test_hit_rate_unavailable(self):
        out = render_hit_rate_text({"available": False})
        assert any("not yet available" in line for line in out)

    def test_hit_rate_with_data(self):
        hr = {
            "available": True, "window_days": 30,
            "resolved_count": 10, "correct_count": 6,
            "hit_rate_pct": 60.0,
            "bucket_hit_rates": {"high": {"hit_rate": 0.75, "count": 5}},
            "recent_correct": [{"symbol": "AAPL", "decision": "BUY", "return_pct": 3.0}],
            "recent_missed": [{"symbol": "TSLA", "decision": "BUY", "return_pct": -2.0}],
        }
        out = render_hit_rate_text(hr)
        assert any("6 of 10" in line for line in out)
        assert any("BUY AAPL" in line for line in out)
        assert any("BUY TSLA" in line for line in out)

    def test_what_to_watch_unavailable(self):
        out = render_what_to_watch_text({"available": False})
        assert any("No sandbox" in line for line in out)

    def test_what_to_watch_disclaimer_always_present(self):
        wtw = {
            "available": True, "monitor_count": 1, "needs_review_count": 0,
            "monitor_top": [{"ticker": "NVDA", "evidence_score": 0.8, "catalyst_flags": ["x"]}],
            "needs_review_top": [],
            "safety_disclaimer": _SANDBOX_DISCLAIMER,
        }
        out = render_what_to_watch_text(wtw)
        assert any(_SANDBOX_DISCLAIMER in line for line in out)


# ---------------------------------------------------------------------------
# 6. Renderers — markdown variant
# ---------------------------------------------------------------------------

class TestMarkdownRenderers:
    def test_growth_md_unavailable(self):
        out = render_growth_md({"available": False})
        assert any("_Portfolio growth data not yet available._" in line for line in out)

    def test_growth_md_bold_markers(self):
        g = {
            "available": True,
            "today_value": 10000.0, "today_cash": 500.0,
            "delta_day": (500.0, 5.0),
            "delta_week": None, "delta_month": None, "delta_ytd": None,
        }
        out = render_growth_md(g)
        assert any("**Total value:**" in line for line in out)

    def test_movers_md_ticker_backticks(self):
        m = {
            "available": True, "total_held": 1, "total_covered": 1,
            "winners": [{"symbol": "QQQ", "shares": 6, "change_1d_pct": 2.0, "change_1d_dollar": 60.0}],
            "losers": [],
        }
        out = render_top_movers_md(m)
        assert any("`QQQ`" in line for line in out)

    def test_hit_rate_md_buckets(self):
        hr = {
            "available": True, "window_days": 30,
            "resolved_count": 5, "correct_count": 4, "hit_rate_pct": 80.0,
            "bucket_hit_rates": {"high": {"hit_rate": 0.8, "count": 5}},
            "recent_correct": [], "recent_missed": [],
        }
        out = render_hit_rate_md(hr)
        assert any("`high`" in line for line in out)

    def test_what_to_watch_md_disclaimer(self):
        wtw = {
            "available": True, "monitor_count": 0, "needs_review_count": 0,
            "monitor_top": [], "needs_review_top": [],
            "safety_disclaimer": _SANDBOX_DISCLAIMER,
        }
        out = render_what_to_watch_md(wtw)
        assert any(_SANDBOX_DISCLAIMER in line for line in out)


# ---------------------------------------------------------------------------
# 7. Safety — no trading-instruction language in any rendered output
# ---------------------------------------------------------------------------

_FORBIDDEN_PHRASES = (
    "buy now", "sell now", "hold now", "execute trade", "rebalance now",
    "promote candidate", "add to watchlist", "trim position",
)


def _render_all_sections() -> list[str]:
    """Render every section under realistic data and return combined output."""
    growth = {
        "available": True, "today_value": 10000.0, "today_cash": 500.0,
        "delta_day": (250.0, 2.5), "delta_week": (100.0, 1.0),
        "delta_month": None, "delta_ytd": (1500.0, 17.6),
    }
    movers = {
        "available": True, "total_held": 6, "total_covered": 4,
        "winners": [{"symbol": "QQQ", "shares": 6, "change_1d_pct": 2.5, "change_1d_dollar": 75.0}],
        "losers": [{"symbol": "GLD", "shares": 4, "change_1d_pct": -1.2, "change_1d_dollar": -10.0}],
    }
    hr = {
        "available": True, "window_days": 30,
        "resolved_count": 8, "correct_count": 5, "hit_rate_pct": 62.5,
        "bucket_hit_rates": {"high": {"hit_rate": 0.75, "count": 4}},
        "recent_correct": [{"symbol": "AAPL", "decision": "BUY", "return_pct": 3.0}],
        "recent_missed": [{"symbol": "TSLA", "decision": "BUY", "return_pct": -1.5}],
    }
    wtw = {
        "available": True, "monitor_count": 1, "needs_review_count": 0,
        "monitor_top": [{"ticker": "NVDA", "evidence_score": 0.85, "catalyst_flags": ["beat estimates"]}],
        "needs_review_top": [], "safety_disclaimer": _SANDBOX_DISCLAIMER,
    }
    combined: list[str] = []
    for fn, arg in [
        (render_growth_text, growth), (render_growth_md, growth),
        (render_top_movers_text, movers), (render_top_movers_md, movers),
        (render_hit_rate_text, hr), (render_hit_rate_md, hr),
        (render_what_to_watch_text, wtw), (render_what_to_watch_md, wtw),
    ]:
        combined.extend(fn(arg))
    return combined


class TestSafetyLanguage:
    def test_no_trading_instructions(self):
        combined = "\n".join(_render_all_sections()).lower()
        # Strip the safety disclaimer (it legitimately contains "buy/sell/hold")
        combined = combined.replace(_SANDBOX_DISCLAIMER.lower(), "")
        for phrase in _FORBIDDEN_PHRASES:
            assert phrase not in combined, \
                f"Forbidden instruction phrase {phrase!r} found in rendered output"

    def test_no_bare_action_imperatives(self):
        """Should not emit standalone 'BUY' / 'SELL' / 'HOLD' as imperatives."""
        combined = "\n".join(_render_all_sections())
        # We do emit 'BUY' as a decision label (e.g. "BUY AAPL: +3.00%") — that's
        # describing a recorded decision, not a recommendation.  But we should
        # NOT have lines like "ACTION: BUY" without context.
        for line in combined.split("\n"):
            stripped = line.strip().strip("-").strip().strip("*").strip()
            assert stripped.upper() not in {"BUY", "SELL", "HOLD"}, \
                f"Bare action imperative line: {line!r}"


# ---------------------------------------------------------------------------
# 8. Determinism
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_compute_funcs_are_deterministic(self):
        auto = {"decisions": [
            {"ticker": "NVDA", "proposed_status": "MONITOR", "evidence_score": 0.85},
            {"ticker": "AAPL", "proposed_status": "MONITOR", "evidence_score": 0.65},
        ]}
        r1 = compute_what_to_watch(auto, {})
        r2 = compute_what_to_watch(auto, {})
        assert r1 == r2


# ---------------------------------------------------------------------------
# 9. load_enrichment_data / build_enrichment integration
# ---------------------------------------------------------------------------

class TestLoaderAndBuilder:
    def test_load_enrichment_data_with_missing_files(self, tmp_path):
        src = load_enrichment_data(tmp_path)
        assert isinstance(src.holdings, list)
        assert isinstance(src.decision_outcomes, list)
        assert isinstance(src.calibration, dict)

    def test_load_enrichment_data_picks_up_config_holdings(self, tmp_path):
        cfg = {
            "portfolio": {
                "holdings": [
                    {"symbol": "QQQ", "shares": 6},
                    {"symbol": "GLD", "shares": 4},
                ],
                "cash_available": 500.0,
            }
        }
        (tmp_path / "config.json").write_text(json.dumps(cfg))
        src = load_enrichment_data(tmp_path)
        assert len(src.holdings) == 2
        assert src.holdings[0]["symbol"] == "QQQ"

    def test_build_enrichment_returns_four_keys(self, tmp_path):
        out = build_enrichment(tmp_path)
        assert set(out.keys()) == {"growth", "movers", "hit_rate", "what_to_watch"}
        # All unavailable (no data sources) — should not raise
        for key in out:
            assert isinstance(out[key], dict)
            # Either available True or False with a reason — never an exception
            assert "available" in out[key]
