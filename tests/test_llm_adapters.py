"""Focused tests for shared LLM adapter behavior."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from agent.llm_adapters import (
    call_ollama,
    resolve_ollama_base_url,
    resolve_provider,
    resolve_task_provider,
    validate_ollama_connection,
)


class TestLLMAdapters(unittest.TestCase):

    def test_resolve_provider_defaults_to_ollama(self):
        with patch.dict("os.environ", {"STOCKBOT_LLM_PROVIDER": ""}, clear=False):
            self.assertEqual(resolve_provider(None, default="ollama"), "ollama")

    def test_resolve_provider_rejects_unknown_value(self):
        with patch.dict("os.environ", {"STOCKBOT_LLM_PROVIDER": ""}, clear=False):
            with self.assertRaises(RuntimeError):
                resolve_provider("not-a-provider", default="ollama")

    def test_resolve_task_provider_precedence(self):
        with patch.dict("os.environ", {"STOCKBOT_LLM_PROVIDER": "openai"}, clear=False):
            resolved = resolve_task_provider(
                cli_provider="anthropic",
                task_provider="ollama",
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

    def test_resolve_ollama_base_url_adds_v1_suffix(self):
        self.assertEqual(
            resolve_ollama_base_url("http://localhost:11434"),
            "http://localhost:11434/v1",
        )

    def test_resolve_ollama_base_url_rejects_invalid_url(self):
        with self.assertRaises(RuntimeError) as ctx:
            resolve_ollama_base_url("localhost:11434")
        self.assertIn("OLLAMA_BASE_URL", str(ctx.exception))

    def test_call_ollama_uses_chat_completions_and_parses_text(self):
        body = {
            "choices": [
                {
                    "message": {
                        "content": "OK from Ollama",
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
            text = call_ollama(
                model="gemma3:4b",
                prompt="Reply with OK",
                base_url="http://localhost:11434/v1",
                api_key="ollama",
            )

        self.assertEqual(text, "OK from Ollama")
        request = mock_urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/v1/chat/completions"))

    def test_call_ollama_reports_malformed_response(self):
        body = {"unexpected": "shape"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(body).encode("utf-8")
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_resp
        mock_ctx.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=mock_ctx):
            with self.assertRaises(RuntimeError) as ctx:
                call_ollama(
                    model="gemma3:4b",
                    prompt="Reply with OK",
                    base_url="http://localhost:11434/v1",
                    api_key="ollama",
                )
        self.assertIn("malformed response", str(ctx.exception).lower())

    def test_call_ollama_missing_model_message_suggests_pull(self):
        with patch(
            "agent.llm_adapters._call_openai_compatible_chat",
            side_effect=RuntimeError("Ollama API error: HTTP 404 - model 'gemma3:4b' not found"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                call_ollama(
                    model="gemma3:4b",
                    prompt="Reply with OK",
                    base_url="http://localhost:11434/v1",
                    api_key="ollama",
                )
        self.assertIn("ollama pull gemma3:4b", str(ctx.exception))

    def test_validate_ollama_connection_reports_missing_model(self):
        tags_body = {"models": [{"name": "llama3.2:3b"}]}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(tags_body).encode("utf-8")
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_resp
        mock_ctx.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=mock_ctx):
            result = validate_ollama_connection(
                model="gemma3:4b",
                base_url="http://localhost:11434/v1",
                timeout=5,
            )

        self.assertFalse(result["ok"])
        self.assertIn("ollama pull gemma3:4b", result["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
