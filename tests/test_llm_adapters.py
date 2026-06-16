"""Focused tests for shared LLM adapter behavior."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from agent.llm_adapters import (
    call_openai,
    resolve_provider,
    resolve_task_provider,
    validate_openai_connection,
)


class TestLLMAdapters(unittest.TestCase):

    def test_resolve_provider_defaults_to_openai(self):
        with patch.dict("os.environ", {"STOCKBOT_LLM_PROVIDER": ""}, clear=False):
            self.assertEqual(resolve_provider(None), "openai")

    def test_resolve_provider_rejects_unknown_value(self):
        with patch.dict("os.environ", {"STOCKBOT_LLM_PROVIDER": ""}, clear=False):
            with self.assertRaises(RuntimeError):
                resolve_provider("not-a-provider")

    # NOTE: ollama is no longer a supported provider; resolving it now raises.
    def test_resolve_provider_rejects_removed_ollama(self):
        with patch.dict("os.environ", {"STOCKBOT_LLM_PROVIDER": ""}, clear=False):
            with self.assertRaises(RuntimeError):
                resolve_provider("ollama")

    def test_resolve_task_provider_precedence(self):
        # Repointed off the removed "ollama" task_provider; the global override
        # (STOCKBOT_LLM_PROVIDER) still beats the cli/task selections below it.
        with patch.dict("os.environ", {"STOCKBOT_LLM_PROVIDER": "openai"}, clear=False):
            resolved = resolve_task_provider(
                cli_provider="anthropic",
                task_provider="anthropic",
                fallback_task_provider="anthropic",
            )
        self.assertEqual(resolved, "anthropic")

    def test_resolve_task_provider_uses_task_when_global_override_absent(self):
        with patch.dict("os.environ", {"STOCKBOT_LLM_PROVIDER": ""}, clear=False):
            resolved = resolve_task_provider(
                cli_provider=None,
                task_provider="openai",
                fallback_task_provider="anthropic",
            )
        self.assertEqual(resolved, "openai")

    def test_call_openai_uses_chat_completions_and_parses_text(self):
        # Repointed from the removed call_ollama happy-path test; preserves the
        # generic OpenAI-compatible /v1/chat/completions parse coverage.
        body = {
            "choices": [
                {
                    "message": {
                        "content": "OK from OpenAI",
                    }
                }
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(body).encode("utf-8")
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_resp
        mock_ctx.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=mock_ctx) as mock_urlopen:
            text = call_openai(
                model="gpt-4o-mini",
                prompt="Reply with OK",
                base_url="https://api.openai.com/v1",
                api_key="test-key",
            )

        self.assertEqual(text, "OK from OpenAI")
        request = mock_urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/v1/chat/completions"))

    def test_call_openai_reports_malformed_response(self):
        # Repointed from the removed call_ollama malformed-response test.
        body = {"unexpected": "shape"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(body).encode("utf-8")
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_resp
        mock_ctx.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=mock_ctx):
            with self.assertRaises(RuntimeError) as ctx:
                call_openai(
                    model="gpt-4o-mini",
                    prompt="Reply with OK",
                    base_url="https://api.openai.com/v1",
                    api_key="test-key",
                )
        self.assertIn("malformed response", str(ctx.exception).lower())

    def test_validate_openai_connection_reports_missing_api_key(self):
        # Repointed from the removed validate_ollama_connection test; preserves
        # the health-probe "ok=False with a helpful message" coverage.
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
            with patch("agent.llm_adapters.get_secret", return_value=""):
                result = validate_openai_connection(
                    model="gpt-4o-mini",
                    base_url="https://api.openai.com/v1",
                    api_key="",
                    timeout=5,
                )

        self.assertFalse(result["ok"])
        self.assertEqual(result["provider"], "openai")
        self.assertIn("OPENAI_API_KEY", result["message"])

    def test_validate_openai_connection_ok_on_successful_response(self):
        body = {"choices": [{"message": {"content": "OK"}}]}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(body).encode("utf-8")
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_resp
        mock_ctx.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=mock_ctx):
            result = validate_openai_connection(
                model="gpt-4o-mini",
                base_url="https://api.openai.com/v1",
                api_key="test-key",
                timeout=5,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "openai")
        self.assertEqual(result["response"], "OK")
        self.assertIn("latency_ms", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
