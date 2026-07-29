"""
Tests for portfolio_automation/risk_delta_advisor.py.

The advisor is a thin compose-of-pure-functions producer. Tests focus on:

  - VaR math is correct on a known sigma + portfolio value pair
  - Concentration classification respects the breach/near_cap/ok ladder
  - Leverage aggregation handles factor + weight correctly
  - Degraded modes return safe dicts (no exceptions, available=False)
  - observe_only invariant is hardcoded
  - run_risk_delta_advisor never mutates decision/score artifacts
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.risk_delta_advisor import (
    _classify_headroom,
    build_risk_delta,
    compute_concentration,
    compute_leverage,
    compute_var,
    render_risk_delta_md,
    run_risk_delta_advisor,
    _VAR_95_Z,
    _TRADING_DAYS_PER_YEAR,
)


class TestClassifyHeadroom(unittest.TestCase):
    def test_breach_when_headroom_zero_or_negative(self):
        self.assertEqual(_classify_headroom(0.0), "breach")
        self.assertEqual(_classify_headroom(-0.05), "breach")

    def test_near_cap_inside_5pp(self):
        self.assertEqual(_classify_headroom(0.01), "near_cap")
        self.assertEqual(_classify_headroom(0.05), "near_cap")

    def test_ok_when_headroom_more_than_5pp(self):
        self.assertEqual(_classify_headroom(0.06), "ok")
        self.assertEqual(_classify_headroom(0.20), "ok")


class TestComputeConcentration(unittest.TestCase):
    def test_top_position_ranked_first(self):
        holdings = [
            {"symbol": "A", "shares": 1.0, "target_weight": 0.10},
            {"symbol": "B", "shares": 1.0, "target_weight": 0.50},
            {"symbol": "C", "shares": 1.0, "target_weight": 0.30},
        ]
        result = compute_concentration(holdings, portfolio_value=100.0, cap=0.60)
        self.assertTrue(result["available"])
        self.assertEqual(result["top_position"]["symbol"], "B")
        self.assertAlmostEqual(result["top_position"]["weight"], 0.50, places=4)

    def test_quotes_override_target_weight(self):
        holdings = [{"symbol": "X", "shares": 5.0, "target_weight": 0.20}]
        # 5 shares × $20 = $100 → 100% of a $100 portfolio (overrides 20% target).
        result = compute_concentration(
            holdings, portfolio_value=100.0, cap=0.60, quotes={"X": 20.0}
        )
        self.assertAlmostEqual(result["top_position"]["weight"], 1.00, places=4)
        self.assertEqual(result["top_position"]["status"], "breach")

    def test_target_weight_fallback_when_no_quote(self):
        """Degraded mode: shares UNKNOWN and no quote → target_weight.

        Updated 2026-07-29. This test previously used ``shares: 0.0`` as the
        vehicle for "unpriceable", and that conflation was the defect: an
        explicit zero means the position is NOT HELD, so rendering its
        target_weight asserted a holding the operator does not own (VFH showed
        15% and VXUS 10% on the live portfolio). The degraded-mode intent is
        preserved here with the correct vehicle — shares absent entirely — and
        the shares==0 case is pinned separately as ``not_held`` below.
        """
        holdings = [{"symbol": "Y", "target_weight": 0.30}]
        result = compute_concentration(holdings, portfolio_value=100.0, cap=0.60)
        self.assertAlmostEqual(result["top_position"]["weight"], 0.30, places=4)
        self.assertEqual(result["top_position"]["price_source"], "target_weight_fallback")

    def test_explicit_zero_shares_is_not_held(self):
        holdings = [{"symbol": "Y", "shares": 0.0, "target_weight": 0.30}]
        result = compute_concentration(holdings, portfolio_value=100.0, cap=0.60)
        self.assertAlmostEqual(result["top_position"]["weight"], 0.0, places=4)
        self.assertEqual(result["top_position"]["price_source"], "not_held")

    def test_breach_count_and_near_cap_count(self):
        holdings = [
            {"symbol": "A", "shares": 1.0, "target_weight": 0.65},  # breach (cap 0.60)
            {"symbol": "B", "shares": 1.0, "target_weight": 0.58},  # near_cap
            {"symbol": "C", "shares": 1.0, "target_weight": 0.10},  # ok
        ]
        result = compute_concentration(holdings, portfolio_value=100.0, cap=0.60)
        self.assertEqual(result["breach_count"], 1)
        self.assertEqual(result["near_cap_count"], 1)

    def test_no_holdings_unavailable(self):
        self.assertFalse(compute_concentration([], 100.0, 0.60)["available"])

    def test_zero_portfolio_value_unavailable(self):
        self.assertFalse(
            compute_concentration(
                [{"symbol": "X", "shares": 1.0}], 0.0, 0.60
            )["available"]
        )


class TestComputeLeverage(unittest.TestCase):
    def test_aggregates_leveraged_positions_only(self):
        holdings = [
            {"symbol": "QQQ", "shares": 1.0, "target_weight": 0.40, "is_leveraged": False},
            {"symbol": "QLD", "shares": 1.0, "target_weight": 0.05,
             "is_leveraged": True, "leverage_factor": 2},
            {"symbol": "TQQQ", "shares": 1.0, "target_weight": 0.05,
             "is_leveraged": True, "leverage_factor": 3},
        ]
        result = compute_leverage(holdings, portfolio_value=100.0, cap=0.25)
        # 0.05 × 2 + 0.05 × 3 = 0.25 exposure → exactly at cap → breach (<=0)
        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["total_exposure"], 0.25, places=4)
        self.assertAlmostEqual(result["headroom"], 0.0, places=4)
        self.assertEqual(result["status"], "breach")
        self.assertEqual(len(result["leveraged_positions"]), 2)

    def test_no_leveraged_holdings_zero_exposure(self):
        holdings = [{"symbol": "QQQ", "shares": 1.0, "target_weight": 0.40}]
        result = compute_leverage(holdings, portfolio_value=100.0, cap=0.25)
        self.assertAlmostEqual(result["total_exposure"], 0.0, places=4)
        self.assertEqual(result["leveraged_positions"], [])
        self.assertEqual(result["status"], "ok")

    def test_zero_portfolio_unavailable(self):
        self.assertFalse(
            compute_leverage(
                [{"symbol": "X", "is_leveraged": True}], 0.0, 0.25
            )["available"]
        )


class TestComputeVaR(unittest.TestCase):
    def test_known_value_at_15pct_annual_vol(self):
        # Sanity check the math: 15% annual vol, $10k portfolio, 1-day 95% VaR
        # daily_sigma = 0.15 / sqrt(252) ≈ 0.00945
        # var_pct = 1.645 × 0.00945 ≈ 0.01554
        # var_dollar ≈ $155.40
        result = compute_var(portfolio_value=10_000.0, sigma_annual=0.15)
        expected_daily_sigma = 0.15 / math.sqrt(_TRADING_DAYS_PER_YEAR)
        expected_var_pct = _VAR_95_Z * expected_daily_sigma
        self.assertAlmostEqual(result["sigma_daily"], expected_daily_sigma, places=4)
        self.assertAlmostEqual(result["var_pct"], expected_var_pct, places=4)
        self.assertAlmostEqual(
            result["var_dollar"], expected_var_pct * 10_000.0, places=1
        )

    def test_horizon_scaling_uses_square_root_of_time(self):
        # VaR over 4 days = 2 × 1-day VaR (sqrt(4) = 2). Artifact rounds to
        # 4 decimal places, so we allow ~1% slack on the ratio.
        one_day = compute_var(10_000.0, 0.15, horizon_days=1)
        four_day = compute_var(10_000.0, 0.15, horizon_days=4)
        ratio = four_day["var_pct"] / one_day["var_pct"]
        self.assertGreater(ratio, 1.98)
        self.assertLess(ratio, 2.02)

    def test_zero_sigma_unavailable(self):
        self.assertFalse(compute_var(10_000.0, 0.0)["available"])

    def test_none_sigma_unavailable(self):
        self.assertFalse(compute_var(10_000.0, None)["available"])

    def test_zero_portfolio_unavailable(self):
        self.assertFalse(compute_var(0.0, 0.15)["available"])


class TestBuildRiskDelta(unittest.TestCase):
    def _holdings(self):
        return [
            {"symbol": "QQQ", "shares": 1.0, "target_weight": 0.55},
            {"symbol": "QLD", "shares": 1.0, "target_weight": 0.10,
             "is_leveraged": True, "leverage_factor": 2},
            {"symbol": "GLD", "shares": 1.0, "target_weight": 0.20},
        ]

    def test_full_artifact_shape(self):
        payload = build_risk_delta(
            holdings=self._holdings(),
            portfolio_value=10_000.0,
            concentration_cap=0.60,
            leverage_cap=0.25,
            sigma_annual=0.15,
        )
        self.assertTrue(payload["observe_only"])
        self.assertEqual(payload["schema_version"], "1")
        self.assertEqual(payload["source"], "risk_delta_advisor")
        self.assertIn("concentration", payload)
        self.assertIn("leverage", payload)
        self.assertIn("var", payload)

    def test_overall_status_picks_worst_subsection(self):
        # Force a breach: concentration cap 0.50, QQQ at 0.55 → breach
        payload = build_risk_delta(
            holdings=self._holdings(),
            portfolio_value=10_000.0,
            concentration_cap=0.50,
            leverage_cap=0.25,
            sigma_annual=0.15,
        )
        self.assertEqual(payload["overall_status"], "breach")

    def test_observe_only_invariant_in_artifact(self):
        payload = build_risk_delta(
            holdings=self._holdings(),
            portfolio_value=10_000.0,
            concentration_cap=0.60,
            leverage_cap=0.25,
            sigma_annual=0.15,
        )
        self.assertIs(payload["observe_only"], True)


class TestRenderRiskDeltaMd(unittest.TestCase):
    def test_renders_three_sections(self):
        payload = build_risk_delta(
            holdings=[{"symbol": "QQQ", "shares": 1.0, "target_weight": 0.55}],
            portfolio_value=10_000.0,
            concentration_cap=0.60,
            leverage_cap=0.25,
            sigma_annual=0.15,
        )
        md = render_risk_delta_md(payload)
        self.assertIn("Concentration vs 60% cap", md)
        self.assertIn("Leverage vs 25% cap", md)
        self.assertIn("1-day 95% Value-at-Risk", md)
        self.assertIn("Risk Delta Panel", md)

    def test_renders_disclaimer(self):
        payload = build_risk_delta(
            holdings=[{"symbol": "QQQ", "shares": 1.0, "target_weight": 0.55}],
            portfolio_value=10_000.0,
            concentration_cap=0.60,
            leverage_cap=0.25,
            sigma_annual=0.15,
        )
        md = render_risk_delta_md(payload)
        self.assertIn("Observe-only", md)

    def test_headroom_rendered_in_pp_not_percent(self):
        # headroom is a percentage-point delta; the canonical convention (and the
        # daily memo) label it `pp`, not `%`. QQQ at 55% under a 60% cap -> +5.0pp
        # concentration headroom; unleveraged -> +25.0pp leverage headroom.
        payload = build_risk_delta(
            holdings=[{"symbol": "QQQ", "shares": 1.0, "target_weight": 0.55}],
            portfolio_value=10_000.0,
            concentration_cap=0.60,
            leverage_cap=0.25,
            sigma_annual=0.15,
        )
        md = render_risk_delta_md(payload)
        self.assertIn("headroom +5.0pp", md)   # concentration top position
        self.assertIn("headroom +25.0pp", md)  # leverage
        # the same headroom values must NOT be labeled with a percent sign
        self.assertNotIn("headroom +5.0%", md)
        self.assertNotIn("headroom +25.0%", md)


class TestRunRiskDeltaAdvisor(unittest.TestCase):
    """Integration test: feed a temp repo, ensure the advisor writes valid artifacts."""

    def test_writes_both_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.json").write_text(json.dumps({
                "portfolio": {
                    "holdings": [
                        {"symbol": "QQQ", "shares": 1, "target_weight": 0.50,
                         "is_leveraged": False},
                    ]
                },
                "growth_mode": {
                    "concentration_cap": 0.60,
                    "leverage_cap": 0.25,
                },
            }))
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "decision_plan.json").write_text(json.dumps({
                "portfolio_context": {"total_portfolio_value": 10_000.0},
            }))
            (root / "outputs" / "latest" / "vol_regime_advisor.json").write_text(json.dumps({
                "sigma_annual": 0.15,
            }))

            result = run_risk_delta_advisor(root=root)
            self.assertEqual(result["status"], "ok")
            self.assertTrue((root / "outputs" / "latest" / "risk_delta.json").exists())
            self.assertTrue((root / "outputs" / "latest" / "risk_delta.md").exists())

            payload = json.loads(
                (root / "outputs" / "latest" / "risk_delta.json").read_text()
            )
            self.assertTrue(payload["observe_only"])
            self.assertEqual(payload["source"], "risk_delta_advisor")

    def test_does_not_mutate_decision_plan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.json").write_text(json.dumps({"portfolio": {"holdings": []}}))
            (root / "outputs" / "latest").mkdir(parents=True)
            plan_path = root / "outputs" / "latest" / "decision_plan.json"
            original = {"portfolio_context": {"total_portfolio_value": 5000.0},
                        "decisions": [{"symbol": "X", "decision": "BUY"}]}
            plan_path.write_text(json.dumps(original))

            run_risk_delta_advisor(root=root)

            after = json.loads(plan_path.read_text())
            self.assertEqual(after, original)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ── Stale-price / not-held weight integrity (2026-07-29) ─────────────────────
#
# Defect found by the memo-reviewer: every operator-facing concentration and
# leverage figure was computed from data/price_cache.json, whose entries were
# dated 2026-06-12 — 47 days stale — with no freshness check anywhere.
#
# On the live 2026-07-29 portfolio that produced:
#   * rendered position weights summing to 107.1%
#   * QQQ 50.2% vs 47.35% actual
#   * VFH 15.0% and VXUS 10.0% rendered although shares == 0 (not held), because
#     shares<=0 silently fell back to target_weight
#   * genuinely held LCID (50 sh, $392, 3.92%) rendered 0.0%, because it is absent
#     from the price cache and its target_weight is 0.0
#   * VFH/VXUS labelled price_source "live_quote" — the label was derived from
#     `price is not None` alone, so it described the quote lookup, not the number
#     actually used
#
# No cap status flipped (headroom is wide), which is exactly why nothing else
# caught it. schwab_positions.json carries authoritative per-position
# market_value and is refreshed by Stage 0b ahead of the decision run, so it is
# the correct primary source.

import json as _json
from pathlib import Path as _Path

import pytest as _pytest

from portfolio_automation.risk_delta_advisor import compute_concentration

_PV = 9989.54


def _h(symbol, shares, target_weight=None):
    d = {"symbol": symbol, "shares": shares}
    if target_weight is not None:
        d["target_weight"] = target_weight
    return d


def _by_symbol(result):
    return {r["symbol"]: r for r in result["positions"]}


def test_broker_market_value_is_preferred_over_quote_math():
    """market_value is authoritative; shares x price is a reconstruction."""
    res = compute_concentration(
        [_h("QQQ", 7.0, 0.35)], _PV, 0.60,
        quotes={"QQQ": 717.12},                 # stale price
        market_values={"QQQ": 4729.97},         # broker truth
    )
    row = _by_symbol(res)["QQQ"]
    assert row["weight"] == _pytest.approx(0.4735, abs=1e-4)
    assert row["price_source"] == "broker_market_value"


def test_zero_share_position_is_not_held_not_its_target_weight():
    """VFH/VXUS: shares == 0 is KNOWN, so current weight is 0 — rendering the
    target weight asserts a position the operator does not own."""
    res = compute_concentration(
        [_h("VFH", 0.0, 0.15), _h("VXUS", 0.0, 0.10)], _PV, 0.60,
        quotes={"VFH": 129.24, "VXUS": 65.0},
        market_values={},
    )
    rows = _by_symbol(res)
    for sym in ("VFH", "VXUS"):
        assert rows[sym]["weight"] == 0.0, f"{sym} is not held; weight must be 0"
        assert rows[sym]["price_source"] == "not_held"


def test_stale_quotes_are_rejected_and_do_not_price_a_position():
    """A quote older than the freshness window must not be used at all."""
    res = compute_concentration(
        [_h("NASA", 15.0, 0.10)], _PV, 0.60,
        quotes={},                    # loader dropped the stale entry
        market_values={},
    )
    row = _by_symbol(res)["NASA"]
    assert row["price_source"] == "target_weight_fallback"
    assert row["weight"] == _pytest.approx(0.10, abs=1e-6)


def test_price_source_names_the_number_actually_used():
    """The old label came from `price is not None`, so a target_weight fallback
    taken because shares<=0 was still stamped live_quote."""
    res = compute_concentration(
        [_h("QQQ", 7.0, 0.35), _h("VFH", 0.0, 0.15), _h("ZZZ", 3.0, 0.07)],
        _PV, 0.60,
        quotes={"QQQ": 675.49, "VFH": 129.24},
        market_values={},
    )
    rows = _by_symbol(res)
    assert rows["QQQ"]["price_source"] == "live_quote"
    assert rows["VFH"]["price_source"] == "not_held"
    assert rows["ZZZ"]["price_source"] == "target_weight_fallback"


def test_weights_sum_diagnostic_is_reported():
    res = compute_concentration(
        [_h("QQQ", 7.0, 0.35), _h("GLD", 4.0, 0.20)], _PV, 0.60,
        quotes={}, market_values={"QQQ": 4729.97, "GLD": 1481.32},
    )
    expected = (4729.97 + 1481.32) / _PV
    assert res["weights_sum"] == _pytest.approx(round(expected, 4), abs=1e-4)
    assert res["weights_sum_exceeds_100"] is False


def test_impossible_weight_sum_is_flagged_not_silently_rendered():
    """The 107.1% case must surface a flag rather than read as fact."""
    res = compute_concentration(
        [_h("A", None, 0.60), _h("B", None, 0.60)], _PV, 0.95,
        quotes={}, market_values={},
    )
    assert res["weights_sum"] > 1.0
    assert res["weights_sum_exceeds_100"] is True


def test_live_2026_07_29_shape_reconciles_to_broker_truth():
    """Regression lock on the exact portfolio that exposed this."""
    holdings = [
        _h("NASA", 15.0, 0.10), _h("CHAT", 4.0, 0.05), _h("QQQ", 7.0, 0.35),
        _h("LCID", 50.0, 0.0), _h("GLD", 4.0, 0.20), _h("QLD", 8.0, 0.05),
        _h("VFH", 0.0, 0.15), _h("VXUS", 0.0, 0.10),
    ]
    market_values = {"NASA": 325.95, "CHAT": 312.76, "QQQ": 4729.97,
                     "LCID": 392.0, "GLD": 1481.32, "QLD": 647.36}
    res = compute_concentration(holdings, _PV, 0.60, quotes={},
                                market_values=market_values)
    rows = _by_symbol(res)
    assert rows["QQQ"]["weight"] == _pytest.approx(0.4735, abs=1e-4)
    assert rows["LCID"]["weight"] == _pytest.approx(0.0392, abs=1e-4), \
        "held LCID must no longer render 0.0%"
    assert rows["VFH"]["weight"] == 0.0 and rows["VXUS"]["weight"] == 0.0
    assert res["weights_sum"] <= 1.0
    assert res["weights_sum_exceeds_100"] is False
    assert res["top_position"]["symbol"] == "QQQ"
    assert res["top_3_total"] == _pytest.approx(0.6870, abs=1e-3), \
        "top-3 was 80.7% off stale prices; broker truth is 68.7%"


def test_quote_loader_rejects_stale_cache_entries(tmp_path):
    from portfolio_automation.risk_delta_advisor import _load_quotes
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "price_cache.json").write_text(_json.dumps({
        "FRESH": {"price": 10.0, "timestamp": "2026-07-29T13:00:00"},
        "STALE": {"price": 20.0, "timestamp": "2026-06-12T13:04:26"},
        "NOTS":  {"price": 30.0},
    }), encoding="utf-8")
    quotes = _load_quotes(_Path(tmp_path), now_iso="2026-07-29T15:00:00+00:00")
    assert "FRESH" in quotes
    assert "STALE" not in quotes, "a 47-day-old quote must not price a position"
    assert "NOTS" not in quotes, "an undated entry cannot be proven fresh"


def test_md_surfaces_weight_sum_and_degraded_sources():
    """A weight derived from a fallback must be visibly marked, and an impossible
    total must be stated — the 107.1% sum rendered as fact because the Markdown
    showed only the percentages."""
    from portfolio_automation.risk_delta_advisor import (
        build_risk_delta, render_risk_delta_md,
    )
    payload = build_risk_delta(
        holdings=[
            {"symbol": "QQQ", "shares": 7.0, "target_weight": 0.35},
            {"symbol": "VFH", "shares": 0.0, "target_weight": 0.15},
            {"symbol": "ZZZ", "target_weight": 0.07},
        ],
        portfolio_value=_PV, concentration_cap=0.60, leverage_cap=0.25,
        sigma_annual=0.0946, quotes={}, market_values={"QQQ": 4729.97},
    )
    md = render_risk_delta_md(payload)
    assert "Weights sum" in md
    assert "not held" in md.lower()
    assert "target-weight estimate" in md.lower()
    # A clean total must not be shouted about.
    assert "EXCEEDS 100%" not in md


def test_md_flags_an_impossible_weight_total():
    from portfolio_automation.risk_delta_advisor import (
        build_risk_delta, render_risk_delta_md,
    )
    payload = build_risk_delta(
        holdings=[{"symbol": "A", "target_weight": 0.60},
                  {"symbol": "B", "target_weight": 0.60}],
        portfolio_value=_PV, concentration_cap=0.95, leverage_cap=0.25,
        sigma_annual=0.0946, quotes={}, market_values={},
    )
    md = render_risk_delta_md(payload)
    assert "EXCEEDS 100%" in md
