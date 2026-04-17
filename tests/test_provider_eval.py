from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.provider_eval import (
    _artifact_filename,
    _build_eval_row,
    _run_and_collect,
    _write_eval_csv,
    _write_eval_summary,
)


class TestProviderEval(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.root = Path(self.tmp_dir)
        (self.root / "outputs" / "latest").mkdir(parents=True, exist_ok=True)
        self.eval_dir = self.root / "outputs" / "evals" / "test_eval"

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_build_eval_row_contains_manual_scoring_fields(self):
        row = _build_eval_row(
            task="agent_daily",
            requested_provider="ollama",
            metadata={
                "run_id": "run-1",
                "resolved_provider": "ollama",
                "actual_provider": "ollama",
                "actual_model": "gemma3:4b",
                "latency_ms": 1234,
                "success": True,
                "fallback_triggered": False,
                "actual_base_url": "http://localhost:11434/v1",
                "error_type": None,
                "fallback_reason": None,
                "git_commit": "abc1234",
            },
            output_file="outputs/evals/test_eval/artifacts/agent_daily__ollama.md",
        )

        self.assertEqual(row["run_id"], "run-1")
        self.assertEqual(row["provider"], "ollama")
        self.assertEqual(row["model"], "gemma3:4b")
        self.assertEqual(row["latency_ms"], 1234)
        self.assertTrue(row["success"])
        self.assertEqual(row["manual_score_relevance"], "")
        self.assertEqual(row["manual_score_clarity"], "")
        self.assertEqual(row["manual_score_structure"], "")
        self.assertEqual(row["manual_score_actionability"], "")
        self.assertEqual(row["manual_score_hallucination_risk"], "")

    def test_build_eval_row_includes_fallback_note(self):
        row = _build_eval_row(
            task="agent_daily",
            requested_provider="ollama",
            metadata={
                "run_id": "run-2",
                "resolved_provider": "ollama",
                "actual_provider": "anthropic",
                "actual_model": "claude-haiku-4-5-20251001",
                "latency_ms": 2200,
                "success": True,
                "fallback_triggered": True,
                "actual_base_url": "(n/a)",
                "error_type": "RuntimeError",
                "fallback_reason": "ollama failed: timeout",
                "git_commit": "abc1234",
            },
            output_file="outputs/evals/test_eval/artifacts/agent_daily__ollama.md",
        )

        self.assertEqual(row["provider"], "anthropic")
        self.assertTrue(row["fallback_triggered"])
        self.assertIn("actual_provider=anthropic", row["notes"])
        self.assertIn("ollama failed: timeout", row["notes"])

    def test_artifact_filename_reflects_requested_and_actual_provider(self):
        same = _artifact_filename(
            task="agent_daily",
            requested_provider="ollama",
            actual_provider="ollama",
            suffix=".md",
        )
        fallback = _artifact_filename(
            task="agent_daily",
            requested_provider="ollama",
            actual_provider="anthropic",
            suffix=".md",
        )
        self.assertEqual(same, "agent_daily__requested-ollama.md")
        self.assertEqual(fallback, "agent_daily__requested-ollama__actual-anthropic.md")

    def test_run_and_collect_reuses_sidecar_and_copies_artifact(self):
        source_output = self.root / "outputs" / "latest" / "decision_memo.md"
        source_output.write_text("# memo", encoding="utf-8")

        def _fake_subprocess(*args, **kwargs):
            sidecar = {
                "run_id": "agent-daily-1",
                "started_at": "2026-04-14T13:00:00",
                "completed_at": "2026-04-14T13:00:02",
                "git_commit": "abc1234",
                "tasks": [
                    {
                        "run_id": "agent-daily-1",
                        "task": "agent.daily",
                        "resolved_provider": "ollama",
                        "actual_provider": "ollama",
                        "model": "gemma3:4b",
                        "actual_model": "gemma3:4b",
                        "base_url": "http://localhost:11434/v1",
                        "actual_base_url": "http://localhost:11434/v1",
                        "latency_ms": 1500,
                        "success": True,
                        "error_type": None,
                        "fallback_reason": None,
                        "fallback_triggered": False,
                        "output_file": "outputs/latest/decision_memo.md",
                        "git_commit": "abc1234",
                    }
                ],
            }
            (self.root / "outputs" / "latest" / "agent_llm_metadata.json").write_text(
                json.dumps(sidecar, indent=2),
                encoding="utf-8",
            )

            class _Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return _Result()

        with patch("tools.provider_eval.subprocess.run", side_effect=_fake_subprocess):
            row = _run_and_collect(
                root=self.root,
                task="agent_daily",
                provider="ollama",
                eval_dir=self.eval_dir,
                config="config.json",
                profile=None,
            )

        self.assertEqual(row["provider"], "ollama")
        self.assertTrue(row["success"])
        copied = self.root / row["output_file"]
        self.assertTrue(copied.exists())
        self.assertEqual(copied.read_text(encoding="utf-8"), "# memo")
        self.assertIn("requested-ollama", copied.name)

    def test_disable_fallback_sets_eval_env_flag(self):
        def _fake_subprocess(*args, **kwargs):
            self.assertEqual(kwargs["env"].get("STOCKBOT_DISABLE_LLM_FALLBACK"), "1")
            sidecar = {
                "llm_metadata": {
                    "run_id": "theme-daily-1",
                    "task": "theme_engine.daily",
                    "resolved_provider": "openai",
                    "actual_provider": "openai",
                    "model": "gpt-4o-mini",
                    "actual_model": "gpt-4o-mini",
                    "base_url": "https://api.openai.com/v1",
                    "actual_base_url": "https://api.openai.com/v1",
                    "latency_ms": 1200,
                    "success": True,
                    "error_type": None,
                    "fallback_reason": None,
                    "fallback_triggered": False,
                    "output_file": "outputs/latest/theme_signals.json",
                    "git_commit": "abc1234",
                }
            }
            (self.root / "outputs" / "latest" / "theme_engine_llm_metadata.json").write_text(
                json.dumps(sidecar, indent=2),
                encoding="utf-8",
            )
            (self.root / "outputs" / "latest" / "theme_signals.json").write_text("{}", encoding="utf-8")

            class _Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return _Result()

        with patch("tools.provider_eval.subprocess.run", side_effect=_fake_subprocess):
            row = _run_and_collect(
                root=self.root,
                task="theme_daily",
                provider="openai",
                eval_dir=self.eval_dir,
                config="config.json",
                profile=None,
                disable_fallback=True,
            )

        self.assertEqual(row["provider"], "openai")
        self.assertFalse(row["fallback_triggered"])

    def test_write_eval_csv_writes_required_columns(self):
        csv_path = self.eval_dir / "provider_eval.csv"
        _write_eval_csv(
            csv_path,
            [
                {
                    "run_id": "run-1",
                    "task": "agent_daily",
                    "provider": "ollama",
                    "model": "gemma3:4b",
                    "latency_ms": 1500,
                    "success": True,
                    "fallback_triggered": False,
                    "output_file": "outputs/evals/test_eval/artifacts/agent_daily__ollama.md",
                    "manual_score_relevance": "",
                    "manual_score_clarity": "",
                    "manual_score_structure": "",
                    "manual_score_actionability": "",
                    "manual_score_hallucination_risk": "",
                    "notes": "",
                    "requested_provider": "ollama",
                    "resolved_provider": "ollama",
                    "actual_provider": "ollama",
                    "base_url": "http://localhost:11434/v1",
                    "error_type": "",
                    "fallback_reason": "",
                    "git_commit": "abc1234",
                }
            ],
        )
        content = csv_path.read_text(encoding="utf-8")
        self.assertIn("manual_score_relevance", content)
        self.assertIn("manual_score_hallucination_risk", content)

    def test_write_eval_summary_generates_markdown(self):
        summary_path = self.eval_dir / "provider_eval_summary.md"
        rows = [
            {
                "requested_provider": "ollama",
                "actual_provider": "ollama",
                "success": True,
                "fallback_triggered": False,
                "latency_ms": 1500,
                "output_file": "outputs/evals/test_eval/artifacts/agent_daily__requested-ollama.md",
            },
            {
                "requested_provider": "ollama",
                "actual_provider": "anthropic",
                "success": True,
                "fallback_triggered": True,
                "latency_ms": 2200,
                "output_file": "outputs/evals/test_eval/artifacts/agent_daily__requested-ollama__actual-anthropic.md",
            },
        ]
        _write_eval_summary(
            summary_path,
            task="agent_daily",
            providers=["ollama", "anthropic"],
            rows=rows,
        )

        content = summary_path.read_text(encoding="utf-8")
        self.assertIn("# Provider Evaluation Summary", content)
        self.assertIn("`agent_daily`", content)
        self.assertIn("`ollama, anthropic`", content)
        self.assertIn("agent_daily__requested-ollama.md", content)
        self.assertIn("agent_daily__requested-ollama__actual-anthropic.md", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
