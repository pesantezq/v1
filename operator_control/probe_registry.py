"""Probe registry — the machine-readable catalog of known dashboard probes.

A *probe* is a named, recurring health/quality concern the dashboard already
surfaces (a failed pipeline stage, data-quality warnings, an AI-budget breach,
a near-cap portfolio risk, …). Each probe maps to a **recommended skill** and a
bounded set of allowed actions, so the dashboard can offer "Diagnose" /
"Propose Fix" buttons that are derived from this registry rather than hardcoded
per card.

Probes REFERENCE existing artifact paths (``source_artifact``) — they never copy
artifact contents. The artifacts themselves remain the source of truth.

Nothing here executes anything; this is pure data + lookup helpers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Severity vocabulary shared with the GUI status palette (info/warning/red are
# the actionable ones; ``ok`` probes exist for completeness but rarely create
# work orders).
SEVERITIES = ("info", "warning", "red")

# Risk levels, ordered low → high. Used by repair_policies to decide approval.
RISK_LEVELS = ("low", "medium", "high")

# The full action vocabulary. A probe's ``allowed_actions`` is a subset.
ACTIONS = ("diagnose", "propose_fix", "safe_repair")

_OBSERVE_ONLY_NOTICE = (
    "Observe-only: creates a work order for review. No trades, no broker "
    "orders, no code execution from this action."
)
_PROPOSAL_ONLY_NOTICE = (
    "Proposal-only evidence. NOT official advice. The decision_plan.json "
    "remains the sole source of advisory actions. No trades executed."
)


@dataclass(frozen=True)
class Probe:
    probe_id: str
    display_name: str
    source_view: str  # today | portfolio | quant | system | memo
    source_artifact: str
    severity: str
    description: str
    recommended_skill_id: str
    allowed_actions: tuple[str, ...]
    risk_level: str
    approval_required: bool
    observe_only_notice: str = _OBSERVE_ONLY_NOTICE

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "display_name": self.display_name,
            "source_view": self.source_view,
            "source_artifact": self.source_artifact,
            "severity": self.severity,
            "description": self.description,
            "recommended_skill_id": self.recommended_skill_id,
            "allowed_actions": list(self.allowed_actions),
            "risk_level": self.risk_level,
            "approval_required": self.approval_required,
            "observe_only_notice": self.observe_only_notice,
        }


# ---------------------------------------------------------------------------
# The registry — one entry per known dashboard probe.
# ---------------------------------------------------------------------------

_PROBES: tuple[Probe, ...] = (
    # ── System / Developer lens ──────────────────────────────────────────
    Probe(
        probe_id="daily_run.failed_stages",
        display_name="Daily run — failed/warned stages",
        source_view="system",
        source_artifact="outputs/latest/daily_run_status.json",
        severity="red",
        description="One or more daily pipeline stages reported failed or warn.",
        recommended_skill_id="diagnose_daily_run_failure",
        allowed_actions=("diagnose",),
        risk_level="low",
        approval_required=False,
    ),
    Probe(
        probe_id="data_quality.warnings",
        display_name="Data quality warnings",
        source_view="system",
        source_artifact="outputs/latest/data_quality_report.json",
        severity="warning",
        description="Symbol-level data-quality warnings or critical symbols present.",
        recommended_skill_id="diagnose_data_quality_warnings",
        allowed_actions=("diagnose", "propose_fix", "safe_repair"),
        risk_level="low",
        approval_required=False,
    ),
    Probe(
        probe_id="pipeline.run_status",
        display_name="Pipeline run status",
        source_view="system",
        source_artifact="outputs/latest/pipeline_run_status.json",
        severity="warning",
        description="Step-level pipeline run health (succeeded/failed/skipped).",
        recommended_skill_id="diagnose_pipeline_status",
        allowed_actions=("diagnose",),
        risk_level="low",
        approval_required=False,
    ),
    Probe(
        probe_id="ai_budget.status",
        display_name="AI / LLM budget status",
        source_view="system",
        source_artifact="outputs/latest/ai_budget_summary.json",
        severity="warning",
        description="AI/LLM spend near or over the configured daily/monthly cap.",
        recommended_skill_id="diagnose_pipeline_status",
        allowed_actions=("diagnose",),
        risk_level="low",
        approval_required=False,
    ),
    Probe(
        probe_id="fmp_budget.status",
        display_name="FMP / API budget status",
        source_view="system",
        source_artifact="outputs/latest/fmp_budget_status.json",
        severity="warning",
        description="FMP API call budget near or over the configured cap.",
        recommended_skill_id="diagnose_pipeline_status",
        allowed_actions=("diagnose",),
        risk_level="low",
        approval_required=False,
    ),
    Probe(
        probe_id="memo.delivery_status",
        display_name="Memo delivery status",
        source_view="system",
        source_artifact="outputs/latest/memo_delivery_status.json",
        severity="warning",
        description="Memo email delivery skipped/failed (delivery health only).",
        recommended_skill_id="diagnose_pipeline_status",
        allowed_actions=("diagnose",),
        risk_level="low",
        approval_required=False,
    ),
    Probe(
        probe_id="schwab.broker_health",
        display_name="Schwab broker health (read-only)",
        source_view="system",
        source_artifact="outputs/latest/broker_sync_status.json",
        severity="info",
        description="Schwab read-only sync connection/auth health. No trade capability.",
        recommended_skill_id="diagnose_schwab_read_only_health",
        allowed_actions=("diagnose",),
        risk_level="low",
        approval_required=False,
    ),
    Probe(
        probe_id="artifact_registry.status",
        display_name="Artifact registry status",
        source_view="system",
        source_artifact="outputs/latest/artifact_registry_status.json",
        severity="warning",
        description="Artifact governance registry reports missing/stale/unattributed rows.",
        recommended_skill_id="inspect_artifact_registry",
        allowed_actions=("diagnose",),
        risk_level="low",
        approval_required=False,
    ),
    # ── Quant lens (proposal-only) ───────────────────────────────────────
    Probe(
        probe_id="quant.confidence_calibration",
        display_name="Quant — confidence calibration",
        source_view="quant",
        source_artifact="outputs/latest/confidence_calibration.json",
        severity="warning",
        description="Confidence calibration slope/coverage looks off; review only.",
        recommended_skill_id="diagnose_quant_calibration",
        allowed_actions=("diagnose",),
        risk_level="low",
        approval_required=False,
        observe_only_notice=_PROPOSAL_ONLY_NOTICE,
    ),
    Probe(
        probe_id="quant.pattern_efficacy",
        display_name="Quant — pattern efficacy / insufficient history",
        source_view="quant",
        source_artifact="outputs/latest/pattern_efficacy_weekly.json",
        severity="info",
        description="Pattern efficacy weak or insufficient OOS history; review only.",
        recommended_skill_id="diagnose_quant_calibration",
        allowed_actions=("diagnose",),
        risk_level="low",
        approval_required=False,
        observe_only_notice=_PROPOSAL_ONLY_NOTICE,
    ),
    Probe(
        probe_id="quant.retune_suggestions",
        display_name="Quant — retune suggestions",
        source_view="quant",
        source_artifact="outputs/latest/gate_retune_suggestions.json",
        severity="info",
        description="Gate/weight retune suggestions present; generate a review work order.",
        recommended_skill_id="propose_quant_retune_review",
        allowed_actions=("diagnose", "propose_fix"),
        risk_level="medium",
        approval_required=True,
        observe_only_notice=_PROPOSAL_ONLY_NOTICE,
    ),
    Probe(
        probe_id="quant.regime_classifier_health",
        display_name="Quant — regime classifier health / degeneracy",
        source_view="quant",
        source_artifact="outputs/latest/quant_watch_status.json",
        severity="warning",
        description="Market-regime classifier output diversity. Flags a "
        "degenerate collapse (e.g. signal_outcomes regime_label stuck at a "
        "single constant bucket). Supports simulation-lane diagnosis and a "
        "narrow, reversible, human-approved producer-wiring repair. Never "
        "tunes thresholds to manufacture diversity; never alters allocations "
        "or production decisions.",
        recommended_skill_id="diagnose_regime_classifier",
        allowed_actions=("diagnose", "propose_fix", "safe_repair"),
        risk_level="medium",
        approval_required=True,
    ),
    # ── Portfolio lens (advisory / read-only) ────────────────────────────
    Probe(
        probe_id="portfolio.risk_near_cap",
        display_name="Portfolio — risk near cap",
        source_view="portfolio",
        source_artifact="outputs/latest/risk_delta.json",
        severity="warning",
        description="A portfolio risk metric is near or at its configured cap.",
        recommended_skill_id="diagnose_portfolio_risk",
        allowed_actions=("diagnose",),
        risk_level="low",
        approval_required=False,
    ),
    Probe(
        probe_id="portfolio.advisory_decision_queue",
        display_name="Portfolio — advisory decision queue",
        source_view="portfolio",
        source_artifact="outputs/latest/decision_plan.json",
        severity="info",
        description="Explain/triage the advisory decision queue. Read-only; advisory only.",
        recommended_skill_id="diagnose_portfolio_risk",
        allowed_actions=("diagnose",),
        risk_level="low",
        approval_required=False,
    ),
    # ── Memo lens ────────────────────────────────────────────────────────
    Probe(
        probe_id="memo.generation_readability",
        display_name="Memo — generation / readability",
        source_view="memo",
        source_artifact="outputs/latest/daily_memo.md",
        severity="info",
        description="Diagnose memo formatting/readability or regenerate memo from artifacts.",
        recommended_skill_id="regenerate_memo_from_artifacts",
        allowed_actions=("diagnose", "safe_repair"),
        risk_level="medium",
        approval_required=True,
    ),
)

PROBES: dict[str, Probe] = {p.probe_id: p for p in _PROBES}


# ---------------------------------------------------------------------------
# Lookup / validation helpers
# ---------------------------------------------------------------------------


def get_probe(probe_id: str) -> Probe | None:
    return PROBES.get(probe_id)


def probe_ids() -> list[str]:
    return list(PROBES.keys())


def list_probes() -> list[Probe]:
    return list(PROBES.values())


def probes_for_view(source_view: str) -> list[Probe]:
    return [p for p in PROBES.values() if p.source_view == source_view]


def validate_registry() -> list[str]:
    """Return a list of structural problems (empty = valid).

    Checks every probe's fields are internally consistent. Cross-registry
    allowlist consistency (probe.recommended_skill_id must accept the probe)
    is validated in :mod:`operator_control.skill_registry`.
    """
    errors: list[str] = []
    seen: set[str] = set()
    for p in PROBES.values():
        if p.probe_id in seen:
            errors.append(f"duplicate probe_id: {p.probe_id}")
        seen.add(p.probe_id)
        if p.severity not in SEVERITIES + ("ok",):
            errors.append(f"{p.probe_id}: bad severity {p.severity!r}")
        if p.risk_level not in RISK_LEVELS:
            errors.append(f"{p.probe_id}: bad risk_level {p.risk_level!r}")
        if not p.allowed_actions:
            errors.append(f"{p.probe_id}: empty allowed_actions")
        for a in p.allowed_actions:
            if a not in ACTIONS:
                errors.append(f"{p.probe_id}: bad action {a!r}")
        if p.source_view not in ("today", "portfolio", "quant", "system", "memo"):
            errors.append(f"{p.probe_id}: bad source_view {p.source_view!r}")
        if not p.source_artifact:
            errors.append(f"{p.probe_id}: empty source_artifact")
        if not p.recommended_skill_id:
            errors.append(f"{p.probe_id}: empty recommended_skill_id")
    return errors


__all__ = [
    "Probe",
    "PROBES",
    "SEVERITIES",
    "RISK_LEVELS",
    "ACTIONS",
    "get_probe",
    "probe_ids",
    "list_probes",
    "probes_for_view",
    "validate_registry",
]
