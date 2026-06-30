"""Phase 1 — run identity, lineage, and artifact integrity.

Covers the immutable daily run manifest, deterministic run_id + config hash,
the canonical lineage envelope helper, and mixed-run / completeness guards.

TDD: these were written BEFORE portfolio_automation/run_manifest.py existed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import portfolio_automation.run_manifest as rm
from portfolio_automation.next_stage.contracts import observe_only_envelope, lineage


_NOW = "2026-06-30T09:00:00+00:00"
_LATER = "2026-06-30T09:08:00+00:00"


# ---------------------------------------------------------------------------
# Deterministic identity
# ---------------------------------------------------------------------------


def test_run_id_is_deterministic_for_date_and_mode():
    a = rm.make_run_id("daily", generated_at=_NOW)
    b = rm.make_run_id("daily", generated_at=_NOW)
    assert a == b == "2026-06-30_daily_official"


def test_config_hash_changes_with_content(tmp_path):
    p1 = tmp_path / "c1.json"
    p2 = tmp_path / "c2.json"
    p1.write_text('{"a": 1}')
    p2.write_text('{"a": 2}')
    h1 = rm.compute_config_hash(p1)
    h2 = rm.compute_config_hash(p2)
    assert h1 != h2
    # same content -> same hash (idempotent)
    p3 = tmp_path / "c3.json"
    p3.write_text('{"a": 1}')
    assert rm.compute_config_hash(p3) == h1


def test_config_hash_missing_file_is_labeled_not_crash(tmp_path):
    assert rm.compute_config_hash(tmp_path / "nope.json") == "missing"


# ---------------------------------------------------------------------------
# Manifest shape + lineage fields
# ---------------------------------------------------------------------------

_REQUIRED = {
    "run_id", "started_at", "completed_at", "data_as_of", "source_commit",
    "config_hash", "pipeline_mode", "runtime", "upstream_freshness",
    "status", "failure_stage", "schema_version", "artifact_type",
}


def test_manifest_carries_all_required_fields(tmp_path):
    m = rm.build_manifest(
        run_id="2026-06-30_daily_official", started_at=_NOW, data_as_of=_NOW,
        source_commit="abc1234", config_hash="deadbeef", pipeline_mode="daily",
    )
    assert _REQUIRED.issubset(set(m)), _REQUIRED - set(m)
    assert m["artifact_type"] == "run_manifest"
    assert m["status"] == "running"          # default before completion
    assert m["completed_at"] is None
    assert m["failure_stage"] is None


# ---------------------------------------------------------------------------
# Begin / complete lifecycle + completeness guard
# ---------------------------------------------------------------------------


def test_begin_run_writes_running_then_complete_marks_complete(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"x": 1}')
    started = rm.begin_run(tmp_path, pipeline_mode="daily", started_at=_NOW,
                           data_as_of=_NOW, config_path=cfg)
    assert started["status"] == "running"
    assert rm.is_complete(started) is False
    # persisted manifest reads back as running
    on_disk = rm.read_manifest(tmp_path)
    assert on_disk["run_id"] == started["run_id"]
    assert rm.is_complete(on_disk) is False

    done = rm.complete_run(tmp_path, completed_at=_LATER, status="complete")
    assert done["status"] == "complete"
    assert done["completed_at"] == _LATER
    assert rm.is_complete(done) is True
    assert rm.is_complete(rm.read_manifest(tmp_path)) is True


def test_failed_run_is_not_complete_and_records_stage(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    rm.begin_run(tmp_path, pipeline_mode="daily", started_at=_NOW, config_path=cfg)
    done = rm.complete_run(tmp_path, completed_at=_LATER, status="failed",
                           failure_stage="decision_engine")
    assert done["status"] == "failed"
    assert done["failure_stage"] == "decision_engine"
    assert rm.is_complete(done) is False


def test_read_manifest_absent_returns_none(tmp_path):
    assert rm.read_manifest(tmp_path) is None


# ---------------------------------------------------------------------------
# Mixed-run detection
# ---------------------------------------------------------------------------


def test_coherent_run_ids_accepts_matching_and_rejects_mixed():
    rid = "2026-06-30_daily_official"
    good = [{"run_id": rid}, {"run_id": rid}]
    bad = [{"run_id": rid}, {"run_id": "2026-06-29_daily_official"}]
    assert rm.coherent_run_ids(rid, good) is True
    assert rm.coherent_run_ids(rid, bad) is False
    # artifacts with no run_id are not treated as a coherent match (degrade honestly)
    assert rm.coherent_run_ids(rid, [{"foo": 1}]) is False


# ---------------------------------------------------------------------------
# Lineage envelope helper (additive to observe_only_envelope)
# ---------------------------------------------------------------------------

_LINEAGE_KEYS = {
    "run_id", "data_as_of", "producer", "source_commit", "config_hash",
    "upstream_refs", "quality", "freshness",
}


def test_lineage_helper_returns_canonical_keys():
    out = lineage(run_id="r1", data_as_of=_NOW, producer="decision_engine",
                  source_commit="abc", config_hash="def")
    assert _LINEAGE_KEYS.issubset(set(out))
    assert out["upstream_refs"] == []          # default empty list, not None
    assert out["quality"] == "ok"
    assert out["freshness"] == "fresh"


def test_lineage_merges_into_envelope_without_breaking_safety():
    env = observe_only_envelope(_NOW, **lineage(
        run_id="r1", data_as_of=_NOW, producer="p", source_commit="c",
        config_hash="h", upstream_refs=["decision_plan.json"]))
    assert env["observe_only"] is True       # safety flags never overridden
    assert env["no_trade"] is True
    assert env["run_id"] == "r1"
    assert env["producer"] == "p"
    assert env["upstream_refs"] == ["decision_plan.json"]


# ---------------------------------------------------------------------------
# Phase 1 wiring: daily_run_status surfaces the run manifest (DoD: "pipeline
# status identifies the exact coherent run")
# ---------------------------------------------------------------------------


def test_daily_run_status_surfaces_run_manifest(tmp_path):
    from portfolio_automation.daily_run_status import build_daily_run_status
    (tmp_path / "config.json").write_text("{}")
    rm.begin_run(tmp_path, pipeline_mode="daily", started_at=_NOW,
                 config_path=tmp_path / "config.json")
    payload = build_daily_run_status(root=tmp_path)
    block = payload["run_manifest"]
    assert block["present"] is True
    assert block["run_id"] == "2026-06-30_daily_official"
    assert block["status"] == "running"
    assert block["complete"] is False


def test_daily_run_status_run_manifest_absent_is_safe(tmp_path):
    from portfolio_automation.daily_run_status import build_daily_run_status
    payload = build_daily_run_status(root=tmp_path)
    assert payload["run_manifest"] == {
        "present": False, "run_id": None, "status": None, "complete": False}
