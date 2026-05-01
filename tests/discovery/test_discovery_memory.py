"""Tests for portfolio_automation.discovery.discovery_memory."""
import json
import pytest
from datetime import datetime, timezone
from pathlib import Path

from portfolio_automation.discovery.candidate_promotion_engine import (
    CandidateStatus,
    DiscoveryCandidate,
)
from portfolio_automation.discovery.event_classifier import EventType
from portfolio_automation.discovery.discovery_memory import (
    DiscoveryMemory,
    MemoryEntry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = "2026-05-01T00:00:00+00:00"
_TS2 = "2026-05-02T00:00:00+00:00"


def _make_candidate(
    ticker: str,
    status: CandidateStatus = CandidateStatus.DISCOVERED,
    score: float = 1.0,
    mention_count: int = 1,
    unique_source_count: int = 1,
    event_type: EventType = EventType.EARNINGS,
    risk_flag: bool = False,
    first_seen: str = _TS,
    last_seen: str = _TS,
    rejection_reason: str | None = None,
) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        ticker=ticker,
        status=status,
        score=score,
        mention_count=mention_count,
        unique_source_count=unique_source_count,
        event_type=event_type,
        event_confidence=0.5,
        risk_flag=risk_flag,
        rejection_reason=rejection_reason,
        first_seen=first_seen,
        last_seen=last_seen,
    )


# ---------------------------------------------------------------------------
# 1. Missing file tolerated
# ---------------------------------------------------------------------------

class TestMissingFile:
    def test_load_missing_path_returns_empty(self, tmp_path):
        mem = DiscoveryMemory.load_from_path(tmp_path / "no_such_file.json")
        assert len(mem) == 0

    def test_load_missing_path_no_exception(self, tmp_path):
        mem = DiscoveryMemory.load_from_path(tmp_path / "ghost.json")
        assert isinstance(mem, DiscoveryMemory)


# ---------------------------------------------------------------------------
# 2. Corrupt JSON tolerated
# ---------------------------------------------------------------------------

class TestCorruptFile:
    def test_corrupt_json_returns_empty(self, tmp_path):
        p = tmp_path / "corrupt.json"
        p.write_text("not valid json {{{{", encoding="utf-8")
        mem = DiscoveryMemory.load_from_path(p)
        assert len(mem) == 0

    def test_empty_file_returns_empty(self, tmp_path):
        p = tmp_path / "empty.json"
        p.write_text("", encoding="utf-8")
        mem = DiscoveryMemory.load_from_path(p)
        assert len(mem) == 0

    def test_whitespace_file_returns_empty(self, tmp_path):
        p = tmp_path / "ws.json"
        p.write_text("   \n  ", encoding="utf-8")
        mem = DiscoveryMemory.load_from_path(p)
        assert len(mem) == 0

    def test_malformed_entries_skipped(self, tmp_path):
        p = tmp_path / "malformed.json"
        data = {
            "entries": [
                {"ticker": "NVDA", "first_seen": _TS, "last_seen": _TS,
                 "mention_count": 1, "source_count": 1, "seen_runs": 1,
                 "status": "discovered", "last_score": 1.0, "last_event_type": "earnings"},
                {"bad_entry": True},  # no ticker field
            ]
        }
        p.write_text(json.dumps(data), encoding="utf-8")
        mem = DiscoveryMemory.load_from_path(p)
        assert len(mem) == 1
        assert mem.get("NVDA") is not None


# ---------------------------------------------------------------------------
# 3. first_seen preserved
# ---------------------------------------------------------------------------

class TestFirstSeenPreserved:
    def test_first_seen_set_on_first_update(self):
        mem = DiscoveryMemory()
        cand = _make_candidate("NVDA", first_seen=_TS)
        mem.update([cand])
        entry = mem.get("NVDA")
        assert entry is not None
        assert entry.first_seen == _TS

    def test_first_seen_preserved_on_second_update(self):
        mem = DiscoveryMemory()
        cand1 = _make_candidate("NVDA", first_seen=_TS, last_seen=_TS)
        mem.update([cand1])
        cand2 = _make_candidate("NVDA", first_seen=_TS2, last_seen=_TS2)
        mem.update([cand2])
        entry = mem.get("NVDA")
        assert entry.first_seen == _TS  # original preserved


# ---------------------------------------------------------------------------
# 4. last_seen updated
# ---------------------------------------------------------------------------

class TestLastSeenUpdated:
    def test_last_seen_updated_on_second_update(self):
        mem = DiscoveryMemory()
        mem.update([_make_candidate("NVDA", first_seen=_TS, last_seen=_TS)])
        mem.update([_make_candidate("NVDA", first_seen=_TS2, last_seen=_TS2)])
        entry = mem.get("NVDA")
        assert entry.last_seen == _TS2


# ---------------------------------------------------------------------------
# 5. mention_count accumulates
# ---------------------------------------------------------------------------

class TestMentionCountAccumulates:
    def test_mention_count_adds_across_runs(self):
        mem = DiscoveryMemory()
        mem.update([_make_candidate("NVDA", mention_count=3)])
        mem.update([_make_candidate("NVDA", mention_count=5)])
        entry = mem.get("NVDA")
        assert entry.mention_count == 8

    def test_mention_count_single_run(self):
        mem = DiscoveryMemory()
        mem.update([_make_candidate("AAPL", mention_count=7)])
        assert mem.get("AAPL").mention_count == 7


# ---------------------------------------------------------------------------
# 6. seen_runs accumulates
# ---------------------------------------------------------------------------

class TestSeenRunsAccumulates:
    def test_seen_runs_increments(self):
        mem = DiscoveryMemory()
        mem.update([_make_candidate("NVDA")])
        mem.update([_make_candidate("NVDA")])
        mem.update([_make_candidate("NVDA")])
        assert mem.get("NVDA").seen_runs == 3

    def test_seen_runs_starts_at_one(self):
        mem = DiscoveryMemory()
        mem.update([_make_candidate("AAPL")])
        assert mem.get("AAPL").seen_runs == 1


# ---------------------------------------------------------------------------
# 7. Status and score updated
# ---------------------------------------------------------------------------

class TestStatusAndScoreUpdated:
    def test_status_updated_to_latest(self):
        mem = DiscoveryMemory()
        mem.update([_make_candidate("NVDA", status=CandidateStatus.DISCOVERED)])
        mem.update([_make_candidate("NVDA", status=CandidateStatus.WATCH)])
        assert mem.get("NVDA").status == CandidateStatus.WATCH.value

    def test_last_score_updated(self):
        mem = DiscoveryMemory()
        mem.update([_make_candidate("NVDA", score=1.0)])
        mem.update([_make_candidate("NVDA", score=3.5)])
        assert mem.get("NVDA").last_score == pytest.approx(3.5)


# ---------------------------------------------------------------------------
# 8. to_dict / serialization
# ---------------------------------------------------------------------------

class TestToDictSerialization:
    def test_to_dict_has_entries_key(self):
        mem = DiscoveryMemory()
        mem.update([_make_candidate("NVDA")])
        d = mem.to_dict()
        assert "entries" in d

    def test_to_dict_discovery_only_true(self):
        mem = DiscoveryMemory()
        d = mem.to_dict()
        assert d["discovery_only"] is True

    def test_to_dict_sandbox_only_true(self):
        mem = DiscoveryMemory()
        d = mem.to_dict()
        assert d["sandbox_only"] is True

    def test_to_dict_entry_count_matches(self):
        mem = DiscoveryMemory()
        mem.update([_make_candidate("NVDA"), _make_candidate("AAPL")])
        d = mem.to_dict()
        assert d["entry_count"] == 2
        assert len(d["entries"]) == 2

    def test_to_dict_is_json_serializable(self):
        mem = DiscoveryMemory()
        mem.update([_make_candidate("NVDA")])
        json.dumps(mem.to_dict())  # should not raise


# ---------------------------------------------------------------------------
# 9. Load from valid JSON
# ---------------------------------------------------------------------------

class TestLoadFromValidJson:
    def test_load_existing_entries(self, tmp_path):
        data = {
            "entries": [
                {
                    "ticker": "NVDA",
                    "first_seen": _TS,
                    "last_seen": _TS,
                    "mention_count": 3,
                    "source_count": 2,
                    "seen_runs": 2,
                    "status": "watch",
                    "last_score": 2.5,
                    "last_event_type": "earnings",
                }
            ]
        }
        p = tmp_path / "memory.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        mem = DiscoveryMemory.load_from_path(p)
        assert len(mem) == 1
        entry = mem.get("NVDA")
        assert entry.seen_runs == 2
        assert entry.status == "watch"

    def test_load_then_update_accumulates(self, tmp_path):
        data = {"entries": [
            {"ticker": "NVDA", "first_seen": _TS, "last_seen": _TS,
             "mention_count": 2, "source_count": 1, "seen_runs": 1,
             "status": "discovered", "last_score": 1.0, "last_event_type": "earnings"}
        ]}
        p = tmp_path / "memory.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        mem = DiscoveryMemory.load_from_path(p)
        mem.update([_make_candidate("NVDA", mention_count=3)])
        entry = mem.get("NVDA")
        assert entry.seen_runs == 2
        assert entry.mention_count == 5  # 2 + 3

    def test_first_seen_preserved_after_load(self, tmp_path):
        data = {"entries": [
            {"ticker": "NVDA", "first_seen": _TS, "last_seen": _TS,
             "mention_count": 1, "source_count": 1, "seen_runs": 1,
             "status": "discovered", "last_score": 1.0, "last_event_type": "earnings"}
        ]}
        p = tmp_path / "memory.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        mem = DiscoveryMemory.load_from_path(p)
        mem.update([_make_candidate("NVDA", first_seen=_TS2, last_seen=_TS2)])
        assert mem.get("NVDA").first_seen == _TS  # original preserved


# ---------------------------------------------------------------------------
# 10. MemoryEntry.from_dict tolerates missing optional fields
# ---------------------------------------------------------------------------

class TestMemoryEntryFromDict:
    def test_minimal_dict(self):
        entry = MemoryEntry.from_dict({
            "ticker": "NVDA",
            "first_seen": _TS,
            "last_seen": _TS,
            "mention_count": 1,
            "source_count": 1,
            "seen_runs": 1,
            "status": "discovered",
            "last_score": 1.0,
            "last_event_type": "earnings",
        })
        assert entry.ticker == "NVDA"
        assert entry.rejected_reason is None
        assert entry.discovery_only is True

    def test_missing_optional_fields_use_defaults(self):
        entry = MemoryEntry.from_dict({"ticker": "AAPL"})
        assert entry.rejected_reason is None
        assert entry.discovery_only is True
        assert entry.sandbox_only is True
