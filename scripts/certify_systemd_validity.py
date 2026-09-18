#!/usr/bin/env python3
"""Turn captured systemd evidence into a SYSTEMD_UNIT_VALIDITY verdict.

Reads the record stream produced by ``scripts/collect_systemd_validity_evidence.sh``
(on stdin or from a file) and writes the structured artifact as JSON.

The split is deliberate: the collector observes the host and nothing else, this
certifier decides and touches no host. That keeps the production side of the
gate strictly read-only and keeps the decision logic unit-testable without a
VPS, root, or systemd.

Usage::

    ssh host 'bash -s' < scripts/collect_systemd_validity_evidence.sh \\
        | python3 scripts/certify_systemd_validity.py --namespace policy

Exit status: 0 when SYSTEMD_UNIT_VALIDITY == PASS, 1 otherwise, so the gate can
be wired into a runbook step without parsing its own output.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Stands in for an exit status that could not be read. Non-zero so it can
#: never be mistaken for success even if a later change stopped checking
#: `malformed` explicitly.
MALFORMED_STATUS = 255

#: Namespaces this artifact may be written to. Containment is not belonging:
#: `safe_write_json` proves a write stays inside the namespace it was given, not
#: that a production release certificate belongs there. Writing one into the
#: replay-only tree (`historical` -> `outputs/backtest/`) would contaminate it
#: with live-lane evidence, which the artifact contract forbids.
APPROVED_NAMESPACES = frozenset({"policy"})

from portfolio_automation.release import systemd_validity as V  # noqa: E402


END_MARKER = "##END"


def stream_defects(text: str) -> list[str]:
    """Structural checks on the capture as a whole.

    The capture is untrusted text -- it can be stored, edited, concatenated or
    truncated between collection and certification -- so its shape is checked
    before its contents are believed. A substring search for the terminator is
    not enough: a complete run followed by a truncated retry contains an
    ``##END`` in the middle, and reading that as "the capture completed" would
    attribute the first run's unit evidence to the second run's host and time.
    """
    defects: list[str] = []
    lines = [line for line in text.splitlines() if line.strip()]
    markers = [i for i, line in enumerate(lines) if line.strip() == END_MARKER]

    if not markers:
        defects.append("evidence stream is truncated — the capture did not "
                       "complete")
    elif len(markers) > 1:
        defects.append(f"evidence stream contains {len(markers)} terminators — "
                       f"it is more than one capture concatenated")
    elif markers[0] != len(lines) - 1:
        defects.append("evidence stream continues past its terminator — "
                       "records after ##END are not part of a completed capture")
    return defects


#: Sections that describe the RUN rather than a unit. Each may appear exactly
#: once in a capture. Counting terminators is not enough: a run truncated
#: before its ``##END`` can be prepended to a complete one, leaving exactly one
#: terminal marker while the reader silently keeps units from both runs and
#: lets the later scalars overwrite the earlier host, id and release. The
#: result attributes one host's unit evidence to another. A capture is one
#: ordered run or it is not evidence.
RUN_LEVEL_SECTIONS = (
    "HOST", "OBSERVATION_ID", "CHECKED_AT", "SYSTEMD_VERSION",
    "RELEASE_POINTER_BEFORE", "RELEASE_POINTER_AFTER",
    "SEARCH_PATH_SOURCE", "SEARCH_PATH", "SEARCH_PATH_EFFECTIVE",
    "EXPECTED", "OPTIONAL", "DISCOVERED",
)


#: Sections that describe ONE UNIT within the run. Each may appear exactly
#: once per unit, for the same reason the run-level sections may appear once
#: per capture: `parse_evidence` keeps the LAST value it sees, so a capture
#: carrying a failing ``##VERIFY`` followed by a passing one for the same unit
#: silently resolves the contradiction by ordering. Evidence that says two
#: different things about one unit is not evidence about that unit.
#: In COLLECTION order -- the ordering check below zips adjacent pairs, so
#: this tuple IS the required sequence, not just a membership set.
PER_UNIT_SECTIONS = ("SHOW", "VERIFYCMD", "VERIFY", "RECHECK")


def per_unit_defects(text: str) -> list[str]:
    """Ways a capture says more than one thing about a single unit."""
    seen: dict[tuple[str, str], int] = {}
    for line in (text or "").splitlines():
        if not line.startswith("##"):
            continue
        parts = line[2:].split()
        if len(parts) >= 2 and parts[0] in PER_UNIT_SECTIONS:
            key = (parts[0], parts[1])
            seen[key] = seen.get(key, 0) + 1
    defects = [
        f"evidence contains {count} ##{section} sections for {unit} — a "
        f"capture records each unit once, so a stream carrying several "
        f"contradicts itself about that unit and the contradiction would "
        f"otherwise be resolved by ordering alone"
        for (section, unit), count in sorted(seen.items()) if count > 1
    ]

    # ...and in COLLECTION order. The RECHECK snapshot only proves the
    # configuration held still if it was taken AFTER the verifier ran; a
    # stream that moves it before ##VERIFY still has every section exactly
    # once while its "post-verification" observation predates the
    # verification. Order is evidence here exactly as it is for the outer
    # flow's anchors.
    positions: dict[tuple[str, str], int] = {}
    for index, line in enumerate((text or "").splitlines()):
        if not line.startswith("##"):
            continue
        parts = line[2:].split()
        if len(parts) >= 2 and parts[0] in PER_UNIT_SECTIONS:
            positions.setdefault((parts[1], parts[0]), index)
    units = {unit for (unit, _section) in positions}
    for unit in sorted(units):
        ordered = [positions.get((unit, section))
                   for section in PER_UNIT_SECTIONS]
        present = [(section, at) for section, at
                   in zip(PER_UNIT_SECTIONS, ordered) if at is not None]
        for (earlier, at_a), (later, at_b) in zip(present, present[1:]):
            if at_a > at_b:
                defects.append(
                    f"{unit}: ##{later} appears before ##{earlier} — the "
                    f"sections are out of collection order, and a re-check "
                    f"taken before verification brackets nothing")
    return defects


#: The validity capture's run-level sections, in COLLECTION order. Uniqueness
#: alone lets a unique section MOVE: a ##RELEASE_POINTER_AFTER relocated
#: before the first ##SHOW still appears exactly once while the two pointer
#: readings no longer bracket verification -- a deployment during the run
#: would hide between them.
RUN_LEVEL_ORDER = (
    "HOST", "OBSERVATION_ID", "CHECKED_AT", "RELEASE_POINTER_BEFORE",
    "SEARCH_PATH_SOURCE", "SEARCH_PATH", "SEARCH_PATH_EFFECTIVE",
    "SYSTEMD_VERSION", "EXPECTED", "OPTIONAL", "DISCOVERED",
    "RELEASE_POINTER_AFTER", "END",
)


def run_order_defects(text: str) -> list[str]:
    """Ways the capture's SHAPE is wrong: sections out of collection order,
    or unit evidence outside the pointer bracket."""
    seen = [line[2:].split(" ", 1)[0].strip()
            for line in (text or "").splitlines() if line.startswith("##")]
    positions: dict[str, int] = {}
    for index, name in enumerate(seen):
        positions.setdefault(name, index)

    defects: list[str] = []
    present = [name for name in RUN_LEVEL_ORDER if name in positions]
    for earlier, later in zip(present, present[1:]):
        if positions[earlier] > positions[later]:
            defects.append(
                f"evidence has ##{later} before ##{earlier} — the run-level "
                f"sections are out of collection order, and a pointer reading "
                f"that does not bracket the unit evidence conceals a "
                f"deployment during the run")

    # every per-unit observation must lie between the two pointer readings
    before_at = positions.get("RELEASE_POINTER_BEFORE")
    after_at = positions.get("RELEASE_POINTER_AFTER")
    if before_at is not None and after_at is not None:
        for index, name in enumerate(seen):
            if name in PER_UNIT_SECTIONS and not before_at < index < after_at:
                side = ("before ##RELEASE_POINTER_BEFORE" if index <= before_at
                        else "after ##RELEASE_POINTER_AFTER")
                defects.append(
                    f"evidence has unit section ##{name} {side} — unit "
                    f"evidence outside the pointer bracket was not proven to "
                    f"describe a single release")
    if "END" in positions and seen and seen[-1] != "END":
        defects.append(
            "evidence continues after ##END — records past the terminal "
            "marker were not part of the capture")
    return defects


#: Every marker the collector emits. Anything else is malformed residue, and
#: treating it as a section change silently discards every following line
#: until the next recognised marker -- a ##IGNORED placed before a
#: contradictory NeedDaemonReload=yes made the fact vanish and restored PASS.
KNOWN_SECTIONS = (frozenset(RUN_LEVEL_SECTIONS)
                  | frozenset(PER_UNIT_SECTIONS) | {"END"})


def unknown_marker_defects(text: str) -> list[str]:
    """Markers the collector never emits — by NAME or by SHAPE.

    A known name with the wrong arity is as malformed as an unknown one: a
    bare ``##SHOW`` switches parsing to a unitless section and every following
    record is silently discarded, exactly the truncation the name check exists
    to prevent. So each marker's complete collector-emitted shape is required:
    per-unit markers carry their unit (and ``VERIFY`` its exit status);
    run-level markers carry nothing.
    """
    defects: list[str] = []
    for line in (text or "").splitlines():
        if not line.startswith("##"):
            continue
        tokens = line[2:].split()
        name = tokens[0] if tokens else ""
        if name not in KNOWN_SECTIONS:
            defects.append(
                f"evidence contains unknown section marker {line.strip()!r} — "
                f"the collector never emits it, and treating it as a section "
                f"change would silently discard every following record until "
                f"the next recognised marker")
            continue
        if name == "VERIFY":
            expected_arity = 3          # VERIFY <unit> <exit status>
        elif name in PER_UNIT_SECTIONS:
            expected_arity = 2          # SHOW/RECHECK/VERIFYCMD <unit>
        else:
            expected_arity = 1          # run-level markers carry no operand
        if len(tokens) != expected_arity:
            defects.append(
                f"evidence contains malformed section marker {line.strip()!r} "
                f"— the collector emits ##{name} with "
                f"{expected_arity - 1} operand(s), and a differently-shaped "
                f"marker silently redirects every following record")
    return defects


def run_level_defects(text: str) -> list[str]:
    """Ways a stream is not one complete, ordered capture."""
    seen: dict[str, int] = {}
    for line in (text or "").splitlines():
        if not line.startswith("##"):
            continue
        name = line[2:].split(" ", 1)[0].strip()
        if name in RUN_LEVEL_SECTIONS:
            seen[name] = seen.get(name, 0) + 1
    defects = []
    for name, count in sorted(seen.items()):
        if count > 1:
            defects.append(
                f"evidence contains {count} ##{name} sections — a capture is "
                f"ONE run, so a stream carrying several has been concatenated, "
                f"and its unit evidence cannot be attributed to a single host "
                f"or release however the later values overwrite the earlier"
            )
    return defects


def parse_evidence(text: str) -> dict:
    """Parse the collector's record stream into the certifier's inputs."""
    host = checked_at = version = observation_id = ""
    release_before = release_after = ""
    search_path_source = search_path = search_path_effective = ""
    expected: list[str] = []
    optional: list[str] = []
    discovered: list[str] = []
    shows: dict[str, list[str]] = {}
    # The post-verification pass. Kept separate from ``shows`` rather than
    # merged into it: the whole point of the second observation is that it can
    # DISAGREE with the first, and a merge would silently resolve exactly the
    # conflict the gate needs to see.
    rechecks: dict[str, list[str]] = {}
    verifies: dict[str, tuple[int, list[str]]] = {}
    commands: dict[str, list[str]] = {}

    malformed: list[str] = []
    section = None
    unit = None
    for raw in text.splitlines():
        if raw.startswith("##"):
            parts = raw[2:].split()
            section = parts[0] if parts else ""
            unit = parts[1] if len(parts) > 1 else None
            if section == "VERIFY" and unit is not None:
                # An unreadable status is malformed evidence, not a passing
                # unit. Raising here would abort before the certifier could
                # emit its structured NOT_CERTIFIABLE artifact, so operators
                # would lose the blockers that distinguish an unreadable
                # capture from an established unit failure.
                try:
                    status = int(parts[2]) if len(parts) > 2 else 1
                except ValueError:
                    status = MALFORMED_STATUS
                    malformed.append(f"{unit}: unreadable exit status "
                                     f"{parts[2]!r}")
                verifies[unit] = (status, [])
            elif section == "SHOW" and unit is not None:
                shows[unit] = []
            elif section == "RECHECK" and unit is not None:
                rechecks[unit] = []
            elif section == "VERIFYCMD" and unit is not None:
                commands[unit] = []
            continue

        if section == "HOST" and raw.strip():
            host = raw.strip()
        elif section == "OBSERVATION_ID" and raw.strip():
            observation_id = raw.strip()
        elif section == "SEARCH_PATH_SOURCE" and raw.strip():
            search_path_source = raw.strip()
        elif section == "SEARCH_PATH" and raw.strip():
            search_path = raw.strip()
        elif section == "SEARCH_PATH_EFFECTIVE" and raw.strip():
            search_path_effective = raw.strip()
        elif section == "RELEASE_POINTER_BEFORE" and raw.strip():
            release_before = raw.strip()
        elif section == "RELEASE_POINTER_AFTER" and raw.strip():
            release_after = raw.strip()
        elif section == "CHECKED_AT" and raw.strip():
            checked_at = raw.strip()
        elif section == "SYSTEMD_VERSION" and raw.strip():
            version = raw.strip()
        elif section == "EXPECTED" and raw.strip():
            expected.append(raw.strip())
        elif section == "OPTIONAL" and raw.strip():
            optional.append(raw.strip())
        elif section == "DISCOVERED" and raw.strip():
            discovered.append(raw.strip())
        elif section == "VERIFYCMD" and unit is not None and raw.strip():
            # A damaged command record (an unmatched quote, say) is malformed
            # evidence like any other. Raising would lose the whole structured
            # result over one unreadable line.
            try:
                commands[unit].extend(shlex.split(raw.strip()))
            except ValueError as exc:
                malformed.append(f"{unit}: unreadable verifier command ({exc})")
                commands[unit] = []
        elif section == "SHOW" and unit is not None:
            shows[unit].append(raw)
        elif section == "RECHECK" and unit is not None:
            rechecks[unit].append(raw)
        elif section == "VERIFY" and unit is not None:
            verifies[unit][1].append(raw)

    provenance = {
        u: V.provenance_from_show("\n".join(lines), unit=u)
        for u, lines in shows.items()
    }
    recheck = {
        u: V.provenance_from_show("\n".join(lines), unit=u)
        for u, lines in rechecks.items()
    }
    # The command comes from the capture. If a capture does not say what ran,
    # the command is empty, `uses_required_flag` is False, and the certifier
    # blocks -- rather than crediting the run with an invocation it may never
    # have used.
    outcomes = {
        u: V.VerifierOutcome(
            unit=u,
            command=tuple(commands.get(u, ())),
            exit_status=status,
            output="\n".join(lines),
        )
        for u, (status, lines) in verifies.items()
    }
    return {
        "malformed_evidence": tuple(malformed),
        "host": host,
        "observation_id": observation_id,
        "search_path_source": search_path_source,
        "search_path": search_path,
        "search_path_effective": search_path_effective,
        "release_pointer_before": release_before,
        "release_pointer_after": release_after,
        "checked_at": checked_at,
        "systemd_version": version,
        "expected_units": tuple(expected),
        "optional_units": tuple(optional),
        "discovered_units": tuple(discovered),
        "provenance": provenance,
        "recheck": recheck,
        "outcomes": outcomes,
    }


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then ``os.replace``.

    Phase E evidence is captured deliberately OUTSIDE the repository, so it has
    no governed namespace; but an interrupted certification must never truncate
    a previously valid certificate. ``os.replace`` is atomic within a
    filesystem, so the target is either the old artifact or the new one.
    Governed in-repo writes use ``--namespace`` and go through
    ``data_governance.safe_write_json`` instead.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
    ap.add_argument("evidence", nargs="?", help="collector output (default: stdin)")
    # Two destinations, named separately -- the same rule the aggregate CLI
    # follows, for the same reason: one generic path argument meaning both is
    # how production-certification evidence becomes writable into a replay
    # tree. There is deliberately no raw output path.
    ap.add_argument("--external-evidence-dir",
                    help="EXTERNAL evidence destination for Phase-E, outside "
                         "the production checkout. Must be an absolute path "
                         "that resolves outside the repository; the write is "
                         "atomic. This is not a general output path.")
    ap.add_argument("--namespace", choices=sorted(APPROVED_NAMESPACES),
                    help="governed output namespace; when given, the artifact "
                         "is written through data_governance.safe_write_json "
                         "instead of to a raw path")
    ap.add_argument("--artifact-name", default="systemd_unit_validity.json",
                    help="filename to use within --namespace")
    ap.add_argument("--classified", default="",
                    help="comma-separated relevant units an operator has "
                         "explicitly classified as not requiring validation")
    args = ap.parse_args()
    # Confined up front: a bad name must be rejected before any
    # evidence is built, not discovered at write time.
    args.artifact_name = _safe_artifact_name(args.artifact_name)

    text = (Path(args.evidence).read_text(encoding="utf-8")
            if args.evidence else sys.stdin.read())

    parsed = parse_evidence(text)
    defects = (stream_defects(text) + run_level_defects(text)
               + run_order_defects(text) + per_unit_defects(text)
               + unknown_marker_defects(text))
    classified = tuple(u.strip() for u in args.classified.split(",") if u.strip())

    # A truncated, doubled or edited capture must not look like a clean host.
    if defects:
        result = V.certify_systemd_unit_validity(
            classified_units=classified, verifier_available=False, **parsed)
        for defect in reversed(defects):
            result["blockers"].insert(0, defect)
    else:
        result = V.certify_systemd_unit_validity(
            classified_units=classified, **parsed)

    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.namespace:
        # Repository output artifacts go through data governance, which
        # validates the namespace and owns the write.
        from portfolio_automation.data_governance import safe_write_json
        # base_dir defaults to a CWD-relative "outputs"; anchored to the
        # repository explicitly, or a CLI launched from elsewhere would write
        # "governed" evidence into <cwd>/outputs while advertising an
        # in-repository destination.
        safe_write_json(args.namespace, args.artifact_name, result,
                        base_dir=Path(__file__).resolve().parent.parent
                        / "outputs")
    if args.external_evidence_dir:
        destination = Path(args.external_evidence_dir)
        if not destination.is_absolute():
            raise SystemExit("--external-evidence-dir must be an absolute path")
        repo = Path(__file__).resolve().parent.parent
        try:
            # resolve() follows symlinks, so a link pointing back into the
            # repository cannot smuggle the write inside it.
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
        _atomic_write(resolved / args.artifact_name, payload + "\n")
    print(payload)
    return 0 if result["SYSTEMD_UNIT_VALIDITY"] == V.PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
