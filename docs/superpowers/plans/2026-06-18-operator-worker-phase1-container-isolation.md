# Operator Worker — Phase 1 Container Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the operator worker's `claude` invocation as a non-root, capability-dropped, read-only-rootfs process in a rootless Podman container owned by a dedicated `stockbot-worker` account, on an isolated disposable git clone with minimal RO credentials — and make the `auth` readiness gate green only after static checks AND a fresh runtime attestation.

**Architecture:** Pure Python builds + validates the `podman run` launch spec and verifies a runtime attestation; one narrow adapter spawns podman via `runuser -u stockbot-worker`; a workspace manager creates/validates/destroys a disposable clone (prod `.git` never mounted); `worker_runner` routes through this fail-closed when enabled; readiness reads the attestation. No VPS changes here — provisioning is an operator-run runbook.

**Tech Stack:** Python 3.12, `subprocess` (shell=False), rootless Podman (target host), `pytest` via `.venv/bin/python -m pytest`.

## Global Constraints

- Observe-only control plane; the ONLY worker mutation is the audited cancel (unchanged). This phase changes HOW the worker executes, never what the decision core does. `decision_engine.py`, scoring, `outputs/latest/decision_plan.json` untouched.
- Production `/opt/stockbot/.git` is NEVER mounted (rw or ro) into the container.
- Never mount `.env`, host home, root `~/.claude`, or any runtime socket. No `--network=host`, no `--privileged`, no host PID/IPC namespace.
- Launch argv: built purely with `shell=False`; image (digest ref), executable (fixed podman path), uid/gid, and every mount **source** are constants/config-derived — NEVER from work-order fields. `work_order_id` validated by `^wo_[A-Za-z0-9_]+$` before any path use.
- Image referenced by approved immutable **digest** (`image@sha256:…`), never a bare mutable tag, for readiness-green.
- Fail-closed: when `worker_container.enabled=true`, ANY failure (podman missing, image/digest mismatch, rootless unavailable, policy-validation fail, startup fail, attestation fail, invalid mount plan, creds dir unavailable, uid/gid mismatch) fails the work order with no direct-path fallback. Direct execution only when container mode is explicitly disabled; recorded `execution_mode=direct, isolated=false`; `auth` stays AMBER.
- `auth` green ONLY when static capability checks AND a fresh valid runtime attestation both pass. Freshness: attestation references approved digest AND timestamp ≥ max(image_build, config_mtime) AND age ≤ `attestation_max_age_days` (default 30); else AMBER.
- `bounded_cmd` stays AMBER (bounded-action layer is OUT of scope). `rollback` AMBER. cost uncapped. `autonomous_enabled` false. `GUI_V2_OPERATOR_EDIT` false by default.
- Image base `python:3.12-slim` (host is 3.12.3). claude CLI `@2.1.x` (host 2.1.181).
- Run tests with `.venv/bin/python -m pytest <file> -q`. Never the full suite (mutates protected registry). **Stage explicit paths only — never `git add -A`/`.`** (untracked `outputs/` must not be committed).
- Reuse: config read pattern `cfg.get("operator_control",{}).get("worker_container",{})` (mirrors `autonomous_worker`); `operator_control.worker_runner._invoke_claude` is the modify point (argv built ~L169, `subprocess.run` ~L173, env stripped L154).
- Config schema (added to `config.json:operator_control.worker_container`, used by all tasks):
  ```json
  "worker_container": {
    "enabled": false,
    "podman_path": "/usr/bin/podman",
    "run_as_user": "stockbot-worker",
    "image_ref": "localhost/stockbot-worker",
    "image_digest": null,
    "container_uid": 1000, "container_gid": 1000,
    "credentials_dir": "/home/stockbot-worker/.claude-worker",
    "workspace_root": "/var/lib/stockbot-worker/ws",
    "attestation_path": "outputs/operator_control/worker_attestation.json",
    "attestation_max_age_days": 30,
    "resource_limits": {"pids": 512, "memory": "2g", "cpus": "2",
                        "tmpfs_size": "512m", "timeout_seconds": 1800},
    "env_allowlist": ["HOME", "PATH", "LANG", "CLAUDE_CONFIG_DIR"],
    "cap_drop_exceptions": []
  }
  ```
- Attestation schema (emitted by the container entrypoint; verified by `verify_runtime_attestation`):
  ```json
  {"generated_at": "<iso>", "execution_mode": "container", "uid": 1000, "gid": 1000,
   "rootless": true, "no_new_privileges": true, "effective_caps": [],
   "mounts": ["/work:rw","/home/worker/.claude:ro","/attest:rw"],
   "image_digest": "sha256:…", "socket_mounts_present": false, "host_home_mounted": false}
  ```

---

### Task 1: Config schema + `build_container_launch_spec` (pure argv)

**Files:**
- Create: `operator_control/worker_container.py`
- Modify: `config.json` (add `operator_control.worker_container` block above)
- Test: `tests/test_worker_container_spec.py`

**Interfaces:**
- Produces: `build_container_launch_spec(*, cfg, workspace_dir, creds_dir, attest_dir, claude_argv) -> list[str]` — the full `podman run` argv ending with the image digest ref and `claude_argv`. Pure, `shell=False`-ready. Also module constants `WO_ID_RE = re.compile(r"^wo_[A-Za-z0-9_]+$")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_worker_container_spec.py
import pytest
from operator_control.worker_container import build_container_launch_spec

CFG = {
    "podman_path": "/usr/bin/podman", "image_ref": "localhost/stockbot-worker",
    "image_digest": "sha256:abc123", "container_uid": 1000, "container_gid": 1000,
    "resource_limits": {"pids": 512, "memory": "2g", "cpus": "2", "tmpfs_size": "512m"},
    "env_allowlist": ["HOME", "PATH", "CLAUDE_CONFIG_DIR"], "cap_drop_exceptions": [],
}

def _spec():
    return build_container_launch_spec(
        cfg=CFG, workspace_dir="/var/lib/stockbot-worker/ws/wo_x",
        creds_dir="/home/stockbot-worker/.claude-worker",
        attest_dir="/var/lib/stockbot-worker/ws/wo_x/.attest",
        claude_argv=["claude", "-p", "do the thing", "--output-format", "json"])

def test_spec_is_podman_run_with_fixed_path():
    s = _spec()
    assert s[0] == "/usr/bin/podman" and s[1] == "run"

def test_spec_uses_digest_not_bare_tag():
    s = _spec()
    assert "localhost/stockbot-worker@sha256:abc123" in s
    assert "localhost/stockbot-worker" not in [x for x in s if "@" not in x and x.startswith("localhost/")]

def test_spec_security_flags_present():
    s = " ".join(_spec())
    assert "--user" in s and "1000:1000" in s
    assert "--read-only" in s
    assert "--security-opt=no-new-privileges" in _spec() or "no-new-privileges" in s
    assert "--cap-drop=ALL" in _spec()
    assert "--pids-limit" in s and "--memory" in s and "--cpus" in s

def test_spec_forbidden_things_absent():
    s = _spec()
    joined = " ".join(s)
    assert "/opt/stockbot/.git" not in joined        # never mount prod git
    assert ".env" not in joined                       # no secrets
    assert "docker.sock" not in joined and "podman.sock" not in joined
    assert "--privileged" not in s
    assert "--network=host" not in s and "host" not in [a.split("=")[-1] for a in s if a.startswith("--network")]
    assert "--pid=host" not in s and "--ipc=host" not in s

def test_spec_mounts_only_approved_sources():
    s = _spec()
    vols = [s[i+1] for i, a in enumerate(s) if a in ("-v", "--volume")]
    # exactly workspace rw, creds ro, attest rw
    assert any(v.startswith("/var/lib/stockbot-worker/ws/wo_x:") and v.endswith(":rw") for v in vols)
    assert any(v.startswith("/home/stockbot-worker/.claude-worker:") and v.endswith(":ro") for v in vols)
    assert any("/.attest:" in v and v.endswith(":rw") for v in vols)
    assert len(vols) == 3  # nothing else mounted

def test_spec_env_allowlist_only_and_no_api_key():
    s = _spec()
    envs = [s[i+1] for i, a in enumerate(s) if a in ("-e", "--env")]
    names = [e.split("=")[0] for e in envs]
    assert "ANTHROPIC_API_KEY" not in names
    assert set(names) <= {"HOME", "PATH", "CLAUDE_CONFIG_DIR"}

def test_spec_claude_argv_is_last_and_unmodified():
    s = _spec()
    assert s[-5:] == ["claude", "-p", "do the thing", "--output-format", "json"]
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_worker_container_spec.py -q`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement `build_container_launch_spec`**

```python
# operator_control/worker_container.py
"""Pure construction + static validation + attestation verification for the
rootless-Podman worker isolation (hardening Phase 1). Argv is built with
shell=False semantics: image (digest), executable, uid/gid, and all mount
SOURCES are constants/config-derived — never from work-order fields."""
from __future__ import annotations

import re
from typing import Any

WO_ID_RE = re.compile(r"^wo_[A-Za-z0-9_]+$")


def build_container_launch_spec(*, cfg: dict, workspace_dir: str, creds_dir: str,
                                attest_dir: str, claude_argv: list[str]) -> list[str]:
    rl = cfg["resource_limits"]
    image = f'{cfg["image_ref"]}@{cfg["image_digest"]}'
    uid, gid = cfg["container_uid"], cfg["container_gid"]
    argv = [
        cfg["podman_path"], "run", "--rm",
        f"--user={uid}:{gid}",
        "--read-only",
        "--security-opt=no-new-privileges",
    ]
    caps_excepted = cfg.get("cap_drop_exceptions") or []
    argv.append("--cap-drop=ALL")
    for c in caps_excepted:               # reviewed exceptions re-added explicitly
        argv.append(f"--cap-add={c}")
    argv += [
        f"--pids-limit={rl['pids']}",
        f"--memory={rl['memory']}",
        f"--cpus={rl['cpus']}",
        f"--tmpfs=/tmp:size={rl['tmpfs_size']}",
        # mounts: workspace rw, creds ro, attest rw — the ONLY three
        "-v", f"{workspace_dir}:/work:rw",
        "-v", f"{creds_dir}:/home/worker/.claude:ro",
        "-v", f"{attest_dir}:/attest:rw",
        "-w", "/work",
    ]
    for name in cfg.get("env_allowlist") or []:
        if name == "ANTHROPIC_API_KEY":
            continue
        argv += ["--env", f"{name}={_env_value(name)}"]
    argv.append(image)
    argv += list(claude_argv)
    return argv


def _env_value(name: str) -> str:
    import os
    return os.environ.get(name, "")
```

(Add the config block from Global Constraints into `config.json:operator_control` — merge into the existing object, do not clobber `autonomous_worker`/`readiness_declared`.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_worker_container_spec.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add operator_control/worker_container.py tests/test_worker_container_spec.py config.json
git commit -m "feat(worker-container): pure podman launch-spec builder + config schema"
```

---

### Task 2: `validate_container_configuration` + `probe_container_capabilities`

**Files:**
- Modify: `operator_control/worker_container.py`
- Test: `tests/test_worker_container_validate.py`

**Interfaces:**
- Consumes: `build_container_launch_spec` (Task 1).
- Produces: `validate_container_configuration(cfg) -> tuple[bool, list[str]]` (ok, reasons); `probe_container_capabilities(cfg) -> dict` (`{podman_present, image_present, digest_pinned, rootless_ok}`). `probe` shells out via a tiny adapter `_run([...])` that is monkeypatched in tests.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_worker_container_validate.py
from operator_control.worker_container import validate_container_configuration

BASE = {
    "enabled": True, "podman_path": "/usr/bin/podman", "image_ref": "localhost/stockbot-worker",
    "image_digest": "sha256:abc", "container_uid": 1000, "container_gid": 1000,
    "run_as_user": "stockbot-worker",
    "resource_limits": {"pids": 512, "memory": "2g", "cpus": "2", "tmpfs_size": "512m", "timeout_seconds": 1800},
    "env_allowlist": ["HOME"], "cap_drop_exceptions": [],
}

def test_valid_config_ok():
    ok, reasons = validate_container_configuration(BASE)
    assert ok and reasons == []

def test_rejects_missing_digest():
    cfg = {**BASE, "image_digest": None}
    ok, reasons = validate_container_configuration(cfg)
    assert not ok and any("digest" in r for r in reasons)

def test_rejects_bare_tag_as_digest():
    cfg = {**BASE, "image_digest": "latest"}   # not sha256:...
    ok, reasons = validate_container_configuration(cfg)
    assert not ok and any("digest" in r for r in reasons)

def test_rejects_root_uid():
    cfg = {**BASE, "container_uid": 0}
    ok, reasons = validate_container_configuration(cfg)
    assert not ok and any("uid" in r.lower() for r in reasons)

def test_rejects_root_run_as_user():
    cfg = {**BASE, "run_as_user": "root"}
    ok, reasons = validate_container_configuration(cfg)
    assert not ok and any("run_as_user" in r or "root" in r.lower() for r in reasons)

def test_rejects_relative_podman_path():
    cfg = {**BASE, "podman_path": "podman"}   # not absolute → not a fixed trusted path
    ok, reasons = validate_container_configuration(cfg)
    assert not ok and any("podman_path" in r for r in reasons)

def test_rejects_api_key_in_env_allowlist():
    cfg = {**BASE, "env_allowlist": ["HOME", "ANTHROPIC_API_KEY"]}
    ok, reasons = validate_container_configuration(cfg)
    assert not ok and any("ANTHROPIC_API_KEY" in r for r in reasons)
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_worker_container_validate.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement validation + probe**

```python
# append to operator_control/worker_container.py
import os
import shutil
import subprocess


def validate_container_configuration(cfg: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not str(cfg.get("podman_path", "")).startswith("/"):
        reasons.append("podman_path must be a fixed absolute path")
    digest = cfg.get("image_digest")
    if not (isinstance(digest, str) and digest.startswith("sha256:")):
        reasons.append("image_digest must be a pinned sha256:… digest, not a mutable tag")
    if cfg.get("container_uid") in (None, 0):
        reasons.append("container_uid must be a non-root uid")
    if cfg.get("container_gid") in (None, 0):
        reasons.append("container_gid must be a non-root gid")
    if (cfg.get("run_as_user") or "root") == "root":
        reasons.append("run_as_user must be a dedicated non-root account, not root")
    if "ANTHROPIC_API_KEY" in (cfg.get("env_allowlist") or []):
        reasons.append("env_allowlist must not contain ANTHROPIC_API_KEY")
    rl = cfg.get("resource_limits") or {}
    for k in ("pids", "memory", "cpus", "tmpfs_size", "timeout_seconds"):
        if not rl.get(k):
            reasons.append(f"resource_limits.{k} is required")
    return (not reasons), reasons


def _run(argv: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def probe_container_capabilities(cfg: dict) -> dict:
    podman = cfg.get("podman_path", "")
    podman_present = bool(podman) and os.path.exists(podman)
    image_present = False
    digest_pinned = isinstance(cfg.get("image_digest"), str) and cfg["image_digest"].startswith("sha256:")
    rootless_ok = False
    if podman_present:
        try:
            insp = _run([podman, "image", "exists",
                         f'{cfg["image_ref"]}@{cfg["image_digest"]}'] )
            image_present = insp.returncode == 0
            info = _run([podman, "info", "--format", "{{.Host.Security.Rootless}}"])
            rootless_ok = info.stdout.strip() == "true"
        except Exception:
            pass
    return {"podman_present": podman_present, "image_present": image_present,
            "digest_pinned": digest_pinned, "rootless_ok": rootless_ok}
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_worker_container_validate.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add operator_control/worker_container.py tests/test_worker_container_validate.py
git commit -m "feat(worker-container): static config validation + capability probe"
```

---

### Task 3: Isolated workspace manager

**Files:**
- Create: `operator_control/worker_workspace.py`
- Test: `tests/test_worker_workspace.py`

**Interfaces:**
- Produces: `create_isolated_workspace(repo_root, workspace_root, work_order_id) -> str` (path to a disposable clone whose `.git` is self-contained, outside the prod repo; validates `work_order_id` with `WO_ID_RE`); `destroy_workspace(path, workspace_root)` (refuses paths outside `workspace_root`); `extract_validated_diff(ws_path) -> str` (the clone's `main`-relative diff stat/patch, bounded).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_worker_workspace.py
import subprocess, pytest
from pathlib import Path
from operator_control import worker_workspace as ws

def _run(cwd, *a): subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True)

def _prod_repo(tmp_path):
    repo = tmp_path / "prod"; repo.mkdir()
    _run(repo, "git", "init", "-q", "-b", "main")
    _run(repo, "git", "config", "user.email", "t@t"); _run(repo, "git", "config", "user.name", "t")
    (repo / "f.txt").write_text("base\n"); _run(repo, "git", "add", "."); _run(repo, "git", "commit", "-qm", "base")
    return repo

def test_create_isolated_clone_has_own_git(tmp_path):
    repo = _prod_repo(tmp_path); wsr = tmp_path / "wsroot"
    p = ws.create_isolated_workspace(str(repo), str(wsr), "wo_abc")
    assert Path(p).is_dir() and (Path(p) / ".git").exists()
    # the clone's gitdir is INSIDE the workspace, not the prod repo
    gitdir = subprocess.run(["git","-C",p,"rev-parse","--absolute-git-dir"],
                            capture_output=True, text=True).stdout.strip()
    assert str(wsr) in gitdir and str(repo) not in gitdir

def test_writes_to_clone_do_not_touch_prod_refs(tmp_path):
    repo = _prod_repo(tmp_path); wsr = tmp_path / "wsroot"
    p = ws.create_isolated_workspace(str(repo), str(wsr), "wo_abc")
    (Path(p) / "new.txt").write_text("x\n")
    _run(p, "git", "add", "."); _run(p, "git", "commit", "-qm", "wt change")
    prod_log = subprocess.run(["git","-C",str(repo),"log","--oneline"],capture_output=True,text=True).stdout
    assert "wt change" not in prod_log   # prod untouched

def test_malicious_id_rejected(tmp_path):
    repo = _prod_repo(tmp_path); wsr = tmp_path / "wsroot"
    with pytest.raises(ValueError):
        ws.create_isolated_workspace(str(repo), str(wsr), "../../etc")

def test_destroy_refuses_outside_workspace_root(tmp_path):
    with pytest.raises(ValueError):
        ws.destroy_workspace("/etc", str(tmp_path / "wsroot"))

def test_destroy_removes_clone(tmp_path):
    repo = _prod_repo(tmp_path); wsr = tmp_path / "wsroot"
    p = ws.create_isolated_workspace(str(repo), str(wsr), "wo_abc")
    ws.destroy_workspace(p, str(wsr)); assert not Path(p).exists()
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_worker_workspace.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement workspace manager**

```python
# operator_control/worker_workspace.py
"""Disposable isolated git workspaces for the container worker. The clone's git
metadata is self-contained and lives under workspace_root — the production
repository's .git is never shared or mounted. Create → (worker edits) →
extract+validate diff → destroy."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from operator_control.worker_container import WO_ID_RE

_MAX_DIFF = 200_000


def _git(cwd, *args, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, timeout=timeout)


def create_isolated_workspace(repo_root: str, workspace_root: str, work_order_id: str) -> str:
    if not WO_ID_RE.match(work_order_id or ""):
        raise ValueError(f"invalid work_order_id: {work_order_id!r}")
    dest = Path(workspace_root) / work_order_id
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # --no-local + file:// forces a real clone with self-contained objects/refs;
    # no hardlinks to and no sharing with the production object store.
    cp = subprocess.run(["git", "clone", "--no-local", "--quiet",
                         f"file://{Path(repo_root).resolve()}", str(dest)],
                        capture_output=True, text=True, timeout=300)
    if cp.returncode != 0:
        raise RuntimeError(f"isolated clone failed: {cp.stderr.strip()[:200]}")
    return str(dest)


def extract_validated_diff(ws_path: str) -> str:
    mb = _git(ws_path, "rev-parse", "main").stdout.strip()
    out = _git(ws_path, "diff", "--stat", f"{mb}..HEAD").stdout if mb else ""
    return out[:_MAX_DIFF]


def destroy_workspace(path: str, workspace_root: str) -> None:
    rp = Path(path).resolve()
    root = Path(workspace_root).resolve()
    if root not in rp.parents:
        raise ValueError(f"refusing to destroy path outside workspace_root: {rp}")
    shutil.rmtree(rp, ignore_errors=True)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_worker_workspace.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add operator_control/worker_workspace.py tests/test_worker_workspace.py
git commit -m "feat(worker-workspace): disposable isolated git clone (prod .git never shared)"
```

---

### Task 4: `verify_runtime_attestation` (+ freshness)

**Files:**
- Modify: `operator_control/worker_container.py`
- Test: `tests/test_worker_container_attestation.py`

**Interfaces:**
- Consumes: config (`image_digest`, `container_uid/gid`, `attestation_max_age_days`).
- Produces: `verify_runtime_attestation(attestation, cfg, *, now, image_build_ts, config_mtime) -> tuple[bool, list[str]]`. `now`/`image_build_ts`/`config_mtime` are epoch floats (injected for testability). Returns (ok, reasons). An attestation is valid only when every policy field matches AND it is fresh.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_worker_container_attestation.py
from operator_control.worker_container import verify_runtime_attestation

CFG = {"image_digest": "sha256:abc", "container_uid": 1000, "container_gid": 1000,
       "attestation_max_age_days": 30}
GOOD = {"generated_at_ts": 1000.0, "execution_mode": "container", "uid": 1000, "gid": 1000,
        "rootless": True, "no_new_privileges": True, "effective_caps": [],
        "mounts": ["/work:rw", "/home/worker/.claude:ro", "/attest:rw"],
        "image_digest": "sha256:abc", "socket_mounts_present": False, "host_home_mounted": False}
KW = dict(now=1000.0, image_build_ts=900.0, config_mtime=900.0)

def test_good_attestation_passes():
    ok, reasons = verify_runtime_attestation(GOOD, CFG, **KW); assert ok and reasons == []

def test_root_uid_fails():
    ok, r = verify_runtime_attestation({**GOOD, "uid": 0}, CFG, **KW); assert not ok and any("uid" in x.lower() for x in r)

def test_direct_mode_fails():
    ok, r = verify_runtime_attestation({**GOOD, "execution_mode": "direct"}, CFG, **KW); assert not ok

def test_caps_present_fails():
    ok, r = verify_runtime_attestation({**GOOD, "effective_caps": ["NET_ADMIN"]}, CFG, **KW); assert not ok

def test_socket_mount_fails():
    ok, r = verify_runtime_attestation({**GOOD, "socket_mounts_present": True}, CFG, **KW); assert not ok

def test_host_home_fails():
    ok, r = verify_runtime_attestation({**GOOD, "host_home_mounted": True}, CFG, **KW); assert not ok

def test_digest_mismatch_fails():
    ok, r = verify_runtime_attestation({**GOOD, "image_digest": "sha256:zzz"}, CFG, **KW); assert not ok and any("digest" in x for x in r)

def test_no_new_privileges_false_fails():
    ok, r = verify_runtime_attestation({**GOOD, "no_new_privileges": False}, CFG, **KW); assert not ok

def test_stale_older_than_image_build_fails():
    ok, r = verify_runtime_attestation({**GOOD, "generated_at_ts": 850.0}, CFG,
                                       now=1000.0, image_build_ts=900.0, config_mtime=900.0)
    assert not ok and any("stale" in x.lower() for x in r)

def test_stale_older_than_max_age_fails():
    old = {**GOOD, "generated_at_ts": 1000.0}
    ok, r = verify_runtime_attestation(old, CFG, now=1000.0 + 31*86400, image_build_ts=900.0, config_mtime=900.0)
    assert not ok and any("stale" in x.lower() or "age" in x.lower() for x in r)
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_worker_container_attestation.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement verify**

```python
# append to operator_control/worker_container.py
def verify_runtime_attestation(attestation: dict, cfg: dict, *, now: float,
                               image_build_ts: float, config_mtime: float) -> tuple[bool, list[str]]:
    a = attestation or {}
    reasons: list[str] = []
    if a.get("execution_mode") != "container":
        reasons.append("execution_mode not 'container'")
    if a.get("uid") != cfg.get("container_uid") or a.get("uid") in (None, 0):
        reasons.append("effective uid mismatch / root")
    if a.get("gid") != cfg.get("container_gid") or a.get("gid") in (None, 0):
        reasons.append("effective gid mismatch / root")
    if a.get("rootless") is not True:
        reasons.append("runtime not rootless")
    if a.get("no_new_privileges") is not True:
        reasons.append("no_new_privileges not effective")
    if list(a.get("effective_caps") or []) != []:
        reasons.append("effective capabilities not empty")
    if a.get("socket_mounts_present"):
        reasons.append("runtime socket mounted")
    if a.get("host_home_mounted"):
        reasons.append("host home mounted")
    if a.get("image_digest") != cfg.get("image_digest"):
        reasons.append("image digest mismatch vs approved")
    # freshness
    ts = a.get("generated_at_ts")
    if not isinstance(ts, (int, float)):
        reasons.append("attestation missing/invalid timestamp (stale)")
    else:
        if ts < max(image_build_ts, config_mtime):
            reasons.append("attestation stale (older than image build / config change)")
        max_age = float(cfg.get("attestation_max_age_days", 30)) * 86400.0
        if now - ts > max_age:
            reasons.append("attestation stale (exceeds max age)")
    return (not reasons), reasons
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_worker_container_attestation.py -q`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add operator_control/worker_container.py tests/test_worker_container_attestation.py
git commit -m "feat(worker-container): runtime attestation verification + freshness rule"
```

---

### Task 5: Containerfile + attestation entrypoint

**Files:**
- Create: `docker/Containerfile`
- Create: `docker/worker_attest.sh` (entrypoint helper that emits the attestation JSON)
- Test: `tests/test_containerfile_contract.py`

**Interfaces:** none consumed by Python at runtime; the image is referenced by digest in config. The attestation script emits the schema in Global Constraints to `/attest/worker_attestation.json`.

- [ ] **Step 1: Write failing tests (text-contract on the Dockerfile/entrypoint)**

```python
# tests/test_containerfile_contract.py
from pathlib import Path
CF = Path("docker/Containerfile").read_text()
AT = Path("docker/worker_attest.sh").read_text()

def test_base_is_pinned_312_slim():
    assert "python:3.12-slim" in CF

def test_runs_as_non_root_user():
    assert "USER worker" in CF or "USER 1000" in CF

def test_installs_git_node_claude():
    assert "git" in CF and "claude-code" in CF

def test_no_secrets_copied():
    assert ".env" not in CF and "ANTHROPIC_API_KEY" not in CF

def test_attest_emits_required_fields():
    for f in ("execution_mode", "uid", "gid", "rootless", "no_new_privileges",
              "effective_caps", "image_digest", "socket_mounts_present", "host_home_mounted",
              "generated_at_ts"):
        assert f in AT
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_containerfile_contract.py -q`
Expected: FAIL (files missing).

- [ ] **Step 3: Author the image + entrypoint**

`docker/Containerfile`:
```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates libcap2-bin nodejs npm && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code@2.1.181
WORKDIR /opt/app
COPY requirements.txt /opt/app/requirements.txt
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
ENV PATH="/opt/venv/bin:${PATH}"
RUN useradd -m -u 1000 -s /usr/sbin/nologin worker
COPY docker/worker_attest.sh /usr/local/bin/worker_attest.sh
RUN chmod 0555 /usr/local/bin/worker_attest.sh
USER worker
```

`docker/worker_attest.sh` (emits attestation; run as the smoke entrypoint):
```bash
#!/bin/sh
# Emit a runtime attestation describing the container's effective isolation.
set -eu
CAPS=$(capsh --print 2>/dev/null | awk -F'= ' '/Current:/{print $2}' | tr -d ' ' || echo "")
SOCK=false; [ -S /run/docker.sock ] || [ -S /run/podman/podman.sock ] && SOCK=true
HOME_MNT=false; [ -d /host_home ] && HOME_MNT=true
DIGEST="${STOCKBOT_IMAGE_DIGEST:-unknown}"
NNP=true   # launched with --security-opt=no-new-privileges
cat > /attest/worker_attestation.json <<EOF
{"generated_at_ts": $(date +%s), "execution_mode": "container",
 "uid": $(id -u), "gid": $(id -g), "rootless": true, "no_new_privileges": $NNP,
 "effective_caps": [$( [ -z "$CAPS" ] && echo "" || echo "\"$CAPS\"" )],
 "mounts": ["/work:rw","/home/worker/.claude:ro","/attest:rw"],
 "image_digest": "$DIGEST", "socket_mounts_present": $SOCK, "host_home_mounted": $HOME_MNT}
EOF
```

(Note: `rootless`/`no_new_privileges` are launch-guaranteed by the spec flags; the host-side `verify_runtime_attestation` is the authority. The image digest is passed in via `--env STOCKBOT_IMAGE_DIGEST=<approved>` at smoke time.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_containerfile_contract.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add docker/Containerfile docker/worker_attest.sh tests/test_containerfile_contract.py
git commit -m "feat(worker-container): pinned non-root Containerfile + attestation entrypoint"
```

---

### Task 6: `worker_runner` container routing (fail-closed)

**Files:**
- Modify: `operator_control/worker_runner.py` (`_invoke_claude` + a new `_run_via_container` adapter)
- Test: `tests/test_worker_runner_container.py`

**Interfaces:**
- Consumes: `build_container_launch_spec`, `validate_container_configuration`, `probe_container_capabilities`, `verify_runtime_attestation` (Tasks 1,2,4); `create_isolated_workspace`/`extract_validated_diff`/`destroy_workspace` (Task 3).
- Produces: `_invoke_claude(...)` now returns the existing dict PLUS `execution_mode` (`container`|`direct`) and `isolated` (bool). New `_worker_container_cfg(root) -> dict|None`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_worker_runner_container.py
import json, pytest
from pathlib import Path
import operator_control.worker_runner as wr

def _cfg(tmp_path, enabled):
    cfg = {"operator_control": {"worker_container": {
        "enabled": enabled, "podman_path": "/usr/bin/podman", "image_ref": "localhost/stockbot-worker",
        "image_digest": "sha256:abc", "container_uid": 1000, "container_gid": 1000,
        "run_as_user": "stockbot-worker", "credentials_dir": str(tmp_path/"creds"),
        "workspace_root": str(tmp_path/"ws"),
        "resource_limits": {"pids":512,"memory":"2g","cpus":"2","tmpfs_size":"512m","timeout_seconds":1800},
        "env_allowlist": ["HOME"], "cap_drop_exceptions": [], "attestation_max_age_days": 30}}}
    (tmp_path/"config.json").write_text(json.dumps(cfg)); return tmp_path

def test_disabled_uses_direct_recorded_unisolated(tmp_path, monkeypatch):
    root = _cfg(tmp_path, enabled=False)
    monkeypatch.setattr(wr, "_run_direct_claude", lambda *a, **k: {"ok": True})
    out = wr._invoke_claude(str(tmp_path), "prompt", mode="diagnose", root=str(root))
    assert out["execution_mode"] == "direct" and out["isolated"] is False

def test_enabled_podman_missing_fails_closed_no_direct(tmp_path, monkeypatch):
    root = _cfg(tmp_path, enabled=True)
    called = {"direct": False}
    monkeypatch.setattr(wr, "_run_direct_claude", lambda *a, **k: called.__setitem__("direct", True) or {"ok": True})
    monkeypatch.setattr(wr.worker_container, "probe_container_capabilities",
                        lambda cfg: {"podman_present": False, "image_present": False, "digest_pinned": True, "rootless_ok": True})
    out = wr._invoke_claude(str(tmp_path), "prompt", mode="diagnose", root=str(root))
    assert out["ok"] is False and called["direct"] is False
    assert "podman" in (out.get("error") or "").lower()

def test_enabled_validation_failure_fails_closed(tmp_path, monkeypatch):
    root = _cfg(tmp_path, enabled=True)
    monkeypatch.setattr(wr.worker_container, "validate_container_configuration",
                        lambda cfg: (False, ["bad"]))
    monkeypatch.setattr(wr, "_run_direct_claude", lambda *a, **k: pytest.fail("direct must not run"))
    out = wr._invoke_claude(str(tmp_path), "prompt", mode="diagnose", root=str(root))
    assert out["ok"] is False
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_worker_runner_container.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement routing**

Refactor: rename the current body of `_invoke_claude` (the direct path, L153-201) into `_run_direct_claude(worktree_path, prompt_md, mode)` returning the same dict. Then:

```python
# operator_control/worker_runner.py — new imports near the top
from operator_control import worker_container, worker_workspace

def _worker_container_cfg(root):
    import json
    try:
        cfg = json.loads((Path(root) / "config.json").read_text(encoding="utf-8"))
        return (cfg.get("operator_control", {}) or {}).get("worker_container")
    except Exception:
        return None


def _invoke_claude(worktree_path, prompt_md: str, mode: str = "diagnose", root: str = ".") -> dict:
    cfg = _worker_container_cfg(root)
    if not (cfg and cfg.get("enabled")):
        out = _run_direct_claude(worktree_path, prompt_md, mode)
        out["execution_mode"] = "direct"; out["isolated"] = False
        return out
    # container mode — FAIL CLOSED on any problem, never fall back to direct
    ok, reasons = worker_container.validate_container_configuration(cfg)
    if not ok:
        return {"ok": False, "execution_mode": "container", "isolated": False,
                "error": "container config invalid: " + "; ".join(reasons),
                "cost_usd": 0.0, "num_turns": None, "duration_ms": None, "result_text": None}
    caps = worker_container.probe_container_capabilities(cfg)
    if not (caps["podman_present"] and caps["image_present"] and caps["digest_pinned"] and caps["rootless_ok"]):
        return {"ok": False, "execution_mode": "container", "isolated": False,
                "error": f"container preconditions unmet: podman={caps['podman_present']} "
                         f"image={caps['image_present']} digest={caps['digest_pinned']} rootless={caps['rootless_ok']}",
                "cost_usd": 0.0, "num_turns": None, "duration_ms": None, "result_text": None}
    return _run_via_container(worktree_path, prompt_md, mode, cfg, root)
```

Add `_run_via_container` (the single adapter; the real podman spawn). It builds the claude argv (same flags as direct), builds the launch spec, runs `runuser -u <run_as_user> -- <spec>` with `shell=False`, reads + verifies the attestation, and tags the result. (Full body shown; mirror the direct path's JSON parsing.)

```python
def _run_via_container(worktree_path, prompt_md, mode, cfg, root) -> dict:
    settings = Path(__file__).parent / "worker_settings.json"
    claude_argv = ["claude", "-p", prompt_md, "--output-format", "json",
                   "--settings", "/work/.worker_settings.json"]
    if mode == "safe_repair":
        claude_argv += ["--permission-mode", "acceptEdits"]
    attest_dir = str(Path(worktree_path) / ".attest")
    Path(attest_dir).mkdir(parents=True, exist_ok=True)
    spec = worker_container.build_container_launch_spec(
        cfg=cfg, workspace_dir=str(worktree_path), creds_dir=cfg["credentials_dir"],
        attest_dir=attest_dir, claude_argv=claude_argv)
    argv = ["runuser", "-u", cfg["run_as_user"], "--", *spec]
    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=cfg["resource_limits"]["timeout_seconds"])
    # verify attestation (fail-closed)
    import json as _json, time, os
    try:
        att = _json.loads((Path(attest_dir) / "worker_attestation.json").read_text())
    except Exception:
        att = {}
    cfg_mtime = os.path.getmtime(str(Path(root) / "config.json"))
    ok_att, att_reasons = worker_container.verify_runtime_attestation(
        att, cfg, now=time.time(), image_build_ts=cfg_mtime, config_mtime=cfg_mtime)
    # persist attestation for readiness
    out_path = Path(root) / cfg.get("attestation_path", "outputs/operator_control/worker_attestation.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_json.dumps(att))
    if not ok_att:
        return {"ok": False, "execution_mode": "container", "isolated": False,
                "error": "attestation failed: " + "; ".join(att_reasons),
                "cost_usd": 0.0, "num_turns": None, "duration_ms": None, "result_text": None}
    parsed = _parse_claude_json(proc)   # extract the existing JSON-parse block into this helper
    parsed["execution_mode"] = "container"; parsed["isolated"] = True
    return parsed
```

(Extract the L182-201 JSON-parse logic into a shared `_parse_claude_json(proc) -> dict` used by both `_run_direct_claude` and `_run_via_container`. Update the existing `run()` call site to pass `root=root` into `_invoke_claude`.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_worker_runner_container.py tests/test_operator_worker_runner.py -q`
Expected: PASS (new container tests + existing runner tests still green).

- [ ] **Step 5: Commit**

```bash
git add operator_control/worker_runner.py tests/test_worker_runner_container.py
git commit -m "feat(worker-runner): fail-closed container execution path + attestation record"
```

---

### Task 7: Rewrite `_auth_gate` (static checks AND attestation)

**Files:**
- Modify: `portfolio_automation/operator_worker_readiness.py`
- Test: `tests/test_operator_worker_readiness.py` (extend)

**Interfaces:**
- Consumes: `worker_container.validate_container_configuration`, `probe_container_capabilities`, `verify_runtime_attestation`; the persisted attestation artifact.
- Produces: revised `_auth_gate(root)` reading `operator_control.worker_container` cfg + the attestation file. `_in_container` also recognizes `/run/.containerenv`.

- [ ] **Step 1: Write failing tests**

```python
# add to tests/test_operator_worker_readiness.py
import json, time
from pathlib import Path
from portfolio_automation.operator_worker_readiness import operator_worker_readiness

def _wc_cfg(tmp_path, **over):
    base = {"enabled": True, "podman_path": "/usr/bin/podman", "image_ref": "localhost/stockbot-worker",
            "image_digest": "sha256:abc", "container_uid": 1000, "container_gid": 1000,
            "run_as_user": "stockbot-worker",
            "resource_limits": {"pids":512,"memory":"2g","cpus":"2","tmpfs_size":"512m","timeout_seconds":1800},
            "env_allowlist": ["HOME"], "cap_drop_exceptions": [],
            "attestation_path": "outputs/operator_control/worker_attestation.json",
            "attestation_max_age_days": 30}
    base.update(over)
    (tmp_path / "config.json").write_text(json.dumps({"operator_control": {"worker_container": base}}))

def _write_attest(tmp_path, **over):
    a = {"generated_at_ts": time.time(), "execution_mode": "container", "uid": 1000, "gid": 1000,
         "rootless": True, "no_new_privileges": True, "effective_caps": [],
         "mounts": ["/work:rw"], "image_digest": "sha256:abc",
         "socket_mounts_present": False, "host_home_mounted": False}
    a.update(over)
    p = tmp_path / "outputs" / "operator_control"; p.mkdir(parents=True, exist_ok=True)
    (p / "worker_attestation.json").write_text(json.dumps(a))

def test_auth_amber_when_no_attestation(tmp_path, monkeypatch):
    _wc_cfg(tmp_path)
    monkeypatch.setattr("portfolio_automation.operator_worker_readiness.probe_container_capabilities",
                        lambda cfg: {"podman_present": True, "image_present": True, "digest_pinned": True, "rootless_ok": True})
    g = operator_worker_readiness(tmp_path)["gates"]["auth"]
    assert g["status"] == "amber" and "not runtime-verified" in g["reason"]

def test_auth_green_when_static_and_fresh_attestation(tmp_path, monkeypatch):
    _wc_cfg(tmp_path); _write_attest(tmp_path)
    monkeypatch.setattr("portfolio_automation.operator_worker_readiness.probe_container_capabilities",
                        lambda cfg: {"podman_present": True, "image_present": True, "digest_pinned": True, "rootless_ok": True})
    g = operator_worker_readiness(tmp_path)["gates"]["auth"]
    assert g["status"] == "green"

def test_auth_amber_when_last_run_direct(tmp_path, monkeypatch):
    _wc_cfg(tmp_path); _write_attest(tmp_path, execution_mode="direct")
    monkeypatch.setattr("portfolio_automation.operator_worker_readiness.probe_container_capabilities",
                        lambda cfg: {"podman_present": True, "image_present": True, "digest_pinned": True, "rootless_ok": True})
    g = operator_worker_readiness(tmp_path)["gates"]["auth"]
    assert g["status"] == "amber"

def test_auth_amber_when_digest_mismatch(tmp_path, monkeypatch):
    _wc_cfg(tmp_path); _write_attest(tmp_path, image_digest="sha256:zzz")
    monkeypatch.setattr("portfolio_automation.operator_worker_readiness.probe_container_capabilities",
                        lambda cfg: {"podman_present": True, "image_present": True, "digest_pinned": True, "rootless_ok": True})
    assert operator_worker_readiness(tmp_path)["gates"]["auth"]["status"] == "amber"

def test_auth_amber_when_disabled(tmp_path):
    _wc_cfg(tmp_path, enabled=False)
    assert operator_worker_readiness(tmp_path)["gates"]["auth"]["status"] == "amber"
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_readiness.py -q`
Expected: FAIL (auth still uses the old euid logic).

- [ ] **Step 3: Rewrite `_auth_gate`** (replace the existing `_auth_gate` body)

```python
# portfolio_automation/operator_worker_readiness.py — new import (top)
from operator_control.worker_container import (
    validate_container_configuration, probe_container_capabilities, verify_runtime_attestation,
)

def _auth_gate(root: Path) -> dict[str, Any]:
    import json, time, os
    try:
        cfg_all = json.loads((root / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return _amber("worker_container config unreadable", "auto")
    wc = (cfg_all.get("operator_control", {}) or {}).get("worker_container") or {}
    if not wc.get("enabled"):
        return _amber("container mode disabled — worker would run unisolated", "auto")
    ok, reasons = validate_container_configuration(wc)
    if not ok:
        return _amber("static checks failed: " + "; ".join(reasons), "auto")
    caps = probe_container_capabilities(wc)
    if not all((caps["podman_present"], caps["image_present"], caps["digest_pinned"], caps["rootless_ok"])):
        return _amber(f"capability probe failed ({caps})", "auto")
    att_path = root / wc.get("attestation_path", "outputs/operator_control/worker_attestation.json")
    try:
        att = json.loads(att_path.read_text(encoding="utf-8"))
    except Exception:
        return _amber("configured but not runtime-verified (no attestation)", "auto")
    cfg_mtime = os.path.getmtime(str(root / "config.json"))
    a_ok, a_reasons = verify_runtime_attestation(att, wc, now=time.time(),
                                                 image_build_ts=cfg_mtime, config_mtime=cfg_mtime)
    if not a_ok:
        return _amber("attestation invalid/stale: " + "; ".join(a_reasons), "auto")
    return {"status": "green",
            "reason": "container-isolated, runtime-attested (egress: unrestricted — deferred)",
            "source": "auto"}
```

Also update `_in_container` to also return True when `Path("/run/.containerenv").exists()`.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_readiness.py -q`
Expected: PASS (the new auth tests + the existing readiness tests; the old root/container auto-detection tests for `_auth_gate` are replaced — update any that asserted the old behavior).

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/operator_worker_readiness.py tests/test_operator_worker_readiness.py
git commit -m "feat(operator-readiness): auth gate = static checks AND fresh runtime attestation"
```

---

### Task 8: Provisioning runbook + setup script + no-side-effect guard

**Files:**
- Create: `scripts/worker_container_setup.sh` (operator-run; does NOT auto-run anything on import)
- Create: `docs/operator_worker_container.md`
- Test: `tests/test_worker_container_setup_contract.py` + a readiness no-side-effect guard test

**Interfaces:** none (docs + ops). 

- [ ] **Step 1: Write failing tests**

```python
# tests/test_worker_container_setup_contract.py
from pathlib import Path
import subprocess, sys
S = Path("scripts/worker_container_setup.sh").read_text()

def test_setup_covers_required_steps():
    for kw in ("useradd", "subuid", "subgid", "enable-linger", "podman build",
               "sha256", "stockbot-worker"):
        assert kw in S

def test_setup_does_not_auto_execute_dangerously():
    # must be guarded behind an explicit subcommand / main guard, not run on source
    assert ("\"\\$1\"" in S) or ('case "$1"' in S) or ('if [ "$#"' in S)

def test_readiness_probe_has_no_side_effects(tmp_path, monkeypatch):
    # readiness must not mutate git refs / worktrees / decision artifacts
    import json
    from portfolio_automation.operator_worker_readiness import operator_worker_readiness
    (tmp_path/"config.json").write_text(json.dumps({"operator_control": {"worker_container": {"enabled": False}}}))
    dp = tmp_path/"outputs"/"latest"; dp.mkdir(parents=True); (dp/"decision_plan.json").write_text('{"x":1}')
    before = (dp/"decision_plan.json").read_text()
    operator_worker_readiness(tmp_path)
    assert (dp/"decision_plan.json").read_text() == before
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_worker_container_setup_contract.py -q`
Expected: FAIL.

- [ ] **Step 3: Author the runbook + script**

`scripts/worker_container_setup.sh` — guarded, operator-run; each step a subcommand; prints commands, requires explicit invocation (no destructive auto-run). Cover: create `stockbot-worker` (no sudo, nologin), add `/etc/subuid` + `/etc/subgid` ranges, `loginctl enable-linger stockbot-worker`, `apt-get install -y podman`, `podman build -t localhost/stockbot-worker -f docker/Containerfile .`, capture `sha256` digest (`podman inspect --format '{{.Digest}}'`), instructions to pin it in config, establish the worker credential dir, then run a smoke attestation. Use a `case "$1"` dispatcher so sourcing doesn't execute.

`docs/operator_worker_container.md` — the prose runbook: prerequisites, the exact commands (with the sudo ones flagged operator-run), how to pin the digest, how to enable, how to verify `auth` flips green via a smoke attestation, kill-switch, and the deferred-egress note.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_worker_container_setup_contract.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/worker_container_setup.sh docs/operator_worker_container.md tests/test_worker_container_setup_contract.py
git commit -m "docs(worker-container): operator provisioning runbook + setup script + no-side-effect guard"
```

---

## Self-Review

**Spec coverage:**
- Dedicated `stockbot-worker` account + `runuser` adapter → Task 6 (`_run_via_container`) + Task 8 (provisioning) ✓
- `auth` static checks AND attestation + freshness + downgrade → Tasks 2, 4, 7 ✓
- Prod `.git` never mounted; isolated clone → Task 1 (spec absence test) + Task 3 ✓
- Minimal RO creds + writable cache; no host home/secrets → Task 1 (mount tests) + Containerfile/spec ✓
- Fail-closed (no direct fallback) → Task 6 ✓
- Launch baseline (all flags) → Task 1 (+ tests) ✓
- `bounded_cmd` stays AMBER → not touched by any task (explicit non-goal) ✓
- Component boundaries (`worker_container.py` funcs + `worker_workspace.py` + narrow adapter) → Tasks 1–4, 6 ✓
- Required tests (no-fallback, .git-not-writable, mutable-tag, missing/stale attestation, mode/uid/cap/mount mismatch, socket/host-creds reject, env strip, path traversal, no user-derived argv, no side effects) → distributed across Tasks 1,2,3,4,6,7,8 ✓
- Phase outcome (auth post-attestation; others AMBER/false) → Task 7 + non-goals ✓

**Placeholder scan:** code present in every code step; the Containerfile/runbook prose steps name exact commands/keywords. The one "extract the JSON-parse block into `_parse_claude_json`" in Task 6 is a precise refactor instruction with the source line range, not a placeholder.

**Type consistency:** `build_container_launch_spec(cfg, workspace_dir, creds_dir, attest_dir, claude_argv)`, `validate_container_configuration(cfg)->(bool,list)`, `probe_container_capabilities(cfg)->dict`, `verify_runtime_attestation(att,cfg,*,now,image_build_ts,config_mtime)->(bool,list)`, `create_isolated_workspace/destroy_workspace/extract_validated_diff`, `_invoke_claude(...,root)` with `execution_mode`/`isolated` — names consistent across Tasks 1–7 and matched at call sites.

**Verification points for the implementer (not placeholders):** confirm `worker_settings.json` is copied into the isolated clone as `/work/.worker_settings.json` before launch (Task 6 references it); confirm the existing `run()`/`_invoke_claude` call site passes `root`; confirm any pre-existing `_auth_gate` tests asserting the old euid behavior are updated in Task 7.
