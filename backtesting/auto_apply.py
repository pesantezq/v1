"""
Full auto-apply orchestrator  (🔒 SANCTIONED MUTATING PATH | gated | reversible | audited)

Pattern-Improvement Loop — sub-project E. The single operator-approved exception
(2026-06-05) to the owner-gated Step 5 path: when EVERY gate clears, this module authors
``config/approved_weight_changes.json`` and invokes the existing reversible protected apply
(``backtesting.registry_apply``) WITHOUT a human in the loop — with a GPT approver layered
on top of the deterministic gates.

Design: the safety IS the gates, not the human. Fail-closed at every step; default
``enabled=False``; cannot fire until the walk-forward OOS window matures
(``oos_window.folds_possible``, ≈2027). The GPT approver may only VETO or APPROVE the
pre-bounded Step-4 delta — it can never widen a bound, change the magnitude, or pick a
different signal. Pre- AND post-apply score-invariance gates; a post-apply regression
auto-rolls-back. Kill-switch (file or env) hard-disables regardless of ``enabled``. Every
terminal decision is audited.

This is NOT observe-only (it can mutate registry config weights) — hence ``observe_only``
is False in its output. It is the lone gated mutator; everything else in the loop stays
observe-only. See docs/PATTERN_LOOP_AUTO_APPLY.md and the CLAUDE.md sanction.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_GENERATED_BY = "backtesting.auto_apply"
_KILL_SWITCH_FILE = "config/auto_apply.DISABLED"
_KILL_SWITCH_ENV = "STOCKBOT_AUTO_APPLY_DISABLED"
_NON_ACTIONABLE = {"insufficient_evidence", "unknown_signal", "no_significant_edge"}
_DEFAULT_MODEL = "gpt-4o-mini"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _month_key(now_iso: str) -> str:
    return now_iso[:7]  # YYYY-MM


def _kill_switched() -> bool:
    return Path(_KILL_SWITCH_FILE).exists() or bool(os.environ.get(_KILL_SWITCH_ENV))


def _actionable_proposals(proposals: dict) -> list[dict]:
    """Step-4 proposals with a real, significant, pre-bounded delta to act on."""
    items = (proposals or {}).get("proposals") or []
    out = []
    for p in items:
        if not isinstance(p, dict):
            continue
        try:
            delta = float(p.get("proposed_delta") or 0.0)
        except (TypeError, ValueError):
            continue
        if delta != 0.0 and str(p.get("status")) not in _NON_ACTIONABLE:
            out.append(p)
    return out


def _score_gate(registry_path: str) -> dict[str, Any]:
    """Thin wrapper over the Step-5 protected-score invariance gate (probed with the
    default representative signal). Separated so tests can monkeypatch it."""
    from backtesting.score_invariance_gate import assert_scores_invariant_across_apply
    return assert_scores_invariant_across_apply(registry_path=registry_path)


def _build_prompt(item: dict) -> str:
    return (
        "You are a risk gate for an advisory portfolio system. A walk-forward out-of-sample "
        "analysis proposes a SMALL, already-bounded change to one signal's registry weight. "
        "You may ONLY approve (apply exactly the given delta) or veto. You may NOT change the "
        "magnitude, pick a different signal, or widen any bound. Veto if the out-of-sample "
        "evidence is thin, the confidence interval straddles 50%, or anything looks off.\n\n"
        f"signal_id: {item.get('signal_id')}\n"
        f"current_weight: {item.get('current_weight')}\n"
        f"proposed_weight: {item.get('proposed_weight')}\n"
        f"proposed_delta: {item.get('proposed_delta')}\n"
        f"oos_hit_rate: {item.get('oos_hit_rate')}\n"
        f"oos_hit_rate_ci95: {item.get('oos_hit_rate_ci95')}\n"
        f"avg_return: {item.get('avg_return')}\n\n"
        'Return ONLY a JSON object: {"decision":"approve"|"veto",'
        '"within_bounds":true|false,"reason":"<one line>"}'
    )


def _parse_verdict(raw: str) -> dict[str, Any]:
    """Parse the approver's reply, fail-closed: anything not an unambiguous in-bounds
    approval is treated as a veto."""
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        doc = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError, TypeError):
        return {"decision": "veto", "within_bounds": False, "reason": "unparseable_verdict"}
    decision = str(doc.get("decision", "")).strip().lower()
    within = doc.get("within_bounds") is True
    if decision == "approve" and within:
        return {"decision": "approve", "within_bounds": True,
                "reason": str(doc.get("reason", ""))[:200]}
    return {"decision": "veto", "within_bounds": within,
            "reason": str(doc.get("reason", "")) or "not_an_in_bounds_approval"}


def _gpt_approve(item: dict, *, provider: str | None, model: str | None,
                 approver: Callable[[str], str] | None) -> dict[str, Any]:
    """Get an approve/veto verdict for one proposal. Fail-closed: any error, empty
    reply, or non-approval → veto. ``approver`` is injectable for tests."""
    prompt = _build_prompt(item)
    try:
        if approver is not None:
            raw = approver(prompt)
        else:
            from agent.llm_adapters import call_provider
            raw = call_provider(provider=provider or "openai", model=model or _DEFAULT_MODEL,
                                prompt=prompt, max_tokens=200, timeout=60)
        if not raw or not str(raw).strip():
            return {"decision": "veto", "within_bounds": False, "reason": "empty_reply"}
        return _parse_verdict(str(raw))
    except Exception as exc:  # fail-closed
        return {"decision": "veto", "within_bounds": False, "reason": f"approver_error:{exc}"}


def _load_state(state_path: str) -> dict[str, Any]:
    try:
        s = json.loads(Path(state_path).read_text(encoding="utf-8"))
        if isinstance(s, dict):
            s.setdefault("monthly_drift", {})
            return s
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {"apply_enabled": True, "monthly_drift": {}}


def _save_state(state_path: str, state: dict) -> None:
    try:
        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        Path(state_path).write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def _audit(base_dir: str, entry: dict) -> str | None:
    try:
        from portfolio_automation.data_governance import OutputNamespace, safe_write_json
        path = Path(base_dir) / "policy" / "auto_apply_audit.json"
        existing: list = []
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    existing = loaded
            except (OSError, json.JSONDecodeError, ValueError):
                existing = []
        existing.append(entry)
        safe_write_json(OutputNamespace.POLICY, "auto_apply_audit.json", existing, base_dir=base_dir)
        return str(path)
    except Exception:
        return None


def _result(status: str, *, now_iso: str, base_dir: str, write: bool,
            **extra: Any) -> dict[str, Any]:
    out = {"observe_only": False, "generated_by": _GENERATED_BY, "status": status,
           "ts": now_iso, **extra}
    if write:
        out["audit_path"] = _audit(base_dir, out)
    return out


def maybe_auto_apply(
    *,
    enabled: bool = False,
    poc: dict | None = None,
    proposals: dict | None = None,
    registry_path: str = "config/signal_registry.yaml",
    approval_path: str | None = None,
    history_dir: str | None = None,
    base_dir: str = "outputs",
    state_path: str | None = None,
    max_monthly_drift: float = 0.10,
    max_abs_delta: float = 0.05,
    provider: str | None = None,
    model: str | None = None,
    approver: Callable[[str], str] | None = None,
    now_iso: str | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Apply Step-4 weight proposals automatically IFF every gate clears. Fail-closed;
    never raises. Returns a status dict (see module docstring for the status set).

    Default ``enabled=False`` + the OOS-maturity gate mean this is a guaranteed no-op
    today. ``approver`` is injectable so tests never call a real LLM.
    """
    now_iso = now_iso or _now_iso()
    approval_path = approval_path or "config/approved_weight_changes.json"
    history_dir = history_dir or "config/history"
    state_path = state_path or "data/auto_apply_state.json"
    poc = poc or {}
    proposals = proposals or {}

    try:
        # G0 — enabled
        if not enabled:
            return _result("disabled", now_iso=now_iso, base_dir=base_dir, write=False)
        # G1 — kill-switch
        if _kill_switched():
            return _result("kill_switched", now_iso=now_iso, base_dir=base_dir, write=write)
        # G2 — OOS maturity
        if not (poc.get("oos_window") or {}).get("folds_possible"):
            return _result("oos_immature", now_iso=now_iso, base_dir=base_dir, write=False)
        # G3 — actionable proposals
        actionable = _actionable_proposals(proposals)
        if not actionable:
            return _result("no_actionable_proposal", now_iso=now_iso, base_dir=base_dir, write=False)
        total_delta = round(sum(abs(float(p.get("proposed_delta") or 0.0)) for p in actionable), 6)
        # G4 — drift cap
        state = _load_state(state_path)
        mk = _month_key(now_iso)
        used = float((state.get("monthly_drift") or {}).get(mk, 0.0))
        if used + total_delta > max_monthly_drift + 1e-9:
            return _result("drift_capped", now_iso=now_iso, base_dir=base_dir, write=write,
                           reason=f"{used}+{total_delta} > {max_monthly_drift}")
        # G5 — pre-apply score-invariance gate must be GREEN
        pre = _score_gate(registry_path)
        if pre.get("status") != "GREEN":
            return _result("score_gate_blocked", now_iso=now_iso, base_dir=base_dir, write=write,
                           gate=pre.get("status"))
        # G6 — AI budget (only the real LLM path consults it)
        if approver is None:
            try:
                from portfolio_automation.ai_budget import with_ai_budget
                with with_ai_budget(provider=provider or "openai", model=model or _DEFAULT_MODEL,
                                    estimated_input_tokens=400, estimated_output_tokens=120,
                                    observe_only=True) as budget_event:
                    if not getattr(budget_event, "allowed", True):
                        return _result("budget_exceeded", now_iso=now_iso, base_dir=base_dir, write=write)
            except Exception:
                pass  # budget telemetry failure must not force an apply; proceed to GPT gate
        # G7 — GPT approver (per item; only approved items are applied)
        approved: list[dict] = []
        verdicts: list[dict] = []
        for item in actionable:
            v = _gpt_approve(item, provider=provider, model=model, approver=approver)
            verdicts.append({"signal_id": item.get("signal_id"), **v})
            if v["decision"] == "approve":
                approved.append(item)
        if not approved:
            return _result("gpt_vetoed", now_iso=now_iso, base_dir=base_dir, write=write,
                           verdicts=verdicts)

        # Author the approval artifact (the protected path's input) and apply.
        approved_delta = round(sum(abs(float(p.get("proposed_delta") or 0.0)) for p in approved), 6)
        approval = {
            "approved_by": "auto_apply:gpt_approver",
            "generated_at": now_iso,
            "provenance": {"source": _GENERATED_BY, "verdicts": verdicts,
                           "pre_score_gate": pre.get("status")},
            "changes": [{"signal_id": p.get("signal_id"),
                         "delta": round(float(p.get("proposed_delta")), 4)} for p in approved],
        }
        Path(approval_path).parent.mkdir(parents=True, exist_ok=True)
        Path(approval_path).write_text(json.dumps(approval, indent=2), encoding="utf-8")

        from backtesting.registry_apply import apply_approved_changes, revert_last
        applied = apply_approved_changes(
            registry_path=registry_path, approval_path=approval_path,
            history_dir=history_dir, base_dir=base_dir, max_abs_delta=max_abs_delta,
            now_iso=now_iso,
        )
        if applied.get("status") != "applied":
            return _result("apply_failed", now_iso=now_iso, base_dir=base_dir, write=write,
                           apply_result=applied)

        # Post-apply score-invariance gate — regression → auto-rollback.
        post = _score_gate(registry_path)
        if post.get("status") == "RED":
            rev = revert_last(registry_path=registry_path, history_dir=history_dir,
                              base_dir=base_dir, now_iso=now_iso)
            return _result("rolled_back", now_iso=now_iso, base_dir=base_dir, write=write,
                           gate=post.get("status"), revert=rev.get("status"),
                           changes=applied.get("applied"))

        # Commit the drift and report success.
        state.setdefault("monthly_drift", {})[mk] = round(used + approved_delta, 6)
        _save_state(state_path, state)
        return _result("applied", now_iso=now_iso, base_dir=base_dir, write=write,
                       changes=applied.get("applied"), verdicts=verdicts,
                       monthly_drift=state["monthly_drift"][mk])
    except Exception as exc:  # never raise from the loop
        return _result("error", now_iso=now_iso, base_dir=base_dir, write=False, error=str(exc))
