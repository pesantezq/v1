"""
Tests for portfolio_automation/retune_suggestions.py and
portfolio_automation/retune_auto_apply.py.

Covers:
  - Suggestions degrade to "no_efficacy_input" when no input
  - Weight proposals reflect tag efficacy
  - Auto_applicable flag follows guardrails (magnitude, n)
  - Apply step honours all six guardrails
  - 2-run confirmation rule (queue → apply)
  - Monthly drift cap enforced
  - Audit log records every apply
  - Rollback restores prior value
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.retune_suggestions import (
    _AUTO_APPLY_MIN_N,
    _AUTO_APPLY_WEIGHT_MAX_DELTA,
    build_retune_suggestions,
    run_retune_suggestions,
)
from portfolio_automation.retune_auto_apply import (
    apply_suggestions,
    rollback,
)


def _build_efficacy(tag_specs: list[dict]) -> dict:
    by_tag = {}
    for spec in tag_specs:
        by_tag[spec["tag"]] = {
            "n_samples": spec.get("n", 100),
            "hit_rate_1d": spec.get("hr", 0.5),
            "vs_baseline_pp": spec.get("delta_pp", 0.0),
            "significance": spec.get("significance", "neutral"),
        }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": 30,
        "universe_baseline": {"n_samples": 500, "hit_rate_1d": 0.5},
        "by_tag": by_tag,
    }


class TestSuggestionDegradation(unittest.TestCase):
    def test_no_efficacy_input_returns_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td))
            self.assertFalse(r["available"])
            self.assertEqual(r["reason"], "no_efficacy_input")


class TestWeightProposals(unittest.TestCase):
    def test_positive_delta_increases_weight(self):
        eff = _build_efficacy([
            {"tag": "source:theme_candidate", "n": 250, "delta_pp": 10.0, "significance": "winner"},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            theme_prop = next(p for p in r["weight_proposals"]
                              if p["parameter"] == "sanitation_weight.theme")
            self.assertGreater(theme_prop["proposed_value"], theme_prop["current_value"])
            self.assertGreater(theme_prop["delta"], 0)

    def test_negative_delta_decreases_weight(self):
        eff = _build_efficacy([
            {"tag": "source:fmp_top100", "n": 250, "delta_pp": -10.0, "significance": "loser"},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            fmp_prop = next(p for p in r["weight_proposals"]
                            if p["parameter"] == "sanitation_weight.fmp")
            self.assertLess(fmp_prop["proposed_value"], fmp_prop["current_value"])


class TestAutoApplicableFlag(unittest.TestCase):
    def test_low_n_not_auto_applicable(self):
        eff = _build_efficacy([
            {"tag": "source:theme_candidate", "n": _AUTO_APPLY_MIN_N - 1,
             "delta_pp": 3.0, "significance": "winner"},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            theme_prop = next(p for p in r["weight_proposals"]
                              if p["parameter"] == "sanitation_weight.theme")
            self.assertFalse(theme_prop["auto_applicable"])

    def test_high_magnitude_not_auto_applicable(self):
        # Δ ≈ 10pp × 0.005 = 0.05 weight shift, exceeds 0.03 cap
        eff = _build_efficacy([
            {"tag": "source:theme_candidate", "n": 250, "delta_pp": 10.0, "significance": "winner"},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            theme_prop = next(p for p in r["weight_proposals"]
                              if p["parameter"] == "sanitation_weight.theme")
            self.assertFalse(theme_prop["auto_applicable"])

    def test_safe_proposal_is_auto_applicable(self):
        # Δ ≈ 3pp × 0.005 = 0.015 weight shift, under 0.03 cap; n ≥ 200
        eff = _build_efficacy([
            {"tag": "source:theme_candidate", "n": 250, "delta_pp": 3.0, "significance": "winner"},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            theme_prop = next(p for p in r["weight_proposals"]
                              if p["parameter"] == "sanitation_weight.theme")
            self.assertTrue(theme_prop["auto_applicable"])


class TestAutoApplyFlow(unittest.TestCase):
    """Two-run confirmation + audit + rollback."""

    def _seed_suggestion(self, root: Path, proposal: dict) -> None:
        p = root / "outputs" / "latest" / "gate_retune_suggestions.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "available": True,
            "weight_proposals": [proposal],
            "gate_proposal": None,
        }))

    def _seed_config(self, root: Path, cfg: dict) -> None:
        (root / "config.json").write_text(json.dumps(cfg))

    def test_first_run_queues_second_run_applies(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._seed_config(root, {})
            proposal = {
                "parameter": "sanitation_weight.theme",
                "current_value": 0.30,
                "proposed_value": 0.315,
                "delta": 0.015,
                "n_samples": 250,
                "auto_applicable": True,
                "significance": "winner",
            }
            self._seed_suggestion(root, proposal)

            # First run — queues for confirmation, no mutation
            r1 = apply_suggestions(root=root)
            self.assertEqual(r1["applied_count"], 0)
            self.assertEqual(r1["queued_count"], 1)

            # Second run with same payload — applies
            r2 = apply_suggestions(root=root)
            self.assertEqual(r2["applied_count"], 1)
            cfg = json.loads((root / "config.json").read_text())
            self.assertAlmostEqual(cfg["sanitation_weight"]["theme"], 0.315, places=4)

    def test_audit_log_records_apply_and_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._seed_config(root, {})
            proposal = {
                "parameter": "sanitation_weight.fmp",
                "current_value": 0.10,
                "proposed_value": 0.115,
                "delta": 0.015,
                "n_samples": 250,
                "auto_applicable": True,
                "significance": "winner",
            }
            self._seed_suggestion(root, proposal)
            apply_suggestions(root=root)              # queue
            apply_suggestions(root=root)              # apply
            audit = (root / "data" / "retune_audit_log.jsonl").read_text().splitlines()
            self.assertEqual(len(audit), 1)
            entry = json.loads(audit[0])
            self.assertEqual(entry["parameter"], "sanitation_weight.fmp")
            self.assertEqual(entry["applied_by"], "auto")
            self.assertAlmostEqual(entry["new_value"], 0.115, places=4)

            # Rollback
            r = rollback(root=root, parameter="sanitation_weight.fmp")
            self.assertEqual(r["status"], "ok")
            cfg = json.loads((root / "config.json").read_text())
            self.assertAlmostEqual(cfg["sanitation_weight"]["fmp"], 0.10, places=4)
            audit_after = (root / "data" / "retune_audit_log.jsonl").read_text().splitlines()
            self.assertEqual(len(audit_after), 2)
            self.assertEqual(json.loads(audit_after[1])["applied_by"], "rollback")

    def test_monthly_drift_cap_blocks_further_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._seed_config(root, {})
            # Seed state with already-near-cap drift
            (root / "data").mkdir(parents=True, exist_ok=True)
            (root / "data" / "retune_auto_apply_state.json").write_text(json.dumps({
                "apply_enabled": True,
                "month": f"{datetime.now(timezone.utc).year:04d}-{datetime.now(timezone.utc).month:02d}",
                "pending_confirmations": {
                    "sanitation_weight.theme": [0.32, 0.02],  # match the confirm_token
                },
                "monthly_drift": {"sanitation_weight.theme": 0.24},  # near cap
            }))
            proposal = {
                "parameter": "sanitation_weight.theme",
                "current_value": 0.30,
                "proposed_value": 0.32,
                "delta": 0.02,
                "n_samples": 250,
                "auto_applicable": True,
                "significance": "winner",
            }
            self._seed_suggestion(root, proposal)
            r = apply_suggestions(root=root)
            # 0.24 + 0.02 = 0.26 > 0.25 → blocked
            self.assertEqual(r["skipped_count"], 1)
            self.assertEqual(r["applied_count"], 0)
            self.assertIn("monthly_drift_cap", r["actions"][0]["reason"])

    def test_apply_disabled_blocks_everything(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data").mkdir(parents=True, exist_ok=True)
            (root / "data" / "retune_auto_apply_state.json").write_text(json.dumps({
                "apply_enabled": False,
                "month": f"{datetime.now(timezone.utc).year:04d}-{datetime.now(timezone.utc).month:02d}",
                "pending_confirmations": {},
                "monthly_drift": {},
            }))
            proposal = {
                "parameter": "sanitation_weight.theme",
                "current_value": 0.30,
                "proposed_value": 0.315,
                "delta": 0.015,
                "n_samples": 250,
                "auto_applicable": True,
            }
            self._seed_suggestion(root, proposal)
            r = apply_suggestions(root=root)
            self.assertEqual(r["status"], "skipped")
            self.assertEqual(r["reason"], "apply_disabled_by_state")


class TestOrchestrator(unittest.TestCase):
    def test_run_retune_suggestions_writes_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            r = run_retune_suggestions(root=Path(td))
            self.assertEqual(r["status"], "ok")
            p = Path(td) / "outputs" / "latest" / "gate_retune_suggestions.json"
            self.assertTrue(p.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
