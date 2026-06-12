from portfolio_automation.brokers import schwab_tax_lots as tl


def test_normalize_lots_present():
    raw = {"positions": [
        {"symbol": "AAA", "taxLots": [
            {"quantity": 4, "costBasis": 400.0, "acquiredDate": "2024-01-10"},
            {"quantity": 6, "costBasis": 660.0, "acquiredDate": "2026-05-01"}]}]}
    out = tl.normalize_tax_lots(raw, now_iso="2026-06-12T00:00:00+00:00")
    assert out["observe_only"] is True and out["no_trade"] is True
    assert out["has_lots"] is True
    lots = out["by_symbol"]["AAA"]
    assert len(lots) == 2
    assert lots[0]["acquired_date"] == "2024-01-10" and lots[0]["cost_basis"] == 400.0


def test_normalize_no_lots_marker():
    raw = {"positions": [{"symbol": "AAA", "average_cost": 100.0}]}
    out = tl.normalize_tax_lots(raw, now_iso="2026-06-12T00:00:00+00:00")
    assert out["has_lots"] is False and out["by_symbol"] == {}
    assert "no per-lot" in out["reason"].lower()


def test_normalize_handles_garbage():
    assert tl.normalize_tax_lots(None, now_iso="t")["has_lots"] is False
