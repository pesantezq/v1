"""R&D Control Plane — local CLI / test driver (Phase 0A).

Smallest useful driver to create/advance/inspect jobs and exercise recovery +
health locally. It NEVER executes an LLM, sandbox, or worker process — it only
drives the deterministic registry.

Examples:
    python -m portfolio_automation.rd_control init
    python -m portfolio_automation.rd_control create --type FINANCE_RESEARCH --authority W0_ANALYZE
    python -m portfolio_automation.rd_control queue <job_id>
    python -m portfolio_automation.rd_control advance <job_id> ADMITTED
    python -m portfolio_automation.rd_control show [job_id]
    python -m portfolio_automation.rd_control recover --max-seconds 21600
    python -m portfolio_automation.rd_control health
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from portfolio_automation.rd_control import registry as reg, health as hlth
from portfolio_automation.rd_control.contracts import (
    JobType, JobStatus, WorkerAuthority, RDControlError,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rd_control", description="R&D control plane (Phase 0A).")
    p.add_argument("--db", default=reg.DEFAULT_DB_PATH, help="SQLite DB path")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create/upgrade the registry schema")

    c = sub.add_parser("create", help="create a job (status CREATED)")
    c.add_argument("--type", required=True, choices=[t.value for t in JobType])
    c.add_argument("--authority", required=True, choices=[a.value for a in WorkerAuthority])
    c.add_argument("--stockbot-sha", default=None)
    c.add_argument("--snapshot-id", default=None)
    c.add_argument("--snapshot-hash", default=None)

    q = sub.add_parser("queue", help="CREATED -> QUEUED")
    q.add_argument("job_id")

    a = sub.add_parser("advance", help="transition a job to a target status")
    a.add_argument("job_id")
    a.add_argument("status", choices=[s.value for s in JobStatus])
    a.add_argument("--reason", default=None)

    s = sub.add_parser("show", help="show one job (with events) or all jobs")
    s.add_argument("job_id", nargs="?", default=None)

    r = sub.add_parser("recover", help="mark stale RUNNING jobs INTERRUPTED")
    r.add_argument("--max-seconds", type=int, default=21600)

    sub.add_parser("health", help="print observe-only health JSON")

    args = p.parse_args(argv)

    try:
        if args.cmd == "init":
            with reg.connect(args.db) as conn:
                print(json.dumps({"db": args.db, "schema_version": reg.schema_version(conn)}))
            return 0
        if args.cmd == "create":
            with reg.connect(args.db) as conn:
                rec = reg.create_job(
                    conn, job_type=JobType(args.type),
                    authority=WorkerAuthority(args.authority), created_at=_now(),
                    stockbot_sha=args.stockbot_sha, input_snapshot_id=args.snapshot_id,
                    input_snapshot_hash=args.snapshot_hash,
                )
                print(json.dumps({"job_id": rec.job_id, "status": rec.status.value,
                                  "input_manifest_hash": rec.input_manifest_hash}))
            return 0
        if args.cmd == "queue":
            with reg.connect(args.db) as conn:
                rec = reg.transition(conn, args.job_id, JobStatus.QUEUED, at=_now(), reason="cli")
                print(json.dumps({"job_id": rec.job_id, "status": rec.status.value}))
            return 0
        if args.cmd == "advance":
            with reg.connect(args.db) as conn:
                rec = reg.transition(conn, args.job_id, JobStatus(args.status),
                                     at=_now(), reason=args.reason or "cli")
                print(json.dumps({"job_id": rec.job_id, "status": rec.status.value}))
            return 0
        if args.cmd == "show":
            with reg.connect(args.db) as conn:
                if args.job_id:
                    rec = reg.get_job(conn, args.job_id)
                    print(json.dumps({"job": rec.to_row(),
                                      "events": reg.job_events(conn, args.job_id)}, indent=2))
                else:
                    print(json.dumps([r.to_row() for r in reg.list_jobs(conn)], indent=2))
            return 0
        if args.cmd == "recover":
            with reg.connect(args.db) as conn:
                ids = reg.recover_stale_running(conn, now=_now(), max_running_seconds=args.max_seconds)
                print(json.dumps({"recovered": ids}))
            return 0
        if args.cmd == "health":
            print(json.dumps(hlth.build_health(args.db), indent=2))
            return 0
    except RDControlError as exc:
        print(f"RD_CONTROL_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
