"""Defect & Intervention Journal — an append-only JSONL file and nothing more.

WHAT THIS IS NOT.

It is not a service, a database, a classifier, an agent, or a workflow engine.
It has no authority: nothing reads it to decide whether work may proceed, and
writing an entry neither blocks nor permits anything. It is contemporaneous
evidence about how the system actually behaved, written down while the details
are still true, so that later analysis is not reconstructed from memory.

WHY THE VAGUE VALUES ARE ALLOWED ON PURPOSE.

``unclear``, ``unknown`` and ``not_yet_determined`` are first-class values for
every interpretive field. A journal that forces each observation into a
confident taxonomy produces confident wrong taxonomies: the honest answer at
the moment of writing is often that nobody knows yet what class of defect this
was, or whether it could have been caught earlier. Recording that is more
useful than a guess that later reads as a finding.

``experimental_noncanonical``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "engineering.defect_intervention_journal.v0"
SCHEMA_KIND = "experimental_noncanonical"

DEFAULT_REL = "docs/DEFECT_INTERVENTION_JOURNAL.jsonl"

#: Permitted everywhere an interpretation is asked for. Not a fallback the
#: writer may reach for silently -- it must be chosen.
UNRESOLVED_VALUES = ("unclear", "unknown", "not_yet_determined")

#: Every field an entry carries. Required ones have no default because an
#: entry that cannot say what happened is not evidence.
REQUIRED_FIELDS = (
    "journal_id",
    "timestamp",
    "mission",
    "component",
    "what_happened",
    "expected_behavior",
    "actual_behavior",
    "first_detector",
)

OPTIONAL_FIELDS = (
    "task_or_experiment",
    "candidate_sha",
    "detection_timing",
    "controls_with_opportunity",
    "control_outcome",
    "provisional_defect_class",
    "earlier_prevention_candidate",
    "human_intervention",
    "intervention_reason",
    "human_decision",
    "plausibly_automatable",
    "resolution",
    "regression_evidence_added",
    "mutation_candidate",
    "notes",
)

ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS


class JournalError(ValueError):
    """An entry that would be misleading is refused rather than written."""


def validate_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Refuse entries that are unreadable later. Interpretation is never forced."""
    unknown = sorted(set(entry) - set(ALL_FIELDS) - {"schema_version", "schema_kind"})
    if unknown:
        raise JournalError(
            f"unknown journal field(s) {unknown}; the journal is a fixed record, "
            "not a place to invent structure per entry")
    for field in REQUIRED_FIELDS:
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            raise JournalError(
                f"{field} is required and must be a non-empty string: an entry "
                "that cannot say what happened is not evidence")
    out = {"schema_version": SCHEMA_VERSION, "schema_kind": SCHEMA_KIND}
    for field in ALL_FIELDS:
        if field in entry:
            out[field] = entry[field]
    return out


def append_entry(entry: Mapping[str, Any], *, repo_root: Path,
                 rel: str = DEFAULT_REL) -> dict[str, Any]:
    """Append one validated entry. Append-only: existing lines are never read,
    rewritten or reordered, so a bad writer cannot silently edit history."""
    record = validate_entry(entry)
    path = Path(repo_root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, separators=(",", ":"))
    if "\n" in line:
        raise JournalError("a journal entry must serialize to a single line")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return record


def read_entries(*, repo_root: Path, rel: str = DEFAULT_REL) -> list[dict[str, Any]]:
    path = Path(repo_root) / rel
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]
