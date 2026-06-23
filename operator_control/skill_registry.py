"""Skill registry — the allowlist of work-order skills.

A *skill* is a bounded capability a future Claude Code worker may perform for a
given probe: ``diagnose`` (read + explain), ``propose_fix`` (write a proposal,
never apply), or ``safe_repair`` (a narrow, reversible, test-gated repair of a
non-protected artifact). Each skill declares which probes it accepts
(``allowed_probe_ids``), which modes it permits, what it must never do
(``forbidden_actions``), the tests a worker must run, and which modes require
human approval before a worker could ever act.

The dashboard derives its operator actions from probe → recommended skill, then
this registry's allowlist. A (probe, skill, mode) tuple that is not explicitly
allowed here cannot become a work order.

GLOBAL FORBIDDEN ACTIONS apply to EVERY skill, on top of any skill-specific
entries. These encode the system's hard boundaries (no trades, no broker
orders, no scoring/decision-logic edits, no secrets, no arbitrary shell, no
deploy/systemd/dependency changes).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from operator_control.probe_registry import ACTIONS, RISK_LEVELS, PROBES

# Applied to every skill's effective forbidden list. These mirror the hard
# boundaries in CLAUDE.md and are repeated into every generated worker prompt.
GLOBAL_FORBIDDEN_ACTIONS: tuple[str, ...] = (
    "Execute, simulate, or place any trade or broker order",
    "Introduce broker integration, execution logic, or auto-trading behavior",
    "Modify investment logic, scoring logic, allocation targets, signal logic, "
    "or recommendation logic",
    "Edit decision_engine.py, scoring.py, or any score-semantics "
    "(signal_score/confidence_score/effective_score/conviction_score/"
    "final_rank_score/recommendation_score)",
    "Read, print, log, or exfiltrate secrets or credentials",
    "Run arbitrary shell commands requested via the web UI",
    "Install or upgrade dependencies",
    "Restart services, edit systemd units, or change deployment configuration",
    "Recompute or override decision_plan.json outside the core decision layers",
    "Remove or make conditional the observe_only invariant",
)


@dataclass(frozen=True)
class Skill:
    skill_id: str
    name: str
    description: str
    allowed_probe_ids: tuple[str, ...]
    allowed_modes: tuple[str, ...]  # subset of ACTIONS
    forbidden_actions: tuple[str, ...]  # skill-specific; global added on top
    required_tests: tuple[str, ...]
    risk_level: str
    approval_required_for_modes: tuple[str, ...]
    output_report_requirements: tuple[str, ...]

    def effective_forbidden_actions(self) -> list[str]:
        """Skill-specific forbidden actions plus the global hard boundaries."""
        seen: list[str] = list(self.forbidden_actions)
        for a in GLOBAL_FORBIDDEN_ACTIONS:
            if a not in seen:
                seen.append(a)
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "allowed_probe_ids": list(self.allowed_probe_ids),
            "allowed_modes": list(self.allowed_modes),
            "forbidden_actions": self.effective_forbidden_actions(),
            "required_tests": list(self.required_tests),
            "risk_level": self.risk_level,
            "approval_required_for_modes": list(self.approval_required_for_modes),
            "output_report_requirements": list(self.output_report_requirements),
        }


_REPORT_BASE = (
    "Summary of the issue investigated",
    "Source artifacts inspected (paths)",
    "Findings / root-cause analysis",
    "Changes made (files + diff summary) or 'none — diagnose only'",
    "Tests run and their results",
    "Residual risks and recommended follow-up",
    "Confirmation: no trades, no broker, no scoring/decision-logic changes",
)

_SKILLS: tuple[Skill, ...] = (
    Skill(
        skill_id="diagnose_daily_run_failure",
        name="Diagnose daily run failure",
        description="Read the daily run status and stage logs; explain why "
        "stages failed/warned. Read-only investigation.",
        allowed_probe_ids=("daily_run.failed_stages", "pipeline.run_status"),
        allowed_modes=("diagnose",),
        forbidden_actions=("Re-run the production pipeline",),
        required_tests=("python -m pytest -q tests/test_daily_run_status.py",),
        risk_level="low",
        approval_required_for_modes=(),
        output_report_requirements=_REPORT_BASE,
    ),
    Skill(
        skill_id="diagnose_data_quality_warnings",
        name="Diagnose data-quality warnings",
        description="Read the data-quality report; explain which symbols are "
        "degraded and the likely upstream cause. Read-only.",
        allowed_probe_ids=("data_quality.warnings",),
        allowed_modes=("diagnose",),
        forbidden_actions=(),
        required_tests=("python -m pytest -q tests/test_data_quality_monitor.py",),
        risk_level="low",
        approval_required_for_modes=(),
        output_report_requirements=_REPORT_BASE,
    ),
    Skill(
        skill_id="propose_data_quality_fix",
        name="Propose data-quality fix",
        description="Propose a focused, reversible fix for data-quality "
        "warnings (e.g. a coverage/universe adjustment). propose_fix writes a "
        "proposal only; safe_repair applies a narrow non-protected change and "
        "requires approval.",
        allowed_probe_ids=("data_quality.warnings",),
        allowed_modes=("propose_fix", "safe_repair"),
        forbidden_actions=("Modify protected scoring or signal-registry weights",),
        required_tests=("python -m pytest -q tests/test_data_quality_monitor.py",),
        risk_level="medium",
        approval_required_for_modes=("safe_repair",),
        output_report_requirements=_REPORT_BASE,
    ),
    Skill(
        skill_id="diagnose_pipeline_status",
        name="Diagnose pipeline / budget status",
        description="Read pipeline run status and budget artifacts; explain "
        "step failures, skips, or budget pressure. Read-only.",
        allowed_probe_ids=(
            "pipeline.run_status",
            "ai_budget.status",
            "fmp_budget.status",
            "memo.delivery_status",
        ),
        allowed_modes=("diagnose",),
        forbidden_actions=("Change budget caps without explicit operator approval",),
        required_tests=("python -m pytest -q tests/test_pipeline_run_status.py",),
        risk_level="low",
        approval_required_for_modes=(),
        output_report_requirements=_REPORT_BASE,
    ),
    Skill(
        skill_id="diagnose_quant_calibration",
        name="Diagnose quant calibration",
        description="Read confidence-calibration / pattern-efficacy artifacts "
        "and explain the quant signal. Proposal-only context; NOT advice. "
        "Read-only.",
        allowed_probe_ids=("quant.confidence_calibration", "quant.pattern_efficacy"),
        allowed_modes=("diagnose",),
        forbidden_actions=(
            "Apply any weight/gate change",
            "Present proposal-only evidence as official advice",
        ),
        required_tests=("python -m pytest -q tests/test_confidence_calibration.py",),
        risk_level="low",
        approval_required_for_modes=(),
        output_report_requirements=_REPORT_BASE,
    ),
    Skill(
        skill_id="propose_quant_retune_review",
        name="Propose quant retune review",
        description="Summarize retune/weight suggestions into a review work "
        "order for the operator. propose_fix writes a review document only — it "
        "NEVER applies a retune (auto-apply is a separate, gated, inert path).",
        allowed_probe_ids=("quant.retune_suggestions", "quant.pattern_efficacy"),
        allowed_modes=("diagnose", "propose_fix"),
        forbidden_actions=(
            "Apply or stage any weight/gate retune",
            "Touch signal_registry.yaml default_weight values",
            "Present proposal-only evidence as official advice",
        ),
        required_tests=("python -m pytest -q tests/test_retune_suggestions.py",),
        risk_level="medium",
        approval_required_for_modes=("propose_fix",),
        output_report_requirements=_REPORT_BASE,
    ),
    Skill(
        skill_id="diagnose_regime_classifier",
        name="Diagnose / repair regime classifier",
        description="Diagnose why the market-regime classifier output is "
        "degenerate (e.g. signal_outcomes regime_label collapsed to a constant) "
        "and, in safe_repair mode, apply a narrow, reversible repair of the "
        "regime PRODUCER WIRING in the simulation/test lane only. This skill is "
        "for label-DIVERSITY/degeneracy defects (ordering, data flow, "
        "serialization), NOT threshold tuning. Threshold edits to manufacture "
        "diversity are forbidden.",
        allowed_probe_ids=("quant.regime_classifier_health",),
        allowed_modes=("diagnose", "propose_fix", "safe_repair"),
        forbidden_actions=(
            "Tune regime thresholds merely to manufacture label diversity",
            "Change regime label assignment unsupported by classifier intent, "
            "input scale, tests, and observed distributions",
            "Rewrite protected historical signal_outcomes evidence or the "
            "production signal-feedback DB",
            "Mutate production allocations or production decision artifacts",
        ),
        required_tests=(
            "python -m pytest -q tests/test_market_regime.py "
            "tests/test_regime_classifier_degeneracy.py",
        ),
        risk_level="medium",
        approval_required_for_modes=("propose_fix", "safe_repair"),
        output_report_requirements=_REPORT_BASE,
    ),
    Skill(
        skill_id="diagnose_portfolio_risk",
        name="Diagnose portfolio risk / advisory queue",
        description="Explain a near-cap risk metric or the advisory decision "
        "queue from existing artifacts. Advisory/read-only — decision_plan.json "
        "is the sole source of advisory actions.",
        allowed_probe_ids=(
            "portfolio.risk_near_cap",
            "portfolio.advisory_decision_queue",
        ),
        allowed_modes=("diagnose",),
        forbidden_actions=(
            "Recommend or imply trade execution",
            "Recompute decisions outside the core decision layers",
        ),
        required_tests=("python -m pytest -q tests/test_risk_delta_advisor.py",),
        risk_level="low",
        approval_required_for_modes=(),
        output_report_requirements=_REPORT_BASE,
    ),
    Skill(
        skill_id="regenerate_memo_from_artifacts",
        name="Regenerate memo from artifacts",
        description="Diagnose memo formatting/readability, or regenerate the "
        "operator memo from EXISTING artifacts (no new decisions). safe_repair "
        "rewrites the memo markdown only and requires approval. Never sends "
        "email.",
        allowed_probe_ids=("memo.generation_readability",),
        allowed_modes=("diagnose", "safe_repair"),
        forbidden_actions=(
            "Send email or trigger memo delivery",
            "Invent decisions not present in decision_plan.json",
        ),
        required_tests=("python -m pytest -q tests/test_daily_memo.py",),
        risk_level="medium",
        approval_required_for_modes=("safe_repair",),
        output_report_requirements=_REPORT_BASE,
    ),
    Skill(
        skill_id="diagnose_schwab_read_only_health",
        name="Diagnose Schwab read-only health",
        description="Read the Schwab broker_sync_status artifact and explain "
        "connection/auth health. Read-only; Schwab is read-only with no trade "
        "capability.",
        allowed_probe_ids=("schwab.broker_health",),
        allowed_modes=("diagnose",),
        forbidden_actions=(
            "Place, simulate, or imply any broker order",
            "Modify broker credentials or connection config",
        ),
        required_tests=("python -m pytest -q tests/test_schwab_sync.py",),
        risk_level="low",
        approval_required_for_modes=(),
        output_report_requirements=_REPORT_BASE,
    ),
    Skill(
        skill_id="inspect_artifact_registry",
        name="Inspect artifact registry",
        description="Read the artifact-registry status; explain missing/stale/"
        "unattributed rows and what producer/consumer is implicated. Read-only.",
        allowed_probe_ids=("artifact_registry.status",),
        allowed_modes=("diagnose",),
        forbidden_actions=(),
        required_tests=("python -m pytest -q tests/test_artifact_registry.py",),
        risk_level="low",
        approval_required_for_modes=(),
        output_report_requirements=_REPORT_BASE,
    ),
)

SKILLS: dict[str, Skill] = {s.skill_id: s for s in _SKILLS}


# ---------------------------------------------------------------------------
# Lookup / validation helpers
# ---------------------------------------------------------------------------


def get_skill(skill_id: str) -> Skill | None:
    return SKILLS.get(skill_id)


def skill_ids() -> list[str]:
    return list(SKILLS.keys())


def list_skills() -> list[Skill]:
    return list(SKILLS.values())


def is_probe_allowed(skill_id: str, probe_id: str) -> bool:
    s = SKILLS.get(skill_id)
    return bool(s and probe_id in s.allowed_probe_ids)


def is_mode_allowed(skill_id: str, mode: str) -> bool:
    s = SKILLS.get(skill_id)
    return bool(s and mode in s.allowed_modes)


def validate_registry() -> list[str]:
    """Return structural + cross-registry problems (empty = valid)."""
    errors: list[str] = []
    seen: set[str] = set()
    for s in SKILLS.values():
        if s.skill_id in seen:
            errors.append(f"duplicate skill_id: {s.skill_id}")
        seen.add(s.skill_id)
        if s.risk_level not in RISK_LEVELS:
            errors.append(f"{s.skill_id}: bad risk_level {s.risk_level!r}")
        if not s.allowed_modes:
            errors.append(f"{s.skill_id}: empty allowed_modes")
        for m in s.allowed_modes:
            if m not in ACTIONS:
                errors.append(f"{s.skill_id}: bad mode {m!r}")
        for m in s.approval_required_for_modes:
            if m not in s.allowed_modes:
                errors.append(
                    f"{s.skill_id}: approval mode {m!r} not in allowed_modes"
                )
        for pid in s.allowed_probe_ids:
            if pid not in PROBES:
                errors.append(f"{s.skill_id}: unknown allowed_probe_id {pid!r}")
        if not s.output_report_requirements:
            errors.append(f"{s.skill_id}: empty output_report_requirements")

    # Cross-registry: a probe's recommended skill must accept that probe (the
    # default button must resolve). A probe may offer MORE actions than its
    # default skill serves (e.g. diagnose via one skill, propose_fix via
    # another) — so every offered action must be served by SOME allowlisting
    # skill, not necessarily the recommended one.
    for p in PROBES.values():
        s = SKILLS.get(p.recommended_skill_id)
        if s is None:
            errors.append(
                f"probe {p.probe_id}: recommended_skill_id "
                f"{p.recommended_skill_id!r} not in skill registry"
            )
            continue
        if p.probe_id not in s.allowed_probe_ids:
            errors.append(
                f"probe {p.probe_id}: recommended skill "
                f"{p.recommended_skill_id!r} does not allow this probe"
            )
        # Each action the probe offers must be served by at least one skill that
        # allowlists the probe and permits that mode.
        for a in p.allowed_actions:
            if skill_for_probe_action(p.probe_id, a) is None:
                errors.append(
                    f"probe {p.probe_id}: action {a!r} is offered but no "
                    f"allowlisting skill permits that mode"
                )
    return errors


def skills_for_probe(probe_id: str) -> list[Skill]:
    """Every skill that allowlists ``probe_id``."""
    return [s for s in SKILLS.values() if probe_id in s.allowed_probe_ids]


def skill_for_probe_action(probe_id: str, action: str) -> Skill | None:
    """Resolve the skill that serves ``action`` for ``probe_id``.

    Prefers the probe's recommended skill when it permits the action; otherwise
    returns the first allowlisting skill that permits the action. Returns None
    when no skill serves it.
    """
    probe = PROBES.get(probe_id)
    if probe is not None:
        rec = SKILLS.get(probe.recommended_skill_id)
        if rec is not None and probe_id in rec.allowed_probe_ids and action in rec.allowed_modes:
            return rec
    for s in skills_for_probe(probe_id):
        if action in s.allowed_modes:
            return s
    return None


__all__ = [
    "Skill",
    "SKILLS",
    "GLOBAL_FORBIDDEN_ACTIONS",
    "get_skill",
    "skill_ids",
    "list_skills",
    "is_probe_allowed",
    "is_mode_allowed",
    "skills_for_probe",
    "skill_for_probe_action",
    "validate_registry",
]
