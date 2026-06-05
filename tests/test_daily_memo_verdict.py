"""
Tests for watchlist_scanner.daily_memo._build_verdict.

Covers the mood-priority ladder + body composition. The verdict is a
single-line synthesis intended for the top of the memo, so the test
asserts each mood label maps to the expected output substring.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchlist_scanner.daily_memo import _advisor_stack_items, _build_verdict


def _decisions(*specs):
    """Build a list of decision rows from (urgency, source) tuples."""
    return [{"urgency": u, "source": s, "decision": "SELL", "symbol": "ABC"}
            for u, s in specs]


class TestBuildVerdict(unittest.TestCase):

    def _summary(self, generated_at: str | None = None) -> dict:
        # Default to "right now" so the test isn't fragile to wall-clock time —
        # _freshness_banner fires at >=2 days old, which would silently flip
        # every verdict to "Stale" once the calendar advanced past a hardcoded
        # date by 2 days. (Pre-existing fragility caught 2026-05-21.)
        if generated_at is None:
            from datetime import datetime
            generated_at = datetime.now().isoformat(timespec="seconds")
        return {"generated_at": generated_at}

    def test_steady_when_no_urgent_actions(self):
        with tempfile.TemporaryDirectory() as td:
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("low", "portfolio")),
                capital_counts={"SELL": 0, "SCALE": 1, "BUY": 0},
                root=Path(td),
            )
            self.assertIn("Steady", verdict)
            self.assertIn("1 advisory action", verdict)

    def test_action_required_when_critical_urgency_present(self):
        with tempfile.TemporaryDirectory() as td:
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("critical", "structural")),
                capital_counts={"SELL": 1, "SCALE": 0, "BUY": 0},
                root=Path(td),
            )
            # critical-urgency wins over the structural-risk demotion path
            # because action_required has higher priority than structural_risk.
            self.assertIn("Action required", verdict)

    def test_structural_risk_when_structural_source_no_urgent(self):
        with tempfile.TemporaryDirectory() as td:
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("low", "structural")),
                capital_counts={"SELL": 0, "SCALE": 0, "BUY": 0},
                root=Path(td),
            )
            self.assertIn("Structural risk", verdict)

    def test_cautious_when_medium_urgency_present(self):
        with tempfile.TemporaryDirectory() as td:
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("medium", "market"), ("low", "portfolio")),
                capital_counts={"SELL": 0, "SCALE": 1, "BUY": 0},
                root=Path(td),
            )
            self.assertIn("Cautious", verdict)

    def test_stale_when_summary_is_old(self):
        # _freshness_banner fires at >= 2 days old.
        with tempfile.TemporaryDirectory() as td:
            verdict = _build_verdict(
                {"generated_at": "2026-04-01T00:00:00"},
                decision_rows=_decisions(("critical", "structural")),
                capital_counts={"SELL": 5, "SCALE": 0, "BUY": 0},
                root=Path(td),
            )
            self.assertIn("Stale", verdict)

    def test_risk_delta_near_cap_promotes_to_cautious(self):
        # Even with no medium-urgency decisions, a near_cap risk_delta
        # status pushes the verdict to cautious.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "risk_delta.json").write_text(
                '{"overall_status": "near_cap"}'
            )
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("low", "portfolio")),
                capital_counts={"SELL": 0, "SCALE": 0, "BUY": 0},
                root=root,
            )
            self.assertIn("Cautious", verdict)
            self.assertIn("near a cap", verdict)

    def test_retune_milestone_appended_when_n_ge_30_and_delta_ge_10pp(self):
        # When current-fp has crossed n=30 and the hit-rate delta exceeds
        # 10pp, the verdict gets a trailing "Retune validated" callout
        # regardless of mood (except stale).
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "retune_impact.json").write_text(json.dumps({
                "current_fingerprint": "fp_new",
                "outcome_attribution": {
                    "available": True,
                    "pre_tracker_label": "pre_tracker_unknown",
                    "by_fingerprint": {
                        "fp_new": {"resolved_1d": 44, "hit_rate_1d": 0.795},
                        "pre_tracker_unknown": {"resolved_1d": 352, "hit_rate_1d": 0.406},
                    },
                },
            }))
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("low", "portfolio")),
                capital_counts={"SELL": 0, "SCALE": 0, "BUY": 0},
                root=root,
            )
            self.assertIn("Steady", verdict)
            self.assertIn("Retune validated", verdict)
            self.assertIn("n=44", verdict)

    def test_retune_milestone_underperforming_when_negative_delta(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "retune_impact.json").write_text(json.dumps({
                "current_fingerprint": "fp_new",
                "outcome_attribution": {
                    "available": True,
                    "pre_tracker_label": "pre_tracker_unknown",
                    "by_fingerprint": {
                        "fp_new": {"resolved_1d": 35, "hit_rate_1d": 0.20},
                        "pre_tracker_unknown": {"resolved_1d": 352, "hit_rate_1d": 0.40},
                    },
                },
            }))
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("low", "portfolio")),
                capital_counts={"SELL": 0, "SCALE": 0, "BUY": 0},
                root=root,
            )
            self.assertIn("Retune underperforming", verdict)

    def test_retune_milestone_suppressed_when_n_below_30(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "retune_impact.json").write_text(json.dumps({
                "current_fingerprint": "fp_new",
                "outcome_attribution": {
                    "available": True,
                    "pre_tracker_label": "pre_tracker_unknown",
                    "by_fingerprint": {
                        "fp_new": {"resolved_1d": 22, "hit_rate_1d": 0.795},
                        "pre_tracker_unknown": {"resolved_1d": 352, "hit_rate_1d": 0.406},
                    },
                },
            }))
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("low", "portfolio")),
                capital_counts={"SELL": 0, "SCALE": 0, "BUY": 0},
                root=root,
            )
            self.assertNotIn("Retune validated", verdict)
            self.assertNotIn("Retune underperforming", verdict)

    def test_retune_trap_not_validated_when_below_prior_gauge(self):
        # Favorable-baseline trap: current-fp beats the stale pre_tracker
        # baseline (+12.4pp) but is a regression vs the prior gauge era it
        # replaced (-15.9pp) with negative mean_return. The verdict must NOT
        # read "validated"; it must LEAD with the prior-gauge delta and
        # relegate the stale-baseline delta to a parenthetical.
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "retune_impact.json").write_text(json.dumps({
                "current_fingerprint": "d95e3096443925b0",
                "outcome_attribution": {
                    "available": True,
                    "pre_tracker_label": "pre_tracker_unknown",
                    "by_fingerprint": {
                        "d95e3096443925b0": {
                            "resolved_1d": 132, "hit_rate_1d": 0.5303,
                            "mean_return_1d": -0.2239,
                            "last_signal_time": "2026-06-04T09:02:33",
                        },
                        "f60e0b9d51bec808": {
                            "resolved_1d": 264, "hit_rate_1d": 0.6894,
                            "mean_return_1d": 1.879,
                            "last_signal_time": "2026-05-29T09:01:45",
                        },
                        "pre_tracker_unknown": {
                            "resolved_1d": 352, "hit_rate_1d": 0.4062,
                            "last_signal_time": "2026-05-19T01:22:36",
                        },
                    },
                },
            }))
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("low", "portfolio")),
                capital_counts={"SELL": 0, "SCALE": 0, "BUY": 0},
                root=root,
            )
            # Must not be framed as validated off the stale baseline.
            self.assertNotIn("Retune validated", verdict)
            self.assertIn("NOT validated", verdict)
            # Leads with the prior-gauge delta and names the prior gauge.
            self.assertIn("-15.9pp", verdict)
            self.assertIn("prior gauge", verdict)
            self.assertIn("f60e0b9d", verdict)
            # Stale-baseline delta is present but secondary.
            self.assertIn("+12.4pp", verdict)
            self.assertIn("n=132", verdict)

    def test_retune_validated_when_holds_vs_prior_gauge(self):
        # When current-fp beats BOTH the stale baseline and the prior gauge,
        # "validated" stands and still leads with the prior-gauge comparison.
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "retune_impact.json").write_text(json.dumps({
                "current_fingerprint": "aaaa1111",
                "outcome_attribution": {
                    "available": True,
                    "pre_tracker_label": "pre_tracker_unknown",
                    "by_fingerprint": {
                        "aaaa1111": {
                            "resolved_1d": 60, "hit_rate_1d": 0.72,
                            "mean_return_1d": 1.50,
                            "last_signal_time": "2026-06-04T09:00:00",
                        },
                        "bbbb2222": {
                            "resolved_1d": 200, "hit_rate_1d": 0.70,
                            "mean_return_1d": 1.20,
                            "last_signal_time": "2026-05-29T09:00:00",
                        },
                        "pre_tracker_unknown": {
                            "resolved_1d": 352, "hit_rate_1d": 0.406,
                            "last_signal_time": "2026-05-19T01:22:36",
                        },
                    },
                },
            }))
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("low", "portfolio")),
                capital_counts={"SELL": 0, "SCALE": 0, "BUY": 0},
                root=root,
            )
            self.assertIn("Retune validated", verdict)
            self.assertIn("prior gauge", verdict)
            self.assertIn("bbbb2222", verdict)

    def test_retune_trap_detected_when_stale_delta_below_10pp(self):
        # Production 2026-06-05 scenario and the core regression this fix
        # closes: current-fp beats the stale pre_tracker baseline by only
        # +9.4pp — BELOW the 10pp report gate — yet is -18.9pp BELOW the prior
        # gauge era it replaced, with negative mean_return. The trap must still
        # surface; the report gate must NOT key off the stale-baseline
        # magnitude alone (that magnitude is exactly what the trap suppresses).
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "retune_impact.json").write_text(json.dumps({
                "current_fingerprint": "d95e3096443925b0",
                "outcome_attribution": {
                    "available": True,
                    "pre_tracker_label": "pre_tracker_unknown",
                    "by_fingerprint": {
                        "d95e3096443925b0": {
                            "resolved_1d": 154, "hit_rate_1d": 0.50,
                            "mean_return_1d": -0.4707,
                            "last_signal_time": "2026-06-05T09:02:36",
                        },
                        "f60e0b9d51bec808": {
                            "resolved_1d": 264, "hit_rate_1d": 0.6894,
                            "mean_return_1d": 1.879,
                            "last_signal_time": "2026-05-29T09:01:45",
                        },
                        "pre_tracker_unknown": {
                            "resolved_1d": 352, "hit_rate_1d": 0.4062,
                            "last_signal_time": "2026-05-19T01:22:36",
                        },
                    },
                },
            }))
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("low", "portfolio")),
                capital_counts={"SELL": 0, "SCALE": 0, "BUY": 0},
                root=root,
            )
            self.assertNotIn("Retune validated", verdict)
            self.assertIn("NOT validated", verdict)
            self.assertIn("-18.9pp", verdict)
            self.assertIn("prior gauge", verdict)
            self.assertIn("f60e0b9d", verdict)
            # Stale-baseline delta is present but secondary (parenthetical).
            self.assertIn("+9.4pp", verdict)

    def test_advisor_stack_retune_line_leads_with_prior_gauge_trap(self):
        # The operator-facing Advisor Stack line (daily_memo.md "Retune impact"
        # bullet) is produced by _advisor_stack_items, a SEPARATE path from
        # _build_verdict. It must apply the same prior-gauge trap framing so the
        # operator's primary read isn't the favorable-baseline +Δ alone.
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "retune_impact.json").write_text(json.dumps({
                "current_fingerprint": "d95e3096443925b0",
                "outcome_attribution": {
                    "available": True,
                    "pre_tracker_label": "pre_tracker_unknown",
                    "by_fingerprint": {
                        "d95e3096443925b0": {
                            "resolved_1d": 154, "hit_rate_1d": 0.50,
                            "mean_return_1d": -0.4707,
                            "last_signal_time": "2026-06-05T09:02:36",
                        },
                        "f60e0b9d51bec808": {
                            "resolved_1d": 264, "hit_rate_1d": 0.6894,
                            "mean_return_1d": 1.879,
                            "last_signal_time": "2026-05-29T09:01:45",
                        },
                        "pre_tracker_unknown": {
                            "resolved_1d": 352, "hit_rate_1d": 0.4062,
                            "last_signal_time": "2026-05-19T01:22:36",
                        },
                    },
                },
            }))
            items = _advisor_stack_items(root)
            retune = [i for i in items if "Retune" in i]
            self.assertEqual(len(retune), 1, f"expected one retune line, got {items}")
            line = retune[0]
            self.assertIn("NOT validated", line)
            self.assertIn("prior gauge", line)
            self.assertIn("f60e0b9d", line)
            self.assertIn("-18.9pp", line)
            # The pre→current numbers remain available but no longer lead.
            self.assertIn("50.0%", line)

    def test_advisor_stack_retune_line_first_gauge_keeps_pre_current(self):
        # First-gauge era (no prior gauge to regress against): keep the legacy
        # "pre X → current Y" framing — there is nothing to be trapped by.
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "retune_impact.json").write_text(json.dumps({
                "current_fingerprint": "fp_only",
                "outcome_attribution": {
                    "available": True,
                    "pre_tracker_label": "pre_tracker_unknown",
                    "by_fingerprint": {
                        "fp_only": {"resolved_1d": 44, "hit_rate_1d": 0.62},
                        "pre_tracker_unknown": {"resolved_1d": 352, "hit_rate_1d": 0.406},
                    },
                },
            }))
            items = _advisor_stack_items(root)
            retune = [i for i in items if "Retune" in i]
            self.assertEqual(len(retune), 1)
            line = retune[0]
            self.assertNotIn("NOT validated", line)
            self.assertIn("pre", line)
            self.assertIn("current", line)

    def test_advisor_stack_retune_line_validated_when_holds_vs_prior(self):
        # When current-fp holds vs the prior gauge (and mean_return >= 0), the
        # operator line reads "validated" and still leads with the prior-gauge
        # comparison.
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "retune_impact.json").write_text(json.dumps({
                "current_fingerprint": "aaaa1111",
                "outcome_attribution": {
                    "available": True,
                    "pre_tracker_label": "pre_tracker_unknown",
                    "by_fingerprint": {
                        "aaaa1111": {
                            "resolved_1d": 60, "hit_rate_1d": 0.72,
                            "mean_return_1d": 1.50,
                            "last_signal_time": "2026-06-04T09:00:00",
                        },
                        "bbbb2222": {
                            "resolved_1d": 200, "hit_rate_1d": 0.70,
                            "mean_return_1d": 1.20,
                            "last_signal_time": "2026-05-29T09:00:00",
                        },
                        "pre_tracker_unknown": {
                            "resolved_1d": 352, "hit_rate_1d": 0.406,
                            "last_signal_time": "2026-05-19T01:22:36",
                        },
                    },
                },
            }))
            items = _advisor_stack_items(root)
            retune = [i for i in items if "Retune" in i]
            self.assertEqual(len(retune), 1)
            line = retune[0]
            self.assertIn("validated", line)
            self.assertNotIn("NOT validated", line)
            self.assertIn("prior gauge", line)
            self.assertIn("bbbb2222", line)

    def test_risk_delta_breach_promotes_to_structural_risk(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "risk_delta.json").write_text(
                '{"overall_status": "breach"}'
            )
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("low", "portfolio")),
                capital_counts={"SELL": 0, "SCALE": 0, "BUY": 0},
                root=root,
            )
            self.assertIn("Structural risk", verdict)


if __name__ == "__main__":
    unittest.main(verbosity=2)
