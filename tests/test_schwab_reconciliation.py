from portfolio_automation.brokers import broker_reconciliation as rec

_SNAP = {"accounts": [{"cash": 464.16}], "totals": {"market_value": 5400.0, "cash": 464.16}}
_POS = {"positions": [
    {"symbol": "QQQ", "quantity": 6, "market_value": 4200.0},
    {"symbol": "GLD", "quantity": 4, "market_value": 1200.0},
]}
_CFG = {"portfolio": {"cash_available": 464.16, "holdings": [
    {"symbol": "QQQ", "shares": 6}, {"symbol": "GLD", "shares": 5}, {"symbol": "NASA", "shares": 14},
]}}


def test_reconcile_classifies():
    r = rec.reconcile(_SNAP, _POS, _CFG)
    matched = {m["symbol"] for m in r["matched"]}
    mism = {m["symbol"] for m in r["quantity_mismatches"]}
    miss_schwab = {m["symbol"] for m in r["missing_in_schwab"]}
    assert "QQQ" in matched
    assert "GLD" in mism                      # 4 vs 5
    assert "NASA" in miss_schwab              # local only
    assert r["missing_in_local"] == []        # nothing schwab-only here
    assert r["cash"]["delta"] == 0.0
    assert r["summary_status"] == "mismatch"
    assert "buy" not in r["operator_review_message"].lower()
    assert "sell" not in r["operator_review_message"].lower()


def test_reconcile_missing_in_local():
    pos = {"positions": [{"symbol": "TSLA", "quantity": 3}]}
    r = rec.reconcile(_SNAP, pos, {"portfolio": {"holdings": [], "cash_available": 0}})
    assert {m["symbol"] for m in r["missing_in_local"]} == {"TSLA"}
    assert r["summary_status"] in ("mismatch", "no_local_config")


def test_reconcile_no_broker_data():
    r = rec.reconcile({"totals": {}}, {"positions": []}, _CFG)
    assert r["summary_status"] == "no_broker_data"
