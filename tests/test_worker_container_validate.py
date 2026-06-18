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
