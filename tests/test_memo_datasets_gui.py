"""Tests for the GUI domain sub-tab loader over outputs/latest/memo_datasets.json.

Observe-only: this loader never recomputes memo content, it only reshapes the
memo_datasets.json artifact for the /dashboard/memo domain sub-tabs. Must be
null-tolerant (absent/corrupt artifact never raises).
"""
import json
from pathlib import Path

from gui_v2.data.dash_memo_datasets import collect_memo_datasets_view


def test_view_absent_is_null_tolerant(tmp_path):
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    v = collect_memo_datasets_view(tmp_path)
    assert v["has_datasets"] is False and v["domains"] == []


def test_view_shapes_domains(tmp_path):
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (latest / "memo_datasets.json").write_text(json.dumps({
        "feeds_decision_engine": False, "domains": {
            "portfolio": {"headline": "Portfolio & Capital", "status": "ok",
                          "sections": [{"title": "Bottom Line", "lines": ["x"], "severity": "info"}],
                          "warnings": []},
            "risk": {"headline": "Risk", "status": "unavailable", "sections": [], "warnings": []}}}))
    v = collect_memo_datasets_view(tmp_path)
    assert v["has_datasets"] is True
    keys = {d["key"] for d in v["domains"]}
    assert "portfolio" in keys
    port = next(d for d in v["domains"] if d["key"] == "portfolio")
    assert port["status"] == "ok" and port["sections"]
