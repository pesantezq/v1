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
        n = spec.get("n", 100)
        # Default mean_return_1d is a small positive number so existing
        # tests (written before the expectancy gate existed) keep passing;
        # tests that care about the gate pass mean_return_1d explicitly
        # (including None, to simulate missing return data).
        by_tag[spec["tag"]] = {
            "n_samples": n,
            "hit_rate_1d": spec.get("hr", 0.5),
            "vs_baseline_pp": spec.get("delta_pp", 0.0),
            "significance": spec.get("significance", "neutral"),
            "mean_return_1d": spec.get("mean_return_1d", 0.5),
            "resolved_1d": spec.get("resolved_1d", n),
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


class TestExpectancyGate(unittest.TestCase):
    """Defect: the weight proposer was blind to expectancy — a tag with a
    positive hit-rate delta (proposing a weight INCREASE) but a NEGATIVE
    mean return could still be auto_applicable=True. These pin the fix:
    mean_return_1d is always surfaced, and a hit-rate/expectancy sign
    contradiction (or missing return data) refuses auto-applicability
    without hiding the proposal itself.
    """

    def test_mean_return_always_recorded_on_proposal(self):
        eff = _build_efficacy([
            {"tag": "source:theme_candidate", "n": 250, "delta_pp": 3.0,
             "significance": "winner", "mean_return_1d": 0.42, "resolved_1d": 220},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            prop = next(p for p in r["weight_proposals"]
                        if p["parameter"] == "sanitation_weight.theme")
            self.assertEqual(prop["mean_return_1d"], 0.42)
            self.assertEqual(prop["mean_return_resolved_n"], 220)
            self.assertTrue(prop["expectancy_available"])

    def test_positive_hitrate_delta_negative_mean_return_blocks_auto_apply(self):
        # Strong positive hit-rate delta (would raise the weight) but the
        # tag actually loses money on average — must NOT be auto-applicable.
        eff = _build_efficacy([
            {"tag": "source:theme_candidate", "n": 250, "delta_pp": 5.0,
             "significance": "winner", "mean_return_1d": -0.30, "resolved_1d": 210},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            prop = next(p for p in r["weight_proposals"]
                        if p["parameter"] == "sanitation_weight.theme")
            self.assertGreater(prop["delta"], 0, "sanity: this would have raised the weight")
            self.assertTrue(prop["expectancy_contradiction"])
            self.assertFalse(prop["auto_applicable"])
            # Visible, not dropped, for human review.
            self.assertIn("sanitation_weight.theme", [
                p["parameter"] for p in r["weight_proposals"]
            ])

    def test_missing_mean_return_blocks_auto_apply_without_imputing_zero(self):
        eff = _build_efficacy([
            {"tag": "source:theme_candidate", "n": 250, "delta_pp": 3.0,
             "significance": "winner", "mean_return_1d": None},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            prop = next(p for p in r["weight_proposals"]
                        if p["parameter"] == "sanitation_weight.theme")
            self.assertIsNone(prop["mean_return_1d"])
            self.assertFalse(prop["expectancy_available"])
            self.assertFalse(prop["expectancy_contradiction"])  # not a contradiction, just unknown
            self.assertFalse(prop["auto_applicable"])

    def test_positive_hitrate_delta_positive_mean_return_stays_auto_applicable(self):
        # Same magnitude/n/significance as the safe-proposal test, but now
        # with an explicit, confirmed positive mean return: the gate must
        # not block a proposal whose expectancy agrees with its hit-rate delta.
        eff = _build_efficacy([
            {"tag": "source:theme_candidate", "n": 250, "delta_pp": 3.0,
             "significance": "winner", "mean_return_1d": 0.45, "resolved_1d": 210},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            prop = next(p for p in r["weight_proposals"]
                        if p["parameter"] == "sanitation_weight.theme")
            self.assertTrue(prop["auto_applicable"])

    def test_negative_hitrate_delta_negative_mean_return_not_a_contradiction(self):
        # Weight DECREASE proposed, and the tag also loses money: signals
        # agree (both say "less weight"), so this is not a contradiction —
        # the pre-existing guardrails alone govern auto_applicable here.
        eff = _build_efficacy([
            {"tag": "source:fmp_top100", "n": 250, "delta_pp": -3.0,
             "significance": "loser", "mean_return_1d": -0.20, "resolved_1d": 210},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            prop = next(p for p in r["weight_proposals"]
                        if p["parameter"] == "sanitation_weight.fmp")
            self.assertLess(prop["delta"], 0)
            self.assertFalse(prop["expectancy_contradiction"])
            self.assertTrue(prop["auto_applicable"])


class TestGateExpectancyGate(unittest.TestCase):
    """Completion of the c0fc3c6c expectancy gate (2026-07-29).

    c0fc3c6c gated `_propose_weight_changes` on expectancy but left
    `_propose_promotion_gate` in the exact pre-fix shape: its `auto_applicable`
    read delta magnitude, n, and significance only, never mean_return. The same
    accuracy-vs-expectancy confusion therefore survived on the sibling path, and
    the gate proposal is counted into the same `auto_applicable_count` the armed
    auto-apply layer reads. These pin the symmetric fix.

    Direction semantics: a POSITIVE `high_theme_confidence` hit-rate delta raises
    extended_watchlist.confidence_threshold, i.e. leans HARDER on that tag. If
    the tag simultaneously loses money on average, leaning harder on it is the
    contradiction — mirroring `proposed_delta > 0` on the weight path.
    """

    def test_gate_proposal_always_records_expectancy(self):
        eff = _build_efficacy([
            {"tag": "high_theme_confidence", "n": 250, "delta_pp": 4.0,
             "significance": "winner", "mean_return_1d": 0.61, "resolved_1d": 230},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            gate = r["gate_proposal"]
            self.assertEqual(gate["mean_return_1d"], 0.61)
            self.assertEqual(gate["mean_return_resolved_n"], 230)
            self.assertTrue(gate["expectancy_available"])
            self.assertFalse(gate["expectancy_contradiction"])

    def test_gate_positive_delta_negative_mean_return_blocks_auto_apply(self):
        eff = _build_efficacy([
            {"tag": "high_theme_confidence", "n": 250, "delta_pp": 5.0,
             "significance": "winner", "mean_return_1d": -0.30, "resolved_1d": 210},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            gate = r["gate_proposal"]
            self.assertGreater(gate["delta"], 0, "sanity: this would have raised the threshold")
            self.assertTrue(gate["expectancy_contradiction"])
            self.assertFalse(gate["auto_applicable"])
            # Visible for human review, never dropped.
            self.assertIn("EXPECTANCY CONTRADICTION", gate["rationale"])

    def test_gate_missing_mean_return_blocks_auto_apply_without_imputing_zero(self):
        eff = _build_efficacy([
            {"tag": "high_theme_confidence", "n": 250, "delta_pp": 4.0,
             "significance": "winner", "mean_return_1d": None},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            gate = r["gate_proposal"]
            self.assertIsNone(gate["mean_return_1d"])
            self.assertFalse(gate["expectancy_available"])
            self.assertFalse(gate["auto_applicable"])
            # A fabricated 0.0 would read as "not negative" and pass the gate.
            self.assertFalse(gate["expectancy_contradiction"])

    def test_gate_healthy_expectancy_stays_auto_applicable(self):
        """Fail-closed tightening only — never revoke a legitimately clean gate."""
        eff = _build_efficacy([
            {"tag": "high_theme_confidence", "n": 250, "delta_pp": 4.0,
             "significance": "winner", "mean_return_1d": 0.55, "resolved_1d": 230},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            self.assertTrue(r["gate_proposal"]["auto_applicable"])

    def test_gate_threshold_decrease_with_negative_return_is_not_a_contradiction(self):
        """Lowering the threshold means leaning LESS on the tag; a negative mean
        return agrees with that direction, so the pre-existing guardrails alone
        govern auto_applicable (mirrors the weight path's negative-delta case)."""
        eff = _build_efficacy([
            {"tag": "high_theme_confidence", "n": 250, "delta_pp": -4.0,
             "significance": "loser", "mean_return_1d": -0.25, "resolved_1d": 210},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            gate = r["gate_proposal"]
            self.assertLess(gate["delta"], 0)
            self.assertFalse(gate["expectancy_contradiction"])
            self.assertTrue(gate["auto_applicable"])

    def test_renderer_surfaces_gate_expectancy_and_contradiction(self):
        """The weight table gained a mean-return column + a ⚠ contradiction line
        in c0fc3c6c; the gate section must state the same facts rather than
        burying them inside the rationale prose."""
        from portfolio_automation.retune_suggestions import render_retune_suggestions_md
        eff = _build_efficacy([
            {"tag": "high_theme_confidence", "n": 250, "delta_pp": 5.0,
             "significance": "winner", "mean_return_1d": -0.30, "resolved_1d": 210},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            md = render_retune_suggestions_md(r)
        gate_section = md.split("## Promotion gate proposal", 1)[1]
        self.assertIn("-0.3000", gate_section)
        self.assertIn("210", gate_section)
        self.assertIn("EXPECTANCY CONTRADICTION", gate_section)
        self.assertIn("⚠", gate_section)

    def test_renderer_gate_states_unavailable_expectancy(self):
        from portfolio_automation.retune_suggestions import render_retune_suggestions_md
        eff = _build_efficacy([
            {"tag": "high_theme_confidence", "n": 250, "delta_pp": 4.0,
             "significance": "winner", "mean_return_1d": None},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            md = render_retune_suggestions_md(r)
        gate_section = md.split("## Promotion gate proposal", 1)[1]
        self.assertIn("unavailable", gate_section)

    def test_insufficient_sample_gate_carries_expectancy_keys(self):
        """The no-change default must still expose the schema, so a consumer
        never has to distinguish 'key absent' from 'expectancy unverified'."""
        eff = _build_efficacy([
            {"tag": "high_theme_confidence", "n": 10, "delta_pp": 4.0,
             "significance": "insufficient_sample", "mean_return_1d": 0.4},
        ])
        with tempfile.TemporaryDirectory() as td:
            r = build_retune_suggestions(root=Path(td), efficacy_payload=eff)
            gate = r["gate_proposal"]
            self.assertFalse(gate["auto_applicable"])
            for key in ("mean_return_1d", "mean_return_resolved_n",
                        "expectancy_available", "expectancy_contradiction"):
                self.assertIn(key, gate)


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


class TestAutoApplyEvidenceGate(unittest.TestCase):
    """Guardrail 1b — bounded is not the same as justified.

    retune_auto_apply's five guardrails bound MAGNITUDE (delta cap), SAMPLE SIZE
    (n>=200), REPETITION (2-run confirm) and CUMULATIVE DRIFT — but none of them
    looked at `significance`, which was recorded in the audit entry and otherwise
    ignored. pattern_learning._classify returns "neutral" precisely when
    |delta vs baseline| < 5pp, i.e. NO measured effect, so a zero-evidence
    proposal could mutate config.json purely by repeating itself for two runs.

    Live on 2026-08-03: three auto_applicable weight proposals were all `neutral`
    (theme +0.85pp, hit_rate +0.35pp, sources +3.43pp). Sibling defect to the
    Pattern-Loop mutator's missing CI screen (backtesting/auto_apply.py G3b).
    """

    def _seed(self, root: Path, proposal: dict) -> None:
        p = root / "outputs" / "latest" / "gate_retune_suggestions.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "available": True, "weight_proposals": [proposal], "gate_proposal": None}))
        (root / "config.json").write_text(json.dumps({}))

    def _proposal(self, significance):
        return {
            "parameter": "sanitation_weight.theme",
            "current_value": 0.30, "proposed_value": 0.315, "delta": 0.015,
            "n_samples": 250, "auto_applicable": True,
            "significance": significance,
        }

    def _run_twice(self, significance):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._seed(root, self._proposal(significance))
            apply_suggestions(root=root)          # would queue
            r = apply_suggestions(root=root)      # would apply
            cfg = json.loads((root / "config.json").read_text())
            return r, cfg

    def test_neutral_significance_never_applies(self):
        r, cfg = self._run_twice("neutral")
        self.assertEqual(r["applied_count"], 0)
        self.assertEqual(cfg, {}, "config.json must be untouched by a no-evidence proposal")
        reasons = [x.get("reason", "") for x in r.get("actions", [])]
        self.assertTrue(any("no_significant_evidence" in x for x in reasons), reasons)
        self.assertEqual(r["skipped_count"], 1)

    def test_insufficient_sample_significance_never_applies(self):
        r, cfg = self._run_twice("insufficient_sample")
        self.assertEqual(r["applied_count"], 0)
        self.assertEqual(cfg, {})

    def test_missing_significance_fails_closed(self):
        r, cfg = self._run_twice(None)
        self.assertEqual(r["applied_count"], 0)
        self.assertEqual(cfg, {})

    def test_unknown_significance_string_fails_closed(self):
        r, cfg = self._run_twice("probably_fine")
        self.assertEqual(r["applied_count"], 0)
        self.assertEqual(cfg, {})

    def test_winner_still_applies(self):
        """The gate screens evidence — it must not block an evidenced proposal."""
        r, cfg = self._run_twice("winner")
        self.assertEqual(r["applied_count"], 1)
        self.assertAlmostEqual(cfg["sanitation_weight"]["theme"], 0.315, places=4)

    def test_every_directional_class_still_applies(self):
        for sig in ("winner", "strong_winner", "loser", "strong_loser"):
            r, _ = self._run_twice(sig)
            self.assertEqual(r["applied_count"], 1, f"{sig} should be evidenced")

    def test_gate_rejects_before_queueing_for_confirmation(self):
        """A no-evidence proposal must not even occupy the confirmation queue."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._seed(root, self._proposal("neutral"))
            apply_suggestions(root=root)
            state = json.loads((root / "data" / "retune_auto_apply_state.json").read_text())
            self.assertNotIn("sanitation_weight.theme", state.get("pending_confirmations", {}))
