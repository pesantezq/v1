"""Phase 2 — immutable daily input snapshot.

One frozen point-in-time input bundle (references + content hashes, not copies)
so production and every daily simulation evaluate the SAME data. Each input
carries observation/available-as-of/freshness/quality/source; future-dated
inputs are rejected; the snapshot hash is stable for identical inputs and
changes when a meaningful input changes.

TDD: written before portfolio_automation/daily_input_snapshot.py existed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import portfolio_automation.daily_input_snapshot as ds


_NOW = "2026-06-30T12:00:00+00:00"


def _src(key, path, kind="market", source="test", stale_after_hours=48.0):
    return ds.InputSource(key=key, path=path, kind=kind, source=source,
                          stale_after_hours=stale_after_hours)


def _write(root: Path, rel: str, payload: dict, generated_at: str | None = None):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if generated_at is not None:
        payload = {"generated_at": generated_at, **payload}
    p.write_text(json.dumps(payload))
    return p


# ---------------------------------------------------------------------------
# Per-input record shape
# ---------------------------------------------------------------------------

_REC_KEYS = {"key", "kind", "source", "path", "present", "observation_timestamp",
             "available_as_of", "freshness", "age_hours", "quality", "content_hash"}


def test_input_record_carries_provenance(tmp_path):
    _write(tmp_path, "outputs/latest/holdings.json", {"AAPL": 1},
           generated_at="2026-06-30T09:00:00+00:00")
    snap = ds.build_input_snapshot(
        tmp_path, run_id="2026-06-30_daily_official", data_as_of=_NOW, now=_NOW,
        sources=[_src("holdings", "outputs/latest/holdings.json", kind="holdings")])
    rec = snap["inputs"][0]
    assert _REC_KEYS.issubset(set(rec))
    assert rec["present"] is True
    assert rec["quality"] == "ok"
    assert rec["available_as_of"] == _NOW
    assert rec["observation_timestamp"] == "2026-06-30T09:00:00+00:00"
    assert rec["content_hash"]  # non-empty


def test_missing_input_degrades_not_crash(tmp_path):
    snap = ds.build_input_snapshot(
        tmp_path, run_id="r", data_as_of=_NOW, now=_NOW,
        sources=[_src("gone", "outputs/latest/nope.json")])
    rec = snap["inputs"][0]
    assert rec["present"] is False
    assert rec["quality"] == "missing"
    assert rec["content_hash"] is None


# ---------------------------------------------------------------------------
# Future-date rejection (no look-ahead leak)
# ---------------------------------------------------------------------------


def test_future_dated_input_is_rejected(tmp_path):
    _write(tmp_path, "outputs/latest/fut.json", {"x": 1},
           generated_at="2026-07-15T00:00:00+00:00")  # AFTER now
    snap = ds.build_input_snapshot(
        tmp_path, run_id="r", data_as_of=_NOW, now=_NOW,
        sources=[_src("fut", "outputs/latest/fut.json")])
    rec = snap["inputs"][0]
    assert rec["quality"] == "invalid_future"
    assert rec["freshness"] == "invalid_future"
    assert snap["future_rejected_count"] == 1
    # excluded from the coherent snapshot hash (cannot leak into input identity)
    assert rec["content_hash"] not in (snap["snapshot_hash"],)


# ---------------------------------------------------------------------------
# Stale policy (degrade-but-usable)
# ---------------------------------------------------------------------------


def test_stale_input_degrades_but_is_referenced(tmp_path):
    _write(tmp_path, "outputs/latest/old.json", {"x": 1},
           generated_at="2026-06-25T00:00:00+00:00")  # ~5 days old
    snap = ds.build_input_snapshot(
        tmp_path, run_id="r", data_as_of=_NOW, now=_NOW,
        sources=[_src("old", "outputs/latest/old.json", stale_after_hours=48.0)])
    rec = snap["inputs"][0]
    assert rec["quality"] == "stale"
    assert rec["present"] is True
    assert rec["content_hash"]  # still referenced (real data, just old)
    assert snap["stale_count"] == 1


# ---------------------------------------------------------------------------
# Snapshot hash: stable for identical inputs, changes on meaningful change
# ---------------------------------------------------------------------------


def test_snapshot_hash_is_idempotent_and_change_sensitive(tmp_path):
    _write(tmp_path, "outputs/latest/a.json", {"v": 1}, generated_at="2026-06-30T09:00:00+00:00")
    srcs = [_src("a", "outputs/latest/a.json")]
    h1 = ds.build_input_snapshot(tmp_path, run_id="r", data_as_of=_NOW, now=_NOW, sources=srcs)["snapshot_hash"]
    # retry: same inputs -> same hash (idempotent, no silent input drift)
    h2 = ds.build_input_snapshot(tmp_path, run_id="r", data_as_of=_NOW, now=_NOW, sources=srcs)["snapshot_hash"]
    assert h1 == h2
    # meaningful change -> different hash
    _write(tmp_path, "outputs/latest/a.json", {"v": 2}, generated_at="2026-06-30T09:00:00+00:00")
    h3 = ds.build_input_snapshot(tmp_path, run_id="r", data_as_of=_NOW, now=_NOW, sources=srcs)["snapshot_hash"]
    assert h3 != h1


# ---------------------------------------------------------------------------
# Single frozen source: production + shadow read the SAME snapshot
# ---------------------------------------------------------------------------


def test_write_read_roundtrip_freezes_inputs(tmp_path):
    _write(tmp_path, "outputs/latest/prices.json", {"AAPL": 200},
           generated_at="2026-06-30T09:00:00+00:00")
    snap = ds.build_input_snapshot(
        tmp_path, run_id="r", data_as_of=_NOW, now=_NOW,
        sources=[_src("prices", "outputs/latest/prices.json", kind="prices")])
    ds.write_input_snapshot(tmp_path, snap)
    # two independent consumers (production + a shadow strategy) read the same frozen bundle
    a = ds.read_input_snapshot(tmp_path)
    b = ds.read_input_snapshot(tmp_path)
    assert a["snapshot_hash"] == b["snapshot_hash"] == snap["snapshot_hash"]
    assert ds.load_input(a, "prices")["content_hash"] == ds.load_input(b, "prices")["content_hash"]
    assert a["observe_only"] is True and a["no_trade"] is True


# ---------------------------------------------------------------------------
# Envelope + lineage + summary integrity
# ---------------------------------------------------------------------------


def test_snapshot_carries_envelope_and_summary(tmp_path):
    _write(tmp_path, "outputs/latest/x.json", {"v": 1}, generated_at="2026-06-30T09:00:00+00:00")
    snap = ds.build_input_snapshot(
        tmp_path, run_id="2026-06-30_daily_official", data_as_of=_NOW, now=_NOW,
        source_commit="abc1234", config_hash="deadbeef",
        sources=[_src("x", "outputs/latest/x.json")])
    assert snap["artifact_type"] == "daily_input_snapshot"
    assert snap["run_id"] == "2026-06-30_daily_official"
    assert snap["data_as_of"] == _NOW
    assert snap["producer"] == "daily_input_snapshot"
    assert snap["source_commit"] == "abc1234"
    assert snap["input_count"] == 1 and snap["valid_count"] == 1


# ---------------------------------------------------------------------------
# Operator visibility: daily_run_status surfaces the snapshot (Phase 2 DoD)
# ---------------------------------------------------------------------------


def test_daily_run_status_surfaces_input_snapshot(tmp_path):
    from portfolio_automation.daily_run_status import build_daily_run_status
    _write(tmp_path, "outputs/latest/x.json", {"v": 1}, generated_at="2026-06-30T09:00:00+00:00")
    snap = ds.build_input_snapshot(tmp_path, run_id="r", data_as_of=_NOW, now=_NOW,
                                   sources=[_src("x", "outputs/latest/x.json")])
    ds.write_input_snapshot(tmp_path, snap)
    blk = build_daily_run_status(root=tmp_path)["input_snapshot"]
    assert blk["present"] is True
    assert blk["snapshot_hash"] == snap["snapshot_hash"]
    assert blk["valid_count"] == 1
    assert blk["future_rejected_count"] == 0


def test_daily_run_status_input_snapshot_absent_is_safe(tmp_path):
    from portfolio_automation.daily_run_status import build_daily_run_status
    blk = build_daily_run_status(root=tmp_path)["input_snapshot"]
    assert blk == {"present": False, "snapshot_hash": None, "valid_count": 0,
                   "stale_count": 0, "missing_count": 0, "future_rejected_count": 0}


# ---------------------------------------------------------------------------
# Phase 13 — operator surface exposes the new SQG producers (graceful degrade)
# ---------------------------------------------------------------------------


def test_daily_run_status_sqg_surfaces_block(tmp_path):
    from portfolio_automation.daily_run_status import build_daily_run_status
    blk = build_daily_run_status(root=tmp_path)["sqg_surfaces"]
    # absent artifacts degrade gracefully (present=False), no crash
    for key in ("scenario_risk", "quant_feedback", "semantic_liveness"):
        assert key in blk and blk[key]["present"] is False
