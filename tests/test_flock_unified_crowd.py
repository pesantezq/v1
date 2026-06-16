"""Flock Intelligence now PREFERS the unified crowd bus.

Asserts:
  (a) when outputs/latest/unified_crowd_intelligence.json exists with records,
      load_crowd_metrics sources from the unified bus and enriches each entry
      with the unified cross-source fields; build_group_metrics surfaces the
      group-level aggregate (crowd_source == 'unified').
  (b) when the unified artifact is absent, the legacy multi_source +
      public_knowledge fallback still works unchanged (crowd_source 'legacy').
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.flock_intelligence import data_sources as ds
from portfolio_automation.flock_intelligence.producer import build_group_metrics


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _unified_record(ticker: str, *, retail=None, fmp=None) -> dict:
    return {
        "ticker": ticker,
        "retail_attention_score": retail,
        "fmp_attention_score": fmp,
        "source_breadth_total": 2,
        "source_breadth_social": 1,
        "source_breadth_fmp": 1,
        "cross_source_confirmation_score": 0.8,
        "cross_source_divergence_score": 0.1,
        "crowd_state": "confirmed_attention",
        "news_score": 0.4,
        "analyst_score": 0.6,
        "insider_score": -0.2,
        "congress_score": 0.0,
        "crowd_confidence": 0.7,
    }


# ---------------------------------------------------------------------------
# (a) unified bus present -> preferred + enriched
# ---------------------------------------------------------------------------

def test_load_crowd_metrics_prefers_unified_bus(tmp_path):
    _write(
        tmp_path / "outputs/latest/unified_crowd_intelligence.json",
        {"records": [
            _unified_record("AAPL", retail=0.6, fmp=0.4),
            _unified_record("XOM", retail=None, fmp=0.8),  # FMP-only ticker
        ]},
    )
    # A legacy artifact that would WIN if unified were ignored — proves preference.
    _write(
        tmp_path / "outputs/sandbox/discovery/crowd_multi_source_velocity.json",
        {"records": [{"ticker": "AAPL", "mention_velocity": 99.0, "source_breadth": 1}]},
    )

    crowd = ds.load_crowd_metrics(tmp_path)

    assert set(crowd) == {"AAPL", "XOM"}
    aapl = crowd["AAPL"]
    # contract keys preserved
    assert {"velocity", "breadth", "mentions"} <= set(aapl)
    # velocity derived from retail attention * scale (0.6 * 5.0), NOT the legacy 99
    assert aapl["velocity"] == 0.6 * 5.0
    assert aapl["breadth"] == 2.0
    # enriched unified fields present
    assert aapl["retail_attention"] == 0.6
    assert aapl["fmp_attention"] == 0.4
    assert aapl["confirmation"] == 0.8
    assert aapl["divergence"] == 0.1
    assert aapl["crowd_state"] == "confirmed_attention"
    assert aapl["source_breadth_total"] == 2.0
    assert aapl["analyst"] == 0.6

    # FMP-only ticker falls back to fmp attention for velocity (no longer dark).
    assert crowd["XOM"]["velocity"] == 0.8 * 5.0
    assert crowd["XOM"]["retail_attention"] == 0.0
    assert crowd["XOM"]["fmp_attention"] == 0.8


def test_build_group_metrics_surfaces_unified_aggregate(tmp_path):
    _write(
        tmp_path / "outputs/latest/unified_crowd_intelligence.json",
        {"records": [
            _unified_record("AAPL", retail=0.6, fmp=0.4),
            _unified_record("MSFT", retail=0.5, fmp=0.5),
        ]},
    )
    crowd = ds.load_crowd_metrics(tmp_path)
    gm = build_group_metrics("Tech", "theme", ["AAPL", "MSFT"], crowd, {}, None)

    assert gm.crowd_source == "unified"
    assert gm.cross_source_confirmation == 0.8
    assert gm.cross_source_divergence == 0.1
    # fmp_context = mean of |news|,|analyst|,|insider|,|congress| over members
    # = (0.4 + 0.6 + 0.2 + 0.0) / 4 = 0.3
    assert gm.fmp_context_score == 0.3


# ---------------------------------------------------------------------------
# (b) unified absent -> legacy fallback unchanged
# ---------------------------------------------------------------------------

def test_load_crowd_metrics_falls_back_to_legacy(tmp_path):
    # No unified artifact written.
    _write(
        tmp_path / "outputs/sandbox/discovery/crowd_multi_source_velocity.json",
        {"records": [{"ticker": "AAPL", "mention_velocity": 3.5, "source_breadth": 4}]},
    )
    _write(
        tmp_path / "outputs/sandbox/discovery/public_knowledge_velocity.json",
        {"records": [{"ticker": "AAPL", "mention_velocity_zscore": 2.1,
                      "unique_author_count": 12, "mention_count": 88}]},
    )

    crowd = ds.load_crowd_metrics(tmp_path)

    assert "AAPL" in crowd
    aapl = crowd["AAPL"]
    # public-knowledge z-score overrides velocity; author count raises breadth
    assert aapl["velocity"] == 2.1
    assert aapl["breadth"] == 12.0
    assert aapl["mentions"] == 88.0
    # legacy entries carry NO unified enrichment keys
    assert "retail_attention" not in aapl
    assert "confirmation" not in aapl


def test_build_group_metrics_legacy_source_marker(tmp_path):
    _write(
        tmp_path / "outputs/sandbox/discovery/crowd_multi_source_velocity.json",
        {"records": [
            {"ticker": "AAPL", "mention_velocity": 1.2, "source_breadth": 2},
            {"ticker": "MSFT", "mention_velocity": 1.4, "source_breadth": 2},
        ]},
    )
    crowd = ds.load_crowd_metrics(tmp_path)
    gm = build_group_metrics("Tech", "theme", ["AAPL", "MSFT"], crowd, {}, None)

    assert gm.crowd_source == "legacy"
    assert gm.cross_source_confirmation == 0.0
    assert gm.cross_source_divergence == 0.0
    assert gm.fmp_context_score == 0.0


def test_load_crowd_metrics_empty_when_nothing_present(tmp_path):
    assert ds.load_crowd_metrics(tmp_path) == {}
