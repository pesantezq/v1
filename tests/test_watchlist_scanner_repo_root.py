"""
Regression tests for watchlist_scanner repo-root resolution.

Several watchlist_scanner writers default their root directory to
``Path(__file__).resolve().parents[N]`` when the caller passes no explicit
``root``.  The watchlist_scanner package lives one level below the repo
root, so ``parents[1]`` is correct and ``parents[2]`` walks one level too
far (e.g. ``/opt`` instead of ``/opt/stockbot``).

The bug is invisible in tests that pass an explicit ``root=tmp_path`` and
silent in production because ``mkdir(parents=True, exist_ok=True)``
happily creates the wrong tree.  These tests pin the correct resolution
so a future refactor cannot quietly re-introduce the off-by-one.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

# Each entry: (module dotted path, source-file relative to repo root)
AFFECTED_MODULES: list[tuple[str, str]] = [
    ("watchlist_scanner.daily_memo",                        "watchlist_scanner/daily_memo.py"),
    ("watchlist_scanner.allocation_preview",                "watchlist_scanner/allocation_preview.py"),
    ("watchlist_scanner.allocation_policy_simulation",      "watchlist_scanner/allocation_policy_simulation.py"),
    ("watchlist_scanner.allocation_policy_activation",      "watchlist_scanner/allocation_policy_activation.py"),
    ("watchlist_scanner.approved_allocation_policy_loader", "watchlist_scanner/approved_allocation_policy_loader.py"),
]


@pytest.mark.parametrize("dotted, rel_path", AFFECTED_MODULES)
def test_module_resolves_repo_root_correctly(dotted: str, rel_path: str):
    """
    Importing the module and computing ``Path(module.__file__).resolve().parents[1]``
    must equal the repo root (the directory that contains main.py and
    watchlist_scanner/).
    """
    mod = importlib.import_module(dotted)
    resolved_root = Path(mod.__file__).resolve().parents[1]
    assert resolved_root == REPO_ROOT, (
        f"{dotted}: parents[1] resolves to {resolved_root}, expected {REPO_ROOT}"
    )
    # Sanity: the resolved root contains repo-root markers.
    assert (resolved_root / "main.py").exists(), f"{resolved_root} missing main.py"


@pytest.mark.parametrize("dotted, rel_path", AFFECTED_MODULES)
def test_source_uses_parents_1_not_parents_2(dotted: str, rel_path: str):
    """
    The source file must not use ``Path(__file__).resolve().parents[2]`` for
    its root-directory default.  ``parents[1]`` is the correct value for
    files at ``<repo>/watchlist_scanner/*.py``.

    This guards against the regression that, between 2026-05-12 and
    2026-05-15, silently misrouted daily memo outputs to ``/opt/`` instead
    of ``/opt/stockbot/`` on the production VPS.
    """
    src_path = REPO_ROOT / rel_path
    source = src_path.read_text(encoding="utf-8", errors="replace")
    bad = re.findall(r"Path\(__file__\)\.resolve\(\)\.parents\[2\]", source)
    assert bad == [], (
        f"{rel_path} contains {len(bad)} use(s) of "
        "Path(__file__).resolve().parents[2] — should be parents[1] for files "
        "at depth 1 of the repo."
    )


def test_no_other_watchlist_scanner_file_uses_parents_2():
    """
    Catch-all: scan every watchlist_scanner/*.py for the same off-by-one
    pattern, so a new module added with the wrong default fails CI.
    """
    package_dir = REPO_ROOT / "watchlist_scanner"
    bad_files: list[str] = []
    for py in package_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        if re.search(r"Path\(__file__\)\.resolve\(\)\.parents\[2\]", text):
            bad_files.append(str(py.relative_to(REPO_ROOT)))
    assert bad_files == [], (
        "watchlist_scanner files using parents[2] for repo-root resolution:\n  "
        + "\n  ".join(bad_files)
    )
