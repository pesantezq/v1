"""Runtime-mutable vs release-immutable path classification.

Why this exists
---------------
Production ran directly out of a long-lived ``git`` checkout at ``/opt/stockbot``
and wrote its generated artifacts back into that same tree. Twelve of those
generated artifacts were *tracked*, so ordinary operation left the checkout
permanently dirty — eleven of the twelve were observed modified during a single
normal production day. ``.gitignore`` cannot fix that: it has no effect on a
file git already tracks.

That made the target invariant unreachable:

    production_code_sha == approved_release_sha AND no tracked code drift

Eleven were untracked (see ``RUNTIME_GENERATED_ARTIFACTS``). The twelfth,
VS-001's frozen evidence, is deliberately still tracked — but production no
longer writes it, because the live signal-outcomes producer was repointed to a
runtime path (see ``portfolio_automation/signal_outcomes_paths.py``). Moving the
*writer* rather than the evidence is what let the invariant hold without
disturbing a completed experiment.

Classification
--------------
``RUNTIME_MUTABLE``                production may write here; the release must
                                   not track it.
``RELEASE_IMMUTABLE``              part of the approved release; production must
                                   never write.
``IMMUTABLE_EXPERIMENT_EVIDENCE``  committed evidence of a completed experiment
                                   sitting under a runtime root. Tracked on
                                   purpose; production must never write it.

A path that is none of these is ``UNCLASSIFIED`` — deliberately not an error,
because the repository has many paths (tooling, CI config) that production
neither reads nor writes. Only the named classes carry guarantees.
"""
from __future__ import annotations

from pathlib import PurePosixPath

RUNTIME_MUTABLE = "RUNTIME_MUTABLE"
RELEASE_IMMUTABLE = "RELEASE_IMMUTABLE"
#: Committed evidence of a completed experiment that happens to sit under a
#: runtime root. Tracked on purpose, immutable by contract, and production must
#: never write it. Distinct from RUNTIME_GENERATED so the guard can forbid the
#: latter under outputs/ without also forbidding legitimate frozen evidence.
IMMUTABLE_EXPERIMENT_EVIDENCE = "IMMUTABLE_EXPERIMENT_EVIDENCE"
UNCLASSIFIED = "UNCLASSIFIED"

# Roots production writes into during ordinary operation. At the time of this
# contract these held 12 / 0 / 0 tracked files. All eleven production-generated
# artifacts are now untracked; the single remaining tracked file under
# ``outputs/`` is VS-001's frozen evidence, which is
# IMMUTABLE_EXPERIMENT_EVIDENCE rather than runtime output and which production
# no longer writes (the live producer was repointed — see
# portfolio_automation/signal_outcomes_paths.py).
#
# So the invariant is: zero tracked RUNTIME_GENERATED artifacts anywhere, and
# the tracked set under a runtime root is exactly the immutable evidence
# allowlist. A blanket "nothing tracked under outputs/" would be wrong: it
# would forbid legitimate frozen experiment evidence.
RUNTIME_ROOTS: tuple[str, ...] = (
    "data/",
    "outputs/",
    "logs/",
)

# Additional writable locations that are not under a runtime root.
_RUNTIME_MUTABLE_EXTRA: tuple[str, ...] = (
    ".pytest_cache/",
    "__pycache__/",
)

# Source of the approved release. Production reads these and must never write
# them; a runtime write here is precisely the drift this contract forbids.
_RELEASE_IMMUTABLE_PREFIXES: tuple[str, ...] = (
    "agent/",
    "config/",
    "docs/",
    "evals/",
    "gui/",
    "gui_v2/",
    "policy_evaluator/",
    "portfolio_automation/",
    "scripts/",
    "tests/",
    "theme_engine/",
    "tools/",
    "watchlist_scanner/",
)

# Tracked single files that belong to the release.
_RELEASE_IMMUTABLE_FILES: tuple[str, ...] = (
    ".gitignore",
    "main.py",
    "requirements.txt",
)

# Eleven generated artifacts removed from git authority by this contract.
#
# Each was verified to have a producer in production code, consumers that
# tolerate absence (``_load_json`` returns ``{}``; ``gui_operator_data`` guards
# with ``exists()``), **no test that reads the committed copy**, and no
# human-authored content — all were last committed by ``bfeee70 chore: sync
# cron + monthly-analysis run artifacts``, a runtime sync rather than authorship.
#
# The "no test reads the committed copy" claim is the one that matters, and it
# is the one an earlier pass of this work got wrong by grepping with a capped
# result list. It is now established by running the full suite against a tree
# with these files absent. An earlier pass also wrongly untracked
# portfolio_snapshot.json; its test coupling to the committed copy has since
# been replaced by an explicit fixture (tests/fixtures/portfolio_snapshot_sample.json).
RUNTIME_GENERATED_ARTIFACTS: tuple[str, ...] = (
    "outputs/performance/allocation_policy_preview.json",
    "outputs/performance/allocation_policy_simulation.json",
    "outputs/performance/performance_summary.json",
    "outputs/performance/weight_tuning_suggestions.json",
    "outputs/portfolio/portfolio_snapshot.json",
    "outputs/portfolio/portfolio_summary.md",
    "outputs/regime/regime_performance.json",
    "outputs/regime/regime_performance.md",
    "outputs/sandbox/discovery/automatic_promotion_candidates.json",
    "outputs/sandbox/discovery/automatic_promotion_decisions.jsonl",
    "outputs/sandbox/discovery/automatic_promotion_summary.md",
)

#: Committed evidence of completed experiments that sits under a runtime root.
#:
#: These stay tracked deliberately. They are NOT runtime output: production does
#: not write them, and their bytes are load-bearing for a frozen experiment.
#:
#: The collision that used to exist here has been resolved by moving the
#: *writer*, not the evidence. VS-001's EVIDENCE_REL is unchanged and its
#: recorded evidence_sha256 / freeze_digest still verify, while the live
#: signal-outcomes producer now writes
#: ``outputs/runtime/performance/signal_outcomes.csv`` — see
#: portfolio_automation/signal_outcomes_paths.py for why that direction was the
#: only safe one.
IMMUTABLE_EVIDENCE_ARTIFACTS: dict[str, str] = {
    "outputs/performance/signal_outcomes.csv": (
        "VS-001 frozen evidence. vertical_slice.preregistration.EVIDENCE_REL "
        "names this exact path and evidence_digest() is sha256 of the file on "
        "disk, so the bytes are content-addressed into the preregistration and "
        "its registered freeze_digest. VS-002 feasibility reads the same bytes "
        "to keep VS-002_blocked.json reproducible. Production no longer writes "
        "here; the live projection goes to signal_outcomes_paths.RUNTIME_REL."
    ),
}


def _normalise(rel_path: str) -> str:
    """Normalise to a forward-slash relative path without a leading ``./``.

    Rejects absolute paths and ``..`` traversal so a caller cannot classify a
    path outside the release tree and act on the answer.
    """
    text = str(rel_path).replace("\\", "/").strip()
    if not text:
        raise ValueError("empty path")
    if text.startswith("/"):
        raise ValueError(f"expected a release-relative path, got absolute: {rel_path!r}")
    parts = PurePosixPath(text).parts
    if ".." in parts:
        raise ValueError(f"path escapes the release tree: {rel_path!r}")
    return "/".join(p for p in parts if p not in (".",))


def classify_path(rel_path: str) -> str:
    """Classify a release-relative path.

    Immutable experiment evidence is checked first: it lives under a runtime
    root but is emphatically not runtime-mutable, and getting that order wrong
    would classify VS-001's frozen evidence as something production may write.
    """
    norm = _normalise(rel_path)
    if norm in IMMUTABLE_EVIDENCE_ARTIFACTS:
        return IMMUTABLE_EXPERIMENT_EVIDENCE
    for prefix in RUNTIME_ROOTS + _RUNTIME_MUTABLE_EXTRA:
        if norm.startswith(prefix):
            return RUNTIME_MUTABLE
    if norm in _RELEASE_IMMUTABLE_FILES:
        return RELEASE_IMMUTABLE
    for prefix in _RELEASE_IMMUTABLE_PREFIXES:
        if norm.startswith(prefix):
            return RELEASE_IMMUTABLE
    return UNCLASSIFIED


def is_runtime_mutable(rel_path: str) -> bool:
    """True when production may write this path without dirtying the release."""
    return classify_path(rel_path) == RUNTIME_MUTABLE


def runtime_root_of(rel_path: str) -> str | None:
    """Return the runtime root containing ``rel_path``, or None."""
    norm = _normalise(rel_path)
    for prefix in RUNTIME_ROOTS:
        if norm.startswith(prefix):
            return prefix
    return None
