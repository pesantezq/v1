"""
Hermetic tests for the Agent Production Export subsystem.

No dependency on /opt/stockbot, Hetzner, production credentials, or real
pipeline artifacts. Everything is built from tmp_path fixtures.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from portfolio_automation import agent_export as ax


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _write(root: Path, relpath: str, payload) -> Path:
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (dict, list)):
        p.write_text(json.dumps(payload), encoding="utf-8")
    else:
        p.write_text(str(payload), encoding="utf-8")
    return p


@pytest.fixture
def artifacts_root(tmp_path: Path) -> Path:
    """A minimal but valid outputs/ tree with the two required artifacts + a few optional."""
    root = tmp_path / "outputs"
    _write(root, "latest/decision_plan.json", {"decisions": [], "observe_only": True})
    _write(root, "latest/decision_plan.md", "# Decision Plan\n")
    _write(root, "latest/system_decision_summary.json", {"summary": "ok"})
    _write(root, "latest/daily_run_status.json", {"overall_status": "ok"})
    _write(root, "portfolio/portfolio_snapshot.json", {"positions": []})
    return root


def _build(artifacts_root: Path, output_root: Path, **kw):
    defaults = dict(
        production_git_sha="a" * 40,
        production_run_id="2026-08-08_daily",
        run_started_at="2026-08-08T09:00:00Z",
        run_completed_at="2026-08-08T09:05:00Z",
        artifacts_root=artifacts_root,
        output_root=output_root,
    )
    defaults.update(kw)
    return ax.build_snapshot(**defaults)


# ---------------------------------------------------------------------------
# Normal build + determinism
# ---------------------------------------------------------------------------
def test_normal_build_produces_valid_snapshot(artifacts_root, tmp_path):
    out = tmp_path / "out"
    manifest = _build(artifacts_root, out)
    snap = out / ax.EXPORT_SUBDIR / ax.SNAPSHOTS_SUBDIR / manifest["snapshot_id"]
    assert snap.is_dir()
    result = ax.validate_agent_snapshot(snap)
    assert result["valid"], result["errors"]
    # required artifacts present
    names = {a["logical_name"] for a in manifest["artifacts"]}
    assert {"decision_plan", "system_decision_summary"} <= names
    # latest pointer written and points at this snapshot
    pointer = json.loads((out / ax.EXPORT_SUBDIR / ax.LATEST_POINTER_NAME).read_text())
    assert pointer["snapshot_id"] == manifest["snapshot_id"]


def test_determinism_same_inputs_same_fingerprint(artifacts_root, tmp_path):
    m1 = _build(artifacts_root, tmp_path / "o1", created_at="2026-01-01T00:00:00Z")
    m2 = _build(artifacts_root, tmp_path / "o2", created_at="2026-12-31T23:59:59Z")
    # created_at differs but fingerprint must match (it is excluded from the hash)
    assert m1["created_at"] != m2["created_at"]
    assert m1["snapshot_hash"] == m2["snapshot_hash"]
    assert m1["snapshot_id"] == m2["snapshot_id"]


# ---------------------------------------------------------------------------
# Required / optional missing
# ---------------------------------------------------------------------------
def test_required_missing_fails_closed(artifacts_root, tmp_path):
    (artifacts_root / "latest/decision_plan.json").unlink()
    with pytest.raises(ax.SnapshotValidationError):
        _build(artifacts_root, tmp_path / "out")
    # no finalized snapshot left behind
    snaps = tmp_path / "out" / ax.EXPORT_SUBDIR / ax.SNAPSHOTS_SUBDIR
    assert not snaps.exists() or not any(snaps.iterdir())


def test_optional_missing_is_amber_gap(artifacts_root, tmp_path):
    (artifacts_root / "portfolio/portfolio_snapshot.json").unlink()
    manifest = _build(artifacts_root, tmp_path / "out")
    gaps = {g["logical_name"] for g in manifest["gaps"]}
    assert "portfolio_snapshot" in gaps
    assert manifest["health"]["overall_status"] == ax.AMBER


# ---------------------------------------------------------------------------
# Secret boundary
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("secret_name", [".env", "id_rsa", "auth.json", "credentials.json"])
def test_forbidden_names_are_detected(secret_name):
    assert ax.is_forbidden_name(secret_name)


def test_secret_in_allowlist_is_rejected(artifacts_root, tmp_path):
    _write(artifacts_root, "latest/.env", "SECRET=abc")
    bad = ax.ALLOWLIST + (ax.AllowlistEntry("leak", "latest/.env", False, "x", "core_decisions"),)
    with pytest.raises(ax.SecurityBoundaryError):
        _build(artifacts_root, tmp_path / "out", allowlist=bad)


def test_snapshot_never_contains_forbidden_file(artifacts_root, tmp_path):
    manifest = _build(artifacts_root, tmp_path / "out")
    snap = tmp_path / "out" / ax.EXPORT_SUBDIR / ax.SNAPSHOTS_SUBDIR / manifest["snapshot_id"]
    for dirpath, _dn, filenames in os.walk(snap):
        for fn in filenames:
            assert not ax.is_forbidden_name(fn), fn


# ---------------------------------------------------------------------------
# Path escapes
# ---------------------------------------------------------------------------
def test_traversal_relpath_rejected(artifacts_root, tmp_path):
    outside = tmp_path / "secret_outside.json"
    outside.write_text('{"x":1}')
    bad = (ax.AllowlistEntry("esc", "../secret_outside.json", False, "x", "core_decisions"),)
    with pytest.raises(ax.SecurityBoundaryError):
        _build(artifacts_root, tmp_path / "out", allowlist=bad)


def test_absolute_path_escape_rejected(artifacts_root, tmp_path):
    outside = tmp_path / "abs_secret.json"
    outside.write_text('{"x":1}')
    bad = (ax.AllowlistEntry("esc", str(outside), False, "x", "core_decisions"),)
    with pytest.raises(ax.SecurityBoundaryError):
        _build(artifacts_root, tmp_path / "out", allowlist=bad)


def test_symlink_escape_rejected(artifacts_root, tmp_path):
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    secret = outside / "data.json"
    secret.write_text('{"x":1}')
    link = artifacts_root / "latest" / "linked.json"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    bad = (ax.AllowlistEntry("linked", "latest/linked.json", False, "x", "core_decisions"),)
    with pytest.raises(ax.SecurityBoundaryError):
        _build(artifacts_root, tmp_path / "out", allowlist=bad)


# ---------------------------------------------------------------------------
# Tampering detection
# ---------------------------------------------------------------------------
def test_hash_tamper_detected(artifacts_root, tmp_path):
    manifest = _build(artifacts_root, tmp_path / "out")
    snap = tmp_path / "out" / ax.EXPORT_SUBDIR / ax.SNAPSHOTS_SUBDIR / manifest["snapshot_id"]
    victim = snap / manifest["artifacts"][0]["snapshot_path"]
    os.chmod(victim, 0o644)
    victim.write_text(victim.read_text(encoding="utf-8") + " ", encoding="utf-8")
    result = ax.validate_agent_snapshot(snap)
    assert not result["valid"]
    assert any("hash mismatch" in e for e in result["errors"])


def test_manifest_tamper_detected(artifacts_root, tmp_path):
    manifest = _build(artifacts_root, tmp_path / "out")
    snap = tmp_path / "out" / ax.EXPORT_SUBDIR / ax.SNAPSHOTS_SUBDIR / manifest["snapshot_id"]
    mpath = snap / ax.MANIFEST_NAME
    os.chmod(mpath, 0o644)
    data = json.loads(mpath.read_text())
    data["code_identity"]["production_git_sha"] = "b" * 40  # identity tamper
    mpath.write_text(json.dumps(data), encoding="utf-8")
    result = ax.validate_agent_snapshot(snap)
    assert not result["valid"]
    assert any("snapshot_hash mismatch" in e for e in result["errors"])


def test_unexpected_file_detected(artifacts_root, tmp_path):
    manifest = _build(artifacts_root, tmp_path / "out")
    snap = tmp_path / "out" / ax.EXPORT_SUBDIR / ax.SNAPSHOTS_SUBDIR / manifest["snapshot_id"]
    os.chmod(snap / ax.ARTIFACTS_DIRNAME, 0o755)
    (snap / ax.ARTIFACTS_DIRNAME / "stowaway.json").write_text("{}", encoding="utf-8")
    result = ax.validate_agent_snapshot(snap)
    assert not result["valid"]
    assert any("unexpected file" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# Atomicity / duplicate id
# ---------------------------------------------------------------------------
def test_partial_build_not_finalized(artifacts_root, tmp_path, monkeypatch):
    # Force a failure AT the finalize rename; the final dir must not exist and no
    # partial temp should masquerade as a snapshot.
    def boom(*_a, **_k):
        raise RuntimeError("simulated crash at finalize")
    monkeypatch.setattr(ax.os, "replace", boom)
    with pytest.raises(RuntimeError):
        _build(artifacts_root, tmp_path / "out")
    monkeypatch.undo()
    snaps = tmp_path / "out" / ax.EXPORT_SUBDIR / ax.SNAPSHOTS_SUBDIR
    assert not snaps.exists() or not any(snaps.iterdir())
    # temp dir cleaned up
    tmpd = tmp_path / "out" / ax.EXPORT_SUBDIR / ax.TMP_SUBDIR
    assert not tmpd.exists() or not any(tmpd.iterdir())


def test_duplicate_id_identical_is_idempotent(artifacts_root, tmp_path):
    out = tmp_path / "out"
    m1 = _build(artifacts_root, out)
    m2 = _build(artifacts_root, out)  # same inputs -> identical content
    assert m1["snapshot_hash"] == m2["snapshot_hash"]


def test_duplicate_id_different_content_fails_closed(artifacts_root, tmp_path):
    out = tmp_path / "out"
    _build(artifacts_root, out)
    # change an artifact's bytes -> same id (run-id derived) but different content
    (artifacts_root / "latest/decision_plan.json").write_text(
        json.dumps({"decisions": ["CHANGED"], "observe_only": True}), encoding="utf-8")
    with pytest.raises(ax.SnapshotExistsError):
        _build(artifacts_root, out)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------
def test_git_sha_and_run_id_recorded(artifacts_root, tmp_path):
    manifest = _build(artifacts_root, tmp_path / "out",
                      production_git_sha="c" * 40, production_run_id="RUN-XYZ")
    assert manifest["code_identity"]["production_git_sha"] == "c" * 40
    assert manifest["run_identity"]["production_run_id"] == "RUN-XYZ"
    assert manifest["snapshot_id"] == "snap-RUN-XYZ"


def test_empty_git_sha_rejected(artifacts_root, tmp_path):
    with pytest.raises(ax.SnapshotValidationError):
        _build(artifacts_root, tmp_path / "out", production_git_sha="")


# ---------------------------------------------------------------------------
# SHA comparison helper
# ---------------------------------------------------------------------------
def test_compare_shas_match():
    assert ax.compare_shas("abc123", "abc123") == "MATCH"
    assert ax.compare_shas("abc123def", "abc123") == "MATCH"  # prefix


def test_compare_shas_unknown():
    assert ax.compare_shas(None, "x") == "UNKNOWN"
    assert ax.compare_shas("x", "") == "UNKNOWN"
    assert ax.compare_shas("aaa", "bbb") == "UNKNOWN"  # no ancestry


def test_compare_shas_behind_ahead():
    ancestry = ["c0", "c1", "c2", "c3"]  # oldest -> newest
    assert ax.compare_shas("c2", "c1", ancestry) == "SHADOW_BEHIND"
    assert ax.compare_shas("c1", "c3", ancestry) == "SHADOW_AHEAD"
    assert ax.compare_shas("c2", "c2", ancestry) == "MATCH"
    assert ax.compare_shas("c2", "zz", ancestry) == "UNKNOWN"


# ---------------------------------------------------------------------------
# Consumer validation is non-mutating
# ---------------------------------------------------------------------------
def test_validation_does_not_mutate(artifacts_root, tmp_path):
    manifest = _build(artifacts_root, tmp_path / "out")
    snap = tmp_path / "out" / ax.EXPORT_SUBDIR / ax.SNAPSHOTS_SUBDIR / manifest["snapshot_id"]
    before = {p: p.stat().st_mtime_ns for p in snap.rglob("*") if p.is_file()}
    ax.validate_agent_snapshot(snap)
    after = {p: p.stat().st_mtime_ns for p in snap.rglob("*") if p.is_file()}
    assert before == after


def test_finalized_snapshot_is_readonly(artifacts_root, tmp_path):
    manifest = _build(artifacts_root, tmp_path / "out")
    snap = tmp_path / "out" / ax.EXPORT_SUBDIR / ax.SNAPSHOTS_SUBDIR / manifest["snapshot_id"]
    mode = stat.S_IMODE((snap / ax.MANIFEST_NAME).stat().st_mode)
    assert not (mode & stat.S_IWUSR), f"manifest should be read-only, got {oct(mode)}"


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------
def test_dry_run_writes_nothing(artifacts_root, tmp_path):
    out = tmp_path / "out"
    plan = _build(artifacts_root, out, dry_run=True)
    assert plan["dry_run"] is True
    assert "decision_plan" in plan["would_include"]
    assert not (out / ax.EXPORT_SUBDIR).exists()


# ---------------------------------------------------------------------------
# Health producer
# ---------------------------------------------------------------------------
def test_health_amber_when_no_snapshot(tmp_path):
    h = ax.build_agent_export_health(output_root=tmp_path)
    assert h["status"] == ax.AMBER
    assert h["latest_snapshot_id"] is None


def test_health_green_after_full_build(tmp_path):
    root = tmp_path / "outputs"
    _write(root, "latest/decision_plan.json", {"observe_only": True})
    _write(root, "latest/system_decision_summary.json", {"s": 1})
    # include ALL optional allowlist sources so there are no gaps -> GREEN
    for e in ax.ALLOWLIST:
        src = root / e.source_relpath
        if not src.exists():
            _write(root, e.source_relpath, {"stub": e.logical_name})
    ax.build_snapshot(production_git_sha="d" * 40, production_run_id="R1",
                      run_started_at=None, run_completed_at=None,
                      artifacts_root=root, output_root=root)
    h = ax.build_agent_export_health(output_root=root)
    assert h["status"] == ax.GREEN, h
    assert h["latest_snapshot_id"] == "snap-R1"


def test_health_red_on_corruption(tmp_path):
    root = tmp_path / "outputs"
    _write(root, "latest/decision_plan.json", {"observe_only": True})
    _write(root, "latest/system_decision_summary.json", {"s": 1})
    m = ax.build_snapshot(production_git_sha="e" * 40, production_run_id="R2",
                          run_started_at=None, run_completed_at=None,
                          artifacts_root=root, output_root=root)
    snap = root / ax.EXPORT_SUBDIR / ax.SNAPSHOTS_SUBDIR / m["snapshot_id"]
    victim = snap / m["artifacts"][0]["snapshot_path"]
    os.chmod(victim, 0o644)
    victim.write_text("tampered", encoding="utf-8")
    h = ax.build_agent_export_health(output_root=root)
    assert h["status"] == ax.RED
