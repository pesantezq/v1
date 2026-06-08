import ast
from pathlib import Path
from portfolio_automation.brokers import schwab_client as cl


def test_no_trading_capability_anywhere_in_brokers_package():
    pkg = Path("portfolio_automation/brokers")
    forbidden = ("place_order", "submit_order", "buy", "sell", "execute_trade", "cancel_order")
    offenders = []
    for py in pkg.glob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name.lower()
                if any(f in name for f in forbidden) or name.startswith(("order", "trade")):
                    offenders.append(f"{py.name}:{node.name}")
    assert offenders == [], f"trading-capable functions present: {offenders}"


def test_client_has_only_read_methods():
    methods = [m for m in dir(cl.SchwabClient) if not m.startswith("_")]
    for m in methods:
        assert not any(k in m.lower() for k in ("order", "trade", "buy", "sell", "place")), m
    # has the read methods we expect
    assert "get_account_numbers" in methods and "get_accounts" in methods


def test_client_get_uses_bearer_and_returns_json(monkeypatch):
    captured = {}
    class _Resp:
        status_code = 200
        def json(self): return [{"ok": True}]
    def fake_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url; captured["headers"] = headers; return _Resp()
    monkeypatch.setattr(cl, "_requests_get", fake_get)
    c = cl.SchwabClient(access_token="TOK")
    out = c.get_accounts(positions=True)
    assert out == [{"ok": True}]
    assert captured["headers"]["Authorization"] == "Bearer TOK"
    assert "fields=positions" in captured["url"]
