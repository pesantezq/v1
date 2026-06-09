"""Unit tests for the worker protected-path classifier."""
from __future__ import annotations

from operator_control.protected_paths import is_protected, violating_paths


def test_protected_basenames():
    assert is_protected("decision_engine.py")
    assert is_protected("scoring.py")
    assert is_protected("portfolio_decision_engine.py")
    assert is_protected("config.json")
    assert is_protected("requirements.txt")


def test_protected_paths_and_dirs():
    assert is_protected("config/signal_registry.yaml")
    assert is_protected(".claude/commands/x.md")
    assert is_protected("deploy/anything.conf")
    assert is_protected("portfolio_automation/brokers/schwab_sync.py")
    assert is_protected(".env")
    assert is_protected(".env.local")
    assert is_protected("deploy/stockbot-dashboard.service")


def test_non_protected():
    assert not is_protected("operator_control/worker_runner.py")
    assert not is_protected("gui_v2/data/today.py")
    assert not is_protected("docs/operator_control.md")
    assert not is_protected("tests/test_x.py")


def test_violating_paths_filters():
    changed = ["gui_v2/app.py", "scoring.py", "docs/x.md", ".env"]
    assert violating_paths(changed) == ["scoring.py", ".env"]
