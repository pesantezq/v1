"""Session 3.1D — immutable hypothesis preregistration.

Research-only. This module freezes *scientific claims*, not trades. It contains
no P&L, execution, cost, sizing, optimization, or winner-selection path.

Authority chain:
    Session 3.0 durable graduation
      -> immutable hypothesis/strategy definitions
      -> immutable preregistration set
      -> explicit pointer (selection only)
      -> session3_1_status() re-verifies everything

Rendered Session 3 reports are never authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from portfolio_automation.intraday_lab import features as FT
from portfolio_automation.intraday_lab import irregular_sessions as IR
from portfolio_automation.intraday_lab import population_audit as PA
from portfolio_automation.intraday_lab import storage as ST

SCHEMA_VERSION = "1"
STRATEGY_IDENTITY_SCHEMA = "intraday_strategy_definition_v1"
HYPOTHESIS_IDENTITY_SCHEMA = "intraday_hypothesis_registration_v1"
PREREGISTRATION_SET_SCHEMA = "intraday_session3_preregistration_set_v1"
PREREGISTRATIONS = "session3/preregistration/content"
PREREGISTRATION_POINTER = "session3/preregistration/pointer.json"

HYPOTHESIS_PREREGISTRATION_READY = "HYPOTHESIS_PREREGISTRATION_READY"
HYPOTHESIS_PREREGISTRATION_LIMITED = "HYPOTHESIS_PREREGISTRATION_LIMITED"
SESSION_3_2_GO = "SESSION_3_2_GO"
SESSION_3_2_NO_GO = "SESSION_3_2_NO_GO"

DRAFT_PRE_FOUNDATION = "DRAFT_PRE_FOUNDATION"
NON_AUTHORITATIVE = "NON_AUTHORITATIVE"

EARLY_TO_LATE_INTRADAY_MOMENTUM_V1 = "EARLY_TO_LATE_INTRADAY_MOMENTUM_V1"
SHORT_HORIZON_MEAN_REVERSION_V1 = "SHORT_HORIZON_MEAN_REVERSION_V1"
OPENING_RANGE_BREAKOUT_CONTINUATION_V1 = "OPENING_RANGE_BREAKOUT_CONTINUATION_V1"

MEAN_REVERSION_RULE_V1 = "MEAN_REVERSION_RULE_V1"
OPENING_RANGE_BREAKOUT_RULE_V1 = "OPENING_RANGE_BREAKOUT_RULE_V1"
EARLY_TO_LATE_MOMENTUM_RULE_V1 = "EARLY_TO_LATE_MOMENTUM_RULE_V1"

CUSTOM_PRIMITIVE_VERSIONS = {
    "close_endpoint": "intraday_close_endpoint_v1",
    "session_open_to_early_close_return": "intraday_session_open_to_early_close_return_v1",
    "opening_range_construction": "intraday_opening_range_construction_v1",
    "close_breakout_comparison": "intraday_close_breakout_comparison_v1",
    "late_session_return": "intraday_late_session_return_v1",
}


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    value: int | float | str | bool
    unit: str
    semantic_meaning: str
    ex_ante_rationale: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "semantic_meaning": self.semantic_meaning,
            "ex_ante_rationale": self.ex_ante_rationale,
        }


@dataclass(frozen=True)
class PrimitiveRequirement:
    primitive_id: str
    semantic_version: str
    source: str
    halt_boundary_policy_key: str | None = None

    def to_dict(self) -> dict:
        return {
            "primitive_id": self.primitive_id,
            "semantic_version": self.semantic_version,
            "source": self.source,
            "halt_boundary_policy_key": self.halt_boundary_policy_key,
        }


@dataclass(frozen=True)
class WindowSpec:
    kind: str
    bars: int
    unit: str
    anchor: str
    description: str

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "bars": self.bars,
            "unit": self.unit,
            "anchor": self.anchor,
            "description": self.description,
        }


@dataclass(frozen=True)
class FoundationBinding:
    population_policy_id: str
    population_policy_fingerprint: str
    population_metric_definitions_version: str
    halt_boundary_policy_version: str
    halt_boundary_policy_fingerprint: str
    feature_set_version: str
    primitive_feature_versions: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict:
        return {
            "population_policy_id": self.population_policy_id,
            "population_policy_fingerprint": self.population_policy_fingerprint,
            "population_metric_definitions_version": self.population_metric_definitions_version,
            "halt_boundary_policy_version": self.halt_boundary_policy_version,
            "halt_boundary_policy_fingerprint": self.halt_boundary_policy_fingerprint,
            "feature_set_version": self.feature_set_version,
            "primitive_feature_versions": [
                {"id": k, "version": v} for k, v in self.primitive_feature_versions
            ],
        }


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    semantic_rule_version: str
    hypothesis_claim: str
    formula: str
    timeframe: str
    parameters: tuple[ParameterSpec, ...]
    observation_window: WindowSpec
    prediction_known_time: str
    evaluation_window: WindowSpec
    required_primitives: tuple[PrimitiveRequirement, ...]
    invalidation_conditions: tuple[str, ...]
    foundation: FoundationBinding
    optimization_performed: bool = False

    @property
    def parameter_set_fingerprint(self) -> str:
        return ST.content_hash({
            "schema": "intraday_parameter_set_v1",
            "strategy_id": self.strategy_id,
            "parameters": [p.to_dict() for p in self.parameters],
            "optimization_performed": self.optimization_performed,
        })

    def identity_payload(self) -> dict:
        return {
            "identity_schema": STRATEGY_IDENTITY_SCHEMA,
            "strategy_id": self.strategy_id,
            "semantic_rule_version": self.semantic_rule_version,
            "hypothesis_claim": self.hypothesis_claim,
            "formula": self.formula,
            "timeframe": self.timeframe,
            "parameters": [p.to_dict() for p in self.parameters],
            "parameter_set_fingerprint": self.parameter_set_fingerprint,
            "observation_window": self.observation_window.to_dict(),
            "prediction_known_time": self.prediction_known_time,
            "evaluation_window": self.evaluation_window.to_dict(),
            "required_primitives": [p.to_dict() for p in self.required_primitives],
            "invalidation_conditions": list(self.invalidation_conditions),
            "foundation": self.foundation.to_dict(),
            "optimization_performed": self.optimization_performed,
        }

    @property
    def fingerprint(self) -> str:
        return ST.content_hash(self.identity_payload())

    def to_dict(self) -> dict:
        return {**self.identity_payload(), "strategy_fingerprint": self.fingerprint}


@dataclass(frozen=True)
class HypothesisRegistration:
    hypothesis_id: str
    claim: str
    strategy_fingerprint: str
    parameter_set_fingerprint: str
    observation_window: WindowSpec
    prediction_known_time: str
    future_evaluation_window: WindowSpec
    primary_outcome: str
    invalidation_conditions: tuple[str, ...]
    foundation: FoundationBinding
    optimization_performed: bool = False
    amendment_of: str | None = None
    supersedes: tuple[str, ...] = ()

    def identity_payload(self) -> dict:
        return {
            "identity_schema": HYPOTHESIS_IDENTITY_SCHEMA,
            "hypothesis_id": self.hypothesis_id,
            "claim": self.claim,
            "strategy_fingerprint": self.strategy_fingerprint,
            "parameter_set_fingerprint": self.parameter_set_fingerprint,
            "observation_window": self.observation_window.to_dict(),
            "prediction_known_time": self.prediction_known_time,
            "future_evaluation_window": self.future_evaluation_window.to_dict(),
            "primary_outcome": self.primary_outcome,
            "invalidation_conditions": list(self.invalidation_conditions),
            "foundation": self.foundation.to_dict(),
            "optimization_performed": self.optimization_performed,
            "amendment_of": self.amendment_of,
            "supersedes": list(self.supersedes),
        }

    @property
    def fingerprint(self) -> str:
        return ST.content_hash(self.identity_payload())

    def to_dict(self) -> dict:
        return {**self.identity_payload(), "registration_fingerprint": self.fingerprint}


def foundation_binding(required_feature_ids: Iterable[str] = ()) -> FoundationBinding:
    feature_versions = []
    for feature_id in sorted(set(required_feature_ids)):
        meta = FT.FEATURE_REGISTRY.get(feature_id)
        if not meta:
            raise ValueError(f"unknown Session 2 feature {feature_id!r}")
        feature_versions.append((feature_id, str(meta["version"])))
    return FoundationBinding(
        population_policy_id=IR.POLICY_ID,
        population_policy_fingerprint=IR.policy_fingerprint(),
        population_metric_definitions_version=IR.METRIC_DEFINITIONS_VERSION,
        halt_boundary_policy_version=IR.HALT_BOUNDARY_POLICY_VERSION,
        halt_boundary_policy_fingerprint=ST.content_hash(IR.halt_boundary_policy()),
        feature_set_version=FT.FEATURE_SET_VERSION,
        primitive_feature_versions=tuple(feature_versions),
    )


def _p(name, value, unit, meaning, rationale) -> ParameterSpec:
    return ParameterSpec(name, value, unit, meaning, rationale)


def generation1_strategies() -> tuple[StrategyDefinition, ...]:
    """The three preregistered Generation-1 definitions.

    Parameter choices are ex-ante declarations. They are not tuned here and this
    module has no result channel from which tuning could occur.
    """
    early = StrategyDefinition(
        strategy_id=EARLY_TO_LATE_INTRADAY_MOMENTUM_V1,
        semantic_rule_version=EARLY_TO_LATE_MOMENTUM_RULE_V1,
        hypothesis_claim=(
            "Direction from the certified session open through the first 30 minutes "
            "contains same-direction information about the final 30 minutes."
        ),
        formula=(
            "early_return = close(first_6th_5m_bar) / open(first_5m_bar) - 1; "
            "predict LONG when early_return > 0, SHORT when early_return < 0, "
            "NO_SIGNAL when early_return == 0. Future outcome is late_return over "
            "the final six certified 5-minute bars and is not measured in Session 3.1."
        ),
        timeframe="5min",
        parameters=(
            _p("observation_bars", 6, "5-minute bars", "first 30 minutes",
               "Six 5-minute bars are the conventional half-hour opening observation; not optimized."),
            _p("direction_threshold", 0.0, "return fraction",
               "strict sign threshold for the early return",
               "Zero adds no fitted magnitude threshold; a buffered rule must become a new version."),
            _p("evaluation_window_bars", 6, "5-minute bars", "final 30 minutes",
               "Mirrors the half-hour research horizon while anchoring to certified session close."),
        ),
        observation_window=WindowSpec(
            "OPENING_OBSERVATION", 6, "5-minute bars", "CERTIFIED_SESSION_OPEN",
            "First six certified bars; session-open price through close of bar six.",
        ),
        prediction_known_time=(
            "max(known_at) of the six opening bars; with the frozen 60-second "
            "publication floor, the prediction cannot be registered before 10:01 ET "
            "on a normal 09:30 open."
        ),
        evaluation_window=WindowSpec(
            "FUTURE_EVALUATION", 6, "5-minute bars", "CERTIFIED_SESSION_CLOSE",
            "Final six certified bars of the same session; works on regular and early-close sessions.",
        ),
        required_primitives=(
            PrimitiveRequirement("close_endpoint", CUSTOM_PRIMITIVE_VERSIONS["close_endpoint"],
                                 "SESSION3_PRIMITIVE", "close_endpoint"),
            PrimitiveRequirement("session_open_to_early_close_return",
                                 CUSTOM_PRIMITIVE_VERSIONS["session_open_to_early_close_return"],
                                 "SESSION3_PRIMITIVE", "intra_bar_open_to_close"),
            PrimitiveRequirement("late_session_return",
                                 CUSTOM_PRIMITIVE_VERSIONS["late_session_return"],
                                 "FUTURE_EVALUATION_ONLY", "close_to_close_return"),
        ),
        invalidation_conditions=(
            "authoritative halt intersects the required opening observation window -> FEATURE_UNAVAILABLE",
            "fewer than six opening bars are knowable -> NOT_ENOUGH_HISTORY",
            "session is outside the graduated Session 3 population -> INELIGIBLE_SESSION",
            "overnight or previous-close information is prohibited in V1 because dividend/corporate-action semantics are unresolved",
        ),
        foundation=foundation_binding(),
    )

    mean_reversion = StrategyDefinition(
        strategy_id=SHORT_HORIZON_MEAN_REVERSION_V1,
        semantic_rule_version=MEAN_REVERSION_RULE_V1,
        hypothesis_claim=(
            "A sufficiently large 15-minute displacement in a contiguous intraday "
            "segment partially reverses over the next 15 minutes."
        ),
        formula=(
            "displacement = close_t / close_(t-3) - 1; predict SHORT when "
            "displacement >= +0.005 and LONG when displacement <= -0.005; "
            "otherwise NO_SIGNAL. Future evaluation horizon is three 5-minute bars "
            "and is not measured in Session 3.1."
        ),
        timeframe="5min",
        parameters=(
            _p("lookback_bars", 3, "5-minute intervals", "15-minute displacement",
               "Three intervals is the shortest preregistered multi-bar displacement; not optimized."),
            _p("displacement_threshold", 0.005, "return fraction",
               "absolute displacement required for a prediction",
               "0.5% is a round ex-ante material-move threshold for liquid index/equity research, chosen before outcomes and never tuned in V1."),
            _p("evaluation_horizon_bars", 3, "5-minute bars", "15-minute future evaluation",
               "Matches the observation horizon symmetrically; not optimized."),
        ),
        observation_window=WindowSpec(
            "ROLLING_CONTIGUOUS", 4, "observed close endpoints", "LATEST_KNOWN_BAR",
            "Four close endpoints are required to measure three consecutive 5-minute intervals.",
        ),
        prediction_known_time="known_at of the latest close endpoint used in the displacement",
        evaluation_window=WindowSpec(
            "FUTURE_EVALUATION", 3, "5-minute bars", "AFTER_PREDICTION",
            "Next three eligible 5-minute bars; outcome measurement belongs to later validation sessions.",
        ),
        required_primitives=(
            PrimitiveRequirement("return_nbar", "feature_v" + str(FT.FEATURE_REGISTRY["return_nbar"]["version"]),
                                 "SESSION2_FEATURE", "close_to_close_return"),
            PrimitiveRequirement("n_bar_displacement", "intraday_n_bar_displacement_v1",
                                 "SESSION3_SEGMENTED_PRIMITIVE", "n_bar_displacement"),
        ),
        invalidation_conditions=(
            "rolling observation would cross a gap/halt boundary -> reset segment and require four new close endpoints",
            "fewer than four contiguous known close endpoints -> NOT_ENOUGH_HISTORY",
            "session is outside the graduated Session 3 population -> INELIGIBLE_SESSION",
        ),
        foundation=foundation_binding(("return_nbar",)),
    )

    opening_range = StrategyDefinition(
        strategy_id=OPENING_RANGE_BREAKOUT_CONTINUATION_V1,
        semantic_rule_version=OPENING_RANGE_BREAKOUT_RULE_V1,
        hypothesis_claim=(
            "After an uninterrupted first-30-minute range is established, a strict "
            "close outside that range contains same-direction continuation information."
        ),
        formula=(
            "range_high = max(high) and range_low = min(low) over first six 5-minute "
            "bars; after the range is complete, predict LONG when close > "
            "range_high*(1+0.0), SHORT when close < range_low*(1-0.0), otherwise "
            "NO_SIGNAL. Future six-bar continuation outcome is not measured in Session 3.1."
        ),
        timeframe="5min",
        parameters=(
            _p("opening_range_bars", 6, "5-minute bars", "first 30-minute opening range",
               "Six bars define the preregistered half-hour range; not optimized."),
            _p("break_threshold", 0.0, "fraction beyond range boundary",
               "strict breakout buffer",
               "Zero is deliberately the simplest falsifiable V1; any buffer is a new parameter set/version."),
            _p("evaluation_horizon_bars", 6, "5-minute bars", "30-minute future continuation window",
               "Half-hour future horizon is declared before outcomes and is not optimized."),
        ),
        observation_window=WindowSpec(
            "OPENING_RANGE", 6, "5-minute bars", "CERTIFIED_SESSION_OPEN",
            "High/low geometry of the first six certified bars, only if the window is uninterrupted.",
        ),
        prediction_known_time=(
            "known_at of the first post-range close endpoint being tested; the range "
            "itself is only available after all six opening bars are knowable."
        ),
        evaluation_window=WindowSpec(
            "FUTURE_EVALUATION", 6, "5-minute bars", "AFTER_FIRST_BREAK_PREDICTION",
            "Next six eligible bars after the registered breakout prediction; execution is not defined here.",
        ),
        required_primitives=(
            PrimitiveRequirement("opening_range_construction",
                                 CUSTOM_PRIMITIVE_VERSIONS["opening_range_construction"],
                                 "SESSION3_PRIMITIVE", "opening_range_construction"),
            PrimitiveRequirement("close_breakout_comparison",
                                 CUSTOM_PRIMITIVE_VERSIONS["close_breakout_comparison"],
                                 "SESSION3_PRIMITIVE", "close_endpoint"),
        ),
        invalidation_conditions=(
            "authoritative halt intersects any required opening-range interval -> FEATURE_UNAVAILABLE",
            "any required opening-range bar is partially halt-overlapped -> FEATURE_UNAVAILABLE",
            "fewer than six opening bars are knowable -> NOT_ENOUGH_HISTORY",
            "session is outside the graduated Session 3 population -> INELIGIBLE_SESSION",
        ),
        foundation=foundation_binding(),
    )
    return (mean_reversion, opening_range, early)


def generation1_strategy_by_id(strategy_id: str) -> StrategyDefinition:
    for strategy in generation1_strategies():
        if strategy.strategy_id == strategy_id:
            return strategy
    raise KeyError(strategy_id)


def generation1_hypotheses() -> tuple[HypothesisRegistration, ...]:
    out = []
    for strategy in generation1_strategies():
        if strategy.strategy_id == SHORT_HORIZON_MEAN_REVERSION_V1:
            outcome = "future 3-bar return has sign opposite the registered displacement prediction"
        elif strategy.strategy_id == OPENING_RANGE_BREAKOUT_CONTINUATION_V1:
            outcome = "future 6-bar return has the same sign as the registered breakout prediction"
        else:
            outcome = "return over the final six certified bars has the same sign as the registered early-session prediction"
        out.append(HypothesisRegistration(
            hypothesis_id=strategy.strategy_id,
            claim=strategy.hypothesis_claim,
            strategy_fingerprint=strategy.fingerprint,
            parameter_set_fingerprint=strategy.parameter_set_fingerprint,
            observation_window=strategy.observation_window,
            prediction_known_time=strategy.prediction_known_time,
            future_evaluation_window=strategy.evaluation_window,
            primary_outcome=outcome,
            invalidation_conditions=strategy.invalidation_conditions,
            foundation=strategy.foundation,
            optimization_performed=False,
        ))
    return tuple(out)


def research_burden() -> dict:
    return {
        "schema": "intraday_research_burden_v1",
        "strategy_families": 3,
        "registered_hypotheses": 3,
        "parameter_sets": 3,
        "directional_subhypotheses": 6,
        "optimization_trials": 0,
        "post_result_amendments": 0,
        "optimization_performed": False,
    }


def _legacy_candidates(root: str = ".") -> list[Path]:
    base = ST.intraday_root(root) / "session3"
    if not base.exists():
        return []
    out = []
    for path in sorted(base.glob("*.json")):
        if path.name == "irregular_session_population.json":
            continue
        if path.name == "signal_contract.json" or path.name == "strategy_registry.json" or path.name.startswith("strategy_"):
            out.append(path)
    return out


def _legacy_strategy_refs(value: Any) -> tuple[tuple[str, str | None], ...]:
    """Extract declared prototype strategy identities without trusting them."""
    found: set[tuple[str, str | None]] = set()
    aliases = {
        "OPENING_MOMENTUM_CONTINUATION_V1",
        "OPENING_RANGE_BEHAVIOR_V1",
        EARLY_TO_LATE_INTRADAY_MOMENTUM_V1,
        OPENING_RANGE_BREAKOUT_CONTINUATION_V1,
        SHORT_HORIZON_MEAN_REVERSION_V1,
    }

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            sid = obj.get("strategy_id")
            if sid in aliases:
                fp = obj.get("strategy_fingerprint") or obj.get("fingerprint")
                found.add((sid, str(fp) if fp else None))
            for child in obj.values():
                walk(child)
        elif isinstance(obj, list):
            for child in obj:
                walk(child)

    walk(value)
    return tuple(sorted(found))


def _legacy_artifact_fingerprint(path: Path) -> tuple[str, Any]:
    """The frozen fingerprint contract for one superseded prototype artifact.

    Parsed JSON is hashed by MEANING, so reformatting an old artifact is not
    corruption; anything unparseable falls back to its raw bytes. Discovery and
    verification MUST call this same function — two implementations would
    eventually disagree about what "unchanged" means, and the disagreement would
    surface as either a false corruption alarm or a silent tamper window.

    Returns (fingerprint, parsed_payload_or_None).
    """
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return hashlib.sha256(raw).hexdigest(), None
    return ST.content_hash(payload), payload


def _resolve_legacy_path(base: Path, relative: Any) -> Path | None:
    """Resolve a preregistered legacy path, refusing anything outside the tree.

    Immutable evidence names the artifact to re-read, so a tampered or malformed
    record must not be able to turn verification into an arbitrary file read.
    Scoped deliberately to this evidence contract, not a general path framework.
    """
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        return None
    root = base.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


def legacy_prototype_lineage(root: str = ".") -> list[dict]:
    """Describe old prototype artifacts without modifying or deleting them."""
    out = []
    base = ST.intraday_root(root)
    for path in _legacy_candidates(root):
        fp, payload = _legacy_artifact_fingerprint(path)
        refs: tuple[tuple[str, str | None], ...] = ()
        if payload is not None:
            refs = _legacy_strategy_refs(payload)
        out.append({
            "path": str(path.relative_to(base)),
            "content_fingerprint": fp,
            "status": DRAFT_PRE_FOUNDATION,
            "authority": NON_AUTHORITATIVE,
            "relation": "superseded_by_this_authoritative_preregistration_set",
            "declared_strategy_refs": [
                {"strategy_id": sid, "strategy_fingerprint": sfp} for sid, sfp in refs
            ],
            "preserved": True,
        })
    return out


def _registration_lineage(legacy: list[dict],
                          hypotheses: tuple[HypothesisRegistration, ...]) -> list[dict]:
    """Explicitly map pre-foundation strategy identities to final registrations."""
    alias = {
        "OPENING_MOMENTUM_CONTINUATION_V1": EARLY_TO_LATE_INTRADAY_MOMENTUM_V1,
        "OPENING_RANGE_BEHAVIOR_V1": OPENING_RANGE_BREAKOUT_CONTINUATION_V1,
        SHORT_HORIZON_MEAN_REVERSION_V1: SHORT_HORIZON_MEAN_REVERSION_V1,
        EARLY_TO_LATE_INTRADAY_MOMENTUM_V1: EARLY_TO_LATE_INTRADAY_MOMENTUM_V1,
        OPENING_RANGE_BREAKOUT_CONTINUATION_V1: OPENING_RANGE_BREAKOUT_CONTINUATION_V1,
    }
    current = {h.hypothesis_id: h for h in hypotheses}
    out = []
    seen = set()
    for artifact in legacy:
        for ref in artifact.get("declared_strategy_refs") or []:
            old_id = ref.get("strategy_id")
            new_id = alias.get(old_id)
            if not new_id or new_id not in current:
                continue
            key = (artifact["content_fingerprint"], old_id, ref.get("strategy_fingerprint"), new_id)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "relation": "supersedes",
                "legacy_status": DRAFT_PRE_FOUNDATION,
                "legacy_authority": NON_AUTHORITATIVE,
                "legacy_artifact_fingerprint": artifact["content_fingerprint"],
                "legacy_strategy_id": old_id,
                "legacy_strategy_fingerprint": ref.get("strategy_fingerprint"),
                "authoritative_hypothesis_id": new_id,
                "authoritative_registration_fingerprint": current[new_id].fingerprint,
                "reason": "prototype was minted before final Session 3 foundation/policy binding",
            })
    return sorted(out, key=lambda x: (
        x["authoritative_hypothesis_id"], x["legacy_artifact_fingerprint"],
        x["legacy_strategy_id"],
    ))


def preregistration_payload(*, root: str = ".") -> dict:
    """Build the immutable Session 3.1 preregistration set from durable authority."""
    s30 = PA.session3_0_status(root=root)
    if s30.get("status") != PA.SESSION_3_0_POLICY_READY or s30.get("session_3_1_gate") != PA.SESSION_3_1_GO:
        raise ValueError(f"Session 3.0 durable gate is not ready: {s30.get('blockers')}")
    if s30.get("strategy_validation_allowed") is not False:
        raise ValueError("Session 3.0 unexpectedly permits strategy validation")
    strategies = generation1_strategies()
    hypotheses = generation1_hypotheses()
    legacy = legacy_prototype_lineage(root)
    return {
        "identity_schema": PREREGISTRATION_SET_SCHEMA,
        "observe_only": True,
        "session": "3.1D",
        "source_population_fingerprint": s30.get("population_fingerprint"),
        "source_population_policy_fingerprint": IR.policy_fingerprint(),
        "halt_boundary_policy_version": IR.HALT_BOUNDARY_POLICY_VERSION,
        "halt_boundary_policy_fingerprint": ST.content_hash(IR.halt_boundary_policy()),
        "strategies": [s.to_dict() for s in strategies],
        "hypotheses": [h.to_dict() for h in hypotheses],
        "research_burden": research_burden(),
        "supersedes_legacy_artifacts": legacy,
        "registration_lineage": _registration_lineage(legacy, hypotheses),
        "strategy_validation_allowed": False,
        "out_of_scope": [
            "P&L", "Sharpe", "fills", "execution costs", "position sizing",
            "optimization", "winner selection",
        ],
    }


def preregistration_fingerprint(payload: dict) -> str:
    return ST.content_hash(payload)


def persist_preregistration_set(*, root: str = ".") -> str:
    payload = preregistration_payload(root=root)
    fp = preregistration_fingerprint(payload)
    ST.write_snapshot(PREREGISTRATIONS, fp, {
        "preregistration.json": payload,
        "manifest.json": {
            "schema_version": SCHEMA_VERSION,
            "identity_schema": PREREGISTRATION_SET_SCHEMA,
            "preregistration_fingerprint": fp,
            "hypothesis_fingerprints": [h["registration_fingerprint"] for h in payload["hypotheses"]],
            "strategy_fingerprints": [s["strategy_fingerprint"] for s in payload["strategies"]],
            "source_population_fingerprint": payload["source_population_fingerprint"],
        },
    }, root=root)
    return fp


def _current_strategy_payloads() -> list[dict]:
    return [s.to_dict() for s in generation1_strategies()]


def _current_hypothesis_payloads() -> list[dict]:
    return [h.to_dict() for h in generation1_hypotheses()]


def verify_preregistration_set(fingerprint: str, *, root: str = ".") -> dict:
    def fail(reason: str, **extra) -> dict:
        return {"verified": False, "reason": reason,
                "preregistration_fingerprint": fingerprint, **extra}

    body = ST.read_snapshot(PREREGISTRATIONS, fingerprint,
                            "preregistration.json", root=root)
    man = ST.read_snapshot(PREREGISTRATIONS, fingerprint,
                           "manifest.json", root=root)
    if body is None or man is None:
        return fail("missing preregistration content or manifest")
    if body.get("identity_schema") != PREREGISTRATION_SET_SCHEMA:
        return fail("wrong preregistration identity schema")
    if preregistration_fingerprint(body) != fingerprint:
        return fail("preregistration content does not hash to its identity")
    if man.get("preregistration_fingerprint") != fingerprint:
        return fail("manifest names a different preregistration fingerprint")

    s30 = PA.session3_0_status(root=root)
    if s30.get("status") != PA.SESSION_3_0_POLICY_READY or s30.get("session_3_1_gate") != PA.SESSION_3_1_GO:
        return fail("durable Session 3.0 gate is not ready", session3_0=s30)
    if body.get("source_population_fingerprint") != s30.get("population_fingerprint"):
        return fail("preregistration is bound to a different population evidence object")
    if body.get("source_population_policy_fingerprint") != IR.policy_fingerprint():
        return fail("population policy fingerprint changed")
    if body.get("halt_boundary_policy_version") != IR.HALT_BOUNDARY_POLICY_VERSION:
        return fail("halt-boundary policy version changed")
    if body.get("halt_boundary_policy_fingerprint") != ST.content_hash(IR.halt_boundary_policy()):
        return fail("halt-boundary policy meaning changed")
    if body.get("strategies") != _current_strategy_payloads():
        return fail("strategy definitions no longer match the preregistered bytes")
    if body.get("hypotheses") != _current_hypothesis_payloads():
        return fail("hypothesis registrations no longer match the preregistered bytes")
    if body.get("research_burden") != research_burden():
        return fail("research-burden record changed")
    if body.get("strategy_validation_allowed") is not False:
        return fail("preregistration unexpectedly enables strategy validation")
    if any(x.get("status") != DRAFT_PRE_FOUNDATION or x.get("authority") != NON_AUTHORITATIVE
           for x in body.get("supersedes_legacy_artifacts") or []):
        return fail("legacy prototype lineage is not explicitly non-authoritative")

    # Each superseded prototype must STILL be on disk and still mean what it meant
    # when it was preregistered. Recording that an artifact was preserved is not
    # evidence that it was: only re-reading the exact path the immutable evidence
    # names, and recomputing its fingerprint, can show that. Discovering whatever
    # happens to be on disk now would answer a different question.
    base = ST.intraday_root(root)
    legacy_verified = 0
    for record in body.get("supersedes_legacy_artifacts") or []:
        rel = record.get("path")
        resolved = _resolve_legacy_path(base, rel)
        if resolved is None:
            return fail(f"legacy artifact path escapes the Intraday root: {rel!r}")
        if not resolved.is_file():
            return fail(f"superseded legacy artifact is missing: {rel}")
        try:
            current_fp, _ = _legacy_artifact_fingerprint(resolved)
        except OSError as exc:
            return fail(f"superseded legacy artifact is unreadable: {rel} "
                        f"({type(exc).__name__})")
        if current_fp != record.get("content_fingerprint"):
            return fail(f"superseded legacy artifact changed since "
                        f"preregistration: {rel}")
        legacy_verified += 1

    current_regs = {h.fingerprint for h in generation1_hypotheses()}
    if any(x.get("relation") != "supersedes"
           or x.get("legacy_status") != DRAFT_PRE_FOUNDATION
           or x.get("legacy_authority") != NON_AUTHORITATIVE
           or x.get("authoritative_registration_fingerprint") not in current_regs
           for x in body.get("registration_lineage") or []):
        return fail("registration supersession lineage is invalid")

    return {
        "verified": True,
        "reason": None,
        "preregistration_fingerprint": fingerprint,
        "source_population_fingerprint": body["source_population_fingerprint"],
        "strategy_fingerprints": [s["strategy_fingerprint"] for s in body["strategies"]],
        "hypothesis_fingerprints": [h["registration_fingerprint"] for h in body["hypotheses"]],
        "legacy_artifacts_superseded": len(body.get("supersedes_legacy_artifacts") or []),
        "legacy_artifacts_reverified": legacy_verified,
    }


def set_session3_1_preregistration_evidence(fingerprint: str, *, root: str = ".") -> dict:
    verified = verify_preregistration_set(fingerprint, root=root)
    if not verified.get("verified"):
        raise ValueError(f"refusing preregistration pointer: {verified.get('reason')}")
    path = ST.intraday_root(root) / PREREGISTRATION_POINTER
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "preregistration_fingerprint": fingerprint,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "note": "Selection only. session3_1_status re-verifies immutable content and Session 3.0 authority.",
    }
    path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    return payload


def load_session3_1_preregistration_evidence(*, root: str = ".") -> dict:
    path = ST.intraday_root(root) / PREREGISTRATION_POINTER
    if not path.exists():
        return {"available": False, "verified": False,
                "reason": "no Session 3.1 preregistration pointer"}
    try:
        pointer = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": False, "verified": False,
                "reason": f"preregistration pointer unreadable: {type(exc).__name__}"}
    fp = pointer.get("preregistration_fingerprint")
    if not fp:
        return {"available": False, "verified": False,
                "reason": "preregistration pointer names no fingerprint"}
    result = verify_preregistration_set(fp, root=root)
    return {**result, "available": bool(result.get("verified")), "pointer": pointer}


def freeze_session3_1_preregistration(*, root: str = ".") -> dict:
    """Mint the authoritative Session 3.1D preregistration, or refuse outright.

    Composes the existing operations in their only safe order. It deliberately
    owns no authority of its own: the gate it reports is whatever
    ``session3_1_status`` derives from durable evidence, never a claim this
    function makes. A pointer is installed only after the persisted object has
    verified, so a failed certification leaves no selection behind to be mistaken
    for one that succeeded.
    """
    s30 = PA.session3_0_status(root=root)
    if (s30.get("status") != PA.SESSION_3_0_POLICY_READY
            or s30.get("session_3_1_gate") != PA.SESSION_3_1_GO):
        raise ValueError(f"refusing to preregister: Session 3.0 durable gate is "
                         f"not ready: {s30.get('blockers')}")

    fingerprint = persist_preregistration_set(root=root)
    verified = verify_preregistration_set(fingerprint, root=root)
    if not verified.get("verified"):
        raise ValueError(f"refusing to select preregistration {fingerprint}: "
                         f"{verified.get('reason')}")

    pointer = set_session3_1_preregistration_evidence(fingerprint, root=root)
    status = session3_1_status(root=root)
    return {
        "schema_version": SCHEMA_VERSION,
        "session": "3.1D",
        "preregistration_fingerprint": fingerprint,
        "verified": True,
        "pointer": pointer,
        "session_3_1_status": status,
        "status": status["status"],
        "session_3_2_gate": status["session_3_2_gate"],
        "source_population_fingerprint": verified.get("source_population_fingerprint"),
        "strategy_fingerprints": verified.get("strategy_fingerprints"),
        "hypothesis_fingerprints": verified.get("hypothesis_fingerprints"),
        "legacy_artifacts_superseded": verified.get("legacy_artifacts_superseded"),
        "legacy_artifacts_reverified": verified.get("legacy_artifacts_reverified"),
        "strategy_validation_allowed": False,
    }


def session3_1_status(*, root: str = ".") -> dict:
    """Gate Session 3.2 from durable evidence only.

    Deliberately does NOT read session3/irregular_session_population.json or any
    other rendered report.
    """
    s30 = PA.session3_0_status(root=root)
    durable_s30_ready = (
        s30.get("status") == PA.SESSION_3_0_POLICY_READY
        and s30.get("session_3_1_gate") == PA.SESSION_3_1_GO
    )
    evidence = load_session3_1_preregistration_evidence(root=root)
    checks = {
        "durable_session3_0_gate_ready": durable_s30_ready,
        "preregistration_evidence_available": bool(evidence.get("available")),
        "preregistration_evidence_verifies": bool(evidence.get("verified")),
        "three_strategy_definitions": len(generation1_strategies()) == 3,
        "three_hypothesis_registrations": len(generation1_hypotheses()) == 3,
        "research_burden_frozen": research_burden() == {
            "schema": "intraday_research_burden_v1",
            "strategy_families": 3, "registered_hypotheses": 3,
            "parameter_sets": 3, "directional_subhypotheses": 6,
            "optimization_trials": 0, "post_result_amendments": 0,
            "optimization_performed": False,
        },
        "optimization_not_performed": all(not s.optimization_performed for s in generation1_strategies()),
        "strategy_validation_stays_false": s30.get("strategy_validation_allowed") is False,
    }
    blockers = sorted(k for k, ok in checks.items() if not ok)
    return {
        "schema_version": SCHEMA_VERSION,
        "session": "3.1D",
        "status": HYPOTHESIS_PREREGISTRATION_READY if not blockers else HYPOTHESIS_PREREGISTRATION_LIMITED,
        "session_3_2_gate": SESSION_3_2_GO if not blockers else SESSION_3_2_NO_GO,
        "checks": checks,
        "blockers": blockers,
        "preregistration_fingerprint": evidence.get("preregistration_fingerprint"),
        "source_population_fingerprint": evidence.get("source_population_fingerprint"),
        "strategy_validation_allowed": False,
    }
