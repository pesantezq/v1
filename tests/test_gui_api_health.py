import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import gui.app as gui_app


class TestGuiApiHealth(unittest.TestCase):

    def test_ollama_timeout_is_treated_as_running_but_slow(self):
        with patch.object(
            gui_app,
            "validate_ollama_connection",
            return_value={
                "ok": False,
                "available_models": ["qwen2.5:7b-instruct", "gemma3:4b"],
                "message": "Ollama timed out for model 'qwen2.5:7b-instruct' at http://localhost:11434/v1.",
            },
        ):
            status = gui_app._get_ollama_status(
                {"theme_engine": {"ollama_model": "qwen2.5:7b-instruct"}}
            )

        self.assertTrue(status["running"])
        self.assertTrue(status["timed_out"])
        self.assertFalse(status["model_available"])
        self.assertEqual(status["timeout_seconds"], 20)
        self.assertIn("qwen2.5:7b-instruct", status["available_models"])

    def test_ollama_health_timeout_can_be_configured(self):
        with patch.dict("os.environ", {"OLLAMA_HEALTH_TIMEOUT": "30"}, clear=False):
            with patch.object(
                gui_app,
                "validate_ollama_connection",
                return_value={
                    "ok": True,
                    "available_models": ["gemma3:4b"],
                    "message": "ok",
                },
            ) as mock_validate:
                status = gui_app._get_ollama_status({})

        self.assertEqual(status["timeout_seconds"], 30)
        self.assertTrue(status["running"])
        self.assertTrue(status["model_available"])
        self.assertEqual(mock_validate.call_args.kwargs["timeout"], 30)

    def test_ollama_unreachable_still_reports_not_running(self):
        with patch.object(
            gui_app,
            "validate_ollama_connection",
            return_value={
                "ok": False,
                "available_models": [],
                "message": "Ollama is not reachable at http://localhost:11434/v1 (connection refused).",
            },
        ):
            status = gui_app._get_ollama_status({})

        self.assertFalse(status["running"])
        self.assertFalse(status["timed_out"])
        self.assertIn("not reachable", status["error"].lower())


if __name__ == "__main__":
    unittest.main()
