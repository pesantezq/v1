from gui_v2.data import dash_strategy_tax as d


def test_loader_degrades_gracefully(tmp_path):
    ctx = d.load_strategy_tax_context(base_dir=tmp_path)
    assert ctx["available"] is False
    assert "scorecard" in ctx and "harvest" in ctx and "strategy" in ctx
