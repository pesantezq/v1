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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.release import pointer as P  # noqa: E402
from portfolio_automation.release import scheduler as S  # noqa: E402
from portfolio_automation.release import systemd_validity as V  # noqa: E402
from portfolio_automation.release.observation import ObservationContext  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import certify_systemd_validity as CV  # noqa: E402

#: Run-level sections of the OUTER flow. Each may appear exactly once, for the
#: same reason the validity capture's own sections may: a stream carrying two
#: runs cannot attribute its evidence to one, and `parse` keeps the last value
#: it sees, so the contradiction would be resolved silently by ordering.
FLOW_SECTIONS = (
    "HOST", "OBSERVATION_ID", "CHECKED_AT",
    "CONFIGURATION_ANCHOR_BEFORE", "CONFIGURATION_ANCHOR_AFTER",
    "RELEASE_ROOT", "RELEASES_ROOT", "SCHEDULER_CRON",
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
        elif section in FLOW_SECTIONS and raw.strip():
            scalars.setdefault(section, raw.strip())
    return {"scalars": scalars, "units": units, "cron": "\n".join(cron)}


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
#: EnvironmentFiles=/path (ignore_errors=no) -> EnvironmentFile=/path
_ENVFILE = re.compile(r"^(\S+?)(?:\s*\(ignore_errors=\S+\))?$")


def _exec_command(value: str) -> str:
    """The command line `systemctl show` says this ExecStart would run."""
    value = value.strip()
    if not value.startswith("{"):
        return value          # already unit-file syntax
    argv = _ARGV.search(value)
    if argv and argv.group(1).strip():
        return argv.group(1).strip()
    path = _PATH_ONLY.search(value)
    return path.group(1) if path else ""


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
        if key == "ExecStart":
            command = _exec_command(value)
            if command:
                body.append(f"ExecStart={command}")
        elif key in ("WorkingDirectory", "RootDirectory"):
            body.append(f"{key}={value}")
        elif key == "EnvironmentFiles":
            match = _ENVFILE.match(value)
            if match:
                body.append(f"EnvironmentFile={match.group(1)}")
    return "[Service]\n" + "\n".join(body) + "\n"


def build(text: str, *, approved_sha: str, expected_origins: tuple[str, ...],
          expected_validity_units: tuple[str, ...]) -> dict:
    outer, inner = split_validity(text)
    defects = flow_defects(outer)
    flow = parse_flow(outer)
    sc = flow["scalars"]

    host = sc.get("HOST", "")
    observation_id = sc.get("OBSERVATION_ID", "")
    checked_at = sc.get("CHECKED_AT", "")
    before = sc.get("CONFIGURATION_ANCHOR_BEFORE", "")
    after = sc.get("CONFIGURATION_ANCHOR_AFTER", "")
    obs = ObservationContext(host=host, observation_id=observation_id)
    bracket = {"configuration_anchor_before": before,
               "configuration_anchor_after": after}
    release_root = sc.get("RELEASE_ROOT", "/opt/stockbot/current")

    # --- scheduler leg ----------------------------------------------------
    surfaces: list = []
    for unit, props in sorted(flow["units"].items()):
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
                            releases_root=sc.get("RELEASES_ROOT",
                                                 "/opt/stockbot/releases"),
                            observation=obs),
        **bracket,
    }

    # --- validity leg -----------------------------------------------------
    parsed = CV.parse_evidence(inner)
    stream_defects = (CV.stream_defects(inner) + CV.run_level_defects(inner)
                      + CV.per_unit_defects(inner))
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
    ap.add_argument("--out", help="write the JSON bundle here")
    args = ap.parse_args()

    text = (Path(args.evidence).read_text(encoding="utf-8")
            if args.evidence else sys.stdin.read())
    bundle = build(
        text,
        approved_sha=args.approved_sha.strip(),
        expected_origins=tuple(o.strip() for o in args.expected_origins.split(",")
                               if o.strip()),
        expected_validity_units=tuple(
            u.strip() for u in args.expected_validity_units.split(",") if u.strip()),
    )
    payload = json.dumps(bundle, indent=2, sort_keys=True)
    if args.out:
        tmp = Path(args.out).with_suffix(Path(args.out).suffix + ".tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, args.out)
    print(payload)
    return (0 if bundle["production_release_identity"][
        "production_release_identity"] == "PASS" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
