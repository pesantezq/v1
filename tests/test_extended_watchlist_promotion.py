"""Tests for ExtendedWatchlist cross-day-persistence reinforcement gate.

Covers the fix that lets a single-theme candidate that has *persisted* across
multiple distinct days satisfy the reinforcement gate, instead of being
permanently stuck on `insufficient_reinforcement`.

Background: before this fix, `evaluate_candidates` only treated a candidate as
reinforced when it appeared under >=2 themes OR carried a "direct" mention.
Defense/energy single-theme candidates (NOC/LMT/RTX/...) that recur day after
day under one theme never qualified, so the extended watchlist stayed dormant.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from watchlist_scanner.extended_watchlist import ExtendedWatchlist


def _cand(ticker, *, themes, sources, confidence=0.9, persistence_7d=0):
    return {
        "ticker": ticker,
        "themes": list(themes),
        "sources": list(sources),
        "confidence": confidence,
        "persistence_7d": persistence_7d,
    }


class TestPersistenceReinforcement(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "portfolio.db"

    def _ew(self, **kw):
        return ExtendedWatchlist(
            db_path=str(self.db),
            ttl_days=7,
            max_symbols=5,
            confidence_threshold=0.80,
            **kw,
        )

    def test_single_theme_persistent_candidate_promotes(self):
        """A single-theme candidate seen on >= reinforce_persistence_days
        distinct days is treated as reinforced and promoted."""
        ew = self._ew(reinforce_persistence_days=3)
        cand = _cand("NOC", themes=["Defense"], sources=["theme"], persistence_7d=3)
        result = ew.evaluate_candidates(candidates=[cand], static_watchlist=[])
        self.assertIn("NOC", result["promoted"])
        self.assertNotIn(
            "NOC", [s["symbol"] for s in result["skipped"]],
        )

    def test_single_theme_low_persistence_still_skipped(self):
        """Below the persistence threshold, single-theme candidates remain
        gated as insufficient_reinforcement."""
        ew = self._ew(reinforce_persistence_days=3)
        cand = _cand("LMT", themes=["Defense"], sources=["theme"], persistence_7d=2)
        result = ew.evaluate_candidates(candidates=[cand], static_watchlist=[])
        self.assertNotIn("LMT", result["promoted"])
        skipped = {s["symbol"]: s["reason"] for s in result["skipped"]}
        self.assertEqual(skipped.get("LMT"), "insufficient_reinforcement")

    def test_multi_theme_still_promotes_regardless_of_persistence(self):
        """Regression: the original multi-theme reinforcement path is intact."""
        ew = self._ew(reinforce_persistence_days=3)
        cand = _cand("RTX", themes=["Defense", "Aerospace"], sources=["theme"], persistence_7d=0)
        result = ew.evaluate_candidates(candidates=[cand], static_watchlist=[])
        self.assertIn("RTX", result["promoted"])

    def test_direct_mention_still_promotes_regardless_of_persistence(self):
        """Regression: the original direct-mention reinforcement path is intact."""
        ew = self._ew(reinforce_persistence_days=3)
        cand = _cand("XOM", themes=["Energy"], sources=["direct"], persistence_7d=0)
        result = ew.evaluate_candidates(candidates=[cand], static_watchlist=[])
        self.assertIn("XOM", result["promoted"])

    def test_persistence_gate_disabled_when_threshold_zero(self):
        """reinforce_persistence_days=0 disables the persistence path, restoring
        the pre-fix behavior (single-theme persistent candidate stays skipped)."""
        ew = self._ew(reinforce_persistence_days=0)
        cand = _cand("CVX", themes=["Energy"], sources=["theme"], persistence_7d=7)
        result = ew.evaluate_candidates(candidates=[cand], static_watchlist=[])
        self.assertNotIn("CVX", result["promoted"])
        skipped = {s["symbol"]: s["reason"] for s in result["skipped"]}
        self.assertEqual(skipped.get("CVX"), "insufficient_reinforcement")

    def test_default_threshold_is_active(self):
        """The default constructor (no explicit kwarg) enables persistence
        reinforcement so the fix is on by default, not opt-in."""
        ew = ExtendedWatchlist(
            db_path=str(self.db), ttl_days=7, max_symbols=5, confidence_threshold=0.80,
        )
        cand = _cand("NOC", themes=["Defense"], sources=["theme"], persistence_7d=5)
        result = ew.evaluate_candidates(candidates=[cand], static_watchlist=[])
        self.assertIn("NOC", result["promoted"])


if __name__ == "__main__":
    unittest.main()
