"""``vs002.transport_digest.v1`` -- the committed VS-002 package transport identity.

Every package here is SYNTHETIC and built in a temporary directory. No test
opens, enumerates or hashes the real frozen VS-002 evidence package, which is
held outside the repository; the only real artifact read is the committed
preregistration JSON, and only to assert that the HISTORICAL legacy digest is
preserved verbatim as a recorded value.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import stat
from pathlib import Path

import pytest

from portfolio_automation.vs002_evidence import builder as B
from portfolio_automation.vs002_evidence import consumer as CON
from portfolio_automation.vs002_evidence import transport_digest as TD

REPO_ROOT = Path(__file__).resolve().parent.parent

FLAT_ARTIFACTS = {
    B.MANIFEST_REL: b'{"schema_version": "engineering.vs002_evidence.v1", "package_id": "vs002evd_synthetic"}\n',
    B.SIGNALS_REL: b'[{"ticker": "AAPL", "signal_time": "2026-01-02T09:00:00"}]\n',
    B.RETURNS_REL: b'[{"symbol": "AAPL", "session_date": "2026-01-02", "adj_return": 0.01}]\n',
}
BAR_ARTIFACTS = {
    B.MANIFEST_REL: (b'{"schema_version": "engineering.vs002_evidence.v1", '
                     b'"package_id": "vs002evd_synthetic_bars", '
                     b'"bar_endpoint": "/stable/historical-price-eod/dividend-adjusted"}\n'),
    B.SIGNALS_REL: b'[]\n',
    B.RETURNS_REL: b'[]\n',
    B.BARS_REL: b'[{"symbol": "SPY", "session_date": "2026-01-02", "adj_close": 100.0}]\n',
    B.BARS_RAW_REL: b'{"SPY": [{"date": "2026-01-02", "adjClose": 100.0}]}\n',
    B.BARS_SNAPSHOTS_REL: b'[]\n',
    B.BARS_WITNESS_RAW_REL: b'{"observe_only": true, "rows": []}\n',
}


def _write_package(root: Path, artifacts: dict[str, bytes], *, order=None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    names = list(artifacts) if order is None else list(order)
    assert sorted(names) == sorted(artifacts)
    for name in names:
        (root / name).write_bytes(artifacts[name])
    return root


def _reference(artifacts: dict[str, bytes]) -> str:
    """An independent, hand-rolled implementation of the documented algorithm,
    written from the prose spec and NOT from transport_digest.py."""
    h = hashlib.sha256()
    h.update(b"vs002.transport_digest.v1\x00")
    for name in sorted(artifacts, key=lambda s: s.encode("utf-8")):
        p = name.encode("utf-8")
        data = artifacts[name]
        h.update(len(p).to_bytes(4, "big") + p + len(data).to_bytes(8, "big") + data)
    return h.hexdigest()


# ── the covered set is the consumer's, not a second allowlist ───────────────


def test_expected_set_is_the_consumers_manifest_driven_set():
    flat = json.loads(FLAT_ARTIFACTS[B.MANIFEST_REL])
    bars = json.loads(BAR_ARTIFACTS[B.MANIFEST_REL])
    assert CON.expected_artifacts(flat) == frozenset(FLAT_ARTIFACTS)
    assert CON.expected_artifacts(bars) == frozenset(BAR_ARTIFACTS)
    assert B.MANIFEST_REL in CON.expected_artifacts(flat)


@pytest.mark.parametrize("artifacts", [FLAT_ARTIFACTS, BAR_ARTIFACTS], ids=["flat", "bars"])
def test_digest_covers_exactly_the_expected_artifacts(tmp_path: Path, artifacts):
    d = TD.compute_transport_digest(_write_package(tmp_path / "p", artifacts))
    assert d.algorithm == TD.TRANSPORT_DIGEST_ALGORITHM == "vs002.transport_digest.v1"
    assert [a.path for a in d.artifacts] == sorted(artifacts)
    assert {a.path: a.byte_length for a in d.artifacts} == {
        k: len(v) for k, v in artifacts.items()}
    assert d.total_bytes == sum(len(v) for v in artifacts.values())


# ── determinism and the reference algorithm ────────────────────────────────


@pytest.mark.parametrize("artifacts", [FLAT_ARTIFACTS, BAR_ARTIFACTS], ids=["flat", "bars"])
def test_matches_the_independent_reference_implementation(tmp_path: Path, artifacts):
    d = TD.compute_transport_digest(_write_package(tmp_path / "p", artifacts))
    assert d.sha256 == _reference(artifacts)
    assert TD.digest_of_entries(artifacts).sha256 == _reference(artifacts)


def test_file_creation_order_does_not_matter(tmp_path: Path):
    names = list(BAR_ARTIFACTS)
    digests = set()
    for i in range(4):
        order = list(names)
        random.Random(i).shuffle(order)
        digests.add(TD.compute_transport_digest(
            _write_package(tmp_path / f"p{i}", BAR_ARTIFACTS, order=order)).sha256)
    assert len(digests) == 1


def test_directory_traversal_order_does_not_matter(tmp_path: Path, monkeypatch):
    root = _write_package(tmp_path / "p", BAR_ARTIFACTS)
    baseline = TD.compute_transport_digest(root).sha256
    real_scandir = os.scandir

    class _Reversed:
        def __init__(self, path):
            with real_scandir(path) as it:
                self._entries = list(it)[::-1]

        def __enter__(self):
            return iter(self._entries)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(TD.os, "scandir", _Reversed)
    assert TD.compute_transport_digest(root).sha256 == baseline


def test_mtimes_and_permissions_do_not_affect_the_digest(tmp_path: Path):
    root = _write_package(tmp_path / "p", BAR_ARTIFACTS)
    before = TD.compute_transport_digest(root).sha256
    for i, name in enumerate(BAR_ARTIFACTS):
        p = root / name
        os.utime(p, (1_000_000 + i, 2_000_000 + i))
        os.chmod(p, stat.S_IRUSR | (stat.S_IWUSR if i % 2 else 0) | stat.S_IRGRP)
    os.utime(root, (3_000_000, 3_000_000))
    assert TD.compute_transport_digest(root).sha256 == before


def test_streaming_matches_the_in_memory_reference_for_a_large_artifact(tmp_path: Path):
    big = bytes(random.Random(7).getrandbits(8) for _ in range(3 * TD.CHUNK_BYTES + 17))
    artifacts = dict(FLAT_ARTIFACTS, **{B.SIGNALS_REL: big})
    d = TD.compute_transport_digest(_write_package(tmp_path / "p", artifacts))
    assert d.sha256 == _reference(artifacts) == TD.digest_of_entries(artifacts).sha256
    assert next(a for a in d.artifacts if a.path == B.SIGNALS_REL).byte_length == len(big)


# ── sensitivity: every covered byte matters ────────────────────────────────


def test_one_byte_mutation_in_every_covered_artifact_changes_the_digest(tmp_path: Path):
    base = TD.compute_transport_digest(_write_package(tmp_path / "base", BAR_ARTIFACTS)).sha256
    seen = {base}
    for name, data in BAR_ARTIFACTS.items():
        mutated = dict(BAR_ARTIFACTS)
        # flip one byte in the middle; JSON validity is irrelevant to transport
        # identity except for the manifest, whose bar_endpoint key must survive
        idx = len(data) // 2 if name != B.MANIFEST_REL else data.index(b"synthetic")
        mutated[name] = data[:idx] + bytes([data[idx] ^ 0x01]) + data[idx + 1:]
        d = TD.compute_transport_digest(_write_package(tmp_path / f"m_{name}", mutated)).sha256
        assert d != base, name
        assert d not in seen, name
        seen.add(d)


def test_appending_one_byte_changes_the_digest(tmp_path: Path):
    base = TD.compute_transport_digest(_write_package(tmp_path / "a", FLAT_ARTIFACTS)).sha256
    longer = dict(FLAT_ARTIFACTS, **{B.RETURNS_REL: FLAT_ARTIFACTS[B.RETURNS_REL] + b"\n"})
    assert TD.compute_transport_digest(_write_package(tmp_path / "b", longer)).sha256 != base


# ── framing is unambiguous ────────────────────────────────────────────────


def test_moving_a_byte_across_an_artifact_boundary_changes_the_digest(tmp_path: Path):
    """Without length framing, 'ab'+'c' and 'a'+'bc' would hash identically."""
    a = dict(FLAT_ARTIFACTS, **{B.RETURNS_REL: b"ab", B.SIGNALS_REL: b"c"})
    b = dict(FLAT_ARTIFACTS, **{B.RETURNS_REL: b"a", B.SIGNALS_REL: b"bc"})
    da = TD.compute_transport_digest(_write_package(tmp_path / "a", a)).sha256
    db = TD.compute_transport_digest(_write_package(tmp_path / "b", b)).sha256
    assert da != db


def test_path_bytes_and_content_bytes_cannot_be_confused():
    """Same concatenated bytes, different split between path and content."""
    assert TD.digest_of_entries({"ab": b"c"}).sha256 != TD.digest_of_entries({"a": b"bc"}).sha256


def test_header_framing_is_length_prefixed_big_endian():
    hdr = TD.frame_artifact_header("manifest.json", 5)
    assert hdr == (13).to_bytes(4, "big") + b"manifest.json" + (5).to_bytes(8, "big")
    assert TD.DOMAIN_SEPARATION_PREFIX == b"vs002.transport_digest.v1\x00"


def test_empty_artifact_is_framed_not_omitted(tmp_path: Path):
    a = dict(FLAT_ARTIFACTS, **{B.SIGNALS_REL: b""})
    d = TD.compute_transport_digest(_write_package(tmp_path / "p", a))
    assert next(x for x in d.artifacts if x.path == B.SIGNALS_REL).byte_length == 0
    assert d.sha256 == _reference(a)


# ── fail closed ────────────────────────────────────────────────────────────


def test_missing_artifact_fails_closed(tmp_path: Path):
    for name in BAR_ARTIFACTS:
        partial = {k: v for k, v in BAR_ARTIFACTS.items() if k != name}
        root = _write_package(tmp_path / f"m_{name}", partial)
        with pytest.raises(TD.TransportDigestError, match="missing artifact"):
            TD.compute_transport_digest(root)


def test_extra_artifact_fails_closed(tmp_path: Path):
    root = _write_package(tmp_path / "p", BAR_ARTIFACTS)
    (root / "notes.txt").write_bytes(b"not part of the package")
    with pytest.raises(TD.TransportDigestError, match="unexpected artifact"):
        TD.compute_transport_digest(root)


def test_flat_manifest_refuses_smuggled_bar_artifacts(tmp_path: Path):
    """A manifest that does not declare bars makes the bar files UNEXPECTED,
    exactly as consumer.validate treats them."""
    root = _write_package(tmp_path / "p", FLAT_ARTIFACTS)
    (root / B.BARS_REL).write_bytes(b"[]")
    with pytest.raises(TD.TransportDigestError, match="unexpected artifact"):
        TD.compute_transport_digest(root)
    with pytest.raises(CON.SnapshotInvalid, match="unexpected artifact"):
        CON.validate(root)


def test_renamed_artifact_fails_closed(tmp_path: Path):
    root = _write_package(tmp_path / "p", FLAT_ARTIFACTS)
    (root / B.RETURNS_REL).rename(root / "Returns.json")
    with pytest.raises(TD.TransportDigestError) as exc:
        TD.compute_transport_digest(root)
    assert "missing artifact" in str(exc.value) and "unexpected artifact" in str(exc.value)


def test_directory_where_a_file_is_expected_fails_closed(tmp_path: Path):
    root = _write_package(tmp_path / "p", {k: v for k, v in FLAT_ARTIFACTS.items()
                                           if k != B.SIGNALS_REL})
    (root / B.SIGNALS_REL).mkdir()
    with pytest.raises(TD.TransportDigestError, match="directory"):
        TD.compute_transport_digest(root)


def test_any_subdirectory_fails_closed(tmp_path: Path):
    root = _write_package(tmp_path / "p", FLAT_ARTIFACTS)
    (root / "extra_dir").mkdir()
    with pytest.raises(TD.TransportDigestError, match="directory"):
        TD.compute_transport_digest(root)


def test_symlink_fails_closed(tmp_path: Path):
    root = _write_package(tmp_path / "p", {k: v for k, v in FLAT_ARTIFACTS.items()
                                           if k != B.RETURNS_REL})
    target = tmp_path / "elsewhere.json"
    target.write_bytes(FLAT_ARTIFACTS[B.RETURNS_REL])
    try:
        os.symlink(target, root / B.RETURNS_REL)
    except (OSError, NotImplementedError):  # pragma: no cover - no symlink support
        pytest.skip("symlinks unsupported on this filesystem")
    with pytest.raises(TD.TransportDigestError, match="symlink"):
        TD.compute_transport_digest(root)


def test_symlinked_extra_entry_fails_closed_even_when_dangling(tmp_path: Path):
    root = _write_package(tmp_path / "p", FLAT_ARTIFACTS)
    try:
        os.symlink(tmp_path / "does-not-exist", root / "dangling")
    except (OSError, NotImplementedError):  # pragma: no cover
        pytest.skip("symlinks unsupported on this filesystem")
    with pytest.raises(TD.TransportDigestError, match="symlink"):
        TD.compute_transport_digest(root)


def test_package_root_must_be_a_directory(tmp_path: Path):
    f = tmp_path / "file"
    f.write_bytes(b"x")
    with pytest.raises(TD.TransportDigestError, match="not a directory"):
        TD.compute_transport_digest(f)
    with pytest.raises(TD.TransportDigestError, match="not a directory"):
        TD.compute_transport_digest(tmp_path / "absent")


def test_unparseable_manifest_fails_closed(tmp_path: Path):
    root = _write_package(tmp_path / "p", dict(FLAT_ARTIFACTS, **{B.MANIFEST_REL: b"{not json"}))
    with pytest.raises(TD.TransportDigestError, match="not parseable JSON"):
        TD.compute_transport_digest(root)
    root2 = _write_package(tmp_path / "q", dict(FLAT_ARTIFACTS, **{B.MANIFEST_REL: b"[]"}))
    with pytest.raises(TD.TransportDigestError, match="not a JSON object"):
        TD.compute_transport_digest(root2)


def test_manifest_bytes_are_hashed_raw_not_reserialized(tmp_path: Path):
    """Two manifests with identical JSON meaning but different bytes are
    different transported packages."""
    compact = FLAT_ARTIFACTS[B.MANIFEST_REL]
    pretty = json.dumps(json.loads(compact), indent=4).encode("utf-8")
    assert json.loads(compact) == json.loads(pretty) and compact != pretty
    a = TD.compute_transport_digest(_write_package(tmp_path / "a", FLAT_ARTIFACTS)).sha256
    b = TD.compute_transport_digest(_write_package(
        tmp_path / "b", dict(FLAT_ARTIFACTS, **{B.MANIFEST_REL: pretty}))).sha256
    assert a != b


def test_size_change_during_read_fails_closed(tmp_path: Path, monkeypatch):
    root = _write_package(tmp_path / "p", FLAT_ARTIFACTS)
    real_fstat = os.fstat

    def _lying_fstat(fd):
        st = real_fstat(fd)
        # claim one byte more than the file really holds
        return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid,
                               st.st_gid, st.st_size + 1, st.st_atime, st.st_mtime,
                               st.st_ctime))

    monkeypatch.setattr(TD.os, "fstat", _lying_fstat)
    with pytest.raises(TD.TransportDigestError, match="changed while being read"):
        TD.compute_transport_digest(root)


# ── path normalization ─────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "/manifest.json", "../manifest.json", "a/../b",
                                 "./manifest.json", "a//b", "a\\b", "x\x00y", 7, None])
def test_path_escape_and_malformed_paths_are_refused(bad):
    with pytest.raises(TD.TransportDigestError):
        TD.normalize_relative_path(bad)


def test_nfc_normalization_and_duplicate_detection():
    composed = "café.json"
    decomposed = "café.json"
    assert TD.normalize_relative_path(decomposed) == composed
    with pytest.raises(TD.TransportDigestError, match="duplicate artifact path"):
        TD.digest_of_entries([(composed, b"a"), (decomposed, b"b")])


def test_ordering_is_by_utf8_bytes_regardless_of_insertion_order():
    entries = {"b.json": b"1", "a.json": b"2", "é.json": b"3", "Z.json": b"4"}
    d = TD.digest_of_entries(entries)
    assert [a.path for a in d.artifacts] == sorted(entries, key=lambda s: s.encode("utf-8"))
    assert d.sha256 == TD.digest_of_entries(dict(reversed(list(entries.items())))).sha256


# ── verification helper and the legacy value ───────────────────────────────


def test_verify_accepts_the_v1_digest_and_refuses_a_mismatch(tmp_path: Path):
    root = _write_package(tmp_path / "p", BAR_ARTIFACTS)
    good = TD.compute_transport_digest(root)
    assert TD.verify_transport_digest(root, good.sha256,
                                      algorithm=TD.TRANSPORT_DIGEST_ALGORITHM) == good
    with pytest.raises(TD.TransportDigestMismatch):
        TD.verify_transport_digest(root, "0" * 64, algorithm=TD.TRANSPORT_DIGEST_ALGORITHM)
    with pytest.raises(TD.TransportDigestError, match="64 lowercase hex"):
        TD.verify_transport_digest(root, good.sha256.upper(),
                                   algorithm=TD.TRANSPORT_DIGEST_ALGORITHM)


def test_verify_refuses_unknown_algorithms(tmp_path: Path):
    root = _write_package(tmp_path / "p", FLAT_ARTIFACTS)
    with pytest.raises(TD.TransportDigestError, match="unsupported transport digest algorithm"):
        TD.verify_transport_digest(root, "0" * 64, algorithm="vs002.transport_digest.v2")


def test_legacy_digest_is_preserved_verbatim_and_is_not_a_verification_target(tmp_path: Path):
    prereg = json.loads((REPO_ROOT / "evals" / "vertical_slice"
                         / "VS-002_preregistration.json").read_text(encoding="utf-8"))
    recorded = prereg["binding_core"]["evidence_binding"]["package_transport_digest"]
    assert recorded == TD.LEGACY_TRANSPORT_DIGEST == \
        "4e1f5a6f432e6b1df7d062ab878922afa1439834f151c0bbfa214c289c216136"
    assert TD.LEGACY_TRANSPORT_DIGEST_ALGORITHM == "LEGACY_TRANSPORT_DIGEST_ALGORITHM_UNRECOVERABLE"
    assert TD.LEGACY_TRANSPORT_DIGEST_ALGORITHM != TD.TRANSPORT_DIGEST_ALGORITHM
    root = _write_package(tmp_path / "p", FLAT_ARTIFACTS)
    # The refusal must come from the DEDICATED legacy branch (it explains WHY the
    # value is not recomputable), not merely from the generic unknown-algorithm
    # path -- otherwise the explanation could silently disappear.
    with pytest.raises(TD.TransportDigestError,
                       match="unrecoverable, uncommitted procedure") as exc:
        TD.verify_transport_digest(root, TD.LEGACY_TRANSPORT_DIGEST,
                                   algorithm=TD.LEGACY_TRANSPORT_DIGEST_ALGORITHM)
    assert "historical recorded value" in str(exc.value)
    assert "unsupported transport digest algorithm" not in str(exc.value)


def test_no_claim_that_v1_reproduces_the_legacy_value(tmp_path: Path):
    """The module must not contain an assertion, table or fixture that equates
    a v1 digest with the historical value: the legacy procedure is unrecoverable."""
    src = (REPO_ROOT / "portfolio_automation" / "vs002_evidence"
           / "transport_digest.py").read_text(encoding="utf-8")
    assert src.count(TD.LEGACY_TRANSPORT_DIGEST) == 1  # the one preserved constant
    assert "UNRECOVERABLE" in src


def test_to_dict_is_plain_json(tmp_path: Path):
    d = TD.compute_transport_digest(_write_package(tmp_path / "p", FLAT_ARTIFACTS))
    payload = json.loads(json.dumps(d.to_dict()))
    assert payload["algorithm"] == "vs002.transport_digest.v1"
    assert payload["artifact_count"] == 3
    assert payload["sha256"] == d.sha256
