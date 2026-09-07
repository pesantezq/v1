"""Entry points behind the three scripts/ wrappers.

Kept thin on purpose: the shell wrappers preserve the documented, cron-wired
paths, and all logic lives in testable Python.

``experimental_noncanonical``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from portfolio_automation.backup import offbox as OB
from portfolio_automation.backup import snapshot as SNAP
from portfolio_automation.backup import verify as VER


def _repo_root(arg: str | None) -> Path:
    return Path(arg) if arg else Path.cwd()


def main_backup_state(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Create a local backup snapshot.")
    ap.add_argument("--repo-root")
    ap.add_argument("--backup-root")
    ap.add_argument("--code-sha", default="UNAVAILABLE")
    ap.add_argument("--retain", type=int, default=SNAP.RETAIN_SNAPSHOTS)
    a = ap.parse_args(argv)
    try:
        m = SNAP.create_snapshot(
            _repo_root(a.repo_root),
            backup_root=Path(a.backup_root) if a.backup_root else None,
            code_sha=a.code_sha, retain=a.retain)
    except SNAP.BackupError as exc:
        print(f"BACKUP_FAILED: {exc}", file=sys.stderr)
        return 1
    for d in m["databases"]:
        print(f"  {d['logical_name']}: integrity={d['integrity_check']} "
              f"sha256={d['sha256'][:16]} rows={sum(d['row_counts'].values())}")
    gp = m["git_provenance"]
    print(f"  snapshot={m['snapshot_id']} content_id={m['snapshot_content_id']}")
    print(f"  git head={gp['head'][:12]} pushed={gp['head_contained_in_origin_main']} "
          f"tracked_dirty={gp['tracked_recovery_state_dirty']}")
    if gp["head_contained_in_origin_main"] != "yes" or gp["tracked_recovery_state_dirty"]:
        print("  WARNING: source-controlled recovery state may exist only on "
              "this host — see git_provenance in the manifest", file=sys.stderr)
    print("BACKUP_OK")
    return 0


def main_offbox_push(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Encrypt the newest snapshot; upload only when asked.")
    ap.add_argument("--backup-root", required=True)
    ap.add_argument("--key-file", default=OB.DEFAULT_KEY_FILE)
    ap.add_argument("--snapshot", help="specific snapshot dir (default: newest)")
    ap.add_argument("--upload", action="store_true",
                    help="publish to the configured GitHub repo (explicit only)")
    ap.add_argument("--gh-repo", default=OB.DEFAULT_GH_REPO)
    ap.add_argument("--retention", type=int, default=OB.DEFAULT_OFFBOX_RETENTION)
    a = ap.parse_args(argv)

    snap = Path(a.snapshot) if a.snapshot else SNAP.latest_snapshot(Path(a.backup_root))
    if snap is None:
        print("OFFBOX_FAILED: no snapshot found", file=sys.stderr)
        return 1
    try:
        res = OB.encrypt_snapshot(snap, key_file=Path(a.key_file))
        print(f"  artifact={res['artifact_name']} sha256={res['sha256']}")
        if a.upload:
            up = OB.upload_artifact(
                Path(res["artifact"]),
                target=OB.OffboxTarget(repo=a.gh_repo, retention=a.retention))
            res.update(up)
            print(f"  uploaded tag={up['tag']} repo={up['repo']}")
        else:
            print("  off-box upload SKIPPED (--upload not given): local-only mode")
    except OB.OffboxError as exc:
        print(f"OFFBOX_FAILED: {exc}", file=sys.stderr)
        return 1
    print("OFFBOX_OK")
    return 0


def main_restore_verify(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Prove a backup restores. Never touches production.")
    ap.add_argument("target", help="encrypted *.tar.gz.enc OR a snapshot dir")
    ap.add_argument("--key-file", default=OB.DEFAULT_KEY_FILE)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    target = Path(a.target)
    if target.is_dir():
        proof = VER.verify_snapshot_dir(target)
    else:
        proof = VER.verify_encrypted_artifact(target, key_file=Path(a.key_file))

    if a.json:
        print(json.dumps(proof.to_dict(), indent=2, sort_keys=True))
    else:
        for c in proof.checks:
            print(f"  ok: {c}")
        for e in proof.errors:
            print(f"  FAIL: {e}", file=sys.stderr)
    print(proof.status)
    return 0 if proof.status == VER.PROOF_OK else 1
