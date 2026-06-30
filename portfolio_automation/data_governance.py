"""
Data Governance — Output Namespace Utilities
============================================

Defines safe output namespaces and validates write paths to prevent accidental
contamination between live, historical replay, sandbox, and future user-scoped
outputs.

Usage (new modules should use these helpers instead of raw Path writes)::

    from portfolio_automation.data_governance import (
        OutputNamespace, get_output_path, safe_write_json,
    )

    path = safe_write_json(
        OutputNamespace.HISTORICAL, "historical_calibration.json", payload
    )

Existing production writers (outputs/latest, outputs/policy, outputs/portfolio)
are not changed by this module. They carry a TODO(v2-data-governance) comment
marking where the migration should happen.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class OutputNamespace(str, Enum):
    """Logical output partitions. Values are human-readable labels."""
    LIVE        = "live"
    HISTORICAL  = "historical"
    SANDBOX     = "sandbox"
    POLICY      = "policy"
    PORTFOLIO   = "portfolio"
    LATEST      = "latest"
    USER        = "user"
    # ── Simulation-governance lane (added 2026-06-16) ──────────────────────
    # SIMULATION holds the consolidated daily simulation bundle produced by the
    # active simulation/test lane. PROMOTION_REVIEW holds the AI/product review
    # packet + verdicts + pending production proposals. PROMOTION_APPROVALS holds
    # the human-approval manifest and the production-application audit trail.
    # These are distinct from SANDBOX (raw experiment artifacts) so the gated
    # promotion workflow has its own auditable partitions.
    SIMULATION          = "simulation"
    PROMOTION_REVIEW    = "promotion_review"
    PROMOTION_APPROVALS = "promotion_approvals"


class DataGovernanceError(Exception):
    """Raised when a path write violates namespace boundaries."""


@dataclass
class OutputPathPolicy:
    """Describes the governance contract for one namespace."""
    namespace: OutputNamespace
    root: Path
    description: str
    user_scoped: bool = False
    allow_existing_legacy_path: bool = False


# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

# Subdirectory under base_dir for each namespace
_NAMESPACE_SUBDIR: dict[OutputNamespace, str] = {
    OutputNamespace.LIVE:                "live",
    OutputNamespace.HISTORICAL:          "backtest",
    OutputNamespace.SANDBOX:             "sandbox",
    OutputNamespace.POLICY:              "policy",
    OutputNamespace.PORTFOLIO:           "portfolio",
    OutputNamespace.LATEST:              "latest",
    OutputNamespace.USER:                "users",
    OutputNamespace.SIMULATION:          "simulation",
    OutputNamespace.PROMOTION_REVIEW:    "promotion_review",
    OutputNamespace.PROMOTION_APPROVALS: "promotion_approvals",
}

# Namespaces that include user_id as a path segment
_USER_SCOPED: frozenset[OutputNamespace] = frozenset({
    OutputNamespace.LIVE,
    OutputNamespace.USER,
})

# user_id must be safe for use as a filesystem path component
_SAFE_USER_ID_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")

# Reverse map: subdir name → namespace (for namespace_for_existing_path)
_SUBDIR_TO_NAMESPACE: dict[str, OutputNamespace] = {
    v: k for k, v in _NAMESPACE_SUBDIR.items()
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _validate_user_id(user_id: str) -> None:
    if not user_id or not _SAFE_USER_ID_RE.match(user_id):
        raise DataGovernanceError(
            f"Invalid user_id {user_id!r}: must be non-empty and match "
            r"[a-zA-Z0-9_\-\.]+ (no slashes, dots-only names, or traversal)"
        )


def _namespace_root(
    namespace: OutputNamespace,
    user_id: str,
    base_dir: Path,
) -> Path:
    """Return the root directory for a namespace, without creating it."""
    subdir = _NAMESPACE_SUBDIR[namespace]
    if namespace in _USER_SCOPED:
        return base_dir / subdir / user_id
    return base_dir / subdir


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_output_path(
    namespace: OutputNamespace,
    filename: str | Path,
    user_id: str = "owner",
    base_dir: Path | str = "outputs",
) -> Path:
    """
    Return the canonical output path for *filename* inside *namespace*.

    Does not create directories or validate the path — call
    :func:`ensure_output_dir` or :func:`safe_write_text` for that.

    Namespace → directory mapping::

        LIVE        → {base_dir}/live/{user_id}/{filename}
        HISTORICAL  → {base_dir}/backtest/{filename}
        SANDBOX     → {base_dir}/sandbox/{filename}
        POLICY      → {base_dir}/policy/{filename}
        PORTFOLIO   → {base_dir}/portfolio/{filename}
        LATEST      → {base_dir}/latest/{filename}
        USER        → {base_dir}/users/{user_id}/{filename}
    """
    _validate_user_id(user_id)
    root = _namespace_root(namespace, user_id, Path(base_dir))
    return root / filename


def validate_output_path(
    namespace: OutputNamespace,
    path: Path | str,
    user_id: str = "owner",
    base_dir: Path | str = "outputs",
) -> Path:
    """
    Assert that *path* falls inside the expected namespace directory.

    Resolves both paths to absolute form before comparison so that relative
    paths, symlinks, and ``..`` components are all normalised.

    Raises :exc:`DataGovernanceError` if:

    - ``user_id`` contains slashes or other unsafe characters
    - the resolved path is not under the namespace root
    - (path traversal attempts are caught by the containment check)

    Returns the resolved :class:`~pathlib.Path` when valid.
    """
    _validate_user_id(user_id)
    resolved_base = Path(base_dir).resolve()
    expected_root = _namespace_root(namespace, user_id, resolved_base).resolve()
    resolved_path = Path(path).resolve()

    if not resolved_path.is_relative_to(expected_root):
        raise DataGovernanceError(
            f"Path {str(path)!r} resolves to {resolved_path} which is not "
            f"within namespace {namespace.value!r} root {expected_root}. "
            "Path traversal or wrong namespace."
        )
    return resolved_path


def ensure_output_dir(
    namespace: OutputNamespace,
    filename: str | Path | None = None,
    user_id: str = "owner",
    base_dir: Path | str = "outputs",
) -> Path:
    """
    Create the namespace directory (and any missing parents).

    If *filename* is provided, create the parent directory for that specific
    file path inside the namespace.

    Returns the directory that was created or already existed.
    """
    _validate_user_id(user_id)
    root = _namespace_root(namespace, user_id, Path(base_dir))
    if filename is not None:
        target_dir = (root / filename).parent
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_write_text(
    namespace: OutputNamespace,
    filename: str | Path,
    content: str,
    user_id: str = "owner",
    base_dir: Path | str = "outputs",
    encoding: str = "utf-8",
) -> Path:
    """
    Write *content* to the canonical path for *filename* inside *namespace*.

    Steps:

    1. Compute expected path via :func:`get_output_path`.
    2. Validate the path via :func:`validate_output_path`.
    3. Create parent directory.
    4. Write text.
    5. Return the written path.

    Raises :exc:`DataGovernanceError` on any namespace violation.
    """
    out_path = get_output_path(namespace, filename, user_id=user_id, base_dir=base_dir)
    validate_output_path(namespace, out_path, user_id=user_id, base_dir=base_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write (Phase 1): serialize to a temp file in the SAME directory,
    # then os.replace() — an interrupted write never leaves a valid-looking
    # partial artifact and never clobbers a prior good artifact. The temp is
    # cleaned up on any failure so no ``.tmp`` debris is left behind.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(out_path.parent), prefix=f".{out_path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(content)
        os.replace(tmp_name, out_path)
    except BaseException:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return out_path


def safe_write_json(
    namespace: OutputNamespace,
    filename: str | Path,
    payload: Any,
    user_id: str = "owner",
    base_dir: Path | str = "outputs",
    indent: int = 2,
) -> Path:
    """
    Serialize *payload* to JSON and write it inside *namespace*.

    Identical contract to :func:`safe_write_text`; serializes with
    ``json.dumps`` using ``default=str`` for non-serialisable types.
    """
    content = json.dumps(payload, indent=indent, default=str)
    return safe_write_text(
        namespace, filename, content,
        user_id=user_id, base_dir=base_dir, encoding="utf-8",
    )


def namespace_for_existing_path(path: Path | str) -> OutputNamespace | None:
    """
    Detect which namespace *path* belongs to by inspecting its components.

    Checks each path part against the known namespace subdirectory names.
    Returns ``None`` if no known namespace is recognised.

    This is a best-effort heuristic for legacy path classification — it does
    not validate that the path is fully within the namespace.
    """
    parts = Path(path).parts
    for i, part in enumerate(parts):
        if part in _SUBDIR_TO_NAMESPACE:
            namespace = _SUBDIR_TO_NAMESPACE[part]
            # For "live" and "users" the subdirectory name alone is ambiguous;
            # require at least one more component (the user_id segment).
            if namespace in _USER_SCOPED and i + 1 >= len(parts):
                continue
            return namespace
    return None


# ---------------------------------------------------------------------------
# Policy registry (informational — not enforced at module level)
# ---------------------------------------------------------------------------

def get_policies(base_dir: Path | str = "outputs") -> dict[OutputNamespace, OutputPathPolicy]:
    """
    Return a dict of :class:`OutputPathPolicy` describing each namespace.

    Useful for auditing or generating documentation.
    """
    base = Path(base_dir)
    return {
        OutputNamespace.LIVE: OutputPathPolicy(
            namespace=OutputNamespace.LIVE,
            root=base / "live",
            description="Future live pipeline outputs, scoped per user",
            user_scoped=True,
        ),
        OutputNamespace.HISTORICAL: OutputPathPolicy(
            namespace=OutputNamespace.HISTORICAL,
            root=base / "backtest",
            description="Historical replay outputs — never mixed with live",
            allow_existing_legacy_path=True,
        ),
        OutputNamespace.SANDBOX: OutputPathPolicy(
            namespace=OutputNamespace.SANDBOX,
            root=base / "sandbox",
            description="Discovery / test outputs pending promotion",
        ),
        OutputNamespace.POLICY: OutputPathPolicy(
            namespace=OutputNamespace.POLICY,
            root=base / "policy",
            description="Policy evaluation, calibration, and coverage outputs",
            allow_existing_legacy_path=True,
        ),
        OutputNamespace.PORTFOLIO: OutputPathPolicy(
            namespace=OutputNamespace.PORTFOLIO,
            root=base / "portfolio",
            description="Portfolio snapshot and summary reports",
            allow_existing_legacy_path=True,
        ),
        OutputNamespace.LATEST: OutputPathPolicy(
            namespace=OutputNamespace.LATEST,
            root=base / "latest",
            description="Current-run decision artifacts (decision_plan, memo, etc.)",
            allow_existing_legacy_path=True,
        ),
        OutputNamespace.USER: OutputPathPolicy(
            namespace=OutputNamespace.USER,
            root=base / "users",
            description="Future per-user scoped outputs",
            user_scoped=True,
        ),
        OutputNamespace.SIMULATION: OutputPathPolicy(
            namespace=OutputNamespace.SIMULATION,
            root=base / "simulation",
            description="Active simulation/test lane — consolidated daily simulation bundle",
        ),
        OutputNamespace.PROMOTION_REVIEW: OutputPathPolicy(
            namespace=OutputNamespace.PROMOTION_REVIEW,
            root=base / "promotion_review",
            description="AI/product review packet, verdicts, and pending production proposals",
        ),
        OutputNamespace.PROMOTION_APPROVALS: OutputPathPolicy(
            namespace=OutputNamespace.PROMOTION_APPROVALS,
            root=base / "promotion_approvals",
            description="Human approvals + production-application audit trail and snapshots",
        ),
    }
