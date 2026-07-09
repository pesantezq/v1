"""GUI Phase 1 — Item 3: severity-token drift.

The solid left-rail severity color (the ``w-1`` bar) was hand-copied into
``status_card`` (_ui.html), portfolio.html, and strategy_lab.html. The three
copies drifted:

  - strategy_lab used the strings ``amber``/``sky`` and dropped the ``red`` and
    ``blue`` branches, so a ``blue`` severity (emitted by ``_SUMMARY_META`` for
    the "Best Balance" card) silently fell through to the gray ``else`` rail.

These tests pin a single public ``sev_rail`` macro as the one source of truth
for the rail color and assert the drifted inline ladders are gone.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TPL = REPO_ROOT / "gui_v2" / "templates"


def _sev_rail(severity: str) -> str:
    from gui_v2.app import templates

    module = templates.env.get_template("components/_ui.html").module
    return module.sev_rail(severity)


def test_sev_rail_covers_full_severity_vocab():
    # green / red are canonical
    assert "bg-emerald-500/70" in _sev_rail("green")
    assert "bg-rose-500/70" in _sev_rail("red")
    # yellow and amber are aliases for the same amber rail
    assert "bg-amber-500/70" in _sev_rail("yellow")
    assert "bg-amber-500/70" in _sev_rail("amber")
    # blue and sky are aliases for the same sky rail (the drift that turned
    # "Best Balance" gray was a missing blue branch)
    assert "bg-sky-500/60" in _sev_rail("blue")
    assert "bg-sky-500/60" in _sev_rail("sky")
    # unknown / empty falls back to the neutral zinc rail
    assert "bg-zinc-700" in _sev_rail("mystery")
    assert "bg-zinc-700" in _sev_rail("")


def test_status_card_uses_shared_sev_rail_not_inline_ladder():
    body = (TPL / "components" / "_ui.html").read_text()
    # status_card must delegate to sev_rail, not re-implement the ladder inline.
    assert "sev_rail(c.severity)" in body


def test_portfolio_and_strategy_lab_have_no_inline_rail_ladder():
    """The two page templates must not re-implement the rail severity ladder."""
    ladder = re.compile(r"severity == '(green|yellow|amber)'\s*%\}\s*bg-")
    for name in ("portfolio.html", "strategy_lab.html"):
        body = (TPL / "dashboard" / name).read_text()
        assert not ladder.search(body), f"{name} still has an inline rail ladder"
        assert "ui.sev_rail(" in body, f"{name} should call ui.sev_rail"
