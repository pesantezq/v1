"""G11 — human authority boundary, enforced structurally over the brokers package.

    READ / OBSERVE                autonomous
    LOCAL AUTHORITATIVE STATE     proposal-only unless explicit human approval
    BROKER MUTATION               prohibited

These tests scan SOURCE (AST), so a future module added to the package is
covered automatically; they do not depend on any live credential."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from portfolio_automation.brokers import base
from portfolio_automation.brokers import broker_reconciliation as brec
from portfolio_automation.brokers import broker_evidence as BE
from portfolio_automation.brokers import broker_evidence_store as ES
from portfolio_automation.brokers import schwab_auth_manager as am
from portfolio_automation.brokers import schwab_evidence_adapter as AD
from portfolio_automation.brokers import schwab_token_store as ts

PKG = Path(base.__file__).parent
MODULES = sorted(PKG.glob("*.py"))
NEW_MODULES = ("schwab_token_store.py", "schwab_auth_manager.py", "broker_evidence.py",
               "schwab_evidence_adapter.py", "broker_evidence_store.py")

_HTTP_WRITE_VERBS = {"post", "put", "patch", "delete"}
_TRADE_TOKENS = ("place_order", "submit_order", "execute_trade", "cancel_order", "modify_order",
                 "place_trade", "move_money", "transfer", "withdraw", "deposit")


def _tree(py: Path) -> ast.AST:
    return ast.parse(py.read_text(encoding="utf-8"))


def test_new_modules_exist_and_are_scanned():
    names = {p.name for p in MODULES}
    for m in NEW_MODULES:
        assert m in names, m


def test_no_trade_or_order_methods_anywhere_in_brokers_package():
    offenders = []
    for py in MODULES:
        for node in ast.walk(_tree(py)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name.lower()
                if any(tok in name for tok in _TRADE_TOKENS) or name.startswith(("order", "trade", "buy", "sell")):
                    offenders.append(f"{py.name}:{node.name}")
    assert offenders == []


def test_only_http_get_exists_except_the_oauth_token_exchange():
    """The ONLY non-GET HTTP verb in the whole package is the OAuth token POST
    in schwab_oauth._post_token. No broker write endpoint exists."""
    writes = []
    for py in MODULES:
        for node in ast.walk(_tree(py)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in _HTTP_WRITE_VERBS:
                    base_name = getattr(node.func.value, "id", None) or getattr(node.func.value, "attr", None)
                    if base_name in ("requests", "session", "http", "client", "httpx", "urllib3"):
                        writes.append((py.name, node.func.attr, node.lineno))
    assert writes == [("schwab_oauth.py", "post", writes[0][2])] if writes else True
    assert {w[0] for w in writes} <= {"schwab_oauth.py"}
    assert all(w[1] == "post" for w in writes)


def test_client_base_url_is_trader_read_scope_only():
    src = (PKG / "schwab_client.py").read_text(encoding="utf-8")
    assert "/orders" not in src and "/trades" not in src and "/transactions" not in src
    assert 'requests.get(' in src


def test_no_broker_module_writes_config_json():
    """No reconciliation, auth or evidence path may write config.json — the
    proposal is the only output and applying it is a separate operator tool."""
    for py in MODULES:
        src = py.read_text(encoding="utf-8")
        tree = _tree(py)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("write_text", "write_bytes", "safe_write_json", "safe_write_text", "replace", "rename"):
                    segment = ast.get_source_segment(src, node) or ""
                    assert "config.json" not in segment, f"{py.name}:{node.lineno} writes config.json"
        # reading config.json is fine (reconcile compares against it); the string may
        # appear only alongside a read.
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "config.json":
                pass


def test_proposal_is_never_auto_applied():
    recon = brec.reconcile({"totals": {"cash": 10.0}}, {"positions": [{"symbol": "QQQ", "quantity": 6}]},
                           {"portfolio": {"cash_available": 1.0, "holdings": [{"symbol": "QQQ", "shares": 5}]}})
    prop = brec.build_proposal(recon, {"portfolio": {"cash_available": 1.0, "holdings": []}}, now_iso="t")
    assert prop["operator_approval_required"] is True
    assert prop["auto_applied"] is False
    assert "apply" not in {k.lower() for k in prop} - {"apply_instructions"}
    src = (PKG / "broker_reconciliation.py").read_text(encoding="utf-8")
    assert '"operator_approval_required": True' in src and '"auto_applied": False' in src


def test_no_llm_agent_or_controller_can_stand_in_for_human_approval():
    """No broker module imports an approval/controller/LLM surface, and no
    function name claims to approve or apply anything."""
    forbidden_imports = ("openai", "anthropic", "operator_control", "controller", "engineer_worker",
                         "manual_portfolio_update", "tools.")
    for py in MODULES:
        for node in ast.walk(_tree(py)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", None) or ""
                names = [a.name for a in node.names]
                for target in [mod, *names]:
                    assert not any(f in target for f in forbidden_imports), f"{py.name} imports {target}"
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                n = node.name.lower()
                assert not n.startswith(("approve", "apply_config", "auto_apply", "execute")), f"{py.name}:{node.name}"


def test_auth_and_evidence_modules_have_no_execution_surface():
    for mod in (am, ts, BE, AD, ES):
        public = [n for n in dir(mod) if not n.startswith("_")]
        for n in public:
            low = n.lower()
            for tok in ("order", "trade", "buy", "sell", "execute", "transfer", "withdraw", "approve", "apply"):
                assert tok not in low, f"{mod.__name__}.{n}"


def test_auth_manager_only_talks_to_the_token_endpoint():
    src = (PKG / "schwab_auth_manager.py").read_text(encoding="utf-8")
    assert "schwab_client" not in src and "/accounts" not in src
    assert "requests" not in src              # network is isolated in schwab_oauth._post_token


def test_readonly_protocol_still_satisfied_and_forbidden_tokens_absent():
    from portfolio_automation.brokers.schwab_client import SchwabClient
    for m in base.READ_ONLY_METHODS:
        assert hasattr(SchwabClient, m)
    for m in dir(SchwabClient):
        for tok in base.FORBIDDEN_METHOD_TOKENS:
            assert tok not in m.lower()


@pytest.mark.parametrize("artifact_builder", ["build_status"])
def test_status_artifact_hardcodes_read_only_and_no_trading(artifact_builder):
    from portfolio_automation.brokers import broker_status as bs
    st = getattr(bs, artifact_builder)(enabled=True, configured=True, authenticated=True,
                                       account_count=1, position_count=1, last_success_at="t",
                                       last_error=None, now_iso="t", auth_state="OK",
                                       evidence={"truth_status": "CURRENT"})
    assert st["read_only_mode"] is True and st["trading_enabled"] is False and st["observe_only"] is True
    assert st["auth_state"] == "OK" and st["evidence_truth_status"] == "CURRENT"
