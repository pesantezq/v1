"""Sanity checks for the gui_v2 systemd unit file."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIT = REPO_ROOT / "deploy" / "systemd" / "stockbot-dashboard.service"

# The schemes systemd's manager accepts for Documentation= (see
# systemd's documentation_url_is_valid). A bare relative path such as
# "docs/foo.md" matches none of these and systemd rejects it as "Invalid URL".
_VALID_DOC_SCHEMES = ("http://", "https://", "file:", "info:", "man:")
# The stable release-pointer prefix the shipped file: URIs are built on, so a
# retained reference can be mapped back to a real repository file.
_RELEASE_PREFIX = "file:/opt/stockbot/current/"


def _documentation_values() -> list[str]:
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        if line.startswith("Documentation="):
            return line[len("Documentation="):].split()
    return []


def test_unit_file_exists():
    assert UNIT.exists(), f"missing {UNIT}"


def test_unit_invokes_uvicorn_on_port_8502():
    body = UNIT.read_text(encoding="utf-8")
    assert "uvicorn" in body
    assert "gui_v2.app:app" in body
    assert "8502" in body
    # Streamlit on 8501 must not be touched
    assert "8501" not in body


def test_unit_is_oneshot_or_simple_with_restart_on_failure():
    body = UNIT.read_text(encoding="utf-8")
    assert "Type=" in body
    assert "Restart=on-failure" in body


def test_unit_loads_env_file():
    body = UNIT.read_text(encoding="utf-8")
    assert "EnvironmentFile=" in body



def test_documentation_entries_are_valid_uris_not_relative_paths():
    """Pins the exact release-blocking defect: production pre-switch
    certification found `systemd-analyze verify` rejecting a relative
    Documentation= path as an Invalid URL. Every repository-shipped
    Documentation= entry must be a valid systemd URI, never a bare relative
    path like `docs/foo.md`."""
    values = _documentation_values()
    assert values, "Documentation= directive missing or empty"
    for token in values:
        assert token.startswith(_VALID_DOC_SCHEMES), (
            f"Documentation= entry {token!r} is not a valid systemd URI "
            f"(must start with one of {_VALID_DOC_SCHEMES}); a relative path "
            f"is rejected by systemd-analyze as an Invalid URL")
        # the precise defect shape must never come back
        assert not token.startswith("docs/"), (
            f"Documentation= entry {token!r} regressed to a relative repo path")


def test_documentation_references_resolve_to_real_repository_files():
    """Retained references must still point at real files in the repository,
    via the stable /opt/stockbot/current release pointer."""
    for token in _documentation_values():
        if token.startswith(_RELEASE_PREFIX):
            rel = token[len(_RELEASE_PREFIX):]
            assert (REPO_ROOT / rel).is_file(), (
                f"Documentation= references {rel!r} which is not a file in the "
                f"repository")


def _invalid_url_lines(unit_path: Path) -> list[str]:
    proc = subprocess.run(
        ["systemd-analyze", "verify", "--recursive-errors=no", str(unit_path)],
        capture_output=True, text=True)
    return [ln for ln in (proc.stdout + proc.stderr).splitlines()
            if "Invalid URL" in ln]


@pytest.mark.skipif(shutil.which("systemd-analyze") is None,
                    reason="systemd-analyze not available in this environment")
def test_real_systemd_accepts_shipped_documentation_and_rejects_relative(tmp_path):
    """Real-verifier regression (runs only where systemd-analyze exists):
    the shipped unit emits NO 'Invalid URL' for Documentation=, while an
    otherwise-identical unit reverted to the old relative path DOES -- proving
    the check is load-bearing, and letting real systemd own the acceptance
    semantics rather than reimplementing them."""
    good = tmp_path / "stockbot-dashboard.service"
    good.write_text(UNIT.read_text(encoding="utf-8"), encoding="utf-8")
    assert _invalid_url_lines(good) == [], (
        "shipped unit still produces an Invalid URL for Documentation=")

    bad_body = UNIT.read_text(encoding="utf-8").replace(
        [v for v in UNIT.read_text(encoding="utf-8").splitlines()
         if v.startswith("Documentation=")][0],
        "Documentation=docs/superpowers/specs/2026-05-15-gui-v2-design.md")
    bad = tmp_path / "bad.service"
    bad.write_text(bad_body, encoding="utf-8")
    assert _invalid_url_lines(bad), (
        "a relative Documentation= path should be rejected by systemd-analyze "
        "as an Invalid URL -- the regression is not exercising the defect")
