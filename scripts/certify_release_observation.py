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
        if key in EXEC_DIRECTIVES:
            command = _exec_command(value)
            if command:
                body.append(f"{key}={command}")
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

    if args.namespace:
        # In-repository writes are governed. This bundle is production-release
        # certification evidence; it must never land in a replay, sandbox or
        # historical tree, and data governance owns that decision.
        from portfolio_automation.data_governance import safe_write_json
        safe_write_json(args.namespace, args.artifact_name, bundle)

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
        target = resolved / "release_observation.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, target)

    print(payload)
    return (0 if bundle["production_release_identity"][
        "production_release_identity"] == "PASS" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
