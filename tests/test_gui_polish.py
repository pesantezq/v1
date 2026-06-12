import pytest
from pathlib import Path

_UI = Path("gui_v2/templates/components/_ui.html")

_DENSE = ["portfolio", "system", "portfolio_sync", "strategy_tax"]


@pytest.mark.parametrize("tab", _DENSE)
def test_dense_tabs_wrap_tables(tab):
    src = Path(f"gui_v2/templates/dashboard/{tab}.html").read_text(encoding="utf-8")
    assert "responsive_table" in src or "overflow-x-auto" in src


def test_ui_has_responsive_table_macro():
    src = _UI.read_text(encoding="utf-8")
    assert "macro responsive_table" in src
    assert "overflow-x-auto" in src


def test_ui_badges_have_a11y():
    src = _UI.read_text(encoding="utf-8")
    assert src.count('role="status"') >= 1
    assert "aria-label" in src


def test_portfolio_sync_imports_ui():
    src = Path("gui_v2/templates/dashboard/portfolio_sync.html").read_text(encoding="utf-8")
    assert 'import "components/_ui.html"' in src


def test_portfolio_config_imports_ui_and_keeps_edit_form():
    src = Path("gui_v2/templates/dashboard/portfolio_config.html").read_text(encoding="utf-8")
    assert 'import "components/_ui.html"' in src
    assert 'include "components/portfolio_edit_form.html"' in src
