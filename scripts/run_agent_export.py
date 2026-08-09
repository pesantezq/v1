#!/usr/bin/env python3
"""
run_agent_export.py — local CLI for the Agent Production Export subsystem.

Builds / validates an immutable agent snapshot from local StockBot artifacts.
Local development tool ONLY. It does NOT run the daily pipeline, invoke Prime,
SSH, upload, call external AI, send email, or call broker APIs.

Examples:
    python scripts/run_agent_export.py --dry-run
    python scripts/run_agent_export.py --run-id 2026-08-08_daily
    python scripts/run_agent_export.py --validate-only outputs/agent_export/snapshots/snap-...
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Ensure repo root on path when invoked as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation import agent_export as ax  # noqa: E402


def _detect_git_sha(root: str) -> str | None:
    """Best-effort LOCAL git sha (no network). Returns None if unavailable."""
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        sha = out.stdout.strip()
        return sha or None
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build/validate an agent production snapshot.")
    p.add_argument("--artifacts-root", default=ax.DEFAULT_ARTIFACTS_ROOT,
                   help="Root of StockBot output artifacts (default: outputs)")
    p.add_argument("--output-root", default=None,
                   help="Where to write the snapshot tree (default: same as --artifacts-root)")
    p.add_argument("--run-id", default=None, help="Production run id (drives snapshot id)")
    p.add_argument("--git-sha", default=None,
                   help="Production git sha; if omitted, detected locally via git rev-parse")
    p.add_argument("--run-started-at", default=None, help="UTC ISO8601 run start")
    p.add_argument("--run-completed-at", default=None, help="UTC ISO8601 run completion")
    p.add_argument("--dry-run", action="store_true", help="Plan only; write nothing")
    p.add_argument("--validate-only", metavar="SNAPSHOT_DIR", default=None,
                   help="Validate an existing snapshot dir and exit")
    p.add_argument("--write-health", action="store_true",
                   help="Also (re)write outputs/policy/agent_export_health.json")
    args = p.parse_args(argv)

    if args.validate_only:
        result = ax.validate_agent_snapshot(args.validate_only)
        print(json.dumps(result, indent=2))
        return 0 if result["valid"] else 2

    git_sha = args.git_sha or _detect_git_sha(args.artifacts_root) or _detect_git_sha(".")
    if not git_sha:
        print("ERROR: could not determine production git sha; pass --git-sha", file=sys.stderr)
        return 2

    try:
        manifest = ax.build_snapshot(
            production_git_sha=git_sha,
            production_run_id=args.run_id,
            run_started_at=args.run_started_at,
            run_completed_at=args.run_completed_at,
            artifacts_root=args.artifacts_root,
            output_root=args.output_root,
            dry_run=args.dry_run,
        )
    except ax.AgentExportError as exc:
        print(f"AGENT EXPORT FAILED (fail-closed): {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(json.dumps(manifest, indent=2))
        return 0

    out_root = args.output_root or args.artifacts_root
    snap_path = Path(out_root) / ax.EXPORT_SUBDIR / ax.SNAPSHOTS_SUBDIR / manifest["snapshot_id"]
    result = ax.validate_agent_snapshot(snap_path)

    if args.write_health:
        ax.run_agent_export_health(output_root=out_root, write_files=True)

    print(json.dumps({
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_path": str(snap_path),
        "snapshot_hash": manifest["snapshot_hash"],
        "overall_status": manifest["health"]["overall_status"],
        "artifacts": len(manifest["artifacts"]),
        "gaps": len(manifest["gaps"]),
        "valid": result["valid"],
    }, indent=2))
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
