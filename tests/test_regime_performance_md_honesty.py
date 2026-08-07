"""Regime-performance markdown must not overstate its own evidence.

Two defects found by the 2026-08-07 memo review, both of the same family as the
memo fixes in 371cdf0c: the JSON is correct and the RENDERED layer overstates.

#9  The producer computes ``effective_signals`` and ``hit_rate_uncertainty_pp``
    — a day-block deflation for non-independent samples — and the markdown
    dropped both, printing only ``total_signals``. Live 2026-08-06: neutral
    rendered "Total signals: 2319" while the JSON carried effective_signals
    1033, overstating independent evidence by 2.2x.

#8  ``by_regime`` only contains regimes with resolved history. On 2026-08-06 the
    live regime was ``risk_on`` and there was NO risk_on bucket at all — the
    document simply omitted it, so a reader could not tell that the regime the
    portfolio is actually in has zero historical evidence. Silence read as
    "nothing to report" when it meant "nothing is known".
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchlist_scanner.performance_feedback import _render_regime_performance_markdown


def _summary(**over):
    base = {
        "generated_at": "2026-08-06T09:03:00+00:00",
        "resolved_signals": 2508,
        "by_regime": {
            "neutral": {
                "total_signals": 2319, "effective_signals": 1033,
                "hit_rate_uncertainty_pp": 2.0, "win_rate": 0.527,
                "avg_return_pct": 0.34,
            },
            "risk_off": {
                "total_signals": 108, "effective_signals": 108,
                "hit_rate_uncertainty_pp": 9.1, "win_rate": 0.60,
                "avg_return_pct": -1.24,
            },
        },
    }
    base.update(over)
    return base


class TestEffectiveSignalsAreRendered:
    def test_effective_count_appears_when_deflated(self):
        md = _render_regime_performance_markdown(_summary())
        assert "1033" in md, (
            "effective_signals must be shown; 2319 alone overstates "
            "independent evidence by 2.2x")

    def test_total_is_still_shown(self):
        """The haircut is context for the total, not a replacement."""
        assert "2319" in _render_regime_performance_markdown(_summary())

    def test_uncertainty_band_appears(self):
        md = _render_regime_performance_markdown(_summary())
        assert "2.0" in md and "9.1" in md

    def test_no_deflation_reads_plainly(self):
        """effective == total means no day-block deflation was applied."""
        s = _summary(by_regime={"risk_off": {
            "total_signals": 108, "effective_signals": 108,
            "win_rate": 0.60, "avg_return_pct": -1.24}})
        md = _render_regime_performance_markdown(s)
        assert "108" in md

    def test_missing_effective_field_degrades_quietly(self):
        """Back-compat: payloads predating the deflation carry no field."""
        s = _summary(by_regime={"neutral": {
            "total_signals": 50, "win_rate": 0.5, "avg_return_pct": 0.1}})
        md = _render_regime_performance_markdown(s)
        assert "50" in md
        assert "effective" not in md.lower()


class TestAbsentRegimesAreNamed:
    def test_regime_with_no_evidence_is_stated_not_omitted(self):
        """Silence must not read as 'nothing to report'."""
        md = _render_regime_performance_markdown(
            _summary(current_regime="risk_on"))
        assert "risk_on" in md
        assert "no resolved" in md.lower() or "no evidence" in md.lower(), (
            "the live regime having zero history must be stated explicitly")

    def test_current_regime_with_evidence_adds_no_warning(self):
        md = _render_regime_performance_markdown(
            _summary(current_regime="neutral"))
        assert "no evidence" not in md.lower()

    def test_absent_current_regime_is_not_invented(self):
        """No current_regime supplied → no claim either way."""
        md = _render_regime_performance_markdown(_summary())
        assert "no evidence" not in md.lower()


class TestCurrentRegimeIsActuallyWired:
    """A renderer that nothing populates is an inert control.

    The absent-regime clause reads summary['current_regime'], which NOTHING set
    — so shipping only the renderer would have produced a feature that can
    never fire, the same defect class this batch exists to remove.
    """

    def test_resolver_reads_the_live_label(self, tmp_path):
        import json
        from watchlist_scanner.performance_feedback import _resolve_current_regime
        p = tmp_path / "portfolio_snapshot.json"
        p.write_text(json.dumps({"market_regime": {"regime_label": "risk_on"}}))
        assert _resolve_current_regime(p) == "risk_on"

    def test_resolver_is_non_fatal_on_missing_or_bad_input(self, tmp_path):
        from watchlist_scanner.performance_feedback import _resolve_current_regime
        assert _resolve_current_regime(tmp_path / "nope.json") is None
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert _resolve_current_regime(bad) is None
        empty = tmp_path / "empty.json"
        empty.write_text("{}")
        assert _resolve_current_regime(empty) is None

    def test_report_stamps_current_regime_end_to_end(self, tmp_path):
        import json
        from watchlist_scanner.performance_feedback import (
            generate_regime_performance_reports)
        snap = tmp_path / "portfolio_snapshot.json"
        snap.write_text(json.dumps({"market_regime": {"regime_label": "risk_on"}}))
        out = generate_regime_performance_reports(
            db_path=tmp_path / "empty.db", output_dir=tmp_path / "regime",
            snapshot_path=snap)
        assert out["summary"].get("current_regime") == "risk_on"

    def test_explicit_argument_overrides_the_snapshot(self, tmp_path):
        import json
        from watchlist_scanner.performance_feedback import (
            generate_regime_performance_reports)
        snap = tmp_path / "portfolio_snapshot.json"
        snap.write_text(json.dumps({"market_regime": {"regime_label": "risk_on"}}))
        out = generate_regime_performance_reports(
            db_path=tmp_path / "empty.db", output_dir=tmp_path / "regime",
            snapshot_path=snap, current_regime="high_volatility")
        assert out["summary"]["current_regime"] == "high_volatility"
