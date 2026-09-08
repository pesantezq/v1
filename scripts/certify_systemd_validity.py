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

from portfolio_automation.release import systemd_validity as V  # noqa: E402


def parse_evidence(text: str) -> dict:
    """Parse the collector's record stream into the certifier's inputs."""
    host = checked_at = version = ""
    expected: list[str] = []
    optional: list[str] = []
    discovered: list[str] = []
    shows: dict[str, list[str]] = {}
    verifies: dict[str, tuple[int, list[str]]] = {}
    commands: dict[str, list[str]] = {}

    section = None
    unit = None
    for raw in text.splitlines():
        if raw.startswith("##"):
            parts = raw[2:].split()
            section = parts[0] if parts else ""
            unit = parts[1] if len(parts) > 1 else None
            if section == "VERIFY" and unit is not None:
                status = int(parts[2]) if len(parts) > 2 else 1
                verifies[unit] = (status, [])
            elif section == "SHOW" and unit is not None:
                shows[unit] = []
            elif section == "VERIFYCMD" and unit is not None:
                commands[unit] = []
            continue

        if section == "HOST" and raw.strip():
            host = raw.strip()
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
            commands[unit].extend(shlex.split(raw.strip()))
        elif section == "SHOW" and unit is not None:
            shows[unit].append(raw)
        elif section == "VERIFY" and unit is not None:
            verifies[unit][1].append(raw)

    provenance = {
        u: V.provenance_from_show("\n".join(lines), unit=u)
        for u, lines in shows.items()
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
        "host": host,
        "checked_at": checked_at,
        "systemd_version": version,
        "expected_units": tuple(expected),
        "optional_units": tuple(optional),
        "discovered_units": tuple(discovered),
        "provenance": provenance,
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
    ap.add_argument("--namespace",
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
    # A truncated or failed capture must not look like a clean host.
    if "##END" not in text:
        parsed["expected_units"] = tuple(parsed["expected_units"])
        result = V.certify_systemd_unit_validity(
            classified_units=tuple(
                u for u in args.classified.split(",") if u.strip()),
            verifier_available=False, **parsed)
        result["blockers"].insert(0, "evidence stream is truncated — the "
                                     "capture did not complete")
    else:
        result = V.certify_systemd_unit_validity(
            classified_units=tuple(
                u.strip() for u in args.classified.split(",") if u.strip()),
            **parsed)

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
