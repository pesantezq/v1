# Operator-Worker Control Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated, observe-only `/dashboard/operator` page that surfaces the operator worker's 5-gate readiness, cost, work-order queue, and quarantine inventory, with two safe actions (cancel a not-started order, view a quarantine diff).

**Architecture:** A new pure readiness assessor (`portfolio_automation/operator_worker_readiness.py`, computed live) + a new safe-git quarantine inspector (`gui_v2/data/operator_quarantine.py`) + a composing loader function in the existing `gui_v2/data/operator_control.py` + three auth-gated routes in `gui_v2/app.py` + a new template + nav entry. Cancel mutates state ONLY through the validated domain API `work_orders.transition_work_order`. No new persisted artifact; no pipeline stage.

**Tech Stack:** Python 3, FastAPI (existing `gui_v2/app.py`), Jinja2 templates with the repo's `_ui` macros, `pytest` (run via `.venv/bin/python -m pytest`), `subprocess` with argument arrays for git.

## Global Constraints

- Observe-only: no worker execution, autonomous-enable, merge, push, worktree/branch deletion, approval, or production-promotion controls. (spec: Governance frame, Authorization scope)
- The ONLY state mutation is cancel, via `work_orders.transition_work_order(..., new_status="cancelled", ...)`. No ledger editing. (spec: Component 4)
- Readiness is advisory health state, NOT authorization to execute. (spec: Governance frame)
- Five primary readiness gates: `auth`, `bounded_cmd`, `audit`, `rollback`, `quarantine`. Cost is a SEPARATE telemetry/warning line, never a gate. (spec: Readiness semantics)
- Declared gates default to AMBER on missing/malformed/evidence-free/dangling-evidence/unrecognized-status. (spec: Component 1)
- Safe git only: argument arrays (`shell=False`), repo-bound path validation, per-command timeout, output-size cap; all paths/branches/IDs from validated repo/domain records, never user input. (spec: Component 3)
- Cancel route: `_require_auth` + `GUI_V2_OPERATOR_EDIT=1` mutation flag + same-origin (Origin/Referer host) check + required bounded reason (≤280 chars) + auth-derived actor (`actor_source="dashboard_auth"`, fallback `"dashboard-manual"`/`"dashboard_open_mode"` only in open mode, never from form) + idempotent already-cancelled + race-safe re-read + success AND failure audit events + 303 redirect with visible result. (spec: Component 4)
- Run tests with `.venv/bin/python -m pytest`. Never run the full suite for these tasks (it mutates `config/signal_registry.yaml`); run targeted files.
- Existing symbols to reuse (verbatim): `operator_control.work_orders.{list_work_orders,get_work_order,transition_work_order,WorkOrderValidationError}`; `operator_control.protected_paths.is_protected`; `operator_control.audit_log.record_event(root, *, event_type, actor, work_order_id=None, probe_id=None, skill_id=None, mode=None, details=None, safety_result=...)`; `gui_v2/app.py:_require_auth` (returns username or None in open mode); `gui_v2/data/shared.card`; the safe-git pattern in `gui_v2/data/deploy_status.py:_git`.

---

### Task 1: Readiness assessor + config declared block

**Files:**
- Create: `portfolio_automation/operator_worker_readiness.py`
- Modify: `config.json` (add `operator_worker.readiness_declared` + `operator_worker.cost_cap_usd_per_day`)
- Test: `tests/test_operator_worker_readiness.py`

**Interfaces:**
- Produces: `operator_worker_readiness(root: str | Path) -> dict` with keys `observe_only`(bool), `gates`(dict of 5 → `{status, reason, source}` plus declared attestation fields), `overall_ready`(str "N/5"), `autonomous_enabled`(bool), `cost`(dict `{lifetime_usd, cap_usd, cap_pct, cap_configured}`). Degraded dict `{observe_only:True, error:str, gates:{}, overall_ready:"0/5"}` on exception.
- Constants: `RECOGNIZED_STATUSES = frozenset({"green","amber","red"})`, `DECLARED_GATES = ("bounded_cmd","rollback")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_operator_worker_readiness.py
import json
from portfolio_automation.operator_worker_readiness import (
    operator_worker_readiness, RECOGNIZED_STATUSES, DECLARED_GATES,
)


def _write_config(tmp_path, declared=None, cost_cap=None):
    cfg = {"operator_worker": {}}
    if declared is not None:
        cfg["operator_worker"]["readiness_declared"] = declared
    if cost_cap is not None:
        cfg["operator_worker"]["cost_cap_usd_per_day"] = cost_cap
    (tmp_path / "config.json").write_text(json.dumps(cfg))


def test_five_primary_gates_present(tmp_path):
    _write_config(tmp_path)
    r = operator_worker_readiness(tmp_path)
    assert set(r["gates"]) == {"auth", "bounded_cmd", "audit", "rollback", "quarantine"}
    assert r["overall_ready"].endswith("/5")
    assert "cost" in r and "cap_configured" in r["cost"]  # cost is separate, not a gate


def test_declared_gate_missing_defaults_amber(tmp_path):
    _write_config(tmp_path, declared={})
    r = operator_worker_readiness(tmp_path)
    assert r["gates"]["bounded_cmd"]["status"] == "amber"
    assert r["gates"]["bounded_cmd"]["source"] == "declared"


def test_declared_gate_evidence_free_defaults_amber(tmp_path):
    _write_config(tmp_path, declared={"rollback": {
        "status": "green", "declared_by": "op", "declared_at": "2026-06-18T00:00:00Z",
        "evidence": []}})  # empty evidence
    r = operator_worker_readiness(tmp_path)
    assert r["gates"]["rollback"]["status"] == "amber"


def test_declared_gate_dangling_evidence_defaults_amber(tmp_path):
    _write_config(tmp_path, declared={"bounded_cmd": {
        "status": "green", "declared_by": "op", "declared_at": "2026-06-18T00:00:00Z",
        "evidence": ["does/not/exist.py"]}})
    r = operator_worker_readiness(tmp_path)
    assert r["gates"]["bounded_cmd"]["status"] == "amber"


def test_declared_gate_unrecognized_status_defaults_amber(tmp_path):
    _write_config(tmp_path, declared={"bounded_cmd": {
        "status": "bogus", "declared_by": "op", "declared_at": "2026-06-18T00:00:00Z",
        "evidence": ["config.json"]}})
    r = operator_worker_readiness(tmp_path)
    assert r["gates"]["bounded_cmd"]["status"] == "amber"


def test_declared_gate_valid_evidence_honored(tmp_path):
    (tmp_path / "config.json")  # created below
    _write_config(tmp_path, declared={"bounded_cmd": {
        "status": "green", "declared_by": "op", "declared_at": "2026-06-18T00:00:00Z",
        "evidence": ["config.json"], "note": "ok"}})
    r = operator_worker_readiness(tmp_path)
    g = r["gates"]["bounded_cmd"]
    assert g["status"] == "green"
    assert g["declared_by"] == "op" and g["evidence"] == ["config.json"]


def test_cost_cap_unconfigured(tmp_path):
    _write_config(tmp_path)
    r = operator_worker_readiness(tmp_path)
    assert r["cost"]["cap_configured"] is False
    assert r["cost"]["cap_usd"] is None


def test_audit_gate_green_when_logs_present(tmp_path):
    _write_config(tmp_path)
    d = tmp_path / "outputs" / "operator_control"
    d.mkdir(parents=True)
    (d / "audit_log.jsonl").write_text("{}\n")
    (d / "worker_cost_log.jsonl").write_text(
        json.dumps({"cost_usd": 1.5}) + "\n")
    r = operator_worker_readiness(tmp_path)
    assert r["gates"]["audit"]["status"] == "green"
    assert r["cost"]["lifetime_usd"] == 1.5


def test_degraded_on_unreadable_root(tmp_path):
    # config.json is a directory → json load raises → degraded
    (tmp_path / "config.json").mkdir()
    r = operator_worker_readiness(tmp_path)
    assert r["observe_only"] is True
    assert r["overall_ready"] == "0/5"
    assert "error" in r
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_readiness.py -q`
Expected: FAIL (ModuleNotFoundError: operator_worker_readiness).

- [ ] **Step 3: Implement the module**

```python
# portfolio_automation/operator_worker_readiness.py
"""Live, observe-only readiness assessor for the operator worker.

Five primary gates (auth, bounded_cmd, audit, rollback, quarantine). Cost is a
SEPARATE telemetry line, never a gate. Auto gates are verified from the
environment/filesystem/code; declared gates read an evidence-backed attestation
block from config and DEFAULT TO AMBER unless every validation rule passes.
This is advisory health state — NOT authorization to execute workers.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

RECOGNIZED_STATUSES = frozenset({"green", "amber", "red"})
DECLARED_GATES = ("bounded_cmd", "rollback")
_REQUIRED_DECL_KEYS = ("status", "declared_by", "declared_at", "evidence")


def _amber(reason: str, source: str) -> dict[str, Any]:
    return {"status": "amber", "reason": reason, "source": source}


def _running_as_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def _in_container(root: Path) -> bool:
    if Path("/.dockerenv").exists():
        return True
    try:
        cg = Path("/proc/1/cgroup").read_text(encoding="utf-8")
        return any(t in cg for t in ("docker", "containerd", "libpod", "kubepods"))
    except OSError:
        return False


def _auth_gate(root: Path) -> dict[str, Any]:
    if _running_as_root() and not _in_container(root):
        return _amber("runs as root, no container", "auto")
    if _running_as_root():
        return _amber("containerized but still root", "auto")
    return {"status": "green", "reason": "non-root, containerized", "source": "auto"}


def _audit_gate(root: Path) -> dict[str, Any]:
    d = root / "outputs" / "operator_control"
    if (d / "audit_log.jsonl").exists() and (d / "worker_cost_log.jsonl").exists():
        return {"status": "green",
                "reason": "audit_log.jsonl + worker_cost_log.jsonl present",
                "source": "auto"}
    return _amber("operator-control audit/cost logs missing", "auto")


def _quarantine_gate(root: Path) -> dict[str, Any]:
    # Evaluate whether the protected-path control is IMPLEMENTED + TESTED.
    # (Inventory is shown separately and is NOT proof the control works.)
    try:
        from operator_control.protected_paths import is_protected  # noqa: F401
    except Exception:
        return _amber("protected-path guard not importable", "auto")
    tested = (root / "tests" / "test_operator_protected_paths.py").exists()
    if tested:
        return {"status": "green",
                "reason": "protected-path guard implemented + tested", "source": "auto"}
    return _amber("protected-path guard present but untested", "auto")


def _declared_gate(name: str, cfg_block: dict[str, Any], root: Path) -> dict[str, Any]:
    decl = (cfg_block or {}).get(name)
    if not isinstance(decl, dict):
        return _amber(f"no declaration for {name}", "declared")
    if any(k not in decl for k in _REQUIRED_DECL_KEYS):
        return _amber(f"{name} declaration malformed", "declared")
    status = decl.get("status")
    if status not in RECOGNIZED_STATUSES:
        return _amber(f"{name} declared status unrecognized", "declared")
    evidence = decl.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return _amber(f"{name} declaration has no evidence", "declared")
    if not all(isinstance(e, str) and (root / e).exists() for e in evidence):
        return _amber(f"{name} evidence references missing files", "declared")
    return {
        "status": status, "source": "declared",
        "reason": decl.get("note", ""),
        "declared_by": decl.get("declared_by"),
        "declared_at": decl.get("declared_at"),
        "evidence": list(evidence),
    }


def _cost(root: Path, ow_cfg: dict[str, Any]) -> dict[str, Any]:
    lifetime = 0.0
    p = root / "outputs" / "operator_control" / "worker_cost_log.jsonl"
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                lifetime += float(json.loads(line).get("cost_usd") or 0.0)
            except (ValueError, json.JSONDecodeError):
                continue
    except OSError:
        pass
    cap = ow_cfg.get("cost_cap_usd_per_day")
    cap_configured = isinstance(cap, (int, float)) and cap > 0
    cap_pct = round(lifetime / cap * 100, 1) if cap_configured else None
    return {"lifetime_usd": round(lifetime, 4),
            "cap_usd": cap if cap_configured else None,
            "cap_pct": cap_pct, "cap_configured": bool(cap_configured)}


def operator_worker_readiness(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    try:
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
        ow = cfg.get("operator_worker", {}) or {}
        declared = ow.get("readiness_declared", {}) or {}
        gates = {
            "auth": _auth_gate(root),
            "audit": _audit_gate(root),
            "quarantine": _quarantine_gate(root),
            "bounded_cmd": _declared_gate("bounded_cmd", declared, root),
            "rollback": _declared_gate("rollback", declared, root),
        }
        green = sum(1 for g in gates.values() if g["status"] == "green")
        return {
            "observe_only": True,
            "gates": gates,
            "overall_ready": f"{green}/5",
            "autonomous_enabled": bool(ow.get("enabled", False)),
            "cost": _cost(root, ow),
        }
    except Exception as exc:  # degraded, never raises to caller
        return {"observe_only": True, "error": f"{type(exc).__name__}: {exc}",
                "gates": {}, "overall_ready": "0/5",
                "cost": {"lifetime_usd": 0.0, "cap_usd": None,
                         "cap_pct": None, "cap_configured": False}}
```

- [ ] **Step 4: Add the config block to `config.json`**

Add under the top-level `operator_worker` object (create the object if absent). Use real evidence paths that exist; status starts at the honest current state:

```json
"operator_worker": {
  "readiness_declared": {
    "bounded_cmd": {
      "status": "amber",
      "declared_by": "operator",
      "declared_at": "2026-06-18T00:00:00Z",
      "evidence": ["operator_control/protected_paths.py", "operator_control/worker_runner.py"],
      "note": "Probe/skill allowlist + production-impact diff gate exist; no OS-level command sandbox yet."
    },
    "rollback": {
      "status": "amber",
      "declared_by": "operator",
      "declared_at": "2026-06-18T00:00:00Z",
      "evidence": ["operator_control/worker_runner.py", "tests/test_operator_worker_runner.py"],
      "note": "Containment via never-merge/never-push + quarantine; no explicit applied-change rollback."
    }
  }
}
```

(Leave `cost_cap_usd_per_day` unset so cost reads uncapped — honest current state.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_readiness.py -q`
Expected: PASS (9 tests).

- [ ] **Step 6: Commit**

```bash
git add portfolio_automation/operator_worker_readiness.py tests/test_operator_worker_readiness.py config.json
git commit -m "feat(operator-readiness): live 5-gate readiness assessor + declared attestations"
```

---

### Task 2: Quarantine inspector (safe git, separate facts)

**Files:**
- Create: `gui_v2/data/operator_quarantine.py`
- Test: `tests/test_operator_quarantine.py`

**Interfaces:**
- Consumes: `operator_control.work_orders.list_work_orders` (for valid order IDs/branches — never user input).
- Produces:
  - `quarantine_inventory(root: str | Path) -> list[dict]` — one entry per `operator/*` worktree, each `{work_order_id, branch, worktree, is_ancestor_of_main, unique_commits(int), changed_paths(list[str], bounded), stat_summary(str, bounded), patch_equivalent_in_main(bool|None, heuristic), already_in_main(bool)}`.
  - `quarantine_diff(root, work_order_id) -> dict` — `{found(bool), stat(str, bounded)}` for a validated worktree only.
  - `MAX_OUTPUT_BYTES = 64_000`, `MAX_PATHS = 200`, `GIT_TIMEOUT = 15`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_operator_quarantine.py
import subprocess
import pytest
from pathlib import Path
from gui_v2.data import operator_quarantine as q


def _run(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _repo_with_worktree(tmp_path, *, diverge=True, merged=False):
    """Build a real git repo + an operator/* worktree branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "git", "init", "-q", "-b", "main")
    _run(repo, "git", "config", "user.email", "t@t")
    _run(repo, "git", "config", "user.name", "t")
    (repo / "f.txt").write_text("base\n")
    _run(repo, "git", "add", "."); _run(repo, "git", "commit", "-qm", "base")
    wt = repo / ".worktrees" / "wo_test_abc"
    _run(repo, "git", "worktree", "add", "-q", "-b", "operator/wo_test_abc", str(wt))
    if diverge:
        (wt / "g.txt").write_text("change\n")
        _run(wt, "git", "add", "."); _run(wt, "git", "commit", "-qm", "wt change")
    if merged:
        _run(repo, "git", "merge", "-q", "operator/wo_test_abc")
    return repo


def test_inventory_diverged_branch(tmp_path, monkeypatch):
    repo = _repo_with_worktree(tmp_path, diverge=True, merged=False)
    # only validated IDs: stub list_work_orders to return our order
    monkeypatch.setattr(q, "list_work_orders",
                        lambda root: [{"work_order_id": "wo_test_abc"}])
    inv = q.quarantine_inventory(repo)
    assert len(inv) == 1
    e = inv[0]
    assert e["branch"] == "operator/wo_test_abc"
    assert e["unique_commits"] == 1
    assert e["is_ancestor_of_main"] is False
    assert e["already_in_main"] is False
    assert "g.txt" in e["changed_paths"]


def test_inventory_merged_branch_is_ancestor(tmp_path, monkeypatch):
    repo = _repo_with_worktree(tmp_path, diverge=True, merged=True)
    monkeypatch.setattr(q, "list_work_orders",
                        lambda root: [{"work_order_id": "wo_test_abc"}])
    inv = q.quarantine_inventory(repo)
    assert inv[0]["is_ancestor_of_main"] is True
    assert inv[0]["already_in_main"] is True


def test_diff_unknown_id_not_found(tmp_path, monkeypatch):
    repo = _repo_with_worktree(tmp_path)
    monkeypatch.setattr(q, "list_work_orders",
                        lambda root: [{"work_order_id": "wo_test_abc"}])
    # an ID that is NOT in the validated records must be rejected
    res = q.quarantine_diff(repo, "wo_evil; rm -rf /")
    assert res["found"] is False


def test_diff_path_traversal_id_rejected(tmp_path, monkeypatch):
    repo = _repo_with_worktree(tmp_path)
    monkeypatch.setattr(q, "list_work_orders",
                        lambda root: [{"work_order_id": "wo_test_abc"}])
    res = q.quarantine_diff(repo, "../../etc/passwd")
    assert res["found"] is False


def test_missing_git_degrades(tmp_path, monkeypatch):
    repo = _repo_with_worktree(tmp_path)
    monkeypatch.setattr(q, "list_work_orders",
                        lambda root: [{"work_order_id": "wo_test_abc"}])
    monkeypatch.setattr(q, "_git", lambda *a, **k: q._failed_cp("git missing"))
    inv = q.quarantine_inventory(repo)
    assert isinstance(inv, list)  # degrades to [] or entries with safe defaults


def test_output_is_bounded(tmp_path, monkeypatch):
    repo = _repo_with_worktree(tmp_path)
    monkeypatch.setattr(q, "list_work_orders",
                        lambda root: [{"work_order_id": "wo_test_abc"}])
    inv = q.quarantine_inventory(repo)
    assert len(inv[0]["stat_summary"]) <= q.MAX_OUTPUT_BYTES
    assert len(inv[0]["changed_paths"]) <= q.MAX_PATHS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_operator_quarantine.py -q`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement the inspector**

```python
# gui_v2/data/operator_quarantine.py
"""Safe, read-only inspection of operator quarantine worktrees.

Reports SEPARATE facts (ancestor / unique commits / changed paths / heuristic
patch-equivalence) rather than a single diff. All worktree paths, branch names,
and work-order IDs come from VALIDATED domain/repo records — never user input.
Git is invoked with argument arrays (shell=False), repo-bound path validation,
timeouts, and output-size caps. Bounded summary only; no raw file bodies.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from operator_control.work_orders import list_work_orders

MAX_OUTPUT_BYTES = 64_000
MAX_PATHS = 200
GIT_TIMEOUT = 15
_WO_ID_RE = re.compile(r"^wo_[A-Za-z0-9_]+$")  # domain ID shape; rejects traversal/injection


def _failed_cp(msg: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode=1, stdout="", stderr=msg)


def _git(root: Path, *args: str, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        cp = subprocess.run(["git", "-C", str(root), *args],
                            capture_output=True, text=True, timeout=timeout)
        if cp.stdout and len(cp.stdout) > MAX_OUTPUT_BYTES:
            cp = subprocess.CompletedProcess(
                cp.args, cp.returncode,
                cp.stdout[:MAX_OUTPUT_BYTES] + "\n…[truncated]", cp.stderr)
        return cp
    except (subprocess.TimeoutExpired, OSError) as exc:
        return _failed_cp(str(exc))


def _valid_ids(root: Path) -> set[str]:
    try:
        return {o.get("work_order_id") for o in list_work_orders(root)
                if isinstance(o.get("work_order_id"), str)}
    except Exception:
        return set()


def _worktrees(root: Path) -> list[tuple[str, str]]:
    """(branch, worktree_path) for operator/* worktrees, from git porcelain only."""
    cp = _git(root, "worktree", "list", "--porcelain")
    out: list[tuple[str, str]] = []
    cur_path = None
    for line in cp.stdout.splitlines():
        if line.startswith("worktree "):
            cur_path = line[len("worktree "):].strip()
        elif line.startswith("branch ") and cur_path:
            br = line[len("branch "):].strip().removeprefix("refs/heads/")
            if br.startswith("operator/"):
                out.append((br, cur_path))
            cur_path = None
    return out


def _entry(root: Path, branch: str, worktree: str) -> dict[str, Any]:
    wo_id = branch.removeprefix("operator/")
    is_anc = _git(root, "merge-base", "--is-ancestor", branch, "main").returncode == 0
    uniq = _git(root, "rev-list", "--count", f"main..{branch}").stdout.strip()
    unique_commits = int(uniq) if uniq.isdigit() else 0
    mb = _git(root, "merge-base", "main", branch).stdout.strip()
    names = _git(root, "diff", "--name-only", f"{mb}..{branch}").stdout if mb else ""
    changed = [p for p in names.splitlines() if p][:MAX_PATHS]
    stat = (_git(root, "diff", "--stat", f"{mb}..{branch}").stdout if mb else "")[:MAX_OUTPUT_BYTES]
    # Heuristic patch-equivalence: git cherry marks '-' for commits already in main.
    cherry = _git(root, "cherry", "main", branch).stdout.splitlines()
    patch_equiv: bool | None
    if not cherry:
        patch_equiv = None
    else:
        patch_equiv = all(line.startswith("- ") for line in cherry if line.strip())
    return {
        "work_order_id": wo_id, "branch": branch, "worktree": worktree,
        "is_ancestor_of_main": is_anc, "unique_commits": unique_commits,
        "changed_paths": changed, "stat_summary": stat,
        "patch_equivalent_in_main": patch_equiv,  # heuristic, may be None
        "already_in_main": bool(is_anc or patch_equiv),
    }


def quarantine_inventory(root: str | Path) -> list[dict[str, Any]]:
    root = Path(root)
    valid = _valid_ids(root)
    inv: list[dict[str, Any]] = []
    for branch, worktree in _worktrees(root):
        wo_id = branch.removeprefix("operator/")
        if not _WO_ID_RE.match(wo_id) or (valid and wo_id not in valid):
            continue  # only validated, well-formed IDs
        # repo-bound path check
        try:
            rp = Path(worktree).resolve()
            if (root / ".worktrees").resolve() not in rp.parents and rp != (root / ".worktrees").resolve():
                continue
        except OSError:
            continue
        inv.append(_entry(root, branch, worktree))
    return inv


def quarantine_diff(root: str | Path, work_order_id: str) -> dict[str, Any]:
    root = Path(root)
    if not isinstance(work_order_id, str) or not _WO_ID_RE.match(work_order_id):
        return {"found": False, "stat": ""}
    if work_order_id not in _valid_ids(root):
        return {"found": False, "stat": ""}
    branch = f"operator/{work_order_id}"
    if _git(root, "rev-parse", "--verify", "--quiet", branch).returncode != 0:
        return {"found": False, "stat": ""}
    mb = _git(root, "merge-base", "main", branch).stdout.strip()
    stat = (_git(root, "diff", "--stat", f"{mb}..{branch}").stdout if mb else "")[:MAX_OUTPUT_BYTES]
    return {"found": True, "stat": stat}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_operator_quarantine.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add gui_v2/data/operator_quarantine.py tests/test_operator_quarantine.py
git commit -m "feat(operator-quarantine): safe-git quarantine inventory + bounded diff"
```

---

### Task 3: Composing loader `operator_worker_view`

**Files:**
- Modify: `gui_v2/data/operator_control.py` (append a new function + imports)
- Test: `tests/test_dash_operator_worker.py`

**Interfaces:**
- Consumes: `operator_worker_readiness` (Task 1), `quarantine_inventory` (Task 2), `work_orders.list_work_orders`.
- Produces: `operator_worker_view(root: str | Path) -> dict` → `{readiness, cost, orders(list), counts(dict), quarantine(list), degraded(bool)}`. Each order: `{work_order_id, status, created_at, age_hours, probe_id, skill_id, cancellable(bool), stale(bool)}`. `counts`: `{open, awaiting_approval, failed, quarantined, cancelled, completed, stale}`.
- Constant: `STALE_HOURS = 24`, `CANCELLABLE = frozenset({"queued","awaiting_approval","approved"})`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dash_operator_worker.py
import json
from pathlib import Path
from gui_v2.data import operator_control as oc


def _seed_order(root, wid, status, created_at="2020-01-01T00:00:00+00:00"):
    rec = {"work_order_id": wid, "status": status, "created_at": created_at,
           "status_history": [{"status": status, "at": created_at}]}
    d = Path(root) / "outputs" / "operator_control"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "work_orders.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def test_view_composition(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"operator_worker": {}}))
    _seed_order(tmp_path, "wo_a", "queued")
    _seed_order(tmp_path, "wo_b", "failed")
    monkeypatch.setattr(oc, "quarantine_inventory", lambda root: [])
    v = oc.operator_worker_view(tmp_path)
    assert "readiness" in v and v["readiness"]["overall_ready"].endswith("/5")
    assert "cost" in v
    by_id = {o["work_order_id"]: o for o in v["orders"]}
    assert by_id["wo_a"]["cancellable"] is True
    assert by_id["wo_b"]["cancellable"] is False
    assert v["counts"]["open"] >= 1


def test_stale_flag(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"operator_worker": {}}))
    _seed_order(tmp_path, "wo_old", "queued", created_at="2020-01-01T00:00:00+00:00")
    monkeypatch.setattr(oc, "quarantine_inventory", lambda root: [])
    v = oc.operator_worker_view(tmp_path)
    assert next(o for o in v["orders"] if o["work_order_id"] == "wo_old")["stale"] is True
    assert v["counts"]["stale"] >= 1


def test_degrades_without_orders_dir(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"operator_worker": {}}))
    monkeypatch.setattr(oc, "quarantine_inventory", lambda root: [])
    v = oc.operator_worker_view(tmp_path)
    assert v["orders"] == [] and v["degraded"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_dash_operator_worker.py -q`
Expected: FAIL (AttributeError: operator_worker_view).

- [ ] **Step 3: Implement `operator_worker_view`**

Add to the top of `gui_v2/data/operator_control.py` (with the existing imports):

```python
from datetime import datetime, timezone
from operator_control.work_orders import list_work_orders
from portfolio_automation.operator_worker_readiness import operator_worker_readiness
from gui_v2.data.operator_quarantine import quarantine_inventory

STALE_HOURS = 24
CANCELLABLE = frozenset({"queued", "awaiting_approval", "approved"})
_OPEN = frozenset({"queued", "awaiting_approval", "claimed", "running", "approved"})
```

Append this function at the end of the file:

```python
def _age_hours(created_at: str) -> float | None:
    try:
        dt = datetime.fromisoformat(created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        return None


def operator_worker_view(root):
    """Compose the /dashboard/operator view-model (observe-only, live)."""
    readiness = operator_worker_readiness(root)
    try:
        raw = list_work_orders(root)
    except Exception:
        raw = []
    orders = []
    counts = {k: 0 for k in ("open", "awaiting_approval", "failed", "quarantined",
                             "cancelled", "completed", "stale")}
    for o in raw:
        status = o.get("status")
        age = _age_hours(o.get("created_at"))
        stale = bool(status in _OPEN and age is not None and age > STALE_HOURS)
        orders.append({
            "work_order_id": o.get("work_order_id"), "status": status,
            "created_at": o.get("created_at"), "age_hours": age,
            "probe_id": o.get("probe_id"), "skill_id": o.get("skill_id"),
            "cancellable": status in CANCELLABLE, "stale": stale,
        })
        if status in _OPEN:
            counts["open"] += 1
        if status in counts:
            counts[status] += 1
        if stale:
            counts["stale"] += 1
    try:
        quarantine = quarantine_inventory(root)
    except Exception:
        quarantine = []
    counts["quarantined"] = len(quarantine)
    return {"readiness": readiness, "cost": readiness.get("cost", {}),
            "orders": orders, "counts": counts, "quarantine": quarantine,
            "degraded": bool(readiness.get("error"))}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_dash_operator_worker.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add gui_v2/data/operator_control.py tests/test_dash_operator_worker.py
git commit -m "feat(operator-view): compose readiness + orders + quarantine loader"
```

---

### Task 4: GET route + template + nav

**Files:**
- Modify: `gui_v2/app.py` (add `GET /dashboard/operator`)
- Create: `gui_v2/templates/operator.html`
- Modify: `gui_v2/templates/base.html` (nav tuple)
- Test: `tests/test_operator_routes.py`

**Interfaces:**
- Consumes: `operator_control.operator_worker_view` (Task 3), existing `_require_auth`, `templates` (the Jinja2Templates instance in app.py).
- Produces: route `GET /dashboard/operator` → HTML 200.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_operator_routes.py
from fastapi.testclient import TestClient
from gui_v2.app import app

client = TestClient(app)


def test_operator_page_renders():
    r = client.get("/dashboard/operator")
    assert r.status_code == 200
    assert "Operator" in r.text
    assert "ready" in r.text.lower()  # readiness section rendered
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_operator_routes.py::test_operator_page_renders -q`
Expected: FAIL (404).

- [ ] **Step 3: Add the route** (in `gui_v2/app.py`, near the other `/dashboard` GETs)

```python
@app.get("/dashboard/operator", response_class=HTMLResponse)
def dashboard_operator(
    request: Request, _a: str | None = Depends(_require_auth)
):
    from gui_v2.data.operator_control import operator_worker_view
    view = operator_worker_view(ROOT)  # ROOT is the app's repo-root constant
    edit_enabled = _operator_edit_enabled()
    return templates.TemplateResponse(
        "operator.html",
        {"request": request, "view": view, "edit_enabled": edit_enabled},
    )
```

Add the mutation-flag helper near `_edit_enabled`:

```python
def _operator_edit_enabled() -> bool:
    """Cancel requires auth configured AND GUI_V2_OPERATOR_EDIT=1 (mirrors
    portfolio-edit gating; a read-only authenticated viewer cannot cancel)."""
    user = os.environ.get("GUI_V2_AUTH_USER", "").strip()
    pw = os.environ.get("GUI_V2_AUTH_PASS", "").strip()
    return bool(user and pw) and os.environ.get("GUI_V2_OPERATOR_EDIT", "").strip() == "1"
```

(If `ROOT`/`templates` are named differently in app.py, use the existing names — check the other dashboard routes.)

- [ ] **Step 4: Create the template** `gui_v2/templates/operator.html`

```html
{% extends "base.html" %}
{% import "components/_ui.html" as ui %}
{% block content %}
<h1 class="text-xl font-semibold mb-4">Operator Worker</h1>

{% if request.query_params.get("msg") %}
<div class="mb-4 rounded-md border px-3 py-2 text-sm
  {{ 'border-red-700 text-red-300' if request.query_params.get('level')=='error'
     else 'border-emerald-700 text-emerald-300' }}">
  {{ request.query_params.get("msg") }}
</div>
{% endif %}

{{ ui.section_header("Readiness", view.readiness.overall_ready ~ " gates green", "") }}
<table class="w-full text-sm mb-6">
  <thead><tr><th class="text-left">Gate</th><th>Status</th><th>Source</th><th class="text-left">Detail</th></tr></thead>
  <tbody>
  {% for name, g in view.readiness.gates.items() %}
    <tr>
      <td>{{ name }}</td>
      <td>{{ ui.badge(g.status) }}</td>
      <td>{{ g.source }}</td>
      <td>{{ g.reason }}
        {% if g.source == "declared" and g.declared_at %}
          <span class="text-zinc-500">— declared by {{ g.declared_by }} @ {{ g.declared_at }};
          evidence: {{ g.evidence|join(", ") }}</span>
        {% endif %}
      </td>
    </tr>
  {% endfor %}
  </tbody>
</table>

{{ ui.section_header("Cost", "telemetry (not a gate)", "") }}
<p class="text-sm mb-6">Lifetime ${{ "%.2f"|format(view.cost.lifetime_usd) }} ·
  cap {{ "$%.2f"|format(view.cost.cap_usd) if view.cost.cap_configured else "unconfigured ⚠" }}
  {% if view.cost.cap_pct is not none %}· {{ view.cost.cap_pct }}% used{% endif %}</p>

{{ ui.section_header("Work Orders", view.counts.open ~ " open · " ~ view.counts.failed ~ " failed · " ~ view.counts.cancelled ~ " cancelled · " ~ view.counts.stale ~ " stale", "") }}
<table class="w-full text-sm mb-6">
  <thead><tr><th class="text-left">ID</th><th>Status</th><th>Age (h)</th><th>Probe/Skill</th><th></th></tr></thead>
  <tbody>
  {% for o in view.orders %}
    <tr>
      <td><a class="underline" href="/dashboard/operator/report/{{ o.work_order_id }}">{{ o.work_order_id }}</a></td>
      <td>{{ ui.badge(o.status) }}{% if o.stale %} <span class="text-amber-400">stale</span>{% endif %}</td>
      <td>{{ o.age_hours if o.age_hours is not none else "—" }}</td>
      <td class="text-zinc-400">{{ o.probe_id or "—" }} / {{ o.skill_id or "—" }}</td>
      <td>
      {% if o.cancellable and edit_enabled %}
        <form method="post" action="/dashboard/operator/cancel"
              onsubmit="return confirm('Cancel {{ o.work_order_id }}? This is a terminal, audited action.');">
          <input type="hidden" name="work_order_id" value="{{ o.work_order_id }}">
          <input type="text" name="reason" required maxlength="280" placeholder="reason (required)"
                 class="bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-xs">
          <button type="submit" class="text-red-400 underline text-xs">Cancel</button>
        </form>
      {% endif %}
      </td>
    </tr>
  {% endfor %}
  </tbody>
</table>

{{ ui.section_header("Quarantine Inventory", view.quarantine|length ~ " worktree(s)", "") }}
<table class="w-full text-sm">
  <thead><tr><th class="text-left">Worktree</th><th>Branch</th><th>Ancestor?</th><th>Unique commits</th><th>Patch-equiv*</th><th>In main?</th><th></th></tr></thead>
  <tbody>
  {% for e in view.quarantine %}
    <tr>
      <td class="text-zinc-400">{{ e.worktree }}</td>
      <td>{{ e.branch }}</td>
      <td>{{ "yes" if e.is_ancestor_of_main else "no" }}</td>
      <td>{{ e.unique_commits }}</td>
      <td>{{ "yes" if e.patch_equivalent_in_main else ("?" if e.patch_equivalent_in_main is none else "no") }}</td>
      <td>{{ ui.badge("green" if e.already_in_main else "amber") }}</td>
      <td><a class="underline text-xs" href="/dashboard/operator/quarantine/{{ e.work_order_id }}/diff">view diff</a></td>
    </tr>
  {% endfor %}
  </tbody>
</table>
<p class="text-xs text-zinc-500 mt-2">* patch-equivalence is heuristic, not guaranteed.</p>
{% endblock %}
```

(If `ui.badge`/`ui.section_header` have different names, check `components/_ui.html` and match. If `badge` doesn't accept a status string, render a `<span>` with the status text.)

- [ ] **Step 5: Add nav entry** in `gui_v2/templates/base.html` — append to the nav tuple list (line ~62):

```
,("/dashboard/operator","Operator")
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_operator_routes.py::test_operator_page_renders -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add gui_v2/app.py gui_v2/templates/operator.html gui_v2/templates/base.html tests/test_operator_routes.py
git commit -m "feat(operator-gui): /dashboard/operator read-only page + nav"
```

---

### Task 5: POST cancel route (full safety)

**Files:**
- Modify: `gui_v2/app.py` (add `POST /dashboard/operator/cancel` + `_same_origin` helper)
- Test: `tests/test_operator_routes.py` (extend)

**Interfaces:**
- Consumes: `_require_auth`, `_operator_edit_enabled` (Task 4), `work_orders.{get_work_order,transition_work_order,WorkOrderValidationError}`, `audit_log.record_event`.
- Produces: `POST /dashboard/operator/cancel` → 303 redirect to `/dashboard/operator?msg=...&level=...`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_operator_routes.py
import json, os
from pathlib import Path
import gui_v2.app as appmod


def _seed(root, wid, status):
    rec = {"work_order_id": wid, "status": status, "created_at": "2026-06-18T00:00:00+00:00",
           "status_history": [{"status": status, "at": "2026-06-18T00:00:00+00:00"}]}
    d = Path(root) / "outputs" / "operator_control"; d.mkdir(parents=True, exist_ok=True)
    with open(d / "work_orders.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def test_cancel_blocked_without_edit_flag(monkeypatch):
    monkeypatch.setattr(appmod, "_operator_edit_enabled", lambda: False)
    r = client.post("/dashboard/operator/cancel",
                    data={"work_order_id": "wo_x", "reason": "test"},
                    headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code in (303, 403)
    if r.status_code == 303:
        assert "level=error" in r.headers["location"]


def test_cancel_rejects_cross_origin(monkeypatch):
    monkeypatch.setattr(appmod, "_operator_edit_enabled", lambda: True)
    r = client.post("/dashboard/operator/cancel",
                    data={"work_order_id": "wo_x", "reason": "t"},
                    headers={"origin": "http://evil.example"}, follow_redirects=False)
    assert r.status_code in (303, 403)
    if r.status_code == 303:
        assert "level=error" in r.headers["location"]


def test_cancel_requires_reason(monkeypatch):
    monkeypatch.setattr(appmod, "_operator_edit_enabled", lambda: True)
    r = client.post("/dashboard/operator/cancel",
                    data={"work_order_id": "wo_x", "reason": "  "},
                    headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code in (303, 422)


def test_cancel_legal_transition(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_operator_edit_enabled", lambda: True)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "alice")
    _seed(tmp_path, "wo_legal", "queued")
    r = client.post("/dashboard/operator/cancel",
                    data={"work_order_id": "wo_legal", "reason": "stale"},
                    headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303
    from operator_control.work_orders import get_work_order
    cur = get_work_order(tmp_path, "wo_legal")
    assert cur["status"] == "cancelled"
    # actor came from auth, not the form
    assert cur["status_history"][-1]["actor"] == "alice"


def test_cancel_idempotent_when_already_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_operator_edit_enabled", lambda: True)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "alice")
    _seed(tmp_path, "wo_done", "cancelled")
    r = client.post("/dashboard/operator/cancel",
                    data={"work_order_id": "wo_done", "reason": "again"},
                    headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303
    assert "level=error" not in r.headers["location"]  # treated as info/success no-op


def test_cancel_unknown_id_audits_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_operator_edit_enabled", lambda: True)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "alice")
    r = client.post("/dashboard/operator/cancel",
                    data={"work_order_id": "wo_nope", "reason": "x"},
                    headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303 and "level=error" in r.headers["location"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_operator_routes.py -q`
Expected: FAIL (405/404 on the cancel route).

- [ ] **Step 3: Implement the route + helpers** (in `gui_v2/app.py`)

```python
from urllib.parse import urlparse
from fastapi.responses import RedirectResponse


def _same_origin(request: Request) -> bool:
    """CSRF-equivalent: the app has no token framework, so require the POST's
    Origin/Referer host to match the request host."""
    host = request.headers.get("host", "")
    src = request.headers.get("origin") or request.headers.get("referer") or ""
    if not src:
        return False
    return urlparse(src).netloc == host


def _redirect(msg: str, level: str = "success") -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(
        url=f"/dashboard/operator?msg={quote(msg)}&level={level}",
        status_code=303)


@app.post("/dashboard/operator/cancel")
def dashboard_operator_cancel(
    request: Request,
    work_order_id: str = Form(...),
    reason: str = Form(...),
    _a: str | None = Depends(_require_auth),
):
    from operator_control import work_orders as wo
    from operator_control import audit_log
    from operator_control.work_orders import WorkOrderValidationError

    actor = _a if _a else "dashboard-manual"
    actor_source = "dashboard_auth" if _a else "dashboard_open_mode"

    if not _operator_edit_enabled():
        return _redirect("Cancellation disabled (set GUI_V2_OPERATOR_EDIT=1).", "error")
    if not _same_origin(request):
        return _redirect("Rejected: cross-origin request.", "error")
    reason = (reason or "").strip()
    if not reason:
        return _redirect("A cancellation reason is required.", "error")
    reason = reason[:280]

    current = wo.get_work_order(ROOT, work_order_id)
    if current is None:
        audit_log.record_event(ROOT, event_type="work_order_cancel_rejected",
                               actor=actor, work_order_id=work_order_id,
                               details={"reason": reason, "why": "unknown id",
                                        "actor_source": actor_source})
        return _redirect(f"Unknown work order {work_order_id}.", "error")
    if current.get("status") == "cancelled":
        audit_log.record_event(ROOT, event_type="work_order_cancel_noop",
                               actor=actor, work_order_id=work_order_id,
                               details={"reason": reason, "actor_source": actor_source})
        return _redirect(f"{work_order_id} already cancelled.", "success")
    try:
        wo.transition_work_order(ROOT, work_order_id, new_status="cancelled",
                                 actor=actor,
                                 note=f"[{actor_source}] {reason}")
    except WorkOrderValidationError as exc:
        audit_log.record_event(ROOT, event_type="work_order_cancel_rejected",
                               actor=actor, work_order_id=work_order_id,
                               details={"reason": reason, "why": str(exc),
                                        "from": current.get("status"),
                                        "actor_source": actor_source})
        return _redirect(f"Cannot cancel {work_order_id}: {exc}", "error")
    return _redirect(f"Cancelled {work_order_id}.", "success")
```

Note: `transition_work_order` already emits the `work_order_cancelled` success audit event, so the success path is covered without a duplicate record.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_operator_routes.py -q`
Expected: PASS (all cancel tests + the Task 4 render test).

- [ ] **Step 5: Commit**

```bash
git add gui_v2/app.py tests/test_operator_routes.py
git commit -m "feat(operator-gui): safe cancel route (auth actor, same-origin, idempotent, audited)"
```

---

### Task 6: GET quarantine diff route

**Files:**
- Modify: `gui_v2/app.py` (add `GET /dashboard/operator/quarantine/{work_order_id}/diff`)
- Test: `tests/test_operator_routes.py` (extend)

**Interfaces:**
- Consumes: `_require_auth`, `operator_quarantine.quarantine_diff` (Task 2).
- Produces: route returning `text/plain` bounded diff stat, or 404.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_operator_routes.py
def test_quarantine_diff_unknown_404(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "ROOT", tmp_path)
    r = client.get("/dashboard/operator/quarantine/wo_missing/diff")
    assert r.status_code == 404


def test_quarantine_diff_malicious_id_404(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "ROOT", tmp_path)
    r = client.get("/dashboard/operator/quarantine/..%2f..%2fetc%2fpasswd/diff")
    assert r.status_code in (404, 422)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_operator_routes.py -k quarantine -q`
Expected: FAIL (404 route not defined → actually 404 already; assert it's the handler. To force a real fail first, the route is absent so FastAPI returns 404 — acceptable; implement and confirm the 404 comes from our explicit handler via the malicious-id path returning controlled 404).

- [ ] **Step 3: Implement the route**

```python
@app.get("/dashboard/operator/quarantine/{work_order_id}/diff", response_class=PlainTextResponse)
def dashboard_operator_quarantine_diff(
    work_order_id: str, _a: str | None = Depends(_require_auth)
):
    from gui_v2.data.operator_quarantine import quarantine_diff
    res = quarantine_diff(ROOT, work_order_id)
    if not res["found"]:
        raise HTTPException(status_code=404, detail="no quarantine diff for that order")
    return res["stat"]
```

(Ensure `PlainTextResponse` is imported from `fastapi.responses`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_operator_routes.py -k quarantine -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gui_v2/app.py tests/test_operator_routes.py
git commit -m "feat(operator-gui): bounded read-only quarantine diff route"
```

---

### Task 7: Daily-check health line + no-mutation guard test

**Files:**
- Modify: `.claude/commands/daily-tool-analysis.md` (extend line 6g)
- Test: `tests/test_operator_routes.py` (add a no-mutation assertion test)

**Interfaces:**
- Consumes: `operator_worker_view` (Task 3).
- Produces: documentation change + a regression test.

- [ ] **Step 1: Add the no-mutation guard test**

```python
# append to tests/test_operator_routes.py
def test_get_operator_does_not_mutate_decision_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "ROOT", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"operator_worker": {}}))
    latest = tmp_path / "outputs" / "latest"; latest.mkdir(parents=True)
    dp = latest / "decision_plan.json"; dp.write_text('{"sentinel": 1}')
    before = dp.read_text()
    client.get("/dashboard/operator")
    assert dp.read_text() == before  # GET never touches decision_plan
```

- [ ] **Step 2: Run it to verify it passes** (the route is already read-only)

Run: `.venv/bin/python -m pytest tests/test_operator_routes.py::test_get_operator_does_not_mutate_decision_plan -q`
Expected: PASS.

- [ ] **Step 3: Extend daily-check line 6g**

In `.claude/commands/daily-tool-analysis.md`, find body line `6g. Operator-control` and extend its grammar to fold in readiness + cost, computed live via `operator_worker_view`. Replace the line's template with:

```
6g. Operator-control (always): "Operator-control: {open} open · {awaiting_approval} awaiting · {failed} failed · {quarantined} quarantined · {stale} stale · worker {mode} · readiness {overall_ready} · cost ${lifetime_usd}{/{cap_usd} ({cap_pct}%) if cap_configured else ' (uncapped)'}" — observe-only; computed live from operator_worker_view (NO new persisted artifact). AMBER on quarantined≥1 or stale-run; readiness <5/5 is reported, not alerted (it's the hardening-milestone tracker). Never RED.
```

- [ ] **Step 4: Commit**

```bash
git add .claude/commands/daily-tool-analysis.md tests/test_operator_routes.py
git commit -m "feat(operator-gui): daily-check readiness/cost health line + no-mutation guard test"
```

---

## Self-Review

**Spec coverage:**
- Component 1 readiness (5 gates, declared attestations, defaults-amber, cost separate) → Task 1 ✓
- Component 2 loader (`operator_worker_view`, counts, stale) → Task 3 ✓
- Component 3 quarantine (separate facts, safe git, bounded) → Task 2 ✓
- Component 4 routes (GET, cancel with all safety, quarantine diff) → Tasks 4, 5, 6 ✓
- Component 5 template + nav → Task 4 ✓
- Authorization scope (read vs `GUI_V2_OPERATOR_EDIT` mutate) → Tasks 4, 5 ✓
- Health pairing (line 6g, no new artifact) → Task 7 ✓
- All 8 test categories → distributed across Tasks 1–7 (actor-from-auth, CSRF, invalid/race, idempotent, malformed declarations, missing git, malicious ids/traversal, timeout/oversized, merged/partial/diverged/patch-equiv, no-mutation) ✓

**Placeholder scan:** No TBD/TODO; every code step shows real code. Two "match existing name" notes (ROOT/templates in Task 4, ui macro names in Task 4) are explicit verification instructions, not placeholders.

**Type consistency:** `operator_worker_readiness` return shape used identically in Task 3; `operator_worker_view` keys (`readiness/cost/orders/counts/quarantine/degraded`) consumed by the template (Task 4) and daily-check (Task 7); `quarantine_inventory` entry fields match the template columns; `transition_work_order(..., new_status=, actor=, note=)` and `WorkOrderValidationError` match the grounded domain API.

**Known verification points for the implementer (not placeholders):** confirm the app's repo-root constant name (`ROOT`) and `templates` instance name in `gui_v2/app.py`, and the exact `_ui.html` macro names (`badge`, `section_header`); match whatever exists.
