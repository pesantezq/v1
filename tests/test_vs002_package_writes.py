"""VS-002 evidence PACKAGE-WRITE GOVERNANCE.

Every VS-002 evidence artifact is written through the governed, path-contained,
atomic writer beneath the `VS002_EVIDENCE` namespace root — WITHOUT weakening the
runner's package-directory validate-before-publish transaction, and WITHOUT
changing any evidence payload, digest, or package identity. All fixtures are
synthetic and offline: no network, no credential, no FMP.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from portfolio_automation import data_governance as DG
from portfolio_automation.vs002_evidence import builder as B
from portfolio_automation.vs002_evidence import consumer as CON
from portfolio_automation.vs002_evidence import contracts as C
from portfolio_automation.vs002_evidence import readiness as R
from portfolio_automation.vs002_evidence import production_runner as PR

from tests.test_vs002_evidence import (
    BENCH, SyntheticAdjustedProvider, _db, _scan_dates, fixed_clock)
from tests.test_vs002_production_runner import FakeStrictClient

_ARTIFACTS = ("signals.json", "returns.json", "bars.json", "bars_raw.json",
              "bars_witness_raw.json", "bars_snapshots.json", "manifest.json")


def _pkg(tmp: Path, *, provider=None, scans: int = 12):
    _db(tmp, scans=_scan_dates(scans))
    manifest = B.build(tmp, code_sha="t", generated_at="2026-09-06T00:00:00Z",
                       bar_provider=provider or SyntheticAdjustedProvider(),
                       bar_clock=fixed_clock())
    return tmp / B.DEFAULT_OUT_REL, manifest


# ── the debt is closed: a dedicated governed namespace exists ──────────────

def test_vs002_evidence_namespace_is_registered():
    assert DG.OutputNamespace.VS002_EVIDENCE.value == "vs002_evidence"
    policies = DG.get_policies()
    assert DG.OutputNamespace.VS002_EVIDENCE in policies
    assert policies[DG.OutputNamespace.VS002_EVIDENCE].root.name == "vs002_evidence"
    assert policies[DG.OutputNamespace.VS002_EVIDENCE].user_scoped is False


# ── every artifact write goes through the governed writer ──────────────────

def test_every_artifact_uses_the_governed_write_path(tmp_path, monkeypatch):
    calls = []
    real = DG.safe_write_namespace_path
    def spy(ns, target, content, **kw):
        calls.append((ns, str(target)))
        return real(ns, target, content, **kw)
    monkeypatch.setattr(DG, "safe_write_namespace_path", spy)
    root, _ = _pkg(tmp_path)
    written = {Path(t).name for _ns, t in calls}
    assert written == set(_ARTIFACTS)
    assert all(ns is DG.OutputNamespace.VS002_EVIDENCE for ns, _ in calls)
    for _ns, t in calls:
        assert "/outputs/vs002_evidence/" in t.replace("\\\\", "/")


def test_builder_writer_has_no_direct_file_write(tmp_path):
    """Static guard: the VS-002 writer must not reintroduce a raw
    Path.write_text/open(...write...); all writes go through the governed helper."""
    src = inspect.getsource(B)
    assert ".write_text(" not in src, "direct write_text reintroduced in builder"
    assert "safe_write_namespace_path" in src


# ── path containment: fail closed on any escape ───────────────────────────

def test_traversal_escape_is_refused(tmp_path):
    bad = tmp_path / "outputs" / "vs002_evidence" / ".." / "escape.json"
    with pytest.raises(DG.DataGovernanceError):
        B._write(tmp_path, bad, {"x": 1})
    assert not (tmp_path / "outputs" / "escape.json").exists()


def test_external_absolute_path_is_refused(tmp_path):
    outside = tmp_path / "totally_outside.json"
    with pytest.raises(DG.DataGovernanceError):
        B._write(tmp_path, outside, {"x": 1})
    assert not outside.exists()


def test_symlink_escape_is_refused(tmp_path):
    vs = tmp_path / "outputs" / "vs002_evidence"
    vs.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = vs / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this platform")
    with pytest.raises(DG.DataGovernanceError):
        B._write(tmp_path, link / "x.json", {"x": 1})
    assert not (outside / "x.json").exists()


def test_valid_staging_path_is_accepted(tmp_path):
    target = (tmp_path / "outputs" / "vs002_evidence" / ".staging-t"
              / "signals.json")
    B._write(tmp_path, target, [{"a": 1}])
    assert target.is_file()


def test_valid_package_path_is_accepted(tmp_path):
    target = (tmp_path / "outputs" / "vs002_evidence" / "packages" / "pkgid"
              / "manifest.json")
    B._write(tmp_path, target, {"ok": True})
    assert target.is_file()


# ── serialization preserved byte-for-byte ──────────────────────────────────

def test_deterministic_serialization_is_preserved(tmp_path):
    root, _ = _pkg(tmp_path)
    for name in _ARTIFACTS:
        txt = (root / name).read_text(encoding="utf-8")
        expected = json.dumps(json.loads(txt), indent=2, sort_keys=True) + "\n"
        assert txt == expected, f"{name} serialization changed"


def test_writer_output_matches_prior_serialization_exactly(tmp_path):
    target = (tmp_path / "outputs" / "vs002_evidence" / ".staging-t" / "x.json")
    payload = {"b": 2, "a": [3, 1, 2], "z": "q"}
    B._write(tmp_path, target, payload)
    assert target.read_text(encoding="utf-8") == (
        json.dumps(payload, indent=2, sort_keys=True) + "\n")


# ── per-file atomicity: no partial/temp debris ─────────────────────────────

def test_no_temp_debris_left_after_write(tmp_path):
    root, _ = _pkg(tmp_path)
    leftovers = list(root.rglob(".*.tmp"))
    assert leftovers == [], leftovers


# ── content identity unchanged by the governance repair ────────────────────

def test_same_fixture_yields_same_package_id_and_digests(tmp_path):
    a = _pkg(tmp_path / "a")[1]
    b = _pkg(tmp_path / "b")[1]
    assert a["package_id"] == b["package_id"]
    assert a["artifact_digests"] == b["artifact_digests"]
    assert a["companion_raw_digest"] == b["companion_raw_digest"]
    assert a["schema_version"] == "engineering.vs002_evidence.v1"


# ── the whole contract downstream is unchanged ─────────────────────────────

def test_seven_artifact_v1_contract_and_witness_envelope_unchanged(tmp_path):
    root, m = _pkg(tmp_path)
    names = {p.name for p in root.iterdir() if p.is_file()}
    assert names == set(_ARTIFACTS)
    assert m["schema_version"] == "engineering.vs002_evidence.v1"
    wit = json.loads((root / "bars_witness_raw.json").read_text())
    assert wit["observe_only"] is True and isinstance(wit["rows"], list)


def test_consumer_validation_and_readiness_unchanged(tmp_path):
    root, _ = _pkg(tmp_path)
    snap = CON.validate(root)          # governed writes still reconcile
    assert snap.has_bars
    assert R.evaluate(snap).status == R.READY


# ── failure behavior: an artifact write failure cannot publish ─────────────

def test_write_failure_cannot_publish_a_package(tmp_path, monkeypatch):
    _db(tmp_path, scans=_scan_dates(12))
    real = DG.safe_write_namespace_path
    def boom(ns, target, content, **kw):
        if str(target).endswith("manifest.json"):
            raise OSError("synthetic disk failure")
        return real(ns, target, content, **kw)
    monkeypatch.setattr(DG, "safe_write_namespace_path", boom)
    res = PR.run(tmp_path, client=FakeStrictClient(),
                 credential_present=lambda: True, clock=fixed_clock(),
                 code_sha="x", run_id="wfail")
    assert res.result != PR.PASS
    assert not (tmp_path / "outputs" / "vs002_evidence" / "packages").exists()
    assert not list((tmp_path / "outputs" / "vs002_evidence").glob(".staging-*"))


def test_governed_write_refusal_cannot_publish(tmp_path, monkeypatch):
    """A governance refusal (path containment) during the build also prevents
    any package publication and leaves no staging behind."""
    _db(tmp_path, scans=_scan_dates(12))
    real = DG.safe_write_namespace_path
    def refuse(ns, target, content, **kw):
        if str(target).endswith("bars.json"):
            raise DG.DataGovernanceError("synthetic containment refusal")
        return real(ns, target, content, **kw)
    monkeypatch.setattr(DG, "safe_write_namespace_path", refuse)
    res = PR.run(tmp_path, client=FakeStrictClient(),
                 credential_present=lambda: True, clock=fixed_clock(),
                 code_sha="x", run_id="grefuse")
    assert res.result != PR.PASS
    assert not (tmp_path / "outputs" / "vs002_evidence" / "packages").exists()
