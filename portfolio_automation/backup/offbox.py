"""Bridge the producer/verifier format gap, and hold the off-box contract.

DEFECT A, WHICH THIS CLOSES.

``create_snapshot`` writes a plaintext snapshot directory. The restore verifier
consumes a single encrypted ``*.tar.gz.enc``. Nothing joined those two formats,
so the verifier could never actually be pointed at what the producer made. This
module is that missing step: snapshot directory -> tar -> encrypted blob.

Local-only is the DEFAULT. Producing the encrypted artifact never requires a
network, so tests and offline recovery drills work with no GitHub involvement
and a nightly cron cannot fail because a remote was unreachable. Upload is a
separate, explicit mode.

THE ENCRYPTION SCHEME IS DELIBERATELY UNCHANGED.

AES-256-CBC / PBKDF2 / 200,000 iterations / salt, via ``openssl enc``. It is
not authenticated encryption, and a future hardening mission may want AES-GCM
or age. It is kept as-is here because this exact scheme has already completed
an end-to-end restore proof on real data, and swapping the cipher in the same
change that repairs the format gap would invalidate that evidence.

``experimental_noncanonical``.
"""
from __future__ import annotations

import hashlib
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from portfolio_automation.backup.snapshot import MANIFEST_NAME

CIPHER = "-aes-256-cbc"
PBKDF2_ITER = 200_000
ENC_SUFFIX = ".tar.gz.enc"
ARTIFACT_PREFIX = "stockbot-state-"
DEFAULT_KEY_FILE = "/root/.stockbot_backup_key"
DEFAULT_GH_REPO = "pesantezq/stockbot-backups"
DEFAULT_OFFBOX_RETENTION = 14
RELEASE_TAG_PREFIX = "backup-"
ARTIFACT_MODE = 0o600


class OffboxError(RuntimeError):
    """Fails closed: no partial artifact, no ambiguous upload, no deletion."""


def _openssl_args(encrypt: bool, key_file: Path) -> list[str]:
    return ["openssl", "enc", CIPHER, *(["-e"] if encrypt else ["-d"]),
            "-salt", "-pbkdf2", "-iter", str(PBKDF2_ITER),
            "-pass", f"file:{key_file}"]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def encrypt_snapshot(snapshot_dir: Path, *, key_file: Path,
                     out_dir: Optional[Path] = None,
                     stamp: Optional[str] = None) -> dict[str, Any]:
    """Package exactly one verified snapshot into one encrypted blob."""
    snap = Path(snapshot_dir)
    if not (snap / MANIFEST_NAME).is_file():
        raise OffboxError(
            f"refusing to package an unverified snapshot (no {MANIFEST_NAME}): {snap}")
    key = Path(key_file)
    if not key.is_file():
        raise OffboxError(f"passphrase file absent: {key}")

    stamp = stamp or snap.name
    out = Path(out_dir) if out_dir else snap.parent
    out.mkdir(parents=True, exist_ok=True)
    blob = out / f"{ARTIFACT_PREFIX}{stamp}{ENC_SUFFIX}"
    if blob.exists():
        raise OffboxError(f"encrypted artifact already exists: {blob}")

    # umask 077 for the plaintext tar: it exists only inside a private temp dir
    # and is removed when the context closes.
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = Path(tmp) / f"{stamp}.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            # Archive the snapshot under its own name so extraction produces a
            # single predictable top-level directory.
            tar.add(snap, arcname=snap.name)
        tar_path.chmod(ARTIFACT_MODE)
        with tar_path.open("rb") as fin, blob.open("wb") as fout:
            proc = subprocess.run(_openssl_args(True, key), stdin=fin,
                                  stdout=fout, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            blob.unlink(missing_ok=True)
            raise OffboxError(
                f"openssl encrypt failed (rc={proc.returncode}): "
                f"{proc.stderr.decode(errors='replace')[:200]}")
    blob.chmod(ARTIFACT_MODE)
    return {
        "artifact": str(blob),
        "artifact_name": blob.name,
        "sha256": _sha256_file(blob),
        "size_bytes": blob.stat().st_size,
        "snapshot_id": snap.name,
        "encryption_contract":
            f"openssl enc {CIPHER} -pbkdf2 -iter {PBKDF2_ITER} -salt",
        "uploaded": False,
    }


def decrypt_artifact(blob: Path, dest_tar: Path, *, key_file: Path) -> None:
    key = Path(key_file)
    if not key.is_file():
        raise OffboxError(f"passphrase file absent: {key}")
    with Path(blob).open("rb") as fin, Path(dest_tar).open("wb") as fout:
        proc = subprocess.run(_openssl_args(False, key), stdin=fin,
                              stdout=fout, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise OffboxError(
            "decryption failed — wrong key or corrupt artifact "
            f"(rc={proc.returncode})")


# ── off-box upload: explicit mode only ────────────────────────────────────

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True)


@dataclass(frozen=True)
class OffboxTarget:
    repo: str = DEFAULT_GH_REPO
    retention: int = DEFAULT_OFFBOX_RETENTION


def upload_artifact(blob: Path, *, target: OffboxTarget,
                    runner: Runner = _default_runner) -> dict[str, Any]:
    """Publish the blob as a private GitHub release asset.

    ``runner`` is injected so tests exercise the argv and the retention logic
    without ever contacting GitHub. Nothing here stores a credential; ``gh``
    supplies its own, and if it is unauthorized the command simply fails."""
    blob = Path(blob)
    if not blob.is_file():
        raise OffboxError(f"artifact absent: {blob}")
    tag = f"{RELEASE_TAG_PREFIX}{blob.name[len(ARTIFACT_PREFIX):].split('.')[0]}"
    create = runner(["gh", "release", "create", tag, str(blob),
                     "--repo", target.repo, "--notes",
                     "Automated encrypted StockBot state backup."])
    if create.returncode != 0:
        raise OffboxError(
            f"gh release create failed (rc={create.returncode}): "
            f"{(create.stderr or '')[:200]}")
    return {"uploaded": True, "tag": tag, "repo": target.repo}


def prune_releases(*, target: OffboxTarget,
                   runner: Runner = _default_runner) -> list[str]:
    """Delete the oldest OUR-PREFIX releases beyond the retention count.

    Fails closed on ambiguity. It only ever considers tags starting with
    ``backup-``, so an unrelated release cannot be deleted, and it refuses to
    act at all if the listing cannot be parsed."""
    listing = runner(["gh", "release", "list", "--repo", target.repo,
                      "--limit", "200"])
    if listing.returncode != 0:
        raise OffboxError("gh release list failed; refusing to prune blindly")
    tags: list[str] = []
    for line in (listing.stdout or "").splitlines():
        parts = line.split("\t")
        candidate = next((p for p in parts
                          if p.startswith(RELEASE_TAG_PREFIX)), None)
        if candidate:
            tags.append(candidate.strip())
    if not tags:
        return []
    tags = sorted(set(tags))
    doomed = tags[:-target.retention] if len(tags) > target.retention else []
    deleted = []
    for tag in doomed:
        if not tag.startswith(RELEASE_TAG_PREFIX):
            raise OffboxError(f"refusing to delete non-backup release: {tag}")
        rc = runner(["gh", "release", "delete", tag, "--repo", target.repo,
                     "--yes"])
        if rc.returncode != 0:
            raise OffboxError(f"failed deleting {tag}; stopping")
        deleted.append(tag)
    return deleted
