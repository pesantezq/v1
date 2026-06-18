"""Live, observe-only readiness assessor for the operator worker.

Five primary gates (auth, bounded_cmd, audit, rollback, quarantine). Cost is a
SEPARATE telemetry line, never a gate. Auto gates are verified from the
environment/filesystem/code; declared gates read an evidence-backed attestation
block from config and DEFAULT TO AMBER unless every validation rule passes.
This is advisory health state — NOT authorization to execute workers.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from operator_control.worker_container import (
    validate_container_configuration,
    probe_container_capabilities,
    verify_runtime_attestation,
)

RECOGNIZED_STATUSES = frozenset({"green", "amber", "red"})
DECLARED_GATES = ("bounded_cmd", "rollback")
_REQUIRED_DECL_KEYS = ("status", "declared_by", "declared_at", "evidence")


def _amber(reason: str, source: str) -> dict[str, Any]:
    return {"status": "amber", "reason": reason, "source": source}


def _running_as_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def _in_container(root: Path) -> bool:
    if Path("/.dockerenv").exists():
        return True
    if Path("/run/.containerenv").exists():
        return True
    try:
        cg = Path("/proc/1/cgroup").read_text(encoding="utf-8")
        return any(t in cg for t in ("docker", "containerd", "libpod", "kubepods"))
    except OSError:
        return False


def _auth_gate(root: Path) -> dict[str, Any]:
    try:
        cfg_all = json.loads((root / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return _amber("worker_container config unreadable", "auto")
    wc = (cfg_all.get("operator_control", {}) or {}).get("worker_container") or {}
    if not wc.get("enabled"):
        return _amber("container mode disabled — worker would run unisolated", "auto")
    ok, reasons = validate_container_configuration(wc)
    if not ok:
        return _amber("static checks failed: " + "; ".join(reasons), "auto")
    caps = probe_container_capabilities(wc)
    if not all((caps["podman_present"], caps["image_present"], caps["digest_pinned"], caps["rootless_ok"])):
        return _amber(f"capability probe failed ({caps})", "auto")
    att_path = root / wc.get("attestation_path", "outputs/operator_control/worker_attestation.json")
    try:
        att = json.loads(att_path.read_text(encoding="utf-8"))
    except Exception:
        return _amber("configured but not runtime-verified (no attestation)", "auto")
    cfg_path = str(root / "config.json")
    image_build_ts = wc.get("image_build_ts") or os.path.getmtime(cfg_path)
    config_mtime = os.path.getmtime(cfg_path)
    a_ok, a_reasons = verify_runtime_attestation(
        att, wc, now=time.time(),
        image_build_ts=image_build_ts,
        config_mtime=config_mtime,
    )
    if not a_ok:
        return _amber("attestation invalid/stale: " + "; ".join(a_reasons), "auto")
    return {
        "status": "green",
        "reason": "container-isolated, runtime-attested (egress: unrestricted — deferred)",
        "source": "auto",
    }


def _audit_gate(root: Path) -> dict[str, Any]:
    d = root / "outputs" / "operator_control"
    if (d / "audit_log.jsonl").exists() and (d / "worker_cost_log.jsonl").exists():
        return {"status": "green",
                "reason": "audit_log.jsonl + worker_cost_log.jsonl present",
                "source": "auto"}
    return _amber("operator-control audit/cost logs missing", "auto")


def _quarantine_gate(root: Path) -> dict[str, Any]:
    # Evaluate whether the protected-path control is IMPLEMENTED + TESTED.
    # (Inventory is shown separately and is NOT proof the control works.)
    try:
        from operator_control.protected_paths import is_protected  # noqa: F401
    except Exception:
        return _amber("protected-path guard not importable", "auto")
    tested = (root / "tests" / "test_operator_protected_paths.py").exists()
    if tested:
        return {"status": "green",
                "reason": "protected-path guard implemented + tested", "source": "auto"}
    return _amber("protected-path guard present but untested", "auto")


def _declared_gate(name: str, cfg_block: dict[str, Any], root: Path) -> dict[str, Any]:
    decl = (cfg_block or {}).get(name)
    if not isinstance(decl, dict):
        return _amber(f"no declaration for {name}", "declared")
    if any(k not in decl for k in _REQUIRED_DECL_KEYS):
        return _amber(f"{name} declaration malformed", "declared")
    status = decl.get("status")
    if status not in RECOGNIZED_STATUSES:
        return _amber(f"{name} declared status unrecognized", "declared")
    evidence = decl.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return _amber(f"{name} declaration has no evidence", "declared")
    if not all(isinstance(e, str) and (root / e).exists() for e in evidence):
        return _amber(f"{name} evidence references missing files", "declared")
    return {
        "status": status, "source": "declared",
        "reason": decl.get("note", ""),
        "declared_by": decl.get("declared_by"),
        "declared_at": decl.get("declared_at"),
        "evidence": list(evidence),
    }


def _autonomous_enabled_safe(root: Path) -> bool:
    """Canonical accessor — honors the operator_control.autonomous_worker.enabled
    config AND the config/operator_worker.DISABLED kill-switch file."""
    try:
        from operator_control.worker_runner import autonomous_enabled
        return bool(autonomous_enabled(root))
    except Exception:
        return False


def _cost(root: Path, oc_cfg: dict[str, Any]) -> dict[str, Any]:
    lifetime = 0.0
    p = root / "outputs" / "operator_control" / "worker_cost_log.jsonl"
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                lifetime += float(json.loads(line).get("cost_usd") or 0.0)
            except (ValueError, json.JSONDecodeError):
                continue
    except OSError:
        pass
    cap = oc_cfg.get("cost_cap_usd_per_day")
    cap_configured = isinstance(cap, (int, float)) and cap > 0
    cap_pct = round(lifetime / cap * 100, 1) if cap_configured else None
    return {"lifetime_usd": round(lifetime, 4),
            "cap_usd": cap if cap_configured else None,
            "cap_pct": cap_pct, "cap_configured": bool(cap_configured)}


def operator_worker_readiness(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    try:
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
        oc = cfg.get("operator_control", {}) or {}
        declared = oc.get("readiness_declared", {}) or {}
        gates = {
            "auth": _auth_gate(root),
            "audit": _audit_gate(root),
            "quarantine": _quarantine_gate(root),
            "bounded_cmd": _declared_gate("bounded_cmd", declared, root),
            "rollback": _declared_gate("rollback", declared, root),
        }
        green = sum(1 for g in gates.values() if g["status"] == "green")
        return {
            "observe_only": True,
            "gates": gates,
            "overall_ready": f"{green}/5",
            "autonomous_enabled": _autonomous_enabled_safe(root),
            "cost": _cost(root, oc),
        }
    except Exception as exc:  # degraded, never raises to caller
        return {"observe_only": True, "error": f"{type(exc).__name__}: {exc}",
                "gates": {}, "overall_ready": "0/5",
                "cost": {"lifetime_usd": 0.0, "cap_usd": None,
                         "cap_pct": None, "cap_configured": False}}
