"""Artifact contracts + record schemas for the next-stage workstreams (Phase 1).

This module is the **single source of truth** for the new artifacts introduced by
``docs/NEXT_STAGE_PORTFOLIO_INTELLIGENCE_SPEC.md`` (Section 18). It defines:

* :data:`NEW_ARTIFACTS` — one :class:`ArtifactContract` per new artifact (path,
  namespace, write mode, role/lens/cadence, intended producer, schema summary).
* :func:`observe_only_envelope` — the shared envelope every artifact embeds.
* Record dataclasses for the structured payloads (system-improvement idea,
  strategy profile, opportunity score, shadow record, learning event).
* :func:`degraded_payload` — a valid minimal artifact for the failure path.

Phase 1 deliberately ships **no producers and no pipeline wiring** — only these
contracts + their registry rows + tests. Producers in later phases import from
here so the on-disk shape can never drift from the contract.

Safety: every payload helper hard-codes ``observe_only=True``. Nothing here
trades, writes to a broker, places an order, or mutates holdings.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

# Imported lazily-safe: the namespace enum is the governance source of truth.
from portfolio_automation.data_governance import OutputNamespace

SCHEMA_VERSION = 1
OBSERVE_ONLY = True  # hard invariant for every next-stage artifact


# ---------------------------------------------------------------------------
# Shared envelope
# ---------------------------------------------------------------------------


def observe_only_envelope(generated_at: str, **extra: Any) -> dict[str, Any]:
    """Return the base envelope embedded by every next-stage artifact.

    ``generated_at`` is supplied by the producer (ISO-8601 string) — this module
    never calls ``datetime.now`` so it stays pure and deterministic for tests.
    ``observe_only`` is always ``True`` and cannot be overridden by ``extra``.
    """
    env: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "observe_only": OBSERVE_ONLY,
        "no_trade": True,
    }
    # extra may add producer fields but can never flip the safety flags.
    for k, v in extra.items():
        if k in ("observe_only", "no_trade"):
            continue
        env[k] = v
    env["observe_only"] = OBSERVE_ONLY
    env["no_trade"] = True
    return env


def lineage(
    *,
    run_id: str,
    data_as_of: str,
    producer: str,
    source_commit: str,
    config_hash: str,
    upstream_refs: list[str] | None = None,
    quality: str = "ok",
    freshness: str = "fresh",
) -> dict[str, Any]:
    """Canonical artifact-lineage fields (Phase 1).

    Splat into :func:`observe_only_envelope` so every critical artifact is
    traceable to its run + provenance::

        env = observe_only_envelope(now, **lineage(run_id=..., data_as_of=...,
              producer="decision_engine", source_commit=sha, config_hash=h))

    Pure: no I/O, no clock. Defaults keep the keys present (``upstream_refs``
    becomes ``[]``, not ``None``) so consumers can rely on the shape.
    """
    return {
        "run_id": run_id,
        "data_as_of": data_as_of,
        "producer": producer,
        "source_commit": source_commit,
        "config_hash": config_hash,
        "upstream_refs": list(upstream_refs or []),
        "quality": quality,
        "freshness": freshness,
    }


# ---------------------------------------------------------------------------
# Artifact contract descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactContract:
    """Declarative contract for one next-stage artifact.

    ``write_mode`` is ``"replace_latest"`` (overwrite each run) or ``"append"``
    (append-only JSONL event/decision log — never rewritten).
    ``required_fields`` are the top-level keys a healthy (non-degraded) payload
    must contain *in addition to* the envelope fields.
    """

    filename: str
    namespace: OutputNamespace
    write_mode: str          # "replace_latest" | "append"
    role: str                # registry role enum
    lens: str                # registry lens enum
    cadence: str             # registry cadence enum
    producer: str            # intended producer module (may not exist yet)
    phase: str               # implementation phase that ships the producer
    summary: str
    required_fields: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        """Repo-relative output path, e.g. ``outputs/sandbox/foo.json``."""
        return f"outputs/{self.namespace.value}/{self.filename}"

    @property
    def append_only(self) -> bool:
        return self.write_mode == "append"


_SANDBOX = OutputNamespace.SANDBOX
_POLICY = OutputNamespace.POLICY
_PORTFOLIO = OutputNamespace.PORTFOLIO
_LATEST = OutputNamespace.LATEST


def _c(filename, namespace, write_mode, role, lens, cadence, producer, phase,
       summary, required_fields=()):
    return ArtifactContract(
        filename=filename, namespace=namespace, write_mode=write_mode, role=role,
        lens=lens, cadence=cadence, producer=producer, phase=phase,
        summary=summary, required_fields=tuple(required_fields),
    )


# The full set of new artifacts (Section 18 of the spec). Keyed by filename.
NEW_ARTIFACTS: dict[str, ArtifactContract] = {
    # ── Research / sandbox lane (universe + opportunity) ──────────────────
    "universe_scan_candidates.json": _c(
        "universe_scan_candidates.json", _SANDBOX, "replace_latest", "advisor",
        "market_discovery", "daily", "universe_scanner", "5",
        "Scored candidates across watchlist/ETF/sector/commodity/theme/private universes.",
        ("candidates",)),
    "opportunity_radar.json": _c(
        "opportunity_radar.json", _SANDBOX, "replace_latest", "advisor",
        "market_discovery", "daily", "universe_scanner", "6",
        "Aggregated, scored opportunity radar across all candidate sources.",
        ("opportunities",)),
    "private_ipo_watchlist.json": _c(
        "private_ipo_watchlist.json", _SANDBOX, "replace_latest", "advisor",
        "market_discovery", "daily", "universe_scanner", "5",
        "Private/IPO watch items with access routes — never tradeable tickers.",
        ("items",)),
    "theme_candidates.json": _c(
        "theme_candidates.json", _SANDBOX, "replace_latest", "advisor",
        "market_discovery", "daily", "universe_scanner", "5",
        "Theme-basket candidates for the radar.", ("themes",)),
    "market_opportunity_prompts.json": _c(
        "market_opportunity_prompts.json", _SANDBOX, "replace_latest", "advisor",
        "market_discovery", "daily", "market_opportunity_prompts", "8",
        "Market-opportunity research prompt records (LLM or keyword fallback).",
        ("prompts",)),
    "market_opportunity_review_cards.json": _c(
        "market_opportunity_review_cards.json", _SANDBOX, "replace_latest", "advisor",
        "market_discovery", "daily", "market_opportunity_prompts", "8",
        "Operator-facing review cards for discovered opportunities.", ("cards",)),
    "opportunity_approval_queue.json": _c(
        "opportunity_approval_queue.json", _SANDBOX, "replace_latest", "advisor",
        "market_discovery", "on_demand", "approval_layer", "8",
        "Opportunity items awaiting operator review (artifact-based, executes nothing).",
        ("queue",)),
    # ── Sandbox shadow tracking + shadow portfolios ───────────────────────
    "shadow_opportunity_tracking.json": _c(
        "shadow_opportunity_tracking.json", _SANDBOX, "replace_latest", "advisor",
        "market_discovery", "daily", "shadow_tracker", "7",
        "Per-candidate shadow forward-performance tracking (proxies only).",
        ("records",)),
    "shadow_portfolios.json": _c(
        "shadow_portfolios.json", _SANDBOX, "replace_latest", "advisor",
        "market_discovery", "daily", "shadow_tracker", "7",
        "Simulated shadow portfolios (never real positions).", ("portfolios",)),
    "strategy_comparison.json": _c(
        "strategy_comparison.json", _SANDBOX, "replace_latest", "advisor",
        "market_discovery", "on_demand", "strategy_comparator", "7",
        "Strategy-vs-strategy comparison. Shared writer with shadow_tracker; "
        "carries produced_by ∈ {shadow_tracker, strategy_comparator} (§23.13).",
        ("produced_by", "comparison")),
    "candidate_promotion_review.json": _c(
        "candidate_promotion_review.json", _SANDBOX, "replace_latest", "advisor",
        "market_discovery", "on_demand", "shadow_tracker", "7",
        "Human-review surface for promoting sandbox candidates to watchlist review.",
        ("candidates",)),
    # ── Multi-strategy objective engine (§24) ─────────────────────────────
    "strategy_profiles.json": _c(
        "strategy_profiles.json", _SANDBOX, "replace_latest", "advisor",
        "market_discovery", "on_demand", "strategy_profiles", "11A",
        "The strategy profile definitions (objective, tilts, hard caps, horizon).",
        ("profiles",)),
    "strategy_shadow_results.json": _c(
        "strategy_shadow_results.json", _SANDBOX, "replace_latest", "advisor",
        "market_discovery", "on_demand", "strategy_comparator", "11A",
        "Sandbox/backtest results per strategy profile.", ("results",)),
    "strategy_risk_scorecard.json": _c(
        "strategy_risk_scorecard.json", _SANDBOX, "replace_latest", "advisor",
        "risk_action", "on_demand", "strategy_comparator", "11A",
        "Risk metrics per strategy (drawdown, concentration, leverage, vol).",
        ("scorecards",)),
    "strategy_tax_scorecard.json": _c(
        "strategy_tax_scorecard.json", _SANDBOX, "replace_latest", "advisor",
        "risk_action", "on_demand", "tax_scorecard", "11A",
        "Tax-aware scorecard; degrades (degraded:true + placeholders) without tax-lot data.",
        ("scorecards",)),
    # ── Broker-aware portfolio manager ────────────────────────────────────
    "broker_aware_portfolio.json": _c(
        "broker_aware_portfolio.json", _PORTFOLIO, "replace_latest", "advisor",
        "risk_action", "daily", "holdings_resolver", "10",
        "Actual-vs-config holdings, drift/concentration/leverage/cash, holdings_source. "
        "Read-only side-panel — never feeds decision_plan (§23.10).",
        ("holdings_source", "freshness")),
    # ── Operational improvement lane (system-improvement skill) ───────────
    "system_improvement_ideas.json": _c(
        "system_improvement_ideas.json", _LATEST, "replace_latest", "advisor",
        "developer", "daily", "system_improvement", "3",
        "Ranked daily system-improvement ideas (NOT market recommendations).",
        ("ideas",)),
    "system_improvement_brief.md": _c(
        "system_improvement_brief.md", _LATEST, "replace_latest", "narrative",
        "developer", "daily", "system_improvement", "3",
        "Operator-readable brief of the top system-improvement ideas."),
    "system_improvement_scorecard.json": _c(
        "system_improvement_scorecard.json", _LATEST, "replace_latest", "telemetry",
        "developer", "daily", "system_improvement", "3",
        "Counts/scores summary for the improvement ideas.", ("counts",)),
    "operator_action_queue.json": _c(
        "operator_action_queue.json", _LATEST, "replace_latest", "advisor",
        "meta_governance", "daily", "approval_layer", "4",
        "Open operator review items (market-opportunity + health), artifact-based.",
        ("queue",)),
    "system_improvement_action_queue.json": _c(
        "system_improvement_action_queue.json", _LATEST, "replace_latest", "advisor",
        "meta_governance", "daily", "approval_layer", "4",
        "Open system-improvement review items.", ("queue",)),
    "strategy_review_queue.json": _c(
        "strategy_review_queue.json", _LATEST, "replace_latest", "advisor",
        "meta_governance", "on_demand", "approval_layer", "11A",
        "Strategy profiles awaiting operator review/preference (executes nothing).",
        ("queue",)),
    # ── Learning-loop event spine + decision logs (append-only JSONL) ─────
    "pattern_events.jsonl": _c(
        "pattern_events.jsonl", _POLICY, "append", "telemetry",
        "quant_learning", "daily", "event_store", "11",
        "Append-only pattern activation/evaluation events."),
    "opportunity_events.jsonl": _c(
        "opportunity_events.jsonl", _POLICY, "append", "telemetry",
        "quant_learning", "daily", "event_store", "11",
        "Append-only opportunity discovery/dismissal/resolution events."),
    "outcome_events.jsonl": _c(
        "outcome_events.jsonl", _POLICY, "append", "telemetry",
        "quant_learning", "daily", "event_store", "11",
        "Append-only market-outcome-vs-decision/opportunity events."),
    "user_action_log.jsonl": _c(
        "user_action_log.jsonl", _POLICY, "append", "telemetry",
        "meta_governance", "daily", "approval_layer", "11",
        "Append-only operator interactions (approvals/rejections/edits)."),
    "user_decisions.jsonl": _c(
        "user_decisions.jsonl", _POLICY, "append", "telemetry",
        "meta_governance", "on_demand", "approval_layer", "4",
        "Append-only market-opportunity approval decisions."),
    "system_improvement_history.jsonl": _c(
        "system_improvement_history.jsonl", _POLICY, "append", "telemetry",
        "developer", "daily", "system_improvement", "3",
        "Append-only history of generated improvement ideas (dedup/cooldown source)."),
    "system_improvement_decisions.jsonl": _c(
        "system_improvement_decisions.jsonl", _POLICY, "append", "telemetry",
        "meta_governance", "on_demand", "approval_layer", "4",
        "Append-only operator decisions on improvement ideas."),
    "system_improvement_outcomes.jsonl": _c(
        "system_improvement_outcomes.jsonl", _POLICY, "append", "telemetry",
        "developer", "on_demand", "system_improvement", "15",
        "Append-only outcomes of implemented improvements."),
    "strategy_decisions.jsonl": _c(
        "strategy_decisions.jsonl", _POLICY, "append", "telemetry",
        "meta_governance", "on_demand", "approval_layer", "11A",
        "Append-only operator strategy decisions (approve/prefer/reject/defer)."),
    "strategy_outcomes.jsonl": _c(
        "strategy_outcomes.jsonl", _POLICY, "append", "telemetry",
        "quant_learning", "on_demand", "strategy_comparator", "11A",
        "Append-only realized-vs-expected strategy metrics over time."),
}


# ---------------------------------------------------------------------------
# Enums (status / category vocabularies)
# ---------------------------------------------------------------------------


class OpportunityStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    WATCHING = "WATCHING"
    SANDBOX_TRACKING = "SANDBOX_TRACKING"
    QUALIFIED = "QUALIFIED"
    APPROVED_WATCHLIST_REVIEW = "APPROVED_WATCHLIST_REVIEW"
    REJECTED = "REJECTED"
    HYPE_NOISE = "HYPE_NOISE"
    ACCESS_LIMITED = "ACCESS_LIMITED"
    PRIVATE_WATCH_ONLY = "PRIVATE_WATCH_ONLY"


class CandidateType(str, Enum):
    PUBLIC_TICKER = "public_ticker"
    ETF = "etf"
    COMMODITY_PROXY = "commodity_proxy"
    THEME_BASKET = "theme_basket"
    PRIVATE_IPO = "private_ipo"


class AccessRoute(str, Enum):
    IPO_WATCH = "ipo_watch"
    PUBLIC_SUPPLIER = "public_supplier"
    ETF = "etf"
    FUND = "fund"
    PROXY = "proxy"
    WATCH_ONLY = "watch_only"


class StrategyId(str, Enum):
    AGGRESSIVE_GROWTH = "aggressive_growth"
    SHORT_TERM_TACTICAL = "short_term_tactical"
    LONG_TERM_COMPOUNDING = "long_term_compounding"
    TAX_AWARE = "tax_aware"
    DEFENSIVE = "defensive_capital_preservation"
    INCOME_DIVIDEND = "income_dividend"
    BALANCED_CORE_SATELLITE = "balanced_core_satellite"
    BOOM_BUCKET = "boom_bucket"


class SystemImprovementCategory(str, Enum):
    RELIABILITY = "reliability"
    OBSERVABILITY = "observability"
    DASHBOARD_UX = "dashboard_ux"
    MOBILE_UX = "mobile_ux"
    DATA_QUALITY = "data_quality"
    ARTIFACT_CONTRACT = "artifact_contract"
    SCANNER_COVERAGE = "scanner_coverage"
    SANDBOX_QUALITY = "sandbox_quality"
    PATTERN_MEMORY = "pattern_memory"
    CONFIDENCE_CALIBRATION = "confidence_calibration"
    DOCUMENTATION = "documentation"
    TESTING = "testing"
    SECURITY_PRIVACY = "security_privacy"
    PERFORMANCE = "performance"
    COST_BUDGET = "cost_budget"
    ROADMAP_ALIGNMENT = "roadmap_alignment"
    DEVELOPER_EXPERIENCE = "developer_experience"


class SystemImprovementStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEFERRED = "deferred"
    DUPLICATE = "duplicate"
    COMPLETED = "completed"
    IN_QUEUE = "in_queue"


class EventStream(str, Enum):
    PATTERN = "pattern_events.jsonl"
    OPPORTUNITY = "opportunity_events.jsonl"
    OUTCOME = "outcome_events.jsonl"
    USER_ACTION = "user_action_log.jsonl"


# Hard risk caps (resolved decision §23.5: Higher tier).
BOOM_BUCKET_TOTAL_CAP = 0.15   # ≤15% total speculative exposure
BOOM_BUCKET_PER_IDEA_CAP = 0.05  # ≤5% per idea

# Actions the strategy/approval layer must NEVER expose (asserted by tests).
BLOCKED_STRATEGY_ACTIONS = (
    "place_trade", "submit_order", "move_money",
    "broker_write_action", "auto_rebalance", "modify_real_holdings",
)


# ---------------------------------------------------------------------------
# Record dataclasses (structured payloads)
# ---------------------------------------------------------------------------


@dataclass
class SystemImprovementIdea:
    """One daily system-improvement idea (§14). NOT a market recommendation."""
    id: str
    title: str
    category: str
    source: str
    created_at: str
    updated_at: str
    status: str = SystemImprovementStatus.PROPOSED.value
    priority: str = "medium"
    impact_score: float = 0.0
    urgency_score: float = 0.0
    effort_score: float = 0.0
    risk_score: float = 0.0
    confidence_score: float = 0.0
    roadmap_alignment_score: float = 0.0
    final_rank_score: float = 0.0
    summary: str = ""
    evidence: list[str] = field(default_factory=list)
    affected_modules: list[str] = field(default_factory=list)
    affected_artifacts: list[str] = field(default_factory=list)
    proposed_change: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    suggested_tests: list[str] = field(default_factory=list)
    safety_constraints: list[str] = field(default_factory=list)
    blocked_actions: list[str] = field(default_factory=list)
    implementation_prompt: str = ""
    owner_decision: str | None = None
    duplicate_of: str | None = None
    cooldown_until: str | None = None
    observe_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["observe_only"] = True
        return d


@dataclass
class StrategyProfile:
    """A strategy objective profile (§24). Advisory configuration only."""
    strategy_id: str
    name: str
    objective: str
    characteristics: list[str] = field(default_factory=list)
    # tilts/caps are advisory hints; never executed
    max_total_speculative: float = BOOM_BUCKET_TOTAL_CAP
    max_per_idea: float = BOOM_BUCKET_PER_IDEA_CAP
    drawdown_tolerance: str = "normal"
    horizon: str = "long_term"
    eligible_candidate_types: list[str] = field(default_factory=list)
    observe_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["observe_only"] = True
        return d


@dataclass
class OpportunityScore:
    """Opportunity scoring output (§8) — DISTINCT from protected portfolio scores."""
    candidate: str
    candidate_type: str
    access_route: str
    # dimensions (0..1)
    catalyst_strength: float = 0.0
    price_volume_confirmation: float = 0.0
    fundamental_support: float = 0.0
    market_regime_fit: float = 0.0
    portfolio_diversification_value: float = 0.0
    access_investability: float = 0.0
    risk_adjusted_timing: float = 0.0
    boom_potential: float = 0.0
    evidence_quality: float = 0.0
    liquidity_quality: float = 0.0
    data_quality: float = 0.0
    # penalties (0..1, subtractive)
    hype_penalty: float = 0.0
    crowded_trade_penalty: float = 0.0
    single_headline_penalty: float = 0.0
    portfolio_overlap_penalty: float = 0.0
    # outputs
    opportunity_score: float = 0.0
    boom_score: float = 0.0
    risk_score: float = 0.0
    investability_score: float = 0.0
    evidence_score: float = 0.0
    portfolio_fit_score: float = 0.0
    final_status: str = OpportunityStatus.DISCOVERED.value
    observe_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["observe_only"] = True
        return d


@dataclass
class ShadowRecord:
    """Per-candidate sandbox shadow-tracking record (§10). Proxies only."""
    candidate: str
    theme: str
    candidate_type: str
    discovered_date: str
    proxy_tickers: list[str] = field(default_factory=list)
    entry_reference_price: float | None = None  # public proxies only
    fwd_perf: dict[str, float] = field(default_factory=dict)  # {"1d","3d","7d","30d"}
    volatility: float | None = None
    drawdown: float | None = None
    news_followthrough: float | None = None
    catalyst_persistence: float | None = None
    diversification_value: float | None = None
    would_have_helped_portfolio: float | None = None
    observe_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["observe_only"] = True
        return d


@dataclass
class LearningEvent:
    """Envelope for the append-only learning-loop event streams (§11)."""
    event_id: str
    timestamp: str
    source: str
    run_mode: str
    namespace: str
    ticker_or_theme: str = ""
    signal_type: str = ""
    market_context: dict[str, Any] = field(default_factory=dict)
    portfolio_context: dict[str, Any] = field(default_factory=dict)
    confidence: float | None = None
    recommendation_or_action_or_status: str = ""
    user_decision: str | None = None
    outcome_windows: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    data_quality: str = ""
    observe_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["observe_only"] = True
        return d


# ---------------------------------------------------------------------------
# Degraded payloads (failure path)
# ---------------------------------------------------------------------------


def degraded_payload(filename: str, generated_at: str, reason: str) -> dict[str, Any]:
    """Return a valid minimal degraded payload for ``filename``.

    Used by producers' ``except`` branches so a failure still writes a
    schema-valid, observe-only artifact rather than crashing the pipeline.
    Append-only (.jsonl) streams have no degraded *file* payload — callers
    simply skip the append — so this raises for them to catch the misuse.
    """
    contract = NEW_ARTIFACTS.get(filename)
    if contract is None:
        raise KeyError(f"unknown next-stage artifact: {filename!r}")
    if contract.append_only:
        raise ValueError(
            f"{filename} is append-only; degrade by skipping the append, not by "
            "writing a degraded file")
    payload = observe_only_envelope(generated_at, degraded_mode=True,
                                    degraded_reason=reason)
    # Seed each declared required field with an empty container.
    for f in contract.required_fields:
        payload.setdefault(f, [] if f != "produced_by" else "")
    return payload
