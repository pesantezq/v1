from watchlist_scanner import daily_memo as dm


def test_tax_strategy_line_renders():
    line = dm.render_tax_strategy_line(
        scorecard={"degraded_mode": False, "portfolio_unrealized_gain": 300.0, "degraded_fields": []},
        harvest={"basis_source": "broker", "harvestable_count": 1},
        strategy={"context_source": "broker"})
    assert "300" in line and "broker" in line.lower() and "1" in line


def test_tax_strategy_line_degraded():
    line = dm.render_tax_strategy_line(
        scorecard={"degraded_mode": True, "degraded_fields": ["unrealized_gain_loss"]},
        harvest={"basis_source": "config", "harvestable_count": 0},
        strategy={"context_source": "config"})
    assert "degraded" in line.lower() or "config" in line.lower()
