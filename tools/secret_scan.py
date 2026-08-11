"""Deterministic repository secret-hygiene gate.

Scans tracked text files for obvious private-key material (and, for the unit
tests, credential-shaped assignments). Fail-closed and dependency-free so it can
run as a plain pytest check (see tests/test_secret_hygiene.py) or standalone.

SCOPE / HONESTY: this gate prevents *new* accidental key commits to the working
tree. It does NOT and cannot remediate historical git-history exposure — a key
already pushed to a remote must be rotated regardless of what HEAD contains.
See docs/LEGACY_CREDENTIAL_EXPOSURE.md.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

# A PEM/OpenSSH private-key banner is an unambiguous, low-false-positive signal.
PRIVATE_KEY_MARKER = re.compile(
    r"-----BEGIN (?:OPENSSH|RSA|EC|DSA|PGP|ENCRYPTED)? ?PRIVATE KEY(?: BLOCK)?-----"
)

# Credential-shaped assignments (used by the unit tests; NOT applied repo-wide to
# avoid false positives on config templates). Placeholder values are ignored.
_PLACEHOLDER = r"(?:<[^>]*>|null|none|changeme|example[a-z0-9]*|x{3,}|\.\.\.|redacted)"
CREDENTIAL_ASSIGN = re.compile(
    r"\b(?:aws_secret_access_key|secret_access_key|private_key|client_secret|"
    r"refresh_token|access_token|api_key)\b\s*[:=]\s*"
    r"(?![\"']?" + _PLACEHOLDER + r"[\"']?[\s\"']*$)"
    r"[\"']?[A-Za-z0-9/+_\-]{12,}",
    re.IGNORECASE,
)


# Tracked files that legitimately carry key-shaped strings: secret-REJECTION
# test fixtures and the scanner/tests that embed the detection patterns. Adding a
# path here requires explicit operator sign-off (it is a hole in the gate).
DEFAULT_ALLOWLIST: frozenset[str] = frozenset({
    "tests/test_prod_evidence.py",     # fake private-key body for secret-rejection tests
    "tests/test_secret_hygiene.py",    # synthetic key samples
    "tools/secret_scan.py",            # the detection patterns themselves
})


def has_private_key_marker(text: str) -> bool:
    """True if the text contains a private-key banner."""
    return bool(PRIVATE_KEY_MARKER.search(text))


def has_credential_assignment(text: str) -> bool:
    """True if the text contains a non-placeholder credential assignment."""
    return bool(CREDENTIAL_ASSIGN.search(text))


def _tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True, check=True,
    )
    return [f for f in out.stdout.decode("utf-8", "replace").split("\0") if f]


def scan_repo(root: str | Path, allowlist: set[str] | frozenset[str] = frozenset()) -> list[tuple[str, str]]:
    """Return [(relpath, reason)] for tracked non-binary files that contain
    private-key material, excluding paths in ``allowlist``. Deterministic order."""
    root = Path(root)
    hits: list[tuple[str, str]] = []
    for rel in _tracked_files(root):
        if rel in allowlist:
            continue
        p = root / rel
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:8192]:      # skip binaries
            continue
        text = data.decode("utf-8", "replace")
        if has_private_key_marker(text):
            hits.append((rel, "private-key-marker"))
    return sorted(hits)


if __name__ == "__main__":
    import sys
    repo = Path(__file__).resolve().parents[1]
    found = scan_repo(repo, DEFAULT_ALLOWLIST)
    for rel, why in found:
        print(f"{why}: {rel}")
    sys.exit(1 if found else 0)
