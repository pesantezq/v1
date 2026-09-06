"""Dashboard origins must bind loopback in every supported production launch path.

WHY THIS TEST EXISTS.

The intended architecture is:

    browser -> authenticated/tunnelled edge -> loopback-only application origin

docs/DASHBOARD_HOSTING.md has documented that for a long time ("Origin bind |
uvicorn --host 127.0.0.1 --port 8502 (localhost-only; not publicly reachable)"),
but the deployment units themselves had drifted to 0.0.0.0. Live production was
bound correctly; the SOURCE was not, so the exposure risk was a future redeploy
from main rather than an observed live exposure.

Nothing failed when that drifted, because nothing checked. This does.

DELIBERATELY NOT A REPO-WIDE GREP.

A blanket search for "0.0.0.0" would fire on historical design documents under
docs/superpowers/, on prose that warns against public binding, and on any
unrelated service that legitimately binds all interfaces. It would be abandoned
the first time it cried wolf. These assertions are pinned to the exact launch
surfaces that start a dashboard origin, parsed structurally.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

STREAMLIT_UNIT = REPO / "deploy" / "stockbot-streamlit.service"
DASHBOARD_UNIT = REPO / "deploy" / "systemd" / "stockbot-dashboard.service"
STREAMLIT_SCRIPT = REPO / "scripts" / "server_start_streamlit.sh"

LOOPBACK = "127.0.0.1"
ANY_INTERFACE = "0.0.0.0"


def _exec_start(unit_path: Path) -> str:
    """Return the unit's ExecStart as one line, joining systemd continuations."""
    text = unit_path.read_text(encoding="utf-8")
    # systemd continues a directive when the line ends with a backslash.
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    for line in joined.splitlines():
        if line.startswith("ExecStart="):
            return line[len("ExecStart="):].strip()
    raise AssertionError(f"no ExecStart in {unit_path.name}")


def _flag_value(command: str, flag: str) -> str:
    """Value of `--flag X` in a command line, tokenised rather than regexed."""
    tokens = shlex.split(command)
    for i, tok in enumerate(tokens):
        if tok == flag:
            assert i + 1 < len(tokens), f"{flag} has no value in: {command}"
            return tokens[i + 1]
        if tok.startswith(flag + "="):
            return tok.split("=", 1)[1]
    raise AssertionError(f"{flag} not found in: {command}")


# ── the three supported production launch surfaces ────────────────────────


def test_streamlit_unit_binds_loopback():
    cmd = _exec_start(STREAMLIT_UNIT)
    assert _flag_value(cmd, "--server.address") == LOOPBACK
    assert _flag_value(cmd, "--server.port") == "8501"


def test_dashboard_v2_unit_binds_loopback():
    cmd = _exec_start(DASHBOARD_UNIT)
    assert _flag_value(cmd, "--host") == LOOPBACK
    assert _flag_value(cmd, "--port") == "8502"


def test_streamlit_start_script_binds_loopback():
    text = STREAMLIT_SCRIPT.read_text(encoding="utf-8")
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    launch = [ln for ln in joined.splitlines() if "streamlit run" in ln]
    assert launch, "no streamlit launch line in the start script"
    for line in launch:
        assert _flag_value(line, "--server.address") == LOOPBACK
        assert _flag_value(line, "--server.port") == "8501"


@pytest.mark.parametrize("path", [STREAMLIT_UNIT, DASHBOARD_UNIT, STREAMLIT_SCRIPT])
def test_launch_surfaces_never_bind_all_interfaces(path: Path):
    """The regression itself: a bind to 0.0.0.0 in any of these files.

    Scoped to executable launch lines so a comment or a warning that mentions
    0.0.0.0 cannot fail the test -- only an actual bind can."""
    text = re.sub(r"\\\s*\n\s*", " ", path.read_text(encoding="utf-8"))
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if not any(k in stripped for k in
                   ("ExecStart=", "streamlit run", "uvicorn")):
            continue
        assert ANY_INTERFACE not in stripped, (
            f"{path.name} binds all interfaces: {stripped}")


def test_documented_architecture_still_says_loopback():
    """The units and the hosting doc must not drift apart again in either
    direction -- the doc was right while the units were wrong, and a future
    change to the doc alone would hide the same defect."""
    doc = (REPO / "docs" / "DASHBOARD_HOSTING.md").read_text(encoding="utf-8")
    assert "--host 127.0.0.1 --port 8502" in doc
    assert "localhost-only" in doc
