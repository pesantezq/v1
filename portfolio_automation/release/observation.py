"""Provenance that ties independently-collected gate evidence to one observation.

Why this exists
---------------
Production release identity is the conjunction of three gates that are
deliberately independent — ``SYSTEMD_UNIT_VALIDITY``, ``SCHEDULER_ALIGNMENT``
and ``RELEASE_POINTER_IDENTITY``. Independence is the point: none may infer
another. But independence at the *decision* layer created a hole at the
*evidence* layer, because three separately-collected results carry no
statement about what they were collected from.

A genuine ``SYSTEMD_UNIT_VALIDITY = PASS`` taken on a staging box covers the
same unit NAMES as production. Composed with production scheduler and pointer
evidence it yielded ``production_release_identity: PASS`` — three green gates
describing two different machines::

    staging   systemd validity     PASS
    production scheduler alignment PASS   ->  false aggregate PASS
    production pointer identity    PASS

Being green is not enough. The three results must be *about the same
observation of the same host*.

Host identity alone does not establish that, and neither does a timestamp. Two
independent observations of the same production host minutes apart are still
two observations: a pointer read before a deploy and a unit verification after
it are each individually truthful and jointly describe a system that never
existed. So the binding is ``(host, observation_id)``, where the id names one
evidence-collection run.

Where the id comes from
-----------------------
The identifier is issued by the collection flow, ONCE, and passed to each
collector — it is never minted here and never stamped onto a result after the
fact. An aggregator that invents a shared id would be manufacturing exactly the
agreement it is supposed to be checking, which is why nothing in this module
can generate one.

Absence is not tolerance. Evidence that cannot say which observation it belongs
to cannot be composed, and the aggregate reports ``NOT_ESTABLISHED`` rather
than assuming co-location. That is a deliberate cost: a flow that does not yet
thread an observation id through all three gates cannot certify production
identity until it does.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Shape of an observation identifier. Deliberately permissive about WHAT the
#: id is (a UUID, a run number, a timestamped slug all qualify) and strict about
#: it being a single substantial token: anything blank, padded or punctuation-
#: only would let unrelated runs "agree" by both being empty.
OBSERVATION_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:+-]{7,63}\Z")

#: Key names used wherever a gate result carries its provenance inline.
HOST_KEY = "host"
OBSERVATION_ID_KEY = "observation_id"


@dataclass(frozen=True)
class ObservationContext:
    """The host an observation was taken from, and which run took it."""

    host: str
    observation_id: str

    def as_dict(self) -> dict:
        return {HOST_KEY: self.host, OBSERVATION_ID_KEY: self.observation_id}


def observation_of(result: dict | None) -> ObservationContext:
    """Read the provenance a gate result carries. Missing fields stay empty.

    Never raises and never substitutes a default: an absent host or id must
    reach :func:`bind_observations` as absent, so it is reported rather than
    quietly satisfied.
    """
    data = result or {}
    return ObservationContext(
        host=str(data.get(HOST_KEY) or "").strip(),
        observation_id=str(data.get(OBSERVATION_ID_KEY) or "").strip(),
    )


def bind_observations(contexts: dict[str, ObservationContext]) -> list[str]:
    """Errors preventing these gate results from describing one observation.

    Empty list means every context is usable and they all agree. Anything else
    means the aggregate must not be established.
    """
    problems: list[str] = []
    for gate, ctx in contexts.items():
        if not (ctx.host or "").strip():
            problems.append(
                f"{gate}: evidence records no host — it cannot be shown to "
                f"describe the production system the other gates observed"
            )
        if not (ctx.observation_id or "").strip():
            problems.append(
                f"{gate}: evidence records no observation id — a gate result "
                f"that cannot say which collection run produced it cannot be "
                f"composed with another, however green it is"
            )
        elif not OBSERVATION_ID.match(ctx.observation_id.strip()):
            problems.append(
                f"{gate}: observation id {ctx.observation_id!r} is not a usable "
                f"identifier, so it cannot establish shared provenance"
            )
    if problems:
        return problems

    distinct = {(c.host, c.observation_id) for c in contexts.values()}
    if len(distinct) > 1:
        detail = "; ".join(
            f"{gate} = {ctx.host}@{ctx.observation_id}"
            for gate, ctx in sorted(contexts.items())
        )
        problems.append(
            "gate evidence comes from more than one observation, so the three "
            "results do not describe a single production context: " + detail
        )
    return problems
