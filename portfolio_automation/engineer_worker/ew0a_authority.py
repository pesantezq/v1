"""EW-0A engineering authority levels + the bounded A1 promotion.

Authority is TRUSTED-CONTROLLED state, never worker-controlled. The Engineer
Worker begins at A0 (read-only diagnostics; no source-edit authority). The A1
promotion (``A1_ASSISTED_ENGINEERING``) grants the Engineer the bounded ability to
perform approved E1/E2 tasks in disposable worktrees with candidate patches under
independent verification — and NOTHING else. A set of operations is denied at
EVERY level (including A1), and the worker can never promote itself: the authority
state file is outside the worker's repair scope and is a protected path.

``experimental_noncanonical``. Does not define canonical Northstar contracts.
"""
from __future__ import annotations

import json
import os
from enum import Enum
from pathlib import Path

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER
from portfolio_automation.engineer_worker.ew0a import (
    RiskClass, Executor, EW0AError, worker_may_execute, default_executor)

SCHEMA_KIND = EXPERIMENTAL_MARKER
AUTHORITY_SCHEMA_VERSION = "engineering.authority.v0"

# Trusted-controlled authority state (NOT in the worker's repair scope; also a
# protected path — see policy._PROTECTED_PATTERNS).
DEFAULT_STATE_REL = "config/ew0a_authority.json"


class AuthorityError(PermissionError):
    """Raised when an action is denied by the engineering authority model."""


class EngineerAuthorityLevel(str, Enum):
    A0_DIAGNOSTIC = "A0_DIAGNOSTIC"                      # read-only diagnostics; no source edits
    A1_ASSISTED_ENGINEERING = "A1_ASSISTED_ENGINEERING"  # bounded E1/E2 assisted engineering


# What A1 grants and (permanently) denies — documentation + enforcement anchors.
A1_GRANTS = (
    "approved E1/E2 StockBot engineering tasks",
    "disposable isolated worktrees",
    "bounded feature-branch source edits",
    "approved deterministic tools",
    "approved test execution",
    "candidate patches/commits",
    "verification-driven retry",
    "evidence generation",
)

# Operations denied at EVERY level, including A1. These are hard boundaries.
FORBIDDEN_OPS = frozenset({
    "MAIN_WRITE", "MERGE", "AUTONOMOUS_PUSH", "PRODUCTION_WRITE", "OPT_STOCKBOT_WRITE",
    "DEPLOY", "SERVICE_RESTART", "CREDENTIAL_ACCESS", "SECURITY_POLICY_SELF_MOD",
    "PROTECTED_SCORING_MODIFICATION", "BROKER_ACTION", "CAPITAL_DECISION",
    "E3_SELF_ASSIGN", "E4_SELF_ASSIGN", "SELF_PROMOTION",
})


def read_authority_level(repo_root: str | Path, rel: str = DEFAULT_STATE_REL) -> EngineerAuthorityLevel:
    """Read the current authority level. Defaults to A0 (fail-closed) if absent
    or malformed — the worker is never implicitly promoted."""
    p = Path(repo_root) / rel
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return EngineerAuthorityLevel(data["level"])
    except (OSError, ValueError, KeyError):
        return EngineerAuthorityLevel.A0_DIAGNOSTIC


def set_authority_level(repo_root: str | Path, level: EngineerAuthorityLevel,
                        actor: str, now: str, rel: str = DEFAULT_STATE_REL) -> None:
    """TRUSTED-side write of the authority state. Records actor + timestamp. This
    is never callable through the worker's tool/repair surface."""
    p = Path(repo_root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": AUTHORITY_SCHEMA_VERSION, "schema_kind": SCHEMA_KIND,
               "level": level.value, "actor": actor, "updated_at": now,
               "grants": list(A1_GRANTS) if level is EngineerAuthorityLevel.A1_ASSISTED_ENGINEERING else [],
               "forbidden_ops": sorted(FORBIDDEN_OPS)}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def admit_engineer_task(level: EngineerAuthorityLevel, risk: RiskClass) -> Executor:
    """Decide whether the Engineer Worker may execute a task of this risk at this
    authority level. E3/E4 always route to Claude(/human). E1/E2 are executable by
    the Engineer ONLY at A1; at A0 the Engineer has diagnostic-only authority.
    Raises AuthorityError otherwise (fail closed)."""
    if not worker_may_execute(risk):
        raise AuthorityError(f"{risk.value} routes to {default_executor(risk).value}; Engineer denied")
    if level is not EngineerAuthorityLevel.A1_ASSISTED_ENGINEERING:
        raise AuthorityError(f"Engineer source-edit authority not enabled at {level.value} "
                             f"(A0 is diagnostic-only)")
    return default_executor(risk)   # ENGINEER (E1) or ENGINEER_STRICT (E2)


def assert_operation_allowed(level: EngineerAuthorityLevel, op: str) -> None:
    """Hard operation boundary. The forbidden operations are denied at EVERY level,
    including A1 — A1 never grants main/merge/push/production/deploy/restart/secret/
    security-policy/scoring/broker/capital/self-assign/self-promotion authority."""
    if op in FORBIDDEN_OPS:
        raise AuthorityError(f"operation {op} is denied at all levels (incl. {level.value})")


def a1_denies() -> frozenset[str]:
    return FORBIDDEN_OPS
