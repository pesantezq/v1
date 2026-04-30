"""
Data governance tests for the Historical Replay subsystem.

Contracts verified:
- JSON artifacts land under outputs/backtest (never live paths)
- Markdown/text artifacts land under outputs/backtest
- Filenames are preserved exactly
- Relative structure under outputs/backtest is preserved
- write_calibration rejects output_dir pointing to outputs/latest
- write_calibration rejects output_dir pointing to outputs/policy
- write_calibration rejects output_dir pointing to outputs/portfolio
- write_attribution rejects output_dir pointing to live namespaces
- No live output path is touched during a replay write
- safe_write_json / safe_write_text are invoked via the governance module
- replay tests pass when base_dir is a temporary test directory
- DataGovernanceError is raised for out-of-namespace paths
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from portfolio_automation.data_governance import DataGovernanceError, OutputNamespace
from portfolio_automation.historical_replay.replay_reports import (
    build_historical_attribution,
    build_historical_calibration,
    write_attribution,
    write_calibration,
    _assert_safe_replay_output_dir,
    _base_dir_from_output_dir,
    _BLOCKED_LIVE_DIRS,
)
from portfolio_automation.historical_replay.replay_runner import run_replay


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolved_row(
    decision: str = "BUY",
    return_pct: float = 0.05,
    direction_correct: bool = True,
    confidence: float = 0.65,
) -> dict:
    return {
        "source": "historical_replay",
        "symbol": "AAPL",
        "decision": decision,
        "date": "2024-03-01",
        "confidence": confidence,
        "strategy": "historical_momentum_proxy",
        "band": "replay",
        "validation_status": "historical_replay",
        "resolved": True,
        "resolved_at": "2024-03-08",
        "return_pct": return_pct,
        "direction_correct": direction_correct,
        "window_days": 7,
    }


# ---------------------------------------------------------------------------
# _assert_safe_replay_output_dir
# ---------------------------------------------------------------------------

class TestAssertSafeReplayOutputDir(unittest.TestCase):

    def test_backtest_dir_is_accepted(self):
        _assert_safe_replay_output_dir(Path("outputs/backtest"))  # no exception

    def test_absolute_backtest_dir_accepted(self):
        _assert_safe_replay_output_dir(Path("/tmp/outputs/backtest"))

    def test_policy_dir_rejected(self):
        with self.assertRaises(DataGovernanceError):
            _assert_safe_replay_output_dir(Path("outputs/policy"))

    def test_latest_dir_rejected(self):
        with self.assertRaises(DataGovernanceError):
            _assert_safe_replay_output_dir(Path("outputs/latest"))

    def test_portfolio_dir_rejected(self):
        with self.assertRaises(DataGovernanceError):
            _assert_safe_replay_output_dir(Path("outputs/portfolio"))

    def test_live_dir_rejected(self):
        with self.assertRaises(DataGovernanceError):
            _assert_safe_replay_output_dir(Path("outputs/live/owner"))

    def test_users_dir_rejected(self):
        with self.assertRaises(DataGovernanceError):
            _assert_safe_replay_output_dir(Path("outputs/users/alice"))

    def test_sandbox_dir_rejected(self):
        with self.assertRaises(DataGovernanceError):
            _assert_safe_replay_output_dir(Path("outputs/sandbox"))

    def test_all_blocked_dirs_are_covered(self):
        for blocked in _BLOCKED_LIVE_DIRS:
            with self.assertRaises(DataGovernanceError, msg=f"Expected rejection for {blocked}"):
                _assert_safe_replay_output_dir(Path("outputs") / blocked)


# ---------------------------------------------------------------------------
# _base_dir_from_output_dir
# ---------------------------------------------------------------------------

class TestBaseDirFromOutputDir(unittest.TestCase):

    def test_backtest_named_dir_returns_parent(self):
        result = _base_dir_from_output_dir(Path("outputs/backtest"))
        self.assertEqual(result, Path("outputs"))

    def test_absolute_backtest_returns_parent(self):
        result = _base_dir_from_output_dir(Path("/tmp/test/outputs/backtest"))
        self.assertEqual(result, Path("/tmp/test/outputs"))

    def test_non_backtest_dir_returns_itself(self):
        result = _base_dir_from_output_dir(Path("/tmp/custom_dir"))
        self.assertEqual(result, Path("/tmp/custom_dir"))


# ---------------------------------------------------------------------------
# write_calibration — artifact location and filename
# ---------------------------------------------------------------------------

class TestWriteCalibrationGovernance(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.out_dir = self.base / "outputs" / "backtest"
        self.rows = [_resolved_row()]
        self.payload = build_historical_calibration(self.rows)

    def tearDown(self):
        self.tmp.cleanup()

    def test_json_lands_in_backtest(self):
        j, _ = write_calibration(self.payload, self.out_dir)
        self.assertIn("backtest", str(j))
        self.assertTrue(j.exists())

    def test_md_lands_in_backtest(self):
        _, m = write_calibration(self.payload, self.out_dir)
        self.assertIn("backtest", str(m))
        self.assertTrue(m.exists())

    def test_json_filename_preserved(self):
        j, _ = write_calibration(self.payload, self.out_dir)
        self.assertEqual(j.name, "historical_calibration.json")

    def test_md_filename_preserved(self):
        _, m = write_calibration(self.payload, self.out_dir)
        self.assertEqual(m.name, "historical_calibration.md")

    def test_json_is_valid(self):
        j, _ = write_calibration(self.payload, self.out_dir)
        loaded = json.loads(j.read_text())
        self.assertEqual(loaded["source"], "historical_replay")

    def test_md_contains_replay_header(self):
        _, m = write_calibration(self.payload, self.out_dir)
        content = m.read_text()
        self.assertIn("Historical replay only", content)

    def test_no_files_written_outside_backtest(self):
        policy_dir = self.base / "outputs" / "policy"
        policy_dir.mkdir(parents=True)
        write_calibration(self.payload, self.out_dir)
        self.assertFalse(any(policy_dir.iterdir()), "policy dir must remain empty")

    def test_rejects_policy_output_dir(self):
        with self.assertRaises(DataGovernanceError):
            write_calibration(self.payload, self.base / "outputs" / "policy")

    def test_rejects_latest_output_dir(self):
        with self.assertRaises(DataGovernanceError):
            write_calibration(self.payload, self.base / "outputs" / "latest")

    def test_rejects_portfolio_output_dir(self):
        with self.assertRaises(DataGovernanceError):
            write_calibration(self.payload, self.base / "outputs" / "portfolio")

    def test_rejects_live_output_dir(self):
        with self.assertRaises(DataGovernanceError):
            write_calibration(self.payload, self.base / "outputs" / "live" / "owner")

    def test_governance_safe_write_json_is_invoked(self):
        with patch(
            "portfolio_automation.historical_replay.replay_reports.safe_write_json",
            wraps=__import__(
                "portfolio_automation.data_governance", fromlist=["safe_write_json"]
            ).safe_write_json,
        ) as mock_swj:
            write_calibration(self.payload, self.out_dir)
            mock_swj.assert_called_once()
            call_args = mock_swj.call_args
            self.assertEqual(call_args.args[0], OutputNamespace.HISTORICAL)

    def test_governance_safe_write_text_is_invoked(self):
        with patch(
            "portfolio_automation.historical_replay.replay_reports.safe_write_text",
            wraps=__import__(
                "portfolio_automation.data_governance", fromlist=["safe_write_text"]
            ).safe_write_text,
        ) as mock_swt:
            write_calibration(self.payload, self.out_dir)
            mock_swt.assert_called_once()
            call_args = mock_swt.call_args
            self.assertEqual(call_args.args[0], OutputNamespace.HISTORICAL)


# ---------------------------------------------------------------------------
# write_attribution — artifact location and filename
# ---------------------------------------------------------------------------

class TestWriteAttributionGovernance(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.out_dir = self.base / "outputs" / "backtest"
        self.rows = [_resolved_row()]
        self.payload = build_historical_attribution(self.rows)

    def tearDown(self):
        self.tmp.cleanup()

    def test_json_lands_in_backtest(self):
        j, _ = write_attribution(self.payload, self.out_dir)
        self.assertIn("backtest", str(j))
        self.assertTrue(j.exists())

    def test_md_lands_in_backtest(self):
        _, m = write_attribution(self.payload, self.out_dir)
        self.assertIn("backtest", str(m))
        self.assertTrue(m.exists())

    def test_json_filename_preserved(self):
        j, _ = write_attribution(self.payload, self.out_dir)
        self.assertEqual(j.name, "historical_performance_attribution.json")

    def test_md_filename_preserved(self):
        _, m = write_attribution(self.payload, self.out_dir)
        self.assertEqual(m.name, "historical_performance_attribution.md")

    def test_json_is_valid(self):
        j, _ = write_attribution(self.payload, self.out_dir)
        loaded = json.loads(j.read_text())
        self.assertEqual(loaded["source"], "historical_replay")

    def test_rejects_policy_output_dir(self):
        with self.assertRaises(DataGovernanceError):
            write_attribution(self.payload, self.base / "outputs" / "policy")

    def test_rejects_latest_output_dir(self):
        with self.assertRaises(DataGovernanceError):
            write_attribution(self.payload, self.base / "outputs" / "latest")

    def test_rejects_portfolio_output_dir(self):
        with self.assertRaises(DataGovernanceError):
            write_attribution(self.payload, self.base / "outputs" / "portfolio")

    def test_no_files_written_outside_backtest(self):
        latest_dir = self.base / "outputs" / "latest"
        latest_dir.mkdir(parents=True)
        write_attribution(self.payload, self.out_dir)
        self.assertFalse(any(latest_dir.iterdir()), "latest dir must remain empty")


# ---------------------------------------------------------------------------
# run_replay — JSONL governance
# ---------------------------------------------------------------------------

class TestRunReplayJSONLGovernance(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.out_dir = self.base / "outputs" / "backtest"
        self.cfg = self.base / "config.json"
        self.cfg.write_text(json.dumps(
            {"portfolio": {"holdings": [{"symbol": "AAPL"}]}}
        ))

    def tearDown(self):
        self.tmp.cleanup()

    def _make_fmp(self):
        from unittest.mock import MagicMock
        from datetime import date, timedelta
        rows = []
        start = date(2024, 1, 1)
        p = 100.0
        for i in range(130):
            d = start + timedelta(days=i)
            p = round(p * 1.007, 4)
            rows.append({
                "date": d.isoformat(), "open": p, "high": p * 1.01,
                "low": p * 0.99, "close": p, "volume": 1_000_000,
            })
        client = MagicMock()
        client.get_historical_prices.return_value = list(reversed(rows))
        return client

    def test_jsonl_lands_in_backtest(self):
        fmp = self._make_fmp()
        summary = run_replay(
            days=90, output_dir=self.out_dir, dry_run=False,
            fmp_client=fmp, config_path=self.cfg, root=self.base,
        )
        jsonl = self.out_dir / "decision_outcomes_historical.jsonl"
        self.assertTrue(jsonl.exists(), "JSONL must exist under backtest")

    def test_jsonl_filename_preserved(self):
        fmp = self._make_fmp()
        run_replay(
            days=90, output_dir=self.out_dir, dry_run=False,
            fmp_client=fmp, config_path=self.cfg, root=self.base,
        )
        self.assertTrue((self.out_dir / "decision_outcomes_historical.jsonl").exists())

    def test_jsonl_rows_have_correct_source(self):
        fmp = self._make_fmp()
        run_replay(
            days=90, output_dir=self.out_dir, dry_run=False,
            fmp_client=fmp, config_path=self.cfg, root=self.base,
        )
        content = (self.out_dir / "decision_outcomes_historical.jsonl").read_text()
        rows = [json.loads(line) for line in content.splitlines() if line.strip()]
        self.assertTrue(rows)
        self.assertTrue(all(r["source"] == "historical_replay" for r in rows))

    def test_no_live_dir_created(self):
        live_dirs = ["latest", "live", "policy", "portfolio", "users"]
        fmp = self._make_fmp()
        run_replay(
            days=90, output_dir=self.out_dir, dry_run=False,
            fmp_client=fmp, config_path=self.cfg, root=self.base,
        )
        outputs_root = self.base / "outputs"
        created = {p.name for p in outputs_root.iterdir() if p.is_dir()}
        for live in live_dirs:
            self.assertNotIn(live, created, f"outputs/{live}/ must not be created by replay")

    def test_output_files_summary_only_backtest(self):
        fmp = self._make_fmp()
        summary = run_replay(
            days=90, output_dir=self.out_dir, dry_run=False,
            fmp_client=fmp, config_path=self.cfg, root=self.base,
        )
        for f in summary.get("output_files", []):
            self.assertIn("backtest", f, f"Output file {f!r} must be under backtest/")

    def test_all_five_artifacts_present(self):
        fmp = self._make_fmp()
        run_replay(
            days=90, output_dir=self.out_dir, dry_run=False,
            fmp_client=fmp, config_path=self.cfg, root=self.base,
        )
        for fname in (
            "decision_outcomes_historical.jsonl",
            "historical_calibration.json",
            "historical_calibration.md",
            "historical_performance_attribution.json",
            "historical_performance_attribution.md",
        ):
            self.assertTrue(
                (self.out_dir / fname).exists(),
                f"{fname} must exist under outputs/backtest",
            )

    def test_governance_safe_write_text_called_for_jsonl(self):
        from portfolio_automation import data_governance as dg_mod
        import portfolio_automation.historical_replay.replay_runner as runner_mod

        fmp = self._make_fmp()
        calls = []

        original = dg_mod.safe_write_text

        def recording_safe_write_text(ns, filename, content, **kwargs):
            calls.append((ns, str(filename)))
            return original(ns, filename, content, **kwargs)

        with patch.object(runner_mod, "safe_write_text", side_effect=recording_safe_write_text):
            run_replay(
                days=90, output_dir=self.out_dir, dry_run=False,
                fmp_client=fmp, config_path=self.cfg, root=self.base,
            )

        jsonl_calls = [(ns, fn) for ns, fn in calls if "jsonl" in fn]
        self.assertTrue(jsonl_calls, "safe_write_text must be called for JSONL")
        for ns, _ in jsonl_calls:
            self.assertEqual(ns, OutputNamespace.HISTORICAL)


if __name__ == "__main__":
    unittest.main()
