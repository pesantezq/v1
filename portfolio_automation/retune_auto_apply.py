"""
Retune Auto-Apply — reads gate_retune_suggestions.json and applies only
the rows where auto_applicable=true AND every guardrail clears.

Guardrails (all must hold; ANY failure → propose-only, no mutation):
  1. Suggestion must be flagged auto_applicable in the source artifact
  2. |Δ| within magnitude bounds (0.03 weight / 0.05 threshold)
  3. n_samples ≥ 200
  4. Same proposal must appear in 2 consecutive auto-apply runs
     (tracked in data/retune_auto_apply_state.json:pending_confirmations)
  5. Monthly cumulative |Δ| per parameter ≤ 0.25
     (tracked in data/retune_auto_apply_state.json:monthly_drift)
  6. apply_enabled flag in state file (operator can hard-disable at any time)

Every application is recorded in data/retune_audit_log.jsonl with:
  - timestamp, parameter, old_value, new_value, delta, n_samples,
    evidence_delta_pp, suggestion_artifact_hash, applied_by="auto"

Rollback: `python -m portfolio_automation.retune_auto_apply --rollback <param>`
re-reads the most recent audit entry for the parameter and writes the
old_value back into config.json with a new audit entry tagged
applied_by="rollback".

Hard guarantees:
  - The ONLY mutator of config.json's tunable parameters in the retune loop
  - Every write goes through _apply_one() which enforces all guardrails
  - Audit log is append-only JSONL; never rewritten

Public API:
  apply_suggestions(root, *, force=False) -> dict
  rollback(root, parameter) -> dict
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("stockbot.portfolio_automation.retune_auto_apply")

_STATE_REL = ("data", "retune_auto_apply_state.json")
_AUDIT_REL = ("data", "retune_audit_log.jsonl")
_CONFIG_REL = "config.json"
_SUGGESTIONS_REL = ("outputs", "latest", "gate_retune_suggestions.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_month_key() -> str:
    n = datetime.now(timezone.utc)
    return f"{n.year:04d}-{n.month:02d}"


def _hash_payload(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# State + audit
# ---------------------------------------------------------------------------


def _load_state(root: Path) -> dict[str, Any]:
    p = root.joinpath(*_STATE_REL)
    if not p.exists():
        return {
            "apply_enabled": True,
            "month": _current_month_key(),
            "pending_confirmations": {},   # parameter → last_seen_proposal_dict
            "monthly_drift": {},           # parameter → cumulative |Δ| this month
        }
    try:
        s = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        s = {}
    s.setdefault("apply_enabled", True)
    s.setdefault("pending_confirmations", {})
    s.setdefault("monthly_drift", {})
    if s.get("month") != _current_month_key():
        s["month"] = _current_month_key()
        s["monthly_drift"] = {}
        s["pending_confirmations"] = {}
    return s


def _write_state(root: Path, state: dict[str, Any]) -> None:
    p = root.joinpath(*_STATE_REL)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True))


def _audit_append(root: Path, entry: dict[str, Any]) -> None:
    p = root.joinpath(*_AUDIT_REL)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def _audit_last_entry(root: Path, parameter: str) -> dict[str, Any] | None:
    p = root.joinpath(*_AUDIT_REL)
    if not p.exists():
        return None
    last = None
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("parameter") == parameter:
            last = d
    return last


# ---------------------------------------------------------------------------
# Config IO
# ---------------------------------------------------------------------------


def _read_config(root: Path) -> dict[str, Any]:
    p = root / _CONFIG_REL
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def _write_config(root: Path, cfg: dict[str, Any]) -> None:
    p = root / _CONFIG_REL
    p.write_text(json.dumps(cfg, indent=2) + "\n")


def _resolve_param_path(cfg: dict[str, Any], parameter: str) -> tuple[dict[str, Any], str]:
    """Walk dotted parameter path; create intermediate dicts as needed.
    Returns (parent_dict, leaf_key)."""
    parts = parameter.split(".")
    cursor = cfg
    for key in parts[:-1]:
        if key not in cursor or not isinstance(cursor[key], dict):
            cursor[key] = {}
        cursor = cursor[key]
    return cursor, parts[-1]


def _get_current(cfg: dict[str, Any], parameter: str, default: Any = None) -> Any:
    parts = parameter.split(".")
    cursor = cfg
    for key in parts:
        if not isinstance(cursor, dict) or key not in cursor:
            return default
        cursor = cursor[key]
    return cursor


# ---------------------------------------------------------------------------
# Apply logic
# ---------------------------------------------------------------------------


def _apply_one(
    root: Path,
    cfg: dict[str, Any],
    state: dict[str, Any],
    proposal: dict[str, Any],
    artifact_hash: str,
) -> dict[str, Any]:
    """Apply one suggestion if all guardrails clear. Returns a result dict."""
    parameter = proposal["parameter"]
    proposed = proposal["proposed_value"]
    delta_abs = abs(float(proposal.get("delta") or 0.0))
    n = int(proposal.get("n_samples") or 0)

    # Guardrail 1 — must be flagged auto_applicable upstream
    if not proposal.get("auto_applicable"):
        return {"parameter": parameter, "action": "skipped", "reason": "not_auto_applicable"}

    # Guardrail 2 — magnitude is enforced by the suggestion builder,
    # but we re-check here in case the artifact was tampered with
    is_weight = parameter.startswith("sanitation_weight.")
    cap = 0.03 if is_weight else 0.05
    if delta_abs > cap:
        return {"parameter": parameter, "action": "skipped",
                "reason": f"magnitude_exceeded:{delta_abs}>{cap}"}

    # Guardrail 3 — sample size
    if n < 200:
        return {"parameter": parameter, "action": "skipped",
                "reason": f"insufficient_samples:{n}<200"}

    # Guardrail 4 — 2-run confirmation
    pending = state["pending_confirmations"].get(parameter)
    confirm_token = (
        round(float(proposed), 4),
        round(float(proposal.get("delta") or 0.0), 4),
    )
    if pending != list(confirm_token):
        state["pending_confirmations"][parameter] = list(confirm_token)
        return {"parameter": parameter, "action": "queued_for_confirmation",
                "proposed": proposed, "delta": proposal.get("delta")}

    # Guardrail 5 — monthly drift cap
    cum = float(state["monthly_drift"].get(parameter, 0.0))
    if cum + delta_abs > 0.25:
        return {"parameter": parameter, "action": "skipped",
                "reason": f"monthly_drift_cap:{cum}+{delta_abs}>0.25"}

    # All guardrails clear — apply
    current = _get_current(cfg, parameter)
    if current is None and is_weight:
        # sanitation weights aren't in config.json today — they live as
        # module constants. We persist them in config so they're discoverable
        # by future readers; the sanitation module can be updated later to
        # read from config first, fall back to constants.
        current = {
            "sanitation_weight.sources":  0.40,
            "sanitation_weight.theme":    0.30,
            "sanitation_weight.hit_rate": 0.20,
            "sanitation_weight.fmp":      0.10,
        }.get(parameter, 0.0)

    parent, leaf = _resolve_param_path(cfg, parameter)
    parent[leaf] = proposed

    # Audit
    _audit_append(root, {
        "ts": _now_iso(),
        "parameter": parameter,
        "old_value": current,
        "new_value": proposed,
        "delta": proposal.get("delta"),
        "n_samples": n,
        "evidence_delta_pp": proposal.get("evidence_delta_pp"),
        "significance": proposal.get("significance"),
        "suggestion_artifact_hash": artifact_hash,
        "applied_by": "auto",
    })

    # Update state
    state["monthly_drift"][parameter] = round(cum + delta_abs, 4)
    state["pending_confirmations"].pop(parameter, None)

    return {
        "parameter": parameter, "action": "applied",
        "old_value": current, "new_value": proposed, "delta": proposal.get("delta"),
    }


def apply_suggestions(
    *,
    root: str | Path = ".",
    force: bool = False,
) -> dict[str, Any]:
    """Read gate_retune_suggestions.json and apply any rows that clear all
    guardrails. Updates config.json + state + audit log in lockstep."""
    root_path = Path(root).resolve()
    p = root_path.joinpath(*_SUGGESTIONS_REL)
    if not p.exists():
        return {"status": "skipped", "reason": "no_suggestions_artifact"}
    suggestions = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    if not suggestions.get("available"):
        return {"status": "skipped", "reason": "suggestions_unavailable"}

    state = _load_state(root_path)
    if not state.get("apply_enabled") and not force:
        return {"status": "skipped", "reason": "apply_disabled_by_state"}

    cfg = _read_config(root_path)
    artifact_hash = _hash_payload(suggestions)

    actions: list[dict[str, Any]] = []
    candidates = list(suggestions.get("weight_proposals") or [])
    gp = suggestions.get("gate_proposal")
    if gp:
        candidates.append(gp)

    cfg_dirty = False
    for proposal in candidates:
        before = json.dumps(cfg, sort_keys=True)
        res = _apply_one(root_path, cfg, state, proposal, artifact_hash)
        actions.append(res)
        if res["action"] == "applied":
            cfg_dirty = True
        # Always check whether _apply_one changed config (for safety)
        if json.dumps(cfg, sort_keys=True) != before:
            cfg_dirty = True

    if cfg_dirty:
        _write_config(root_path, cfg)
    _write_state(root_path, state)

    applied_count = sum(1 for a in actions if a["action"] == "applied")
    queued_count = sum(1 for a in actions if a["action"] == "queued_for_confirmation")
    skipped_count = sum(1 for a in actions if a["action"] == "skipped")
    return {
        "status": "ok",
        "applied_count": applied_count,
        "queued_count": queued_count,
        "skipped_count": skipped_count,
        "actions": actions,
        "artifact_hash": artifact_hash,
    }


def rollback(*, root: str | Path = ".", parameter: str) -> dict[str, Any]:
    """Reverse the most recent auto-applied change for `parameter`.
    Writes a fresh audit entry tagged applied_by='rollback'."""
    root_path = Path(root).resolve()
    last = _audit_last_entry(root_path, parameter)
    if not last:
        return {"status": "error", "error": f"no_audit_entry_for:{parameter}"}
    if last.get("applied_by") == "rollback":
        return {"status": "skipped", "reason": "already_at_rollback_state"}
    cfg = _read_config(root_path)
    parent, leaf = _resolve_param_path(cfg, parameter)
    new_value = last.get("old_value")
    old_value = parent.get(leaf)
    parent[leaf] = new_value
    _write_config(root_path, cfg)
    _audit_append(root_path, {
        "ts": _now_iso(),
        "parameter": parameter,
        "old_value": old_value,
        "new_value": new_value,
        "delta": None,
        "applied_by": "rollback",
        "reverts_audit_ts": last.get("ts"),
    })
    return {"status": "ok", "parameter": parameter, "old_value": old_value, "new_value": new_value}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply pending suggestions")
    parser.add_argument("--rollback", metavar="PARAMETER", help="Roll back the named parameter")
    parser.add_argument("--force", action="store_true", help="Bypass apply_enabled flag")
    parser.add_argument("--root", default=None)
    args = parser.parse_args()
    root_arg = Path(args.root) if args.root else Path(__file__).resolve().parents[1]

    if args.rollback:
        r = rollback(root=root_arg, parameter=args.rollback)
    else:
        r = apply_suggestions(root=root_arg, force=args.force)
    print(json.dumps(r, indent=2))
    import sys
    sys.exit(0 if r.get("status") == "ok" else 1)
