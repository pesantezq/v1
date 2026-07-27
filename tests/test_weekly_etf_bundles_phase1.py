"""
Phase 1 tests — config, models, deterministic analysis + scoring for the
weekly ETF bundle subsystem. All tests are hermetic: synthetic PricePanels,
temp config files, no disk artifacts, no FMP key.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from portfolio_automation.portfolio_sim.prices import PricePanel
from portfolio_automation.weekly_etf_bundles import POSTURE
from portfolio_automation.weekly_etf_bundles.analysis import (
    build_weekly_analysis,
    compute_etf_metrics,
    last_on_or_before,
)
from portfolio_automation.weekly_etf_bundles.config import load_config
from portfolio_automation.weekly_etf_bundles.models import WeeklyEtfConfigError
from portfolio_automation.weekly_etf_bundles.scoring import (
    ScoringParams,
    label_for_score,
    score_etf,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _weekdays(start_iso: str, n: int) -> list[str]:
    d = date.fromisoformat(start_iso)
    out: list[str] = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _panel(series_by_symbol: dict[str, list[float]], dates: list[str]) -> PricePanel:
    closes = {sym: {dates[i]: float(p) for i, p in enumerate(prices)}
              for sym, prices in series_by_symbol.items()}
    volumes = {sym: {d: 1.0 for d in dates} for sym in series_by_symbol}
    return PricePanel(closes, volumes, list(dates), [])


def _write_cfg(tmp_path, body: str):
    p = tmp_path / "wb.yaml"
    p.write_text(body, encoding="utf-8")
    return p


_MIN = """
schema_version: 1
defaults:
  benchmark: SPY
bundles:
  - id: a
    name: A
    benchmark: SPY
    members:
      - symbol: AAA
      - symbol: BBB
"""


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_config_loads_repo_default():
    cfg = load_config()
    assert cfg.schema_version == 1
    assert cfg.content_hash.startswith("sha256:")
    assert len(cfg.enabled_bundles) >= 2
    assert "SPY" in cfg.all_symbols


def test_config_valid_minimal(tmp_path):
    cfg = load_config(_write_cfg(tmp_path, _MIN))
    assert [b.id for b in cfg.enabled_bundles] == ["a"]
    assert cfg.bundle("a").symbols == ["AAA", "BBB"]


def test_config_duplicate_bundle_ids(tmp_path):
    body = _MIN + """
  - id: a
    name: A2
    benchmark: SPY
    members:
      - symbol: CCC
"""
    with pytest.raises(WeeklyEtfConfigError, match="duplicate bundle id"):
        load_config(_write_cfg(tmp_path, body))


def test_config_duplicate_symbols(tmp_path):
    body = """
schema_version: 1
bundles:
  - id: a
    name: A
    benchmark: SPY
    members:
      - symbol: AAA
      - symbol: AAA
"""
    with pytest.raises(WeeklyEtfConfigError, match="duplicate symbol"):
        load_config(_write_cfg(tmp_path, body))


def test_config_custom_weights_must_sum_to_one(tmp_path):
    body = """
schema_version: 1
bundles:
  - id: a
    name: A
    benchmark: SPY
    weighting_method: custom
    members:
      - symbol: AAA
        weight: 0.5
      - symbol: BBB
        weight: 0.2
"""
    with pytest.raises(WeeklyEtfConfigError, match="sum to 1.0"):
        load_config(_write_cfg(tmp_path, body))


def test_config_custom_weights_ok(tmp_path):
    body = """
schema_version: 1
bundles:
  - id: a
    name: A
    benchmark: SPY
    weighting_method: custom
    members:
      - symbol: AAA
        weight: 0.6
      - symbol: BBB
        weight: 0.4
"""
    cfg = load_config(_write_cfg(tmp_path, body))
    assert cfg.bundle("a").resolved_weights() == {"AAA": 0.6, "BBB": 0.4}


def test_config_disabled_bundle_excluded_but_present(tmp_path):
    body = _MIN + """
  - id: b
    name: B
    benchmark: SPY
    enabled: false
    members:
      - symbol: CCC
"""
    cfg = load_config(_write_cfg(tmp_path, body))
    assert [b.id for b in cfg.enabled_bundles] == ["a"]      # disabled excluded
    assert cfg.bundle("b") is not None                        # still in config


def test_config_invalid_schema_fails_closed(tmp_path):
    with pytest.raises(WeeklyEtfConfigError, match="schema_version"):
        load_config(_write_cfg(tmp_path, "schema_version: 99\nbundles: []\n"))


def test_config_no_enabled_bundles_fails_closed(tmp_path):
    body = """
schema_version: 1
bundles:
  - id: a
    name: A
    benchmark: SPY
    enabled: false
    members:
      - symbol: AAA
"""
    with pytest.raises(WeeklyEtfConfigError, match="no enabled bundles"):
        load_config(_write_cfg(tmp_path, body))


def test_config_hash_stable_and_content_sensitive(tmp_path):
    h1 = load_config(_write_cfg(tmp_path, _MIN)).content_hash
    h2 = load_config(_write_cfg(tmp_path, _MIN)).content_hash
    assert h1 == h2                                            # stable
    h3 = load_config(_write_cfg(tmp_path, _MIN.replace("BBB", "CCC"))).content_hash
    assert h3 != h1                                            # membership change bumps it


def test_config_hash_ignores_comments(tmp_path):
    h1 = load_config(_write_cfg(tmp_path, _MIN)).content_hash
    h2 = load_config(_write_cfg(tmp_path, "# a comment\n" + _MIN)).content_hash
    assert h1 == h2


# --------------------------------------------------------------------------- #
# analysis
# --------------------------------------------------------------------------- #
def test_last_on_or_before():
    dates = ["2026-01-01", "2026-01-05", "2026-01-09"]
    assert last_on_or_before(dates, "2026-01-07") == "2026-01-05"
    assert last_on_or_before(dates, "2026-01-09") == "2026-01-09"
    assert last_on_or_before(dates, "2025-12-31") is None


def test_returns_and_relative_returns():
    dates = _weekdays("2025-01-01", 260)
    as_of = dates[-1]
    # ETF up 10% over the last 4 weeks; benchmark flat.
    etf = [100.0] * 240 + [100.0 * (1 + 0.10 * (i / 19)) for i in range(20)]
    bm = [100.0] * 260
    panel = _panel({"AAA": etf, "SPY": bm}, dates)
    m = compute_etf_metrics(panel, "AAA", "SPY", as_of)
    assert m["available"] is True
    assert m["return_4w"] == pytest.approx(0.10, abs=0.02)
    assert m["benchmark_return_4w"] == pytest.approx(0.0, abs=1e-9)
    assert m["excess_return_4w"] == pytest.approx(m["return_4w"], abs=1e-9)


def test_no_future_leakage():
    dates = _weekdays("2025-01-01", 260)
    as_of = dates[200]
    # Flat until as_of, then a huge spike AFTER as_of that must be invisible.
    prices = [100.0] * 201 + [1000.0] * 59
    panel = _panel({"AAA": prices, "SPY": [100.0] * 260}, dates)
    m = compute_etf_metrics(panel, "AAA", "SPY", as_of)
    assert m["price"] == 100.0                       # as-of close, not the spike
    assert m["as_of_used"] == as_of
    assert m["return_4w"] == pytest.approx(0.0, abs=1e-9)
    assert m["return_12w"] == pytest.approx(0.0, abs=1e-9)


def test_moving_average_structure_bullish():
    dates = _weekdays("2025-01-01", 260)
    prices = [50.0 + 0.5 * i for i in range(260)]     # steady uptrend
    panel = _panel({"AAA": prices, "SPY": [100.0] * 260}, dates)
    m = compute_etf_metrics(panel, "AAA", "SPY", dates[-1])
    assert m["above_sma20"] and m["above_sma50"] and m["above_sma200"]
    assert m["ma_ordering"] == "bullish"
    assert m["distance_from_52w_high"] == pytest.approx(0.0, abs=1e-9)  # at the high


def test_partial_history_flags_but_does_not_zero():
    dates = _weekdays("2025-01-01", 60)               # < minimum_history_days
    panel = _panel({"AAA": [100.0] * 60, "SPY": [100.0] * 60}, dates)
    m = compute_etf_metrics(panel, "AAA", "SPY", dates[-1], minimum_history_days=200)
    assert m["insufficient_history"] is True
    assert m["sma200"] is None                        # not computable → None, not 0
    assert m["return_12w"] is None                    # 12w lookback predates history → None (no leak), not 0
    assert m["return_4w"] is not None                 # 4w window fits → computed, not zeroed
    assert any(w.startswith("insufficient_history") for w in m["warnings"])


def test_unavailable_symbol_returns_degraded_not_zero():
    dates = _weekdays("2025-01-01", 30)
    panel = _panel({"SPY": [100.0] * 30}, dates)
    m = compute_etf_metrics(panel, "ZZZ", "SPY", dates[-1])
    assert m["available"] is False
    assert m["reason"] == "no_price_history"


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #
def test_label_thresholds():
    assert label_for_score(90) == "leading"
    assert label_for_score(70) == "strengthening"
    assert label_for_score(50) == "mixed"
    assert label_for_score(35) == "weakening"
    assert label_for_score(10) == "lagging"
    assert label_for_score(None) == "insufficient_data"


def test_score_bounds_and_determinism():
    dates = _weekdays("2025-01-01", 260)
    prices = [50.0 + 0.5 * i for i in range(260)]
    panel = _panel({"AAA": prices, "SPY": [100.0] * 260}, dates)
    m = compute_etf_metrics(panel, "AAA", "SPY", dates[-1])
    s1 = score_etf(m)
    s2 = score_etf(m)
    assert s1 == s2                                   # deterministic
    assert 0 <= s1["watch_score"] <= 100
    assert set(s1["components"]) == set(ScoringParams().weights)


def test_score_missing_component_renormalizes_not_zeroed():
    # No 200-day MA available (short history) → trend component drops out,
    # but the score must not be dragged to zero by treating it as 0.
    dates = _weekdays("2025-01-01", 120)
    prices = [50.0 + 0.5 * i for i in range(120)]
    panel = _panel({"AAA": prices, "SPY": [100.0] * 120}, dates)
    m = compute_etf_metrics(panel, "AAA", "SPY", dates[-1], minimum_history_days=50)
    s = score_etf(m)
    assert s["components"]["trend_structure"] is None
    assert s["watch_score"] is not None
    assert s["available_weight"] < 1.0


def test_insufficient_components_yields_none_score():
    m = {"available": True, "symbol": "AAA"}           # no metrics at all
    s = score_etf(m)
    assert s["watch_score"] is None
    assert s["label"] == "insufficient_data"


# --------------------------------------------------------------------------- #
# build_weekly_analysis (end-to-end, in-memory)
# --------------------------------------------------------------------------- #
def test_build_weekly_analysis_payload(tmp_path):
    cfg = load_config(_write_cfg(tmp_path, _MIN))
    dates = _weekdays("2025-01-01", 260)
    up = [50.0 + 0.5 * i for i in range(260)]
    down = [200.0 - 0.3 * i for i in range(260)]
    panel = _panel({"AAA": up, "BBB": down, "SPY": [100.0] * 260}, dates)
    payload = build_weekly_analysis(cfg, panel, as_of=dates[-1], generated_at="2026-07-27T08:00:00Z")

    assert payload["status"] == "ok"
    assert payload["observe_only"] is True
    assert payload["feeds_decision_engine"] is False
    assert payload["config_version"] == cfg.content_hash
    # generated_at is distinct from market_data_date
    assert payload["generated_at"] == "2026-07-27T08:00:00Z"
    assert payload["market_data_date"] == dates[-1]
    assert payload["market_data_date"] != payload["generated_at"]
    # global ranking: the uptrend ETF ranks above the downtrend one
    ranks = {r["symbol"]: r["rank_global"] for r in payload["ranking_global"]}
    assert ranks["AAA"] < ranks["BBB"]
    assert payload["bundle_count"] == 1
    assert payload["etf_count"] == 2


def test_posture_constant_is_hardcoded():
    assert POSTURE == {
        "observe_only": True,
        "simulation_active": True,
        "production_gated": True,
        "human_approval_required_for_production": True,
        "feeds_decision_engine": False,
    }
