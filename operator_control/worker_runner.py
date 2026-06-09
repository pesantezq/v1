"""Phase 2 worker runner — CLI-only consumer of work orders.

Default behavior = scaffolding (prepare an isolated worktree + prompt for a
human to launch Claude Code). The autonomous headless path runs only when ALL
gates pass; it never merges or pushes and is bounded by the protected-path +
test guards. See docs/operator_control_worker_runner.md.

SAFETY:
  * Runs OUTSIDE the web process (CLI only). The FastAPI app stays create-only.
  * Never merges to main, never pushes. All work happens in a throwaway git
    worktree on branch operator/<id>; humans review and integrate.
  * On failure / protected-path violation the worktree is QUARANTINED (left in
    place), never auto-deleted.
  * Single-flight lock (reuse run_lock). Manual trigger only — no cron.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from operator_control import (
    work_orders as wo,
    worktree,
    audit_log,
    report_path,
)
from operator_control.worker_prompts import render_prompt
from operator_control.skill_registry import get_skill
from operator_control.protected_paths import violating_paths
import run_lock

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
ELIGIBLE_STATUSES = ("queued", "approved")


class WorkerRunnerError(RuntimeError):
    pass


def _lock_file(root):
    return Path(root) / "outputs" / "operator_control" / ".worker.lock"


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def autonomous_enabled(root) -> bool:
    """All three gates must pass: config flag + env + no kill-switch file."""
    root = Path(root)
    if (root / "config" / "operator_worker.DISABLED").exists():
        return False
    if os.environ.get("STOCKBOT_OPERATOR_WORKER_AUTONOMOUS", "").strip() != "1":
        return False
    try:
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(
        cfg.get("operator_control", {}).get("autonomous_worker", {}).get("enabled")
    )


def _eligible(order) -> bool:
    return bool(order and order.get("status") in ELIGIBLE_STATUSES)


# ---------------------------------------------------------------------------
# Shared prepare (claim + worktree + prompt)
# ---------------------------------------------------------------------------


def _run_helper(root, work_order_id) -> str:
    rep = report_path(root, work_order_id)
    return (
        f"# How to run work order {work_order_id}\n\n"
        f"You are in an isolated git worktree on branch `operator/{work_order_id}`.\n\n"
        f"1. Read `WORKER_PROMPT.md` (your contract).\n"
        f"2. Launch Claude Code here: `claude` (interactive) and follow the prompt.\n"
        f"3. Run the skill's required tests.\n"
        f"4. Write your report to `{rep}`.\n"
        f"5. From the repo root run: "
        f"`python -m operator_control.worker_runner complete --id {work_order_id}`\n"
        f"   (or `fail --id {work_order_id} --note '...'`).\n\n"
        f"Do NOT merge or push. A human reviews this branch before integration.\n"
    )


def _prepare(root, work_order_id, actor):
    """Claim + worktree + prompt for both paths. Returns (order, worktree, branch)."""
    order = wo.get_work_order(root, work_order_id)
    if not _eligible(order):
        raise WorkerRunnerError(
            f"work order {work_order_id} not eligible "
            f"(status={order.get('status') if order else 'missing'}; "
            f"need one of {ELIGIBLE_STATUSES})"
        )
    wo.transition_work_order(
        root, work_order_id, new_status="claimed", actor=actor,
        note="claimed by worker_runner",
    )
    wt, branch = worktree.create_worktree(root, work_order_id, base="main")
    md = render_prompt(root, work_order_id)
    (wt / "WORKER_PROMPT.md").write_text(md, encoding="utf-8")
    (wt / "RUN_WORKER.md").write_text(_run_helper(root, work_order_id), encoding="utf-8")
    wo.attach_prompt_path(root, work_order_id, "WORKER_PROMPT.md", actor=actor)
    return order, wt, branch


def scaffold(root, work_order_id, actor="cli") -> dict:
    """Default path: prepare the worktree + prompt; a human launches the worker."""
    _order, wt, branch = _prepare(root, work_order_id, actor)
    return {
        "work_order_id": work_order_id,
        "mode_of_runner": "scaffold",
        "worktree": str(wt),
        "branch": branch,
        "next": f"Launch claude in {wt}; then 'complete --id {work_order_id}'.",
    }


# ---------------------------------------------------------------------------
# Autonomous path (gated) + deterministic guards
# ---------------------------------------------------------------------------


def _invoke_claude(worktree_path, prompt_md: str) -> dict:
    """Run headless Claude Code in the worktree. Real subprocess; mocked in tests."""
    settings = Path(__file__).parent / "worker_settings.json"
    proc = subprocess.run(
        ["claude", "-p", prompt_md, "--output-format", "json",
         "--settings", str(settings)],
        cwd=str(worktree_path), capture_output=True, text=True,
    )
    return {"ok": proc.returncode == 0, "stdout": proc.stdout, "stderr": proc.stderr}


def _run_tests(worktree_path, tests) -> dict:
    """Run the skill's required tests inside the worktree.

    Commands come from the skill registry (trusted, hardcoded). We still split
    with shlex and run WITHOUT a shell so no string is ever interpreted by a
    shell (no command injection, no metacharacter surprises).
    """
    outputs = []
    passed = True
    for t in tests:
        proc = subprocess.run(
            shlex.split(t), cwd=str(worktree_path),
            capture_output=True, text=True,
        )
        outputs.append(f"$ {t}\n{proc.stdout}\n{proc.stderr}")
        if proc.returncode != 0:
            passed = False
    return {"passed": passed, "output": "\n".join(outputs)}


def _write_report(root, work_order_id, *, status, diff, tests, worker, violations, actor):
    rep = report_path(root, work_order_id)
    rep.parent.mkdir(parents=True, exist_ok=True)
    body = [f"# Worker report — {work_order_id}", "", f"Status: **{status}**", ""]
    if violations:
        body += [
            "## ⚠ Protected-path violation (quarantined)", "",
            "The worker diff touched protected paths; the run was failed and the "
            "worktree retained for inspection:", "",
        ]
        body += [f"- `{v}`" for v in violations] + [""]
    body += ["## Changed files", ""] + ([f"- `{d}`" for d in diff] or ["(none)"]) + [""]
    body += ["## Tests", "", "```", (tests or {}).get("output", "")[:8000], "```", ""]
    body += ["## Worker output", "", "```", (worker or {}).get("stdout", "")[:8000], "```", ""]
    rep.write_text("\n".join(body), encoding="utf-8")
    try:
        rel = str(rep.relative_to(Path(root)))
    except (ValueError, TypeError):
        rel = str(rep)
    wo.attach_report_path(root, work_order_id, rel, actor=actor)
    return rep


def run(root, work_order_id, actor="cli") -> dict:
    """Autonomous path when gated on; otherwise falls back to scaffold."""
    root = Path(root)
    lock = _lock_file(root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    if not run_lock.acquire_run_lock(lock):
        raise WorkerRunnerError("another worker run is in progress")
    try:
        if not autonomous_enabled(root):
            return scaffold(root, work_order_id, actor=actor)

        order, wt, branch = _prepare(root, work_order_id, actor)
        wo.transition_work_order(
            root, work_order_id, new_status="running", actor=actor,
            note="autonomous worker started",
        )
        skill = get_skill(order["skill_id"])
        worker = _invoke_claude(wt, (wt / "WORKER_PROMPT.md").read_text(encoding="utf-8"))
        diff = worktree.changed_files(wt, base="main")
        violations = violating_paths(diff)

        if violations:
            audit_log.record_event(
                root, event_type="worker_protected_path_violation", actor=actor,
                work_order_id=work_order_id, probe_id=order["probe_id"],
                skill_id=order["skill_id"], mode=order["mode"],
                details={"violations": violations}, safety_result="quarantined",
            )
            _write_report(root, work_order_id, status="failed", diff=diff,
                          tests=None, worker=worker, violations=violations, actor=actor)
            wo.transition_work_order(
                root, work_order_id, new_status="failed", actor=actor,
                note="protected-path violation (quarantined)",
            )
            return {"work_order_id": work_order_id, "mode_of_runner": "autonomous",
                    "result": "quarantined", "worktree": str(wt), "violations": violations}

        tests = _run_tests(wt, skill.required_tests if skill else [])
        status = "completed" if tests["passed"] else "failed"
        _write_report(root, work_order_id, status=status, diff=diff, tests=tests,
                      worker=worker, violations=[], actor=actor)
        wo.transition_work_order(
            root, work_order_id, new_status=status, actor=actor,
            note=f"autonomous worker {status}",
        )
        return {"work_order_id": work_order_id, "mode_of_runner": "autonomous",
                "result": status, "worktree": str(wt), "branch": branch}
    finally:
        run_lock.release_run_lock(lock)


# ---------------------------------------------------------------------------
# Manual complete / fail
# ---------------------------------------------------------------------------


def complete(root, work_order_id, actor="cli", note="manual worker completed") -> dict:
    cur = wo.get_work_order(root, work_order_id)
    if cur is None:
        raise WorkerRunnerError(f"unknown work order {work_order_id}")
    if cur["status"] == "claimed":
        wo.transition_work_order(
            root, work_order_id, new_status="running", actor=actor,
            note="manual worker started",
        )
    # Attach the report if the worker wrote one at the conventional path
    # (the autonomous path attaches it itself; this covers the manual path).
    rp = report_path(root, work_order_id)
    if rp.exists():
        try:
            rel = str(rp.relative_to(Path(root)))
        except (ValueError, TypeError):
            rel = str(rp)
        wo.attach_report_path(root, work_order_id, rel, actor=actor)
    return wo.transition_work_order(
        root, work_order_id, new_status="completed", actor=actor, note=note,
    )


def fail(root, work_order_id, actor="cli", note="") -> dict:
    cur = wo.get_work_order(root, work_order_id)
    if cur is None:
        raise WorkerRunnerError(f"unknown work order {work_order_id}")
    return wo.transition_work_order(
        root, work_order_id, new_status="failed", actor=actor,
        note=note or "manual fail",
    )


def drain(root, max_orders: int = 10, actor: str = "cron") -> dict:
    """Run eligible orders through the autonomous path until none remain / max hit.

    NO-OP unless the autonomous worker is enabled (Phase 2 three-part gate) —
    unattended *scaffolding* is useless, so the drain only acts when autonomous
    is on. Bounded by ``max_orders``; each order goes through :func:`run` (which
    holds the single-flight lock). Never merges or pushes.
    """
    root = Path(root)
    if not autonomous_enabled(root):
        return {"drained": 0, "status": "inert",
                "reason": "autonomous worker disabled"}
    results = []
    for _ in range(max(0, int(max_orders))):
        elig = [o for o in wo.list_work_orders(root) if _eligible(o)]
        if not elig:
            break
        # Oldest-created first (list_work_orders is newest-first).
        results.append(run(root, elig[-1]["work_order_id"], actor=actor))
    return {"drained": len(results), "status": "ran", "results": results}


def status(root) -> dict:
    root = Path(root)
    orders = wo.list_work_orders(root)
    counts: dict[str, int] = {}
    for o in orders:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
    return {
        "by_status": counts,
        "worktrees": worktree.list_worktrees(root),
        "autonomous_enabled": autonomous_enabled(root),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser():
    p = argparse.ArgumentParser(
        prog="python -m operator_control.worker_runner",
        description="Operator-control worker runner (scaffold by default; autonomous is gated).",
    )
    p.add_argument("--root", default=str(DEFAULT_ROOT))
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("scaffold", "run", "complete"):
        sp = sub.add_parser(name)
        sp.add_argument("--id", required=True)
        sp.add_argument("--actor", default="cli")
    spf = sub.add_parser("fail")
    spf.add_argument("--id", required=True)
    spf.add_argument("--actor", default="cli")
    spf.add_argument("--note", default="")
    spn = sub.add_parser("run-next")
    spn.add_argument("--actor", default="cli")
    spd = sub.add_parser("drain")
    spd.add_argument("--max", type=int, default=10)
    spd.add_argument("--actor", default="cron")
    sub.add_parser("status")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(args.root)
    try:
        if args.command == "scaffold":
            print(json.dumps(scaffold(root, args.id, actor=args.actor), indent=2))
            return 0
        if args.command == "run":
            print(json.dumps(run(root, args.id, actor=args.actor), indent=2))
            return 0
        if args.command == "run-next":
            elig = [o for o in wo.list_work_orders(root) if _eligible(o)]
            if not elig:
                print("No eligible work orders.")
                return 0
            print(json.dumps(run(root, elig[-1]["work_order_id"], actor=args.actor), indent=2))
            return 0
        if args.command == "complete":
            print(json.dumps(complete(root, args.id, actor=args.actor), indent=2))
            return 0
        if args.command == "fail":
            print(json.dumps(fail(root, args.id, actor=args.actor, note=args.note), indent=2))
            return 0
        if args.command == "drain":
            print(json.dumps(drain(root, max_orders=args.max, actor=args.actor), indent=2))
            return 0
        if args.command == "status":
            print(json.dumps(status(root), indent=2))
            return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "autonomous_enabled",
    "scaffold",
    "run",
    "complete",
    "fail",
    "drain",
    "status",
    "main",
    "WorkerRunnerError",
]
