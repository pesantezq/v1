# Operator Control Worker Runner — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a CLI-only runner that consumes Phase 1 work orders by preparing an isolated git worktree (+ generated prompt), and — only behind a default-off hard gate — optionally runs a headless Claude Code worker, with deterministic protected-path + test guards, never merging or pushing.

**Architecture:** New modules in `operator_control/` (`protected_paths`, `worktree`, `worker_runner`) plus a restricted permission profile, a read-only GUI System-tab card, docs, and tests. Runs outside the web process; reuses Phase 1 registries/policies/work-orders/audit and the repo's `run_lock`. The `claude` subprocess and the test runner are injected/mocked so tests need no real LLM.

**Tech Stack:** Python 3, stdlib `subprocess`/`argparse`/`json`, `git worktree`, FastAPI+Jinja (existing GUI), pytest.

---

## File structure

| File | Responsibility |
|---|---|
| `operator_control/protected_paths.py` | Pure deny-list classifier: `is_protected(path)`, `violating_paths(paths)`. |
| `operator_control/worktree.py` | Git-worktree wrapper: `create_worktree`, `changed_files`, `list_worktrees`, `remove_worktree`. |
| `operator_control/worker_runner.py` | Gates, eligibility, lock, scaffold path, autonomous path, guards, complete/fail, status, CLI. |
| `operator_control/worker_settings.json` | Restricted Claude Code permission profile for headless runs (static). |
| `operator_control/work_orders.py` (modify) | Add `attach_report_path()` (mirrors `attach_prompt_path`). |
| `gui_v2/data/operator_control.py` (modify) | Add `worker_runner_status(root)`; include in system context. |
| `gui_v2/templates/dashboard/system.html` (modify) | Render read-only runner card. |
| `docs/operator_control_worker_runner.md` | Runbook: gates, paths, guards, activation, review/merge. |
| `tests/test_operator_worker_runner.py` | All runner tests (claude mocked). |
| `tests/test_operator_protected_paths.py` | Deny-list unit tests. |

---

## Task 1: Protected-path classifier

**Files:**
- Create: `operator_control/protected_paths.py`
- Test: `tests/test_operator_protected_paths.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_operator_protected_paths.py
from operator_control.protected_paths import is_protected, violating_paths

def test_protected_basenames():
    assert is_protected("decision_engine.py")
    assert is_protected("scoring.py")
    assert is_protected("portfolio_decision_engine.py")
    assert is_protected("config.json")
    assert is_protected("requirements.txt")

def test_protected_paths_and_dirs():
    assert is_protected("config/signal_registry.yaml")
    assert is_protected(".claude/commands/x.md")
    assert is_protected("deploy/anything.conf")
    assert is_protected("portfolio_automation/brokers/schwab_sync.py")
    assert is_protected(".env")
    assert is_protected(".env.local")
    assert is_protected("deploy/stockbot-dashboard.service")

def test_non_protected():
    assert not is_protected("operator_control/worker_runner.py")
    assert not is_protected("gui_v2/data/today.py")
    assert not is_protected("docs/operator_control.md")
    assert not is_protected("tests/test_x.py")

def test_violating_paths_filters():
    changed = ["gui_v2/app.py", "scoring.py", "docs/x.md", ".env"]
    assert violating_paths(changed) == ["scoring.py", ".env"]
```

- [ ] **Step 2: Run to verify it fails** — `.venv/bin/python -m pytest -q tests/test_operator_protected_paths.py` → FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# operator_control/protected_paths.py
"""Pure classifier for repo paths a worker must never modify.

Used by worker_runner as a deterministic post-run guard: if a worker's diff
touches any protected path, the run is quarantined regardless of what the
worker claimed. No I/O — just string classification.
"""
from __future__ import annotations

_PROTECTED_EXACT = {
    "decision_engine.py", "portfolio_decision_engine.py", "scoring.py",
    "config.json", "requirements.txt", "config/signal_registry.yaml",
}
_PROTECTED_BASENAMES = {
    "decision_engine.py", "portfolio_decision_engine.py", "scoring.py",
    "config.json", "requirements.txt",
}
_PROTECTED_DIR_PREFIXES = (
    ".claude/", "deploy/", "portfolio_automation/brokers/",
)


def is_protected(path: str) -> bool:
    norm = str(path).replace("\\", "/").lstrip("./")
    if norm in _PROTECTED_EXACT:
        return True
    base = norm.rsplit("/", 1)[-1]
    if base in _PROTECTED_BASENAMES:
        return True
    if any(norm.startswith(p) for p in _PROTECTED_DIR_PREFIXES):
        return True
    if base.startswith(".env"):
        return True
    if base.endswith(".service"):
        return True
    return False


def violating_paths(paths) -> list[str]:
    return [p for p in paths if is_protected(p)]


__all__ = ["is_protected", "violating_paths"]
```

- [ ] **Step 4: Run** — same command → PASS.
- [ ] **Step 5: Commit** — `git add operator_control/protected_paths.py tests/test_operator_protected_paths.py && git commit -m "feat(operator): protected-path classifier for worker guard"`

---

## Task 2: Git-worktree wrapper

**Files:**
- Create: `operator_control/worktree.py`
- Test: in `tests/test_operator_worker_runner.py` (shared fixture builds a real temp git repo)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_operator_worker_runner.py  (top)
import json, subprocess
from pathlib import Path
import pytest

def _git(root, *a):
    return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, check=True)

@pytest.fixture()
def repo(tmp_path):
    """A real git repo with a committed file on `main`."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "keep.txt").write_text("hi\n")
    (tmp_path / "outputs" / "operator_control").mkdir(parents=True)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path

def test_create_worktree_and_changed_files(repo):
    from operator_control import worktree
    wt, branch = worktree.create_worktree(repo, "wo_test", base="main")
    assert wt.exists() and branch == "operator/wo_test"
    (wt / "newfile.py").write_text("x = 1\n")
    _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "edit")
    assert "newfile.py" in worktree.changed_files(wt, base="main")
```

- [ ] **Step 2: Run** → FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# operator_control/worktree.py
"""Thin wrapper over `git worktree` for isolated worker runs.

Each work order gets a throwaway worktree at .worktrees/<id> on branch
operator/<id> cut from base (default main). The runner never merges or pushes
these branches; humans review and integrate.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class WorktreeError(RuntimeError):
    pass


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )


def create_worktree(root, work_order_id: str, base: str = "main"):
    root = Path(root)
    branch = f"operator/{work_order_id}"
    path = root / ".worktrees" / work_order_id
    path.parent.mkdir(parents=True, exist_ok=True)
    r = _git(root, "worktree", "add", "-b", branch, str(path), base)
    if r.returncode != 0:
        raise WorktreeError(r.stderr.strip() or "git worktree add failed")
    return path, branch


def changed_files(worktree_path, base: str = "main") -> list[str]:
    r = _git(Path(worktree_path), "diff", "--name-only", base)
    if r.returncode != 0:
        raise WorktreeError(r.stderr.strip() or "git diff failed")
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def list_worktrees(root) -> list[str]:
    r = _git(Path(root), "worktree", "list", "--porcelain")
    return [ln.split(" ", 1)[1] for ln in r.stdout.splitlines() if ln.startswith("worktree ")]


def remove_worktree(root, worktree_path, force: bool = False) -> None:
    args = ["worktree", "remove", str(worktree_path)]
    if force:
        args.append("--force")
    _git(Path(root), *args)


__all__ = ["WorktreeError", "create_worktree", "changed_files", "list_worktrees", "remove_worktree"]
```

- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `git add operator_control/worktree.py tests/test_operator_worker_runner.py && git commit -m "feat(operator): git-worktree wrapper for worker runs"`

---

## Task 3: Gates, eligibility, lock, `attach_report_path`

**Files:**
- Modify: `operator_control/work_orders.py` (add `attach_report_path`)
- Create: `operator_control/worker_runner.py` (gates + helpers)
- Test: `tests/test_operator_worker_runner.py`

- [ ] **Step 1: Write failing tests**

```python
def test_autonomous_disabled_by_default(repo, monkeypatch):
    from operator_control import worker_runner
    monkeypatch.delenv("STOCKBOT_OPERATOR_WORKER_AUTONOMOUS", raising=False)
    assert worker_runner.autonomous_enabled(repo) is False

def test_autonomous_requires_all_three_gates(repo, monkeypatch):
    from operator_control import worker_runner
    (repo / "config").mkdir(exist_ok=True)
    (repo / "config.json").write_text(json.dumps(
        {"operator_control": {"autonomous_worker": {"enabled": True}}}))
    monkeypatch.setenv("STOCKBOT_OPERATOR_WORKER_AUTONOMOUS", "1")
    assert worker_runner.autonomous_enabled(repo) is True
    # kill switch wins
    (repo / "config" / "operator_worker.DISABLED").write_text("x")
    assert worker_runner.autonomous_enabled(repo) is False
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** — add to `work_orders.py`:

```python
def attach_report_path(root, work_order_id, report_path: str, actor: str) -> dict:
    current = get_work_order(root, work_order_id)
    if current is None:
        raise policy.WorkOrderValidationError(f"unknown work_order_id: {work_order_id!r}")
    new_record = dict(current)
    new_record["result_report_path"] = report_path
    _append_record(root, new_record)
    audit_log.record_event(root, event_type="report_attached", actor=actor,
        work_order_id=work_order_id, probe_id=current.get("probe_id"),
        skill_id=current.get("skill_id"), mode=current.get("mode"),
        details={"report_path": report_path})
    return new_record
```
Add `"attach_report_path"` to `__all__` and `"report_attached"` to `audit_log.EVENT_TYPES`.

Create `worker_runner.py` header + gates:

```python
"""Phase 2 worker runner — CLI-only consumer of work orders.

Default behavior = scaffolding (prepare an isolated worktree + prompt for a
human to launch Claude Code). The autonomous headless path runs only when ALL
gates pass; it never merges or pushes and is bounded by the protected-path +
test guards. See docs/operator_control_worker_runner.md.
"""
from __future__ import annotations

import argparse, json, os, subprocess, sys
from pathlib import Path

from operator_control import (work_orders as wo, worktree, audit_log,
                              prompt_path, report_path, reports_dir)
from operator_control.worker_prompts import render_prompt
from operator_control.skill_registry import get_skill
from operator_control.protected_paths import violating_paths
import run_lock

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
ELIGIBLE_STATUSES = ("queued", "approved")


def _lock_file(root): return Path(root) / "outputs" / "operator_control" / ".worker.lock"


def autonomous_enabled(root) -> bool:
    root = Path(root)
    if (root / "config" / "operator_worker.DISABLED").exists():
        return False
    if os.environ.get("STOCKBOT_OPERATOR_WORKER_AUTONOMOUS", "").strip() != "1":
        return False
    try:
        cfg = json.loads((root / "config.json").read_text())
    except Exception:
        return False
    return bool(cfg.get("operator_control", {}).get("autonomous_worker", {}).get("enabled"))


def _eligible(order) -> bool:
    return bool(order and order.get("status") in ELIGIBLE_STATUSES)
```

- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(operator): worker-runner gates + report-path attach"` (stage explicit paths).

---

## Task 4: Scaffolding path

**Files:** Modify `operator_control/worker_runner.py`; test in `tests/test_operator_worker_runner.py`.

- [ ] **Step 1: Failing test**

```python
def _make_order(root, probe="data_quality.warnings", skill="diagnose_data_quality_warnings", mode="diagnose"):
    return wo_mod.create_work_order(root, probe_id=probe, skill_id=skill, mode=mode, created_by="t")

def test_scaffold_creates_worktree_and_prompt(repo):
    from operator_control import work_orders as wo_mod, worker_runner
    order = wo_mod.create_work_order(repo, probe_id="data_quality.warnings",
        skill_id="diagnose_data_quality_warnings", mode="diagnose", created_by="t")
    res = worker_runner.scaffold(repo, order["work_order_id"], actor="t")
    wt = Path(res["worktree"])
    assert wt.exists()
    assert (wt / "WORKER_PROMPT.md").exists()
    assert (wt / "RUN_WORKER.md").exists()
    assert wo_mod.get_work_order(repo, order["work_order_id"])["status"] == "claimed"

def test_scaffold_refuses_awaiting_approval(repo):
    from operator_control import work_orders as wo_mod, worker_runner
    order = wo_mod.create_work_order(repo, probe_id="memo.generation_readability",
        skill_id="regenerate_memo_from_artifacts", mode="safe_repair", created_by="t")
    assert order["status"] == "awaiting_approval"
    with pytest.raises(Exception):
        worker_runner.scaffold(repo, order["work_order_id"], actor="t")
```
(Note: `tests/test_operator_worker_runner.py` already imports `work_orders`; the `repo` fixture's commit makes `main` exist for worktrees.)

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** in `worker_runner.py`:

```python
class WorkerRunnerError(RuntimeError):
    pass


def _prepare(root, work_order_id, actor):
    """Shared claim+worktree+prompt for both paths. Returns (order, worktree_path)."""
    order = wo.get_work_order(root, work_order_id)
    if not _eligible(order):
        raise WorkerRunnerError(
            f"work order {work_order_id} not eligible "
            f"(status={order.get('status') if order else 'missing'}; "
            f"need one of {ELIGIBLE_STATUSES})")
    wo.transition_work_order(root, work_order_id, new_status="claimed",
                             actor=actor, note="claimed by worker_runner")
    wt, branch = worktree.create_worktree(root, work_order_id, base="main")
    md = render_prompt(root, work_order_id)
    (wt / "WORKER_PROMPT.md").write_text(md, encoding="utf-8")
    (wt / "RUN_WORKER.md").write_text(_run_helper(root, work_order_id), encoding="utf-8")
    wo.attach_prompt_path(root, work_order_id, "WORKER_PROMPT.md", actor=actor)
    return order, wt, branch


def _run_helper(root, work_order_id) -> str:
    rep = report_path(root, work_order_id)
    return (
        f"# How to run work order {work_order_id}\n\n"
        f"You are in an isolated git worktree on branch `operator/{work_order_id}`.\n"
        f"1. Read `WORKER_PROMPT.md` (your contract).\n"
        f"2. Launch Claude Code here: `claude` (interactive) and follow the prompt.\n"
        f"3. Run the skill's required tests.\n"
        f"4. Write your report to `{rep}`.\n"
        f"5. From the repo root run: "
        f"`python -m operator_control.worker_runner complete --id {work_order_id}`\n"
        f"   (or `fail --id {work_order_id} --note '...'`).\n\n"
        f"Do NOT merge or push. A human reviews this branch before integration.\n")


def scaffold(root, work_order_id, actor="cli") -> dict:
    _order, wt, branch = _prepare(root, work_order_id, actor)
    return {"work_order_id": work_order_id, "mode_of_runner": "scaffold",
            "worktree": str(wt), "branch": branch,
            "next": f"Launch claude in {wt}; then 'complete --id {work_order_id}'."}
```

- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — explicit paths.

---

## Task 5: Autonomous path + deterministic guards

**Files:** Modify `operator_control/worker_runner.py`; tests mock `_invoke_claude` and `_run_tests`.

- [ ] **Step 1: Failing tests**

```python
def _enable_autonomous(repo, monkeypatch):
    (repo / "config.json").write_text(json.dumps(
        {"operator_control": {"autonomous_worker": {"enabled": True}}}))
    monkeypatch.setenv("STOCKBOT_OPERATOR_WORKER_AUTONOMOUS", "1")

def test_autonomous_completes_on_clean_diff_and_passing_tests(repo, monkeypatch):
    from operator_control import work_orders as wo_mod, worker_runner
    _enable_autonomous(repo, monkeypatch)
    order = wo_mod.create_work_order(repo, probe_id="data_quality.warnings",
        skill_id="diagnose_data_quality_warnings", mode="diagnose", created_by="t")
    monkeypatch.setattr(worker_runner, "_invoke_claude", lambda wt, p: {"ok": True, "stdout": "done"})
    # worker created a benign new file
    def fake_changes(wt, base="main"):
        return ["operator_control/_scratch_note.py"]
    monkeypatch.setattr(worker_runner.worktree, "changed_files", fake_changes)
    monkeypatch.setattr(worker_runner, "_run_tests", lambda wt, tests: {"passed": True, "output": "ok"})
    res = worker_runner.run(repo, order["work_order_id"], actor="auto")
    assert res["mode_of_runner"] == "autonomous"
    assert wo_mod.get_work_order(repo, order["work_order_id"])["status"] == "completed"

def test_autonomous_quarantines_protected_path(repo, monkeypatch):
    from operator_control import work_orders as wo_mod, worker_runner, audit_log
    _enable_autonomous(repo, monkeypatch)
    order = wo_mod.create_work_order(repo, probe_id="data_quality.warnings",
        skill_id="diagnose_data_quality_warnings", mode="diagnose", created_by="t")
    monkeypatch.setattr(worker_runner, "_invoke_claude", lambda wt, p: {"ok": True, "stdout": ""})
    monkeypatch.setattr(worker_runner.worktree, "changed_files", lambda wt, base="main": ["scoring.py"])
    monkeypatch.setattr(worker_runner, "_run_tests", lambda wt, tests: {"passed": True, "output": ""})
    res = worker_runner.run(repo, order["work_order_id"], actor="auto")
    assert wo_mod.get_work_order(repo, order["work_order_id"])["status"] == "failed"
    assert any(e["event_type"] == "worker_protected_path_violation"
               for e in audit_log.read_events(repo))
    # worktree retained for forensics
    assert Path(res["worktree"]).exists()

def test_autonomous_fails_on_failing_tests(repo, monkeypatch):
    from operator_control import work_orders as wo_mod, worker_runner
    _enable_autonomous(repo, monkeypatch)
    order = wo_mod.create_work_order(repo, probe_id="data_quality.warnings",
        skill_id="diagnose_data_quality_warnings", mode="diagnose", created_by="t")
    monkeypatch.setattr(worker_runner, "_invoke_claude", lambda wt, p: {"ok": True, "stdout": ""})
    monkeypatch.setattr(worker_runner.worktree, "changed_files", lambda wt, base="main": [])
    monkeypatch.setattr(worker_runner, "_run_tests", lambda wt, tests: {"passed": False, "output": "1 failed"})
    worker_runner.run(repo, order["work_order_id"], actor="auto")
    assert wo_mod.get_work_order(repo, order["work_order_id"])["status"] == "failed"

def test_run_falls_back_to_scaffold_when_disabled(repo, monkeypatch):
    from operator_control import work_orders as wo_mod, worker_runner
    monkeypatch.delenv("STOCKBOT_OPERATOR_WORKER_AUTONOMOUS", raising=False)
    order = wo_mod.create_work_order(repo, probe_id="data_quality.warnings",
        skill_id="diagnose_data_quality_warnings", mode="diagnose", created_by="t")
    res = worker_runner.run(repo, order["work_order_id"], actor="auto")
    assert res["mode_of_runner"] == "scaffold"  # no claude invoked
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** in `worker_runner.py`:

```python
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
    import shlex
    outputs = []
    passed = True
    for t in tests:
        proc = subprocess.run(shlex.split(t), cwd=str(worktree_path),
                              capture_output=True, text=True)
        outputs.append(f"$ {t}\n{proc.stdout}\n{proc.stderr}")
        if proc.returncode != 0:
            passed = False
    return {"passed": passed, "output": "\n".join(outputs)}


def _write_report(root, work_order_id, *, status, diff, tests, worker, violations, actor):
    rep = report_path(root, work_order_id)
    rep.parent.mkdir(parents=True, exist_ok=True)
    body = [f"# Worker report — {work_order_id}", "", f"Status: **{status}**", ""]
    if violations:
        body += ["## ⚠ Protected-path violation (quarantined)", "",
                 "The worker diff touched protected paths; the run was failed and "
                 "the worktree retained for inspection:", ""]
        body += [f"- `{v}`" for v in violations] + [""]
    body += ["## Changed files", ""] + ([f"- `{d}`" for d in diff] or ["(none)"]) + [""]
    body += ["## Tests", "", "```", (tests or {}).get("output", "")[:8000], "```", ""]
    body += ["## Worker output", "", "```", (worker or {}).get("stdout", "")[:8000], "```", ""]
    rep.write_text("\n".join(body), encoding="utf-8")
    wo.attach_report_path(root, work_order_id, str(rep.relative_to(Path(root))) if str(rep).startswith(str(root)) else str(rep), actor=actor)
    return rep


def run(root, work_order_id, actor="cli") -> dict:
    root = Path(root)
    lock = _lock_file(root); lock.parent.mkdir(parents=True, exist_ok=True)
    if not run_lock.acquire_run_lock(lock):
        raise WorkerRunnerError("another worker run is in progress")
    try:
        if not autonomous_enabled(root):
            return scaffold(root, work_order_id, actor=actor)
        order, wt, branch = _prepare(root, work_order_id, actor)
        wo.transition_work_order(root, work_order_id, new_status="running",
                                 actor=actor, note="autonomous worker started")
        skill = get_skill(order["skill_id"])
        worker = _invoke_claude(wt, (wt / "WORKER_PROMPT.md").read_text())
        diff = worktree.changed_files(wt, base="main")
        violations = violating_paths(diff)
        if violations:
            audit_log.record_event(root, event_type="worker_protected_path_violation",
                actor=actor, work_order_id=work_order_id,
                probe_id=order["probe_id"], skill_id=order["skill_id"], mode=order["mode"],
                details={"violations": violations}, safety_result="quarantined")
            _write_report(root, work_order_id, status="failed", diff=diff, tests=None,
                          worker=worker, violations=violations, actor=actor)
            wo.transition_work_order(root, work_order_id, new_status="failed",
                                     actor=actor, note="protected-path violation (quarantined)")
            return {"work_order_id": work_order_id, "mode_of_runner": "autonomous",
                    "result": "quarantined", "worktree": str(wt), "violations": violations}
        tests = _run_tests(wt, skill.required_tests if skill else [])
        status = "completed" if tests["passed"] else "failed"
        _write_report(root, work_order_id, status=status, diff=diff, tests=tests,
                      worker=worker, violations=[], actor=actor)
        wo.transition_work_order(root, work_order_id, new_status=status, actor=actor,
                                 note=f"autonomous worker {status}")
        return {"work_order_id": work_order_id, "mode_of_runner": "autonomous",
                "result": status, "worktree": str(wt), "branch": branch}
    finally:
        run_lock.release_run_lock(lock)
```
Add `"worker_protected_path_violation"` to `audit_log.EVENT_TYPES`.

- [ ] **Step 4: Run** → PASS (all Task-5 tests).
- [ ] **Step 5: Commit** — explicit paths.

---

## Task 6: complete / fail (manual path)

**Files:** Modify `operator_control/worker_runner.py`; tests.

- [ ] **Step 1: Failing test**

```python
def test_complete_from_claimed(repo):
    from operator_control import work_orders as wo_mod, worker_runner
    order = wo_mod.create_work_order(repo, probe_id="data_quality.warnings",
        skill_id="diagnose_data_quality_warnings", mode="diagnose", created_by="t")
    worker_runner.scaffold(repo, order["work_order_id"], actor="t")
    worker_runner.complete(repo, order["work_order_id"], actor="t")
    assert wo_mod.get_work_order(repo, order["work_order_id"])["status"] == "completed"

def test_fail_from_claimed(repo):
    from operator_control import work_orders as wo_mod, worker_runner
    order = wo_mod.create_work_order(repo, probe_id="data_quality.warnings",
        skill_id="diagnose_data_quality_warnings", mode="diagnose", created_by="t")
    worker_runner.scaffold(repo, order["work_order_id"], actor="t")
    worker_runner.fail(repo, order["work_order_id"], actor="t", note="gave up")
    assert wo_mod.get_work_order(repo, order["work_order_id"])["status"] == "failed"
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement**

```python
def complete(root, work_order_id, actor="cli", note="manual worker completed") -> dict:
    cur = wo.get_work_order(root, work_order_id)
    if cur is None:
        raise WorkerRunnerError(f"unknown work order {work_order_id}")
    if cur["status"] == "claimed":
        wo.transition_work_order(root, work_order_id, new_status="running",
                                 actor=actor, note="manual worker started")
    return wo.transition_work_order(root, work_order_id, new_status="completed",
                                    actor=actor, note=note)


def fail(root, work_order_id, actor="cli", note="") -> dict:
    cur = wo.get_work_order(root, work_order_id)
    if cur is None:
        raise WorkerRunnerError(f"unknown work order {work_order_id}")
    return wo.transition_work_order(root, work_order_id, new_status="failed",
                                    actor=actor, note=note or "manual fail")
```

- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — explicit paths.

---

## Task 7: CLI

**Files:** Modify `operator_control/worker_runner.py` (add `_build_parser`, `main`, `__main__`); test via `main([...])`.

- [ ] **Step 1: Failing test**

```python
def test_cli_status_and_scaffold(repo, capsys):
    from operator_control import work_orders as wo_mod, worker_runner
    order = wo_mod.create_work_order(repo, probe_id="data_quality.warnings",
        skill_id="diagnose_data_quality_warnings", mode="diagnose", created_by="t")
    rc = worker_runner.main(["--root", str(repo), "scaffold", "--id", order["work_order_id"]])
    assert rc == 0
    assert "worktree" in capsys.readouterr().out.lower()
    rc = worker_runner.main(["--root", str(repo), "status"])
    assert rc == 0
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement**

```python
def status(root) -> dict:
    root = Path(root)
    orders = wo.list_work_orders(root)
    counts = {}
    for o in orders:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
    return {"by_status": counts, "worktrees": worktree.list_worktrees(root),
            "autonomous_enabled": autonomous_enabled(root)}


def _build_parser():
    p = argparse.ArgumentParser(prog="python -m operator_control.worker_runner",
        description="Operator-control worker runner (scaffold by default; autonomous is gated).")
    p.add_argument("--root", default=str(DEFAULT_ROOT))
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("scaffold", "run", "complete"):
        sp = sub.add_parser(name); sp.add_argument("--id", required=True); sp.add_argument("--actor", default="cli")
    spf = sub.add_parser("fail"); spf.add_argument("--id", required=True); spf.add_argument("--actor", default="cli"); spf.add_argument("--note", default="")
    spn = sub.add_parser("run-next"); spn.add_argument("--actor", default="cli")
    sub.add_parser("status")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(args.root)
    try:
        if args.command == "scaffold":
            print(json.dumps(scaffold(root, args.id, actor=args.actor), indent=2)); return 0
        if args.command == "run":
            print(json.dumps(run(root, args.id, actor=args.actor), indent=2)); return 0
        if args.command == "run-next":
            elig = [o for o in wo.list_work_orders(root) if _eligible(o)]
            if not elig:
                print("No eligible work orders."); return 0
            print(json.dumps(run(root, elig[-1]["work_order_id"], actor=args.actor), indent=2)); return 0
        if args.command == "complete":
            print(json.dumps(complete(root, args.id, actor=args.actor), indent=2)); return 0
        if args.command == "fail":
            print(json.dumps(fail(root, args.id, actor=args.actor, note=args.note), indent=2)); return 0
        if args.command == "status":
            print(json.dumps(status(root), indent=2)); return 0
    except (WorkerRunnerError, Exception) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — explicit paths.

---

## Task 8: Restricted worker permission profile

**Files:** Create `operator_control/worker_settings.json`.

- [ ] **Step 1: Create the file** (static; consumed by `_invoke_claude --settings`)

```json
{
  "permissions": {
    "deny": [
      "Bash(git push:*)",
      "Bash(git merge:*)",
      "Bash(pip install:*)",
      "Bash(pip3 install:*)",
      "Bash(npm install:*)",
      "Bash(systemctl:*)",
      "Bash(sudo:*)",
      "Read(./.env)",
      "Read(./.env.*)",
      "Edit(./config.json)",
      "Edit(./config/signal_registry.yaml)",
      "Edit(./decision_engine.py)",
      "Edit(./scoring.py)",
      "Edit(./portfolio_decision_engine.py)"
    ]
  }
}
```

- [ ] **Step 2: Sanity check** — `.venv/bin/python -c "import json,pathlib; json.loads(pathlib.Path('operator_control/worker_settings.json').read_text()); print('valid json')"` → `valid json`.
- [ ] **Step 3: Commit** — `git add operator_control/worker_settings.json && git commit -m "feat(operator): restricted worker permission profile"`

---

## Task 9: GUI System-tab runner card (read-only)

**Files:** Modify `gui_v2/data/operator_control.py`, `gui_v2/templates/dashboard/system.html`; test in `tests/test_operator_control_gui.py`.

- [ ] **Step 1: Failing test**

```python
def test_system_tab_shows_runner_card(client_root):
    client, _ = client_root
    body = client.get("/dashboard/system").text
    assert "Worker Runner" in body
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** — add to `gui_v2/data/operator_control.py`:

```python
def worker_runner_status(root):
    """Read-only runner summary card for the System tab."""
    from operator_control import worker_runner
    try:
        st = worker_runner.status(root)
    except Exception:
        st = {"by_status": {}, "worktrees": [], "autonomous_enabled": False}
    by = st["by_status"]
    completed = by.get("completed", 0); failed = by.get("failed", 0)
    running = by.get("running", 0); claimed = by.get("claimed", 0)
    status = "red" if failed else ("warning" if running or claimed else "ok")
    label = "autonomous ON" if st["autonomous_enabled"] else "scaffold-only"
    return card("Worker Runner", status=status, label=label,
        summary=f"{completed} completed; {failed} failed; {running} running; "
                f"{claimed} claimed; {len([w for w in st['worktrees'] if '.worktrees' in w])} worktrees",
        source_artifacts=["outputs/operator_control/work_orders.jsonl"])
```
In `operator_control_context`, for `view == "system"` add the runner card to the returned dict under key `operator_runner`:
```python
    ctx = { ... existing keys ... }
    if view == "system":
        ctx["operator_runner"] = worker_runner_status(root)
    return ctx
```
In `system.html`, render before the operator panel:
```html
  {% if operator_runner %}
  <section aria-label="Worker runner">
    {{ ui.section_header("Worker Runner", "Phase 2 — observe-only; CLI-driven", "") }}
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">{{ ui.status_card(operator_runner) }}</div>
  </section>
  {% endif %}
```

- [ ] **Step 4: Run** — `.venv/bin/python -m pytest -q tests/test_operator_control_gui.py` → PASS.
- [ ] **Step 5: Commit** — explicit paths.

---

## Task 10: Docs + final verification

**Files:** Create `docs/operator_control_worker_runner.md`; update `docs/operator_control.md` (Phase 2 → done), `docs/roadmap.md`.

- [ ] **Step 1: Write `docs/operator_control_worker_runner.md`** — runbook covering: the two paths, the three-part autonomous gate, the deterministic guards, CLI usage, the human review/merge workflow (`git -C .worktrees/<id> diff main`; merge by hand), quarantine behavior, and the explicit statement that Phase 2 never merges/pushes/crons.
- [ ] **Step 2: Update `docs/operator_control.md`** "Recommended Phase 2" → "Phase 2 — shipped" with a pointer to the new runbook.
- [ ] **Step 3: Add a roadmap entry** under the operator-control section.
- [ ] **Step 4: Full targeted suite** — `.venv/bin/python -m pytest -q tests/test_operator_worker_runner.py tests/test_operator_protected_paths.py tests/test_operator_control.py tests/test_operator_control_gui.py` → all PASS.
- [ ] **Step 5: Broader regression** — `.venv/bin/python -m pytest -q tests/ -k "gui or operator or dashboard"` → PASS.
- [ ] **Step 6: Commit docs** — explicit paths.

---

## Self-review

**Spec coverage:** hybrid execution (Tasks 4 scaffold + 5 autonomous via gate); all-modes-incl-safe_repair (autonomous path runs whatever the order's mode is — no mode filter; containment via guards); worktree off main (Task 2); never-merge/never-push (runner never calls merge/push; asserted implicitly — runner has no such call); protected-path guard (Tasks 1 + 5); restricted profile (Task 8); test gate (Task 5); single-flight lock (Task 5 via run_lock); manual-trigger CLI (Task 7, no cron); kill-switch + 3-part gate (Task 3); System-tab card (Task 9); docs (Task 10); tests mock claude (Tasks 5–7). All covered.

**Placeholder scan:** none — every code step has real code.

**Type consistency:** `_invoke_claude(worktree, prompt)` and `_run_tests(worktree, tests)` signatures match their monkeypatch sites; `worker_runner.worktree.changed_files` is the patched symbol; `attach_report_path` matches the `attach_prompt_path` shape; statuses use the Phase 1 policy graph (`claimed→running→completed/failed`).

**Note for executor:** add `worker_protected_path_violation` and `report_attached` to `audit_log.EVENT_TYPES`, and `attach_report_path` to `work_orders.__all__`. The autonomous tests monkeypatch `worker_runner.worktree.changed_files` — keep the `from operator_control import worktree` import (module ref), not `from .worktree import changed_files`.
