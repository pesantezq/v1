#!/usr/bin/env python
"""Build a frozen Agent Lab snapshot from the last COMPLETED production run.

Inert by construction. This entrypoint:

* reads already-written production artifacts from an explicit allowlist;
* writes only under ``outputs/agent_export/`` plus one health artifact under
  ``outputs/policy/``.

It does NOT run the production pipeline, send email, call a broker, make any
network request, invoke Prime, use SSH, or upload anything. Transport of a
snapshot to the Agent Lab is deliberately not implemented — see
``docs/STOCKBOT_AGENT_EXPORT.md``.

Usage::

    python scripts/run_agent_export.py                 # build + validate + health
    python scripts/run_agent_export.py --dry-run       # assemble, verify, discard
    python scripts/run_agent_export.py --validate-only # re-verify the latest snapshot
    python scripts/run_agent_export.py --snapshot-id <id> --validate-only

Exit codes: ``0`` success, ``1`` refusal/failure, ``2`` bad invocation.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    """Walk upward for the repo marker set used by scripts/run_daily_safe.sh."""
    for candidate in (start, *start.parents):
        if (candidate / "main.py").is_file() and (candidate / "portfolio_automation").is_dir():
            return candidate
    return start


REPO_ROOT = _find_repo_root(Path(__file__).resolve().parent)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from portfolio_automation.agent_export import MANIFEST_FILENAME, SNAPSHOTS_DIRNAME  # noqa: E402
from portfolio_automation.agent_export.allowlist import (  # noqa: E402
    SecretBoundaryViolation,
)
from portfolio_automation.agent_export.builder import (  # noqa: E402
    SnapshotBuildError, build_agent_snapshot, export_root, list_snapshots,
    read_latest_pointer,
)
from portfolio_automation.agent_export.health import run_agent_export_health  # noqa: E402
from portfolio_automation.agent_export.validator import (  # noqa: E402
    ValidationError, validate_agent_snapshot,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_agent_export.py",
        description="Freeze a completed production run into a verified Agent Lab snapshot.",
    )
    parser.add_argument("--root", default=str(REPO_ROOT),
                        help="Repository root containing outputs/ (default: detected repo root)")
    parser.add_argument("--output-root", default=None,
                        help="Outputs base directory (default: <root>/outputs). The export "
                             "lane lives at <output-root>/agent_export/.")
    parser.add_argument("--run-id", default=None,
                        help="Label the snapshot with this run id instead of the one in "
                             "run_manifest.json. Does not relax the run-completeness gate.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Assemble and verify a snapshot in a temp dir, then discard it. "
                             "Nothing is promoted and no pointer is written.")
    parser.add_argument("--validate-only", action="store_true",
                        help="Do not build. Verify an existing snapshot and refresh health.")
    parser.add_argument("--snapshot-id", default=None,
                        help="With --validate-only, verify this snapshot instead of the latest.")
    parser.add_argument("--no-health", action="store_true",
                        help="Skip writing outputs/policy/agent_export_health.json.")
    parser.add_argument("--json", action="store_true",
                        help="Emit a machine-readable result object on stdout.")
    return parser.parse_args(argv)


def _emit(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    for line in result.get("_lines", []):
        print(line)


def _resolve_snapshot_dir(root: Path, base_dir, snapshot_id: str | None) -> tuple[Path, str]:
    ids = list_snapshots(root, base_dir)
    if snapshot_id:
        if snapshot_id not in ids:
            raise ValidationError(
                f"snapshot {snapshot_id!r} not found under {export_root(root, base_dir)}")
        chosen = snapshot_id
    else:
        pointer = read_latest_pointer(root, base_dir) or {}
        pointed = pointer.get("snapshot_id")
        if not ids:
            raise ValidationError(f"no snapshots exist under {export_root(root, base_dir)}")
        chosen = pointed if pointed in ids else ids[-1]
    return export_root(root, base_dir) / SNAPSHOTS_DIRNAME / chosen, chosen


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.snapshot_id and not args.validate_only:
        print("--snapshot-id is only meaningful with --validate-only", file=sys.stderr)
        return 2
    if args.dry_run and args.validate_only:
        print("--dry-run and --validate-only are mutually exclusive", file=sys.stderr)
        return 2

    root = Path(args.root).resolve()
    base_dir = Path(args.output_root).resolve() if args.output_root else None
    now = datetime.now(timezone.utc)
    lines: list[str] = []
    result: dict = {"mode": "build", "ok": False}

    try:
        if args.validate_only:
            result["mode"] = "validate"
            snapshot_dir, snapshot_id = _resolve_snapshot_dir(root, base_dir, args.snapshot_id)
            manifest = validate_agent_snapshot(snapshot_dir)
            result.update({
                "ok": True,
                "snapshot_id": snapshot_id,
                "snapshot_dir": str(snapshot_dir),
                "snapshot_sha256": manifest["snapshot_sha256"],
                "production_git_sha": manifest["production"]["production_git_sha"],
                "production_run_id": manifest["production"]["run_id"],
                "snapshot_health": manifest["health"]["status"],
                "artifact_count": manifest["counts"]["artifacts"],
            })
            lines.append(f"VALIDATED  {snapshot_id}")
        else:
            build = build_agent_snapshot(
                root,
                created_at=now.isoformat(),
                base_dir=base_dir,
                run_id_override=args.run_id,
                dry_run=args.dry_run,
            )
            manifest = build.manifest
            result.update({
                "ok": True,
                "mode": "dry-run" if args.dry_run else "build",
                "snapshot_id": build.snapshot_id,
                "snapshot_dir": str(build.snapshot_dir),
                "snapshot_sha256": manifest["snapshot_sha256"],
                "created": build.created,
                "production_git_sha": manifest["production"]["production_git_sha"],
                "production_run_id": manifest["production"]["run_id"],
                "snapshot_health": manifest["health"]["status"],
                "artifact_count": manifest["counts"]["artifacts"],
                "missing_optional": manifest["missing_optional"],
            })
            if args.dry_run:
                lines.append(f"DRY-RUN    {build.snapshot_id} (assembled + verified, discarded)")
            elif build.created:
                lines.append(f"CREATED    {build.snapshot_id}")
            else:
                lines.append(f"IDENTICAL  {build.snapshot_id} (already exported; not rewritten)")

        manifest_health = result.get("snapshot_health")
        lines.append(f"  path            {result['snapshot_dir']}")
        lines.append(f"  manifest        {MANIFEST_FILENAME}")
        lines.append(f"  run_id          {result['production_run_id']}")
        lines.append(f"  production_sha  {result['production_git_sha']}")
        lines.append(f"  artifacts       {result['artifact_count']}")
        lines.append(f"  snapshot_sha256 {result['snapshot_sha256'][:16]}…")
        lines.append(f"  run health      {manifest_health}")
        for name in result.get("missing_optional") or []:
            lines.append(f"  optional absent {name}")

    except (SnapshotBuildError, ValidationError, SecretBoundaryViolation) as exc:
        result.update({"ok": False, "error": type(exc).__name__, "message": str(exc)})
        lines.append(f"REFUSED    {type(exc).__name__}: {exc}")
        result["_lines"] = lines
        _emit(result, args.json)
        if not args.no_health:
            _write_health(root, base_dir, now, args.json)
        return 1

    if not args.no_health and not args.dry_run:
        health = _write_health(root, base_dir, now, args.json)
        result["export_health_status"] = health.get("status")
        lines.append(f"  export health   {health.get('status')}")

    result["_lines"] = lines
    _emit(result, args.json)
    return 0


def _write_health(root: Path, base_dir, now: datetime, as_json: bool) -> dict:
    try:
        return run_agent_export_health(root, now=now, base_dir=base_dir)
    except Exception as exc:  # health must never mask the primary outcome
        if not as_json:
            print(f"  export health   WARN (could not write: {exc})", file=sys.stderr)
        return {"status": "UNKNOWN", "error": str(exc)}


if __name__ == "__main__":
    raise SystemExit(main())
