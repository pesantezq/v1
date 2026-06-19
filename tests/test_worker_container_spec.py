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
    # keep-id maps host worker uid -> same container uid so the :ro 0600 creds
    # mount is readable inside (without it, container 1000 -> subuid -> EACCES).
    assert "--userns=keep-id" in _spec()
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
    # STOCKBOT_IMAGE_DIGEST is an unconditional config-constant injection, not from allowlist.
    assert set(names) <= {"HOME", "PATH", "CLAUDE_CONFIG_DIR", "STOCKBOT_IMAGE_DIGEST"}

def test_spec_claude_argv_is_last_and_unmodified():
    s = _spec()
    assert s[-5:] == ["claude", "-p", "do the thing", "--output-format", "json"]


def test_spec_runs_attest_before_claude():
    """The container must run worker_attest.sh (writes the attestation the host
    verifies, fail-closed) before exec'ing claude. claude_argv stays last."""
    s = _spec()
    # attest script (inside the sh -c arg) precedes the exact "claude" argv element
    attest_idx = next(i for i, p in enumerate(s) if "worker_attest.sh" in p)
    claude_idx = s.index("claude")  # exact element, not the ".claude" mount substring
    assert attest_idx < claude_idx
    assert any('exec "$@"' in part for part in s)
    assert "/bin/sh" in s

def test_spec_injects_image_digest_env():
    s = _spec()
    # Find all --env values
    envs = [s[i+1] for i, a in enumerate(s) if a == "--env"]
    assert f"STOCKBOT_IMAGE_DIGEST={CFG['image_digest']}" in envs
