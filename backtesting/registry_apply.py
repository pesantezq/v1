"""
Governed apply path for signal-registry weights  (🔒 PROTECTED | reversible | audited)

Pattern-Improvement Loop — Step 5. This is the ONLY layer permitted to change
``default_weight`` values in config/signal_registry.yaml, and through them how
decisions are computed. It is therefore built default-INERT and heavily gated,
mirroring portfolio_automation/retune_auto_apply.py:

  - Trigger only when an operator-authored ``approved_weight_changes.json`` exists,
    listing exact signal_ids and approved deltas. No file → no-op (the live-safety
    gate; the repo ships without this file, so nothing applies until you create it).
  - Enforce a per-change magnitude cap (``max_abs_delta``); clamp results to [0, 1];
    refuse unknown signal_ids and anything not in the approved file.
  - Snapshot the prior registry byte-for-byte under config/history/ before writing.
  - Edit the YAML surgically (only the target ``default_weight`` line) so comments
    and structure are preserved; write atomically; re-validate by reloading.
  - Audit every apply/revert to outputs/policy/registry_apply_audit.json.
  - ``revert_last`` restores the most recent snapshot byte-for-byte.

This module edits registry config DATA only. It deliberately does not import or
touch decision/scoring/recommendation logic, so it cannot change the semantics of
the six protected scores — it only changes a weight input value, reversibly.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.signal_registry import SignalRegistryError, load_signal_registry

_SIG_RE = re.compile(r"^\s*-\s*signal_id:\s*(.+?)\s*$")
_DW_RE = re.compile(r"^(\s*default_weight:\s*).*$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_ts(now_iso: str) -> str:
    """Filesystem-safe, lexically-sortable timestamp from an ISO string."""
    return re.sub(r"[^0-9]", "", now_iso)[:14] or "00000000000000"


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _fmt_weight(weight: float) -> str:
    s = f"{weight:.4f}".rstrip("0").rstrip(".")
    return s if "." in s else s + ".0"


def _set_default_weight(text: str, signal_id: str, new_weight: float) -> tuple[str, bool]:
    """Replace ONLY the default_weight line inside *signal_id*'s block, preserving
    every other byte. Returns (new_text, replaced?)."""
    out: list[str] = []
    in_block = False
    replaced = False
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\n")
        sig = _SIG_RE.match(bare)
        if sig:
            in_block = sig.group(1).strip() == signal_id
        elif in_block and not replaced:
            dw = _DW_RE.match(bare)
            if dw:
                nl = "\n" if line.endswith("\n") else ""
                line = f"{dw.group(1)}{_fmt_weight(new_weight)}{nl}"
                replaced = True
                in_block = False
        out.append(line)
    return "".join(out), replaced


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _audit_append(base_dir: str, entry: dict[str, Any]) -> None:
    from portfolio_automation.data_governance import OutputNamespace, safe_write_json
    path = Path(base_dir) / "policy" / "registry_apply_audit.json"
    existing: list[Any] = []
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = loaded
        except (OSError, json.JSONDecodeError, ValueError):
            existing = []
    existing.append(entry)
    safe_write_json(OutputNamespace.POLICY, "registry_apply_audit.json", existing, base_dir=base_dir)


def _load_approval(approval_path: str) -> dict[str, Any] | None:
    p = Path(approval_path)
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def apply_approved_changes(
    *,
    registry_path: str = "config/signal_registry.yaml",
    approval_path: str = "config/approved_weight_changes.json",
    history_dir: str = "config/history",
    base_dir: str = "outputs",
    max_abs_delta: float = 0.05,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Apply owner-approved weight deltas to the registry, within caps, reversibly
    and audited. No-op when the approval file is absent. Never raises."""
    now_iso = now_iso or _now_iso()
    approval = _load_approval(approval_path)
    if approval is None:
        return {"status": "no_approval_file", "applied": [], "rejected": []}

    changes = approval.get("changes")
    if not isinstance(changes, list) or not changes:
        return {"status": "error", "reason": "approval_has_no_changes", "applied": [], "rejected": []}

    reg_path = Path(registry_path)
    try:
        registry = load_signal_registry(str(reg_path))
        original_text = reg_path.read_text(encoding="utf-8")
        original_bytes = reg_path.read_bytes()
    except (FileNotFoundError, SignalRegistryError, OSError) as exc:
        return {"status": "error", "reason": f"registry_unreadable:{exc}", "applied": [], "rejected": []}

    planned: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, dict):
            rejected.append({"signal_id": None, "reason": "malformed_change"})
            continue
        sid = str(change.get("signal_id") or "")
        try:
            delta = float(change.get("delta"))
        except (TypeError, ValueError):
            rejected.append({"signal_id": sid, "reason": "invalid_delta"})
            continue
        defn = registry.get(sid)
        if defn is None:
            rejected.append({"signal_id": sid, "reason": "unknown_signal"})
            continue
        if abs(delta) > max_abs_delta:
            rejected.append({"signal_id": sid, "reason": f"magnitude_exceeded:{abs(delta)}>{max_abs_delta}"})
            continue
        new_weight = round(_clamp(defn.default_weight + delta), 4)
        planned.append({"signal_id": sid, "old_weight": defn.default_weight,
                        "new_weight": new_weight, "delta": round(delta, 4)})

    if not planned:
        return {"status": "no_valid_changes", "applied": [], "rejected": rejected}

    # Snapshot prior registry byte-for-byte BEFORE any write.
    Path(history_dir).mkdir(parents=True, exist_ok=True)
    snapshot = Path(history_dir) / f"signal_registry.{_safe_ts(now_iso)}.yaml"
    snapshot.write_bytes(original_bytes)

    new_text = original_text
    applied: list[dict[str, Any]] = []
    for change in planned:
        new_text, ok = _set_default_weight(new_text, change["signal_id"], change["new_weight"])
        if ok:
            applied.append(change)
        else:
            rejected.append({"signal_id": change["signal_id"], "reason": "default_weight_line_not_found"})

    if not applied:
        return {"status": "no_valid_changes", "applied": [], "rejected": rejected}

    _atomic_write_text(reg_path, new_text)

    # Re-validate; on any failure restore the snapshot and report.
    try:
        load_signal_registry(str(reg_path))
    except SignalRegistryError as exc:
        reg_path.write_bytes(original_bytes)
        return {"status": "error", "reason": f"post_write_invalid_rolled_back:{exc}",
                "applied": [], "rejected": rejected}

    _audit_append(base_dir, {
        "ts": now_iso, "applied_by": "apply", "approved_by": approval.get("approved_by"),
        "registry_path": str(reg_path), "snapshot": str(snapshot),
        "max_abs_delta": max_abs_delta, "changes": applied, "rejected": rejected,
    })
    return {"status": "applied", "applied": applied, "rejected": rejected, "snapshot": str(snapshot)}


def revert_last(
    *,
    registry_path: str = "config/signal_registry.yaml",
    history_dir: str = "config/history",
    base_dir: str = "outputs",
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Restore the most recent registry snapshot byte-for-byte. Never raises."""
    now_iso = now_iso or _now_iso()
    snaps = sorted(Path(history_dir).glob("signal_registry.*.yaml")) if Path(history_dir).is_dir() else []
    if not snaps:
        return {"status": "no_snapshot"}
    latest = snaps[-1]
    try:
        Path(registry_path).write_bytes(latest.read_bytes())
    except OSError as exc:
        return {"status": "error", "reason": f"revert_failed:{exc}"}
    _audit_append(base_dir, {"ts": now_iso, "applied_by": "revert",
                             "registry_path": str(registry_path), "restored_from": str(latest)})
    return {"status": "reverted", "restored_from": str(latest)}
