import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import gui.app as gui_app


class TestGuiApiHealth(unittest.TestCase):

    def test_openai_timeout_is_treated_as_running_but_slow(self):
        with patch.object(
            gui_app,
            "validate_openai_connection",
            return_value={
                "ok": False,
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
                "message": "OpenAI timed out for model 'gpt-4o-mini' at https://api.openai.com/v1.",
            },
        ):
            status = gui_app._get_llm_status(
                {"theme_engine": {"llm_provider": "openai"}}
            )

        self.assertTrue(status["running"])
        self.assertTrue(status["timed_out"])
        self.assertFalse(status["model_available"])
        self.assertEqual(status["timeout_seconds"], 20)
        self.assertEqual(status["provider"], "openai")

    def test_llm_health_timeout_can_be_configured(self):
        with patch.dict("os.environ", {"LLM_HEALTH_TIMEOUT": "30"}, clear=False):
            with patch.object(
                gui_app,
                "validate_openai_connection",
                return_value={
                    "ok": True,
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4o-mini",
                    "message": "ok",
                },
            ) as mock_validate:
                status = gui_app._get_llm_status({})

        self.assertEqual(status["timeout_seconds"], 30)
        self.assertTrue(status["running"])
        self.assertTrue(status["model_available"])
        self.assertEqual(mock_validate.call_args.kwargs["timeout"], 30)

    def test_openai_unreachable_still_reports_not_running(self):
        with patch.object(
            gui_app,
            "validate_openai_connection",
            return_value={
                "ok": False,
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
                "message": "OpenAI is not reachable at https://api.openai.com/v1 (connection refused).",
            },
        ):
            status = gui_app._get_llm_status({})

        self.assertFalse(status["running"])
        self.assertFalse(status["timed_out"])
        self.assertIn("not reachable", status["error"].lower())


if __name__ == "__main__":
    unittest.main()
