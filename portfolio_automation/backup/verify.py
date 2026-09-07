"""Offline restore proof. RESTORE_PROOF_OK means the backup is actually usable.

WHAT "OK" USED TO MEAN, AND WHY THAT WAS NOT ENOUGH.

The manifest already recorded a SHA-256 per component, but the verifier did not
enforce them — so a corrupted gzip that still happened to decompress, or a
substituted database, could pass. "Archive readable" and "archive integrity
matches the frozen manifest" are different claims, and only the second is worth
anything after a disaster.

So every artifact is hashed and compared to the manifest BEFORE it is
decompressed or opened, and the proof additionally requires that each restored
database opens, passes ``PRAGMA integrity_check``, and reproduces the row
signature recorded at backup time.

EXTRACTION IS TREATED AS UNTRUSTED.

A backup blob is exactly the sort of thing an attacker would tamper with, and
``tar -x`` on an unvalidated archive will happily write through ``../`` or an
absolute path. Every member is screened before extraction; a single bad member
fails the whole proof rather than being skipped.

RESTORES ONLY INTO A SCRATCH TREE. Nothing here writes to the live repository.

``experimental_noncanonical``.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from portfolio_automation.backup import offbox as OB
from portfolio_automation.backup import recovery_set as RS
from portfolio_automation.backup.snapshot import (
    MANIFEST_NAME, MANIFEST_SCHEMA, STATE_TAR_NAME)

PROOF_OK = "RESTORE_PROOF_OK"
PROOF_FAILED = "RESTORE_PROOF_FAILED"


class UnsafeArchive(ValueError):
    """An archive member that would escape the extraction root."""


@dataclass
class Proof:
    status: str
    checks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "checks": self.checks,
                "errors": self.errors, "detail": self.detail}


def assert_safe_members(tar: tarfile.TarFile) -> None:
    """Screen every member before anything is written to disk."""
    for m in tar.getmembers():
        name = m.name
        if name.startswith("/") or name.startswith("\\"):
            raise UnsafeArchive(f"absolute path in archive: {name}")
        parts = Path(name).parts
        if ".." in parts:
            raise UnsafeArchive(f"path traversal in archive: {name}")
        if m.issym() or m.islnk():
            # A link may only point inside the archive; the simplest safe rule
            # is to refuse links outright, since no backup component needs one.
            raise UnsafeArchive(f"link member refused: {name} -> {m.linkname}")
        if m.isdev() or m.isfifo():
            raise UnsafeArchive(f"special file refused: {name}")
        if RS.is_forbidden(Path(name).name):
            raise UnsafeArchive(f"secret-shaped member refused: {name}")


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    assert_safe_members(tar)
    tar.extractall(dest)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _row_signature_of(db: Path) -> tuple[str, dict[str, int]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                  for t in tables}
    finally:
        con.close()
    payload = json.dumps(counts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest(), counts


def verify_snapshot_dir(snapshot_dir: Path) -> Proof:
    """Prove a plaintext snapshot directory restores. Scratch tree only."""
    snap = Path(snapshot_dir)
    proof = Proof(status=PROOF_FAILED)
    mpath = snap / MANIFEST_NAME
    if not mpath.is_file():
        proof.errors.append(f"manifest absent: {mpath}")
        return proof
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        # Reached by a tampered unauthenticated ciphertext: it decrypts to
        # garbage, unpacks, and then the manifest is unreadable. Fail closed.
        proof.errors.append(f"manifest unreadable or not JSON: {type(exc).__name__}")
        return proof

    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        proof.errors.append(
            f"unrecognized manifest schema: {manifest.get('schema_version')!r}")
        return proof
    proof.checks.append("manifest schema recognized")

    if not str(manifest.get("snapshot_id") or "").strip():
        proof.errors.append("snapshot_id missing")
    if not str(manifest.get("snapshot_content_id") or "").startswith("bkp_"):
        proof.errors.append("snapshot_content_id missing or malformed")
    if not proof.errors:
        proof.checks.append("snapshot identity valid")

    if not isinstance(manifest.get("databases"), list) or \
            not isinstance(manifest.get("state_archive"), dict):
        proof.errors.append("manifest missing databases[] or state_archive{}")
        return proof

    expected = {MANIFEST_NAME, STATE_TAR_NAME}
    expected |= {d["artifact"] for d in manifest.get("databases", [])}
    present = {p.name for p in snap.iterdir() if p.is_file()}
    missing = sorted(expected - present)
    unexpected = sorted(present - expected)
    if missing:
        proof.errors.append(f"missing artifact(s): {missing}")
    if unexpected:
        proof.errors.append(f"unexpected artifact(s): {unexpected}")
    for name in present:
        if RS.is_forbidden(name):
            proof.errors.append(f"secret-shaped artifact in snapshot: {name}")
    if not missing and not unexpected:
        proof.checks.append("artifact set exactly matches the manifest")

    if proof.errors:
        return proof

    # ---- hashes BEFORE any decompression --------------------------------
    for d in manifest["databases"]:
        art = snap / d["artifact"]
        actual = _sha256_file(art)
        if actual != d["sha256"]:
            proof.errors.append(
                f"{d['artifact']} sha256 mismatch: manifest {d['sha256'][:16]} "
                f"!= actual {actual[:16]}")
    sa = manifest["state_archive"]
    tar_actual = _sha256_file(snap / sa["artifact"])
    if tar_actual != sa["sha256"]:
        proof.errors.append(
            f"{sa['artifact']} sha256 mismatch: manifest {sa['sha256'][:16]} "
            f"!= actual {tar_actual[:16]}")
    if proof.errors:
        return proof
    proof.checks.append("every artifact digest matches the frozen manifest")

    # ---- restore into scratch and prove content -------------------------
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        for d in manifest["databases"]:
            restored = scratch / d["logical_name"]
            try:
                with gzip.open(snap / d["artifact"], "rb") as fin, \
                        restored.open("wb") as fout:
                    fout.write(fin.read())
            except (OSError, EOFError) as exc:
                proof.errors.append(
                    f"{d['artifact']} does not decompress: {type(exc).__name__}")
                continue
            integrity = "unreadable"
            try:
                con = sqlite3.connect(f"file:{restored}?mode=ro", uri=True)
                integrity = str(con.execute(
                    "PRAGMA integrity_check").fetchone()[0])
                con.close()
            except Exception as exc:
                proof.errors.append(
                    f"{d['logical_name']} does not open: {type(exc).__name__}")
                continue
            if integrity != "ok":
                proof.errors.append(
                    f"{d['logical_name']} integrity_check: {integrity}")
                continue
            sig, counts = _row_signature_of(restored)
            if sig != d["row_signature"]:
                proof.errors.append(
                    f"{d['logical_name']} row signature mismatch — the database "
                    f"opens but its content differs from backup time")
                continue
        if not proof.errors:
            proof.checks.append(
                "every database restores, passes integrity_check, and "
                "reproduces its row signature")

        # state archive: safe extraction + per-component digests
        try:
            with tarfile.open(snap / sa["artifact"], "r:gz") as tar:
                _safe_extract(tar, scratch / "state")
            proof.checks.append("state archive extracts safely")
        except UnsafeArchive as exc:
            proof.errors.append(f"unsafe state archive: {exc}")
        except Exception as exc:
            proof.errors.append(f"state archive unreadable: {exc}")

        if not proof.errors:
            for comp in sa.get("components", []):
                restored = scratch / "state" / comp["logical_name"]
                if not restored.is_file():
                    proof.errors.append(
                        f"state component absent after restore: "
                        f"{comp['logical_name']}")
                    continue
                actual = _sha256_file(restored)
                if actual != comp["sha256"]:
                    proof.errors.append(
                        f"{comp['logical_name']} digest mismatch after restore")
            if not proof.errors:
                proof.checks.append(
                    "every BACKUP_REQUIRED state component present and digest-valid")

    # ---- required coverage ----------------------------------------------
    backed_up = {d["source_path"] for d in manifest["databases"]}
    for surface in RS.backup_databases():
        if surface.required and surface.path not in backed_up:
            proof.errors.append(
                f"required database missing from manifest: {surface.path}")
    comps = {c["logical_name"] for c in sa.get("components", [])}
    for surface in RS.backup_state_files():
        if surface.required and surface.path not in comps:
            proof.errors.append(
                f"required state file missing from manifest: {surface.path}")
    if not proof.errors:
        proof.checks.append("all BACKUP_REQUIRED surfaces are covered")

    proof.detail = {
        "snapshot_id": manifest.get("snapshot_id"),
        "snapshot_content_id": manifest.get("snapshot_content_id"),
        "databases": [d["logical_name"] for d in manifest["databases"]],
        "state_components": sorted(comps),
        "gaps": manifest.get("gaps", []),
        "git_provenance": manifest.get("git_provenance", {}),
    }
    proof.status = PROOF_OK if not proof.errors else PROOF_FAILED
    return proof


def verify_encrypted_artifact(blob: Path, *, key_file: Path) -> Proof:
    """Full chain: decrypt -> safe-unpack -> verify the snapshot inside."""
    proof = Proof(status=PROOF_FAILED)
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        tar_path = scratch / "snapshot.tar.gz"
        try:
            OB.decrypt_artifact(Path(blob), tar_path, key_file=Path(key_file))
        except OB.OffboxError as exc:
            proof.errors.append(str(exc))
            return proof
        proof.checks.append("encrypted artifact decrypts")

        unpack = scratch / "unpacked"
        try:
            with tarfile.open(tar_path, "r:gz") as tar:
                _safe_extract(tar, unpack)
        except UnsafeArchive as exc:
            proof.errors.append(f"unsafe archive: {exc}")
            return proof
        except Exception as exc:
            proof.errors.append(f"archive unreadable: {exc}")
            return proof
        proof.checks.append("archive unpacks safely")

        tops = [p for p in unpack.iterdir()]
        if len(tops) != 1 or not tops[0].is_dir():
            proof.errors.append(
                f"expected exactly one top-level snapshot directory, got "
                f"{[p.name for p in tops]}")
            return proof
        proof.checks.append("snapshot layout as expected")

        inner = verify_snapshot_dir(tops[0])
        proof.checks.extend(inner.checks)
        proof.errors.extend(inner.errors)
        proof.detail = inner.detail
        proof.status = PROOF_OK if not proof.errors else PROOF_FAILED
        return proof
