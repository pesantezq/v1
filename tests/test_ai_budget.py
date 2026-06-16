"""
Tests for portfolio_automation/ai_budget.py

Coverage:
- Cost estimation (known model, unknown model, free provider)
- Budget checking (under limit, at warning threshold, exceeded)
- observe_only mode never blocks
- Non-observe mode blocks on exceeded limit
- Disabled config never blocks
- Event JSONL persistence (write path, append, policy namespace)
- Event loader (missing file, malformed lines, max_events cap)
- Summary writer (JSON + MD artifacts, zero events)
- with_ai_budget context manager (allowed, blocked, observe_only)
- No live/backtest/sandbox paths used
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from portfolio_automation.ai_budget import (
    AIBudgetConfig,
    AIBudgetExceeded,
    AIBudgetSummary,
    AIUsageEvent,
    check_ai_budget,
    estimate_ai_cost,
    load_recent_ai_usage_events,
    record_ai_usage_event,
    with_ai_budget,
    write_ai_budget_summary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(**kwargs) -> AIUsageEvent:
    from datetime import datetime, timezone
    defaults = dict(
        timestamp=datetime.now(timezone.utc).isoformat(),
        task_name="test_task",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        estimated_cost_usd=0.0001,
        allowed=True,
    )
    defaults.update(kwargs)
    return AIUsageEvent(**defaults)


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

class TestEstimateAiCost(unittest.TestCase):

    def test_known_haiku_model(self):
        cost = estimate_ai_cost("anthropic", "claude-haiku-4-5-20251001", 1_000_000, 0)
        self.assertAlmostEqual(cost, 1.00, places=2)

    def test_known_haiku_output_tokens(self):
        cost = estimate_ai_cost("anthropic", "claude-haiku-4-5-20251001", 0, 1_000_000)
        self.assertAlmostEqual(cost, 5.00, places=2)

    def test_known_gpt4o_mini(self):
        cost = estimate_ai_cost("openai", "gpt-4o-mini", 1_000_000, 0)
        self.assertAlmostEqual(cost, 0.15, places=2)

    def test_known_opus_model(self):
        cost = estimate_ai_cost("anthropic", "claude-opus-4-7", 1_000_000, 0)
        self.assertAlmostEqual(cost, 15.00, places=2)

    def test_small_token_count(self):
        # 1000 prompt tokens with haiku: 1000/1_000_000 * 1.00 = 0.001
        cost = estimate_ai_cost("anthropic", "claude-haiku-4-5-20251001", 1000, 0)
        self.assertAlmostEqual(cost, 0.001, places=6)

    def test_combined_input_and_output(self):
        # 500k input * 1.00/M + 200k output * 5.00/M = 0.50 + 1.00 = 1.50
        cost = estimate_ai_cost("anthropic", "claude-haiku-4-5-20251001", 500_000, 200_000)
        self.assertAlmostEqual(cost, 1.50, places=4)

    def test_free_provider_returns_zero(self):
        # "local" is now the sole free provider (ollama removed in OpenAI refactor).
        cost = estimate_ai_cost("local", "some-model", 1_000_000, 1_000_000)
        self.assertEqual(cost, 0.0)

    def test_local_provider_returns_zero(self):
        cost = estimate_ai_cost("local", "some-model", 1_000_000, 1_000_000)
        self.assertEqual(cost, 0.0)

    def test_free_provider_case_insensitive(self):
        cost = estimate_ai_cost("LOCAL", "some-model", 500_000, 500_000)
        self.assertEqual(cost, 0.0)

    # test_known_free_model_returns_zero deleted: gemma3:4b free-pricing entry was
    # removed in the ollama->OpenAI refactor; no remaining known-free model to assert.

    def test_unknown_model_returns_zero(self):
        cost = estimate_ai_cost("somevendor", "unknown-model-xyz", 1_000_000, 1_000_000)
        self.assertEqual(cost, 0.0)

    def test_none_provider_none_model_returns_zero(self):
        cost = estimate_ai_cost(None, None, 1000, 1000)
        self.assertEqual(cost, 0.0)

    def test_negative_tokens_clamped_to_zero(self):
        cost = estimate_ai_cost("anthropic", "claude-haiku-4-5-20251001", -100, -100)
        self.assertEqual(cost, 0.0)


# ---------------------------------------------------------------------------
# check_ai_budget: under-limit, warning, exceeded
# ---------------------------------------------------------------------------

class TestCheckAiBudgetAllowed(unittest.TestCase):

    def test_no_limits_always_allowed(self):
        cfg = AIBudgetConfig()
        event = check_ai_budget("task", provider="anthropic", model="claude-haiku-4-5-20251001",
                                prompt_tokens=1000, completion_tokens=200, config=cfg)
        self.assertTrue(event.allowed)
        self.assertIsNone(event.blocked_reason)

    def test_under_daily_limit_allowed(self):
        cfg = AIBudgetConfig(daily_cost_limit_usd=1.00)
        event = check_ai_budget("task", model="claude-haiku-4-5-20251001",
                                prompt_tokens=1000, completion_tokens=200, config=cfg,
                                _current_daily_cost_usd=0.0)
        self.assertTrue(event.allowed)

    def test_under_monthly_limit_allowed(self):
        cfg = AIBudgetConfig(monthly_cost_limit_usd=10.00)
        event = check_ai_budget("task", model="claude-haiku-4-5-20251001",
                                prompt_tokens=1000, completion_tokens=200, config=cfg)
        self.assertTrue(event.allowed)

    def test_tokens_recorded_correctly(self):
        event = check_ai_budget("task", model="gpt-4o-mini",
                                prompt_tokens=300, completion_tokens=150)
        self.assertEqual(event.prompt_tokens, 300)
        self.assertEqual(event.completion_tokens, 150)
        self.assertEqual(event.total_tokens, 450)


class TestCheckAiBudgetWarning(unittest.TestCase):

    def test_daily_warn_at_threshold(self):
        # warn_at_daily_cost_pct=0.80, limit=1.00 → warn when >= 0.80
        cfg = AIBudgetConfig(daily_cost_limit_usd=1.00, warn_at_daily_cost_pct=0.80)
        # current = 0.79, adding a tiny cost would bring to ~0.79 (under), so use 0.82
        event = check_ai_budget("task", config=cfg,
                                _current_daily_cost_usd=0.82,
                                prompt_tokens=0, completion_tokens=0)
        warnings = event.metadata.get("budget_warnings", [])
        self.assertTrue(any("Daily cost at" in w for w in warnings))

    def test_monthly_warn_at_threshold(self):
        cfg = AIBudgetConfig(monthly_cost_limit_usd=5.00, warn_at_monthly_cost_pct=0.80)
        event = check_ai_budget("task", config=cfg,
                                _current_monthly_cost_usd=4.50,
                                prompt_tokens=0, completion_tokens=0)
        warnings = event.metadata.get("budget_warnings", [])
        self.assertTrue(any("Monthly cost at" in w for w in warnings))


class TestCheckAiBudgetExceeded(unittest.TestCase):

    def test_observe_only_never_blocks(self):
        cfg = AIBudgetConfig(daily_cost_limit_usd=0.001, observe_only=True)
        event = check_ai_budget("task", model="claude-opus-4-7",
                                prompt_tokens=1_000_000, completion_tokens=0,
                                config=cfg, _current_daily_cost_usd=0.0)
        # Cost is $15 >> limit $0.001, but observe_only → still allowed
        self.assertTrue(event.allowed)

    def test_observe_only_records_budget_warning(self):
        cfg = AIBudgetConfig(daily_cost_limit_usd=0.001, observe_only=True)
        event = check_ai_budget("task", model="claude-opus-4-7",
                                prompt_tokens=1_000_000, completion_tokens=0,
                                config=cfg)
        warnings = event.metadata.get("budget_warnings", [])
        self.assertTrue(len(warnings) > 0)

    def test_non_observe_blocks_on_daily_exceeded(self):
        cfg = AIBudgetConfig(daily_cost_limit_usd=0.001, observe_only=False)
        event = check_ai_budget("task", model="claude-opus-4-7",
                                prompt_tokens=1_000_000, completion_tokens=0,
                                config=cfg, _current_daily_cost_usd=0.0)
        self.assertFalse(event.allowed)
        self.assertIsNotNone(event.blocked_reason)
        self.assertIn("Daily cost limit", event.blocked_reason)

    def test_non_observe_blocks_on_monthly_exceeded(self):
        # Explicitly disable the default daily_cost_limit_usd so this test
        # exercises the monthly-cap path in isolation (else the daily-cap
        # default of $2/day fires first on the $15 call cost).
        cfg = AIBudgetConfig(
            monthly_cost_limit_usd=0.001,
            daily_cost_limit_usd=None,
            observe_only=False,
        )
        event = check_ai_budget("task", model="claude-opus-4-7",
                                prompt_tokens=1_000_000, completion_tokens=0,
                                config=cfg, _current_monthly_cost_usd=0.0)
        self.assertFalse(event.allowed)
        self.assertIn("Monthly cost limit", event.blocked_reason)

    def test_non_observe_blocks_on_token_limit_exceeded(self):
        cfg = AIBudgetConfig(daily_token_limit=100, observe_only=False)
        event = check_ai_budget("task", config=cfg,
                                prompt_tokens=200, completion_tokens=0,
                                _current_daily_tokens=0)
        self.assertFalse(event.allowed)
        self.assertIn("token limit", event.blocked_reason)

    def test_disabled_config_never_blocks(self):
        cfg = AIBudgetConfig(enabled=False, daily_cost_limit_usd=0.0001, observe_only=False)
        event = check_ai_budget("task", model="claude-opus-4-7",
                                prompt_tokens=1_000_000, completion_tokens=0,
                                config=cfg)
        self.assertTrue(event.allowed)
        self.assertIsNone(event.blocked_reason)


class TestCheckAiBudgetUnknownPricing(unittest.TestCase):

    def test_unknown_model_tagged_in_metadata(self):
        event = check_ai_budget("task", provider="mystery", model="unknown-llm-xyz",
                                prompt_tokens=500, completion_tokens=100)
        self.assertTrue(event.metadata.get("unknown_pricing"))
        self.assertEqual(event.estimated_cost_usd, 0.0)

    def test_unknown_model_still_allowed(self):
        event = check_ai_budget("task", provider="mystery", model="unknown-llm-xyz",
                                prompt_tokens=500, completion_tokens=100)
        self.assertTrue(event.allowed)

    def test_zero_tokens_no_unknown_pricing_flag(self):
        # Zero-token event for unknown model shouldn't set unknown_pricing
        event = check_ai_budget("task", provider="mystery", model="unknown-llm-xyz",
                                prompt_tokens=0, completion_tokens=0)
        self.assertFalse(event.metadata.get("unknown_pricing", False))


# ---------------------------------------------------------------------------
# with_ai_budget context manager
# ---------------------------------------------------------------------------

class TestWithAiBudgetContextManager(unittest.TestCase):

    def test_allowed_under_budget(self):
        cfg = AIBudgetConfig(daily_cost_limit_usd=10.00, observe_only=True)
        with with_ai_budget("test", model="gpt-4o-mini",
                            estimated_prompt_tokens=100,
                            estimated_completion_tokens=50,
                            config=cfg) as ev:
            self.assertTrue(ev.allowed)

    def test_observe_only_does_not_raise_on_exceeded(self):
        cfg = AIBudgetConfig(daily_cost_limit_usd=0.0001, observe_only=True)
        # Should not raise even when budget is exceeded
        try:
            with with_ai_budget("test", model="claude-opus-4-7",
                                estimated_prompt_tokens=1_000_000,
                                estimated_completion_tokens=0,
                                config=cfg) as ev:
                self.assertIsInstance(ev, AIUsageEvent)
        except AIBudgetExceeded:
            self.fail("with_ai_budget raised AIBudgetExceeded in observe_only mode")

    def test_non_observe_raises_when_exceeded(self):
        cfg = AIBudgetConfig(daily_cost_limit_usd=0.0001, observe_only=False)
        with self.assertRaises(AIBudgetExceeded):
            with with_ai_budget("test", model="claude-opus-4-7",
                                estimated_prompt_tokens=1_000_000,
                                estimated_completion_tokens=0,
                                config=cfg):
                pass

    def test_non_observe_allowed_under_budget(self):
        cfg = AIBudgetConfig(daily_cost_limit_usd=100.00, observe_only=False)
        with with_ai_budget("test", model="gpt-4o-mini",
                            estimated_prompt_tokens=100,
                            estimated_completion_tokens=50,
                            config=cfg) as ev:
            self.assertTrue(ev.allowed)

    def test_context_manager_does_not_suppress_exceptions(self):
        cfg = AIBudgetConfig()
        with self.assertRaises(ValueError):
            with with_ai_budget("test", config=cfg):
                raise ValueError("inner error")

    def test_returns_usage_event(self):
        cfg = AIBudgetConfig()
        with with_ai_budget("task_x", provider="openai", model="gpt-4o-mini",
                            estimated_prompt_tokens=200,
                            estimated_completion_tokens=100,
                            config=cfg) as ev:
            self.assertIsInstance(ev, AIUsageEvent)
            self.assertEqual(ev.task_name, "task_x")
            self.assertEqual(ev.model, "gpt-4o-mini")


# ---------------------------------------------------------------------------
# Event persistence: record_ai_usage_event
# ---------------------------------------------------------------------------

class TestRecordAiUsageEvent(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_writes_to_policy_namespace(self):
        event = _make_event(task_name="persist_test")
        path = record_ai_usage_event(event, base_dir=self.tmpdir)
        self.assertIn("policy", str(path))
        self.assertTrue(path.exists())

    def test_jsonl_filename(self):
        event = _make_event()
        path = record_ai_usage_event(event, base_dir=self.tmpdir)
        self.assertEqual(path.name, "ai_usage_events.jsonl")

    def test_event_is_valid_json(self):
        event = _make_event(task_name="json_check")
        path = record_ai_usage_event(event, base_dir=self.tmpdir)
        lines = path.read_text().splitlines()
        self.assertTrue(len(lines) >= 1)
        d = json.loads(lines[-1])
        self.assertEqual(d["task_name"], "json_check")

    def test_multiple_events_appended(self):
        for i in range(3):
            record_ai_usage_event(_make_event(task_name=f"event_{i}"), base_dir=self.tmpdir)
        path = Path(self.tmpdir) / "policy" / "ai_usage_events.jsonl"
        lines = [l for l in path.read_text().splitlines() if l.strip()]
        self.assertEqual(len(lines), 3)

    def test_does_not_write_to_live_paths(self):
        event = _make_event()
        path = record_ai_usage_event(event, base_dir=self.tmpdir)
        for forbidden in ("latest", "backtest", "sandbox"):
            self.assertNotIn(forbidden, str(path))

    def test_contains_all_required_fields(self):
        event = _make_event(provider="openai", model="gpt-4o-mini", run_id="run-abc")
        path = record_ai_usage_event(event, base_dir=self.tmpdir)
        d = json.loads(path.read_text().splitlines()[-1])
        for field in ("timestamp", "task_name", "provider", "model", "prompt_tokens",
                      "completion_tokens", "total_tokens", "estimated_cost_usd", "allowed"):
            self.assertIn(field, d)


# ---------------------------------------------------------------------------
# Event loader: load_recent_ai_usage_events
# ---------------------------------------------------------------------------

class TestLoadRecentAiUsageEvents(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.jsonl_path = Path(self.tmpdir) / "test_events.jsonl"

    def test_missing_file_returns_empty_list(self):
        result = load_recent_ai_usage_events(Path(self.tmpdir) / "nonexistent.jsonl")
        self.assertEqual(result, [])

    def test_empty_file_returns_empty_list(self):
        self.jsonl_path.write_text("")
        result = load_recent_ai_usage_events(self.jsonl_path)
        self.assertEqual(result, [])

    def test_valid_lines_loaded(self):
        event = _make_event(task_name="load_test")
        record_ai_usage_event(event, base_dir=self.tmpdir)
        path = Path(self.tmpdir) / "policy" / "ai_usage_events.jsonl"
        result = load_recent_ai_usage_events(path)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].task_name, "load_test")

    def test_malformed_lines_skipped(self):
        self.jsonl_path.write_text(
            'not json at all\n'
            '{"task_name": "valid_event", "timestamp": "2026-01-01T00:00:00+00:00", '
            '"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, '
            '"estimated_cost_usd": 0.0, "allowed": true}\n'
            '{broken json\n'
        )
        result = load_recent_ai_usage_events(self.jsonl_path)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].task_name, "valid_event")

    def test_max_events_cap(self):
        for i in range(10):
            record_ai_usage_event(_make_event(task_name=f"e{i}"), base_dir=self.tmpdir)
        path = Path(self.tmpdir) / "policy" / "ai_usage_events.jsonl"
        result = load_recent_ai_usage_events(path, max_events=5)
        self.assertEqual(len(result), 5)

    def test_returns_most_recent_events(self):
        # Write 6 events; with max_events=3, last 3 should be returned
        for i in range(6):
            record_ai_usage_event(_make_event(task_name=f"task_{i}"), base_dir=self.tmpdir)
        path = Path(self.tmpdir) / "policy" / "ai_usage_events.jsonl"
        result = load_recent_ai_usage_events(path, max_events=3)
        names = [e.task_name for e in result]
        self.assertIn("task_5", names)
        self.assertNotIn("task_0", names)

    def test_blank_lines_tolerated(self):
        self.jsonl_path.write_text(
            '\n\n'
            '{"task_name": "ok", "timestamp": "2026-01-01T00:00:00+00:00", '
            '"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, '
            '"estimated_cost_usd": 0.0, "allowed": true}\n'
            '\n'
        )
        result = load_recent_ai_usage_events(self.jsonl_path)
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# Summary writer: write_ai_budget_summary
# ---------------------------------------------------------------------------

class TestWriteAiBudgetSummary(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_zero_events_writes_successfully(self):
        summary = write_ai_budget_summary([], base_dir=self.tmpdir)
        self.assertIsInstance(summary, AIBudgetSummary)
        self.assertEqual(summary.daily_token_total, 0)
        self.assertEqual(summary.daily_cost_total_usd, 0.0)

    def test_json_artifact_written_to_latest(self):
        write_ai_budget_summary([], base_dir=self.tmpdir)
        json_path = Path(self.tmpdir) / "latest" / "ai_budget_summary.json"
        self.assertTrue(json_path.exists())

    def test_md_artifact_written_to_latest(self):
        write_ai_budget_summary([], base_dir=self.tmpdir)
        md_path = Path(self.tmpdir) / "latest" / "ai_budget_summary.md"
        self.assertTrue(md_path.exists())

    def test_json_artifact_is_valid_json(self):
        write_ai_budget_summary([], base_dir=self.tmpdir)
        json_path = Path(self.tmpdir) / "latest" / "ai_budget_summary.json"
        d = json.loads(json_path.read_text())
        self.assertIn("generated_at", d)
        self.assertIn("observe_only", d)
        self.assertIn("daily_cost_total_usd", d)
        self.assertIn("summary_line", d)

    def test_json_artifact_observe_only_true(self):
        cfg = AIBudgetConfig(observe_only=True)
        write_ai_budget_summary([], config=cfg, base_dir=self.tmpdir)
        d = json.loads((Path(self.tmpdir) / "latest" / "ai_budget_summary.json").read_text())
        self.assertTrue(d["observe_only"])

    def test_no_policy_artifact_written(self):
        write_ai_budget_summary([], base_dir=self.tmpdir)
        policy_dir = Path(self.tmpdir) / "policy"
        if policy_dir.exists():
            for f in policy_dir.iterdir():
                self.assertNotIn("budget_summary", f.name)

    def test_cost_totals_computed_from_events(self):
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).isoformat()
        events = [
            _make_event(timestamp=now_ts, estimated_cost_usd=0.01, total_tokens=100),
            _make_event(timestamp=now_ts, estimated_cost_usd=0.02, total_tokens=200),
        ]
        summary = write_ai_budget_summary(events, base_dir=self.tmpdir)
        self.assertAlmostEqual(summary.daily_cost_total_usd, 0.03, places=6)
        self.assertEqual(summary.daily_token_total, 300)

    def test_summary_line_non_empty(self):
        summary = write_ai_budget_summary([], base_dir=self.tmpdir)
        self.assertIsInstance(summary.summary_line, str)
        self.assertTrue(len(summary.summary_line) > 0)

    def test_disabled_config_summary_line(self):
        cfg = AIBudgetConfig(enabled=False)
        summary = write_ai_budget_summary([], config=cfg, base_dir=self.tmpdir)
        self.assertIn("disabled", summary.summary_line.lower())

    def test_warning_flag_set_near_limit(self):
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).isoformat()
        cfg = AIBudgetConfig(daily_cost_limit_usd=0.10, warn_at_daily_cost_pct=0.80)
        events = [_make_event(timestamp=now_ts, estimated_cost_usd=0.09, total_tokens=100)]
        summary = write_ai_budget_summary(events, config=cfg, base_dir=self.tmpdir)
        self.assertTrue(summary.warning)

    def test_md_contains_summary_section(self):
        write_ai_budget_summary([], base_dir=self.tmpdir)
        md_content = (Path(self.tmpdir) / "latest" / "ai_budget_summary.md").read_text()
        self.assertIn("## Summary", md_content)
        self.assertIn("AI Budget Summary", md_content)

    def test_blocked_flag_when_event_not_allowed(self):
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).isoformat()
        events = [_make_event(timestamp=now_ts, allowed=False,
                              estimated_cost_usd=0.05, total_tokens=50)]
        summary = write_ai_budget_summary(events, base_dir=self.tmpdir)
        self.assertTrue(summary.blocked)


# ---------------------------------------------------------------------------
# Output namespace correctness (no live/backtest/sandbox contamination)
# ---------------------------------------------------------------------------

class TestOutputNamespaceSafety(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_event_log_not_in_live_namespace(self):
        event = _make_event()
        path = record_ai_usage_event(event, base_dir=self.tmpdir)
        for forbidden_ns in ("latest", "backtest", "sandbox", "portfolio"):
            self.assertNotIn(forbidden_ns, str(path).lower().replace(self.tmpdir.lower(), ""))

    def test_summary_not_in_policy_namespace(self):
        write_ai_budget_summary([], base_dir=self.tmpdir)
        policy_dir = Path(self.tmpdir) / "policy"
        # Summary files must be in latest, not policy
        if policy_dir.exists():
            for f in policy_dir.iterdir():
                self.assertFalse(f.name.endswith("_summary.json"),
                                 f"Found summary in policy namespace: {f.name}")

    def test_event_log_in_policy_namespace(self):
        event = _make_event()
        path = record_ai_usage_event(event, base_dir=self.tmpdir)
        relative = str(path).replace(self.tmpdir, "").lower()
        self.assertIn("policy", relative)


if __name__ == "__main__":
    unittest.main()
