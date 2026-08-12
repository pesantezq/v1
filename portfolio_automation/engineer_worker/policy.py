"""Deterministic authority policy for the Engineer Worker MVP (fail-closed).

Nothing here trusts the model. These functions decide, in trusted code, which
paths may be read, which may be repaired, which are protected, and which tool
capabilities are permitted for a given job type.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from portfolio_automation.engineer_worker.contracts import (
    EngineeringJobType, ToolCapability,
)


class PolicyError(PermissionError):
    """Raised when a path or capability is refused by policy (fail-closed)."""


# --- protected paths: the MVP may DIAGNOSE these, never MODIFY them ----------
# Matched against a repo-relative POSIX path. Patterns are substrings/prefixes
# checked case-sensitively; anything matching is protected.
_PROTECTED_PATTERNS: tuple[str, ...] = (
    "decision_engine.py",
    "decision_plan.json",
    "portfolio_automation/scoring",           # protected scoring implementations
    "portfolio_automation/broker",            # broker execution paths
    "broker_execution",
    ".agent/",                                # authoritative project state
    "config/agent_policy.yaml",               # Northstar authority policy
    "portfolio_automation/agent_policy.py",
    "ops/agent_lab/",                         # sandbox security runtime source
    "portfolio_automation/rd_control/sandbox.py",   # security runtime
    "portfolio_automation/rd_control/registry.py",
    "portfolio_automation/rd_control/contracts.py",
    "portfolio_automation/rd_control/health.py",
    "systemd/",
    ".git/",
    ".env",
    "credentials",
    "secrets",
    "id_ed25519", "id_rsa", ".netrc", ".git-credentials",
    # The worker may never modify its OWN authority/policy or core.
    "portfolio_automation/engineer_worker/policy.py",
    "portfolio_automation/engineer_worker/controller.py",
    "portfolio_automation/engineer_worker/contracts.py",
    "portfolio_automation/engineer_worker/adapters.py",
    "portfolio_automation/engineer_worker/model_adapter.py",
    # EW-0A certification/authority core + the trusted authority/runtime state.
    "portfolio_automation/engineer_worker/ew0a.py",
    "portfolio_automation/engineer_worker/ew0a_authority.py",
    "portfolio_automation/engineer_worker/ew0a_loop.py",
    "portfolio_automation/engineer_worker/gpt_supervisor.py",
    "config/ew0a_authority",                  # trusted-controlled authority state
    "config/ew0a_runtime",                    # trusted-controlled runtime policy
)

# --- repair scope: candidate edits are only allowed under these prefixes -----
# (and never if also protected). Deliberately narrow for the MVP.
_REPAIR_ALLOWED_PREFIXES: tuple[str, ...] = (
    "docs/",
    "tests/",
    "devtools/",
    "scripts/dev/",
)

# --- which tool capabilities each job type may use ---------------------------
_JOB_TOOL_GRANTS: dict[EngineeringJobType, frozenset[ToolCapability]] = {
    EngineeringJobType.ENVIRONMENT_DIAGNOSTIC: frozenset({
        ToolCapability.CHECK_RD_HEALTH, ToolCapability.CHECK_SANDBOX,
        ToolCapability.CHECK_OLLAMA, ToolCapability.CHECK_REPO_STATUS,
    }),
    EngineeringJobType.DAILY_RUN_DIAGNOSTIC: frozenset({
        ToolCapability.READ_DAILY_LOG, ToolCapability.READ_DAILY_ARTIFACT,
        ToolCapability.CHECK_REPO_STATUS, ToolCapability.CHECK_RD_HEALTH,
        ToolCapability.READ_PRODUCTION_DAILY_EVIDENCE,
    }),
    EngineeringJobType.REPOSITORY_DIAGNOSTIC: frozenset({
        ToolCapability.CHECK_REPO_STATUS, ToolCapability.RUN_APPROVED_TEST,
    }),
    EngineeringJobType.TEST_FAILURE_DIAGNOSTIC: frozenset({
        ToolCapability.CHECK_REPO_STATUS, ToolCapability.RUN_APPROVED_TEST,
    }),
    EngineeringJobType.REPAIR_CANDIDATE: frozenset({
        ToolCapability.CHECK_REPO_STATUS, ToolCapability.RUN_APPROVED_TEST,
        ToolCapability.READ_DAILY_ARTIFACT,
    }),
}


def _relposix(repo_root: Path, target: Path) -> str:
    return target.relative_to(repo_root).as_posix()


def _norm(rel_path: str) -> str:
    """Normalize to a POSIX relative path WITHOUT corrupting dotfiles.
    (Note: str.lstrip('./') would strip the chars '.' and '/', mangling
    '.git'/'.env'/'.agent' — so we strip only a leading './' sequence.)"""
    p = rel_path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def is_protected(rel_path: str) -> bool:
    p = _norm(rel_path)
    return any(pat in p or p.startswith(pat) for pat in _PROTECTED_PATTERNS)


def is_repair_allowed(rel_path: str) -> bool:
    """A path may be repaired iff it is under an allowed prefix AND not protected."""
    p = _norm(rel_path)
    if is_protected(p):
        return False
    return any(p.startswith(prefix) for prefix in _REPAIR_ALLOWED_PREFIXES)


def safe_join(root: str | Path, rel_path: str, *, must_exist: bool = False) -> Path:
    """Resolve *rel_path* under *root*, refusing absolute paths, traversal, and
    symlink escapes. Returns the resolved absolute Path or raises PolicyError."""
    root_p = Path(root).resolve()
    if os.path.isabs(rel_path) or rel_path.strip() == "":
        raise PolicyError(f"path must be relative and non-empty: {rel_path!r}")
    if "\x00" in rel_path:
        raise PolicyError("NUL in path")
    candidate = (root_p / rel_path)
    # Reject traversal via the lexical form before resolving.
    if ".." in Path(rel_path).parts:
        raise PolicyError(f"path traversal denied: {rel_path!r}")
    resolved = candidate.resolve()
    # Must stay within root even after resolving symlinks.
    try:
        resolved.relative_to(root_p)
    except ValueError:
        raise PolicyError(f"path escapes workspace: {rel_path!r}")
    # Refuse if any existing component is a symlink pointing outside root.
    if candidate.exists() or candidate.is_symlink():
        if candidate.is_symlink():
            raise PolicyError(f"symlink not allowed: {rel_path!r}")
    if must_exist and not resolved.exists():
        raise PolicyError(f"path does not exist: {rel_path!r}")
    return resolved


def tools_for_job(job_type: EngineeringJobType) -> frozenset[ToolCapability]:
    return _JOB_TOOL_GRANTS.get(job_type, frozenset())


def check_tool_allowed(job_type: EngineeringJobType, cap: ToolCapability) -> None:
    if cap not in tools_for_job(job_type):
        raise PolicyError(f"tool {cap.value} not permitted for job {job_type.value}")


def check_test_allowed(allowed_tests: list[str], requested: str) -> None:
    """A pytest target may run only if it is in the job's explicit allowlist and
    is not a protected path."""
    req = requested.strip()
    # normalize a trailing ::node selector to the file for the allowlist match
    file_part = req.split("::", 1)[0]
    if is_protected(file_part):
        raise PolicyError(f"test target is a protected path: {req}")
    if req not in allowed_tests and file_part not in allowed_tests:
        raise PolicyError(f"test target not in allowlist: {req}")
