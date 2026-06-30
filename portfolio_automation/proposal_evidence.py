"""Phase 10 — governance proposal hardening (observe-only).

Augments the existing sim-governance proposal flow with: overlay **power
classes** (1-6, increasing production reach), complete **evidence cards**, and
**dedup / expiration / supersession / conflict** detection. The rule: the higher
the overlay's power, the stronger the evidence required — and nothing here ever
self-activates (approval stays `pending`; production is human-gated, Phase 10 of
the architecture / sim_governance.promotion_approvals).

Pure helpers; no production mutation. AI may summarize/score evidence but cannot
flip `approval_status`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Increasing production reach. Higher class => stronger evidence + approval.
OVERLAY_POWER_CLASSES: dict[int, str] = {
    1: "explanation_only",   # annotates the memo/UI; no decision effect
    2: "ranking_only",       # reorders presentation; no decision effect
    3: "eligibility_guard",  # can exclude a name from consideration
    4: "sizing_modifier",    # bounded position-size nudge
    5: "allocation_overlay", # changes target allocation
    6: "decision_override",  # overrides a decision (highest power)
}

# Evidence floors per power class (monotonic in power).
_EVIDENCE_FLOOR: dict[int, dict[str, Any]] = {
    1: {"min_oos_sample": 0,   "min_regime_stability": 0.0, "require_validated_oos": False},
    2: {"min_oos_sample": 0,   "min_regime_stability": 0.0, "require_validated_oos": False},
    3: {"min_oos_sample": 30,  "min_regime_stability": 0.4, "require_validated_oos": True},
    4: {"min_oos_sample": 60,  "min_regime_stability": 0.5, "require_validated_oos": True},
    5: {"min_oos_sample": 100, "min_regime_stability": 0.6, "require_validated_oos": True},
    6: {"min_oos_sample": 150, "min_regime_stability": 0.7, "require_validated_oos": True},
}

__all__ = [
    "OVERLAY_POWER_CLASSES", "required_evidence", "evidence_card", "gate_proposal",
    "dedupe_proposals", "is_expired", "detect_conflicts",
]


def required_evidence(power_class: int) -> dict[str, Any]:
    floor = dict(_EVIDENCE_FLOOR.get(int(power_class), _EVIDENCE_FLOOR[6]))
    floor["human_approval"] = True   # always — AI never self-approves
    return floor


def evidence_card(
    *, proposal_id: str, proposal_type: str, hypothesis: str,
    affected_component: str, proposed_effect: str, power_class: int,
    simulation_result: dict[str, Any], baseline_comparison: dict[str, Any],
    oos_status: str, sample_size: int, cost_adjusted_result: float | None,
    regime_stability: float, risk_impact: str, max_production_impact: str,
    failure_conditions: list[str], rollback_plan: str, expiration: str,
    evidence_freshness: str, source_experiment_ids: list[str], now: str,
    conflicts: list[str] | None = None, supersedes: list[str] | None = None,
    owner: str = "research",
) -> dict[str, Any]:
    """Build a complete proposal evidence card. Always `pending`."""
    return {
        "proposal_id": proposal_id,
        "proposal_type": proposal_type,
        "hypothesis": hypothesis,
        "affected_component": affected_component,
        "proposed_effect": proposed_effect,
        "power_class": int(power_class),
        "power_class_label": OVERLAY_POWER_CLASSES.get(int(power_class), "unknown"),
        "simulation_result": simulation_result,
        "baseline_comparison": baseline_comparison,
        "oos_status": oos_status,
        "sample_size": int(sample_size),
        "cost_adjusted_result": cost_adjusted_result,
        "regime_stability": float(regime_stability),
        "risk_impact": risk_impact,
        "max_production_impact": max_production_impact,
        "failure_conditions": list(failure_conditions),
        "rollback_plan": rollback_plan,
        "expiration": expiration,
        "evidence_freshness": evidence_freshness,
        "source_experiment_ids": list(source_experiment_ids),
        "conflicts": list(conflicts or []),
        "supersedes": list(supersedes or []),
        "owner": owner,
        "created_at": now,
        "approval_status": "pending",   # never self-approves
    }


def _parse(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def is_expired(card: dict[str, Any], *, now: str) -> bool:
    exp, n = _parse(card.get("expiration")), _parse(now)
    return bool(exp and n and exp < n)


def gate_proposal(card: dict[str, Any]) -> dict[str, Any]:
    """Eligibility for human review: evidence must meet the power-class floor,
    evidence must be fresh, and the proposal must not be expired. Never
    approves — only decides whether it is worth a human's attention."""
    reasons: list[str] = []
    floor = required_evidence(card.get("power_class", 6))
    if int(card.get("sample_size", 0)) < floor["min_oos_sample"]:
        reasons.append("insufficient_evidence_for_power_class")
    elif float(card.get("regime_stability", 0.0)) < floor["min_regime_stability"]:
        reasons.append("insufficient_evidence_for_power_class")
    elif floor["require_validated_oos"] and card.get("oos_status") != "validated":
        reasons.append("insufficient_evidence_for_power_class")
    if str(card.get("evidence_freshness", "")).lower() == "stale":
        reasons.append("stale_evidence")
    return {
        "proposal_id": card.get("proposal_id"),
        "eligible_for_review": not reasons,
        "reasons": reasons,
        "human_approval_required": True,   # invariant
    }


def _dedupe_key(card: dict[str, Any]) -> tuple:
    return (card.get("affected_component"), card.get("proposed_effect"),
            card.get("power_class"))


def dedupe_proposals(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set = set()
    out: list[dict[str, Any]] = []
    for c in proposals:
        k = _dedupe_key(c)
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


# contradictory effect tokens on the same component
_OPPOSITES = [{"raise", "lower"}, {"add", "remove"}, {"buy", "sell"},
              {"include", "exclude"}, {"increase", "decrease"}]


def detect_conflicts(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag pairs targeting the same component with contradictory effects."""
    conflicts: list[dict[str, Any]] = []
    by_comp: dict[str, list[dict[str, Any]]] = {}
    for c in proposals:
        by_comp.setdefault(str(c.get("affected_component")), []).append(c)
    for comp, cards in by_comp.items():
        for i in range(len(cards)):
            for j in range(i + 1, len(cards)):
                e1 = str(cards[i].get("proposed_effect", "")).lower()
                e2 = str(cards[j].get("proposed_effect", "")).lower()
                if any({a, b} <= ({w for w in e1.split()} | {w for w in e2.split()})
                       and a in e1 and b in e2 or (a in e2 and b in e1)
                       for opp in _OPPOSITES for a, b in [tuple(opp)]):
                    conflicts.append({
                        "component": comp,
                        "proposal_ids": [cards[i].get("proposal_id"), cards[j].get("proposal_id")],
                        "effects": [e1, e2],
                    })
    return conflicts
