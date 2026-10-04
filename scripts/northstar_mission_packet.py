#!/usr/bin/env python3
"""Deterministic Northstar Claude-mission dispatch packet builder.

Usage:
    python scripts/northstar_mission_packet.py build --inputs <inputs.json>
    python scripts/northstar_mission_packet.py -h

Builds the minimal dispatch packet for an AUTHORIZED, auto-dispatchable Claude
mission, or STOPs. It reads authority ONLY from base-main protected state
(`authoritative_mission`) and the mission registry; a caller-supplied mission id
or prompt can NEVER override protected state or widen authority. The prompt comes
only from the protected `prompt_source`. Fails closed; idempotent by
`(mission, certified_main_sha)`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def build_mission_packet(inputs: dict[str, Any]) -> dict[str, Any]:
    """Deterministic dispatch decision. Returns {decision: DISPATCH|WOULD_DISPATCH|STOP, ...}."""
    mode = inputs.get("mode", "shadow")
    checks: list[dict] = []

    def c(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    def stop(reason: str) -> dict[str, Any]:
        return {"decision": "STOP", "reason": reason, "mode": mode,
                "controller_level": "C0.5_SHADOW" if mode == "shadow" else "enabled",
                "checks": checks, "packet": None}

    authoritative = inputs.get("authoritative_mission")
    requested = inputs.get("requested_mission")          # caller-supplied; must not override
    registry = inputs.get("registry", {}) or {}
    missions = registry.get("missions", {}) or {}
    certified_sha = inputs.get("certified_main_sha")
    protected_sha = inputs.get("protected_main_sha")
    prompt_text = inputs.get("prompt_text")              # read by CLI from the protected prompt_source

    # 1. a caller-supplied mission id may never override protected state
    if requested is not None and not c("caller_mission_matches_protected",
                                       requested == authoritative,
                                       f"requested={requested} authoritative={authoritative}"):
        return stop("caller_mission_override_rejected")

    if not c("authoritative_mission_present", bool(authoritative)):
        return stop("no_authoritative_mission")

    entry = missions.get(authoritative)
    if not c("registry_entry_present", entry is not None, authoritative or ""):
        return stop("missing_registry_entry")

    if not c("executor_claude", entry.get("executor") == "claude", str(entry.get("executor"))):
        return stop("not_a_claude_mission")
    if not c("auto_dispatch_enabled", entry.get("auto_dispatch") is True, str(entry.get("auto_dispatch"))):
        return stop("auto_dispatch_disabled")
    allowed_rc = set(registry.get("auto_dispatch_allowed_risk_classes", []) or [])
    if not c("risk_class_allowed", entry.get("risk_class") in allowed_rc,
             f"rc={entry.get('risk_class')} allowed={sorted(allowed_rc)}"):
        return stop("risk_class_requires_human")
    if entry.get("human_boundary_required") is True:
        c("human_boundary", False, "mission requires a human boundary")
        return stop("human_boundary_required")

    # 2. the exact current main sha must be certified
    if not c("main_sha_certified", bool(certified_sha) and certified_sha == protected_sha,
             f"certified={certified_sha} protected={protected_sha}"):
        return stop("main_sha_not_certified")

    # 3. the prompt must come from the protected source (never caller-provided)
    if not c("prompt_from_protected_source", bool(prompt_text) and bool(entry.get("prompt_source"))):
        return stop("missing_protected_prompt_source")

    packet = {
        "mission_id": authoritative,
        "executor": "claude",
        "risk_class": entry.get("risk_class"),
        "prompt": prompt_text,
        "prompt_source": entry.get("prompt_source"),
        "head_sha": certified_sha,
        "github_permissions": "minimal: contents:read, pull-requests:write (open PR only); NO merge, NO approve",
        "dedup_key": f"{authoritative}@{certified_sha}",
    }
    c("packet_built", True)
    decision = "DISPATCH" if mode == "enabled" else "WOULD_DISPATCH"
    return {
        "decision": decision,
        "mode": mode,
        "controller_level": "C0.5_SHADOW" if mode == "shadow" else "enabled",
        "checks": checks,
        "packet": packet,
        "bootstrap_note": "shadow mode emits the packet for inspection but dispatches nothing",
    }


def _cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="northstar_mission_packet.py", add_help=True,
                                 description="Build a deterministic Claude mission dispatch packet.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build the dispatch packet from an inputs JSON file")
    b.add_argument("--inputs", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "build":
        try:
            inputs = json.loads(Path(args.inputs).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"ERROR: cannot read inputs: {e}", file=sys.stderr)
            return 1
        print(json.dumps(build_mission_packet(inputs), indent=2, sort_keys=True))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
