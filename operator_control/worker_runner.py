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
import hashlib
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from operator_control import (
    work_orders as wo,
    worktree,
    audit_log,
    report_path,
    worker_cost_log_path,
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


def _invoke_claude(worktree_path, prompt_md: str, mode: str = "diagnose") -> dict:
    """Run headless Claude Code in the worktree. Real subprocess; mocked in tests.

    Strips ANTHROPIC_API_KEY from the child env so the worker authenticates via
    the box's Claude Code login (subscription) instead of an external API key.
    On this VPS a stray/invalid ANTHROPIC_API_KEY forced API-key auth and 401'd;
    the login credentials in ~/.claude work headlessly once the key is removed.

    For ``safe_repair`` the worker is run with ``--permission-mode acceptEdits``
    so it can actually edit files in the (isolated) worktree; the deny rules in
    worker_settings.json still apply, and edits land only in the worktree.
    ``diagnose`` runs with default permissions (read-only capable).

    Returns ``ok`` plus the operational-cost fields claude reports
    (``cost_usd``, ``num_turns``, ``duration_ms``) so the runner can log spend.
    """
    settings = Path(__file__).parent / "worker_settings.json"
    child_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    argv = ["claude", "-p", prompt_md, "--output-format", "json",
            "--settings", str(settings)]
    if mode == "safe_repair":
        argv += ["--permission-mode", "acceptEdits"]
    proc = subprocess.run(
        argv, cwd=str(worktree_path), capture_output=True, text=True, env=child_env,
    )
    ok = proc.returncode == 0
    error = None
    cost_usd = 0.0
    num_turns = None
    duration_ms = None
    result_text = None
    try:
        lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
        parsed = json.loads(lines[-1]) if lines else {}
        if isinstance(parsed, dict):
            cost_usd = float(parsed.get("total_cost_usd") or 0.0)
            num_turns = parsed.get("num_turns")
            duration_ms = parsed.get("duration_ms")
            result_text = parsed.get("result")
            # is_error can be set even with a 0 exit code (e.g. an auth 401):
            # treat that as a failed worker run, not a silent success.
            if parsed.get("is_error"):
                ok = False
                error = parsed.get("result") or f"api_error_status={parsed.get('api_error_status')}"
    except (json.JSONDecodeError, IndexError, ValueError, TypeError):
        pass
    if not ok and error is None:
        error = (proc.stderr or "non-zero exit").strip()[:300]
    return {"ok": ok, "stdout": proc.stdout, "stderr": proc.stderr, "error": error,
            "cost_usd": cost_usd, "num_turns": num_turns, "duration_ms": duration_ms,
            "result_text": result_text}


def _run_tests(worktree_path, tests) -> dict:
    """Run the skill's required tests inside the worktree.

    Commands come from the skill registry (trusted, hardcoded). We still split
    with shlex and run WITHOUT a shell so no string is ever interpreted by a
    shell (no command injection, no metacharacter surprises).

    A leading bare ``python`` is rewritten to the runner's own interpreter
    (``sys.executable``) so the skill's tests run under the project venv (which
    has pytest) rather than a bare system ``python`` in the worktree subprocess.
    """
    outputs = []
    passed = True
    for t in tests:
        argv = shlex.split(t)
        if argv and argv[0] == "python":
            argv[0] = sys.executable
        proc = subprocess.run(
            argv, cwd=str(worktree_path),
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


# Live production state a contained worker must NEVER change. The worker works
# in an isolated worktree and we never merge/push — this is a deterministic
# tripwire that a run did not somehow bleed into the live tree or move main.
_PRODUCTION_MARKERS = (
    "config.json",
    "config/signal_registry.yaml",
    "outputs/latest/decision_plan.json",
)


def _file_hash(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def _production_snapshot(root) -> dict:
    root = Path(root)
    r = subprocess.run(["git", "-C", str(root), "rev-parse", "main"],
                       capture_output=True, text=True)
    return {
        "main_sha": r.stdout.strip(),
        "files": {m: _file_hash(root / m) for m in _PRODUCTION_MARKERS},
    }


def _production_impact(root, snap: dict) -> list[str]:
    """Return production markers the run changed (empty = none). The 'failed
    gate': any non-empty result means the worker bled into production."""
    after = _production_snapshot(root)
    changed = []
    if snap.get("main_sha") and after["main_sha"] != snap["main_sha"]:
        changed.append("main HEAD moved")
    for m, h in (snap.get("files") or {}).items():
        if after["files"].get(m) != h:
            changed.append(m)
    return changed


def _record_cost(root, order, worker, *, status: str) -> dict:
    """Append one operational-cost record. SEPARATE from the FMP/AI decision
    budget — this is the cost of running the worker itself, with the 'why'."""
    rec = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "work_order_id": order.get("work_order_id"),
        "probe_id": order.get("probe_id"),
        "skill_id": order.get("skill_id"),
        "mode": order.get("mode"),
        "why": order.get("requested_action"),
        "status": status,
        "cost_usd": round(float(worker.get("cost_usd") or 0.0), 6),
        "num_turns": worker.get("num_turns"),
        "duration_ms": worker.get("duration_ms"),
        "budget_scope": "operator_worker_operational",
    }
    path = worker_cost_log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")
    return rec


def read_cost_log(root, limit: int | None = None) -> list[dict]:
    path = worker_cost_log_path(root)
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out[-limit:] if limit else out


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
        prod = _production_snapshot(root)
        worker = _invoke_claude(
            wt, (wt / "WORKER_PROMPT.md").read_text(encoding="utf-8"),
            mode=order.get("mode", "diagnose"),
        )
        # Cost is incurred regardless of outcome — log it once, immediately,
        # in the operational ledger (NOT the FMP/AI decision budget).
        _record_cost(root, order, worker,
                     status=("ok" if worker.get("ok") else "worker_error"))

        # FAILED GATE — no production impact. A contained worker must never
        # change main or a live production file. If it did (escaped the
        # worktree), fail loudly; the order is never merged regardless.
        impacted = _production_impact(root, prod)
        if impacted:
            audit_log.record_event(
                root, event_type="worker_production_impact", actor=actor,
                work_order_id=work_order_id, probe_id=order["probe_id"],
                skill_id=order["skill_id"], mode=order["mode"],
                details={"impacted": impacted}, safety_result="blocked: production changed",
            )
            _write_report(root, work_order_id, status="failed", diff=[],
                          tests=None, worker=worker, violations=impacted, actor=actor)
            wo.transition_work_order(
                root, work_order_id, new_status="failed", actor=actor,
                note=f"production-impact gate: {', '.join(impacted)}",
            )
            return {"work_order_id": work_order_id, "mode_of_runner": "autonomous",
                    "result": "production_impact_blocked", "worktree": str(wt),
                    "impacted": impacted}

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

        # The worker process itself must have succeeded. A claude error (e.g. an
        # auth 401, or a non-zero exit) means the worker did NOT do the work —
        # do not pass it off as "completed" just because the diff is clean and
        # pre-existing tests pass.
        if not worker.get("ok"):
            _write_report(root, work_order_id, status="failed", diff=diff,
                          tests=None, worker=worker, violations=[], actor=actor)
            wo.transition_work_order(
                root, work_order_id, new_status="failed", actor=actor,
                note=f"worker process errored: {worker.get('error') or 'non-zero exit'}",
            )
            return {"work_order_id": work_order_id, "mode_of_runner": "autonomous",
                    "result": "failed", "reason": "worker_error",
                    "worktree": str(wt), "error": worker.get("error")}

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
    cost_log = read_cost_log(root)
    total_cost = round(sum(float(c.get("cost_usd") or 0.0) for c in cost_log), 4)
    return {
        "by_status": counts,
        "worktrees": worktree.list_worktrees(root),
        "autonomous_enabled": autonomous_enabled(root),
        "operational_cost_usd_total": total_cost,
        "operational_runs": len(cost_log),
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
    sub.add_parser("cost")  # operational cost ledger (separate from FMP/AI budget)
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
        if args.command == "cost":
            log = read_cost_log(root)
            total = round(sum(float(c.get("cost_usd") or 0.0) for c in log), 4)
            print(f"Operator-worker operational cost (separate from FMP/AI decision budget)")
            print(f"  runs: {len(log)} · total: ${total}")
            for c in log:
                print(f"  {c.get('timestamp','')[:19]}  ${c.get('cost_usd')}  "
                      f"{c.get('mode')}  {c.get('probe_id')} → {c.get('skill_id')}  "
                      f"[{c.get('status')}]  ({c.get('num_turns')} turns)")
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
    "read_cost_log",
    "main",
    "WorkerRunnerError",
]
