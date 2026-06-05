"""
Tests for backtesting/score_invariance_gate.py — the Step 5 safety gate
(precondition #2: protected-score value regression across a governed apply).

Fully offline and deterministic. Proves the gate (a) computes the protected
scores over a fixture via the REAL scoring functions, (b) reports GREEN when a
real registry weight delta is applied and the scores stay bit-identical — the
current architecture (default_weight is not read by scoring), (c) flips RED when
a registry-coupled probe IS perturbed by the delta (so a future coupling would
be caught before any live apply), (d) leaves the LIVE registry byte-identical
(works on a temp copy), and (e) returns 'inconclusive' when the apply was a no-op.

Observe-only: operates on a temp copy of config/signal_registry.yaml; never
mutates the live registry or any protected scoring logic.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from backtesting.score_invariance_gate import (
    assert_scores_invariant_across_apply,
    compute_protected_scores,
    default_fixture,
)

_REGISTRY = "config/signal_registry.yaml"


class TestComputeProtectedScores(unittest.TestCase):
    def test_returns_real_scores_in_range(self):
        out = compute_protected_scores(_REGISTRY, default_fixture())
        scores = out["scores"]
        # alert_ranking + confidence import everywhere; scanner (signal_score)
        # needs pandas and may be unavailable in a bare venv — degrade per-probe.
        self.assertIn("final_rank_score", scores)
        self.assertIn("confidence_score", scores)
        for name, val in scores.items():
            self.assertIsInstance(val, (int, float), name)
            self.assertFalse(val != val, f"{name} is NaN")  # NaN check
            self.assertGreaterEqual(val, 0.0, name)


class TestInvarianceGate(unittest.TestCase):
    def test_weight_changes_but_scores_invariant_is_green(self):
        out = assert_scores_invariant_across_apply(
            registry_path=_REGISTRY, target_signal_id="STRONG_MOVE_UP", sample_delta=0.05,
        )
        self.assertEqual(out["status"], "GREEN")
        self.assertEqual(out["apply_status"], "applied")
        # The experiment is valid only if the weight actually moved in the registry…
        self.assertNotEqual(out["registry_weight_before"], out["registry_weight_after"])
        # …yet no protected score changed.
        self.assertEqual(out["diffs"], {})
        self.assertTrue(out["observe_only"])

    def test_detects_coupling_is_red(self):
        # Inject a probe that DOES read the registry weight: a future coupling of
        # default_weight into a score would look like this. The gate must flip RED.
        def coupled_probe(registry_path: str) -> float:
            from portfolio_automation.signal_registry import load_signal_registry
            return load_signal_registry(registry_path).get("STRONG_MOVE_UP").default_weight

        out = assert_scores_invariant_across_apply(
            registry_path=_REGISTRY, target_signal_id="STRONG_MOVE_UP", sample_delta=0.05,
            extra_probes={"coupled_demo": coupled_probe},
        )
        self.assertEqual(out["status"], "RED")
        self.assertIn("coupled_demo", out["diffs"])

    def test_live_registry_byte_identical_after_gate(self):
        before = Path(_REGISTRY).read_bytes()
        assert_scores_invariant_across_apply(
            registry_path=_REGISTRY, target_signal_id="STRONG_MOVE_UP", sample_delta=0.05,
        )
        self.assertEqual(Path(_REGISTRY).read_bytes(), before,
                         "gate must operate on a temp copy; live registry untouched")

    def test_inconclusive_when_apply_is_noop(self):
        # Delta beyond the per-change cap → apply rejects it → nothing changed →
        # the gate cannot conclude invariance.
        out = assert_scores_invariant_across_apply(
            registry_path=_REGISTRY, target_signal_id="STRONG_MOVE_UP",
            sample_delta=0.95, max_abs_delta=0.05,
        )
        self.assertEqual(out["status"], "inconclusive")

    def test_inconclusive_for_unknown_signal(self):
        out = assert_scores_invariant_across_apply(
            registry_path=_REGISTRY, target_signal_id="NOT_A_REAL_SIGNAL", sample_delta=0.05,
        )
        self.assertEqual(out["status"], "inconclusive")


if __name__ == "__main__":
    unittest.main(verbosity=2)
