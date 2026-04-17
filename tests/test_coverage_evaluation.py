"""
Tests for the coverage evaluation layer:
  - coverage_tracker   (append_coverage_run, load_coverage_history)
  - coverage_evaluator (evaluate_coverage and all internal helpers)
  - coverage_report_writer (write_coverage_reports, build_coverage_memo)
"""

import json
import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helpers used by tracker tests
# ---------------------------------------------------------------------------

@dataclass
class _FakeCandidate:
    symbol: str
    label: str = "watchlist"
    score: float = 50.0
    rank: int = 1
    events: list = field(default_factory=list)
    portfolio_context: dict = field(default_factory=dict)


@dataclass
class _FakeScanResult:
    symbol: str
    price: Optional[float] = 100.0
    pct_change_1d: Optional[float] = 1.5
    rel_volume: Optional[float] = 1.2


# ---------------------------------------------------------------------------
# CoverageTracker tests
# ---------------------------------------------------------------------------

class TestCoverageTrackerAppend(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history_path = Path(self.tmp.name) / "coverage_history.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_writes_one_record_per_candidate(self):
        from coverage_tracker import append_coverage_run
        candidates = [
            _FakeCandidate("AAPL", "compounder", 80.0, 1),
            _FakeCandidate("MSFT", "momentum", 65.0, 2),
        ]
        written = append_coverage_run(
            run_id="2026-01-10_daily",
            promoted=candidates,
            history_path=self.history_path,
        )
        self.assertEqual(written, 2)
        lines = self.history_path.read_text().strip().split("\n")
        self.assertEqual(len(lines), 2)

    def test_appended_records_are_valid_json(self):
        from coverage_tracker import append_coverage_run
        candidates = [_FakeCandidate("TSLA", "watchlist", 45.0, 1)]
        append_coverage_run("2026-01-10_daily", candidates, history_path=self.history_path)
        for line in self.history_path.read_text().strip().split("\n"):
            rec = json.loads(line)
            self.assertIn("symbol", rec)
            self.assertIn("run_id", rec)
            self.assertIn("date", rec)

    def test_empty_promoted_writes_nothing(self):
        from coverage_tracker import append_coverage_run
        written = append_coverage_run(
            run_id="2026-01-10_daily",
            promoted=[],
            history_path=self.history_path,
        )
        self.assertEqual(written, 0)
        self.assertFalse(self.history_path.exists())

    def test_scan_result_price_written_to_record(self):
        from coverage_tracker import append_coverage_run
        candidates = [_FakeCandidate("NVDA")]
        scan = {"NVDA": _FakeScanResult("NVDA", price=500.0, pct_change_1d=2.5, rel_volume=1.8)}
        append_coverage_run("2026-01-10_daily", candidates, scan_by_symbol=scan,
                            history_path=self.history_path)
        rec = json.loads(self.history_path.read_text().strip())
        self.assertAlmostEqual(rec["price"], 500.0)
        self.assertAlmostEqual(rec["pct_change_1d"], 2.5)
        self.assertAlmostEqual(rec["rel_volume"], 1.8)

    def test_scan_result_as_list_is_normalised(self):
        from coverage_tracker import append_coverage_run
        candidates = [_FakeCandidate("META")]
        scan_list = [_FakeScanResult("META", price=300.0)]
        append_coverage_run("2026-01-10_daily", candidates, scan_by_symbol=scan_list,
                            history_path=self.history_path)
        rec = json.loads(self.history_path.read_text().strip())
        self.assertAlmostEqual(rec["price"], 300.0)

    def test_run_id_date_extracted_into_date_field(self):
        from coverage_tracker import append_coverage_run
        candidates = [_FakeCandidate("AMZN")]
        append_coverage_run("2026-03-15_weekly", candidates, history_path=self.history_path)
        rec = json.loads(self.history_path.read_text().strip())
        self.assertEqual(rec["date"], "2026-03-15")

    def test_drawdown_regime_stored(self):
        from coverage_tracker import append_coverage_run
        candidates = [_FakeCandidate("GOOG")]
        append_coverage_run("2026-01-10_daily", candidates, drawdown_regime="elevated",
                            history_path=self.history_path)
        rec = json.loads(self.history_path.read_text().strip())
        self.assertEqual(rec["drawdown_regime"], "elevated")

    def test_multiple_runs_append_not_overwrite(self):
        from coverage_tracker import append_coverage_run
        append_coverage_run("2026-01-10_daily", [_FakeCandidate("A")],
                            history_path=self.history_path)
        append_coverage_run("2026-01-11_daily", [_FakeCandidate("B")],
                            history_path=self.history_path)
        lines = self.history_path.read_text().strip().split("\n")
        self.assertEqual(len(lines), 2)

    def test_portfolio_context_action_bucket_written(self):
        from coverage_tracker import append_coverage_run
        cand = _FakeCandidate("X")
        cand.portfolio_context = {"action_bucket": "deploy_now"}
        append_coverage_run("2026-01-10_daily", [cand], history_path=self.history_path)
        rec = json.loads(self.history_path.read_text().strip())
        self.assertEqual(rec["action_bucket"], "deploy_now")

    def test_dict_candidates_and_scan_rows_are_supported(self):
        from coverage_tracker import append_coverage_run
        candidates = [
            {
                "symbol": "msft",
                "label": "compounder",
                "score": 0.82,
                "rank": 1,
                "events": ["BREAKOUT_PROXY"],
                "portfolio_context": {
                    "action_bucket": "scanner_confirmation",
                    "action_hint": "Prioritize review",
                },
            }
        ]
        scan_rows = [{"symbol": "MSFT", "price": 420.0, "pct_change_1d": 1.8, "rel_volume": 1.4}]
        append_coverage_run(
            "2026-01-10_daily",
            candidates,
            scan_by_symbol=scan_rows,
            history_path=self.history_path,
        )
        rec = json.loads(self.history_path.read_text().strip())
        self.assertEqual(rec["symbol"], "MSFT")
        self.assertEqual(rec["score"], 82.0)
        self.assertEqual(rec["action_hint"], "Prioritize review")


class TestCoverageTrackerLoad(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history_path = Path(self.tmp.name) / "coverage_history.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_returns_empty_list_when_file_missing(self):
        from coverage_tracker import load_coverage_history
        result = load_coverage_history(self.history_path)
        self.assertEqual(result, [])

    def test_load_returns_all_records(self):
        from coverage_tracker import append_coverage_run, load_coverage_history
        append_coverage_run("2026-01-10_daily",
                            [_FakeCandidate("A"), _FakeCandidate("B")],
                            history_path=self.history_path)
        records = load_coverage_history(self.history_path)
        self.assertEqual(len(records), 2)
        symbols = {r["symbol"] for r in records}
        self.assertEqual(symbols, {"A", "B"})

    def test_load_skips_malformed_lines(self):
        from coverage_tracker import load_coverage_history
        self.history_path.write_text('{"symbol": "A", "date": "2026-01-10"}\nNOT_JSON\n')
        records = load_coverage_history(self.history_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["symbol"], "A")

    def test_load_skips_blank_lines(self):
        from coverage_tracker import load_coverage_history
        self.history_path.write_text('{"symbol": "A", "date": "2026-01-10"}\n\n\n')
        records = load_coverage_history(self.history_path)
        self.assertEqual(len(records), 1)


class TestCoverageTrackerHelpers(unittest.TestCase):

    def test_parse_date_from_run_id_standard(self):
        from coverage_tracker import _parse_date_from_run_id
        d = _parse_date_from_run_id("2026-04-16_daily")
        self.assertEqual(d, date(2026, 4, 16))

    def test_parse_date_from_run_id_bad_format_returns_none(self):
        from coverage_tracker import _parse_date_from_run_id
        self.assertIsNone(_parse_date_from_run_id("notadate"))
        self.assertIsNone(_parse_date_from_run_id(""))
        self.assertIsNone(_parse_date_from_run_id(None))

    def test_to_scan_map_dict_passthrough(self):
        from coverage_tracker import _to_scan_map
        d = {"A": _FakeScanResult("A")}
        self.assertIs(_to_scan_map(d), d)

    def test_to_scan_map_none_returns_empty(self):
        from coverage_tracker import _to_scan_map
        self.assertEqual(_to_scan_map(None), {})

    def test_to_scan_map_list_keyed_by_symbol(self):
        from coverage_tracker import _to_scan_map
        lst = [_FakeScanResult("A"), _FakeScanResult("B")]
        m = _to_scan_map(lst)
        self.assertIn("A", m)
        self.assertIn("B", m)


# ---------------------------------------------------------------------------
# Helpers for evaluator tests — write JSONL directly
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, records: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _make_record(
    symbol: str,
    date_str: str,
    price: float,
    label: str = "watchlist",
    score: float = 50.0,
    events: list = None,
    regime: str = "normal",
    action_bucket: str = "",
    run_id: str = None,
) -> dict:
    if run_id is None:
        run_id = f"{date_str}_daily"
    return {
        "run_id": run_id,
        "date": date_str,
        "recorded_at": f"{date_str}T12:00:00+00:00",
        "symbol": symbol,
        "label": label,
        "score": score,
        "rank": 1,
        "events": events or [],
        "price": price,
        "pct_change_1d": 1.0,
        "rel_volume": 1.5,
        "drawdown_regime": regime,
        "action_bucket": action_bucket,
    }


# ---------------------------------------------------------------------------
# CoverageEvaluator tests
# ---------------------------------------------------------------------------

class TestEvaluateCoverageEmpty(unittest.TestCase):

    def test_empty_history_returns_empty_result(self):
        from coverage_evaluator import evaluate_coverage
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.jsonl"
            result = evaluate_coverage(path)
        self.assertEqual(result.total_entries, 0)
        self.assertEqual(result.attributable_entries, 0)
        self.assertGreater(len(result.data_quality_notes), 0)
        self.assertIn("No coverage history", result.data_quality_notes[0])

    def test_empty_result_has_four_hold_duration_buckets(self):
        from coverage_evaluator import evaluate_coverage, HORIZONS
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.jsonl"
            result = evaluate_coverage(path)
        self.assertEqual(len(result.by_hold_duration), len(HORIZONS))


class TestEvaluateCoverageNoObservations(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cov.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_single_entry_no_observations_not_attributable(self):
        from coverage_evaluator import evaluate_coverage
        _write_jsonl(self.path, [
            _make_record("AAPL", "2026-01-10", 150.0),
        ])
        result = evaluate_coverage(self.path)
        self.assertEqual(result.total_entries, 1)
        self.assertEqual(result.attributable_entries, 0)
        self.assertEqual(result.coverage_rate, 0.0)

    def test_no_observations_coverage_note_added(self):
        from coverage_evaluator import evaluate_coverage
        _write_jsonl(self.path, [
            _make_record("AAPL", "2026-01-10", 150.0),
        ])
        result = evaluate_coverage(self.path)
        self.assertTrue(any("coverage" in n.lower() for n in result.data_quality_notes))


class TestEvaluateCoverageWithObservations(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cov.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _entry_and_obs(self, entry_price=100.0, obs_price=105.0, days_later=5):
        entry_date = date(2026, 1, 10)
        obs_date = entry_date + timedelta(days=days_later)
        records = [
            _make_record("AAPL", entry_date.isoformat(), entry_price),
            _make_record("AAPL", obs_date.isoformat(), obs_price),
        ]
        _write_jsonl(self.path, records)

    def test_entry_becomes_attributable_after_observation(self):
        from coverage_evaluator import evaluate_coverage
        self._entry_and_obs()
        result = evaluate_coverage(self.path)
        self.assertEqual(result.total_entries, 1)
        self.assertEqual(result.attributable_entries, 1)
        self.assertEqual(result.coverage_rate, 1.0)

    def test_forward_return_5d_computed_correctly(self):
        from coverage_evaluator import evaluate_coverage, _build_outcomes
        self._entry_and_obs(entry_price=100.0, obs_price=105.0, days_later=5)
        records = json.loads(self.path.read_text().split("\n")[0])  # entry
        from coverage_tracker import load_coverage_history
        history = load_coverage_history(self.path)
        outcomes = _build_outcomes(history)
        self.assertEqual(len(outcomes), 1)
        o = outcomes[0]
        self.assertTrue(o.attributable)
        self.assertIsNotNone(o.forward_return_5d)
        self.assertAlmostEqual(o.forward_return_5d, 0.05, places=4)

    def test_hit_is_true_for_positive_return(self):
        from coverage_evaluator import _build_outcomes
        from coverage_tracker import load_coverage_history
        self._entry_and_obs(entry_price=100.0, obs_price=105.0)
        history = load_coverage_history(self.path)
        outcomes = _build_outcomes(history)
        self.assertTrue(outcomes[0].hit)

    def test_hit_is_false_for_negative_return(self):
        from coverage_evaluator import _build_outcomes
        from coverage_tracker import load_coverage_history
        self._entry_and_obs(entry_price=100.0, obs_price=95.0)
        history = load_coverage_history(self.path)
        outcomes = _build_outcomes(history)
        self.assertFalse(outcomes[0].hit)

    def test_mfe_is_non_negative(self):
        from coverage_evaluator import _build_outcomes
        from coverage_tracker import load_coverage_history
        self._entry_and_obs(entry_price=100.0, obs_price=95.0)
        history = load_coverage_history(self.path)
        outcomes = _build_outcomes(history)
        self.assertGreaterEqual(outcomes[0].mfe, 0.0)

    def test_mae_is_non_positive(self):
        from coverage_evaluator import _build_outcomes
        from coverage_tracker import load_coverage_history
        self._entry_and_obs(entry_price=100.0, obs_price=95.0)
        history = load_coverage_history(self.path)
        outcomes = _build_outcomes(history)
        self.assertLessEqual(outcomes[0].mae, 0.0)

    def test_exit_quality_computed_when_mfe_positive(self):
        from coverage_evaluator import _build_outcomes
        from coverage_tracker import load_coverage_history
        # Entry 100 → Day 3: 110 → Day 5: 107 (MFE=0.10, latest=0.07)
        entry_date = date(2026, 1, 10)
        _write_jsonl(self.path, [
            _make_record("X", entry_date.isoformat(), 100.0),
            _make_record("X", (entry_date + timedelta(days=3)).isoformat(), 110.0),
            _make_record("X", (entry_date + timedelta(days=5)).isoformat(), 107.0),
        ])
        history = load_coverage_history(self.path)
        outcomes = _build_outcomes(history)
        o = outcomes[0]
        self.assertIsNotNone(o.exit_quality)
        self.assertAlmostEqual(o.exit_quality, 0.07 / 0.10, places=2)

    def test_exit_quality_none_when_mfe_is_zero(self):
        from coverage_evaluator import _build_outcomes
        from coverage_tracker import load_coverage_history
        self._entry_and_obs(entry_price=100.0, obs_price=98.0)
        history = load_coverage_history(self.path)
        outcomes = _build_outcomes(history)
        # MFE is 0.0 (no positive return), so exit_quality should be None
        self.assertIsNone(outcomes[0].exit_quality)


class TestReEntryDetection(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cov.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_reappearance_within_30_days_is_observation_not_new_entry(self):
        from coverage_evaluator import _build_outcomes
        from coverage_tracker import load_coverage_history
        entry_date = date(2026, 1, 1)
        obs_date = entry_date + timedelta(days=15)
        _write_jsonl(self.path, [
            _make_record("SYM", entry_date.isoformat(), 100.0),
            _make_record("SYM", obs_date.isoformat(), 110.0),
        ])
        history = load_coverage_history(self.path)
        outcomes = _build_outcomes(history)
        # Only one entry, one observation
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(len(outcomes[0].observations), 1)

    def test_reappearance_after_30_days_creates_new_entry(self):
        from coverage_evaluator import _build_outcomes, MAX_TRACK_DAYS
        from coverage_tracker import load_coverage_history
        entry_date = date(2026, 1, 1)
        reentry_date = entry_date + timedelta(days=MAX_TRACK_DAYS + 1)
        _write_jsonl(self.path, [
            _make_record("SYM", entry_date.isoformat(), 100.0),
            _make_record("SYM", reentry_date.isoformat(), 120.0),
        ])
        history = load_coverage_history(self.path)
        outcomes = _build_outcomes(history)
        # Two separate entries — re-entry after gap
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes[0].entry_price, 100.0)
        self.assertEqual(outcomes[1].entry_price, 120.0)

    def test_observation_exactly_at_max_track_days_is_included(self):
        from coverage_evaluator import _build_outcomes, MAX_TRACK_DAYS
        from coverage_tracker import load_coverage_history
        entry_date = date(2026, 1, 1)
        obs_date = entry_date + timedelta(days=MAX_TRACK_DAYS)
        _write_jsonl(self.path, [
            _make_record("SYM", entry_date.isoformat(), 100.0),
            _make_record("SYM", obs_date.isoformat(), 105.0),
        ])
        history = load_coverage_history(self.path)
        outcomes = _build_outcomes(history)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(len(outcomes[0].observations), 1)


class TestBucketAggregation(unittest.TestCase):

    def _make_two_label_history(self, path):
        """Two symbols, different labels, with observations."""
        entry_date = date(2026, 1, 10)
        obs_date = entry_date + timedelta(days=5)
        _write_jsonl(path, [
            _make_record("AAPL", entry_date.isoformat(), 100.0, label="compounder", score=80.0),
            _make_record("AAPL", obs_date.isoformat(), 108.0, label="compounder"),
            _make_record("TSLA", entry_date.isoformat(), 200.0, label="momentum", score=60.0),
            _make_record("TSLA", obs_date.isoformat(), 190.0, label="momentum"),
        ])

    def test_by_label_contains_correct_label_names(self):
        from coverage_evaluator import evaluate_coverage
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cov.jsonl"
            self._make_two_label_history(path)
            result = evaluate_coverage(path)
        label_names = {b.name for b in result.by_label}
        self.assertIn("compounder", label_names)
        self.assertIn("momentum", label_names)

    def test_by_label_counts_correct(self):
        from coverage_evaluator import evaluate_coverage
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cov.jsonl"
            self._make_two_label_history(path)
            result = evaluate_coverage(path)
        by_name = {b.name: b for b in result.by_label}
        self.assertEqual(by_name["compounder"].count, 1)
        self.assertEqual(by_name["momentum"].count, 1)

    def test_by_label_hit_rate_correct(self):
        from coverage_evaluator import evaluate_coverage
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cov.jsonl"
            self._make_two_label_history(path)
            result = evaluate_coverage(path)
        by_name = {b.name: b for b in result.by_label}
        # AAPL: +8% → hit; TSLA: -5% → miss
        self.assertEqual(by_name["compounder"].hit_rate, 1.0)
        self.assertEqual(by_name["momentum"].hit_rate, 0.0)

    def test_small_sample_flagged_below_5(self):
        from coverage_evaluator import evaluate_coverage, SMALL_SAMPLE
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cov.jsonl"
            self._make_two_label_history(path)
            result = evaluate_coverage(path)
        for b in result.by_label:
            # Both buckets have only 1 attributable entry
            self.assertTrue(b.small_sample)


class TestScoreBandAggregation(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cov.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_score_bands_returned_in_fixed_order(self):
        from coverage_evaluator import evaluate_coverage
        entry = date(2026, 1, 10)
        obs = entry + timedelta(days=5)
        _write_jsonl(self.path, [
            _make_record("A", entry.isoformat(), 100.0, score=20.0),
            _make_record("A", obs.isoformat(), 110.0, score=20.0),
        ])
        result = evaluate_coverage(self.path)
        names = [b.name for b in result.by_score_band]
        self.assertEqual(names, ["low", "medium", "high"])

    def test_high_score_symbol_in_high_band(self):
        from coverage_evaluator import evaluate_coverage
        entry = date(2026, 1, 10)
        obs = entry + timedelta(days=5)
        _write_jsonl(self.path, [
            _make_record("A", entry.isoformat(), 100.0, score=85.0),
            _make_record("A", obs.isoformat(), 105.0, score=85.0),
        ])
        result = evaluate_coverage(self.path)
        by_name = {b.name: b for b in result.by_score_band}
        self.assertEqual(by_name["high"].count, 1)
        self.assertEqual(by_name["low"].count, 0)
        self.assertEqual(by_name["medium"].count, 0)


class TestEventTypeAggregation(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cov.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_events_goes_to_none_bucket(self):
        from coverage_evaluator import evaluate_coverage
        entry = date(2026, 1, 10)
        obs = entry + timedelta(days=5)
        _write_jsonl(self.path, [
            _make_record("A", entry.isoformat(), 100.0, events=[]),
            _make_record("A", obs.isoformat(), 105.0),
        ])
        result = evaluate_coverage(self.path)
        by_name = {b.name: b for b in result.by_event_type}
        self.assertIn("none", by_name)
        self.assertEqual(by_name["none"].count, 1)

    def test_multi_event_symbol_counted_in_each_bucket(self):
        from coverage_evaluator import evaluate_coverage
        entry = date(2026, 1, 10)
        obs = entry + timedelta(days=5)
        _write_jsonl(self.path, [
            _make_record("A", entry.isoformat(), 100.0,
                         events=["BREAKOUT_PROXY", "VOLUME_SPIKE"]),
            _make_record("A", obs.isoformat(), 105.0),
        ])
        result = evaluate_coverage(self.path)
        by_name = {b.name: b for b in result.by_event_type}
        self.assertIn("BREAKOUT_PROXY", by_name)
        self.assertIn("VOLUME_SPIKE", by_name)
        # Same underlying outcome contributes to both
        self.assertEqual(by_name["BREAKOUT_PROXY"].count, 1)
        self.assertEqual(by_name["VOLUME_SPIKE"].count, 1)


class TestRegimeAggregation(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cov.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_regime_buckets_created_per_regime(self):
        from coverage_evaluator import evaluate_coverage
        entry = date(2026, 1, 10)
        obs = entry + timedelta(days=5)
        _write_jsonl(self.path, [
            _make_record("A", entry.isoformat(), 100.0, regime="normal"),
            _make_record("A", obs.isoformat(), 105.0, regime="normal"),
            _make_record("B", entry.isoformat(), 200.0, regime="elevated"),
            _make_record("B", obs.isoformat(), 210.0, regime="elevated"),
        ])
        result = evaluate_coverage(self.path)
        regime_names = {b.name for b in result.by_regime}
        self.assertIn("normal", regime_names)
        self.assertIn("elevated", regime_names)


class TestHoldDurationAggregation(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cov.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_hold_duration_has_four_horizons(self):
        from coverage_evaluator import evaluate_coverage, HORIZONS
        _write_jsonl(self.path, [
            _make_record("A", "2026-01-10", 100.0),
        ])
        result = evaluate_coverage(self.path)
        self.assertEqual(len(result.by_hold_duration), len(HORIZONS))
        horizon_days = [h.horizon_days for h in result.by_hold_duration]
        self.assertEqual(sorted(horizon_days), sorted(HORIZONS))

    def test_5d_horizon_has_return_when_obs_at_day5(self):
        from coverage_evaluator import evaluate_coverage
        entry = date(2026, 1, 10)
        _write_jsonl(self.path, [
            _make_record("A", entry.isoformat(), 100.0),
            _make_record("A", (entry + timedelta(days=5)).isoformat(), 110.0),
        ])
        result = evaluate_coverage(self.path)
        h5 = next(h for h in result.by_hold_duration if h.horizon_days == 5)
        self.assertEqual(h5.count, 1)
        self.assertAlmostEqual(h5.avg_return, 0.10, places=4)


class TestNotableItems(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cov.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _multi_symbol_history(self):
        entry = date(2026, 1, 10)
        obs = entry + timedelta(days=5)
        records = []
        prices = [("A", 100, 120), ("B", 100, 115), ("C", 100, 90), ("D", 100, 85)]
        for sym, ep, op in prices:
            records.append(_make_record(sym, entry.isoformat(), float(ep)))
            records.append(_make_record(sym, obs.isoformat(), float(op)))
        _write_jsonl(self.path, records)

    def test_notable_wins_sorted_best_first(self):
        from coverage_evaluator import evaluate_coverage
        self._multi_symbol_history()
        result = evaluate_coverage(self.path)
        if result.notable_wins:
            returns = [w["forward_return_5d"] for w in result.notable_wins]
            self.assertEqual(returns, sorted(returns, reverse=True))

    def test_notable_misses_sorted_worst_first(self):
        from coverage_evaluator import evaluate_coverage
        self._multi_symbol_history()
        result = evaluate_coverage(self.path)
        if result.notable_misses:
            returns = [m["forward_return_5d"] for m in result.notable_misses]
            self.assertEqual(returns, sorted(returns))

    def test_notable_wins_have_required_keys(self):
        from coverage_evaluator import evaluate_coverage
        self._multi_symbol_history()
        result = evaluate_coverage(self.path)
        for w in result.notable_wins:
            for key in ("symbol", "label", "forward_return_5d", "mfe", "entry_date", "score"):
                self.assertIn(key, w)


class TestBucketProperties(unittest.TestCase):

    def test_hit_rate_empty_returns_none(self):
        from coverage_evaluator import Bucket
        b = Bucket(name="test")
        self.assertIsNone(b.hit_rate)

    def test_avg_return_empty_returns_none(self):
        from coverage_evaluator import Bucket
        b = Bucket(name="test")
        self.assertIsNone(b.avg_return)

    def test_avg_return_computed_correctly(self):
        from coverage_evaluator import Bucket
        b = Bucket(name="test")
        b.returns = [0.05, 0.10, -0.02]
        self.assertAlmostEqual(b.avg_return, (0.05 + 0.10 - 0.02) / 3, places=5)

    def test_hit_rate_computed_correctly(self):
        from coverage_evaluator import Bucket
        b = Bucket(name="test")
        b.returns = [0.05, -0.02, 0.03]
        b.hit_count = 2
        self.assertAlmostEqual(b.hit_rate, 2 / 3, places=4)

    def test_avg_exit_quality_empty_returns_none(self):
        from coverage_evaluator import Bucket
        b = Bucket(name="test")
        self.assertIsNone(b.avg_exit_quality)

    def test_to_dict_has_required_keys(self):
        from coverage_evaluator import Bucket
        b = Bucket(name="test")
        d = b.to_dict()
        for k in ("name", "count", "attributable", "hit_count", "hit_rate",
                  "avg_return_5d", "small_sample"):
            self.assertIn(k, d)


class TestHorizonStatsProperties(unittest.TestCase):

    def test_avg_return_empty_returns_none(self):
        from coverage_evaluator import HorizonStats
        h = HorizonStats(horizon_days=5)
        self.assertIsNone(h.avg_return)

    def test_hit_rate_empty_returns_none(self):
        from coverage_evaluator import HorizonStats
        h = HorizonStats(horizon_days=5)
        self.assertIsNone(h.hit_rate)

    def test_hit_rate_computed_correctly(self):
        from coverage_evaluator import HorizonStats
        h = HorizonStats(horizon_days=5)
        h.returns = [0.05, -0.02, 0.03, -0.01]
        self.assertAlmostEqual(h.hit_rate, 0.5, places=4)

    def test_to_dict_has_required_keys(self):
        from coverage_evaluator import HorizonStats
        h = HorizonStats(horizon_days=5)
        d = h.to_dict()
        for k in ("horizon_days", "count", "avg_return", "hit_rate"):
            self.assertIn(k, d)


class TestCoverageEvalResultToDict(unittest.TestCase):

    def test_to_dict_round_trips_through_json(self):
        from coverage_evaluator import evaluate_coverage
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.jsonl"
            result = evaluate_coverage(path)
        d = result.to_dict()
        json_str = json.dumps(d)
        round_tripped = json.loads(json_str)
        self.assertEqual(round_tripped["total_entries"], 0)
        self.assertIn("by_label", round_tripped)
        self.assertIn("generated_at", round_tripped)

    def test_to_dict_by_hold_duration_is_list(self):
        from coverage_evaluator import evaluate_coverage
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.jsonl"
            result = evaluate_coverage(path)
        d = result.to_dict()
        self.assertIsInstance(d["by_hold_duration"], list)


# ---------------------------------------------------------------------------
# CoverageReportWriter tests
# ---------------------------------------------------------------------------

class TestWriteCoverageReports(unittest.TestCase):

    def _make_empty_result(self):
        from coverage_evaluator import evaluate_coverage
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.jsonl"
            return evaluate_coverage(path)

    def test_writes_json_and_md_files(self):
        from coverage_report_writer import write_coverage_reports
        result = self._make_empty_result()
        with tempfile.TemporaryDirectory() as tmp:
            policy_dir = Path(tmp) / "policy"
            ok = write_coverage_reports(result, policy_dir=policy_dir)
            self.assertTrue(ok)
            self.assertTrue((policy_dir / "coverage_evaluation.json").exists())
            self.assertTrue((policy_dir / "coverage_evaluation.md").exists())

    def test_json_file_is_valid_json(self):
        from coverage_report_writer import write_coverage_reports
        result = self._make_empty_result()
        with tempfile.TemporaryDirectory() as tmp:
            policy_dir = Path(tmp) / "policy"
            write_coverage_reports(result, policy_dir=policy_dir)
            data = json.loads((policy_dir / "coverage_evaluation.json").read_text())
            self.assertIn("total_entries", data)
            self.assertIn("by_label", data)

    def test_md_file_has_markdown_heading(self):
        from coverage_report_writer import write_coverage_reports
        result = self._make_empty_result()
        with tempfile.TemporaryDirectory() as tmp:
            policy_dir = Path(tmp) / "policy"
            write_coverage_reports(result, policy_dir=policy_dir)
            md = (policy_dir / "coverage_evaluation.md").read_text()
            self.assertIn("# Market Coverage Evaluation", md)

    def test_md_file_can_render_action_bucket_section(self):
        from coverage_evaluator import evaluate_coverage
        from coverage_report_writer import write_coverage_reports
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cov.jsonl"
            policy_dir = Path(tmp) / "policy"
            entry = date(2026, 1, 10)
            obs = entry + timedelta(days=5)
            _write_jsonl(path, [
                _make_record("A", entry.isoformat(), 100.0, action_bucket="scanner_confirmation"),
                _make_record("A", obs.isoformat(), 105.0, action_bucket="scanner_confirmation"),
            ])
            result = evaluate_coverage(path)
            write_coverage_reports(result, policy_dir=policy_dir)
            md = (policy_dir / "coverage_evaluation.md").read_text()
        self.assertIn("## Portfolio Action Bucket Breakdown", md)

    def test_dry_run_writes_no_files(self):
        from coverage_report_writer import write_coverage_reports
        result = self._make_empty_result()
        with tempfile.TemporaryDirectory() as tmp:
            policy_dir = Path(tmp) / "policy"
            ok = write_coverage_reports(result, policy_dir=policy_dir, dry_run=True)
            self.assertTrue(ok)
            self.assertFalse((policy_dir / "coverage_evaluation.json").exists())
            self.assertFalse((policy_dir / "coverage_evaluation.md").exists())

    def test_creates_output_directory_if_missing(self):
        from coverage_report_writer import write_coverage_reports
        result = self._make_empty_result()
        with tempfile.TemporaryDirectory() as tmp:
            policy_dir = Path(tmp) / "deep" / "nested" / "policy"
            write_coverage_reports(result, policy_dir=policy_dir)
            self.assertTrue(policy_dir.exists())


class TestBuildCoverageMemo(unittest.TestCase):

    def test_memo_empty_result_no_crash(self):
        from coverage_evaluator import evaluate_coverage
        from coverage_report_writer import build_coverage_memo
        with tempfile.TemporaryDirectory() as tmp:
            result = evaluate_coverage(Path(tmp) / "missing.jsonl")
        memo = build_coverage_memo(result)
        self.assertIn("[Market Coverage Evaluation]", memo)
        self.assertIn("No coverage data", memo)

    def test_memo_with_data_includes_attribution_line(self):
        from coverage_evaluator import evaluate_coverage
        from coverage_report_writer import build_coverage_memo
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cov.jsonl"
            entry = date(2026, 1, 10)
            obs = entry + timedelta(days=5)
            _write_jsonl(path, [
                _make_record("AAPL", entry.isoformat(), 100.0, label="compounder"),
                _make_record("AAPL", obs.isoformat(), 108.0, label="compounder"),
            ])
            result = evaluate_coverage(path)
        memo = build_coverage_memo(result)
        self.assertIn("[Market Coverage Evaluation]", memo)
        self.assertIn("attributed", memo)

    def test_memo_lines_are_reasonable_count(self):
        from coverage_evaluator import evaluate_coverage
        from coverage_report_writer import build_coverage_memo
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cov.jsonl"
            entry = date(2026, 1, 10)
            obs = entry + timedelta(days=5)
            _write_jsonl(path, [
                _make_record("A", entry.isoformat(), 100.0, label="compounder", score=80.0),
                _make_record("A", obs.isoformat(), 108.0),
                _make_record("B", entry.isoformat(), 200.0, label="momentum", score=55.0),
                _make_record("B", obs.isoformat(), 190.0),
            ])
            result = evaluate_coverage(path)
        memo = build_coverage_memo(result)
        lines = [l for l in memo.split("\n") if l.strip()]
        self.assertGreaterEqual(len(lines), 2)
        self.assertLessEqual(len(lines), 8)

    def test_fmt_pct_none_returns_dash(self):
        from coverage_report_writer import _fmt_pct
        self.assertEqual(_fmt_pct(None), "—")

    def test_fmt_pct_positive_shows_plus(self):
        from coverage_report_writer import _fmt_pct
        self.assertIn("+", _fmt_pct(0.05))

    def test_fmt_pct_negative_shows_minus(self):
        from coverage_report_writer import _fmt_pct
        result = _fmt_pct(-0.03)
        self.assertIn("-", result)


# ---------------------------------------------------------------------------
# Integration: tracker → evaluator → writer pipeline
# ---------------------------------------------------------------------------

class TestIntegrationPipeline(unittest.TestCase):

    def test_full_pipeline_runs_without_error(self):
        """
        Simulate a two-run pipeline:
        Run 1: promote AAPL and TSLA
        Run 2: both re-appear with new prices → observations created
        Evaluate and write reports.
        """
        from coverage_tracker import append_coverage_run
        from coverage_evaluator import evaluate_coverage
        from coverage_report_writer import write_coverage_reports

        with tempfile.TemporaryDirectory() as tmp:
            hist_path = Path(tmp) / "coverage_history.jsonl"
            policy_dir = Path(tmp) / "policy"

            # Use direct JSONL writes for test determinism
            entry = date(2026, 1, 10)
            obs = entry + timedelta(days=5)
            _write_jsonl(hist_path, [
                _make_record("AAPL", entry.isoformat(), 150.0, "compounder", 80.0),
                _make_record("AAPL", obs.isoformat(), 162.0, "compounder", 78.0),
                _make_record("TSLA", entry.isoformat(), 250.0, "momentum", 60.0),
                _make_record("TSLA", obs.isoformat(), 240.0, "momentum", 58.0),
            ])

            result = evaluate_coverage(hist_path)
            self.assertEqual(result.total_entries, 2)
            self.assertEqual(result.attributable_entries, 2)
            self.assertEqual(result.coverage_rate, 1.0)

            ok = write_coverage_reports(result, policy_dir=policy_dir)
            self.assertTrue(ok)
            self.assertTrue((policy_dir / "coverage_evaluation.json").exists())

            # Verify AAPL hit (return +8%), TSLA miss (return -4%)
            by_name = {b.name: b for b in result.by_label}
            self.assertIn("compounder", by_name)
            self.assertIn("momentum", by_name)
            self.assertEqual(by_name["compounder"].hit_rate, 1.0)
            self.assertEqual(by_name["momentum"].hit_rate, 0.0)

    def test_pipeline_returns_green_with_no_history(self):
        from coverage_evaluator import evaluate_coverage
        from coverage_report_writer import write_coverage_reports, build_coverage_memo
        with tempfile.TemporaryDirectory() as tmp:
            result = evaluate_coverage(Path(tmp) / "nonexistent.jsonl")
            ok = write_coverage_reports(result, policy_dir=Path(tmp) / "policy")
            memo = build_coverage_memo(result)
        self.assertTrue(ok)
        self.assertIsInstance(memo, str)
        self.assertGreater(len(memo), 0)


if __name__ == "__main__":
    unittest.main()
