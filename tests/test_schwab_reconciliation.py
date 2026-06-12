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


def test_validate_rejects_negative_and_missing_symbol():
    v = rec.validate_proposed_holdings(
        [{"symbol": "QQQ", "shares": -1}, {"symbol": "", "shares": 5}], -10.0, _CFG)
    assert v["ok"] is False
    joined = " ".join(v["errors"]).lower()
    assert "negative" in joined and ("symbol" in joined or "cash" in joined)


def test_build_proposal_is_proposal_only():
    r = rec.reconcile(_SNAP, _POS, _CFG)
    prop = rec.build_proposal(r, _CFG, now_iso="2026-06-08T12:00:00+00:00")
    assert prop["operator_approval_required"] is True
    assert prop["auto_applied"] is False
    assert "before" in prop and "proposed_after" in prop
    # proposed_after aligns GLD toward schwab qty (4)
    after = {h["symbol"]: h["shares"] for h in prop["proposed_after"]["holdings"]}
    assert after["GLD"] == 4
    assert prop["validation"]["ok"] is True
    assert "manual_portfolio_update" in prop["apply_instructions"]


def test_validate_flags_duplicate_and_target_weight_sum():
    # duplicate symbol must fail
    assert rec.validate_proposed_holdings(
        [{"symbol": "Q", "shares": 1}, {"symbol": "Q", "shares": 2}], 0, {})["ok"] is False
    # target weights that sum to >1.02 must fail
    assert rec.validate_proposed_holdings(
        [{"symbol": "A", "shares": 1, "target_weight": 0.5},
         {"symbol": "B", "shares": 1, "target_weight": 0.9}], 0, {})["ok"] is False


def test_build_proposal_retains_local_only_holdings():
    """missing_in_schwab symbols must NOT be dropped from proposed_after and
    the reason string must surface them for operator review."""
    r = rec.reconcile(_SNAP, _POS, _CFG)
    # NASA is missing_in_schwab (local-only)
    assert any(m["symbol"] == "NASA" for m in r["missing_in_schwab"])
    prop = rec.build_proposal(r, _CFG, now_iso="2026-06-08T12:00:00+00:00")
    after_syms = {h["symbol"] for h in prop["proposed_after"]["holdings"]}
    assert "NASA" in after_syms, "local-only symbol must be retained, not auto-removed"
    assert "NASA" in prop["reason"], "reason must surface retained local-only holdings"
    assert "operator review" in prop["reason"].lower()


def test_zero_share_config_target_not_flagged_missing_in_schwab():
    """A 0-share config entry is an allocation TARGET, not a holdings mismatch.
    Held symbols (>0 shares) absent from Schwab are still flagged."""
    snap = {"totals": {"market_value": 4200.0, "cash": 100.0}}
    pos = {"positions": [{"symbol": "QQQ", "quantity": 6, "market_value": 4200.0}]}
    cfg = {"portfolio": {"cash_available": 100.0, "holdings": [
        {"symbol": "QQQ", "shares": 6},
        {"symbol": "VFH", "shares": 0, "target_weight": 0.15},   # 0-share target — NOT a mismatch
        {"symbol": "OWNED", "shares": 3},                         # held, absent from Schwab — IS a mismatch
    ]}}
    r = rec.reconcile(snap, pos, cfg)
    miss = {m["symbol"] for m in r["missing_in_schwab"]}
    assert "VFH" not in miss        # 0-share target ignored
    assert "OWNED" in miss          # real held-but-absent flagged
