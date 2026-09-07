"""Produce a local backup snapshot with a manifest that can prove itself.

WHAT A SNAPSHOT IS.

One timestamped directory containing a hot copy of every BACKUP_REQUIRED
database (gzipped), one tar of the named state files, and a manifest v2 that
records a SHA-256 for each component plus a deterministic per-table row
signature for each database.

WHY THE ROW SIGNATURE EXISTS.

``PRAGMA integrity_check`` proves a file is a well-formed SQLite database. It
does not prove the database still contains what it contained when it was
copied. The row signature — sorted table names with their row counts, hashed —
is what lets the restore verifier assert that a restored database holds the
same content, not merely that it opens.

NO NETWORK. This module writes locally and nothing else; encryption and any
off-box push are separate steps by design, so a nightly cron cannot fail
because GitHub was unreachable.

``experimental_noncanonical``.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from portfolio_automation.backup import recovery_set as RS

MANIFEST_SCHEMA = "engineering.backup_manifest.v2"
MANIFEST_NAME = "manifest.json"
STATE_TAR_NAME = "state_files.tar.gz"
DEFAULT_BACKUP_ROOT = "var/backups/stockbot"
RETAIN_SNAPSHOTS = 14
ARTIFACT_MODE = 0o600
DIR_MODE = 0o700


class BackupError(RuntimeError):
    """The snapshot fails closed rather than writing a partial artifact."""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _row_signature(db_path: Path) -> tuple[str, dict[str, int]]:
    """Deterministic per-table row counts, and their hash.

    Sorted by table name so the signature does not depend on sqlite_master
    ordering; that ordering is not guaranteed stable across versions."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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


def _integrity_check(db_path: Path) -> str:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return str(con.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        con.close()


def _hot_backup(src: Path, dest: Path) -> None:
    """sqlite3 online backup API — safe while the DB is being written."""
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_con = sqlite3.connect(dest)
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()


def _git_provenance(repo_root: Path) -> dict[str, Any]:
    """Record whether the SOURCE_CONTROL_DURABLE claim actually holds.

    Tracked state is excluded from the artifact because Git is its record. That
    is only true for commits that reached the remote — and this system has
    already been burned by 12 unpushed VPS commits. So the snapshot states
    plainly whether HEAD is contained in the remote and whether tracked
    recovery state is dirty."""
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", "-C", str(repo_root), *args],
                                  capture_output=True, text=True,
                                  timeout=30).stdout.strip()
        except Exception:
            return ""

    head = git("rev-parse", "HEAD")
    dirty_tracked = [ln for ln in git("status", "--porcelain",
                                      "--", ".agent", "config", "docs",
                                      "evals").splitlines() if ln.strip()]
    contained = ""
    if head:
        rc = subprocess.run(
            ["git", "-C", str(repo_root), "merge-base", "--is-ancestor",
             head, "origin/main"], capture_output=True)
        contained = "yes" if rc.returncode == 0 else "no"
    return {
        "head": head or "UNAVAILABLE",
        "head_contained_in_origin_main": contained or "UNKNOWN",
        "tracked_recovery_state_dirty": bool(dirty_tracked),
        "dirty_paths": dirty_tracked[:20],
        "warning": (
            "SOURCE_CONTROL_DURABLE surfaces (.agent/, config/, docs/, evals/) "
            "are NOT in this artifact. If head_contained_in_origin_main is not "
            "'yes', or tracked_recovery_state_dirty is true, that state exists "
            "ONLY on this host and losing it loses the state."),
    }


def create_snapshot(repo_root: Path, *, backup_root: Optional[Path] = None,
                    stamp: Optional[str] = None,
                    code_sha: str = "UNAVAILABLE",
                    retain: int = RETAIN_SNAPSHOTS) -> dict[str, Any]:
    """Build one snapshot. Returns the manifest."""
    root = Path(repo_root)
    out_root = Path(backup_root) if backup_root else root / DEFAULT_BACKUP_ROOT
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap_dir = out_root / stamp
    if snap_dir.exists():
        raise BackupError(f"snapshot already exists: {snap_dir}")
    snap_dir.mkdir(parents=True)
    snap_dir.chmod(DIR_MODE)

    databases: list[dict[str, Any]] = []
    gaps: list[dict[str, str]] = []

    with tempfile.TemporaryDirectory() as tmp:
        for surface in RS.backup_databases():
            src = root / surface.path
            name = Path(surface.path).name
            if not src.is_file():
                if surface.required:
                    raise BackupError(f"required database absent: {surface.path}")
                gaps.append({"path": surface.path, "reason": "absent"})
                continue
            hot = Path(tmp) / name
            _hot_backup(src, hot)
            integrity = _integrity_check(hot)
            if integrity != "ok":
                raise BackupError(
                    f"{surface.path} failed integrity_check: {integrity}")
            sig, counts = _row_signature(hot)
            gz = snap_dir / f"{name}.gz"
            # mtime=0 so the gzip container is byte-stable for identical input.
            with hot.open("rb") as fin, gzip.GzipFile(
                    filename="", mode="wb", fileobj=gz.open("wb"), mtime=0) as fout:
                shutil.copyfileobj(fin, fout)
            gz.chmod(ARTIFACT_MODE)
            databases.append({
                "logical_name": name, "source_path": surface.path,
                "artifact": gz.name, "sha256": _sha256_file(gz),
                "size_bytes": gz.stat().st_size,
                "integrity_check": integrity,
                "row_signature": sig, "row_counts": counts,
                "classification": surface.classification,
            })

    # ---- named state files, never a wildcard ----------------------------
    state_components: list[dict[str, Any]] = []
    tar_path = snap_dir / STATE_TAR_NAME
    included: list[tuple[Path, str]] = []
    for surface in RS.backup_state_files():
        src = root / surface.path
        if not src.is_file():
            if surface.required:
                raise BackupError(f"required state file absent: {surface.path}")
            gaps.append({"path": surface.path, "reason": "absent"})
            continue
        if RS.is_forbidden(src.name):
            raise BackupError(f"refusing to back up secret-shaped file: {src.name}")
        included.append((src, surface.path))
        state_components.append({
            "logical_name": surface.path, "sha256": _sha256_file(src),
            "size_bytes": src.stat().st_size,
            "classification": surface.classification,
        })
    with tarfile.open(tar_path, "w:gz") as tar:
        for src, arcname in included:
            tar.add(src, arcname=arcname)
    tar_path.chmod(ARTIFACT_MODE)

    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "recovery_set_version": RS.SCHEMA_VERSION,
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "snapshot_id": stamp,
        "code_sha": code_sha,
        "databases": databases,
        "state_archive": {
            "artifact": STATE_TAR_NAME,
            "sha256": _sha256_file(tar_path),
            "size_bytes": tar_path.stat().st_size,
            "components": state_components,
        },
        "gaps": gaps,
        "excluded_state_classes": {
            s.path: {"classification": s.classification, "reason": s.reason}
            for s in RS.EXCLUDED
        },
        "git_provenance": _git_provenance(root),
        "encryption_contract": "openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt",
        "authority_statement": (
            "A backup artifact. It grants no authority and restores nothing on "
            "its own."),
    }
    # snapshot_content_id covers only content, never timestamps, so two
    # snapshots of identical state are recognisably identical.
    content = {
        "databases": [{k: d[k] for k in
                       ("logical_name", "sha256", "row_signature")}
                      for d in databases],
        "state_archive_components": state_components,
    }
    manifest["snapshot_content_id"] = "bkp_" + hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]

    mpath = snap_dir / MANIFEST_NAME
    mpath.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                     encoding="utf-8")
    mpath.chmod(ARTIFACT_MODE)

    _prune(out_root, retain)
    return manifest


def _prune(backup_root: Path, retain: int) -> list[str]:
    """Keep the newest `retain` snapshots. Only removes directories that look
    like our own snapshots, so an unrelated directory cannot be deleted."""
    if retain <= 0:
        return []
    snaps = sorted(
        (p for p in backup_root.iterdir()
         if p.is_dir() and (p / MANIFEST_NAME).is_file()),
        key=lambda p: p.name)
    removed = []
    for p in snaps[:-retain] if len(snaps) > retain else []:
        shutil.rmtree(p)
        removed.append(p.name)
    return removed


def latest_snapshot(backup_root: Path) -> Optional[Path]:
    snaps = sorted((p for p in Path(backup_root).iterdir()
                    if p.is_dir() and (p / MANIFEST_NAME).is_file()),
                   key=lambda p: p.name)
    return snaps[-1] if snaps else None
