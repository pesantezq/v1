"""Tests for GUI discovery approval loaders in gui_operator_data.py."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gui_operator_data import (
    load_discovery_approval_decisions,
    load_discovery_approval_summary,
    load_discovery_sandbox_status,
)


# ---------------------------------------------------------------------------
# Base helper
# ---------------------------------------------------------------------------

class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel: str, payload: dict | list) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _write_text(self, rel: str, content: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _write_jsonl(self, rel: str, lines: list[dict]) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(d) for d in lines) + "\n",
            encoding="utf-8",
        )
        return path

    def _approval_line(self, symbol: str = "NVDA", decision: str = "keep_watching") -> dict:
        return {
            "generated_at": "2026-05-02T12:00:00+00:00",
            "symbol": symbol,
            "company_name": "",
            "candidate_status": "watch",
            "corroboration_score": 0.72,
            "corroboration_level": "strong",
            "decision": decision,
            "decision_reason": "test reason",
            "operator": "operator",
            "source_artifact": "outputs/sandbox/discovery/emerging_candidates.json",
            "run_id": "2026-05-02_discovery",
            "observe_only": True,
            "sandbox_only": True,
            "no_trade": True,
            "no_official_promotion": True,
        }


_DECISIONS_PATH = "outputs/sandbox/discovery/approval_decisions.jsonl"
_EMERGING_PATH = "outputs/sandbox/discovery/emerging_candidates.json"


# ---------------------------------------------------------------------------
# load_discovery_approval_decisions
# ---------------------------------------------------------------------------

class TestLoadDiscoveryApprovalDecisions(_Base):
    def test_missing_file_returns_empty_list(self):
        result = load_discovery_approval_decisions(self.root)
        self.assertEqual(result, [])

    def test_empty_file_returns_empty_list(self):
        self._write_text(_DECISIONS_PATH, "")
        result = load_discovery_approval_decisions(self.root)
        self.assertEqual(result, [])

    def test_valid_decisions_loaded(self):
        self._write_jsonl(_DECISIONS_PATH, [self._approval_line("NVDA")])
        result = load_discovery_approval_decisions(self.root)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "NVDA")

    def test_multiple_decisions_loaded(self):
        self._write_jsonl(_DECISIONS_PATH, [
            self._approval_line("NVDA"),
            self._approval_line("AAPL", "reject_candidate"),
        ])
        result = load_discovery_approval_decisions(self.root)
        self.assertEqual(len(result), 2)

    def test_malformed_line_skipped(self):
        p = self.root / _DECISIONS_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        good = json.dumps(self._approval_line("NVDA"))
        p.write_text("NOT JSON\n" + good + "\n", encoding="utf-8")
        result = load_discovery_approval_decisions(self.root)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "NVDA")

    def test_blank_lines_skipped(self):
        p = self.root / _DECISIONS_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        good = json.dumps(self._approval_line("MSFT"))
        p.write_text("\n\n" + good + "\n\n", encoding="utf-8")
        result = load_discovery_approval_decisions(self.root)
        self.assertEqual(len(result), 1)

    def test_non_dict_json_line_skipped(self):
        p = self.root / _DECISIONS_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('["a", "b"]\n', encoding="utf-8")
        result = load_discovery_approval_decisions(self.root)
        self.assertEqual(result, [])

    def test_decision_fields_present(self):
        self._write_jsonl(_DECISIONS_PATH, [self._approval_line("NVDA")])
        result = load_discovery_approval_decisions(self.root)
        rec = result[0]
        for key in ("symbol", "decision", "observe_only", "sandbox_only", "no_trade"):
            self.assertIn(key, rec)

    def test_does_not_write_any_files(self):
        # The loader is read-only — calling it should not create new files
        self._write_jsonl(_DECISIONS_PATH, [self._approval_line("NVDA")])
        files_before = set(self.root.rglob("*"))
        load_discovery_approval_decisions(self.root)
        files_after = set(self.root.rglob("*"))
        self.assertEqual(files_before, files_after)


# ---------------------------------------------------------------------------
# load_discovery_approval_summary
# ---------------------------------------------------------------------------

class TestLoadDiscoveryApprovalSummary(_Base):
    def test_missing_file_returns_safe_defaults(self):
        result = load_discovery_approval_summary(self.root)
        self.assertEqual(result["total_decisions"], 0)
        self.assertEqual(result["decision_counts"], {})
        self.assertEqual(result["latest_per_symbol"], {})

    def test_governance_flags_always_true(self):
        result = load_discovery_approval_summary(self.root)
        self.assertTrue(result["observe_only"])
        self.assertTrue(result["sandbox_only"])
        self.assertTrue(result["no_trade"])
        self.assertTrue(result["no_official_promotion"])

    def test_governance_flags_true_even_with_decisions(self):
        self._write_jsonl(_DECISIONS_PATH, [self._approval_line("NVDA")])
        result = load_discovery_approval_summary(self.root)
        self.assertTrue(result["observe_only"])
        self.assertTrue(result["sandbox_only"])
        self.assertTrue(result["no_trade"])
        self.assertTrue(result["no_official_promotion"])

    def test_counts_decisions(self):
        self._write_jsonl(_DECISIONS_PATH, [
            self._approval_line("NVDA", "keep_watching"),
            self._approval_line("AAPL", "keep_watching"),
            self._approval_line("MSFT", "reject_candidate"),
        ])
        result = load_discovery_approval_summary(self.root)
        self.assertEqual(result["total_decisions"], 3)
        self.assertEqual(result["decision_counts"]["keep_watching"], 2)
        self.assertEqual(result["decision_counts"]["reject_candidate"], 1)

    def test_unique_symbols_counted(self):
        self._write_jsonl(_DECISIONS_PATH, [
            self._approval_line("NVDA"),
            self._approval_line("AAPL"),
        ])
        result = load_discovery_approval_summary(self.root)
        self.assertEqual(result["unique_symbols_reviewed"], 2)

    def test_latest_per_symbol_last_wins(self):
        p = self.root / _DECISIONS_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            self._approval_line("NVDA", "keep_watching"),
            self._approval_line("NVDA", "reject_candidate"),
        ]
        p.write_text("\n".join(json.dumps(d) for d in lines) + "\n", encoding="utf-8")
        result = load_discovery_approval_summary(self.root)
        self.assertEqual(result["latest_per_symbol"]["NVDA"]["decision"], "reject_candidate")

    def test_disclaimer_present(self):
        result = load_discovery_approval_summary(self.root)
        self.assertIn("sandbox research notes", result["disclaimer"])

    def test_does_not_write_any_files(self):
        self._write_jsonl(_DECISIONS_PATH, [self._approval_line("NVDA")])
        files_before = set(self.root.rglob("*"))
        load_discovery_approval_summary(self.root)
        files_after = set(self.root.rglob("*"))
        self.assertEqual(files_before, files_after)


# ---------------------------------------------------------------------------
# load_discovery_sandbox_status — approval data threaded in
# ---------------------------------------------------------------------------

class TestDiscoverySandboxStatusWithApproval(_Base):
    def _emerging_payload(self) -> dict:
        return {
            "generated_at": "2026-05-02T09:00:00",
            "run_id": "test_run",
            "observe_only": True,
            "discovery_only": True,
            "sandbox_only": True,
            "disclaimer": "Discovery candidates are not buy/sell recommendations.",
            "total_candidates": 1,
            "watch_count": 1,
            "discovered_count": 0,
            "candidates": [{
                "ticker": "NVDA",
                "status": "watch",
                "score": 2.5,
                "mention_count": 5,
                "unique_source_count": 3,
                "event_type": "earnings",
                "event_confidence": 0.9,
                "risk_flag": False,
                "rejection_reason": None,
                "discovery_only": True,
                "sandbox_only": True,
                "corroboration_required": True,
                "corroboration_met": True,
                "corroboration_score": 0.72,
                "corroboration_level": "strong",
                "corroboration_sources": ["reuters", "bloomberg"],
                "first_seen": "2026-05-01T00:00:00",
                "last_seen": "2026-05-02T00:00:00",
                "evidence_snippets": [],
            }],
        }

    def test_approval_decisions_key_present(self):
        self._write(_EMERGING_PATH, self._emerging_payload())
        result = load_discovery_sandbox_status(self.root)
        self.assertIn("approval_decisions", result)

    def test_approval_summary_key_present(self):
        self._write(_EMERGING_PATH, self._emerging_payload())
        result = load_discovery_sandbox_status(self.root)
        self.assertIn("approval_summary", result)

    def test_approval_decisions_empty_when_no_jsonl(self):
        self._write(_EMERGING_PATH, self._emerging_payload())
        result = load_discovery_sandbox_status(self.root)
        self.assertEqual(result["approval_decisions"], [])

    def test_approval_summary_governance_flags_always_true(self):
        self._write(_EMERGING_PATH, self._emerging_payload())
        result = load_discovery_sandbox_status(self.root)
        summary = result["approval_summary"]
        self.assertTrue(summary["observe_only"])
        self.assertTrue(summary["sandbox_only"])
        self.assertTrue(summary["no_trade"])
        self.assertTrue(summary["no_official_promotion"])

    def test_approval_decisions_loaded_when_jsonl_exists(self):
        self._write(_EMERGING_PATH, self._emerging_payload())
        self._write_jsonl(_DECISIONS_PATH, [self._approval_line("NVDA")])
        result = load_discovery_sandbox_status(self.root)
        self.assertEqual(len(result["approval_decisions"]), 1)
        self.assertEqual(result["approval_decisions"][0]["symbol"], "NVDA")

    def test_approval_artifact_path_in_artifacts_dict(self):
        self._write(_EMERGING_PATH, self._emerging_payload())
        result = load_discovery_sandbox_status(self.root)
        self.assertIn("approval_decisions", result["artifacts"])

    def test_existing_governance_flags_unchanged(self):
        self._write(_EMERGING_PATH, self._emerging_payload())
        result = load_discovery_sandbox_status(self.root)
        self.assertTrue(result["observe_only"])
        self.assertTrue(result["sandbox_only"])
        self.assertFalse(result["can_execute_trades"])
        self.assertFalse(result["official_watchlist_modified"])

    def test_missing_emerging_still_returns_approval_data(self):
        # No emerging candidates file — should still return approval structure
        result = load_discovery_sandbox_status(self.root)
        self.assertIn("approval_decisions", result)
        self.assertIn("approval_summary", result)

    def _approval_line(self, symbol: str, decision: str = "keep_watching") -> dict:
        return {
            "symbol": symbol,
            "decision": decision,
            "observe_only": True,
            "sandbox_only": True,
            "no_trade": True,
            "no_official_promotion": True,
            "corroboration_level": "strong",
            "generated_at": "2026-05-02T12:00:00",
        }
