"""Controller-owned Learning Kernel configuration (Phase 16).

Lives at ``config/ew0a_learning.json`` — the SAME mechanism and directory as
``ew0a_authority.json`` / ``ew0a_runtime.json`` (no parallel config source), and a
protected path so the Worker cannot edit thresholds, trusted actors, or gates.

Fail-closed: a missing or malformed config yields the conservative built-in
defaults; it never yields permissive behavior and never yields an empty trusted
actor set (which would let any actor mutate learning state).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from portfolio_automation.engineer_worker.learning import SCHEMA_KIND
from portfolio_automation.engineer_worker.learning.contracts import (
    Capability, LearningAuthorityError)

LEARNING_CONFIG_SCHEMA_VERSION = "engineering.learning_config.v0"
DEFAULT_LEARNING_CONFIG_REL = "config/ew0a_learning.json"

# Actors permitted to mutate learning state when no config file is present.
# The Engineer worker identity is deliberately absent and can never be added by
# the worker (the config file is a protected path).
_DEFAULT_TRUSTED_ACTORS = ("claude_code", "trusted_controller", "ew0a-certification")

# Outcomes after which the extractor runs automatically.
_DEFAULT_AUTO_EXTRACT_AFTER = (
    "VERIFIED", "REPAIR", "ESCALATE", "POLICY_VIOLATION", "ABSTAIN", "HUMAN_DECISION")


@dataclass(frozen=True)
class GraduationThresholds:
    """Controller-owned graduation policy. NOT universal truth — an initial,
    deliberately conservative baseline that the controller may retune."""
    minimum_observations: int = 20
    minimum_consecutive_safe: int = 10
    minimum_success_rate: float = 0.90
    minimum_lesson_transfer_rate: float = 0.80
    max_authority_violations: int = 0
    max_false_certifications: int = 0
    max_missed_e4_escalations: int = 0
    max_security_escalation_failures: int = 0
    # Stricter multipliers applied to HIGH_RISK_CAPABILITIES.
    high_risk_observation_multiplier: float = 2.0
    high_risk_consecutive_safe_multiplier: float = 2.0
    high_risk_minimum_success_rate: float = 0.95

    def for_capability(self, capability: str) -> "GraduationThresholds":
        """Return the effective thresholds for one capability (stricter if high-risk)."""
        try:
            cap = Capability(capability)
        except ValueError:
            return self          # unknown capability -> base (never laxer)
        from portfolio_automation.engineer_worker.learning.contracts import HIGH_RISK_CAPABILITIES
        if cap not in HIGH_RISK_CAPABILITIES:
            return self
        return GraduationThresholds(
            minimum_observations=int(self.minimum_observations * self.high_risk_observation_multiplier),
            minimum_consecutive_safe=int(self.minimum_consecutive_safe
                                         * self.high_risk_consecutive_safe_multiplier),
            minimum_success_rate=max(self.minimum_success_rate, self.high_risk_minimum_success_rate),
            minimum_lesson_transfer_rate=self.minimum_lesson_transfer_rate,
            max_authority_violations=self.max_authority_violations,
            max_false_certifications=self.max_false_certifications,
            max_missed_e4_escalations=self.max_missed_e4_escalations,
            max_security_escalation_failures=self.max_security_escalation_failures,
            high_risk_observation_multiplier=self.high_risk_observation_multiplier,
            high_risk_consecutive_safe_multiplier=self.high_risk_consecutive_safe_multiplier,
            high_risk_minimum_success_rate=self.high_risk_minimum_success_rate)


@dataclass(frozen=True)
class RetrievalConfig:
    enabled: bool = True
    max_lessons: int = 5
    match_capability: bool = True
    match_task_class: bool = True
    match_subsystem: bool = True
    match_failure_class: bool = True
    match_risk_domain: bool = True
    recency_weight: float = 0.15


@dataclass(frozen=True)
class LearningConfig:
    enabled: bool = True
    auto_extract_after: tuple[str, ...] = _DEFAULT_AUTO_EXTRACT_AFTER
    require_evidence: bool = True
    semantic_lesson_validation: str = "independent_gpt"
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    thresholds: GraduationThresholds = field(default_factory=GraduationThresholds)
    # competence
    competence_update_after_outcome: bool = True
    track_unsafe_separately: bool = True
    recent_window_size: int = 20
    # graduation — automatic assessment only; NEVER automatic promotion
    auto_assess_readiness: bool = True
    automatic_certification: bool = False
    automatic_authority_change: bool = False
    trusted_actors: tuple[str, ...] = _DEFAULT_TRUSTED_ACTORS
    schema_version: str = LEARNING_CONFIG_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND

    def __post_init__(self) -> None:
        # Structural: this config can never authorize self-promotion.
        if self.automatic_certification or self.automatic_authority_change:
            raise LearningAuthorityError(
                "learning config may never enable automatic certification or authority change")
        if not self.trusted_actors:
            raise LearningAuthorityError("trusted_actors must be non-empty (fail closed)")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["auto_extract_after"] = list(self.auto_extract_after)
        d["trusted_actors"] = list(self.trusted_actors)
        return d


def read_learning_config(repo_root: str | Path,
                         rel: str = DEFAULT_LEARNING_CONFIG_REL) -> LearningConfig:
    """Read controller-owned learning config, fail-closed to conservative defaults.

    Any attempt (however it got into the file) to enable automatic certification or
    automatic authority change is REJECTED rather than honored."""
    p = Path(repo_root) / rel
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return LearningConfig()
    if not isinstance(d, dict):
        return LearningConfig()

    retr = d.get("retrieval") or {}
    thr = d.get("thresholds") or {}
    try:
        retrieval = RetrievalConfig(**{k: v for k, v in retr.items()
                                       if k in RetrievalConfig.__dataclass_fields__})
        thresholds = GraduationThresholds(**{k: v for k, v in thr.items()
                                             if k in GraduationThresholds.__dataclass_fields__})
        actors = tuple(d.get("trusted_actors") or _DEFAULT_TRUSTED_ACTORS)
        return LearningConfig(
            enabled=bool(d.get("enabled", True)),
            auto_extract_after=tuple(d.get("auto_extract_after") or _DEFAULT_AUTO_EXTRACT_AFTER),
            require_evidence=bool(d.get("require_evidence", True)),
            semantic_lesson_validation=str(d.get("semantic_lesson_validation", "independent_gpt")),
            retrieval=retrieval, thresholds=thresholds,
            competence_update_after_outcome=bool(d.get("competence_update_after_outcome", True)),
            track_unsafe_separately=bool(d.get("track_unsafe_separately", True)),
            recent_window_size=int(d.get("recent_window_size", 20)),
            auto_assess_readiness=bool(d.get("auto_assess_readiness", True)),
            automatic_certification=False,      # pinned False regardless of file content
            automatic_authority_change=False,   # pinned False regardless of file content
            trusted_actors=actors)
    except (TypeError, ValueError, LearningAuthorityError):
        return LearningConfig()


def write_learning_config(repo_root: str | Path, cfg: LearningConfig,
                          rel: str = DEFAULT_LEARNING_CONFIG_REL) -> None:
    """TRUSTED-side write. (The path is protected, so the Worker's repair scope
    cannot reach it — see policy._PROTECTED_PATTERNS.)"""
    p = Path(repo_root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def assert_controller_actor(cfg: LearningConfig, actor: str) -> None:
    """Hard authority boundary for every learning-state mutation.

    Technically enforced: the trusted actor list lives in a protected config file
    outside the Worker's repair scope, so the Worker can neither be on the list nor
    add itself to it."""
    if actor not in cfg.trusted_actors:
        raise LearningAuthorityError(
            f"actor {actor!r} may not mutate learning state (trusted controllers only)")
