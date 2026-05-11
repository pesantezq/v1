"""
Tests for the Dashboard cockpit summary slice.

Covers:
  - new loaders (news_evidence_layer, market_narrative_daily) graceful degradation
  - new loaders parse valid input
  - load_operator_dashboard_data exposes both new keys
  - cockpit summary helper exists in gui/app.py
  - cockpit summary helper uses no trading-instruction language outside
    fixed safety disclaimer wording
  - page_dashboard() invokes the cockpit summary helper exactly once
  - reusable helpers are wired through (no duplicate badge logic)
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from gui_operator_data import (
    load_news_evidence_layer,
    load_market_narrative_daily,
    load_operator_dashboard_data,
    NEWS_EVIDENCE_LAYER_RELATIVE_PATH,
    MARKET_NARRATIVE_DAILY_RELATIVE_PATH,
)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. New loaders — graceful degradation
# ---------------------------------------------------------------------------

class TestNewLoaderDegradation:
    def test_missing_news_evidence_layer_returns_unavailable(self, tmp_path):
        assert load_news_evidence_layer(tmp_path) == {"available": False}

    def test_malformed_news_evidence_layer_returns_unavailable(self, tmp_path):
        _write(tmp_path.joinpath(*NEWS_EVIDENCE_LAYER_RELATIVE_PATH), "NOT JSON")
        assert load_news_evidence_layer(tmp_path) == {"available": False}

    def test_non_object_news_evidence_layer_returns_unavailable(self, tmp_path):
        _write(tmp_path.joinpath(*NEWS_EVIDENCE_LAYER_RELATIVE_PATH), "[1,2,3]")
        assert load_news_evidence_layer(tmp_path) == {"available": False}

    def test_empty_news_evidence_layer_returns_unavailable(self, tmp_path):
        _write(tmp_path.joinpath(*NEWS_EVIDENCE_LAYER_RELATIVE_PATH), "")
        assert load_news_evidence_layer(tmp_path) == {"available": False}

    def test_missing_market_narrative_returns_unavailable(self, tmp_path):
        assert load_market_narrative_daily(tmp_path) == {"available": False}

    def test_malformed_market_narrative_returns_unavailable(self, tmp_path):
        _write(tmp_path.joinpath(*MARKET_NARRATIVE_DAILY_RELATIVE_PATH), "{not json")
        assert load_market_narrative_daily(tmp_path) == {"available": False}


# ---------------------------------------------------------------------------
# 2. New loaders — valid parsing
# ---------------------------------------------------------------------------

class TestNewLoaderValid:
    def test_valid_news_evidence_layer_loaded(self, tmp_path):
        payload = {
            "observe_only": True,
            "no_trade": True,
            "ticker_contexts": [{"ticker": "NVDA"}, {"ticker": "AAPL"}],
        }
        _write(tmp_path.joinpath(*NEWS_EVIDENCE_LAYER_RELATIVE_PATH),
               json.dumps(payload))
        result = load_news_evidence_layer(tmp_path)
        assert result["available"] is True
        assert len(result["ticker_contexts"]) == 2

    def test_valid_market_narrative_loaded(self, tmp_path):
        payload = {
            "observe_only": True,
            "top_headline": "Markets show AI infrastructure momentum.",
            "narrative_period": "daily",
        }
        _write(tmp_path.joinpath(*MARKET_NARRATIVE_DAILY_RELATIVE_PATH),
               json.dumps(payload))
        result = load_market_narrative_daily(tmp_path)
        assert result["available"] is True
        assert "AI infrastructure" in result["top_headline"]


# ---------------------------------------------------------------------------
# 3. Aggregator wiring
# ---------------------------------------------------------------------------

class TestAggregatorWiring:
    def test_dashboard_data_exposes_new_keys(self, tmp_path):
        dashboard = load_operator_dashboard_data(tmp_path)
        assert "news_evidence_layer" in dashboard
        assert "market_narrative_daily" in dashboard
        # Stable shape — both unavailable when artifacts absent
        assert dashboard["news_evidence_layer"]["available"] is False
        assert dashboard["market_narrative_daily"]["available"] is False

    def test_dashboard_data_picks_up_valid_artifacts(self, tmp_path):
        _write(tmp_path.joinpath(*NEWS_EVIDENCE_LAYER_RELATIVE_PATH),
               json.dumps({"ticker_contexts": [{"ticker": "NVDA"}]}))
        _write(tmp_path.joinpath(*MARKET_NARRATIVE_DAILY_RELATIVE_PATH),
               json.dumps({"top_headline": "Healthy day."}))
        dashboard = load_operator_dashboard_data(tmp_path)
        assert dashboard["news_evidence_layer"]["available"] is True
        assert dashboard["market_narrative_daily"]["available"] is True

    def test_dashboard_loaders_do_not_write(self, tmp_path):
        _write(tmp_path.joinpath(*NEWS_EVIDENCE_LAYER_RELATIVE_PATH),
               json.dumps({"ticker_contexts": []}))
        before = sorted(p.name for p in (tmp_path / "outputs" / "latest").glob("*"))
        load_operator_dashboard_data(tmp_path)
        after = sorted(p.name for p in (tmp_path / "outputs" / "latest").glob("*"))
        assert before == after, "Loaders must not create or modify files"


# ---------------------------------------------------------------------------
# 4. Cockpit summary helper source-level checks
# ---------------------------------------------------------------------------

class TestCockpitSummaryHelper:
    @pytest.fixture(scope="class")
    def src(self) -> str:
        return Path("gui/app.py").read_text(encoding="utf-8")

    def test_helper_defined(self, src):
        assert "def _render_cockpit_summary_grid" in src

    def test_helper_invoked_in_page_dashboard(self, src):
        # _render_cockpit_summary_grid should be called from page_dashboard
        marker = "def page_dashboard"
        assert marker in src
        dashboard_block = src.split(marker, 1)[1]
        # Next page_ definition delimits the function body
        end = re.search(r"\n(def page_\w+|# =+\n#)", dashboard_block)
        if end:
            dashboard_block = dashboard_block[: end.start()]
        assert "_render_cockpit_summary_grid(bundle)" in dashboard_block

    def test_helper_uses_reusable_components(self, src):
        # Helper should compose with render_metric_card / render_status_badge
        marker = "def _render_cockpit_summary_grid"
        assert marker in src
        block = src.split(marker, 1)[1]
        # End at next top-level def
        end = re.search(r"\ndef page_\w+", block)
        if end:
            block = block[: end.start()]
        assert "render_metric_card" in block
        assert "render_status_badge" in block
        assert "render_section_header" in block

    def test_helper_renders_seven_cards(self, src):
        marker = "def _render_cockpit_summary_grid"
        block = src.split(marker, 1)[1]
        end = re.search(r"\ndef page_\w+", block)
        if end:
            block = block[: end.start()]
        # Expect each card title to appear in the helper body
        for title in (
            "Portfolio Status",
            "Today's Market Narrative",
            "Decision Plan",
            "Data Quality",
            "News Evidence",
            "Automatic Promotion",
            "Memo Delivery",
        ):
            assert title in block, f"Card missing: {title!r}"

    def test_helper_avoids_forbidden_trading_language(self, src):
        marker = "def _render_cockpit_summary_grid"
        block = src.split(marker, 1)[1]
        end = re.search(r"\ndef page_\w+", block)
        if end:
            block = block[: end.start()]
        # Strip the fixed safety reminder line which legitimately mentions
        # buy/sell/hold inside the disclaimer wording.
        block_lower = block.lower()
        # The literal disclaimer used in the Safety Boundary card:
        block_lower = block_lower.replace(
            "no trades. no portfolio mutation. no buy/sell/hold recommendation.",
            ""
        )
        for phrase in (
            "buy now", "sell now", "execute trade", "rebalance now",
            "add to watchlist", "promote candidate", "actionable buy",
            "validated sell",
        ):
            assert phrase not in block_lower, \
                f"Forbidden phrase {phrase!r} present in cockpit summary helper"


# ---------------------------------------------------------------------------
# 5. Read-only / no-mutation invariant for new loaders
# ---------------------------------------------------------------------------

class TestReadOnlyInvariant:
    def test_news_evidence_loader_no_writes(self, tmp_path):
        _write(tmp_path.joinpath(*NEWS_EVIDENCE_LAYER_RELATIVE_PATH),
               json.dumps({"ticker_contexts": []}))
        before = sorted(p.name for p in (tmp_path / "outputs" / "latest").glob("*"))
        load_news_evidence_layer(tmp_path)
        after = sorted(p.name for p in (tmp_path / "outputs" / "latest").glob("*"))
        assert before == after

    def test_market_narrative_loader_no_writes(self, tmp_path):
        _write(tmp_path.joinpath(*MARKET_NARRATIVE_DAILY_RELATIVE_PATH),
               json.dumps({"top_headline": "x"}))
        before = sorted(p.name for p in (tmp_path / "outputs" / "latest").glob("*"))
        load_market_narrative_daily(tmp_path)
        after = sorted(p.name for p in (tmp_path / "outputs" / "latest").glob("*"))
        assert before == after


# ---------------------------------------------------------------------------
# 6. Cockpit summary tone logic (text-level verification via source inspection)
# ---------------------------------------------------------------------------

class TestCockpitSummaryToneLogic:
    """Ensure the helper maps known statuses to the documented tones."""

    def test_no_action_label_emission_in_helper(self):
        # Defense in depth: the helper must never directly emit BUY/SELL/HOLD/
        # PROMOTED/VALIDATED/ACTIONABLE/APPROVED/TRADE/RECOMMENDATION as bare
        # standalone status values.
        src = Path("gui/app.py").read_text(encoding="utf-8")
        marker = "def _render_cockpit_summary_grid"
        block = src.split(marker, 1)[1]
        end = re.search(r"\ndef page_\w+", block)
        if end:
            block = block[: end.start()]
        # Strip the safety reminder line and any disclaimer wording
        block_normalised = block.replace(
            "No trades. No portfolio mutation. No buy/sell/hold recommendation.",
            ""
        )
        # No standalone whole-word action emissions in source code outside
        # legitimate Python identifiers (which use underscores)
        for token in ("BUY", "SELL", "HOLD", "ACTIONABLE", "PROMOTED",
                      "VALIDATED", "APPROVED", "TRADE", "RECOMMENDATION"):
            # Allow inside strings already covered above; rare-but-possible
            # accidental string literal — search for f'"{token}"' style
            assert f'"{token}"' not in block_normalised, \
                f"Forbidden action literal {token!r} as bare quoted string"
            assert f"'{token}'" not in block_normalised, \
                f"Forbidden action literal {token!r} as bare quoted string"
