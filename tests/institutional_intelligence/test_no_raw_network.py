"""Phase 2 guard — no raw network I/O in the Institutional Intelligence package
outside the sanctioned governed client (``sec_client.py``).

Mirrors tests/test_data_budget_no_direct_construction.py: an AST/source scan
that fails if any module in portfolio_automation/institutional_intelligence/
(other than the sanctioned transport) imports urllib.request / requests / httpx
or calls urlopen. This enforces "no raw network calls outside an approved
governed client" for the new subsystem without touching the pre-existing
ungoverned EDGAR adapter elsewhere in the repo.
"""

from __future__ import annotations

import ast
from pathlib import Path

_PKG = Path(__file__).resolve().parents[2] / "portfolio_automation" / "institutional_intelligence"
_SANCTIONED = {"sec_client.py"}
_FORBIDDEN_IMPORT_ROOTS = {"requests", "httpx", "urllib"}
_FORBIDDEN_CALLS = {"urlopen"}


def _network_violations() -> list[str]:
    bad: list[str] = []
    for py in _PKG.rglob("*.py"):
        if py.name in _SANCTIONED:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    root = a.name.split(".")[0]
                    if root in _FORBIDDEN_IMPORT_ROOTS:
                        bad.append(f"{py.name}: import {a.name}")
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in _FORBIDDEN_IMPORT_ROOTS:
                    bad.append(f"{py.name}: from {node.module} import ...")
            elif isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if name in _FORBIDDEN_CALLS:
                    bad.append(f"{py.name}: call {name}(...)")
    return bad


def test_no_raw_network_outside_sec_client():
    violations = _network_violations()
    assert violations == [], (
        "Raw network I/O found outside the governed SEC client "
        f"(sec_client.py). All EDGAR traffic must go through GovernedSECClient: {violations}"
    )


def test_sec_client_is_the_only_sanctioned_transport():
    # Guard against the allow-list silently growing.
    assert _SANCTIONED == {"sec_client.py"}
