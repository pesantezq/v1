"""Append-only record of what a real crash proved, and what it left unknown.

WHY A TYPE RATHER THAN PROSE.

The 2026-08-16 machine crash is the only evidence this project has that its
durability design works under real process loss. Written as prose it would decay
into a claim; written as a validated record it stays checkable.

The format forces three things apart:

  * an OBSERVED FACT must cite a line and a JSON path in a sha-pinned file and
    quote the value verbatim. An uncited fact is rejected at construction --
    "no citation, no fact" is enforced, not encouraged.
  * a DERIVED CONCLUSION may reference facts only, never other conclusions.
    Conclusion chains hide their weakest link.
  * an UNRESOLVED GAP names a question the evidence cannot answer.

Confidence admits only ENTAILED and PROBABLE. There is deliberately no
SPECULATIVE: anything weaker than probable is a gap, so the format cannot
express a guess wearing a conclusion's clothes.

ABSENCE IS NOT A FACT. For a ledger written before the write-ahead contract
existed, "no ReviewerCalled record" proves nothing about whether a reviewer ran,
so it is only expressible as a gap.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Sequence

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER

SCHEMA_KIND = EXPERIMENTAL_MARKER
OBSERVATION_SCHEMA_VERSION = "engineering.crash_observation.v1"


class Confidence(str, Enum):
    ENTAILED = "ENTAILED"
    PROBABLE = "PROBABLE"


class ObservationError(ValueError):
    pass


@dataclass(frozen=True)
class Citation:
    ledger_rel: str
    line: int
    json_path: str

    def to_dict(self) -> dict[str, Any]:
        return {"ledger_rel": self.ledger_rel, "line": self.line,
                "json_path": self.json_path}


@dataclass(frozen=True)
class ObservedFact:
    fact_id: str
    statement: str
    citation: Citation
    verbatim_value: Any

    def __post_init__(self) -> None:
        if not self.fact_id or not self.statement:
            raise ObservationError("a fact needs an id and a statement")
        if not isinstance(self.citation, Citation) or self.citation.line < 1:
            raise ObservationError(
                f"fact {self.fact_id!r} has no line-level citation; an uncited "
                "observation is not a fact")

    def to_dict(self) -> dict[str, Any]:
        return {"fact_id": self.fact_id, "statement": self.statement,
                "citation": self.citation.to_dict(),
                "verbatim_value": self.verbatim_value}


@dataclass(frozen=True)
class DerivedConclusion:
    conclusion_id: str
    statement: str
    from_facts: tuple[str, ...]
    inference: str
    confidence: Confidence

    def to_dict(self) -> dict[str, Any]:
        return {"conclusion_id": self.conclusion_id, "statement": self.statement,
                "from_facts": list(self.from_facts), "inference": self.inference,
                "confidence": self.confidence.value}


@dataclass(frozen=True)
class UnresolvedGap:
    gap_id: str
    question: str
    why_unresolvable: str
    what_would_resolve_it: str

    def to_dict(self) -> dict[str, Any]:
        return {"gap_id": self.gap_id, "question": self.question,
                "why_unresolvable_from_this_evidence": self.why_unresolvable,
                "what_would_resolve_it": self.what_would_resolve_it}


def ledger_digest(path: Path) -> str:
    """Pin WHAT was read. An observation about a file that has since changed is
    an observation about nothing."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class CrashObservation:
    observed_at_date: str
    observed_session_id: str
    observed_ledger_rel: str
    observed_ledger_sha256: str
    facts: tuple[ObservedFact, ...] = ()
    conclusions: tuple[DerivedConclusion, ...] = ()
    gaps: tuple[UnresolvedGap, ...] = ()

    def __post_init__(self) -> None:
        fact_ids = {f.fact_id for f in self.facts}
        if len(fact_ids) != len(self.facts):
            raise ObservationError("duplicate fact_id")
        conclusion_ids = {c.conclusion_id for c in self.conclusions}
        for c in self.conclusions:
            if not c.from_facts:
                raise ObservationError(
                    f"conclusion {c.conclusion_id!r} derives from nothing")
            for ref in c.from_facts:
                if ref in conclusion_ids:
                    raise ObservationError(
                        f"conclusion {c.conclusion_id!r} references another "
                        "conclusion; chains hide their weakest link")
                if ref not in fact_ids:
                    raise ObservationError(
                        f"conclusion {c.conclusion_id!r} cites unknown fact {ref!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "RealMachineCrashRecoveryObservation",
                "observation_schema": OBSERVATION_SCHEMA_VERSION,
                "schema_kind": SCHEMA_KIND,
                "observed_at_date": self.observed_at_date,
                "observed_session_id": self.observed_session_id,
                "observed_ledger_rel": self.observed_ledger_rel,
                "observed_ledger_sha256": self.observed_ledger_sha256,
                "observed_facts": [f.to_dict() for f in self.facts],
                "derived_conclusions": [c.to_dict() for c in self.conclusions],
                "unresolved_gaps": [g.to_dict() for g in self.gaps]}
