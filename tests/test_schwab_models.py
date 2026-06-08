from portfolio_automation.brokers import broker_models as bm


def test_mask_account_keeps_last4():
    assert bm.mask_account("123456789") == "…6789"  # last 4
    assert bm.mask_account("") == "…"
    assert bm.mask_account(None) == "…"


def test_redact_scrubs_tokens_and_secrets():
    s = "access_token=abc123 refresh_token=zzz client_secret=shh code=qqq ok"
    out = bm.redact(s)
    for leak in ("abc123", "zzz", "shh", "qqq"):
        assert leak not in out
    assert "ok" in out


def test_redact_handles_non_string():
    assert bm.redact(None) == ""
    assert "5" in bm.redact(5)


def test_redact_scrubs_bearer_authorization_header():
    out = bm.redact("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out and "ghi" not in out


def test_redact_scrubs_json_and_dict_repr_quoted_keys():
    assert "shh" not in bm.redact('"client_secret": "shh"')
    # use a value that is NOT a substring of any key name to avoid false substring matches
    assert "s3cr3t" not in bm.redact(str({"access_token": "s3cr3t"}))
    both = bm.redact('{"client_secret": "shh", "access_token": "s3cr3t"}')
    assert "shh" not in both and "s3cr3t" not in both


def test_redact_preserves_non_secret_text():
    # the existing space-separated case must still preserve trailing 'ok'
    out = bm.redact("access_token=abc123 refresh_token=zzz client_secret=shh code=qqq ok")
    for leak in ("abc123", "zzz", "shh", "qqq"):
        assert leak not in out
    assert "ok" in out


import json
from pathlib import Path

_FIX = Path("tests/fixtures/schwab")


def test_normalize_accounts_from_fixture():
    raw = json.loads((_FIX / "accounts_positions.json").read_text())
    nums = json.loads((_FIX / "account_numbers.json").read_text())
    snap = bm.normalize_accounts(raw, nums, now_iso="2026-06-08T12:00:00+00:00")
    assert len(snap.accounts) == 1
    acct = snap.accounts[0]
    assert acct.account_id_masked == "…6789"   # masked, no full number
    assert acct.account_type == "MARGIN"
    assert acct.total_market_value == 15000.50
    assert acct.cash == 464.16
    assert {p.symbol for p in acct.positions} == {"QQQ", "GLD"}
    qqq = next(p for p in acct.positions if p.symbol == "QQQ")
    assert qqq.quantity == 6 and qqq.market_value == 4200.0 and qqq.average_cost == 600.0
    assert qqq.account_ref_masked == "…6789"


def test_snapshot_and_positions_dicts_have_no_raw_account():
    raw = json.loads((_FIX / "accounts_positions.json").read_text())
    nums = json.loads((_FIX / "account_numbers.json").read_text())
    snap = bm.normalize_accounts(raw, nums, now_iso="2026-06-08T12:00:00+00:00")
    sd = bm.snapshot_dict(snap)
    pr = bm.positions_dict(snap)
    blob = json.dumps(sd) + json.dumps(pr)
    assert "123456789" not in blob           # no full account number leaks
    assert sd["totals"]["market_value"] == 15000.50
    assert len(pr["positions"]) == 2


def test_normalize_is_defensive_on_missing_fields():
    snap = bm.normalize_accounts([{"securitiesAccount": {}}], [], now_iso="t")
    assert len(snap.accounts) == 1
    assert snap.accounts[0].positions == []
