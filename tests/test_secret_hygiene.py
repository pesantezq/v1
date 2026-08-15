"""Repository secret-hygiene gate tests.

Proves the deterministic scanner (tools/secret_scan.py) detects obvious
private-key / credential material, and that the tracked working tree carries no
UNEXPECTED private-key files. This is a forward-looking guard against *new*
accidental key commits; it does not remediate historical git-history exposure
(see docs/LEGACY_CREDENTIAL_EXPOSURE.md).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("secret_scan", REPO / "tools" / "secret_scan.py")
secret_scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(secret_scan)

# Single source of truth for the fixture allowlist lives in the scanner module.
FIXTURE_ALLOWLIST = secret_scan.DEFAULT_ALLOWLIST


@pytest.mark.parametrize("sample", [
    "-----BEGIN OPENSSH PRIVATE KEY-----\nZm9v\n-----END OPENSSH PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
])
def test_detects_private_key_markers(sample):
    assert secret_scan.has_private_key_marker(sample)


@pytest.mark.parametrize("benign", [
    "just some prose about private key handling",
    "PRIVATE_KEY_PATH=/home/user/.ssh/id_ed25519",   # a path, not a key body
    "no secrets here",
])
def test_ignores_benign_text(benign):
    assert not secret_scan.has_private_key_marker(benign)


@pytest.mark.parametrize("cred,expected", [
    ("aws_secret_access_key = AKIAIOSFODNN7REALLOOKINGSECRET", True),
    ("client_secret: s3cr3tVALUElongenough123", True),
    ("api_key = <redacted>", False),
    ("client_secret: changeme", False),
    ("access_token = null", False),
])
def test_credential_assignment_detection(cred, expected):
    assert secret_scan.has_credential_assignment(cred) is expected


def test_no_unexpected_private_key_files_tracked():
    """The tracked tree must carry no private-key material outside the fixture
    allowlist. The operator-accepted, remediation-DEFERRED legacy files
    (stockbot.txt / stockbot.txt.pub) are removed from the tree by this mission,
    so any hit here is a NEW regression that must fail the gate."""
    hits = secret_scan.scan_repo(REPO, allowlist=FIXTURE_ALLOWLIST)
    assert hits == [], (
        "unexpected private-key material tracked (rotate + remove; do not add to "
        f"the allowlist without explicit operator sign-off): {hits}"
    )
