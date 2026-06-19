"""Pure construction + static validation + attestation verification for the
rootless-Podman worker isolation (hardening Phase 1). Argv is built with
shell=False semantics: image (digest), executable, uid/gid, and all mount
SOURCES are constants/config-derived — never from work-order fields."""
from __future__ import annotations

import os
import re
import shutil
import subprocess

WO_ID_RE = re.compile(r"^wo_[A-Za-z0-9_]+$")


def build_container_launch_spec(*, cfg: dict, workspace_dir: str, creds_dir: str,
                                attest_dir: str, claude_argv: list[str]) -> list[str]:
    rl = cfg["resource_limits"]
    image = f'{cfg["image_ref"]}@{cfg["image_digest"]}'
    uid, gid = cfg["container_uid"], cfg["container_gid"]
    argv = [
        cfg["podman_path"], "run", "--rm",
        # keep-id maps the host worker uid -> the same container uid so the
        # read-only :ro creds mount (a 0600 ~/.claude/.credentials.json owned by
        # the host worker user) is readable inside. Without it, container uid 1000
        # maps to a subuid and cannot read the secured creds (verified 2026-06-19).
        "--userns=keep-id",
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
    # Always inject the approved digest so worker_attest.sh can record it for attestation.
    argv += ["--env", f"STOCKBOT_IMAGE_DIGEST={cfg['image_digest']}"]
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
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        reasons.append("attestation missing/invalid timestamp (stale)")
    else:
        if ts < max(image_build_ts, config_mtime):
            reasons.append("attestation stale (older than image build / config change)")
        max_age = float(cfg.get("attestation_max_age_days", 30)) * 86400.0
        if now - ts > max_age:
            reasons.append("attestation stale (exceeds max age)")
    return (not reasons), reasons


def _run(argv: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def worker_cmd_prefix(cfg: dict) -> list[str]:
    """Prefix that runs a podman command AS the rootless worker user, or [] to
    run in-process. The image + rootless context live in the worker's storage
    (not root's), so probing/launching as the current user (root) reports
    image=False/rootless=False. Applies only when run_as_user is configured and
    differs from the current user (needs runuser, i.e. root); degrades to [] for
    a non-root caller (probe then reports honestly from the current context)."""
    import pwd
    user = cfg.get("run_as_user")
    if not user:
        return []
    try:
        if pwd.getpwuid(os.getuid()).pw_name == user:
            return []  # already the worker user — no runuser needed
    except Exception:
        pass
    uid = int(cfg.get("container_uid") or 1000)
    try:
        home = pwd.getpwnam(user).pw_dir
    except Exception:
        home = f"/home/{user}"
    rd = f"/run/user/{uid}"
    return ["runuser", "-u", user, "--", "env",
            f"HOME={home}", f"XDG_RUNTIME_DIR={rd}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path={rd}/bus"]


def probe_container_capabilities(cfg: dict) -> dict:
    podman = cfg.get("podman_path", "")
    podman_present = bool(podman) and os.path.exists(podman)
    image_present = False
    digest_pinned = isinstance(cfg.get("image_digest"), str) and cfg["image_digest"].startswith("sha256:")
    rootless_ok = False
    if podman_present:
        prefix = worker_cmd_prefix(cfg)  # probe the WORKER's rootless context, not root's
        try:
            insp = _run([*prefix, podman, "image", "exists",
                         f'{cfg["image_ref"]}@{cfg["image_digest"]}'])
            image_present = insp.returncode == 0
            info = _run([*prefix, podman, "info", "--format", "{{.Host.Security.Rootless}}"])
            rootless_ok = info.stdout.strip() == "true"
        except Exception:
            pass
    return {"podman_present": podman_present, "image_present": image_present,
            "digest_pinned": digest_pinned, "rootless_ok": rootless_ok}
