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
