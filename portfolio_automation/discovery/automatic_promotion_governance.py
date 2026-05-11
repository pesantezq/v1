"""
Automatic Promotion Governance (sandbox-only research graduation)
==================================================================

Replaces the previously planned ``manual_promotion_proposal`` step with an
automatic, deterministic, observe-only governance layer that evaluates
sandbox discovery candidates and graduates qualified candidates to a safer
MONITOR research state.

This layer:
  - reads sandbox discovery artifacts and decision-adjacent context as input;
  - applies deterministic eligibility gates;
  - emits sandbox-only artifacts that classify candidates into a safe
    research state — never into BUY/SELL/HOLD/ACTIONABLE/PROMOTED/VALIDATED
    or any official portfolio/watchlist/recommendation/decision/scoring/
    allocation mutation.

Safety invariants (hardcoded):
  - observe_only: true
  - no_trade: true
  - not_recommendation: true
  - discovery_only: true
  - no_portfolio_mutation: true
  - no_watchlist_mutation: true
  - no_decision_override: true
  - no_score_mutation: true
  - no_allocation_mutation: true
  - Allowed candidate states: DISCOVERED, WATCH, MONITOR, REJECTED, EXPIRED, NEEDS_REVIEW
  - Forbidden states (never emitted): BUY, SELL, HOLD, ACTIONABLE, PROMOTED,
    VALIDATED, APPROVED, TRADE, RECOMMENDATION
  - Writes only to OutputNamespace.SANDBOX
  - No LLM/AI calls — deterministic rules only
  - Run-mode governance: DISCOVERY and BACKTEST only may write artifacts;
    other modes return results as a dry-run

Public API:
  load_automatic_promotion_inputs(base_dir)
  evaluate_candidate_promotion(candidate, context, gates)
  build_automatic_promotion_report(inputs, run_mode, run_id, gates)
  render_automatic_promotion_markdown(report)
  write_automatic_promotion_report(report, base_dir, run_mode, run_id)
  run_automatic_promotion_governance(base_dir, run_mode, run_id, dry_run,
                                     write_files, gates)
  validate_automatic_promotion_safety(value)
  sanitize_automatic_promotion_text(value)
  sanitize_label(value)
  sanitize_nested_automatic_promotion_payload(payload)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    get_output_path,
    safe_write_json,
    safe_write_text,
)
from portfolio_automation.run_mode_governance import (
    RunMode,
    RunModeViolation,
    assert_can_write_namespace,
    normalize_run_mode,
    validate_output_write,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Safety constants
# ---------------------------------------------------------------------------

_OBSERVE_ONLY = True
_NO_TRADE = True
_NOT_RECOMMENDATION = True
_DISCOVERY_ONLY = True
_NO_PORTFOLIO_MUTATION = True
_NO_WATCHLIST_MUTATION = True
_NO_DECISION_OVERRIDE = True
_NO_SCORE_MUTATION = True
_NO_ALLOCATION_MUTATION = True
_SOURCE_LABEL = "automatic_promotion_governance"

_SAFETY_DISCLAIMER = (
    "This is sandbox research governance only, not a buy/sell/hold "
    "recommendation. Candidates are classified into research-monitor states "
    "for further review; this layer cannot alter official portfolio, "
    "watchlist, allocation, scoring, recommendation, or decision state."
)
_DISCOVERY_DISCLAIMER = (
    "Discovery research is sandbox-only. "
    "No candidates are promoted to official action."
)

# Fixed "Safety Boundary" documentation block.  This block intentionally
# names the forbidden action statuses so the operator can verify policy.
# It is whitelisted as an allowed disclaimer substring so the validator
# does not flag its embedded tokens.
_SAFETY_BOUNDARY_DOC = (
    "- Allowed statuses: `DISCOVERED`, `WATCH`, `MONITOR`, "
    "`REJECTED`, `EXPIRED`, `NEEDS_REVIEW`\n"
    "- Forbidden statuses (never emitted): `BUY`, `SELL`, `HOLD`, "
    "`ACTIONABLE`, `PROMOTED`, `VALIDATED`, `APPROVED`, `TRADE`, "
    "`RECOMMENDATION`\n"
    "- This layer never writes to LATEST, POLICY, or PORTFOLIO namespaces.\n"
    "- This layer never alters scoring, allocation, watchlist, "
    "recommendations, or decisions."
)

_DISCLAIMER_ALLOWED_SUBSTRINGS: tuple[str, ...] = (
    _SAFETY_DISCLAIMER,
    _DISCOVERY_DISCLAIMER,
    _SAFETY_BOUNDARY_DOC,
)

_REDACTION_MARKER = "[REDACTED]"

# Multi-word prohibited phrases (instruction-style).
_PROHIBITED_INSTRUCTION_PATTERNS: list[str] = [
    "buy now",
    "sell now",
    "hold now",
    "trim now",
    "trade now",
    "trim position",
    "rebalance now",
    "add shares",
    "buy shares",
    "sell shares",
    "reduce shares",
    "add to watchlist",
    "execute trade",
    "execute order",
    "execute now",
    "place trade",
    "place order",
    "promote candidate",
    "promote to watchlist",
    "actionable buy",
    "actionable sell",
    "validated buy",
    "validated sell",
    "official recommendation",
    "recommend buying",
    "recommend selling",
    "recommend holding",
    "i recommend",
    "you should buy",
    "you should sell",
    "you should hold",
    "consider buying",
    "consider selling",
]

# Standalone (whole-word) action tokens that must never appear as output values.
_FORBIDDEN_STANDALONE_ACTIONS: frozenset[str] = frozenset({
    "buy",
    "sell",
    "hold",
    "actionable",
    "promoted",
    "validated",
    "approved",
    "trade",
    "recommendation",
})

_NEUTRAL_REDACTED_ACTION_LABEL = "redacted_action_label_context_only"


# ---------------------------------------------------------------------------
# Status model
# ---------------------------------------------------------------------------

# Allowed states this layer may emit.
ALLOWED_STATUSES: frozenset[str] = frozenset({
    "DISCOVERED",
    "WATCH",
    "MONITOR",
    "REJECTED",
    "EXPIRED",
    "NEEDS_REVIEW",
})

# Statuses this layer must never emit (in any field).
FORBIDDEN_STATUSES: frozenset[str] = frozenset({
    "BUY", "SELL", "HOLD", "ACTIONABLE", "PROMOTED", "VALIDATED",
    "APPROVED", "TRADE", "RECOMMENDATION",
})


# ---------------------------------------------------------------------------
# Output filenames (sandbox-relative)
# ---------------------------------------------------------------------------

_CANDIDATES_PATH = "discovery/automatic_promotion_candidates.json"
_DECISIONS_LOG_PATH = "discovery/automatic_promotion_decisions.jsonl"
_SUMMARY_MD_PATH = "discovery/automatic_promotion_summary.md"


# ---------------------------------------------------------------------------
# Run-mode write permission
# ---------------------------------------------------------------------------

_SANDBOX_WRITE_MODES: frozenset[RunMode] = frozenset({
    RunMode.DISCOVERY,
    RunMode.BACKTEST,
})


# ---------------------------------------------------------------------------
# Governance gates (deterministic, conservative defaults)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PromotionGates:
    """Tunable thresholds for promotion eligibility.  Conservative defaults."""
    minimum_corrob_score: float = 0.65
    minimum_source_diversity: int = 2
    minimum_news_relevance: float = 0.4
    maximum_risk_flags: int = 2
    stale_after_days: int = 30
    minimum_persistence_runs: int = 2
    minimum_persistence_mentions: int = 3
    require_watch_status_for_monitor: bool = True
    require_persistence_for_monitor: bool = True
    block_rejected_candidates: bool = True
    block_forbidden_statuses: bool = True


DEFAULT_GATES = PromotionGates()


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class UnsafeAutomaticPromotionArtifactError(RuntimeError):
    """Raised when prohibited language remains after sanitization."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class AutomaticPromotionInputSummary:
    artifact: str
    available: bool
    summary: str = ""


@dataclass
class PromotionEligibilityResult:
    """Per-candidate evaluation outcome (gate-level detail)."""
    ticker: str
    eligible_for_monitor: bool = False
    proposed_status: str = "DISCOVERED"
    decision_type: str = "hold_status"   # promote_to_monitor | demote_to_review | reject | expire | hold_status
    gates_passed: list[str] = field(default_factory=list)
    gates_failed: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class PromotionDecision:
    """A single automatic decision record (also serialized to JSONL log)."""
    ticker: str
    prior_status: str
    proposed_status: str
    decision_type: str
    eligibility_result: str            # summary string of gates passed/failed
    evidence_score: float
    evidence_summary: str
    gates_passed: list[str]
    gates_failed: list[str]
    risk_flags: list[str]
    catalyst_flags: list[str]
    corroboration_score: float
    news_relevance_score: float
    source_diversity: int
    replay_context: str
    memory_context: str
    operator_context: str
    safety_flags: dict[str, bool]
    created_at: str
    reason: str


@dataclass
class AutomaticPromotionReport:
    generated_at: str
    run_mode: str
    run_id: str
    observe_only: bool = True
    no_trade: bool = True
    not_recommendation: bool = True
    discovery_only: bool = True
    no_portfolio_mutation: bool = True
    no_watchlist_mutation: bool = True
    no_decision_override: bool = True
    no_score_mutation: bool = True
    no_allocation_mutation: bool = True
    source: str = _SOURCE_LABEL

    data_available: bool = False
    inputs_used: list[AutomaticPromotionInputSummary] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)

    decisions: list[PromotionDecision] = field(default_factory=list)
    gate_summary: dict[str, int] = field(default_factory=dict)
    safety_disclaimer: str = _SAFETY_DISCLAIMER
    prohibited_actions_detected: list[str] = field(default_factory=list)
    gates: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Safe loaders
# ---------------------------------------------------------------------------

def _safe_load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
        if not raw:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("Failed to load %s: %s", path, exc)
        return None


def _safe_load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                pass
    except Exception as exc:
        logger.warning("Failed to load JSONL %s: %s", path, exc)
    return out


def _load_input(path: Path, label: str) -> tuple[Any, AutomaticPromotionInputSummary]:
    payload = _safe_load_json(path)
    if payload is None:
        return None, AutomaticPromotionInputSummary(artifact=label, available=False)
    if not isinstance(payload, dict):
        return None, AutomaticPromotionInputSummary(
            artifact=label, available=False, summary="non-object JSON"
        )
    return payload, AutomaticPromotionInputSummary(artifact=label, available=True)


def load_automatic_promotion_inputs(base_dir: str | Path = "outputs") -> dict[str, Any]:
    """
    Load all input artifacts safely.

    Returns dict keyed by artifact label, each entry has "payload" and
    "summary".  Missing/malformed/non-object inputs degrade silently.
    """
    base = Path(base_dir)

    def _latest(name: str) -> Path:
        return get_output_path(OutputNamespace.LATEST, name, base_dir=base)

    def _sandbox(name: str) -> Path:
        return get_output_path(OutputNamespace.SANDBOX, name, base_dir=base)

    paths: dict[str, Path] = {
        # Sandbox inputs
        "emerging_candidates":       _sandbox("discovery/emerging_candidates.json"),
        "rejected_candidates":       _sandbox("discovery/rejected_candidates.json"),
        "discovery_memory":          _sandbox("discovery/discovery_memory.json"),
        "news_enriched_candidates":  _sandbox("discovery/news_enriched_candidates.json"),
        "news_candidate_evidence":   _sandbox("discovery/news_candidate_evidence.json"),
        "replay_results":            _sandbox("discovery/replay_results.json"),
        # Latest inputs (read-only context)
        "news_evidence_layer":       _latest("news_evidence_layer.json"),
        "news_intelligence":         _latest("news_intelligence.json"),
        "market_narrative_daily":    _latest("market_narrative_daily.json"),
        "data_quality_report":       _latest("data_quality_report.json"),
    }

    loaded: dict[str, Any] = {}
    for label, path in paths.items():
        payload, summary = _load_input(path, label)
        loaded[label] = {"payload": payload, "summary": summary}

    # Approval decisions JSONL (optional, separate path)
    approval_path = _sandbox("discovery/approval_decisions.jsonl")
    approvals = _safe_load_jsonl(approval_path)
    loaded["approval_decisions"] = {
        "payload": approvals if approvals else None,
        "summary": AutomaticPromotionInputSummary(
            artifact="approval_decisions",
            available=bool(approvals),
        ),
    }
    return loaded


# ---------------------------------------------------------------------------
# Sanitizer / validator
# ---------------------------------------------------------------------------

def _detect_prohibited_phrases(text: str) -> list[str]:
    if not text:
        return []
    lower = text.lower()
    return [p for p in _PROHIBITED_INSTRUCTION_PATTERNS if p in lower]


def _detect_standalone_actions(text: str) -> list[str]:
    if not text:
        return []
    found: list[str] = []
    for token in _FORBIDDEN_STANDALONE_ACTIONS:
        if re.search(rf"\b{re.escape(token)}\b", text, flags=re.IGNORECASE):
            found.append(token)
    return found


def _strip_allowed_disclaimers(text: str) -> str:
    out = text
    for allowed in _DISCLAIMER_ALLOWED_SUBSTRINGS:
        if allowed:
            out = out.replace(allowed, "")
    return out


def validate_automatic_promotion_safety(value: Any) -> list[str]:
    """
    Walk any value (string/dict/list/dataclass) and return prohibited phrases
    or standalone action tokens detected.  Fixed safety disclaimers are
    excluded from violations.
    """
    violations: set[str] = set()

    def _walk(node: Any) -> None:
        if node is None or isinstance(node, (bool, int, float)):
            return
        if isinstance(node, str):
            stripped = _strip_allowed_disclaimers(node)
            for p in _detect_prohibited_phrases(stripped):
                violations.add(p)
            for a in _detect_standalone_actions(stripped):
                violations.add(a.upper())
            return
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(k)
                _walk(v)
            return
        if isinstance(node, (list, tuple, set)):
            for v in node:
                _walk(v)
            return
        if hasattr(node, "__dict__"):
            _walk(vars(node))
            return
        _walk(str(node))

    _walk(value)
    return sorted(violations)


def sanitize_automatic_promotion_text(value: str) -> str:
    """
    Redact prohibited substrings while preserving the fixed safety disclaimer
    wording exactly.
    """
    if not isinstance(value, str) or not value:
        return value if isinstance(value, str) else ""

    placeholders: list[tuple[str, str]] = []
    out = value
    for idx, allowed in enumerate(_DISCLAIMER_ALLOWED_SUBSTRINGS):
        token = f"\x00DISCLAIMER_{idx}\x00"
        if allowed and allowed in out:
            out = out.replace(allowed, token)
            placeholders.append((token, allowed))

    # Phrase-level redaction
    lower = out.lower()
    for pattern in _PROHIBITED_INSTRUCTION_PATTERNS:
        while pattern in lower:
            idx = lower.find(pattern)
            out = out[:idx] + _REDACTION_MARKER + out[idx + len(pattern):]
            lower = out.lower()

    # Whole-word standalone action redaction
    for token_text in _FORBIDDEN_STANDALONE_ACTIONS:
        out = re.sub(
            rf"\b{re.escape(token_text)}\b",
            _REDACTION_MARKER,
            out,
            flags=re.IGNORECASE,
        )

    for token, allowed in placeholders:
        out = out.replace(token, allowed)
    return out


def sanitize_label(value: Any) -> str:
    """Sanitize a label-style string; map pure-action labels to neutral marker."""
    if value is None:
        return ""
    sanitized = sanitize_automatic_promotion_text(str(value))
    if sanitized.strip() == _REDACTION_MARKER:
        return _NEUTRAL_REDACTED_ACTION_LABEL
    return sanitized


def sanitize_nested_automatic_promotion_payload(payload: Any) -> Any:
    """Recursively sanitize every string in a JSON-serializable payload."""
    if payload is None or isinstance(payload, (bool, int, float)):
        return payload
    if isinstance(payload, str):
        return sanitize_automatic_promotion_text(payload)
    if isinstance(payload, dict):
        return {k: sanitize_nested_automatic_promotion_payload(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [sanitize_nested_automatic_promotion_payload(v) for v in payload]
    if isinstance(payload, tuple):
        return tuple(sanitize_nested_automatic_promotion_payload(v) for v in payload)
    if isinstance(payload, set):
        return {sanitize_nested_automatic_promotion_payload(v) for v in payload}
    return payload


# ---------------------------------------------------------------------------
# Helpers — normalization and index building
# ---------------------------------------------------------------------------

def _normalize_ticker(value: Any) -> str:
    if not value:
        return ""
    return sanitize_label(str(value).upper().strip())


def _normalize_status(value: Any) -> str:
    """Normalize an upstream status string to ALLOWED_STATUSES or a safe fallback."""
    if value is None:
        return "DISCOVERED"
    raw = str(value).strip().upper()
    if raw in ALLOWED_STATUSES:
        return raw
    # Map common candidate-promotion-engine lowercase values
    lc = raw.lower()
    if lc == "discovered":
        return "DISCOVERED"
    if lc == "watch":
        return "WATCH"
    if lc == "rejected":
        return "REJECTED"
    if lc == "monitor":
        return "MONITOR"
    if lc == "expired":
        return "EXPIRED"
    if lc in ("needs_review", "review", "pending"):
        return "NEEDS_REVIEW"
    if raw in FORBIDDEN_STATUSES:
        # Forbidden upstream status — never propagate.  Caller may downgrade.
        return "DISCOVERED"
    return "DISCOVERED"


def _index_candidates_by_ticker(payload: dict | None) -> dict[str, dict]:
    if not isinstance(payload, dict):
        return {}
    cands = payload.get("candidates") or []
    if not isinstance(cands, list):
        return {}
    out: dict[str, dict] = {}
    for c in cands:
        if not isinstance(c, dict):
            continue
        t = _normalize_ticker(c.get("ticker") or c.get("symbol"))
        if t:
            out[t] = c
    return out


def _index_enriched(enriched: dict | None) -> dict[str, dict]:
    if not isinstance(enriched, dict):
        return {}
    cands = enriched.get("enriched_candidates") or []
    if not isinstance(cands, list):
        return {}
    out: dict[str, dict] = {}
    for c in cands:
        if not isinstance(c, dict):
            continue
        t = _normalize_ticker(c.get("ticker"))
        if t:
            out[t] = c
    return out


def _index_memory(memory: dict | None) -> dict[str, dict]:
    if not isinstance(memory, dict):
        return {}
    entries = memory.get("entries") or memory.get("memory_entries") or []
    if not isinstance(entries, list):
        return {}
    out: dict[str, dict] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        t = _normalize_ticker(e.get("ticker"))
        if t:
            out[t] = e
    return out


def _index_approvals(records: list[dict] | None) -> dict[str, list[dict]]:
    if not records:
        return {}
    out: dict[str, list[dict]] = {}
    for r in records:
        if not isinstance(r, dict):
            continue
        t = _normalize_ticker(r.get("ticker") or r.get("symbol"))
        if t:
            out.setdefault(t, []).append(r)
    return out


def _index_replay(replay: dict | None) -> dict[str, dict]:
    """Index per-ticker replay outcomes; tolerant of multiple shapes."""
    if not isinstance(replay, dict):
        return {}
    out: dict[str, dict] = {}
    for key in ("ticker_outcomes", "outcomes", "results"):
        items = replay.get(key)
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                t = _normalize_ticker(it.get("ticker") or it.get("symbol"))
                if t:
                    out[t] = it
            if out:
                break
        elif isinstance(items, dict):
            for k, v in items.items():
                t = _normalize_ticker(k)
                if t and isinstance(v, dict):
                    out[t] = v
            if out:
                break
    return out


def _parse_iso_date(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        text = str(s).strip()
        # Try common formats: full ISO, date only.
        if "T" in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Eligibility evaluator
# ---------------------------------------------------------------------------

def evaluate_candidate_promotion(
    candidate: dict,
    context: dict[str, Any],
    gates: PromotionGates = DEFAULT_GATES,
    now: datetime | None = None,
) -> PromotionEligibilityResult:
    """
    Evaluate a single candidate against governance gates.

    Parameters
    ----------
    candidate:
        Raw candidate dict (from emerging_candidates.json).
    context:
        Per-ticker context dict with optional keys:
          - enriched: news enrichment record
          - memory: discovery memory record
          - rejected: True if appears in rejected list
          - approvals: list of approval decision records
          - replay: replay outcome record
    gates:
        Tunable thresholds.
    now:
        Timestamp for staleness computation (defaults to UTC now).
    """
    now = now or datetime.now(timezone.utc)
    ticker = _normalize_ticker(candidate.get("ticker") or candidate.get("symbol"))
    if not ticker:
        return PromotionEligibilityResult(
            ticker="",
            decision_type="hold_status",
            reason="missing ticker — no decision applied",
        )

    prior_status = _normalize_status(candidate.get("status"))
    raw_upstream = str(candidate.get("status") or "").strip().upper()

    enriched = (context or {}).get("enriched") or {}
    memory = (context or {}).get("memory") or {}
    replay = (context or {}).get("replay") or {}
    rejected = bool((context or {}).get("rejected"))

    # Aggregate evidence — prefer enriched, fall back to raw candidate fields.
    corroboration_score = float(
        candidate.get("corroboration_score")
        or enriched.get("corroboration_news_score")
        or 0.0
    )
    source_diversity = int(
        candidate.get("unique_source_count")
        or enriched.get("source_diversity")
        or memory.get("source_count")
        or 0
    )
    news_relevance = float(enriched.get("news_relevance_score") or 0.0)
    risk_flags_count = max(
        len(enriched.get("risk_flags") or []),
        1 if candidate.get("risk_flag") else 0,
    )
    last_seen = _parse_iso_date(
        memory.get("last_seen") or candidate.get("last_seen")
    )
    persistence_runs = int(memory.get("seen_runs") or 0)
    persistence_mentions = int(memory.get("mention_count") or candidate.get("mention_count") or 0)

    # --- Disqualifier gates first (block / reject / expire) -----------------
    gates_passed: list[str] = []
    gates_failed: list[str] = []

    if gates.block_forbidden_statuses and raw_upstream in FORBIDDEN_STATUSES:
        return PromotionEligibilityResult(
            ticker=ticker,
            eligible_for_monitor=False,
            proposed_status="REJECTED",
            decision_type="reject",
            gates_passed=gates_passed,
            gates_failed=["block_forbidden_statuses"],
            reason=(
                f"Upstream candidate carried forbidden status {raw_upstream!r}; "
                "blocked from any promotion."
            ),
        )

    if gates.block_rejected_candidates and rejected:
        return PromotionEligibilityResult(
            ticker=ticker,
            eligible_for_monitor=False,
            proposed_status="REJECTED",
            decision_type="reject",
            gates_failed=["block_rejected_candidates"],
            reason="Candidate is already in rejected list.",
        )

    # Staleness check (no recent appearance)
    is_stale = False
    if last_seen is not None:
        age_days = (now - last_seen).days
        if age_days > gates.stale_after_days:
            is_stale = True
    elif persistence_runs == 0:
        # No recency signal at all
        is_stale = True

    if is_stale and prior_status != "REJECTED":
        return PromotionEligibilityResult(
            ticker=ticker,
            eligible_for_monitor=False,
            proposed_status="EXPIRED",
            decision_type="expire",
            gates_failed=["staleness"],
            reason="Candidate is stale or has no recent supporting evidence.",
        )

    # Severe risk → reject
    if risk_flags_count > gates.maximum_risk_flags:
        return PromotionEligibilityResult(
            ticker=ticker,
            eligible_for_monitor=False,
            proposed_status="REJECTED",
            decision_type="reject",
            gates_failed=["maximum_risk_flags"],
            reason=(
                f"Risk flags ({risk_flags_count}) exceed maximum "
                f"({gates.maximum_risk_flags})."
            ),
        )

    # --- Promotion gates ----------------------------------------------------
    if corroboration_score >= gates.minimum_corrob_score:
        gates_passed.append("minimum_corrob_score")
    else:
        gates_failed.append("minimum_corrob_score")

    if source_diversity >= gates.minimum_source_diversity:
        gates_passed.append("minimum_source_diversity")
    else:
        gates_failed.append("minimum_source_diversity")

    if news_relevance >= gates.minimum_news_relevance:
        gates_passed.append("minimum_news_relevance")
    else:
        gates_failed.append("minimum_news_relevance")

    if risk_flags_count <= gates.maximum_risk_flags:
        gates_passed.append("maximum_risk_flags")
    else:
        gates_failed.append("maximum_risk_flags")

    if gates.require_watch_status_for_monitor:
        if prior_status == "WATCH":
            gates_passed.append("require_watch_status_for_monitor")
        else:
            gates_failed.append("require_watch_status_for_monitor")

    if gates.require_persistence_for_monitor:
        if (persistence_runs >= gates.minimum_persistence_runs
                and persistence_mentions >= gates.minimum_persistence_mentions):
            gates_passed.append("require_persistence_for_monitor")
        else:
            gates_failed.append("require_persistence_for_monitor")

    # Replay context: if explicitly negative, surface as a gate failure
    replay_outcome = str(replay.get("outcome") or replay.get("status") or "").lower()
    if replay_outcome in ("strongly_negative", "fail", "underperform"):
        gates_failed.append("replay_outcome_acceptable")
    else:
        gates_passed.append("replay_outcome_acceptable")

    # --- Decision logic -----------------------------------------------------
    if not gates_failed:
        return PromotionEligibilityResult(
            ticker=ticker,
            eligible_for_monitor=True,
            proposed_status="MONITOR",
            decision_type="promote_to_monitor",
            gates_passed=gates_passed,
            gates_failed=gates_failed,
            reason="All promotion gates passed; candidate moved to MONITOR.",
        )

    # Some gates failed — decide between NEEDS_REVIEW and hold/expire/reject.
    # If only "soft" gates failed (e.g. news_relevance / source_diversity /
    # persistence) but evidence is otherwise present, flag for review.
    soft_only = set(gates_failed).issubset({
        "minimum_news_relevance",
        "minimum_source_diversity",
        "require_persistence_for_monitor",
        "replay_outcome_acceptable",
    })
    if soft_only and corroboration_score >= gates.minimum_corrob_score * 0.5:
        return PromotionEligibilityResult(
            ticker=ticker,
            eligible_for_monitor=False,
            proposed_status="NEEDS_REVIEW",
            decision_type="demote_to_review",
            gates_passed=gates_passed,
            gates_failed=gates_failed,
            reason="Mixed evidence — flagged for operator review.",
        )

    # Otherwise: hold prior status (no movement applied).
    return PromotionEligibilityResult(
        ticker=ticker,
        eligible_for_monitor=False,
        proposed_status=prior_status,
        decision_type="hold_status",
        gates_passed=gates_passed,
        gates_failed=gates_failed,
        reason="Insufficient evidence for promotion; prior status retained.",
    )


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _build_decision_record(
    candidate: dict,
    context: dict[str, Any],
    result: PromotionEligibilityResult,
    created_at: str,
) -> PromotionDecision:
    enriched = (context or {}).get("enriched") or {}
    memory = (context or {}).get("memory") or {}
    replay = (context or {}).get("replay") or {}
    approvals = (context or {}).get("approvals") or []

    risk_flags = [
        sanitize_label(f) for f in (enriched.get("risk_flags") or [])
        if isinstance(f, str)
    ]
    catalyst_flags = [
        sanitize_label(f) for f in (enriched.get("catalyst_flags") or [])
        if isinstance(f, str)
    ]
    risk_flags = [f for f in risk_flags if f]
    catalyst_flags = [f for f in catalyst_flags if f]

    corroboration = float(
        candidate.get("corroboration_score")
        or enriched.get("corroboration_news_score")
        or 0.0
    )
    news_relevance = float(enriched.get("news_relevance_score") or 0.0)
    source_diversity = int(
        candidate.get("unique_source_count")
        or enriched.get("source_diversity")
        or memory.get("source_count")
        or 0
    )

    # Simple evidence score (0..1), weighted mix.
    evidence_score = round(
        0.45 * min(1.0, corroboration) +
        0.30 * min(1.0, news_relevance) +
        0.25 * min(1.0, source_diversity / 5.0),
        3,
    )

    replay_outcome = sanitize_label(
        replay.get("outcome") or replay.get("status") or ""
    )
    replay_context_str = (
        f"replay_outcome={replay_outcome or 'none'}"
    )
    memory_context_str = sanitize_automatic_promotion_text(
        f"runs={int(memory.get('seen_runs') or 0)}, "
        f"mentions={int(memory.get('mention_count') or 0)}, "
        f"last_seen={str(memory.get('last_seen') or 'unknown')}"
    )
    operator_context_str = sanitize_automatic_promotion_text(
        f"approval_records={len(approvals)}"
    )

    evidence_summary = sanitize_automatic_promotion_text(
        f"corr={corroboration:.2f}, news_rel={news_relevance:.2f}, "
        f"sources={source_diversity}, risk_flags={len(risk_flags)}, "
        f"catalysts={len(catalyst_flags)}"
    )

    eligibility_result = sanitize_automatic_promotion_text(
        f"passed={len(result.gates_passed)}, failed={len(result.gates_failed)}: "
        f"{result.reason}"
    )

    safety_flags = {
        "observe_only": _OBSERVE_ONLY,
        "no_trade": _NO_TRADE,
        "not_recommendation": _NOT_RECOMMENDATION,
        "discovery_only": _DISCOVERY_ONLY,
        "no_portfolio_mutation": _NO_PORTFOLIO_MUTATION,
        "no_watchlist_mutation": _NO_WATCHLIST_MUTATION,
        "no_decision_override": _NO_DECISION_OVERRIDE,
        "no_score_mutation": _NO_SCORE_MUTATION,
        "no_allocation_mutation": _NO_ALLOCATION_MUTATION,
    }

    return PromotionDecision(
        ticker=result.ticker,
        prior_status=_normalize_status(candidate.get("status")),
        proposed_status=result.proposed_status,
        decision_type=result.decision_type,
        eligibility_result=eligibility_result,
        evidence_score=evidence_score,
        evidence_summary=evidence_summary,
        gates_passed=list(result.gates_passed),
        gates_failed=list(result.gates_failed),
        risk_flags=risk_flags[:5],
        catalyst_flags=catalyst_flags[:5],
        corroboration_score=round(corroboration, 3),
        news_relevance_score=round(news_relevance, 3),
        source_diversity=source_diversity,
        replay_context=replay_context_str,
        memory_context=memory_context_str,
        operator_context=operator_context_str,
        safety_flags=safety_flags,
        created_at=created_at,
        reason=sanitize_automatic_promotion_text(result.reason),
    )


def build_automatic_promotion_report(
    inputs: dict[str, Any],
    run_mode: str | RunMode = "discovery",
    run_id: str | None = None,
    gates: PromotionGates = DEFAULT_GATES,
) -> AutomaticPromotionReport:
    """
    Build the full AutomaticPromotionReport from loaded inputs.

    Deterministic — same inputs → same output except generated_at and run_id.
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    mode = normalize_run_mode(run_mode)
    _run_id = run_id or f"{generated_at[:10]}_automatic_promotion"

    def _payload(key: str) -> Any:
        return (inputs.get(key) or {}).get("payload")

    def _summary(key: str) -> AutomaticPromotionInputSummary:
        return (inputs.get(key) or {}).get(
            "summary",
            AutomaticPromotionInputSummary(artifact=key, available=False),
        )

    emerging = _payload("emerging_candidates")
    rejected = _payload("rejected_candidates")
    memory = _payload("discovery_memory")
    enriched = _payload("news_enriched_candidates")
    replay = _payload("replay_results")
    approvals = _payload("approval_decisions") or []

    all_summaries = [_summary(k) for k in inputs]
    used = [s for s in all_summaries if s.available]
    missing = [s.artifact for s in all_summaries if not s.available]

    # Build indexes
    emerging_idx = _index_candidates_by_ticker(emerging)
    rejected_idx = _index_candidates_by_ticker(rejected)
    enriched_idx = _index_enriched(enriched)
    memory_idx = _index_memory(memory)
    approvals_idx = _index_approvals(approvals if isinstance(approvals, list) else [])
    replay_idx = _index_replay(replay)

    # Evaluate every candidate (emerging + rejected).  Sorted for determinism.
    decisions: list[PromotionDecision] = []
    seen: set[str] = set()
    now = datetime.now(timezone.utc)

    def _eval_and_record(cand: dict, in_rejected: bool) -> None:
        ticker = _normalize_ticker(cand.get("ticker") or cand.get("symbol"))
        if not ticker or ticker in seen:
            return
        seen.add(ticker)
        ctx = {
            "enriched": enriched_idx.get(ticker, {}),
            "memory": memory_idx.get(ticker, {}),
            "rejected": in_rejected,
            "approvals": approvals_idx.get(ticker, []),
            "replay": replay_idx.get(ticker, {}),
        }
        result = evaluate_candidate_promotion(cand, ctx, gates=gates, now=now)
        decisions.append(_build_decision_record(cand, ctx, result, generated_at))

    for ticker in sorted(emerging_idx):
        _eval_and_record(emerging_idx[ticker], in_rejected=False)
    for ticker in sorted(rejected_idx):
        _eval_and_record(rejected_idx[ticker], in_rejected=True)

    # Gate summary counts
    gate_summary: dict[str, int] = {}
    for d in decisions:
        for g in d.gates_failed:
            gate_summary[f"failed::{g}"] = gate_summary.get(f"failed::{g}", 0) + 1
        for g in d.gates_passed:
            gate_summary[f"passed::{g}"] = gate_summary.get(f"passed::{g}", 0) + 1

    gates_dict = {
        "minimum_corrob_score": gates.minimum_corrob_score,
        "minimum_source_diversity": gates.minimum_source_diversity,
        "minimum_news_relevance": gates.minimum_news_relevance,
        "maximum_risk_flags": gates.maximum_risk_flags,
        "stale_after_days": gates.stale_after_days,
        "minimum_persistence_runs": gates.minimum_persistence_runs,
        "minimum_persistence_mentions": gates.minimum_persistence_mentions,
        "require_watch_status_for_monitor": gates.require_watch_status_for_monitor,
        "require_persistence_for_monitor": gates.require_persistence_for_monitor,
        "block_rejected_candidates": gates.block_rejected_candidates,
        "block_forbidden_statuses": gates.block_forbidden_statuses,
    }

    report = AutomaticPromotionReport(
        generated_at=generated_at,
        run_mode=mode.value,
        run_id=_run_id,
        data_available=bool(used),
        inputs_used=all_summaries,
        missing_inputs=missing,
        decisions=decisions,
        gate_summary=gate_summary,
        gates=gates_dict,
    )

    # Validate full report (will be re-validated at write time).
    report.prohibited_actions_detected = validate_automatic_promotion_safety(report)
    return report


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def render_automatic_promotion_markdown(report: AutomaticPromotionReport) -> str:
    lines: list[str] = []
    lines.append("# Automatic Promotion Governance _(Sandbox Research Only)_")
    lines.append("")
    lines.append(f"**Generated:** {report.generated_at}")
    lines.append(f"**Run mode:** {report.run_mode}")
    lines.append(f"**Run id:** {report.run_id}")
    lines.append("")
    lines.append(f"> **{report.safety_disclaimer}**")
    lines.append("")
    lines.append(f"_{_DISCOVERY_DISCLAIMER}_")
    lines.append("")

    moved_to_monitor = [d for d in report.decisions if d.proposed_status == "MONITOR"]
    needs_review = [d for d in report.decisions if d.proposed_status == "NEEDS_REVIEW"]
    rejected = [d for d in report.decisions if d.proposed_status == "REJECTED"]
    expired = [d for d in report.decisions if d.proposed_status == "EXPIRED"]

    lines.append("## Candidates Moved To Monitor")
    lines.append("")
    if moved_to_monitor:
        for d in moved_to_monitor[:15]:
            lines.append(
                f"- **{d.ticker}**: evidence_score={d.evidence_score}; "
                f"prior=`{d.prior_status}` → proposed=`{d.proposed_status}` "
                f"({d.decision_type})"
            )
    else:
        lines.append("_No candidates currently meet all promotion gates._")
    lines.append("")

    lines.append("## Candidates Needing Review")
    lines.append("")
    if needs_review:
        for d in needs_review[:15]:
            lines.append(
                f"- **{d.ticker}**: {d.reason} (failed: "
                f"{', '.join(d.gates_failed[:3]) or 'none'})"
            )
    else:
        lines.append("_No candidates currently flagged for review._")
    lines.append("")

    lines.append("## Candidates Rejected / Expired")
    lines.append("")
    if rejected or expired:
        for d in (rejected + expired)[:15]:
            lines.append(
                f"- **{d.ticker}** _({d.proposed_status})_: {d.reason}"
            )
    else:
        lines.append("_No rejected or expired candidates._")
    lines.append("")

    lines.append("## Gate Summary")
    lines.append("")
    if report.gate_summary:
        for gate_key, count in sorted(report.gate_summary.items()):
            lines.append(f"- `{gate_key}`: {count}")
    else:
        lines.append("_No gate evaluations recorded (no candidates)._")
    lines.append("")

    lines.append("## Risk Notes")
    lines.append("")
    risky = [d for d in report.decisions if d.risk_flags]
    if risky:
        for d in risky[:10]:
            lines.append(
                f"- **{d.ticker}**: risk flags — {', '.join(d.risk_flags[:3])}"
            )
    else:
        lines.append("_No risk-flagged candidates._")
    lines.append("")

    lines.append("## Safety Boundary")
    lines.append("")
    # Insert the whitelisted documentation block verbatim so its embedded
    # forbidden tokens are recognized by the sanitizer/validator as allowed.
    lines.append(_SAFETY_BOUNDARY_DOC)
    lines.append("")

    # Coverage footer
    available_count = sum(1 for i in report.inputs_used if i.available)
    lines.append("## Coverage")
    lines.append("")
    lines.append(f"- Inputs available: {available_count} / {len(report.inputs_used)}")
    if report.missing_inputs:
        lines.append(f"- Missing inputs: {', '.join(report.missing_inputs[:8])}")
    lines.append("")

    lines.append("---")
    lines.append(f"*Source: {report.source}*")
    lines.append(
        f"*observe_only: {report.observe_only} | "
        f"no_trade: {report.no_trade} | "
        f"not_recommendation: {report.not_recommendation} | "
        f"discovery_only: {report.discovery_only}*"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report → dict serializer
# ---------------------------------------------------------------------------

def _decision_to_dict(d: PromotionDecision) -> dict:
    return {
        "ticker": d.ticker,
        "prior_status": d.prior_status,
        "proposed_status": d.proposed_status,
        "decision_type": d.decision_type,
        "eligibility_result": d.eligibility_result,
        "evidence_score": d.evidence_score,
        "evidence_summary": d.evidence_summary,
        "gates_passed": d.gates_passed,
        "gates_failed": d.gates_failed,
        "risk_flags": d.risk_flags,
        "catalyst_flags": d.catalyst_flags,
        "corroboration_score": d.corroboration_score,
        "news_relevance_score": d.news_relevance_score,
        "source_diversity": d.source_diversity,
        "replay_context": d.replay_context,
        "memory_context": d.memory_context,
        "operator_context": d.operator_context,
        "safety_flags": d.safety_flags,
        "created_at": d.created_at,
        "reason": d.reason,
    }


def _report_to_dict(report: AutomaticPromotionReport) -> dict[str, Any]:
    return {
        "generated_at": report.generated_at,
        "run_mode": report.run_mode,
        "run_id": report.run_id,
        "observe_only": report.observe_only,
        "no_trade": report.no_trade,
        "not_recommendation": report.not_recommendation,
        "discovery_only": report.discovery_only,
        "no_portfolio_mutation": report.no_portfolio_mutation,
        "no_watchlist_mutation": report.no_watchlist_mutation,
        "no_decision_override": report.no_decision_override,
        "no_score_mutation": report.no_score_mutation,
        "no_allocation_mutation": report.no_allocation_mutation,
        "source": report.source,
        "data_available": report.data_available,
        "inputs_used": [
            {"artifact": i.artifact, "available": i.available, "summary": i.summary}
            for i in report.inputs_used
        ],
        "missing_inputs": report.missing_inputs,
        "gates": report.gates,
        "gate_summary": report.gate_summary,
        "decision_count": len(report.decisions),
        "monitor_count": sum(1 for d in report.decisions if d.proposed_status == "MONITOR"),
        "needs_review_count": sum(1 for d in report.decisions if d.proposed_status == "NEEDS_REVIEW"),
        "rejected_count": sum(1 for d in report.decisions if d.proposed_status == "REJECTED"),
        "expired_count": sum(1 for d in report.decisions if d.proposed_status == "EXPIRED"),
        "decisions": [_decision_to_dict(d) for d in report.decisions],
        "prohibited_actions_detected": report.prohibited_actions_detected,
        "safety_disclaimer": report.safety_disclaimer,
    }


# ---------------------------------------------------------------------------
# JSONL log append
# ---------------------------------------------------------------------------

def _append_decisions_jsonl(
    decisions: list[PromotionDecision],
    base_dir: Path,
) -> Path:
    """Append each decision as one JSONL line to the sandbox audit log."""
    path = get_output_path(OutputNamespace.SANDBOX, _DECISIONS_LOG_PATH, base_dir=base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for d in decisions:
            line = sanitize_nested_automatic_promotion_payload(_decision_to_dict(d))
            fh.write(json.dumps(line, default=str) + "\n")
    return path


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def write_automatic_promotion_report(
    report: AutomaticPromotionReport,
    base_dir: str | Path = "outputs",
    run_mode: str | RunMode = "discovery",
    run_id: str | None = None,
) -> dict[str, str]:
    """
    Write all three sandbox artifacts.

    Raises:
      - RunModeViolation if run_mode is not permitted to write sandbox.
      - UnsafeAutomaticPromotionArtifactError if prohibited language remains
        after sanitization.
    """
    base = Path(base_dir)
    mode = normalize_run_mode(run_mode)
    assert_can_write_namespace(mode, OutputNamespace.SANDBOX)

    # Sanitize JSON payload
    payload = _report_to_dict(report)
    payload = sanitize_nested_automatic_promotion_payload(payload)
    payload_violations = validate_automatic_promotion_safety(payload)
    if payload_violations:
        raise UnsafeAutomaticPromotionArtifactError(
            f"Refusing to write {_CANDIDATES_PATH!r}: prohibited language "
            f"remains: {payload_violations!r}"
        )

    # Sanitize markdown
    md_content = sanitize_automatic_promotion_text(
        render_automatic_promotion_markdown(report)
    )
    md_violations = validate_automatic_promotion_safety(md_content)
    if md_violations:
        raise UnsafeAutomaticPromotionArtifactError(
            f"Refusing to write {_SUMMARY_MD_PATH!r}: prohibited language "
            f"remains: {md_violations!r}"
        )

    # Sanitize each decision before appending to JSONL — done inside the appender.
    # But first validate the in-memory list for safety.
    decisions_payload = [
        sanitize_nested_automatic_promotion_payload(_decision_to_dict(d))
        for d in report.decisions
    ]
    jsonl_violations = validate_automatic_promotion_safety(decisions_payload)
    if jsonl_violations:
        raise UnsafeAutomaticPromotionArtifactError(
            f"Refusing to write {_DECISIONS_LOG_PATH!r}: prohibited language "
            f"remains: {jsonl_violations!r}"
        )

    json_path = safe_write_json(
        OutputNamespace.SANDBOX, _CANDIDATES_PATH, payload, base_dir=base
    )
    md_path = safe_write_text(
        OutputNamespace.SANDBOX, _SUMMARY_MD_PATH, md_content, base_dir=base
    )
    jsonl_path = _append_decisions_jsonl(report.decisions, base)

    return {
        "automatic_promotion_candidates_json": str(json_path),
        "automatic_promotion_summary_md": str(md_path),
        "automatic_promotion_decisions_jsonl": str(jsonl_path),
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_automatic_promotion_governance(
    base_dir: str | Path = "outputs",
    run_mode: str | RunMode = "discovery",
    run_id: str | None = None,
    dry_run: bool = False,
    write_files: bool = True,
    gates: PromotionGates = DEFAULT_GATES,
) -> dict[str, Any]:
    """
    Orchestrate loading, evaluation, and (optionally) sandbox writing.

    Parameters
    ----------
    base_dir:
        Output root directory (parent of outputs/).
    run_mode:
        Run mode string or RunMode enum.  Only DISCOVERY and BACKTEST may write.
        Other modes return results as a dry-run (no file writes).
    run_id:
        Optional run identifier.
    dry_run:
        If True, do not write artifacts.
    write_files:
        If False (or dry_run=True or run_mode not permitted), skip file writes.
    gates:
        Tunable promotion gates.
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    _run_id = run_id or f"{generated_at[:10]}_automatic_promotion"
    base = Path(base_dir)

    try:
        mode = normalize_run_mode(run_mode)
    except RunModeViolation as exc:
        logger.error("Invalid run mode: %s", exc)
        return _error_result(str(exc), generated_at)

    can_write = (
        write_files
        and not dry_run
        and validate_output_write(mode, OutputNamespace.SANDBOX)
    )

    result: dict[str, Any] = {
        "generated_at": generated_at,
        "run_id": _run_id,
        "run_mode": mode.value,
        "dry_run": not can_write,
        "observe_only": _OBSERVE_ONLY,
        "no_trade": _NO_TRADE,
        "not_recommendation": _NOT_RECOMMENDATION,
        "discovery_only": _DISCOVERY_ONLY,
        "no_portfolio_mutation": _NO_PORTFOLIO_MUTATION,
        "no_watchlist_mutation": _NO_WATCHLIST_MUTATION,
        "no_decision_override": _NO_DECISION_OVERRIDE,
        "no_score_mutation": _NO_SCORE_MUTATION,
        "no_allocation_mutation": _NO_ALLOCATION_MUTATION,
        "artifacts": {},
    }

    try:
        inputs = load_automatic_promotion_inputs(base)
        report = build_automatic_promotion_report(
            inputs, run_mode=mode, run_id=_run_id, gates=gates
        )
        result["data_available"] = report.data_available
        result["decision_count"] = len(report.decisions)
        result["monitor_count"] = sum(
            1 for d in report.decisions if d.proposed_status == "MONITOR"
        )
        result["needs_review_count"] = sum(
            1 for d in report.decisions if d.proposed_status == "NEEDS_REVIEW"
        )
        result["rejected_count"] = sum(
            1 for d in report.decisions if d.proposed_status == "REJECTED"
        )
        result["expired_count"] = sum(
            1 for d in report.decisions if d.proposed_status == "EXPIRED"
        )
        result["safety_violations"] = report.prohibited_actions_detected

        if can_write:
            try:
                paths = write_automatic_promotion_report(
                    report, base_dir=base, run_mode=mode, run_id=_run_id
                )
                result["artifacts"] = paths
            except UnsafeAutomaticPromotionArtifactError as exc:
                logger.error("Blocked unsafe automatic promotion artifact: %s", exc)
                result["blocked_unsafe_write"] = str(exc)
            except RunModeViolation as exc:
                logger.error("Run mode forbids write: %s", exc)
                result["blocked_run_mode"] = str(exc)
    except Exception as exc:
        logger.error(
            "run_automatic_promotion_governance failed: %s", exc, exc_info=True
        )
        result["error"] = str(exc)

    return result


def _error_result(error: str, generated_at: str) -> dict[str, Any]:
    return {
        "error": error,
        "generated_at": generated_at,
        "observe_only": _OBSERVE_ONLY,
        "no_trade": _NO_TRADE,
        "not_recommendation": _NOT_RECOMMENDATION,
        "discovery_only": _DISCOVERY_ONLY,
    }
