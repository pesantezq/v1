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
