from portfolio_automation.strategy import tax_scorecard as ts

_POS = {"positions": [
    {"symbol": "AAA", "quantity": 10, "average_cost": 100.0, "market_value": 1500.0},
    {"symbol": "BBB", "quantity": 5, "average_cost": 200.0, "market_value": 800.0},
]}


def test_computes_unrealized_gl_from_avg_cost():
    out = ts.build_tax_scorecard("2026-06-12T00:00:00+00:00", _POS)
    assert out["degraded_mode"] is False
    by = {c["symbol"]: c for c in out["scorecards"]}
    assert by["AAA"]["unrealized_gain"] == 500.0 and by["AAA"]["tlh_candidate"] is False
    assert by["BBB"]["unrealized_gain"] == -200.0 and by["BBB"]["tlh_candidate"] is True
    assert out["portfolio_unrealized_gain"] == 300.0


def test_lot_fields_degraded_without_lots():
    out = ts.build_tax_scorecard("t", _POS)
    assert "short_term_vs_long_term" in out["degraded_fields"]
    assert "wash_sale_window" in out["degraded_fields"]


def test_lot_fields_live_with_lots():
    lots = {"AAA": [{"quantity": 10, "cost_basis": 1000.0, "acquired_date": "2024-01-01"}]}
    out = ts.build_tax_scorecard("2026-06-12T00:00:00+00:00", _POS, tax_lots=lots)
    assert "short_term_vs_long_term" not in out["degraded_fields"]
    by = {c["symbol"]: c for c in out["scorecards"]}
    assert by["AAA"]["holding_period"] == "long"


def test_no_positions_degraded():
    out = ts.build_tax_scorecard("t", {"positions": []})
    assert out["degraded_mode"] is True
