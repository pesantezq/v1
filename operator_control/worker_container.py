"""Pure construction + static validation + attestation verification for the
rootless-Podman worker isolation (hardening Phase 1). Argv is built with
shell=False semantics: image (digest), executable, uid/gid, and all mount
SOURCES are constants/config-derived — never from work-order fields."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
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
    return os.environ.get(name, "")


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
                         f'{cfg["image_ref"]}@{cfg["image_digest"]}'])
            image_present = insp.returncode == 0
            info = _run([podman, "info", "--format", "{{.Host.Security.Rootless}}"])
            rootless_ok = info.stdout.strip() == "true"
        except Exception:
            pass
    return {"podman_present": podman_present, "image_present": image_present,
            "digest_pinned": digest_pinned, "rootless_ok": rootless_ok}
