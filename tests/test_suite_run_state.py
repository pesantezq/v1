# tests/test_suite_run_state.py
from datetime import datetime, timedelta, timezone

from portfolio_automation import suite_run_state as srs

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_is_due_true_when_never_run(tmp_path):
    # No state file → the cadence has never run → due.
    assert srs.is_due("weekly", root=tmp_path, now=NOW) is True
    assert srs.days_since("weekly", root=tmp_path, now=NOW) is None


def test_stamp_round_trips_and_makes_not_due(tmp_path):
    srs.stamp("weekly", root=tmp_path, now=NOW)
    state = srs.load_suite_state(tmp_path)
    assert state["last_weekly_run_at"] == NOW.isoformat()
    # Same instant → 0 days elapsed → not due.
    assert srs.is_due("weekly", root=tmp_path, now=NOW) is False
    assert srs.days_since("weekly", root=tmp_path, now=NOW) == 0.0


def test_not_due_within_threshold(tmp_path):
    srs.stamp("weekly", root=tmp_path, now=NOW)
    six_days = NOW + timedelta(days=6, hours=23)
    assert srs.is_due("weekly", root=tmp_path, now=six_days) is False


def test_due_at_and_past_threshold(tmp_path):
    srs.stamp("weekly", root=tmp_path, now=NOW)
    exactly_seven = NOW + timedelta(days=7)
    eight = NOW + timedelta(days=8)
    assert srs.is_due("weekly", root=tmp_path, now=exactly_seven) is True
    assert srs.is_due("weekly", root=tmp_path, now=eight) is True
    assert round(srs.days_since("weekly", root=tmp_path, now=eight), 2) == 8.0


def test_custom_threshold_override(tmp_path):
    srs.stamp("weekly", root=tmp_path, now=NOW)
    two_days = NOW + timedelta(days=2)
    assert srs.is_due("weekly", root=tmp_path, now=two_days, threshold_days=1) is True
    assert srs.is_due("weekly", root=tmp_path, now=two_days, threshold_days=3) is False


def test_cadences_are_independent(tmp_path):
    srs.stamp("weekly", root=tmp_path, now=NOW)
    # weekly stamped, monthly never → weekly not-due, monthly due.
    assert srs.is_due("weekly", root=tmp_path, now=NOW) is False
    assert srs.is_due("monthly", root=tmp_path, now=NOW) is True


def test_monthly_default_threshold_is_30_days(tmp_path):
    # weekly→monthly cascade relies on the 30-day monthly default.
    assert srs.DUE_THRESHOLD_DAYS["monthly"] == 30
    srs.stamp("monthly", root=tmp_path, now=NOW)
    assert srs.is_due("monthly", root=tmp_path, now=NOW + timedelta(days=29)) is False
    assert srs.is_due("monthly", root=tmp_path, now=NOW + timedelta(days=30)) is True


def test_corrupt_state_file_is_treated_as_never_run(tmp_path):
    p = tmp_path / ".agent" / "suite_run_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json", encoding="utf-8")
    assert srs.load_suite_state(tmp_path) == {}
    assert srs.is_due("weekly", root=tmp_path, now=NOW) is True


def test_naive_timestamp_handled(tmp_path):
    # A stored timestamp without tzinfo must not crash days_since.
    p = tmp_path / ".agent" / "suite_run_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"last_weekly_run_at": "2026-07-01T12:00:00"}', encoding="utf-8")
    d = srs.days_since("weekly", root=tmp_path, now=NOW)
    assert d is not None and round(d, 1) == 9.0
