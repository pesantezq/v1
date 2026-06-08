from portfolio_automation.brokers import broker_status as bs


def test_status_unconfigured():
    st = bs.build_status(enabled=True, configured=False, authenticated=False,
                         account_count=0, position_count=0, last_success_at=None,
                         last_error=None, now_iso="t")
    assert st["overall_status"] == "unconfigured"
    assert st["read_only_mode"] is True and st["trading_enabled"] is False
    assert st["observe_only"] is True and st["source"] == "schwab"


def test_status_ok_and_error_redacted():
    ok = bs.build_status(enabled=True, configured=True, authenticated=True,
                         account_count=1, position_count=2, last_success_at="t",
                         last_error=None, now_iso="t")
    assert ok["overall_status"] == "ok" and ok["account_count"] == 1
    err = bs.build_status(enabled=True, configured=True, authenticated=False,
                          account_count=0, position_count=0, last_success_at=None,
                          last_error="boom access_token=SEKRET", now_iso="t")
    assert err["overall_status"] == "error"
    assert "SEKRET" not in err["last_error"]   # redacted


def test_status_disabled():
    st = bs.build_status(enabled=False, configured=True, authenticated=False,
                         account_count=0, position_count=0, last_success_at=None,
                         last_error=None, now_iso="t")
    assert st["overall_status"] == "disabled"
