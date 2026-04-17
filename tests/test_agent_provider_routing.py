"""Provider routing tests for agent_runner without real network calls."""

from __future__ import annotations

import shutil
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.test_agent_runner_offline_mode import _make_repo


class TestAgentProviderRouting(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.root = Path(self.tmp_dir)
        _make_repo(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_daily_run_uses_selected_provider_first(self):
        from agent.agent_runner import run

        with patch("agent.agent_runner.call_provider", return_value="# provider memo") as mock_call:
            with patch.dict("os.environ", {"OLLAMA_BASE_URL": "http://localhost:11434/v1"}, clear=False):
                with self.assertLogs("stockbot.agent.runner", level="INFO") as captured:
                    result = run(
                        mode="daily",
                        offline=False,
                        provider="ollama",
                        ollama_model="gemma3:4b",
                        claude_model="claude-haiku-4-5-20251001",
                        root=self.root,
                    )

        self.assertEqual(result["mode"], "daily")
        self.assertEqual(mock_call.call_args.kwargs["provider"], "ollama")
        self.assertEqual(mock_call.call_args.kwargs["model"], "gemma3:4b")
        memo = (self.root / "outputs" / "latest" / "decision_memo.md").read_text(encoding="utf-8")
        self.assertIn("provider memo", memo)
        self.assertTrue(
            any("resolved_provider=ollama" in message for message in captured.output)
        )
        metadata = json.loads(
            (self.root / "outputs" / "latest" / "agent_llm_metadata.json").read_text(encoding="utf-8")
        )
        self.assertIn("run_id", metadata)
        self.assertIn("started_at", metadata)
        self.assertIn("completed_at", metadata)
        self.assertIn("git_commit", metadata)
        task_meta = metadata["tasks"][0]
        self.assertIn("run_id", task_meta)
        self.assertIn("started_at", task_meta)
        self.assertIn("completed_at", task_meta)
        self.assertIn("latency_ms", task_meta)
        self.assertIn("success", task_meta)
        self.assertIn("error_type", task_meta)
        self.assertIn("fallback_reason", task_meta)
        self.assertEqual(task_meta["resolved_provider"], "ollama")
        self.assertEqual(task_meta["actual_provider"], "ollama")
        self.assertEqual(task_meta["model"], "gemma3:4b")
        self.assertEqual(task_meta["base_url"], "http://localhost:11434/v1")
        self.assertTrue(task_meta["success"])
        self.assertIsNone(task_meta["error_type"])
        self.assertIsNone(task_meta["fallback_reason"])
        self.assertFalse(task_meta["fallback_triggered"])
        self.assertTrue(
            any(
                "Agent LLM summary: task=agent.daily resolved=ollama actual=ollama model=gemma3:4b llm_fallback=no"
                in message
                for message in captured.output
            )
        )

    def test_daily_run_falls_back_after_provider_failure(self):
        from agent.agent_runner import run

        def _side_effect(*, provider, model, prompt, max_tokens):
            if provider == "ollama":
                raise RuntimeError("ollama unavailable")
            return "# fallback memo"

        with patch("agent.agent_runner.call_provider", side_effect=_side_effect) as mock_call:
            with patch.dict("os.environ", {"OLLAMA_BASE_URL": "http://localhost:11434/v1"}, clear=False):
                with self.assertLogs("stockbot.agent.runner", level="INFO") as captured:
                    run(
                        mode="daily",
                        offline=False,
                        provider="ollama",
                        ollama_model="gemma3:4b",
                        claude_model="claude-haiku-4-5-20251001",
                        root=self.root,
                    )

        providers = [call.kwargs["provider"] for call in mock_call.call_args_list]
        self.assertEqual(providers[:2], ["ollama", "anthropic"])
        memo = (self.root / "outputs" / "latest" / "decision_memo.md").read_text(encoding="utf-8")
        self.assertIn("fallback memo", memo)
        metadata = json.loads(
            (self.root / "outputs" / "latest" / "agent_llm_metadata.json").read_text(encoding="utf-8")
        )
        task_meta = metadata["tasks"][0]
        self.assertEqual(task_meta["resolved_provider"], "ollama")
        self.assertEqual(task_meta["actual_provider"], "anthropic")
        self.assertTrue(task_meta["success"])
        self.assertEqual(task_meta["error_type"], "RuntimeError")
        self.assertIn("ollama failed: ollama unavailable", task_meta["fallback_reason"])
        self.assertTrue(task_meta["fallback_triggered"])
        self.assertTrue(
            any(
                "Agent LLM summary: task=agent.daily resolved=ollama actual=anthropic model=claude-haiku-4-5-20251001 llm_fallback=yes"
                in message
                for message in captured.output
            )
        )

    def test_global_override_beats_task_config(self):
        from agent.agent_runner import run

        def _side_effect(*, provider, model, prompt, max_tokens):
            raise RuntimeError(f"{provider} unavailable")

        with patch("agent.agent_runner.call_provider", side_effect=_side_effect) as mock_call:
            with patch.dict(
                "os.environ",
                {"STOCKBOT_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "test", "OPENAI_MODEL": "gpt-4o-mini"},
                clear=False,
            ):
                run(
                    mode="monthly",
                    offline=False,
                    provider=None,
                    ollama_model="gemma3:4b",
                    claude_model="claude-haiku-4-5-20251001",
                    openai_model="gpt-4o-mini",
                    root=self.root,
                    agent_config={"task_providers": {"monthly": "anthropic"}},
                )

        providers = [call.kwargs["provider"] for call in mock_call.call_args_list[:3]]
        self.assertEqual(providers, ["openai", "anthropic", "ollama"])

    def test_task_config_beats_default_monthly_routing(self):
        from agent.agent_runner import run

        def _side_effect(*, provider, model, prompt, max_tokens):
            raise RuntimeError(f"{provider} unavailable")

        with patch("agent.agent_runner.call_provider", side_effect=_side_effect) as mock_call:
            with patch.dict("os.environ", {"STOCKBOT_LLM_PROVIDER": "", "OPENAI_API_KEY": "", "OPENAI_MODEL": ""}, clear=False):
                run(
                    mode="monthly",
                    offline=False,
                    provider=None,
                    ollama_model="gemma3:4b",
                    claude_model="claude-haiku-4-5-20251001",
                    root=self.root,
                    agent_config={"task_providers": {"monthly": "ollama"}},
                )

        providers = [call.kwargs["provider"] for call in mock_call.call_args_list[:2]]
        self.assertEqual(providers, ["ollama", "anthropic"])

    def test_no_task_config_preserves_default_monthly_routing(self):
        from agent.agent_runner import run

        def _side_effect(*, provider, model, prompt, max_tokens):
            raise RuntimeError(f"{provider} unavailable")

        with patch("agent.agent_runner.call_provider", side_effect=_side_effect) as mock_call:
            with patch.dict("os.environ", {"STOCKBOT_LLM_PROVIDER": "", "OPENAI_API_KEY": "", "OPENAI_MODEL": ""}, clear=False):
                run(
                    mode="monthly",
                    offline=False,
                    provider=None,
                    ollama_model="gemma3:4b",
                    claude_model="claude-haiku-4-5-20251001",
                    root=self.root,
                    agent_config={},
                )

        providers = [call.kwargs["provider"] for call in mock_call.call_args_list[:2]]
        self.assertEqual(providers, ["anthropic", "ollama"])

    def test_standalone_task_provider_applies_when_mode_specific_key_absent(self):
        from agent.agent_runner import run

        def _side_effect(*, provider, model, prompt, max_tokens):
            raise RuntimeError(f"{provider} unavailable")

        with patch("agent.agent_runner.call_provider", side_effect=_side_effect) as mock_call:
            with patch.dict(
                "os.environ",
                {"OPENAI_API_KEY": "test", "OPENAI_MODEL": "gpt-4o-mini"},
                clear=False,
            ):
                run(
                    mode="daily",
                    offline=False,
                    provider=None,
                    ollama_model="gemma3:4b",
                    claude_model="claude-haiku-4-5-20251001",
                    openai_model="gpt-4o-mini",
                    root=self.root,
                    agent_config={"task_providers": {"standalone": "openai"}},
                )

        providers = [call.kwargs["provider"] for call in mock_call.call_args_list[:3]]
        self.assertEqual(providers, ["openai", "ollama", "anthropic"])

    def test_main_respects_env_provider_override_and_default_monthly_fallback_order(self):
        import sys
        from agent import agent_runner

        def _side_effect(*, provider, model, prompt, max_tokens):
            raise RuntimeError(f"{provider} unavailable")

        original_argv = sys.argv[:]
        try:
            (self.root / ".env").write_text(
                "STOCKBOT_LLM_PROVIDER=ollama\nOLLAMA_MODEL=gemma3:4b\nOLLAMA_BASE_URL=http://localhost:11434/v1\n",
                encoding="utf-8",
            )
            with patch("agent.agent_runner.call_provider", side_effect=_side_effect) as mock_call:
                with patch.dict("os.environ", {"OPENAI_API_KEY": "", "OPENAI_MODEL": ""}, clear=False):
                    import os
                    os.environ.pop("STOCKBOT_LLM_PROVIDER", None)
                    sys.argv = ["agent", "--mode", "monthly", "--root", str(self.root)]
                    agent_runner.main()
                forced_order = [call.kwargs["provider"] for call in mock_call.call_args_list[:2]]
                self.assertEqual(forced_order, ["ollama", "anthropic"])

            (self.root / ".env").unlink(missing_ok=True)
            with patch("agent.agent_runner.call_provider", side_effect=_side_effect) as mock_call:
                with patch.dict("os.environ", {"OPENAI_API_KEY": "", "OPENAI_MODEL": ""}, clear=False):
                    import os
                    os.environ.pop("STOCKBOT_LLM_PROVIDER", None)
                    sys.argv = ["agent", "--mode", "monthly", "--root", str(self.root)]
                    agent_runner.main()
                default_order = [call.kwargs["provider"] for call in mock_call.call_args_list[:2]]
                self.assertEqual(default_order, ["anthropic", "ollama"])
        finally:
            sys.argv = original_argv


class TestAgentCliEncoding(unittest.TestCase):

    def test_configure_stdio_utf8_reconfigures_supported_streams(self):
        import agent.agent_runner as agent_runner

        stdout = MagicMock()
        stderr = MagicMock()

        with patch.object(agent_runner.sys, "stdout", stdout), patch.object(agent_runner.sys, "stderr", stderr):
            agent_runner._configure_stdio_utf8()

        stdout.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")
        stderr.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    unittest.main(verbosity=2)
