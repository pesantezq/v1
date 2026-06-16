"""
OpenAI provider tests for the theme engine.

All tests are fully offline — no real network calls are made.

Test classes:
    TestOpenAIThemeDetector   — success parsing, API failure fallback, empty headlines
    TestOpenAIConfigResolution — llm block in config resolves provider and model
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from theme_engine.theme_detector import ThemeDetector
from theme_engine.__main__ import _resolve_theme_task_context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _openai_response(themes: list) -> str:
    """Build the compact JSON string that the OpenAI prompt elicits."""
    return json.dumps({"themes": themes})


# ---------------------------------------------------------------------------
# TestOpenAIThemeDetector
# ---------------------------------------------------------------------------

class TestOpenAIThemeDetector(unittest.TestCase):

    def _detector(self, **kwargs) -> ThemeDetector:
        return ThemeDetector(provider="openai", model="gpt-4o-mini", testing_mode=False, **kwargs)

    # -- success: compact keywords format -----------------------------------

    def test_openai_success_keywords_response_parsed(self):
        """OpenAI returns the compact {name, confidence, keywords} format."""
        raw = _openai_response([
            {"name": "AI Infrastructure", "confidence": 0.88, "keywords": ["gpu", "data center", "nvidia"]},
            {"name": "Cybersecurity", "confidence": 0.72, "keywords": ["breach", "zero-trust"]},
        ])
        with patch("theme_engine.theme_detector.call_provider", return_value=raw):
            result = self._detector().detect([{"title": "Nvidia expands AI data centers"}])

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "AI Infrastructure")
        self.assertAlmostEqual(result[0]["confidence"], 0.88)
        self.assertIn("gpu", result[0]["keywords"])

    def test_openai_keywords_mapped_to_evidence_items_when_no_evidence(self):
        """keywords fall back to evidence_items so ThemeStore / ThemeMapper work unchanged."""
        raw = _openai_response([
            {"name": "Defense", "confidence": 0.80, "keywords": ["defense spending", "NATO"]},
        ])
        with patch("theme_engine.theme_detector.call_provider", return_value=raw):
            result = self._detector().detect([{"title": "NATO increases defense budget"}])

        self.assertEqual(result[0]["evidence_items"], ["defense spending", "NATO"])

    def test_openai_both_evidence_items_and_keywords_preserved(self):
        """When the model returns both fields, evidence_items is not overwritten."""
        raw = _openai_response([
            {
                "name": "Cloud Infrastructure",
                "confidence": 0.75,
                "keywords": ["cloud", "hyperscaler"],
                "evidence_items": ["AWS revenue beats estimates"],
            }
        ])
        with patch("theme_engine.theme_detector.call_provider", return_value=raw):
            result = self._detector().detect([{"title": "AWS revenue beats estimates"}])

        self.assertEqual(result[0]["evidence_items"], ["AWS revenue beats estimates"])
        self.assertIn("cloud", result[0]["keywords"])

    def test_openai_prompt_template_used_for_openai_provider(self):
        """Verify the OpenAI-specific prompt (with 'keywords') is sent, not the full one."""
        captured_prompts = []

        def _fake_call_provider(**kwargs):
            captured_prompts.append(kwargs["prompt"])
            return _openai_response([{"name": "AI Infrastructure", "confidence": 0.8, "keywords": ["ai"]}])

        with patch("theme_engine.theme_detector.call_provider", side_effect=_fake_call_provider):
            self._detector().detect([{"title": "AI chip demand surges"}])

        self.assertTrue(captured_prompts, "call_provider was never called")
        prompt_sent = captured_prompts[0]
        self.assertIn("keywords", prompt_sent)
        # The full-format prompt asks for "rationale" and "evidence_items"; the OpenAI
        # template should NOT include those field names in the schema section.
        self.assertNotIn('"evidence_items"', prompt_sent)
        self.assertNotIn('"rationale"', prompt_sent)

    # -- API failure fallback ------------------------------------------------

    def test_openai_api_failure_returns_empty_list(self):
        """RuntimeError from call_provider is caught; detect() returns [] without crashing."""
        with patch("theme_engine.theme_detector.call_provider", side_effect=RuntimeError("OPENAI_API_KEY is not set")):
            result = self._detector().detect([{"title": "Some headline"}])

        self.assertEqual(result, [])

    def test_openai_http_error_returns_empty_list(self):
        """HTTP 401 from OpenAI is caught; pipeline does not crash."""
        import urllib.error
        with patch("theme_engine.theme_detector.call_provider", side_effect=RuntimeError("OpenAI API error: HTTP 401")):
            result = self._detector().detect([{"title": "Some headline"}])

        self.assertEqual(result, [])

    def test_openai_timeout_returns_empty_list(self):
        """Timeout from call_provider is caught; detect() returns []."""
        with patch("theme_engine.theme_detector.call_provider", side_effect=RuntimeError("OpenAI request timed out")):
            result = self._detector().detect([{"title": "Some headline"}])

        self.assertEqual(result, [])

    def test_openai_failure_does_not_raise(self):
        """Any exception from the API must be swallowed — the pipeline must not crash."""
        with patch("theme_engine.theme_detector.call_provider", side_effect=Exception("unexpected error")):
            try:
                result = self._detector().detect([{"title": "Headline"}])
            except Exception as exc:
                self.fail(f"detect() raised unexpectedly: {exc}")
        self.assertEqual(result, [])

    # -- empty headlines -----------------------------------------------------

    def test_openai_empty_headlines_returns_empty_list(self):
        """No headlines → skip LLM call, return []."""
        with patch("theme_engine.theme_detector.call_provider") as mock_call:
            result = self._detector().detect([])

        mock_call.assert_not_called()
        self.assertEqual(result, [])

    def test_openai_empty_headlines_makes_no_network_call(self):
        """Confirm urllib is never touched when headlines list is empty."""
        with patch("urllib.request.urlopen") as mock_url:
            self._detector().detect([])

        mock_url.assert_not_called()

    # -- call_provider wired to openai provider ------------------------------

    def test_openai_call_provider_receives_openai_provider(self):
        """call_provider must be called with provider='openai' and model='gpt-4o-mini'."""
        raw = _openai_response([{"name": "Payments", "confidence": 0.7, "keywords": ["fintech"]}])
        with patch("theme_engine.theme_detector.call_provider", return_value=raw) as mock_call:
            self._detector().detect([{"title": "Fintech growth accelerates"}])

        self.assertTrue(mock_call.called)
        kwargs = mock_call.call_args.kwargs
        self.assertEqual(kwargs["provider"], "openai")
        self.assertEqual(kwargs["model"], "gpt-4o-mini")


# ---------------------------------------------------------------------------
# TestOpenAIConfigResolution
# ---------------------------------------------------------------------------

class TestOpenAIConfigResolution(unittest.TestCase):

    def _ctx(self, config: dict, *, env: dict | None = None, provider_override: str | None = None) -> dict:
        env = env or {}
        with patch.dict(os.environ, {**env, "STOCKBOT_LLM_PROVIDER": ""}, clear=False):
            return _resolve_theme_task_context(mode="daily", config=config, provider_override=provider_override)

    def test_llm_block_provider_resolved(self):
        """theme_engine.llm.provider is picked up when no env/cli override present."""
        config = {"llm": {"provider": "openai", "model": "gpt-4o-mini"}}
        ctx = self._ctx(config)
        self.assertEqual(ctx["provider"], "openai")

    def test_llm_block_model_resolved(self):
        """theme_engine.llm.model is picked up when OPENAI_MODEL env var is absent."""
        config = {"llm": {"provider": "openai", "model": "gpt-4o-mini"}}
        ctx = self._ctx(config, env={"OPENAI_MODEL": ""})
        self.assertEqual(ctx["model"], "gpt-4o-mini")

    def test_env_openai_model_beats_llm_block_model(self):
        """OPENAI_MODEL env var overrides the llm.model config key."""
        config = {"llm": {"provider": "openai", "model": "gpt-4o-mini"}}
        ctx = self._ctx(config, env={"OPENAI_MODEL": "gpt-4o"})
        self.assertEqual(ctx["model"], "gpt-4o")

    def test_cli_provider_beats_llm_block(self):
        """CLI provider_override beats llm.provider in config."""
        config = {"llm": {"provider": "openai", "model": "gpt-4o-mini"}}
        ctx = self._ctx(config, provider_override="anthropic")
        self.assertEqual(ctx["provider"], "anthropic")

    def test_task_providers_beat_llm_block(self):
        """Per-mode task_providers entry beats llm.provider."""
        config = {
            "llm": {"provider": "openai", "model": "gpt-4o-mini"},
            "task_providers": {"daily": "anthropic"},
            "anthropic_model": "claude-haiku-4-5-20251001",
        }
        ctx = self._ctx(config)
        self.assertEqual(ctx["provider"], "anthropic")

    def test_llm_block_missing_falls_back_to_openai(self):
        """No llm block → default provider is openai."""
        config = {"openai_model": "gpt-4o-mini"}
        ctx = self._ctx(config)
        self.assertEqual(ctx["provider"], "openai")

    def test_llm_block_non_dict_ignored(self):
        """Malformed llm key (string instead of dict) does not crash; falls back to openai."""
        config = {"llm": "openai"}
        ctx = self._ctx(config)
        self.assertEqual(ctx["provider"], "openai")

    def test_base_url_set_for_openai_provider(self):
        """OpenAI provider context includes the correct base URL."""
        config = {"llm": {"provider": "openai", "model": "gpt-4o-mini"}}
        ctx = self._ctx(config, env={"OPENAI_BASE_URL": ""})
        self.assertEqual(ctx["base_url"], "https://api.openai.com/v1")


if __name__ == "__main__":
    unittest.main()
