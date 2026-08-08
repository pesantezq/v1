from pathlib import Path
import pytest
from portfolio_automation import doc_audit_state as st


def _write(root, rel, content):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_load_state_defaults_when_missing(tmp_path):
    s = st.load_state(str(tmp_path))
    assert s["apply_enabled"] is True
    assert s["last_audited_sha"] is None


def test_round_trip_save_then_load(tmp_path):
    st.save_state(str(tmp_path), {"last_audited_sha": "deadbee",
                                  "last_run_at": "2026-06-01T00:00:00Z",
                                  "apply_enabled": False, "fixes_last_run": 3})
    s = st.load_state(str(tmp_path))
    assert s["last_audited_sha"] == "deadbee"
    assert s["apply_enabled"] is False
    assert s["fixes_last_run"] == 3


def test_state_path_is_under_agent_dir(tmp_path):
    assert st.state_path(str(tmp_path)).endswith(".agent/doc_audit_state.yaml")


def test_coverage_gap_first_seen_defaults_to_empty_mapping(tmp_path):
    assert st.load_state(str(tmp_path))["coverage_gap_first_seen"] == {}


def test_coverage_gap_first_seen_round_trips(tmp_path):
    st.save_state(str(tmp_path), {
        "open_coverage_gaps": ["docs/a.md"],
        "coverage_gap_first_seen": {"docs/a.md": "2026-08-01T12:00:00+00:00"}})
    s = st.load_state(str(tmp_path))
    assert s["coverage_gap_first_seen"] == {"docs/a.md": "2026-08-01T12:00:00+00:00"}


def test_mutable_defaults_are_not_shared_between_loads(tmp_path):
    """_DEFAULTS holds a dict and a list. `{**_DEFAULTS, **data}` copies the
    OUTER mapping only, so both containers were handed out by reference: a
    caller appending to state["open_coverage_gaps"] mutated the module-level
    default and poisoned every later load in the process."""
    first = st.load_state(str(tmp_path))
    first["open_coverage_gaps"].append("docs/poison.md")
    first["coverage_gap_first_seen"]["docs/poison.md"] = "2026-01-01T00:00:00+00:00"

    second = st.load_state(str(tmp_path))
    assert second["open_coverage_gaps"] == []
    assert second["coverage_gap_first_seen"] == {}
