"""The two signal-outcomes paths, and why there are two.

One path used to serve two incompatible roles.

``outputs/performance/signal_outcomes.csv`` is the **frozen evidence** of the
VS-001 experiment. Its bytes are content-addressed into the preregistration:
``vertical_slice.preregistration.evidence_digest()`` is sha256 of that file as
it sits on disk, and the recorded ``evidence_sha256`` /
``freeze_digest`` in ``evals/vertical_slice/VS-001_preregistration.json`` are
derived from it. Editing the file invalidates a completed experiment.

But the daily production pipeline **rewrote that same path on every run**. So
on the live host the VS-001 freeze was already being invalidated daily, and the
release checkout went dirty during ordinary operation — the defect that made
``no unauthorized tracked code drift`` unreachable.

The split
---------
``FROZEN_EVIDENCE_REL``  immutable historical evidence. Tracked in git,
                         classified IMMUTABLE_EXPERIMENT_EVIDENCE, read by
                         VS-001 preregistration and VS-002 feasibility.
                         **Production never writes it.**

``RUNTIME_REL``          the live, churning projection. Never tracked. Written
                         by the signal-feedback producer, read by every
                         operational consumer.

Why a new ``outputs/runtime/`` root rather than reusing ``outputs/latest/``:
the distinction being encoded *is* runtime-versus-frozen, so it belongs in the
path where it cannot be misread. ``outputs/latest/`` already carries many
artifacts with their own semantics — and the stale docstring in
``universe_sanitation`` claiming signal outcomes live there is evidence that
overloading it already caused confusion.

No history is lost by moving the live path. The producer rebuilds the CSV in
full from ``WatchlistStateStore`` on every run
(``rows = store.list_signal_feedback(limit=10000)``) and never reads the CSV
back, so the database is the source of truth and the first run after a cutover
repopulates the runtime path completely.

There is deliberately **no fallback** from the runtime path to the frozen file.
A fallback would let production silently render 2026-06 frozen evidence as
current data, which is a silent-staleness defect. Absent runtime data reports
as absent — every consumer already degrades gracefully — and self-heals on the
next run.
"""
from __future__ import annotations

from pathlib import Path

#: Immutable VS-001 evidence. Do not write. Do not move: the VS-001
#: preregistration's EVIDENCE_REL and recorded evidence_sha256 both name it.
FROZEN_EVIDENCE_REL = "outputs/performance/signal_outcomes.csv"

#: The live projection production writes and operational consumers read.
RUNTIME_REL = "outputs/runtime/performance/signal_outcomes.csv"

#: Ignored root for runtime-only outputs.
RUNTIME_ROOT_REL = "outputs/runtime"


def frozen_evidence_path(root: str | Path = ".") -> Path:
    """The immutable VS-001 evidence file. Read-only by contract."""
    return Path(root) / FROZEN_EVIDENCE_REL


def runtime_path(root: str | Path = ".") -> Path:
    """The live signal-outcomes projection for the given repository root."""
    return Path(root) / RUNTIME_REL


def runtime_path_from_performance_dir(performance_dir: str | Path) -> Path:
    """Place the runtime CSV as a sibling of the given performance directory.

    The signal-feedback producer is addressed by ``output_dir`` (which still
    owns ``performance_summary.json``), not by a repository root — so the
    runtime path has to be derived from it.

    Deliberately **one** level up, not two. Walking up two levels to recover a
    repository root only works when ``output_dir`` is repo-shaped, and it is
    not always: ``watchlist_scanner`` computes
    ``Path(output_dir).parent / "performance"``, so a caller passing
    ``output_dir="/tmp/ws_test_out"`` yields ``/tmp/performance`` — whose
    grandparent is ``/``. That produced an attempt to create ``/outputs`` and
    is exactly the class of breakage a root-guessing heuristic invites.

    For the production shape ``<root>/outputs/performance`` this returns
    ``<root>/outputs/runtime/performance/signal_outcomes.csv``, i.e. exactly
    ``RUNTIME_REL``; for any other shape it stays contained beside the
    directory the caller already chose.
    """
    perf = Path(performance_dir)
    return perf.parent / "runtime" / perf.name / "signal_outcomes.csv"
