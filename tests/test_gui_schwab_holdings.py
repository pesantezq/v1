import json
from pathlib import Path
from gui_v2.data import dash_schwab_holdings as sh


def _write(tmp, positions):
    L = tmp / "outputs" / "latest"; L.mkdir(parents=True)
    (L / "schwab_positions.json").write_text(json.dumps({"positions": positions}))


def test_rows_computed(tmp_path):
    _write(tmp_path, [{"symbol": "AAA", "quantity": 10, "market_value": 1500.0, "average_cost": 100.0}])
    out = sh.schwab_holdings(tmp_path)
    assert out["available"] is True and out["observe_only"] is True
    r = out["rows"][0]
    assert r["symbol"] == "AAA" and r["cost_basis"] == 1000.0 and r["unrealized_gain"] == 500.0


def test_absent_graceful(tmp_path):
    out = sh.schwab_holdings(tmp_path)
    assert out["available"] is False and out["rows"] == []
