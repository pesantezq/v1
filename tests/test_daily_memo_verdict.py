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
            # Leads with the prior-gauge delta using human label (H3: no raw hash).
            self.assertIn("-15.9pp", verdict)
            self.assertIn("prior gauge", verdict)
            self.assertNotIn("f60e0b9d", verdict)  # H3: no raw hash
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
            self.assertNotIn("bbbb2222", verdict)  # H3: no raw hash

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
            self.assertNotIn("f60e0b9d", verdict)  # H3: no raw hash
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
            self.assertNotIn("f60e0b9d", line)  # H3: no raw hash
            # Advisor Stack now LEADS with the prior-gauge delta (matches the
            # Verdict): current 0.50 vs prior 0.6894 → -18.9pp, framed BELOW.
            self.assertIn("-18.9pp", line)
            self.assertIn("BELOW", line)
            # The stale-baseline breakdown is retained as a parenthetical.
            self.assertIn("50.0%", line)
            self.assertIn("stale baseline", line)

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
            self.assertNotIn("bbbb2222", line)  # H3: no raw hash
            # LEADS with the positive prior-gauge delta (0.72 vs 0.70 = +2.0pp),
            # not "BELOW", and keeps the stale-baseline breakdown parenthetical.
            self.assertIn("+2.0pp", line)
            self.assertIn("vs the prior gauge it replaced", line)
            self.assertNotIn("BELOW", line)
            self.assertIn("stale baseline", line)

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

    # ── H1: mean-return unit glyph ───────────────────────────────────────────

    def test_h1_verdict_mean_return_has_percent_glyph(self):
        """mean_return_1d renders as '…mean-return +1.50%' not '…mean-return +1.50'.
        Uses a validated case (positive prior_delta) where mean-return appears
        in the verdict's stale-baseline parenthetical."""
        import json, re
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
            # H1: mean-return must appear with '%' glyph
            self.assertIn("mean-return", verdict)
            self.assertIn("%", verdict.split("mean-return")[1][:20])
            # must NOT render as bare number without glyph (e.g. "mean-return +1.50)")
            bare_pattern = re.compile(r"mean-return [+-]?\d+\.\d+[^%\w]")
            self.assertIsNone(bare_pattern.search(verdict), f"bare mean-return found: {verdict!r}")

    def test_h1_advisor_stack_mean_return_has_percent_glyph(self):
        """_advisor_stack_items retune line includes 'mean-return …%' not bare."""
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
            # H1: In Advisor Stack the mean-return clause (if present) must have '%'
            if "mean-return" in line:
                import re
                bare_pattern = re.compile(r"mean-return [+-]?\d+\.\d+[^%\w]")
                self.assertIsNone(bare_pattern.search(line), f"bare mean-return in advisor: {line!r}")

    # ── H2: de-duplicate the retune fact ────────────────────────────────────

    def test_advisor_stack_leads_with_prior_delta(self):
        """Advisor Stack retune line LEADS with the prior-gauge pp delta (matches
        the Verdict) and retains the stale-baseline breakdown as a parenthetical.
        Reversed 2026-07-08 (memo_advisor_stack_prior_gauge_lead): the line is
        headlined 'vs the prior gauge it replaced', so it must quantify that
        prior-gauge delta rather than only the favorable stale-baseline +Δ."""
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
            self.assertEqual(len(retune), 1)
            line = retune[0]
            # Advisor Stack now LEADS with the prior_delta (e.g. "-18.9pp")
            self.assertIn("-18.9pp", line)
            # it still shows the stale-baseline breakdown as a parenthetical
            self.assertIn("stale baseline", line)
            # validated/NOT-validated word must still be present
            self.assertIn("NOT validated", line)

    def test_h2_advisor_stack_does_not_repeat_mean_return_trap(self):
        """In trap case the mean_return clause must not appear in the Advisor Stack line."""
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
            line = retune[0]
            # H2: mean_return should NOT appear in the advisor stack retune line
            self.assertNotIn("mean-return", line)

    # ── H3: no raw fingerprint hash in prose ─────────────────────────────────

    def test_h3_verdict_no_raw_hash(self):
        """_build_verdict must not emit a raw hex fingerprint hash."""
        import json, re
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
            # H3: no 8-char hex hash in the verdict
            self.assertNotIn("f60e0b9d", verdict)
            self.assertNotIn("d95e3096", verdict)
            # Must still clearly reference the prior gauge
            self.assertIn("prior gauge", verdict)

    def test_h3_advisor_stack_no_raw_hash(self):
        """_advisor_stack_items retune line must not emit a raw hex fingerprint hash."""
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
            self.assertEqual(len(retune), 1)
            line = retune[0]
            self.assertNotIn("f60e0b9d", line)
            self.assertNotIn("d95e3096", line)
            self.assertIn("prior gauge", line)

    # ── M1: directional clarity ──────────────────────────────────────────────

    def test_m1_verdict_trap_says_below(self):
        """When prior_delta < 0 (trap), verdict must say BELOW, not just the raw delta."""
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
            # M1: the negative delta must be labelled "BELOW"
            self.assertIn("BELOW", verdict)
            self.assertIn("-18.9pp", verdict)
            self.assertIn("prior gauge", verdict)
            # Stale-baseline delta is present as secondary context
            self.assertIn("+9.4pp", verdict)

    def test_m1_verdict_positive_delta_no_below(self):
        """When prior_delta > 0 (validated), verdict does NOT say BELOW."""
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
            self.assertNotIn("BELOW", verdict)
            self.assertIn("Retune validated", verdict)


class TestAdvisorStackFmpBudgetLine(unittest.TestCase):
    """The Advisor Stack FMP-budget line must not render an uncapped budget as
    a malformed "N/0" ratio. Per the project convention (FMPClient treats
    budget <= 0 as uncapped), budget == 0 means "no daily cap", and the status
    artifact carries an explicit ``uncapped: true`` flag. The operator line
    should read "N / uncapped", never "N/0".
    """

    def _write(self, root: Path, budget_block: dict) -> None:
        import json
        (root / "outputs" / "latest").mkdir(parents=True, exist_ok=True)
        (root / "outputs" / "latest" / "fmp_budget_status.json").write_text(
            json.dumps({"budget": budget_block, "news": {"available": False}})
        )

    def test_uncapped_budget_renders_uncapped_not_zero(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root, {
                "available": True, "count_today": 264, "budget": 0,
                "status": "ok", "uncapped": True,
            })
            items = _advisor_stack_items(root)
            fmp = [i for i in items if "FMP budget" in i]
            self.assertEqual(len(fmp), 1, f"expected one FMP line, got {items}")
            line = fmp[0]
            self.assertNotIn("264/0", line)
            self.assertIn("264", line)
            self.assertIn("uncapped", line)

    def test_capped_budget_still_renders_ratio(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root, {
                "available": True, "count_today": 100, "budget": 250,
                "status": "ok", "uncapped": False,
            })
            items = _advisor_stack_items(root)
            fmp = [i for i in items if "FMP budget" in i]
            self.assertEqual(len(fmp), 1, f"expected one FMP line, got {items}")
            self.assertIn("100/250", fmp[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
