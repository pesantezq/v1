"""Phase 11 — learning-loop event store."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import portfolio_automation.event_store as es
from portfolio_automation.next_stage.contracts import EventStream, LearningEvent


def test_append_and_read_roundtrip(tmp_path):
    ok = es.append_event(tmp_path, EventStream.PATTERN,
                         LearningEvent(event_id="e1", timestamp="2026-06-09T00:00:00",
                                       source="s", run_mode="daily", namespace="policy",
                                       ticker_or_theme="AI"))
    assert ok
    evs = es.read_events(tmp_path, EventStream.PATTERN)
    assert len(evs) == 1 and evs[0]["ticker_or_theme"] == "AI"
    assert evs[0]["observe_only"] is True


def test_observe_only_forced_even_if_false(tmp_path):
    es.append_event(tmp_path, "opportunity_events.jsonl",
                    {"event_id": "x", "timestamp": "2026-06-09T00:00:00",
                     "source": "s", "run_mode": "daily", "namespace": "policy",
                     "observe_only": False})
    ev = es.read_events(tmp_path, EventStream.OPPORTUNITY)[0]
    assert ev["observe_only"] is True


def test_append_is_append_only(tmp_path):
    for i in range(3):
        es.record_opportunity_event(tmp_path, ticker_or_theme=f"T{i}",
                                    timestamp=f"2026-06-09T00:0{i}:00")
    assert len(es.read_events(tmp_path, EventStream.OPPORTUNITY)) == 3


def test_unknown_stream_rejected_on_path_helpers(tmp_path):
    with pytest.raises(ValueError):
        es._stream_path(tmp_path, "not_a_stream.jsonl")


def test_append_never_raises_on_bad_input(tmp_path):
    assert es.append_event(tmp_path, EventStream.PATTERN, 12345) is False  # not dict/event


def test_read_tolerates_tampered_lines(tmp_path):
    p = tmp_path / "outputs" / "policy" / "pattern_events.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text('{"event_id":"ok","timestamp":"2026-01-01T00:00:00"}\nGARBAGE\n')
    evs = es.read_events(tmp_path, EventStream.PATTERN)
    assert len(evs) == 1 and evs[0]["event_id"] == "ok"


def test_compaction_moves_old_years_to_archive(tmp_path):
    es.append_event(tmp_path, EventStream.OUTCOME,
                    {"event_id": "old", "timestamp": "2024-05-01T00:00:00",
                     "source": "s", "run_mode": "daily", "namespace": "policy"})
    es.append_event(tmp_path, EventStream.OUTCOME,
                    {"event_id": "new", "timestamp": "2026-05-01T00:00:00",
                     "source": "s", "run_mode": "daily", "namespace": "policy"})
    moved = es.compact_stream(tmp_path, EventStream.OUTCOME, before_year=2026)
    assert moved == 1
    live = es.read_events(tmp_path, EventStream.OUTCOME)
    assert [e["event_id"] for e in live] == ["new"]
    arch = (tmp_path / "outputs" / "policy" / "archive" / "outcome_events.jsonl")
    assert arch.exists() and "old" in arch.read_text()


def test_record_helpers_set_stream_and_source(tmp_path):
    es.record_user_action(tmp_path, user_decision="approve", ticker_or_theme="X")
    ev = es.read_events(tmp_path, EventStream.USER_ACTION)[0]
    assert ev["source"] == "operator" and ev["user_decision"] == "approve"
