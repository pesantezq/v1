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
