"""Backup durability: producer, manifest, encryption, restore proof.

Every test builds its own fixture repo in tmp_path with a throwaway key. No
production path is read, the real passphrase at /root/.stockbot_backup_key is
never touched, and no test contacts GitHub — the off-box runner is injected.
"""
from __future__ import annotations

import gzip
import io
import json
import sqlite3
import subprocess
import tarfile
from pathlib import Path

import pytest

from portfolio_automation.backup import offbox as OB
from portfolio_automation.backup import recovery_set as RS
from portfolio_automation.backup import snapshot as SNAP
from portfolio_automation.backup import verify as VER

TEST_KEY = "test-only-passphrase-not-production\n"


def _db(path: Path, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)",
                    [(f"row-{i}",) for i in range(rows)])
    con.commit()
    con.close()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A fixture repo carrying every BACKUP_REQUIRED surface."""
    for s in RS.backup_databases():
        _db(tmp_path / s.path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "finance_history.json").write_text(
        json.dumps({"2026-09-06": {"value": 1234.5}}), encoding="utf-8")
    # Secrets and caches that must never be swept in.
    (tmp_path / ".env").write_text("FMP_API_KEY=must-never-appear\n", encoding="utf-8")
    (tmp_path / "data" / "fmp_cache").mkdir(exist_ok=True)
    (tmp_path / "data" / "fmp_cache" / "x.json").write_text("{}", encoding="utf-8")
    return tmp_path


@pytest.fixture
def key(tmp_path: Path) -> Path:
    p = tmp_path / "testkey"
    p.write_text(TEST_KEY, encoding="utf-8")
    p.chmod(0o600)
    return p


@pytest.fixture
def snap(repo: Path) -> Path:
    SNAP.create_snapshot(repo, backup_root=repo / "bk", stamp="20260907T000000Z",
                         code_sha="testsha")
    return repo / "bk" / "20260907T000000Z"


def _openssl_available() -> bool:
    try:
        return subprocess.run(["openssl", "version"],
                              capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


needs_openssl = pytest.mark.skipif(
    not _openssl_available(), reason="openssl not available")


# ── recovery-set classification ───────────────────────────────────────────


def test_every_surface_is_classified_with_a_reason():
    for s in RS.ALL_SURFACES:
        assert s.classification in (
            "BACKUP_REQUIRED", "REGENERABLE", "SOURCE_CONTROL_DURABLE",
            "SECRET_EXCLUDED", "EPHEMERAL_EXCLUDED"), s.path
        assert len(s.reason) > 30, f"{s.path} needs a real reason"


def test_the_db_allowlist_is_explicit_and_covers_rd_control():
    """rd_control.db is the concrete August gap: its own docs call it 'the
    single authoritative store' and the old four-DB list omitted it."""
    paths = {s.path for s in RS.backup_databases()}
    assert "data/rd_control.db" in paths
    assert "data/portfolio.db" in paths
    # The phantom DB must stay out.
    assert "data/stockbot.db" not in paths
    assert RS.classification_of("data/stockbot.db") == "EPHEMERAL_EXCLUDED"


def test_protected_state_is_source_control_durable_not_duplicated():
    assert RS.classification_of(".agent/") == "SOURCE_CONTROL_DURABLE"
    assert RS.classification_of("config/") == "SOURCE_CONTROL_DURABLE"


def test_secrets_are_excluded_by_name_not_by_filter():
    assert RS.classification_of(".env") == "SECRET_EXCLUDED"
    for name in (".env", "id_rsa", "token.json", "server.pem",
                 ".stockbot_backup_key"):
        assert RS.is_forbidden(name), name
    assert not RS.is_forbidden("finance_history.json")


# ── local snapshot producer ───────────────────────────────────────────────


def test_snapshot_captures_every_required_surface(snap: Path):
    m = json.loads((snap / SNAP.MANIFEST_NAME).read_text())
    assert m["schema_version"] == SNAP.MANIFEST_SCHEMA
    got = {d["source_path"] for d in m["databases"]}
    assert got == {s.path for s in RS.backup_databases()}
    comps = {c["logical_name"] for c in m["state_archive"]["components"]}
    assert "data/finance_history.json" in comps
    for d in m["databases"]:
        assert d["integrity_check"] == "ok"
        assert len(d["sha256"]) == 64
        assert d["row_counts"]["t"] == 3


def test_snapshot_excludes_secrets_and_caches(snap: Path):
    names = {p.name for p in snap.iterdir()}
    assert ".env" not in names
    with tarfile.open(snap / SNAP.STATE_TAR_NAME, "r:gz") as tar:
        members = tar.getnames()
    assert not any(".env" in n for n in members)
    assert not any("fmp_cache" in n for n in members)


def test_an_unapproved_new_json_file_is_not_silently_backed_up(repo: Path):
    """The wildcard defect: a new data/*.json must NOT enter the backup until
    someone adds it to the allowlist deliberately."""
    (repo / "data" / "surprise_new_state.json").write_text("{}", encoding="utf-8")
    SNAP.create_snapshot(repo, backup_root=repo / "bk2", stamp="s", code_sha="t")
    with tarfile.open(repo / "bk2" / "s" / SNAP.STATE_TAR_NAME, "r:gz") as tar:
        assert not any("surprise" in n for n in tar.getnames())


def test_missing_required_database_fails_closed(repo: Path):
    (repo / "data" / "portfolio.db").unlink()
    with pytest.raises(SNAP.BackupError, match="required database absent"):
        SNAP.create_snapshot(repo, backup_root=repo / "bk3", stamp="s")


def test_artifacts_are_private(snap: Path):
    for p in snap.iterdir():
        assert oct(p.stat().st_mode)[-3:] == "600", p.name


def test_retention_keeps_the_newest_only(repo: Path):
    for i in range(5):
        SNAP.create_snapshot(repo, backup_root=repo / "bk4",
                             stamp=f"2026090{i}T000000Z", retain=3)
    kept = sorted(p.name for p in (repo / "bk4").iterdir() if p.is_dir())
    assert len(kept) == 3 and kept[-1] == "20260904T000000Z"


def test_git_provenance_records_the_durability_assumption(snap: Path):
    """SOURCE_CONTROL_DURABLE is only true for PUSHED commits — this system has
    already been burned by 12 unpushed VPS commits."""
    m = json.loads((snap / SNAP.MANIFEST_NAME).read_text())
    gp = m["git_provenance"]
    assert set(gp) >= {"head", "head_contained_in_origin_main",
                       "tracked_recovery_state_dirty", "warning"}
    assert "ONLY on this host" in gp["warning"]


def test_content_id_ignores_timestamps(repo: Path):
    a = SNAP.create_snapshot(repo, backup_root=repo / "b1", stamp="s1")
    b = SNAP.create_snapshot(repo, backup_root=repo / "b2", stamp="s2")
    assert a["snapshot_content_id"] == b["snapshot_content_id"]
    assert a["snapshot_id"] != b["snapshot_id"]


# ── restore proof over a plaintext snapshot ───────────────────────────────


def test_valid_snapshot_proves_restorable(snap: Path):
    proof = VER.verify_snapshot_dir(snap)
    assert proof.status == VER.PROOF_OK, proof.errors
    joined = " ".join(proof.checks)
    assert "digest matches the frozen manifest" in joined
    assert "reproduces its row signature" in joined


def test_tampered_database_gzip_is_rejected(snap: Path):
    art = next(p for p in snap.iterdir() if p.name.endswith(".db.gz"))
    art.write_bytes(art.read_bytes() + b"junk")
    proof = VER.verify_snapshot_dir(snap)
    assert proof.status == VER.PROOF_FAILED
    assert any("sha256 mismatch" in e for e in proof.errors)


def test_tampered_state_archive_is_rejected(snap: Path):
    p = snap / SNAP.STATE_TAR_NAME
    p.write_bytes(p.read_bytes() + b"junk")
    proof = VER.verify_snapshot_dir(snap)
    assert proof.status == VER.PROOF_FAILED
    assert any("sha256 mismatch" in e for e in proof.errors)


def test_tampered_manifest_digest_is_rejected(snap: Path):
    m = json.loads((snap / SNAP.MANIFEST_NAME).read_text())
    m["databases"][0]["sha256"] = "0" * 64
    (snap / SNAP.MANIFEST_NAME).write_text(json.dumps(m), encoding="utf-8")
    proof = VER.verify_snapshot_dir(snap)
    assert proof.status == VER.PROOF_FAILED


def test_content_swap_is_caught_by_the_row_signature(snap: Path):
    """The case a plain integrity_check misses: a VALID database whose content
    changed. Re-hashed in the manifest so only the row signature can catch it."""
    art = next(p for p in snap.iterdir() if p.name.startswith("portfolio"))
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        swap = Path(td) / "portfolio.db"
        _db(swap, rows=99)                       # valid DB, wrong content
        raw = swap.read_bytes()
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(raw)
    art.write_bytes(buf.getvalue())
    m = json.loads((snap / SNAP.MANIFEST_NAME).read_text())
    import hashlib
    for d in m["databases"]:
        if d["artifact"] == art.name:
            d["sha256"] = hashlib.sha256(art.read_bytes()).hexdigest()
    (snap / SNAP.MANIFEST_NAME).write_text(json.dumps(m), encoding="utf-8")

    proof = VER.verify_snapshot_dir(snap)
    assert proof.status == VER.PROOF_FAILED
    assert any("row signature mismatch" in e for e in proof.errors)


def test_missing_artifact_is_rejected(snap: Path):
    next(p for p in snap.iterdir() if p.name.endswith(".db.gz")).unlink()
    proof = VER.verify_snapshot_dir(snap)
    assert any("missing artifact" in e for e in proof.errors)


def test_unexpected_artifact_is_rejected(snap: Path):
    (snap / "stowaway.bin").write_bytes(b"x")
    proof = VER.verify_snapshot_dir(snap)
    assert any("unexpected artifact" in e for e in proof.errors)


# ── archive extraction safety ─────────────────────────────────────────────


def _tar_with(tmp: Path, name: str, *, link: str | None = None) -> Path:
    """Build a hostile archive.

    Members are added via TarInfo rather than tar.add(arcname=...) because
    tarfile SILENTLY STRIPS a leading slash on add(), so an absolute-path
    member cannot be produced that way -- an earlier version of this fixture
    tested nothing for the /etc/passwd case."""
    p = tmp / "evil.tar.gz"
    body = b"x"
    with tarfile.open(p, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        if link:
            info.type = tarfile.SYMTYPE
            info.linkname = link
            tar.addfile(info)
        else:
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return p


@pytest.mark.parametrize("member", ["../escape.json", "/etc/passwd"])
def test_traversal_and_absolute_paths_are_rejected(tmp_path: Path, member: str):
    p = _tar_with(tmp_path, member)
    with tarfile.open(p, "r:gz") as tar:
        with pytest.raises(VER.UnsafeArchive):
            VER.assert_safe_members(tar)


def test_symlink_escape_is_rejected(tmp_path: Path):
    p = _tar_with(tmp_path, "link", link="/etc/passwd")
    with tarfile.open(p, "r:gz") as tar:
        with pytest.raises(VER.UnsafeArchive, match="link member refused"):
            VER.assert_safe_members(tar)


def test_secret_shaped_member_is_rejected(tmp_path: Path):
    p = _tar_with(tmp_path, "sub/.env")
    with tarfile.open(p, "r:gz") as tar:
        with pytest.raises(VER.UnsafeArchive, match="secret-shaped"):
            VER.assert_safe_members(tar)


# ── encryption round trip ─────────────────────────────────────────────────


@needs_openssl
def test_encrypt_decrypt_restore_round_trip(snap: Path, key: Path, tmp_path: Path):
    res = OB.encrypt_snapshot(snap, key_file=key, out_dir=tmp_path / "enc")
    blob = Path(res["artifact"])
    assert blob.name.startswith(OB.ARTIFACT_PREFIX)
    assert blob.name.endswith(OB.ENC_SUFFIX)
    assert res["uploaded"] is False
    assert len(res["sha256"]) == 64
    assert oct(blob.stat().st_mode)[-3:] == "600"

    proof = VER.verify_encrypted_artifact(blob, key_file=key)
    assert proof.status == VER.PROOF_OK, proof.errors
    assert "encrypted artifact decrypts" in proof.checks


@needs_openssl
def test_tampered_encrypted_blob_fails(snap: Path, key: Path, tmp_path: Path):
    res = OB.encrypt_snapshot(snap, key_file=key, out_dir=tmp_path / "enc")
    blob = Path(res["artifact"])
    data = bytearray(blob.read_bytes())
    data[len(data) // 2] ^= 0xFF
    blob.write_bytes(bytes(data))
    proof = VER.verify_encrypted_artifact(blob, key_file=key)
    assert proof.status == VER.PROOF_FAILED


@needs_openssl
def test_wrong_key_fails_closed(snap: Path, key: Path, tmp_path: Path):
    res = OB.encrypt_snapshot(snap, key_file=key, out_dir=tmp_path / "enc")
    other = tmp_path / "otherkey"
    other.write_text("a-different-passphrase\n", encoding="utf-8")
    proof = VER.verify_encrypted_artifact(Path(res["artifact"]), key_file=other)
    assert proof.status == VER.PROOF_FAILED


def test_encrypting_an_unverified_directory_is_refused(tmp_path: Path, key: Path):
    bare = tmp_path / "not-a-snapshot"
    bare.mkdir()
    with pytest.raises(OB.OffboxError, match="unverified snapshot"):
        OB.encrypt_snapshot(bare, key_file=key)


def test_missing_key_file_is_refused(snap: Path, tmp_path: Path):
    with pytest.raises(OB.OffboxError, match="passphrase file absent"):
        OB.encrypt_snapshot(snap, key_file=tmp_path / "nope")


# ── off-box mode: mocked, never real ──────────────────────────────────────


def test_no_upload_happens_unless_explicitly_requested(snap: Path, key: Path,
                                                       tmp_path: Path,
                                                       monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(OB, "_default_runner",
                        lambda cmd: calls.append(list(cmd)))
    if not _openssl_available():
        pytest.skip("openssl not available")
    OB.encrypt_snapshot(snap, key_file=key, out_dir=tmp_path / "enc")
    assert calls == [], "encryption must never invoke gh"


def test_upload_argv_is_a_private_release(tmp_path: Path):
    seen: list[list[str]] = []

    def runner(cmd):
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    blob = tmp_path / f"{OB.ARTIFACT_PREFIX}20260907T000000Z{OB.ENC_SUFFIX}"
    blob.write_bytes(b"x")
    res = OB.upload_artifact(blob, target=OB.OffboxTarget(repo="acme/backups"),
                             runner=runner)
    assert res["uploaded"] is True
    assert res["tag"].startswith(OB.RELEASE_TAG_PREFIX)
    cmd = seen[0]
    assert cmd[:3] == ["gh", "release", "create"]
    assert "--repo" in cmd and "acme/backups" in cmd
    # No credential may be passed on the command line.
    assert not any("token" in c.lower() or "key" in c.lower() for c in cmd)


def test_retention_never_deletes_unrelated_releases(tmp_path: Path):
    listing = "\n".join(
        [f"{OB.RELEASE_TAG_PREFIX}2026090{i}T000000Z\tBackup\t{i}" for i in range(5)]
        + ["v1.2.3\tApp release\t9"])
    deleted: list[str] = []

    def runner(cmd):
        if cmd[:3] == ["gh", "release", "list"]:
            return subprocess.CompletedProcess(cmd, 0, listing, "")
        if cmd[:3] == ["gh", "release", "delete"]:
            deleted.append(cmd[3])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    OB.prune_releases(target=OB.OffboxTarget(repo="acme/backups", retention=3),
                      runner=runner)
    assert deleted == [f"{OB.RELEASE_TAG_PREFIX}20260900T000000Z",
                       f"{OB.RELEASE_TAG_PREFIX}20260901T000000Z"]
    assert all(t.startswith(OB.RELEASE_TAG_PREFIX) for t in deleted)
    assert "v1.2.3" not in deleted


def test_prune_fails_closed_when_listing_fails(tmp_path: Path):
    def runner(cmd):
        return subprocess.CompletedProcess(cmd, 1, "", "boom")
    with pytest.raises(OB.OffboxError, match="refusing to prune blindly"):
        OB.prune_releases(target=OB.OffboxTarget(), runner=runner)


# ── documented contract + wrappers ────────────────────────────────────────


def test_encryption_contract_is_unchanged_from_the_proven_scheme():
    assert OB.CIPHER == "-aes-256-cbc"
    assert OB.PBKDF2_ITER == 200_000
    args = OB._openssl_args(True, Path("/tmp/k"))
    assert "-pbkdf2" in args and "-salt" in args and "200000" in args


def test_wrappers_exist_and_are_executable():
    repo = Path(__file__).resolve().parent.parent
    for rel in ("scripts/backup_state.sh", "scripts/backup_offbox_push.sh",
                "scripts/backup_restore_verify.sh"):
        p = repo / rel
        assert p.is_file(), rel
        assert p.stat().st_mode & 0o111, f"{rel} not executable"
        body = p.read_text(encoding="utf-8")
        assert "set -euo pipefail" in body
        assert "umask 077" in body


def test_recovery_documentation_states_the_key_risk():
    repo = Path(__file__).resolve().parent.parent
    doc = (repo / "docs" / "BACKUP_RECOVERY.md").read_text(encoding="utf-8")
    assert "/root/.stockbot_backup_key" in doc
    # Normalise first: the document wraps lines and uses markdown emphasis, so
    # a raw substring match on a multi-word claim is brittle.
    flat = " ".join(doc.replace("*", "").split())
    assert "unrecoverable" in flat
    assert "claim an off-box copy exists" in flat
