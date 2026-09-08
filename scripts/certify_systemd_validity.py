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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.release import systemd_validity as V  # noqa: E402


def parse_evidence(text: str) -> dict:
    """Parse the collector's record stream into the certifier's inputs."""
    host = checked_at = version = ""
    expected: list[str] = []
    discovered: list[str] = []
    shows: dict[str, list[str]] = {}
    verifies: dict[str, tuple[int, list[str]]] = {}

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
            continue

        if section == "HOST" and raw.strip():
            host = raw.strip()
        elif section == "CHECKED_AT" and raw.strip():
            checked_at = raw.strip()
        elif section == "SYSTEMD_VERSION" and raw.strip():
            version = raw.strip()
        elif section == "EXPECTED" and raw.strip():
            expected.append(raw.strip())
        elif section == "DISCOVERED" and raw.strip():
            discovered.append(raw.strip())
        elif section == "SHOW" and unit is not None:
            shows[unit].append(raw)
        elif section == "VERIFY" and unit is not None:
            verifies[unit][1].append(raw)

    provenance = {
        u: V.provenance_from_show("\n".join(lines), unit=u)
        for u, lines in shows.items()
    }
    outcomes = {
        u: V.VerifierOutcome(
            unit=u,
            command=V.build_verifier_command(u),
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
        "discovered_units": tuple(discovered),
        "provenance": provenance,
        "outcomes": outcomes,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("evidence", nargs="?", help="collector output (default: stdin)")
    ap.add_argument("--out", help="write the JSON artifact here")
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
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if result["SYSTEMD_UNIT_VALIDITY"] == V.PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
