import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

CF = Path("docker/Containerfile").read_text()
AT = Path("docker/worker_attest.sh").read_text()

REQUIRED_FIELDS = (
    "generated_at_ts", "execution_mode", "uid", "gid", "rootless",
    "no_new_privileges", "effective_caps", "mounts", "image_digest",
    "socket_mounts_present", "host_home_mounted",
)


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

def test_attest_caps_json_array_syntax():
    # effective_caps must use raw variable, not quoted string
    assert '"effective_caps": $CAPS_JSON' in AT

def test_attest_sock_precedence_grouping():
    # Grouping braces must be present to avoid operator-precedence bug
    assert "{ [ -S /run/docker.sock ] || [ -S /run/podman/podman.sock ]; } && SOCK=true" in AT

def test_containerfile_creates_attest_dir():
    assert "mkdir -p /attest" in CF and "chown 1000:1000 /attest" in CF

@pytest.mark.skipif(shutil.which("sh") is None, reason="sh not available")
def test_attest_script_executes_and_emits_valid_json():
    """Run the attestation script (with /attest shimmed to a tmp dir) and validate output."""
    script_src = Path("docker/worker_attest.sh").read_text()
    with tempfile.TemporaryDirectory() as tmpdir:
        # Replace hardcoded /attest with our tmp dir
        shimmed = script_src.replace("/attest/worker_attestation.json",
                                     os.path.join(tmpdir, "worker_attestation.json"))
        shimmed_path = os.path.join(tmpdir, "worker_attest_shimmed.sh")
        with open(shimmed_path, "w") as f:
            f.write(shimmed)

        env = os.environ.copy()
        env["STOCKBOT_IMAGE_DIGEST"] = "sha256:test"

        result = subprocess.run(
            ["sh", shimmed_path],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Script failed: {result.stderr}"

        out_path = os.path.join(tmpdir, "worker_attestation.json")
        assert os.path.exists(out_path), "worker_attestation.json not created"

        with open(out_path) as f:
            data = json.loads(f.read())

        assert isinstance(data["effective_caps"], list), \
            f"effective_caps must be a list, got {type(data['effective_caps'])}: {data['effective_caps']}"
        assert isinstance(data["socket_mounts_present"], bool), \
            f"socket_mounts_present must be bool, got {type(data['socket_mounts_present'])}"
        assert data["image_digest"] == "sha256:test"

        for field in REQUIRED_FIELDS:
            assert field in data, f"Missing field: {field}"
