"""Phase 9 — read-only broker Protocol + no-write guarantee across brokers pkg."""
from __future__ import annotations

import ast
from pathlib import Path

from portfolio_automation.brokers import base
from portfolio_automation.brokers.schwab_client import SchwabClient


def test_schwab_client_satisfies_readonly_protocol():
    for m in base.READ_ONLY_METHODS:
        assert hasattr(SchwabClient, m), f"SchwabClient missing read method {m}"


def test_base_declares_no_write_methods():
    src = (Path(base.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    method_names = [n.name.lower() for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for name in method_names:
        for tok in base.FORBIDDEN_METHOD_TOKENS:
            assert tok not in name, f"forbidden token {tok} in base method {name}"


def test_whole_brokers_package_has_no_trade_methods():
    pkg = Path(base.__file__).parent
    for py in pkg.glob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name.lower()
                for tok in ("place_order", "submit_order", "execute_trade",
                            "cancel_order", "modify_order"):
                    assert tok not in name, f"{py.name}:{node.name}"
