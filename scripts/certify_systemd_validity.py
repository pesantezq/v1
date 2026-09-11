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
        | python3 scripts/certify_systemd_validity.py --out evidence.json

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


def parse_evidence(text: str) -> dict:
    """Parse the collector's record stream into the certifier's inputs."""
    host = checked_at = version = observation_id = ""
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("evidence", nargs="?", help="collector output (default: stdin)")
    ap.add_argument("--out", help="write the JSON artifact here")
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

    text = (Path(args.evidence).read_text(encoding="utf-8")
            if args.evidence else sys.stdin.read())

    parsed = parse_evidence(text)
    defects = stream_defects(text)
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
        safe_write_json(args.namespace, args.artifact_name, result)
    if args.out:
        _atomic_write(Path(args.out), payload + "\n")
    print(payload)
    return 0 if result["SYSTEMD_UNIT_VALIDITY"] == V.PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
