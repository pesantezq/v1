"""Tests for portfolio_automation/correlation_risk_advisor.py."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio_automation.correlation_risk_advisor import (
    _correlation,
    _daily_log_returns,
    build_pair_flags,
    build_plan,
    effective_independent_bets,
    run_correlation_risk_advisor,
)


# ---------------------------------------------------------------------------
# _daily_log_returns
# ---------------------------------------------------------------------------


def test_log_returns_basic():
    # newest-first input
    closes = [110.0, 100.0]
    rets = _daily_log_returns(closes)
    assert rets == [pytest.approx(math.log(110 / 100), rel=1e-6)]


def test_log_returns_handles_short_list():
    assert _daily_log_returns([]) == []
    assert _daily_log_returns([100.0]) == []


def test_log_returns_drops_invalid_rows():
    closes = [110.0, 0.0, 100.0]  # zero price between
    rets = _daily_log_returns(closes)
    # Ascending: [100, 0, 110]; pairs (100,0) drops, (0,110) drops → empty
    assert rets == []


# ---------------------------------------------------------------------------
# _correlation
# ---------------------------------------------------------------------------


def test_correlation_perfect_positive():
    a = [i * 0.01 for i in range(40)]
    b = [i * 0.01 for i in range(40)]
    c = _correlation(a, b)
    assert c == pytest.approx(1.0, abs=1e-6)


def test_correlation_perfect_negative():
    a = [i * 0.01 for i in range(40)]
    b = [-i * 0.01 for i in range(40)]
    c = _correlation(a, b)
    assert c == pytest.approx(-1.0, abs=1e-6)


def test_correlation_below_min_observations_returns_none():
    a = list(range(10))
    b = list(range(10))
    assert _correlation(a, b) is None


def test_correlation_zero_variance_returns_none():
    a = [0.01] * 40
    b = list(range(40))
    assert _correlation(a, b) is None


# ---------------------------------------------------------------------------
# effective_independent_bets
# ---------------------------------------------------------------------------


def test_effective_bets_equal_weight_uncorrelated():
    weights = {"A": 0.25, "B": 0.25, "C": 0.25, "D": 0.25}
    # All pairwise zero correlation → effective = 1 / sum(0.25^2) = 4
    effective = effective_independent_bets(weights, corr={})
    assert effective == pytest.approx(4.0, rel=1e-6)


def test_effective_bets_perfect_correlation_collapses_to_one():
    weights = {"A": 0.5, "B": 0.5}
    corr = {("A", "B"): 1.0}
    effective = effective_independent_bets(weights, corr)
    assert effective == pytest.approx(1.0, rel=1e-6)


def test_effective_bets_empty():
    assert effective_independent_bets({}, {}) == 0.0


# ---------------------------------------------------------------------------
# build_pair_flags
# ---------------------------------------------------------------------------


def test_pair_flag_triggered_when_above_thresholds():
    flags = build_pair_flags(
        weights={"QQQ": 0.30, "QLD": 0.20, "GLD": 0.10},
        corr={("QQQ", "QLD"): 0.95, ("QQQ", "GLD"): -0.10, ("QLD", "GLD"): -0.05},
    )
    assert len(flags) == 1
    assert flags[0]["pair"] == ["QQQ", "QLD"]
    assert flags[0]["correlation"] == 0.95


def test_pair_flag_skipped_when_combined_weight_too_low():
    flags = build_pair_flags(
        weights={"A": 0.10, "B": 0.10},
        corr={("A", "B"): 0.99},
    )
    assert flags == []


def test_pair_flag_skipped_when_corr_below_threshold():
    flags = build_pair_flags(
        weights={"A": 0.50, "B": 0.50},
        corr={("A", "B"): 0.50},
    )
    assert flags == []


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


def test_plan_envelope_and_overall_flag():
    plan = build_plan(
        weights={"A": 0.5, "B": 0.5},
        corr={("A", "B"): 0.95},
        coverage={"A": 60, "B": 60},
        status="ok",
        notes=[],
    )
    assert plan["observe_only"] is True
    assert plan["schema_version"] == "1"
    assert plan["status"] == "ok"
    # Effective bets ≈ 1 / (0.5^2 + 0.5^2 + 2*0.5*0.5*0.95) ≈ 1.026
    assert plan["effective_independent_bets"] < 2.0
    assert "low_effective_independent_bets" in plan["overall_flags"]
    assert len(plan["high_correlation_pairs"]) == 1


def test_plan_no_low_diversification_flag_when_well_diversified():
    plan = build_plan(
        weights={f"S{i}": 0.20 for i in range(5)},
        corr={},
        coverage={f"S{i}": 60 for i in range(5)},
        status="ok",
        notes=[],
    )
    # effective ≈ 5
    assert plan["effective_independent_bets"] >= 4.0
    assert "low_effective_independent_bets" not in plan["overall_flags"]


# ---------------------------------------------------------------------------
# run_correlation_risk_advisor — integration
# ---------------------------------------------------------------------------


def _write_config(path: Path, holdings: list[dict]) -> None:
    path.write_text(
        json.dumps({"portfolio": {"holdings": holdings}}, indent=2),
        encoding="utf-8",
    )


def test_run_with_empty_holdings(tmp_path):
    _write_config(tmp_path / "config.json", [])
    plan = run_correlation_risk_advisor(
        tmp_path, fmp_client=None, base_dir=tmp_path / "outputs"
    )
    assert plan["observe_only"] is True
    assert plan["status"] == "insufficient_data"


def test_run_without_fmp_reports_insufficient_data(tmp_path):
    _write_config(
        tmp_path / "config.json",
        [
            {"symbol": "QQQ", "shares": 6, "target_weight": 0.5},
            {"symbol": "GLD", "shares": 4, "target_weight": 0.5},
        ],
    )
    plan = run_correlation_risk_advisor(
        tmp_path, fmp_client=None, base_dir=tmp_path / "outputs"
    )
    assert plan["status"] == "insufficient_data"
    out_json = tmp_path / "outputs" / "latest" / "correlation_risk_advisor.json"
    assert out_json.exists()
    payload = json.loads(out_json.read_text("utf-8"))
    assert payload["observe_only"] is True


def test_run_with_stub_fmp_computes_correlation(tmp_path):
    _write_config(
        tmp_path / "config.json",
        [
            {"symbol": "QQQ", "shares": 6, "target_weight": 0.5},
            {"symbol": "QLD", "shares": 8, "target_weight": 0.5},
        ],
    )

    # Build identical price series → correlation should be 1.0
    series = [{"date": f"2026-01-{i:02d}", "close": 100.0 + i * 0.5,
               "adjClose": 100.0 + i * 0.5}
              for i in range(60)]
    series.reverse()  # newest-first like FMP

    class StubFMP:
        def get_historical_prices(self, symbol, *, years=1, ttl_days=1):
            return series

    plan = run_correlation_risk_advisor(
        tmp_path, fmp_client=StubFMP(), base_dir=tmp_path / "outputs"
    )
    assert plan["status"] == "ok"
    pair_flags = plan["high_correlation_pairs"]
    assert len(pair_flags) == 1
    assert pair_flags[0]["correlation"] == pytest.approx(1.0, abs=0.01)
    # Effective bets ~1.0 because perfect correlation
    assert plan["effective_independent_bets"] < 1.5


def test_skips_holdings_with_zero_shares(tmp_path):
    _write_config(
        tmp_path / "config.json",
        [
            {"symbol": "QQQ", "shares": 6, "target_weight": 0.5},
            {"symbol": "VFH", "shares": 0, "target_weight": 0.15},  # not yet bought
        ],
    )
    plan = run_correlation_risk_advisor(
        tmp_path, fmp_client=None, base_dir=tmp_path / "outputs"
    )
    assert list(plan["weights"].keys()) == ["QQQ"]


def test_artifact_observe_only_field_is_hardcoded(tmp_path):
    _write_config(
        tmp_path / "config.json",
        [{"symbol": "QQQ", "shares": 6, "target_weight": 0.5}],
    )
    run_correlation_risk_advisor(tmp_path, fmp_client=None, base_dir=tmp_path / "outputs")
    payload = json.loads(
        (tmp_path / "outputs" / "latest" / "correlation_risk_advisor.json")
        .read_text("utf-8")
    )
    assert payload["observe_only"] is True


def test_fmp_failure_is_non_fatal(tmp_path):
    _write_config(
        tmp_path / "config.json",
        [
            {"symbol": "QQQ", "shares": 6, "target_weight": 0.5},
            {"symbol": "GLD", "shares": 4, "target_weight": 0.5},
        ],
    )

    class BrokenFMP:
        def get_historical_prices(self, symbol, *, years=1, ttl_days=1):
            raise RuntimeError("simulated")

    plan = run_correlation_risk_advisor(
        tmp_path, fmp_client=BrokenFMP(), base_dir=tmp_path / "outputs"
    )
    assert plan["observe_only"] is True
    assert plan["status"] == "insufficient_data"


def test_weights_are_normalized(tmp_path):
    _write_config(
        tmp_path / "config.json",
        [
            {"symbol": "A", "shares": 1, "target_weight": 0.30},
            {"symbol": "B", "shares": 1, "target_weight": 0.50},
            # Sum=0.80 not 1.0
        ],
    )
    plan = run_correlation_risk_advisor(
        tmp_path, fmp_client=None, base_dir=tmp_path / "outputs"
    )
    total = sum(plan["weights"].values())
    assert total == pytest.approx(1.0, abs=1e-6)
