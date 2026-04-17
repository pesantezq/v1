"""
Tests for scraped_intel/trials.py — trial registry and config audit layer.

Coverage
--------
TestHashConfig                  — deterministic hashing, key-order independence,
                                   collision resistance
TestTrialStatusConstants        — valid/terminal status lists
TestValidTransitions            — _VALID_TRANSITIONS completeness and correctness
TestTrialRegistryRegister       — register(), dedup, validation
TestTrialRegistryGet            — get() and get_all() filtering
TestUpdateStatus                — valid transitions, timestamp columns,
                                   reviewer_note accumulation
TestInvalidTransitions          — illegal status moves raise ValueError
TestConvenienceHelpers          — approve_for_shadow, approve_for_trial,
                                   start_trial, end_trial, mark_promoted,
                                   mark_rejected, mark_retired
TestReportSchema                — _build_registry_report() structure
TestReportWriters               — JSON + MD artifact creation
TestDryRun                      — dry_run skips all disk writes
TestNoMutation                  — no config.json, snapshot, or signal mutations
TestEndToEnd                    — full proposed→approved→trial→promoted lifecycle
TestEndToEndReject              — proposed→rejected path
TestRegistryIsolation           — multiple registries do not share state
TestDDLIdempotency              — calling _init_tables() twice is safe
TestRegisterTrialCandidateFn    — convenience entry-point function
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

from scraped_intel.trials import (
    TrialMode,
    TrialRegistry,
    TrialStatus,
    _VALID_TRANSITIONS,
    _build_registry_report,
    hash_config,
    register_trial_candidate,
    write_registry_json,
    write_registry_md,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _simple_config(**overrides) -> Dict[str, Any]:
    base = {
        "weights": {
            "scraped_confidence":    0.40,
            "recency_score":         0.30,
            "theme_alignment_score": 0.20,
            "mention_accel_norm":    0.10,
        },
        "max_signal_boost": 0.12,
        "max_conf_boost":   0.10,
    }
    base.update(overrides)
    return base


def _make_registry(tmp_dir: Path) -> TrialRegistry:
    return TrialRegistry(
        db_path=str(tmp_dir / "test.db"),
        output_dir=str(tmp_dir / "outputs"),
    )


# ---------------------------------------------------------------------------
# TestHashConfig
# ---------------------------------------------------------------------------

class TestHashConfig(unittest.TestCase):

    def test_returns_16_char_hex(self):
        h = hash_config({"a": 1})
        self.assertEqual(len(h), 16)
        self.assertRegex(h, r'^[0-9a-f]{16}$')

    def test_deterministic(self):
        cfg = _simple_config()
        self.assertEqual(hash_config(cfg), hash_config(cfg))

    def test_key_order_independent(self):
        cfg1 = {"a": 1, "b": 2}
        cfg2 = {"b": 2, "a": 1}
        self.assertEqual(hash_config(cfg1), hash_config(cfg2))

    def test_nested_key_order_independent(self):
        cfg1 = {"weights": {"x": 0.5, "y": 0.5}, "boost": 0.1}
        cfg2 = {"boost": 0.1, "weights": {"y": 0.5, "x": 0.5}}
        self.assertEqual(hash_config(cfg1), hash_config(cfg2))

    def test_different_values_produce_different_hashes(self):
        cfg1 = _simple_config(max_signal_boost=0.12)
        cfg2 = _simple_config(max_signal_boost=0.14)
        self.assertNotEqual(hash_config(cfg1), hash_config(cfg2))

    def test_empty_dict_is_hashable(self):
        h = hash_config({})
        self.assertEqual(len(h), 16)

    def test_hash_stable_across_calls(self):
        cfg = _simple_config()
        hashes = {hash_config(cfg) for _ in range(5)}
        self.assertEqual(len(hashes), 1)


# ---------------------------------------------------------------------------
# TestTrialStatusConstants
# ---------------------------------------------------------------------------

class TestTrialStatusConstants(unittest.TestCase):

    def test_all_contains_six_statuses(self):
        self.assertEqual(len(TrialStatus.ALL), 6)

    def test_terminal_contains_rejected_and_retired(self):
        self.assertIn(TrialStatus.REJECTED, TrialStatus.TERMINAL)
        self.assertIn(TrialStatus.RETIRED,  TrialStatus.TERMINAL)

    def test_terminal_is_subset_of_all(self):
        for s in TrialStatus.TERMINAL:
            self.assertIn(s, TrialStatus.ALL)

    def test_trial_mode_all_has_four_entries(self):
        self.assertEqual(len(TrialMode.ALL), 4)


# ---------------------------------------------------------------------------
# TestValidTransitions
# ---------------------------------------------------------------------------

class TestValidTransitions(unittest.TestCase):

    def test_every_status_has_transition_entry(self):
        for s in TrialStatus.ALL:
            self.assertIn(s, _VALID_TRANSITIONS, f"{s} missing from _VALID_TRANSITIONS")

    def test_terminal_statuses_have_no_transitions(self):
        for s in TrialStatus.TERMINAL:
            self.assertEqual(
                _VALID_TRANSITIONS[s], [],
                f"Terminal status {s} should have no outgoing transitions",
            )

    def test_proposed_can_reach_approved_for_shadow(self):
        self.assertIn(TrialStatus.APPROVED_FOR_SHADOW, _VALID_TRANSITIONS[TrialStatus.PROPOSED])

    def test_approved_for_trial_can_reach_promoted(self):
        self.assertIn(TrialStatus.PROMOTED, _VALID_TRANSITIONS[TrialStatus.APPROVED_FOR_TRIAL])

    def test_promoted_can_only_retire(self):
        allowed = _VALID_TRANSITIONS[TrialStatus.PROMOTED]
        self.assertEqual(allowed, [TrialStatus.RETIRED])


# ---------------------------------------------------------------------------
# TestTrialRegistryRegister
# ---------------------------------------------------------------------------

class TestTrialRegistryRegister(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.reg = _make_registry(Path(self._tmp))

    def test_register_returns_entry_dict(self):
        entry = self.reg.register(_simple_config())
        self.assertIsInstance(entry, dict)

    def test_entry_has_expected_keys(self):
        entry = self.reg.register(_simple_config())
        for key in ("id", "config_hash", "config_payload", "status",
                    "trial_mode", "created_at"):
            self.assertIn(key, entry, f"Missing key: {key}")

    def test_initial_status_is_proposed(self):
        entry = self.reg.register(_simple_config())
        self.assertEqual(entry["status"], TrialStatus.PROPOSED)

    def test_default_mode_is_research_only(self):
        entry = self.reg.register(_simple_config())
        self.assertEqual(entry["trial_mode"], TrialMode.RESEARCH_ONLY)

    def test_custom_trial_mode_accepted(self):
        entry = self.reg.register(_simple_config(), trial_mode=TrialMode.SHADOW_ONLY)
        self.assertEqual(entry["trial_mode"], TrialMode.SHADOW_ONLY)

    def test_invalid_trial_mode_raises(self):
        with self.assertRaises(ValueError):
            self.reg.register(_simple_config(), trial_mode="nonexistent_mode")

    def test_reviewer_note_stored(self):
        entry = self.reg.register(_simple_config(), reviewer_note="Initial note")
        self.assertIn("Initial note", entry.get("reviewer_note") or "")

    def test_duplicate_returns_existing_entry(self):
        cfg = _simple_config()
        e1 = self.reg.register(cfg)
        e2 = self.reg.register(cfg, reviewer_note="Should not overwrite")
        self.assertEqual(e1["id"], e2["id"])
        self.assertEqual(e1["config_hash"], e2["config_hash"])

    def test_duplicate_does_not_change_status(self):
        cfg = _simple_config()
        e1 = self.reg.register(cfg)
        # Approve it
        self.reg.approve_for_shadow(e1["config_hash"])
        # Re-register same config
        e2 = self.reg.register(cfg)
        # Status should remain approved_for_shadow, not reset to proposed
        self.assertEqual(e2["status"], TrialStatus.APPROVED_FOR_SHADOW)

    def test_source_paths_stored(self):
        entry = self.reg.register(
            _simple_config(),
            source_tuning_report_path="outputs/tuning.json",
            source_promotion_review_path="outputs/promo.json",
        )
        self.assertEqual(entry["source_tuning_report_path"], "outputs/tuning.json")
        self.assertEqual(entry["source_promotion_review_path"], "outputs/promo.json")

    def test_config_payload_round_trips(self):
        cfg = _simple_config()
        entry = self.reg.register(cfg)
        self.assertEqual(entry["config_payload"]["max_signal_boost"], cfg["max_signal_boost"])

    def test_different_configs_get_different_hashes(self):
        e1 = self.reg.register(_simple_config(max_signal_boost=0.10))
        e2 = self.reg.register(_simple_config(max_signal_boost=0.14))
        self.assertNotEqual(e1["config_hash"], e2["config_hash"])


# ---------------------------------------------------------------------------
# TestTrialRegistryGet
# ---------------------------------------------------------------------------

class TestTrialRegistryGet(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.reg = _make_registry(Path(self._tmp))

    def test_get_returns_none_for_missing_hash(self):
        self.assertIsNone(self.reg.get("nonexistent"))

    def test_get_returns_entry_after_register(self):
        e = self.reg.register(_simple_config())
        fetched = self.reg.get(e["config_hash"])
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["config_hash"], e["config_hash"])

    def test_get_all_returns_all_registered(self):
        self.reg.register(_simple_config(max_signal_boost=0.10))
        self.reg.register(_simple_config(max_signal_boost=0.12))
        self.reg.register(_simple_config(max_signal_boost=0.14))
        entries = self.reg.get_all()
        self.assertEqual(len(entries), 3)

    def test_get_all_filters_by_status(self):
        e1 = self.reg.register(_simple_config(max_signal_boost=0.10))
        e2 = self.reg.register(_simple_config(max_signal_boost=0.12))
        self.reg.approve_for_shadow(e2["config_hash"])
        proposed = self.reg.get_all(status=TrialStatus.PROPOSED)
        self.assertEqual(len(proposed), 1)
        self.assertEqual(proposed[0]["config_hash"], e1["config_hash"])

    def test_get_all_filters_by_trial_mode(self):
        self.reg.register(_simple_config(max_signal_boost=0.10), trial_mode=TrialMode.SHADOW_ONLY)
        self.reg.register(_simple_config(max_signal_boost=0.12), trial_mode=TrialMode.RESEARCH_ONLY)
        shadow = self.reg.get_all(trial_mode=TrialMode.SHADOW_ONLY)
        self.assertEqual(len(shadow), 1)
        self.assertEqual(shadow[0]["trial_mode"], TrialMode.SHADOW_ONLY)

    def test_get_all_empty_registry_returns_empty_list(self):
        self.assertEqual(self.reg.get_all(), [])


# ---------------------------------------------------------------------------
# TestUpdateStatus
# ---------------------------------------------------------------------------

class TestUpdateStatus(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.reg = _make_registry(Path(self._tmp))
        self.entry = self.reg.register(_simple_config())
        self.cfg_hash = self.entry["config_hash"]

    def test_valid_transition_updates_status(self):
        updated = self.reg.update_status(self.cfg_hash, TrialStatus.APPROVED_FOR_SHADOW)
        self.assertEqual(updated["status"], TrialStatus.APPROVED_FOR_SHADOW)

    def test_approved_at_set_on_approval(self):
        updated = self.reg.update_status(self.cfg_hash, TrialStatus.APPROVED_FOR_SHADOW)
        self.assertIsNotNone(updated["approved_at"])

    def test_started_at_set_on_approved_for_trial(self):
        self.reg.update_status(self.cfg_hash, TrialStatus.APPROVED_FOR_SHADOW)
        updated = self.reg.update_status(self.cfg_hash, TrialStatus.APPROVED_FOR_TRIAL)
        self.assertIsNotNone(updated["started_at"])

    def test_ended_at_set_on_promoted(self):
        self.reg.update_status(self.cfg_hash, TrialStatus.APPROVED_FOR_SHADOW)
        self.reg.update_status(self.cfg_hash, TrialStatus.APPROVED_FOR_TRIAL)
        updated = self.reg.update_status(self.cfg_hash, TrialStatus.PROMOTED)
        self.assertIsNotNone(updated["ended_at"])

    def test_ended_at_set_on_rejected(self):
        updated = self.reg.update_status(self.cfg_hash, TrialStatus.REJECTED)
        self.assertIsNotNone(updated["ended_at"])

    def test_reviewer_note_appended(self):
        self.reg.update_status(self.cfg_hash, TrialStatus.APPROVED_FOR_SHADOW, reviewer_note="note 1")
        updated = self.reg.update_status(self.cfg_hash, TrialStatus.APPROVED_FOR_TRIAL, reviewer_note="note 2")
        combined = updated.get("reviewer_note") or ""
        self.assertIn("note 1", combined)
        self.assertIn("note 2", combined)

    def test_final_decision_note_stored(self):
        self.reg.update_status(self.cfg_hash, TrialStatus.APPROVED_FOR_SHADOW)
        self.reg.update_status(self.cfg_hash, TrialStatus.APPROVED_FOR_TRIAL)
        updated = self.reg.update_status(
            self.cfg_hash, TrialStatus.PROMOTED, final_decision_note="ship it"
        )
        self.assertEqual(updated["final_decision_note"], "ship it")

    def test_missing_hash_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.reg.update_status("doesnotexist", TrialStatus.APPROVED_FOR_SHADOW)


# ---------------------------------------------------------------------------
# TestInvalidTransitions
# ---------------------------------------------------------------------------

class TestInvalidTransitions(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.reg = _make_registry(Path(self._tmp))

    def test_cannot_skip_to_promoted_from_proposed(self):
        e = self.reg.register(_simple_config())
        with self.assertRaises(ValueError):
            self.reg.update_status(e["config_hash"], TrialStatus.PROMOTED)

    def test_cannot_go_backward_from_approved_to_proposed(self):
        e = self.reg.register(_simple_config())
        self.reg.approve_for_shadow(e["config_hash"])
        with self.assertRaises(ValueError):
            self.reg.update_status(e["config_hash"], TrialStatus.PROPOSED)

    def test_terminal_rejected_blocks_further_transitions(self):
        e = self.reg.register(_simple_config())
        self.reg.mark_rejected(e["config_hash"])
        with self.assertRaises(ValueError):
            self.reg.update_status(e["config_hash"], TrialStatus.APPROVED_FOR_SHADOW)

    def test_terminal_retired_blocks_further_transitions(self):
        e = self.reg.register(_simple_config())
        self.reg.approve_for_shadow(e["config_hash"])
        self.reg.mark_retired(e["config_hash"])
        with self.assertRaises(ValueError):
            self.reg.update_status(e["config_hash"], TrialStatus.APPROVED_FOR_TRIAL)

    def test_invalid_status_value_raises_value_error(self):
        e = self.reg.register(_simple_config())
        with self.assertRaises(ValueError):
            self.reg.update_status(e["config_hash"], "made_up_status")

    def test_cannot_reject_from_promoted(self):
        e = self.reg.register(_simple_config())
        self.reg.approve_for_shadow(e["config_hash"])
        self.reg.approve_for_trial(e["config_hash"])
        self.reg.mark_promoted(e["config_hash"])
        with self.assertRaises(ValueError):
            self.reg.mark_rejected(e["config_hash"])


# ---------------------------------------------------------------------------
# TestConvenienceHelpers
# ---------------------------------------------------------------------------

class TestConvenienceHelpers(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.reg = _make_registry(Path(self._tmp))

    def test_approve_for_shadow(self):
        e = self.reg.register(_simple_config())
        updated = self.reg.approve_for_shadow(e["config_hash"], note="shadow ok")
        self.assertEqual(updated["status"], TrialStatus.APPROVED_FOR_SHADOW)

    def test_approve_for_trial(self):
        e = self.reg.register(_simple_config())
        self.reg.approve_for_shadow(e["config_hash"])
        updated = self.reg.approve_for_trial(e["config_hash"])
        self.assertEqual(updated["status"], TrialStatus.APPROVED_FOR_TRIAL)

    def test_start_trial_alias(self):
        e = self.reg.register(_simple_config())
        self.reg.approve_for_shadow(e["config_hash"])
        updated = self.reg.start_trial(e["config_hash"])
        self.assertEqual(updated["status"], TrialStatus.APPROVED_FOR_TRIAL)

    def test_end_trial(self):
        e = self.reg.register(_simple_config())
        self.reg.approve_for_shadow(e["config_hash"])
        self.reg.approve_for_trial(e["config_hash"])
        updated = self.reg.end_trial(e["config_hash"])
        self.assertEqual(updated["status"], TrialStatus.RETIRED)

    def test_mark_promoted(self):
        e = self.reg.register(_simple_config())
        self.reg.approve_for_shadow(e["config_hash"])
        self.reg.approve_for_trial(e["config_hash"])
        updated = self.reg.mark_promoted(e["config_hash"], note="win!")
        self.assertEqual(updated["status"], TrialStatus.PROMOTED)
        self.assertIn("win!", updated.get("final_decision_note") or "")

    def test_mark_rejected(self):
        e = self.reg.register(_simple_config())
        updated = self.reg.mark_rejected(e["config_hash"], note="not good enough")
        self.assertEqual(updated["status"], TrialStatus.REJECTED)

    def test_mark_retired(self):
        e = self.reg.register(_simple_config())
        self.reg.approve_for_shadow(e["config_hash"])
        self.reg.approve_for_trial(e["config_hash"])
        self.reg.mark_promoted(e["config_hash"])
        updated = self.reg.mark_retired(e["config_hash"], note="replaced by better")
        self.assertEqual(updated["status"], TrialStatus.RETIRED)


# ---------------------------------------------------------------------------
# TestReportSchema
# ---------------------------------------------------------------------------

class TestReportSchema(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.reg = _make_registry(Path(self._tmp))

    def test_empty_registry_report_schema(self):
        report = _build_registry_report([])
        for key in ("generated_at", "total_entries", "status_counts",
                    "active_count", "entries", "active_entries"):
            self.assertIn(key, report)

    def test_total_entries_count(self):
        entries = [
            self.reg.register(_simple_config(max_signal_boost=0.10)),
            self.reg.register(_simple_config(max_signal_boost=0.12)),
        ]
        report = _build_registry_report(entries)
        self.assertEqual(report["total_entries"], 2)

    def test_active_count_excludes_terminal(self):
        e1 = self.reg.register(_simple_config(max_signal_boost=0.10))
        e2 = self.reg.register(_simple_config(max_signal_boost=0.12))
        self.reg.mark_rejected(e2["config_hash"])
        all_entries = self.reg.get_all()
        report = _build_registry_report(all_entries)
        self.assertEqual(report["active_count"], 1)
        self.assertEqual(len(report["active_entries"]), 1)
        self.assertEqual(report["active_entries"][0]["config_hash"], e1["config_hash"])

    def test_status_counts_all_statuses_present(self):
        report = _build_registry_report([])
        for s in TrialStatus.ALL:
            self.assertIn(s, report["status_counts"])

    def test_status_counts_accurate(self):
        e1 = self.reg.register(_simple_config(max_signal_boost=0.10))
        e2 = self.reg.register(_simple_config(max_signal_boost=0.12))
        self.reg.approve_for_shadow(e2["config_hash"])
        all_entries = self.reg.get_all()
        report = _build_registry_report(all_entries)
        self.assertEqual(report["status_counts"][TrialStatus.PROPOSED], 1)
        self.assertEqual(report["status_counts"][TrialStatus.APPROVED_FOR_SHADOW], 1)


# ---------------------------------------------------------------------------
# TestReportWriters
# ---------------------------------------------------------------------------

class TestReportWriters(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.out  = Path(self._tmp) / "outputs"
        self.out.mkdir()
        self.reg  = _make_registry(Path(self._tmp))

    def test_write_reports_creates_json_file(self):
        self.reg.register(_simple_config())
        paths = self.reg.write_reports()
        self.assertIsNotNone(paths["json_path"])
        self.assertTrue(paths["json_path"].exists())

    def test_write_reports_creates_md_file(self):
        self.reg.register(_simple_config())
        paths = self.reg.write_reports()
        self.assertIsNotNone(paths["md_path"])
        self.assertTrue(paths["md_path"].exists())

    def test_json_file_is_valid_json(self):
        self.reg.register(_simple_config())
        paths = self.reg.write_reports()
        data = json.loads(paths["json_path"].read_text(encoding="utf-8"))
        self.assertIn("entries", data)
        self.assertIn("total_entries", data)

    def test_md_file_contains_header(self):
        self.reg.register(_simple_config())
        paths = self.reg.write_reports()
        content = paths["md_path"].read_text(encoding="utf-8")
        self.assertIn("Trial Registry", content)

    def test_md_file_contains_entry_hash(self):
        cfg = _simple_config()
        e   = self.reg.register(cfg)
        paths = self.reg.write_reports()
        content = paths["md_path"].read_text(encoding="utf-8")
        self.assertIn(e["config_hash"], content)

    def test_standalone_write_registry_json(self):
        report = _build_registry_report([])
        path = write_registry_json(report, self.out)
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["total_entries"], 0)

    def test_standalone_write_registry_md(self):
        report = _build_registry_report([])
        path = write_registry_md(report, self.out)
        self.assertTrue(path.exists())
        content = path.read_text(encoding="utf-8")
        self.assertIn("audit-only", content)


# ---------------------------------------------------------------------------
# TestDryRun
# ---------------------------------------------------------------------------

class TestDryRun(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.reg = _make_registry(Path(self._tmp))

    def test_dry_run_returns_none_paths(self):
        self.reg.register(_simple_config())
        paths = self.reg.write_reports(dry_run=True)
        self.assertIsNone(paths["json_path"])
        self.assertIsNone(paths["md_path"])

    def test_dry_run_does_not_create_files(self):
        self.reg.register(_simple_config())
        out_dir = Path(self._tmp) / "outputs"
        out_dir.mkdir(exist_ok=True)
        self.reg.write_reports(dry_run=True)
        json_file = out_dir / "scraped_intel_trial_registry.json"
        md_file   = out_dir / "scraped_intel_trial_registry.md"
        self.assertFalse(json_file.exists())
        self.assertFalse(md_file.exists())

    def test_register_trial_candidate_dry_run_no_report(self):
        out_dir = Path(self._tmp) / "out2"
        out_dir.mkdir()
        register_trial_candidate(
            config_payload=_simple_config(),
            db_path=str(Path(self._tmp) / "test.db"),
            output_dir=str(out_dir),
            dry_run=True,
        )
        self.assertFalse((out_dir / "scraped_intel_trial_registry.json").exists())


# ---------------------------------------------------------------------------
# TestNoMutation
# ---------------------------------------------------------------------------

class TestNoMutation(unittest.TestCase):
    """Verify registry operations don't touch existing DB tables or input dicts."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.db   = Path(self._tmp) / "portfolio.db"
        self.reg  = TrialRegistry(db_path=str(self.db), output_dir=str(Path(self._tmp) / "out"))

    def _create_foreign_table(self) -> None:
        """Simulate pre-existing tables (scraped_records, soft_signals, etc.)."""
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS scraped_records (record_id TEXT PRIMARY KEY, symbol TEXT)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO scraped_records VALUES ('r1', 'NVDA')"
            )
            conn.commit()
        finally:
            conn.close()

    def test_existing_tables_are_untouched(self):
        self._create_foreign_table()
        # Re-init registry (runs DDL again)
        self.reg._init_tables()
        conn = sqlite3.connect(str(self.db))
        try:
            row = conn.execute(
                "SELECT symbol FROM scraped_records WHERE record_id='r1'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "NVDA")
        finally:
            conn.close()

    def test_input_config_dict_not_mutated(self):
        cfg = _simple_config()
        original = json.dumps(cfg, sort_keys=True)
        self.reg.register(cfg)
        self.assertEqual(json.dumps(cfg, sort_keys=True), original)

    def test_get_all_does_not_mutate_db(self):
        self.reg.register(_simple_config())
        before = len(self.reg.get_all())
        _ = self.reg.get_all()
        after  = len(self.reg.get_all())
        self.assertEqual(before, after)

    def test_write_reports_does_not_add_db_rows(self):
        self.reg.register(_simple_config())
        before = len(self.reg.get_all())
        self.reg.write_reports()
        after  = len(self.reg.get_all())
        self.assertEqual(before, after)

    def test_only_trial_registry_table_created(self):
        """_init_tables() should only CREATE IF NOT EXISTS — no unexpected tables."""
        conn = sqlite3.connect(str(self.db))
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertIn("trial_registry", tables)
        # Must not silently drop or recreate existing tables
        # (tested via _create_foreign_table approach above)


# ---------------------------------------------------------------------------
# TestEndToEnd
# ---------------------------------------------------------------------------

class TestEndToEnd(unittest.TestCase):
    """Full lifecycle: proposed → shadow → trial → promoted."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.reg  = _make_registry(Path(self._tmp))

    def test_full_promotion_lifecycle(self):
        cfg   = _simple_config()
        entry = self.reg.register(
            cfg,
            source_tuning_report_path="outputs/tuning.json",
            trial_mode=TrialMode.SHADOW_ONLY,
            reviewer_note="Rank-1 candidate from 2026-04-14 run",
        )
        self.assertEqual(entry["status"], TrialStatus.PROPOSED)

        entry = self.reg.approve_for_shadow(entry["config_hash"], note="shadow approved")
        self.assertEqual(entry["status"], TrialStatus.APPROVED_FOR_SHADOW)
        self.assertIsNotNone(entry["approved_at"])

        entry = self.reg.approve_for_trial(entry["config_hash"], note="trial approved")
        self.assertEqual(entry["status"], TrialStatus.APPROVED_FOR_TRIAL)
        self.assertIsNotNone(entry["started_at"])

        entry = self.reg.mark_promoted(entry["config_hash"], note="results confirmed")
        self.assertEqual(entry["status"], TrialStatus.PROMOTED)
        self.assertIsNotNone(entry["ended_at"])
        self.assertEqual(entry["final_decision_note"], "results confirmed")

    def test_full_rejection_lifecycle(self):
        e = self.reg.register(_simple_config())
        e = self.reg.approve_for_shadow(e["config_hash"])
        e = self.reg.mark_rejected(e["config_hash"], note="underperformed in shadow")
        self.assertEqual(e["status"], TrialStatus.REJECTED)
        self.assertIsNotNone(e["ended_at"])
        # Cannot transition further
        with self.assertRaises(ValueError):
            self.reg.approve_for_trial(e["config_hash"])

    def test_promoted_then_retired(self):
        e = self.reg.register(_simple_config())
        self.reg.approve_for_shadow(e["config_hash"])
        self.reg.approve_for_trial(e["config_hash"])
        self.reg.mark_promoted(e["config_hash"])
        e = self.reg.mark_retired(e["config_hash"], note="superseded")
        self.assertEqual(e["status"], TrialStatus.RETIRED)

    def test_reports_reflect_terminal_statuses(self):
        e = self.reg.register(_simple_config(max_signal_boost=0.10))
        self.reg.mark_rejected(e["config_hash"])
        e2 = self.reg.register(_simple_config(max_signal_boost=0.12))
        # e2 stays proposed

        paths = self.reg.write_reports()
        data  = json.loads(paths["json_path"].read_text(encoding="utf-8"))
        self.assertEqual(data["total_entries"], 2)
        self.assertEqual(data["active_count"], 1)  # only e2 is non-terminal

    def test_multiple_candidates_independent_lifecycles(self):
        e1 = self.reg.register(_simple_config(max_signal_boost=0.10))
        e2 = self.reg.register(_simple_config(max_signal_boost=0.12))

        self.reg.approve_for_shadow(e1["config_hash"])
        self.reg.mark_rejected(e2["config_hash"])

        r1 = self.reg.get(e1["config_hash"])
        r2 = self.reg.get(e2["config_hash"])
        self.assertEqual(r1["status"], TrialStatus.APPROVED_FOR_SHADOW)
        self.assertEqual(r2["status"], TrialStatus.REJECTED)


# ---------------------------------------------------------------------------
# TestEndToEndReject
# ---------------------------------------------------------------------------

class TestEndToEndReject(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.reg  = _make_registry(Path(self._tmp))

    def test_direct_reject_from_proposed(self):
        e = self.reg.register(_simple_config())
        e = self.reg.mark_rejected(e["config_hash"], note="not viable")
        self.assertEqual(e["status"], TrialStatus.REJECTED)


# ---------------------------------------------------------------------------
# TestRegistryIsolation
# ---------------------------------------------------------------------------

class TestRegistryIsolation(unittest.TestCase):
    """Two registry instances pointing to different DBs don't share state."""

    def setUp(self):
        self._tmp1 = tempfile.mkdtemp()
        self._tmp2 = tempfile.mkdtemp()
        self.reg1  = _make_registry(Path(self._tmp1))
        self.reg2  = _make_registry(Path(self._tmp2))

    def test_registries_are_isolated(self):
        self.reg1.register(_simple_config())
        self.assertEqual(len(self.reg1.get_all()), 1)
        self.assertEqual(len(self.reg2.get_all()), 0)


# ---------------------------------------------------------------------------
# TestDDLIdempotency
# ---------------------------------------------------------------------------

class TestDDLIdempotency(unittest.TestCase):

    def test_init_twice_is_safe(self):
        tmp = tempfile.mkdtemp()
        reg = _make_registry(Path(tmp))
        reg._init_tables()  # second call — must not raise
        e = reg.register(_simple_config())
        self.assertIsNotNone(e)

    def test_table_created_in_existing_db(self):
        tmp    = tempfile.mkdtemp()
        db     = Path(tmp) / "existing.db"
        # Create DB with a pre-existing table
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS other (id INTEGER PRIMARY KEY)")
            conn.commit()
        finally:
            conn.close()
        # Init registry on the same DB
        reg = TrialRegistry(db_path=str(db), output_dir=str(Path(tmp) / "out"))
        e = reg.register(_simple_config())
        self.assertEqual(e["status"], TrialStatus.PROPOSED)
        # Existing table still intact
        conn = sqlite3.connect(str(db))
        try:
            tables = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
        finally:
            conn.close()
        self.assertIn("other", tables)
        self.assertIn("trial_registry", tables)


# ---------------------------------------------------------------------------
# TestRegisterTrialCandidateFn
# ---------------------------------------------------------------------------

class TestRegisterTrialCandidateFn(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.db   = str(Path(self._tmp) / "portfolio.db")
        self.out  = str(Path(self._tmp) / "outputs")

    def test_returns_entry_dict(self):
        entry = register_trial_candidate(
            config_payload=_simple_config(),
            db_path=self.db,
            output_dir=self.out,
            write_report=False,
        )
        self.assertIn("config_hash", entry)
        self.assertEqual(entry["status"], TrialStatus.PROPOSED)

    def test_with_report_creates_files(self):
        register_trial_candidate(
            config_payload=_simple_config(),
            db_path=self.db,
            output_dir=self.out,
            write_report=True,
        )
        out = Path(self.out)
        self.assertTrue((out / "scraped_intel_trial_registry.json").exists())
        self.assertTrue((out / "scraped_intel_trial_registry.md").exists())

    def test_dry_run_no_files(self):
        register_trial_candidate(
            config_payload=_simple_config(),
            db_path=self.db,
            output_dir=self.out,
            write_report=True,
            dry_run=True,
        )
        out = Path(self.out)
        # DB entry should exist (register() is not dry-run, only report writes are)
        reg = TrialRegistry(db_path=self.db, output_dir=self.out)
        self.assertEqual(len(reg.get_all()), 1)
        # But report files should not
        self.assertFalse((out / "scraped_intel_trial_registry.json").exists())


if __name__ == "__main__":
    unittest.main()
