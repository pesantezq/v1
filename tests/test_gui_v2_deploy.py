"""Sanity checks for the gui_v2 systemd unit file."""
from __future__ import annotations

from pathlib import Path

UNIT = Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "stockbot-dashboard.service"


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
