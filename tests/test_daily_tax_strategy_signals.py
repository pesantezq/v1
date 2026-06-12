def _amber_signals(scorecard, strategy, broker_ok, broker_aware_on):
    out = []
    if broker_aware_on and broker_ok and scorecard.get("degraded_mode"):
        out.append("tax_scorecard_unexpectedly_degraded")
    if broker_aware_on and broker_ok and strategy.get("context_source") == "config":
        out.append("strategy_context_not_broker")
    return out


def test_healthy_no_amber():
    assert _amber_signals({"degraded_mode": False}, {"context_source": "broker"}, True, True) == []


def test_degraded_raises_amber():
    s = _amber_signals({"degraded_mode": True}, {"context_source": "config"}, True, True)
    assert "tax_scorecard_unexpectedly_degraded" in s and "strategy_context_not_broker" in s


def test_inert_when_broker_aware_off():
    assert _amber_signals({"degraded_mode": True}, {"context_source": "config"}, True, False) == []
