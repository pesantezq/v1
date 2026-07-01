"""Decision-plan lineage stamp (SQG Phase 1 deferred item, shipped 2026-07-01).

The decision_plan.json payload is stamped with the run manifest's lineage
(run_id + provenance) so the source-of-truth artifact is traceable to its run.
This is an ADDITIVE output-payload change written in main._write_decision_engine_outputs
— it never touches decision_engine.py, scoring logic, or any *_score semantics.
The three tests below lock that boundary: lineage present when the manifest is,
degraded-but-stable when it isn't, and decision/score content passed through verbatim.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from main import _write_decision_engine_outputs

_LOG = logging.getLogger("test_decision_plan_lineage")

# every protected score field, to prove the stamp leaves decision content untouched
_SCORE_FIELDS = (
    "signal_score", "confidence_score", "effective_score",
    "conviction_score", "final_rank_score", "recommendation_score",
)


def _make_output_dir(tmp_path: Path) -> Path:
    out = tmp_path / "outputs" / "latest"
    out.mkdir(parents=True)
    (tmp_path / "outputs" / "policy").mkdir(parents=True)
    return out


def _decisions() -> list[dict]:
    return [{
        "recommended_action": "WAIT",
        "decision_type": "hold",
        "signal_score": 0.42,
        "confidence_score": 0.61,
        "effective_score": 0.30,
        "conviction_score": 0.55,
        "final_rank_score": 0.70,
        "recommendation_score": 0.83,
    }]


def _write(tmp_path: Path, out: Path) -> dict:
    _write_decision_engine_outputs(
        out,
        {"decision_plan": _decisions(), "decision_plan_summary": "summary"},
        "daily",
        _LOG,
        explainer_root=tmp_path,
    )
    return json.loads((out / "decision_plan.json").read_text(encoding="utf-8"))


def test_decision_plan_carries_lineage_when_manifest_present(tmp_path):
    out = _make_output_dir(tmp_path)
    (tmp_path / "outputs" / "policy" / "run_manifest.json").write_text(json.dumps({
        "run_id": "2026-07-01_daily_official",
        "data_as_of": "2026-07-01T09:00:00",
        "source_commit": "abcdef1234",
        "config_hash": "deadbeefcafe",
        "status": "running",
    }), encoding="utf-8")

    payload = _write(tmp_path, out)

    assert payload["run_id"] == "2026-07-01_daily_official"
    lin = payload["lineage"]
    assert lin["run_id"] == "2026-07-01_daily_official"
    assert lin["producer"] == "decision_engine"
    assert lin["source_commit"] == "abcdef1234"
    assert lin["config_hash"] == "deadbeefcafe"
    assert lin["data_as_of"] == "2026-07-01T09:00:00"
    assert "run_manifest.json" in lin["upstream_refs"]
    assert lin["quality"] == "ok"


def test_decision_plan_lineage_degrades_without_manifest(tmp_path):
    out = _make_output_dir(tmp_path)  # no manifest written

    payload = _write(tmp_path, out)

    # Stable shape: the keys exist even when no run identity is available.
    assert "run_id" in payload and "lineage" in payload
    assert payload["lineage"]["quality"] == "degraded"
    assert payload["run_id"] in (None, "unknown")
    # still a valid, complete plan
    assert payload["total_decisions"] == 1
    assert payload["observe_only"] is True


def test_lineage_stamp_leaves_scores_and_decisions_untouched(tmp_path):
    out = _make_output_dir(tmp_path)
    (tmp_path / "outputs" / "policy" / "run_manifest.json").write_text(json.dumps({
        "run_id": "r", "data_as_of": "d", "source_commit": "c", "config_hash": "h",
    }), encoding="utf-8")
    original = _decisions()[0]

    payload = _write(tmp_path, out)

    row = payload["decisions"][0]
    for k in _SCORE_FIELDS:
        assert row[k] == original[k], f"{k} altered by the lineage stamp"
    assert payload["total_decisions"] == 1
    # the stamp adds no score-named key at the top level
    assert not any("score" in k for k in payload)
