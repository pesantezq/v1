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

Why an id is still not enough
-----------------------------
A shared ``(host, observation_id)`` proves the collectors were *told* they
belong to one run. It cannot prove the system held still during it, and the
gates read different things:

* the pointer gate reads the release pointer;
* the scheduler gate reads unit ``ExecStart`` text and cron;
* the validity gate reads unit files through ``systemd-analyze``.

Unit files under ``/etc/systemd/system`` are **not** part of the release, so
binding the gates to a release SHA leaves this open::

    scheduler evidence collected     unit A is aligned
    fragment replaced                unit B, syntactically valid, NOT aligned
    validity evidence collected      unit B verifies
    pointer SHA                      unchanged throughout
                                     ->  three green gates, and B is installed

Scheduler certified A. B is what production runs.

So the flow also brackets the collection: it measures the mutation witnesses of
the relevant configuration BEFORE the first gate is collected and AFTER the
last one completes, and every artifact carries that pair. Equal endpoints mean
nothing the gates depend on mutated between them.

The anchors are owned by the COLLECTION FLOW, not by the aggregate's caller,
and they are read back off the artifacts — never accepted as a parameter. A
caller who could hand in an anchor could hand in agreement, which is the thing
being checked. That is the same reason nothing here can mint an observation id.

What the bracket does and does not claim
----------------------------------------
It is not a filesystem snapshot and not a transactional one. It establishes
that the measured witnesses — inode, size, nanosecond mtime/ctime, and the
directory entries of every applicable unit load and drop-in directory — read
identically at both endpoints. Those witnesses are not restorable by ordinary
means (no userspace API sets ctime; a rename changes the inode; the kernel
stamps a directory on link/unlink), which is what makes endpoint sampling
sound about the interval rather than merely about its ends.

It does **not** defend against a privileged actor forging evidence, moving the
system clock, or mutating state through a path outside the measured set.
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


#: Shape of a configuration anchor. The collection flow produces an opaque
#: digest; this only insists it IS one, so an empty or placeholder value cannot
#: let two unrelated runs "agree" by both carrying nothing.
CONFIGURATION_ANCHOR = re.compile(r"\A[A-Za-z0-9]{16,128}\Z")

#: Key names used wherever an artifact carries its bracket inline.
ANCHOR_BEFORE_KEY = "configuration_anchor_before"
ANCHOR_AFTER_KEY = "configuration_anchor_after"


@dataclass(frozen=True)
class ConfigurationBracket:
    """What the configuration measured at both ends of one collection flow."""

    before: str
    after: str

    @property
    def is_closed(self) -> bool:
        """Both endpoints present, well-formed, and equal."""
        return (bool(self.before) and self.before == self.after
                and bool(CONFIGURATION_ANCHOR.match(self.before)))

    def as_dict(self) -> dict:
        return {ANCHOR_BEFORE_KEY: self.before, ANCHOR_AFTER_KEY: self.after}


def bracket_of(result: dict | None) -> ConfigurationBracket:
    """Read the bracket an artifact carries. Missing fields stay empty.

    Never raises and never substitutes a default, for the same reason
    :func:`observation_of` does not: an absent anchor must reach
    :func:`bind_brackets` as absent so it is reported rather than satisfied.
    """
    data = result or {}
    return ConfigurationBracket(
        before=str(data.get(ANCHOR_BEFORE_KEY) or "").strip(),
        after=str(data.get(ANCHOR_AFTER_KEY) or "").strip(),
    )


def bind_brackets(brackets: dict[str, ConfigurationBracket]) -> list[str]:
    """Errors preventing these artifacts from sharing one closed bracket.

    Empty list means every artifact was produced inside the same collection
    flow AND the configuration those gates depend on did not mutate between the
    flow's endpoints. Anything else means the aggregate must not be established.
    """
    problems: list[str] = []
    for gate, bracket in sorted(brackets.items()):
        if not bracket.before or not bracket.after:
            problems.append(
                f"{gate}: evidence carries no configuration bracket — without "
                f"anchors measured before and after the whole collection, a "
                f"unit file replaced between two gates would leave both of "
                f"them individually truthful and jointly wrong"
            )
            continue
        for label, value in ((ANCHOR_BEFORE_KEY, bracket.before),
                             (ANCHOR_AFTER_KEY, bracket.after)):
            if not CONFIGURATION_ANCHOR.match(value):
                problems.append(
                    f"{gate}: {label} {value!r} is not a usable anchor, so it "
                    f"cannot establish that the configuration held still"
                )
        if bracket.before != bracket.after:
            problems.append(
                f"{gate}: the configuration changed during collection "
                f"({bracket.before[:12]} -> {bracket.after[:12]}) — the unit "
                f"files, drop-ins or release pointer these gates depend on "
                f"were not the same at both ends of the run"
            )
    if problems:
        return problems

    distinct = {(b.before, b.after) for b in brackets.values()}
    if len(distinct) > 1:
        detail = "; ".join(
            f"{gate} = {b.before[:12]}..{b.after[:12]}"
            for gate, b in sorted(brackets.items())
        )
        problems.append(
            "gate evidence carries more than one configuration bracket, so the "
            "three results were not produced by one bracketed collection "
            "however they are labelled: " + detail
        )
    return problems
