"""
Tests for gui_v2/data/risk_impact.py + the /risk-impact route.

Covers:
  - View collector returns the expected keys
  - Missing artifacts surface in missing_artifacts list, not as exceptions
  - overall_status takes the worst sub-status
  - Route renders without error against current outputs/latest/
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui_v2.data.risk_impact import (
    _classify_top_status,
    collect_risk_impact_view,
)


class TestClassifyTopStatus(unittest.TestCase):
    def test_picks_worst_across_inputs(self):
        self.assertEqual(_classify_top_status("ok", "ok"), "ok")
        self.assertEqual(_classify_top_status("ok", "near_cap"), "near_cap")
        self.assertEqual(_classify_top_status("near_cap", "breach"), "breach")
        self.assertEqual(_classify_top_status("breach", "failed"), "failed")

    def test_none_inputs_ignored(self):
        self.assertEqual(_classify_top_status(None, "ok", None), "ok")
        self.assertEqual(_classify_top_status(None, None, None), "ok")


class TestCollectView(unittest.TestCase):
    def test_returns_expected_keys(self):
        with tempfile.TemporaryDirectory() as td:
            view = collect_risk_impact_view(Path(td))
            for key in (
                "risk_delta", "retune_impact", "daily_run_status",
                "fmp_budget_status", "overall_status", "missing_artifacts",
            ):
                self.assertIn(key, view)

    def test_missing_artifacts_listed_not_raised(self):
        with tempfile.TemporaryDirectory() as td:
            view = collect_risk_impact_view(Path(td))
            self.assertEqual(
                set(view["missing_artifacts"]),
                {"risk_delta", "retune_impact", "daily_run_status", "fmp_budget_status"},
            )
            self.assertEqual(view["overall_status"], "ok")  # no sub-statuses worse than ok

    def test_loads_present_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            latest = root / "outputs" / "latest"
            latest.mkdir(parents=True)
            (latest / "risk_delta.json").write_text(json.dumps({
                "overall_status": "near_cap",
                "concentration": {"available": True,
                                  "cap": 0.6,
                                  "top_position": {"symbol": "QQQ", "weight": 0.55,
                                                   "headroom": 0.05, "status": "near_cap"}},
                "leverage": {"available": True, "cap": 0.25, "total_exposure": 0.19,
                             "headroom": 0.06, "status": "ok",
                             "leveraged_positions": []},
                "var": {"available": True, "method": "benchmark_sigma_proxy",
                        "sigma_annual": 0.1, "var_pct": 0.01, "var_dollar": 75},
            }))
            view = collect_risk_impact_view(root)
            self.assertEqual(view["overall_status"], "near_cap")
            self.assertIsNotNone(view["risk_delta"])
            self.assertIn("retune_impact", view["missing_artifacts"])


class TestRouteRenders(unittest.TestCase):
    """Smoke-test the actual /risk-impact route against the live outputs/latest/."""

    def test_route_returns_200_and_key_strings(self):
        from gui_v2.app import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        r = client.get("/risk-impact")
        self.assertEqual(r.status_code, 200)
        # Key labels that must appear in the rendered HTML.
        for needle in ("Risk &amp; Impact", "Risk Delta", "Retune Impact",
                       "FMP Budget", "Today's Run"):
            self.assertIn(needle, r.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
