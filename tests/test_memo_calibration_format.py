"""Tests for calibration-hit-rate render formatting in the operator memo.

Regression guard for the 2026-07-09 memo-reviewer finding: the raw
``raw_calibration_hit_rate`` decimal fraction (0-1) was interpolated into
operator-facing prose as a full-precision float
(e.g. ``0.37551020408163266``) next to a cleanly formatted percent sibling
(``42.6% directional``). It must render as a fixed-precision percent
(e.g. ``37.6%``) at every render site, and degrade to a stable placeholder
when unavailable — never ``None`` and never a long float.

Render-layer only; observe-only; no decision/scoring/allocation logic.
"""

from __future__ import annotations

from portfolio_automation import memo_coherence as mc
from watchlist_scanner.daily_memo import _coherence_appendix_text

# 17-digit float from the live 2026-07-09 memo appendix
RAW = 0.37551020408163266
LONG_FLOAT_SUBSTR = "0.3755"


class TestFormatCalibrationHitRate:
    def test_fraction_renders_as_one_decimal_percent(self):
        assert mc.format_calibration_hit_rate(RAW) == "37.6%"

    def test_zero_renders_percent_not_bare_zero(self):
        assert mc.format_calibration_hit_rate(0.0) == "0.0%"

    def test_none_renders_stable_placeholder(self):
        assert mc.format_calibration_hit_rate(None) == "n/a"


def _hit_rate_available(raw):
    return {
        "available": True,
        "neutral_band_pct": 1.0,
        "directional_accuracy_pct": 42.6,
        "correct": 101,
        "incorrect": 136,
        "neutral": 253,
        "raw_calibration_hit_rate": raw,
    }


class TestCoherenceMarkdownRenderer:
    def test_raw_calibration_rendered_as_percent(self):
        out = mc.render_memo_coherence_md({"hit_rate": _hit_rate_available(RAW)})
        assert "37.6%" in out
        assert LONG_FLOAT_SUBSTR not in out

    def test_none_calibration_no_long_float_no_none(self):
        out = mc.render_memo_coherence_md({"hit_rate": _hit_rate_available(None)})
        assert "raw calibration n/a" in out
        assert "raw calibration None" not in out


class TestMemoAppendixRenderer:
    def test_raw_calibration_rendered_as_percent(self):
        out = "\n".join(_coherence_appendix_text({"hit_rate": _hit_rate_available(RAW)}))
        assert "37.6%" in out
        assert LONG_FLOAT_SUBSTR not in out

    def test_none_calibration_no_long_float_no_none(self):
        out = "\n".join(_coherence_appendix_text({"hit_rate": _hit_rate_available(None)}))
        assert "raw calibration n/a" in out
        assert "raw calibration None" not in out


# ---------------------------------------------------------------------------
# Crowd state counts (2026-08-07)
# ---------------------------------------------------------------------------
# The line rendered len() of the ticker LISTS, but memo_coherence caps those
# lists at [:8] for display. The count therefore saturated at 8 and could never
# exceed it: on 2026-08-06 it printed "divergent 8" against a true 11
# (unified_crowd_intelligence_status.state_counts.divergent_attention == 11).
# The same line already read insufficient_data from a COUNT field, so the
# correct pattern sat three fields away.

def _crowd(**over):
    base = {
        "available": True,
        "cross_source_confirmed": [f"C{i}" for i in range(8)],   # capped list
        "divergent": [f"D{i}" for i in range(8)],                # capped list
        "insufficient_data_count": 60,
        "social_sentiment_status": "PLAN_LOCKED",
        "classified_state_counts": {
            "confirmed_attention": 9,
            "divergent_attention": 11,
            "insufficient_data": 60,
        },
    }
    base.update(over)
    return base


def _crowd_line(crowd):
    out = _coherence_appendix_text({"crowd": crowd})
    return next(l for l in out if "Crowd (sandbox" in l)


class TestCrowdCountsNotListLengths:
    def test_divergent_uses_the_count_not_the_capped_list(self):
        line = _crowd_line(_crowd())
        assert "divergent 11" in line, line
        assert "divergent 8" not in line, line

    def test_confirmed_uses_the_count_not_the_capped_list(self):
        """Right only by luck at 2 < 8 in production; the defect is latent."""
        line = _crowd_line(_crowd())
        assert "confirmed 9" in line, line

    def test_falls_back_to_list_length_when_counts_absent(self):
        """Back-compat: older payloads carry no classified_state_counts."""
        crowd = _crowd()
        del crowd["classified_state_counts"]
        line = _crowd_line(crowd)
        assert "divergent 8" in line, line

    def test_insufficient_data_still_reads_its_count_field(self):
        assert "insufficient_data 60" in _crowd_line(_crowd())
