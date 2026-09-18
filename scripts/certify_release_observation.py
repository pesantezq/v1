#!/usr/bin/env python3
"""Turn ONE bracketed collection into three stamped artifacts and an aggregate.

The collection flow (``collect_release_observation.sh``) measures the
configuration anchors that span every gate. This reads that stream, builds the
scheduler, pointer and validity artifacts from it, stamps each with the same
``(host, observation_id)`` and the same bracket, and only then aggregates.

Nothing here touches production, and nothing here invents provenance. A field
the stream does not carry stays absent so the aggregate reports it, rather than
being defaulted into agreement -- an aggregate that repaired its own inputs
would be manufacturing the very consistency it exists to check.
"""
from __future__ import annotations

import argparse
import re
import json
import os
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.release import contracts as C  # noqa: E402
from portfolio_automation.release import pointer as P  # noqa: E402
from portfolio_automation.release import scheduler as S  # noqa: E402
from portfolio_automation.release.scheduler import EXEC_DIRECTIVES  # noqa: E402
from portfolio_automation.release.exec_manifest import (  # noqa: E402
    MANIFEST as EXEC_MANIFEST, directives_in_manifest)
from portfolio_automation.release import systemd_validity as V  # noqa: E402
from portfolio_automation.release.observation import ObservationContext  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import certify_systemd_validity as CV  # noqa: E402
from certify_systemd_validity import APPROVED_NAMESPACES  # noqa: E402

#: The ORDER a release observation must arrive in. Section counts prove the
#: pieces exist; only order proves the anchors bracketed anything. A stream
#: with both anchors moved to the end contains exactly one of every required
#: section while neither one precedes a single gate -- measured, and it
#: certified. So the stream is parsed as a sequence, not a bag.
#:
#: Each entry is (section, required). Sections not listed here may appear
#: between them -- per-unit records, the pointer scalars -- but a listed
#: section may never appear out of this order.
OBSERVATION_ORDER = (
    ("HOST", True),
    ("OBSERVATION_ID", True),
    ("CHECKED_AT", True),
    ("CONFIGURATION_ANCHOR_BEFORE", True),
    ("RELEASE_DIRTY_BEFORE", True),
    ("RELEASE_MAX_CTIME_BEFORE", True),
    ("CRON_WITNESS_BEFORE", True),
    ("EXEC_DIRECTIVES", True),
    ("SCHEDULER_CRON", True),
    ("POINTER_PATH", True),
    ("VALIDITY_BEGIN", True),
    ("VALIDITY_END", True),
    ("RELEASE_DIRTY_AFTER", True),
    ("RELEASE_MAX_CTIME_AFTER", True),
    ("CRON_WITNESS_AFTER", True),
    ("CONFIGURATION_ANCHOR_AFTER", True),
    ("OBSERVATION_END", True),
)

#: Sections that establish IDENTITY or the BRACKET rather than gate evidence.
#: Everything else the consumer reads is gate-bearing by definition -- the set
#: is DERIVED from what `parse_flow`/`build` actually consume (FLOW_SECTIONS
#: plus the repeatable per-unit blocks), not hand-listed, so a new evidence
#: section cannot be added to the consumer without automatically falling under
#: the bracket requirement.
BOUNDARY_SECTIONS = frozenset({
    "HOST", "OBSERVATION_ID", "CHECKED_AT",
    "CONFIGURATION_ANCHOR_BEFORE", "CONFIGURATION_ANCHOR_AFTER",
    "OBSERVATION_END",
})
#: Repeatable evidence blocks the consumer reads that are not run-level
#: scalars. SCHEDULER_UNIT appears once per unit by design.
REPEATABLE_GATE_SECTIONS = ("SCHEDULER_UNIT",)


def gate_bearing_sections() -> frozenset[str]:
    """Every section whose content can contribute to a gate verdict."""
    return (frozenset(FLOW_SECTIONS) | frozenset(REPEATABLE_GATE_SECTIONS)
            ) - BOUNDARY_SECTIONS - {"VALIDITY_BEGIN", "VALIDITY_END"}


#: Run-level sections of the OUTER flow. Each may appear exactly once, for the
#: same reason the validity capture's own sections may: a stream carrying two
#: runs cannot attribute its evidence to one, and `parse` keeps the last value
#: it sees, so the contradiction would be resolved silently by ordering.
FLOW_SECTIONS = (
    "HOST", "OBSERVATION_ID", "CHECKED_AT",
    "CONFIGURATION_ANCHOR_BEFORE", "CONFIGURATION_ANCHOR_AFTER",
    "RELEASE_ROOT", "RELEASES_ROOT", "SCHEDULER_CRON", "EXEC_DIRECTIVES",
    "RELEASE_DIRTY_BEFORE", "RELEASE_DIRTY_AFTER",
    "RELEASE_MAX_CTIME_BEFORE", "RELEASE_MAX_CTIME_AFTER",
    "CRON_WITNESS_BEFORE", "CRON_WITNESS_AFTER",
    "POINTER_PATH", "POINTER_IS_SYMLINK", "POINTER_RESOLVED",
    "POINTER_SHA", "POINTER_DIRTY",
    "VALIDITY_BEGIN", "VALIDITY_END", "OBSERVATION_END",
)


def split_validity(text: str) -> tuple[str, str]:
    """Separate the outer flow stream from the nested validity capture.

    The validity collector is REUSED rather than reimplemented, so its output
    is embedded whole -- including its own ``##HOST`` and ``##OBSERVATION_ID``.
    Those are not duplicates of the flow's own sections; they belong to a
    different stream and must be parsed as one, or each collector's uniqueness
    rules would fire on the other's records.
    """
    lines = text.splitlines()
    outer, inner = [], []
    depth = 0
    for line in lines:
        if line.strip() == "##VALIDITY_BEGIN":
            depth += 1
            outer.append(line)
            continue
        if line.strip() == "##VALIDITY_END":
            depth -= 1
            outer.append(line)
            continue
        (inner if depth > 0 else outer).append(line)
    return "\n".join(outer) + "\n", "\n".join(inner) + "\n"


def order_defects(outer: str) -> list[str]:
    """Ways the stream's SHAPE is wrong, independent of its contents.

    Ordering is evidence. An anchor that does not precede the gates has
    bracketed nothing, however present it is, so the sequence is validated as a
    sequence and never sorted into a valid one afterwards.
    """
    seen = [line[2:].split(" ", 1)[0].strip()
            for line in (outer or "").splitlines() if line.startswith("##")]
    expected = [name for name, required in OBSERVATION_ORDER if required]
    positions: dict[str, int] = {}
    for index, name in enumerate(seen):
        if name in expected and name not in positions:
            positions[name] = index

    defects = []
    missing = [name for name in expected if name not in positions]
    if missing:
        defects.append(
            f"observation stream is missing {', '.join(missing)} — an "
            f"incomplete bracket cannot establish that the configuration held "
            f"still")
        return defects

    previous_name, previous_index = expected[0], positions[expected[0]]
    for name in expected[1:]:
        if positions[name] < previous_index:
            defects.append(
                f"observation stream has ##{name} before ##{previous_name} — "
                f"the sections are out of order, and an anchor that does not "
                f"precede the gates it claims to bracket has bracketed nothing")
        previous_name, previous_index = name, positions[name]

    terminal = [name for name in seen if name == "OBSERVATION_END"]
    if terminal and seen[-1] != "OBSERVATION_END":
        defects.append(
            "observation stream continues after ##OBSERVATION_END — evidence "
            "recorded past the terminal marker was not part of the bracketed "
            "collection")

    # Every OCCURRENCE of every gate-bearing section must lie inside the
    # bracket. The pairwise check above orders the required singletons; it
    # cannot see a repeatable block (a SCHEDULER_UNIT appears once per unit by
    # design) or a duplicated scalar moved past an anchor, and `build()` would
    # consume the late copy -- gate evidence the anchors never bracketed.
    before_at = positions["CONFIGURATION_ANCHOR_BEFORE"]
    after_at = positions["CONFIGURATION_ANCHOR_AFTER"]
    gate_bearing = gate_bearing_sections()
    inner_depth = 0
    for index, name in enumerate(seen):
        # the nested validity capture repeats section names legitimately; its
        # own containment is enforced through the VALIDITY_BEGIN/END markers
        if name == "VALIDITY_BEGIN":
            inner_depth += 1
            continue
        if name == "VALIDITY_END":
            inner_depth -= 1
            continue
        if inner_depth > 0 or name not in gate_bearing:
            continue
        if index < before_at or index > after_at:
            side = ("before the opening anchor" if index < before_at
                    else "after the closing anchor")
            defects.append(
                f"observation stream has gate evidence ##{name} {side} — "
                f"evidence outside the configuration bracket was not proven "
                f"quiet and cannot contribute to release identity")
    return defects


def flow_defects(outer: str) -> list[str]:
    """Ways the outer stream is not one complete, ordered collection."""
    seen: dict[str, int] = {}
    for line in outer.splitlines():
        if not line.startswith("##"):
            continue
        name = line[2:].split(" ", 1)[0].strip()
        if name in FLOW_SECTIONS:
            seen[name] = seen.get(name, 0) + 1
    defects = [
        f"observation stream contains {count} ##{name} sections — one "
        f"bracketed collection records each once, so a stream carrying "
        f"several cannot attribute its evidence to a single run"
        for name, count in sorted(seen.items()) if count > 1
    ]
    defects.extend(order_defects(outer))

    # An unknown marker is malformed residue, not a section change: it flips
    # `section` in parse_flow and silently discards every following property
    # until the next recognised marker -- an aligned ExecStart followed by
    # ##IGNORED and a legacy ExecStop lost the legacy command entirely.
    known = frozenset(FLOW_SECTIONS) | {"SCHEDULER_UNIT"}
    for line in outer.splitlines():
        if line.startswith("##"):
            tokens = line[2:].split()
            name = tokens[0] if tokens else ""
            if name not in known:
                defects.append(
                    f"observation contains unknown section marker "
                    f"{line.strip()!r} — the collector never emits it, and "
                    f"treating it as a section change would silently truncate "
                    f"the evidence around it")
                continue
            # a known name with the wrong SHAPE is equally malformed: a bare
            # ##SCHEDULER_UNIT sets the unit to empty and every following
            # property is silently discarded
            expected_arity = 2 if name == "SCHEDULER_UNIT" else 1
            if len(tokens) != expected_arity:
                defects.append(
                    f"observation contains malformed section marker "
                    f"{line.strip()!r} — the collector emits ##{name} with "
                    f"{expected_arity - 1} operand(s), and a differently-"
                    f"shaped marker silently redirects the evidence after it")

    # One scheduler block per unit. `parse_flow` appends, so two blocks for
    # one unit would MERGE -- an old aligned block compensating for a later
    # one that reports the unit failed or gone, with the contradiction
    # resolved silently by concatenation. Same rule the validity capture's
    # per-unit sections already follow.
    scheduler_blocks: dict[str, int] = {}
    for line in outer.splitlines():
        if line.startswith("##SCHEDULER_UNIT "):
            name = line[len("##SCHEDULER_UNIT "):].strip()
            scheduler_blocks[name] = scheduler_blocks.get(name, 0) + 1
    for name, count in sorted(scheduler_blocks.items()):
        if count > 1:
            defects.append(
                f"observation stream contains {count} ##SCHEDULER_UNIT blocks "
                f"for {name} — a capture records each unit once, and merging "
                f"them would let earlier evidence compensate for a later "
                f"contradictory manager query")
    for required in ("HOST", "OBSERVATION_ID",
                     "CONFIGURATION_ANCHOR_BEFORE", "CONFIGURATION_ANCHOR_AFTER",
                     "OBSERVATION_END"):
        if seen.get(required, 0) == 0:
            defects.append(
                f"observation stream has no ##{required} — the collection did "
                f"not complete, and a truncated bracket cannot establish that "
                f"the configuration held still"
            )
    return defects


def parse_flow(outer: str) -> dict:
    """Read the outer stream's scalar sections and per-unit scheduler blocks."""
    scalars: dict[str, str] = {}
    units: dict[str, list[str]] = {}
    cron: list[str] = []
    release_dirty: dict[str, list[str]] = {"BEFORE": [], "AFTER": []}
    section = unit = ""
    for raw in outer.splitlines():
        if raw.startswith("##"):
            parts = raw[2:].split(" ", 1)
            section = parts[0].strip()
            unit = parts[1].strip() if len(parts) > 1 else ""
            if section == "SCHEDULER_UNIT" and unit:
                units.setdefault(unit, [])
            continue
        if section == "SCHEDULER_UNIT" and unit:
            units[unit].append(raw)
        elif section == "SCHEDULER_CRON":
            cron.append(raw)
        elif section in ("RELEASE_DIRTY_BEFORE", "RELEASE_DIRTY_AFTER"):
            # "clean" and "unreadable" are sentinels about the OBSERVATION,
            # not paths in the tree, so neither becomes a dirty entry. The
            # section is still recorded as a scalar so "the collector looked
            # and found nothing" stays distinguishable from "absent".
            if raw.strip():
                scalars.setdefault(section, "dirty"
                                   if raw.strip() not in ("unreadable", "clean")
                                   else raw.strip())
                if raw.strip() not in ("unreadable", "clean"):
                    release_dirty[section.rsplit("_", 1)[1]].append(raw)
        elif section in FLOW_SECTIONS and raw.strip():
            scalars.setdefault(section, raw.strip())
    return {"scalars": scalars, "units": units, "cron": "\n".join(cron),
            "release_dirty": release_dirty}


#: `systemctl show` reports ExecStart in a STRUCTURED form, not unit-file
#: syntax::
#:
#:     ExecStart={ path=/x/run.sh ; argv[]=/x/run.sh a b ; ignore_errors=no ; ... }
#:
#: Fed to `parse_systemd_unit` verbatim this yields an executable of ``{``,
#: which resolves to nothing and fails closed by accident rather than by
#: design -- and an artifact whose recorded surfaces are wrong is worse than
#: one that admits it could not read them. ``argv[]`` is the authoritative
#: command line the manager would execute, so that is what is recovered.
_ARGV = re.compile(r"argv\[\]=(.*?)(?:\s;\s|\s*\}$)")
_PATH_ONLY = re.compile(r"path=(\S+)")
#: One ``{ ... }`` command record. The D-Bus type behind these properties is an
#: ARRAY (``a(sasbttttuii)``), so one property line can legitimately carry
#: several records, and ``systemctl show`` also emits repeated property lines
#: -- both shapes were measured on systemd 255. Reading only the first record
#: reduces a list to its head: an aligned first command followed by a legacy
#: second one reconstructs as aligned-only, and the scheduler certifies a unit
#: whose later commands it never saw.
_EXEC_RECORD = re.compile(r"\{[^{}]*\}")
#: EnvironmentFiles=/path (ignore_errors=no) -> EnvironmentFile=/path
_ENVFILE = re.compile(r"^(\S+?)(?:\s*\(ignore_errors=\S+\))?$")


def _exec_commands(value: str) -> list[str]:
    """EVERY command line this Exec* property value carries, in order.

    A record that cannot be parsed is returned VERBATIM rather than dropped:
    its braces resolve to nothing under the scheduler's path semantics, so an
    unreadable command fails the certification closed instead of silently
    narrowing the evidence. Dropping it would make "could not read" look like
    "was not there".
    """
    value = value.strip()
    if not value.startswith("{"):
        return [value] if value else []
    # The brace records and the whitespace between them must consume the WHOLE
    # value. findall() would silently discard residue -- an appended
    # "TRAILING /path" after the records, or a damaged later record --
    # making "could not read that part" look like "it was not there". Residue
    # keeps the value verbatim, where it resolves to nothing and fails closed.
    if _EXEC_RECORD.sub("", value).strip():
        return [value]
    commands: list[str] = []
    for record in _EXEC_RECORD.findall(value):
        argv = _ARGV.search(record)
        if argv and argv.group(1).strip():
            commands.append(argv.group(1).strip())
            continue
        path = _PATH_ONLY.search(record)
        commands.append(path.group(1) if path else record)
    # A structured value with no extractable record at all is malformed
    # evidence; kept verbatim for the same fail-closed reason.
    return commands or [value]


def _unit_text(properties: list[str]) -> str:
    """Rebuild a [Service] stanza from `systemctl show` output.

    The surfaces are derived from what the MANAGER ACTUALLY LOADED rather than
    from a unit file re-read afterwards, which is the stronger evidence: a
    fragment replaced after loading would still show the loaded command here,
    and the configuration bracket is what catches that replacement.
    """
    body = []
    for line in properties:
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not value:
            continue
        if key in EXEC_DIRECTIVES:
            for command in _exec_commands(value):
                body.append(f"{key}={command}")
        elif key in ("WorkingDirectory", "RootDirectory",
                     "RootDirectoryStartOnly"):
            # RootDirectoryStartOnly is code identity: under `yes` the chroot
            # applies to ExecStart ONLY, so a non-start hook resolves on the
            # HOST. Dropping it here made the parser default to false and
            # treat a legacy ExecStop as safely chrooted under the release.
            body.append(f"{key}={value}")
        elif key == "EnvironmentFiles":
            match = _ENVFILE.match(value)
            if match:
                body.append(f"EnvironmentFile={match.group(1)}")
    return "[Service]\n" + "\n".join(body) + "\n"


def build(text: str, *, approved_sha: str, expected_origins: tuple[str, ...],
          expected_validity_units: tuple[str, ...],
          release_root: str = "/opt/stockbot/current",
          releases_root: str = "/opt/stockbot/releases") -> dict:
    outer, inner = split_validity(text)
    defects = flow_defects(outer)
    flow = parse_flow(outer)
    sc = flow["scalars"]

    # A capture that collected a NARROWER directive set than the certifier
    # treats as release identity has a blind spot, not a configuration choice:
    # a legacy ExecStartPre it never asked for cannot appear in the surfaces,
    # so the scheduler verdict would be derived from evidence that was never
    # gathered. Recording which set was used is not enough -- it blocks.
    collected = tuple(d for d in sc.get("EXEC_DIRECTIVES", "").split() if d)
    if not collected:
        defects.append(
            "observation records no executable-directive set — there is no way "
            "to tell whether the scheduler evidence covered every directive "
            "that can run code")
    elif set(EXEC_DIRECTIVES) - set(collected):
        missing_directives = sorted(set(EXEC_DIRECTIVES) - set(collected))
        defects.append(
            f"observation collected a NARROWER executable-directive set than "
            f"the scheduler certifier requires (missing: "
            f"{', '.join(missing_directives)}) — a legacy hook in an "
            f"uncollected directive would never reach the surfaces")

    host = sc.get("HOST", "")
    observation_id = sc.get("OBSERVATION_ID", "")
    checked_at = sc.get("CHECKED_AT", "")
    before = sc.get("CONFIGURATION_ANCHOR_BEFORE", "")
    after = sc.get("CONFIGURATION_ANCHOR_AFTER", "")
    # --- P1-3: the release worktree, not just the commit it claims ------
    # `HEAD == approved` is not `worktree == committed release`. Both
    # endpoints are required, and WHICH dirty paths matter is decided by the
    # canonical release-immutability contract rather than restated here:
    # production writes under the runtime roots by design, so a dirty
    # `outputs/` is expected operation, while a tracked modification to a
    # RELEASE_IMMUTABLE path is exactly the drift this gate exists to catch.
    for label in ("RELEASE_DIRTY_BEFORE", "RELEASE_DIRTY_AFTER",
                  "RELEASE_MAX_CTIME_BEFORE", "RELEASE_MAX_CTIME_AFTER",
                  "CRON_WITNESS_BEFORE", "CRON_WITNESS_AFTER"):
        if sc.get(label, "") in ("", "unreadable"):
            defects.append(
                f"observation could not read {label.lower().replace('_', ' ')}"
                f" — the witness it depends on was not observable, and content"
                f" equality alone does not prove interval stability")

    for endpoint in ("BEFORE", "AFTER"):
        for entry in flow["release_dirty"].get(endpoint, ()):
            status, _, path = entry.partition(" ")
            path = path.strip().strip('"')
            if not path:
                continue
            # Renames read as "old -> new"; both sides are classified.
            for part in [p.strip() for p in path.split("->")]:
                if not part:
                    continue
                if C.classify_path(part) == C.RUNTIME_MUTABLE:
                    continue        # production writes here by design
                defects.append(
                    f"the resolved release has a modified tracked file at the "
                    f"{endpoint.lower()} endpoint ({status.strip()} {part}) — "
                    f"HEAD naming the approved commit does not make the "
                    f"worktree the committed release")

    # The non-restorable half. A tracked file modified and restored to its
    # committed content reads clean in porcelain at BOTH ends -- measured --
    # while its ctime advances and cannot be put back.
    if (sc.get("RELEASE_MAX_CTIME_BEFORE") and sc.get("RELEASE_MAX_CTIME_AFTER")
            and sc["RELEASE_MAX_CTIME_BEFORE"] != sc["RELEASE_MAX_CTIME_AFTER"]):
        defects.append(
            f"a tracked file under the resolved release was modified during "
            f"collection (max ctime {sc['RELEASE_MAX_CTIME_BEFORE']} -> "
            f"{sc['RELEASE_MAX_CTIME_AFTER']}) — the release tree did not hold "
            f"still while the gates were read")

    # --- P1-4: cron's backing store -------------------------------------
    if (sc.get("CRON_WITNESS_BEFORE") and sc.get("CRON_WITNESS_AFTER")
            and sc["CRON_WITNESS_BEFORE"] != sc["CRON_WITNESS_AFTER"]):
        defects.append(
            f"the certified crontab's backing store changed during collection "
            f"({sc['CRON_WITNESS_BEFORE']} -> {sc['CRON_WITNESS_AFTER']}) — "
            f"cron content can return to its original bytes, its inode and "
            f"ctime cannot")

    obs = ObservationContext(host=host, observation_id=observation_id)
    bracket = {"configuration_anchor_before": before,
               "configuration_anchor_after": after}
    # The acceptance boundary belongs to the CERTIFICATION REQUEST, exactly
    # as approved_sha and expected_origins do. An untrusted capture that could
    # choose the release root its own commands are certified against -- or
    # widen the releases root the pointer is contained in -- would be setting
    # its own bar; recorded values are cross-checked, never adopted.
    for label, recorded, required in (
            ("RELEASE_ROOT", sc.get("RELEASE_ROOT", ""), release_root),
            ("RELEASES_ROOT", sc.get("RELEASES_ROOT", ""), releases_root),
            ("POINTER_PATH", sc.get("POINTER_PATH", ""), release_root)):
        if recorded != required:
            defects.append(
                f"observation records {label}={recorded!r} while this "
                f"certification requires {required!r} — evidence that chooses "
                f"its own acceptance boundary is not evidence about this "
                f"release")

    # --- scheduler leg ----------------------------------------------------
    surfaces: list = []
    for unit, props in sorted(flow["units"].items()):
        # The block must be about the unit its marker names. `systemctl show`
        # records the unit's own Id; a stored capture that places another
        # service's (aligned) output under this marker would otherwise have
        # that evidence certified under this unit's origin -- one unit's
        # evidence certifying another.
        recorded_ids = [line.split("=", 1)[1].strip()
                        for line in props
                        if line.startswith("Id=") and line.split("=", 1)[1].strip()]
        if not recorded_ids:
            defects.append(
                f"##SCHEDULER_UNIT {unit}: the block records no Id — evidence "
                f"that cannot say which unit it describes cannot be bound to "
                f"one")
            continue
        # The chroot-scope property must be RECORDED. The collector always
        # requests it, so a block without exactly one usable value did not
        # come from the collector -- and reconstructing without it would
        # supply systemd semantics (StartOnly=false) that the evidence never
        # recorded, treating a legacy host-path hook as chrooted beneath the
        # release.
        start_only = [line.split("=", 1)[1].strip() for line in props
                      if line.startswith("RootDirectoryStartOnly=")]
        if len(start_only) != 1 or start_only[0] not in ("yes", "no"):
            defects.append(
                f"##SCHEDULER_UNIT {unit}: the block records "
                f"{start_only!r} for RootDirectoryStartOnly — the collector "
                f"always requests this scalar, and defaulting it would supply "
                f"chroot semantics the evidence never recorded")
            continue
        # A block whose own LoadState is not "loaded" describes a unit the
        # manager was not running; converting it into execution surfaces while
        # the nested validity leg claims the unit loaded would compose two
        # manager observations that cannot both describe one stable run.
        load_states = [line.split("=", 1)[1].strip() for line in props
                       if line.startswith("LoadState=")]
        if load_states != ["loaded"]:
            defects.append(
                f"##SCHEDULER_UNIT {unit}: the block records "
                f"LoadState={load_states!r} — scheduler evidence about a unit "
                f"the manager had not loaded cannot certify that unit")
            continue
        if any(recorded != unit for recorded in recorded_ids):
            defects.append(
                f"##SCHEDULER_UNIT {unit}: the block's own Id says "
                f"{recorded_ids!r} — evidence from one unit must not certify "
                f"another, however aligned it is")
            continue
        surfaces.extend(S.parse_systemd_unit(_unit_text(props),
                                             origin=f"systemd:{unit}"))
    if flow["cron"].strip():
        surfaces.extend(S.parse_crontab(flow["cron"], origin="cron"))
    scheduler_artifact = S.scheduler_alignment_artifact(
        surfaces, release_root=release_root, checked_at=checked_at,
        observation=obs, expected_origins=expected_origins,
        approved_sha=approved_sha, **{
            "configuration_anchor_before": before,
            "configuration_anchor_after": after})

    # --- pointer leg ------------------------------------------------------
    resolved = sc.get("POINTER_RESOLVED", "")
    evidence = P.PointerEvidence(
        pointer_path=sc.get("POINTER_PATH", ""),
        exists=bool(sc.get("POINTER_PATH")) and bool(resolved),
        is_symlink=sc.get("POINTER_IS_SYMLINK", "") == "yes",
        resolved_path=resolved or None,
        resolved_exists=bool(resolved),
        target_sha=sc.get("POINTER_SHA") or None,
        target_tracked_dirty=(None if "POINTER_DIRTY" not in sc
                              else sc["POINTER_DIRTY"] == "yes"),
    )
    pointer_result = {
        **P.certify_pointer(evidence, approved_sha=approved_sha,
                            releases_root=releases_root,
                            observation=obs),
        **bracket,
    }

    # --- validity leg -----------------------------------------------------
    parsed = CV.parse_evidence(inner)
    stream_defects = (CV.stream_defects(inner) + CV.run_level_defects(inner)
                      + CV.run_order_defects(inner) + CV.per_unit_defects(inner)
                      + CV.unknown_marker_defects(inner))
    if stream_defects:
        validity = V.certify_systemd_unit_validity(verifier_available=False,
                                                   **parsed)
        for d in reversed(stream_defects):
            validity["blockers"].insert(0, d)
        validity["SYSTEMD_UNIT_VALIDITY"] = V.NOT_CERTIFIABLE
    else:
        validity = V.certify_systemd_unit_validity(**parsed)
    validity.update(bracket)

    # --- aggregate --------------------------------------------------------
    combined = S.certify_release_identity(
        surfaces, observation=obs, pointer_result=pointer_result,
        release_root=release_root, expected_origins=expected_origins,
        expected_validity_units=expected_validity_units,
        validity_result=validity, scheduler_result=scheduler_artifact)

    # A defective stream can never be repaired into a PASS.
    if defects:
        combined["errors"] = list(defects) + list(combined["errors"])
        combined["production_release_identity"] = "NOT_ESTABLISHED"
    combined["observation_stream_defects"] = list(defects)
    return {
        "scheduler_alignment": scheduler_artifact,
        "release_pointer_identity": pointer_result,
        "systemd_unit_validity": validity,
        "production_release_identity": combined,
    }


def _safe_artifact_name(name: str) -> str:
    """A single plain filename, or nothing.

    Joining a validated directory with an UNvalidated name undoes the
    validation: ``--artifact-name ../escaped.json`` writes beside the
    directory, and an absolute name replaces it entirely — measured. The name
    is therefore confined to one path component before any join.
    """
    candidate = Path(name)
    if (name in ("", ".", "..") or candidate.is_absolute()
            or candidate.name != name):
        raise SystemExit(
            f"--artifact-name must be a plain filename, got {name!r} — the "
            f"destination directory is validated, so the name must not be "
            f"able to leave it")
    return name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("evidence", nargs="?",
                    help="collection output (default: stdin)")
    ap.add_argument("--approved-sha", default="",
                    help="the release SHA this observation is certified against")
    ap.add_argument("--expected-origins", default="",
                    help="comma-separated origins the scheduler gate demands")
    ap.add_argument("--expected-validity-units", default="",
                    help="comma-separated units the validity gate must cover")
    ap.add_argument("--release-root", default="/opt/stockbot/current",
                    help="the release root this certification is against; the "
                         "captured value is cross-checked, never adopted")
    ap.add_argument("--releases-root", default="/opt/stockbot/releases",
                    help="the releases root the pointer must resolve inside; "
                         "cross-checked the same way")
    # Two destinations, deliberately named separately. One generic path
    # argument meaning both is how a production-certification bundle ended up
    # writable into outputs/backtest/ -- a replay-only tree -- and how a raw
    # path bypassed data governance entirely.
    ap.add_argument("--namespace", choices=sorted(APPROVED_NAMESPACES),
                    help="governed IN-REPOSITORY destination; the write is "
                         "owned by data_governance.safe_write_json")
    ap.add_argument("--artifact-name", default="release_observation.json",
                    help="filename to use within --namespace")
    ap.add_argument("--external-evidence-dir",
                    help="EXTERNAL evidence destination, outside the "
                         "production checkout, for Phase-E. Must be an "
                         "absolute path outside the repository; the write is "
                         "atomic. This is not a general output path.")
    args = ap.parse_args()
    # Confined up front: a bad name must be rejected before any
    # evidence is built, not discovered at write time.
    args.artifact_name = _safe_artifact_name(args.artifact_name)

    text = (Path(args.evidence).read_text(encoding="utf-8")
            if args.evidence else sys.stdin.read())
    bundle = build(
        text,
        approved_sha=args.approved_sha.strip(),
        expected_origins=tuple(o.strip() for o in args.expected_origins.split(",")
                               if o.strip()),
        expected_validity_units=tuple(
            u.strip() for u in args.expected_validity_units.split(",") if u.strip()),
        release_root=args.release_root,
        releases_root=args.releases_root,
    )
    payload = json.dumps(bundle, indent=2, sort_keys=True)

    if args.namespace:
        # In-repository writes are governed. This bundle is production-release
        # certification evidence; it must never land in a replay, sandbox or
        # historical tree, and data governance owns that decision.
        from portfolio_automation.data_governance import safe_write_json
        # base_dir defaults to a CWD-relative "outputs"; anchored to the
        # repository explicitly, or a CLI launched from elsewhere would write
        # "governed" evidence into <cwd>/outputs while advertising an
        # in-repository destination.
        safe_write_json(args.namespace, args.artifact_name, bundle,
                        base_dir=Path(__file__).resolve().parent.parent
                        / "outputs")

    if args.external_evidence_dir:
        # Phase E needs evidence OUTSIDE the production checkout. That remains
        # possible, but as a bounded destination rather than an arbitrary path:
        # the root must be absolute and must not lie inside the repository, so
        # this mode cannot be used to reach a governed tree sideways.
        destination = Path(args.external_evidence_dir)
        if not destination.is_absolute():
            raise SystemExit(
                "--external-evidence-dir must be an absolute path")
        repo = Path(__file__).resolve().parent.parent
        try:
            resolved = destination.resolve()
        except OSError as exc:
            raise SystemExit(f"--external-evidence-dir is unusable: {exc}")
        if resolved == repo or repo in resolved.parents:
            raise SystemExit(
                f"--external-evidence-dir must be outside the repository "
                f"({repo}); use --namespace for in-repository evidence")
        if not resolved.is_dir():
            raise SystemExit(
                f"--external-evidence-dir does not exist: {resolved}")
        target = resolved / args.artifact_name
        # Exclusively-created, unpredictably-named temp file, exactly as the
        # sibling validity CLI writes. A predictable `<name>.tmp` path can be
        # pre-planted as a symlink, and write_text() would follow it -- the
        # bundle landing OUTSIDE the directory that was just validated.
        # mkstemp opens with O_CREAT|O_EXCL, which never follows a link.
        fd, tmp = tempfile.mkstemp(dir=str(resolved),
                                   prefix=f".{args.artifact_name}.",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    print(payload)
    return (0 if bundle["production_release_identity"][
        "production_release_identity"] == "PASS" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
