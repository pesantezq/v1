"""Memo lines that must not claim more than their source supports.

Second batch from the 2026-08-07 memo review (the first shipped in 371cdf0c).
Same family throughout: the JSON is correct and the rendered layer overstates.

#7  "stale baseline" names ``pre_tracker_unknown``, which IS
    ``outcome_attribution.unattributed_signals`` — 352 signals the tracker could
    not assign to any gauge. Calling it a baseline implies a measured prior era.

#10 The health section reads ``data_health`` from the summary, which only knows
    about artifact presence AT SUMMARY-GENERATION time. It never consulted
    daily_run_status, so on 2026-08-06 the memo said "2 advisory artifacts not
    yet populated" while the run was ``ok_with_warnings`` with
    ``content_warn_count=1``. The run's own warning state never reached the
    operator.

#11 Per-source crowd health rendered only ``status``, discarding
    ``record_count`` — so four sources reporting ``ok`` with ``record_count: 0``
    printed a bare "ok". Compounding: ``_CROWD_SOURCE_LABELS`` was a hardcoded
    list that had drifted from the artifact, showing "n/a" for three sources
    that no longer exist while omitting bluesky/mastodon/lemmy entirely.

#12 "Generated:" displayed the DECISION RUN's timestamp, not the render time
    (09:03:52 vs a 10:31:22 file write — 88 minutes). The value is deliberate
    (it is the as-of and the dedup key); the LABEL was what lied.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchlist_scanner.daily_memo import (
    _crowd_source_health_lines,
    _health_items,
)


# --------------------------------------------------------------------------
# #10 — the run's own warning state must reach the memo
# --------------------------------------------------------------------------
class TestHealthItemsSurfaceRunStatus:
    def test_ok_with_warnings_is_reported(self):
        items = _health_items({}, run_status={
            "overall_status": "ok_with_warnings", "content_warn_count": 1})
        joined = " ".join(items).lower()
        assert "ok_with_warnings" in joined or "warning" in joined
        assert "1" in " ".join(items)

    def test_clean_run_adds_nothing(self):
        assert _health_items({}, run_status={
            "overall_status": "ok", "content_warn_count": 0}) == []

    def test_absent_run_status_keeps_previous_behaviour(self):
        assert _health_items({}) == []

    def test_run_status_line_survives_the_three_item_cap(self):
        """It must not be crowded out by lower-priority advisory gaps."""
        items = _health_items(
            {"defaulting_artifact_details": ["a"], "optional_artifact_details": ["b"],
             "degraded_mode": True, "data_mode": "cached"},
            run_status={"overall_status": "partial", "content_warn_count": 3})
        assert any("partial" in i or "3" in i for i in items[:3]), items

    def test_failed_run_is_reported(self):
        items = _health_items({}, run_status={
            "overall_status": "failed", "content_warn_count": 0})
        assert any("failed" in i.lower() for i in items)


# --------------------------------------------------------------------------
# #11 — a source reporting "ok" while returning nothing
# --------------------------------------------------------------------------
def _write_crowd_health(root: Path, records):
    d = root / "outputs" / "sandbox" / "discovery"
    d.mkdir(parents=True, exist_ok=True)
    import json
    (d / "crowd_source_health.json").write_text(json.dumps({"records": records}))


class TestCrowdSourceHealthShowsEmptiness:
    def test_ok_with_zero_records_is_qualified(self, tmp_path):
        _write_crowd_health(tmp_path, [
            {"source_name": "apewisdom", "status": "ok", "record_count": 0}])
        line = next(l for l in _crowd_source_health_lines(tmp_path)
                    if "apewisdom" in l.lower())
        assert "0" in line, f"a source returning nothing must say so: {line}"

    def test_ok_with_records_reads_plainly(self, tmp_path):
        _write_crowd_health(tmp_path, [
            {"source_name": "apewisdom", "status": "ok", "record_count": 42}])
        line = next(l for l in _crowd_source_health_lines(tmp_path)
                    if "apewisdom" in l.lower())
        assert "42" in line

    def test_sources_come_from_the_artifact_not_a_hardcoded_list(self, tmp_path):
        """The static list had drifted: it named three dead sources and hid
        bluesky/mastodon/lemmy, which are the ones actually running."""
        _write_crowd_health(tmp_path, [
            {"source_name": "bluesky", "status": "ok", "record_count": 0},
            {"source_name": "mastodon", "status": "ok", "record_count": 3},
            {"source_name": "lemmy", "status": "degraded", "record_count": 0}])
        joined = " ".join(_crowd_source_health_lines(tmp_path)).lower()
        for present in ("bluesky", "mastodon", "lemmy"):
            assert present in joined, f"{present} is live and must appear"
        for gone in ("stocktwits", "finnhub", "quiver"):
            assert gone not in joined, f"{gone} is not in the artifact"

    def test_absent_artifact_still_yields_nothing(self, tmp_path):
        assert _crowd_source_health_lines(tmp_path) == []

    def test_missing_record_count_degrades_quietly(self, tmp_path):
        _write_crowd_health(tmp_path, [
            {"source_name": "apewisdom", "status": "ok"}])
        line = next(l for l in _crowd_source_health_lines(tmp_path)
                    if "apewisdom" in l.lower())
        assert "ok" in line


# --------------------------------------------------------------------------
# Renderer purity — the guard that caught my own first attempt
# --------------------------------------------------------------------------
# The run-status fix was first wired by calling _load_run_status() INSIDE
# _health_items' callers, which made build_daily_memo({}) read
# outputs/latest/daily_run_status.json from the CWD. Four existing tests broke
# immediately, and rightly: a pure renderer that reads live operator state is
# the ambient-leakage class this repo has been burned by before. run_status is
# now a parameter and the I/O lives in the pipeline entry point.

class TestRendererStaysPure:
    def test_compact_memo_ignores_ambient_run_status(self, tmp_path, monkeypatch):
        from watchlist_scanner.daily_memo import build_daily_memo
        monkeypatch.chdir(tmp_path)          # no artifacts here at all
        assert "SYSTEM / DATA HEALTH" not in build_daily_memo({})

    def test_run_status_only_arrives_by_parameter(self):
        from watchlist_scanner.daily_memo import build_daily_memo
        clean = build_daily_memo({})
        warned = build_daily_memo({}, run_status={
            "overall_status": "ok_with_warnings", "content_warn_count": 1})
        assert clean != warned, "the parameter must actually reach the output"
        assert "ok_with_warnings" in warned

    def test_md_builder_takes_the_same_parameter(self):
        from watchlist_scanner.daily_memo import build_daily_memo_md
        md = build_daily_memo_md({}, run_status={
            "overall_status": "failed", "content_warn_count": 0})
        assert "failed" in md


# --------------------------------------------------------------------------
# Brief vs appendix (2026-08-07)
# --------------------------------------------------------------------------
# The memo declared "## Operator / System Appendix — Technical diagnostics, not
# required for the daily decision" and then emitted ELEVEN MORE ## sections as
# siblings of it. The document said where the brief ended; the structure did
# not. Appendix sections are now ###, so the brief is the eight decision
# sections a reader actually needs.

_BRIEF_SECTIONS = {
    "Today's Verdict", "Top Insight", "Today's Capital Plan", "What To Do Today",
    "Deferred Recommendations", "Bottom Line", "Risk Focus", "What Changed",
    # legacy capital headers, still emitted by the empty-summary fallback
    "Top Decisions", "Capital Actions",
}
_APPENDIX_MARKER = "## Operator / System Appendix"


def _md_h2s(md: str) -> list[str]:
    return [l[3:].strip() for l in md.splitlines()
            if l.startswith("## ") and not l.startswith("### ")]


class TestBriefAppendixSplit:
    def test_nothing_after_the_marker_is_a_top_level_section(self):
        from watchlist_scanner.daily_memo import build_daily_memo_md
        md = build_daily_memo_md({})
        if _APPENDIX_MARKER not in md:
            return  # nothing to police on an empty summary
        tail = md.split(_APPENDIX_MARKER, 1)[1]
        assert not _md_h2s(tail), (
            f"appendix content must be ###, found top-level: {_md_h2s(tail)}")

    def test_brief_sections_stay_top_level(self):
        from watchlist_scanner.daily_memo import build_daily_memo_md
        md = build_daily_memo_md({})
        head = md.split(_APPENDIX_MARKER, 1)[0]
        for h in _md_h2s(head):
            assert h in _BRIEF_SECTIONS or "Appendix" in h, (
                f"'{h}' is above the appendix marker but is not a brief section")


class TestDashboardStillSeesDemotedSections:
    """Heading level is load-bearing: dash_memo maps ## headers into six
    buckets, so demoting without teaching it ### would have silently folded
    every appendix section into whichever ## preceded it."""

    def test_h3_headers_are_still_section_boundaries(self):
        from gui_v2.data.dash_memo import _parse_memo
        secs = _parse_memo(
            "## Today's Verdict\n> v\n\n"
            "### Risk Delta\n- concentration ok\n\n"
            "### Portfolio Growth\n- up 1%\n")
        assert "Risk Focus" in secs, "### Risk Delta must map to Risk Focus"
        assert "Quant Notes" in secs, "### Portfolio Growth must map to Quant Notes"
        assert any("concentration ok" in l for l in secs["Risk Focus"])

    def test_h2_headers_still_work(self):
        from gui_v2.data.dash_memo import _parse_memo
        secs = _parse_memo("## Risk Delta\n- concentration ok\n")
        assert "Risk Focus" in secs

    def test_demoted_content_does_not_bleed_into_the_previous_section(self):
        from gui_v2.data.dash_memo import _parse_memo
        secs = _parse_memo(
            "## Operator / System Appendix\n- diag line\n\n"
            "### Portfolio Growth\n- growth line\n")
        assert not any("growth line" in l for l in secs.get("Data Quality", [])), (
            "growth content must not fold into the appendix bucket")
