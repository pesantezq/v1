#!/usr/bin/env python3
"""Deterministic transition MATERIALIZER — surgically applies a trusted proposal.

Usage:
    python scripts/northstar_materialize_transition.py apply \
        --proposal proposal.json --certified-sha <sha> [--repo-root .]
    python scripts/northstar_materialize_transition.py -h

Given the trusted proposal from `northstar_transition.propose_transition` (itself
derived from certified main), this edits ONLY the exact allowlisted protected
fields and leaves everything else — including all comments and the preserved
`paused_bounded_authorization` block — byte-for-byte untouched. It is SURGICAL
(path-resolved scalar edits, never a whole-file re-serialize) and FAIL-CLOSED:

  * current main must still equal the certified SHA (else STOP);
  * every allowlisted anchor must resolve UNIQUELY (else STOP);
  * the in-file `paused_bounded_authorization` must deep-equal the proposal's
    `restore_bounded_authorization` (proves EXACT preservation; else STOP);
  * after editing, a re-parse must show exactly the intended values and an
    unchanged preserved block (else STOP, nothing written);
  * idempotent: re-applying the same certified transition is a no-op.

It does NOT push to main, NOT mark any mission complete, and NOT widen authority.
The caller (the orchestrator effect job) creates a deterministic governance branch
from the surgical edits and opens ONE governance PR against main; the merge still
goes through ordinary CI/review/human-merge governance.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


class MaterializeError(Exception):
    """A fail-closed materialization refusal."""


# --------------------------------------------------------------------------- #
# Surgical, comment-preserving text edits (PURE; unit-tested).                 #
# --------------------------------------------------------------------------- #
def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank_or_comment(line: str) -> bool:
    s = line.strip()
    return s == "" or s.startswith("#")


def _child_span(lines: list[str], start: int, end: int, parent_indent: int,
                key: str) -> tuple[int, int, int]:
    """Find `key:` at indent > parent_indent within [start, end); return
    (header_idx, block_end, indent). Fail closed on 0 or >1 matches."""
    hits = []
    i = start
    while i < end:
        ln = lines[i]
        if _is_blank_or_comment(ln):
            i += 1
            continue
        ind = _indent(ln)
        if ind > parent_indent and re.match(rf"^\s*{re.escape(key)}\s*:", ln):
            hits.append((i, ind))
        i += 1
    # keep only the shallowest indent among hits (direct children of the parent)
    if not hits:
        raise MaterializeError(f"key {key!r} not found under parent indent {parent_indent}")
    min_ind = min(ind for _, ind in hits)
    direct = [idx for idx, ind in hits if ind == min_ind]
    if len(direct) != 1:
        raise MaterializeError(f"ambiguous key {key!r} ({len(direct)} direct matches)")
    hidx = direct[0]
    hind = min_ind
    # block end: next line with indent <= hind
    j = hidx + 1
    while j < end:
        if not _is_blank_or_comment(lines[j]) and _indent(lines[j]) <= hind:
            break
        j += 1
    return hidx, j, hind


def resolve_scalar_line(text: str, dotted_path: str) -> int:
    """Return the line index of the scalar at `dotted_path` (a.b.c), following the
    YAML block nesting. Fail closed if the path is not unique."""
    lines = text.splitlines()
    segs = dotted_path.split(".")
    start, end, parent_indent = 0, len(lines), -1
    for seg in segs[:-1]:
        hidx, bend, hind = _child_span(lines, start, end, parent_indent, seg)
        start, end, parent_indent = hidx + 1, bend, hind
    last = segs[-1]
    hidx, _, _ = _child_span(lines, start, end, parent_indent, last)
    return hidx


_VAL = re.compile(r"^(\s*[^:]+:\s*)(\S.*?)(\s*#.*)?$")


def set_scalar(text: str, dotted_path: str, new_value: str) -> str:
    """Surgically set the scalar value at `dotted_path`, preserving indentation and
    any trailing inline comment. Idempotent. Fail closed on ambiguity."""
    lines = text.splitlines(keepends=True)
    idx = resolve_scalar_line(text, dotted_path)
    raw = lines[idx].rstrip("\n")
    m = _VAL.match(raw)
    if not m:
        raise MaterializeError(f"cannot parse scalar line for {dotted_path!r}: {raw!r}")
    prefix, _oldval, comment = m.group(1), m.group(2), m.group(3) or ""
    newline = f"{prefix}{new_value}{comment}"
    if raw.endswith("\n"):
        newline += "\n"
    lines[idx] = newline + ("\n" if lines[idx].endswith("\n") and not newline.endswith("\n") else "")
    # normalize: ensure exactly one trailing newline as original
    out = "".join(lines)
    if not out.endswith("\n") and text.endswith("\n"):
        out += "\n"
    return out


def set_json_scalar(text: str, key: str, new_value: str) -> str:
    """Surgically set a top-level JSON string value by key, preserving formatting."""
    pat = re.compile(rf'(^\s*"{re.escape(key)}"\s*:\s*")([^"]*)(")', re.MULTILINE)
    hits = pat.findall(text)
    if len(hits) != 1:
        raise MaterializeError(f"JSON key {key!r} not unique ({len(hits)} matches)")
    return pat.sub(rf'\g<1>{new_value}\g<3>', text, count=1)


# --------------------------------------------------------------------------- #
# Path helpers for the proposal's allowlisted edits.                          #
# --------------------------------------------------------------------------- #
# Map a proposal `file#dotted.path` key to (relpath, kind, dotted-or-jsonkey).
def _parse_edit_key(key: str) -> tuple[str, str]:
    if "#" not in key:
        raise MaterializeError(f"malformed edit key {key!r}")
    relpath, locator = key.split("#", 1)
    return relpath, locator


_BA_PATH = "stockbot_northstar_redesign.phases.northstar_phase_0c.bounded_authorization"


def _canonical_transport(value: Any) -> Any:
    """Normalize YAML-native values to the deterministic JSON transport domain.

    PyYAML materializes unquoted dates/timestamps as date/datetime objects while
    transition proposals cross a JSON boundary and therefore carry ISO-8601
    strings. Compare those representations canonically without reconstructing or
    mutating the preserved YAML text. Unknown types fail closed.
    """
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _canonical_transport(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_canonical_transport(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise MaterializeError(
        f"unsupported value type in transition transport: {value.__class__.__name__}"
    )


def _paused_object(phase_text: str) -> Any:
    import yaml
    doc = yaml.safe_load(phase_text)
    return (doc["stockbot_northstar_redesign"]["phases"]["northstar_phase_0c"]
            ["bounded_authorization"]["paused_bounded_authorization"])


def _active_bounded_auth(phase_text: str) -> dict:
    import yaml
    doc = yaml.safe_load(phase_text)
    return (doc["stockbot_northstar_redesign"]["phases"]["northstar_phase_0c"]
            ["bounded_authorization"])


def _block_span_by_path(lines: list[str], dotted: str) -> tuple[int, int, int]:
    start, end, parent = 0, len(lines), -1
    hidx = bend = hind = 0
    for seg in dotted.split("."):
        hidx, bend, hind = _child_span(lines, start, end, parent, seg)
        start, end, parent = hidx + 1, bend, hind
    return hidx, bend, hind


def _dedent_line(line: str, n: int) -> str:
    i = 0
    while i < n and i < len(line) and line[i] == " ":
        i += 1
    return line[i:]


def promote_paused_to_active(phase_text: str) -> str:
    """Make the ACTIVE bounded_authorization BECOME the preserved paused object
    (verbatim, by moving its exact text up one indent level), preserving any
    prior_bounded_authorization sibling. Surgical; no reconstruction."""
    lines = phase_text.splitlines(keepends=True)
    ba_h, ba_end, ba_ind = _block_span_by_path(lines, _BA_PATH)
    pb_h, pb_end, pb_ind = _child_span(lines, ba_h + 1, ba_end, ba_ind, "paused_bounded_authorization")
    dedent = pb_ind - ba_ind   # move paused children up to the active-children level
    paused_children = [(_dedent_line(l, dedent) if l.strip() else l) for l in lines[pb_h + 1:pb_end]]
    try:
        pr_h, pr_end, _pr_ind = _child_span(lines, ba_h + 1, ba_end, ba_ind, "prior_bounded_authorization")
        prior_block = lines[pr_h:pr_end]
    except MaterializeError:
        prior_block = []
    new_children = paused_children + prior_block
    out = lines[:ba_h + 1] + new_children + lines[ba_end:]
    return "".join(out)


def materialize_edits(files_text: dict[str, str], proposal: dict[str, Any]) -> dict[str, Any]:
    """PURE: apply the proposal to in-memory file texts. Returns
    {"files": {relpath: new_text}, "changed": [...], "verified": True}.
    Fail closed on ambiguity, missing anchors, or preserved-block mismatch."""
    edits = proposal.get("allowlisted_field_edits") or {}
    if not edits:
        raise MaterializeError("proposal has no allowlisted_field_edits")
    out = dict(files_text)
    changed: list[str] = []

    # 1) EXACT paused-authorization preservation check (before any edit)
    restore = proposal.get("restore_bounded_authorization")
    restore_transport = _canonical_transport(restore) if restore is not None else None
    phase_rel = ".agent/phase_status.yaml"
    already_promoted = False
    if restore is not None:
        if phase_rel not in out:
            raise MaterializeError("phase_status.yaml text not provided for restore")
        active = _active_bounded_auth(out[phase_rel])
        active_core = {k: v for k, v in active.items() if k != "prior_bounded_authorization"}
        if ("paused_bounded_authorization" not in active
                and _canonical_transport(active_core) == restore_transport):
            already_promoted = True   # idempotent re-apply: the active object already IS it
        else:
            before = _paused_object(out[phase_rel])  # must exist pre-promotion
            if _canonical_transport(before) != restore_transport:
                raise MaterializeError("preserved paused_bounded_authorization != proposal object")

    # 2) surgical scalar edits (idempotent)
    for key, new_value in edits.items():
        relpath, locator = _parse_edit_key(key)
        if relpath not in out:
            raise MaterializeError(f"file {relpath} not provided")
        text = out[relpath]
        if relpath.endswith(".json"):
            new_text = set_json_scalar(text, locator, str(new_value))
        else:
            new_text = set_scalar(text, locator, str(new_value))
        if new_text != text:
            out[relpath] = new_text
            if relpath not in changed:
                changed.append(relpath)

    # 3) restore: PROMOTE the preserved paused object to BE the active bounded
    #    authorization (verbatim text moved up one level), so the active object's
    #    mission, scope, authorizer and markers are the adapter's — not just the
    #    mission id. The exact preserved object is the source; nothing is reconstructed.
    if restore is not None and not already_promoted:
        new_text = promote_paused_to_active(out[phase_rel])
        if new_text != out[phase_rel]:
            out[phase_rel] = new_text
            if phase_rel not in changed:
                changed.append(phase_rel)

    # 4) POST-EDIT verification: intended values present AND the active bounded
    #    authorization now equals the restored paused object (minus any preserved
    #    prior_bounded_authorization history), with the paused nesting consumed.
    import yaml
    for key, new_value in edits.items():
        relpath, locator = _parse_edit_key(key)
        doc = (json.loads(out[relpath]) if relpath.endswith(".json")
               else yaml.safe_load(out[relpath]))
        got = _dig(doc, locator)
        if str(got) != str(new_value):
            raise MaterializeError(f"post-edit mismatch for {key}: {got!r} != {new_value!r}")
    if restore is not None:
        active = _active_bounded_auth(out[phase_rel])
        active_core = {k: v for k, v in active.items() if k != "prior_bounded_authorization"}
        if _canonical_transport(active_core) != restore_transport:
            raise MaterializeError("active bounded_authorization != restored paused object")
        if "paused_bounded_authorization" in active:
            raise MaterializeError("paused_bounded_authorization was not consumed by promotion")

    return {"files": out, "changed": changed, "verified": True}


def _dig(doc: Any, dotted: str) -> Any:
    cur = doc
    for seg in dotted.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            raise MaterializeError(f"cannot read {dotted!r} (missing {seg!r})")
        cur = cur[seg]
    return cur


def branch_name(completed_mission: str, certified_sha: str) -> str:
    """Stable, deterministic governance branch / dedup identity."""
    short = (certified_sha or "")[:12]
    slug = re.sub(r"[^a-z0-9]+", "-", completed_mission.lower()).strip("-")
    return f"governance/transition-{slug}-{short}"


# --------------------------------------------------------------------------- #
# CLI (IO: git-verify + apply files). Git branch/commit/push/PR is the        #
# orchestrator effect job's job; this writes the surgical edits only.         #
# --------------------------------------------------------------------------- #
def _cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="northstar_materialize_transition.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap_apply = sub.add_parser("apply", help="verify main==certified, then write surgical edits")
    ap_apply.add_argument("--proposal", required=True)
    ap_apply.add_argument("--certified-sha", required=True)
    ap_apply.add_argument("--repo-root", default=".")
    ap_apply.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd != "apply":
        return 1
    root = Path(args.repo_root).resolve()
    # FAIL CLOSED: current main must still equal the certified SHA
    cur = subprocess.run(["git", "rev-parse", "origin/main"], capture_output=True,
                         text=True, cwd=str(root)).stdout.strip()
    if not cur or cur != args.certified_sha:
        print(f"ERROR: current main {cur!r} != certified {args.certified_sha!r} — STOP",
              file=sys.stderr)
        return 2
    try:
        proposal = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
        rels = {k.split("#", 1)[0] for k in (proposal.get("allowlisted_field_edits") or {})}
        rels.add(".agent/phase_status.yaml")
        files_text = {rel: (root / rel).read_text(encoding="utf-8") for rel in rels}
        result = materialize_edits(files_text, proposal)
    except (MaterializeError, OSError, json.JSONDecodeError) as e:
        print(f"ERROR: materialization refused: {e}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(json.dumps({"changed": result["changed"],
                          "branch": branch_name(proposal.get("completed_mission", ""),
                                                args.certified_sha)}, indent=2))
        return 0
    for rel, text in result["files"].items():
        if rel in result["changed"]:
            (root / rel).write_text(text, encoding="utf-8")
    print(json.dumps({"applied": result["changed"],
                      "branch": branch_name(proposal.get("completed_mission", ""),
                                            args.certified_sha)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
