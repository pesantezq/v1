"""
News Evidence Layer (decision-engine adjacent, context-only)
=============================================================

Capped, observe-only layer that converts existing structured news, narrative,
and discovery evidence into decision-engine-adjacent context.  It may enrich
decision explanations, risk context, confidence notes, and operator-facing
evidence summaries — but it must NOT create, override, or mutate official
BUY/SELL/HOLD decisions, allocation, scoring, recommendations, portfolio
state, or watchlists.

Safety invariants (hardcoded):
  - observe_only: true
  - no_trade: true
  - not_recommendation: true
  - no_decision_override: true
  - no_score_mutation: true
  - no_allocation_mutation: true
  - no_watchlist_mutation: true
  - No BUY/SELL/HOLD/ACTIONABLE/PROMOTED/VALIDATED action emissions
  - Writes only to OutputNamespace.LATEST
  - No POLICY/PORTFOLIO/SANDBOX writes
  - All input artifacts are read-only
  - No LLM/AI calls — deterministic rules only
  - Hard cap: news_evidence_influence_cap = "context_only"

Public API:
  build_news_evidence_layer_report(inputs, base_dir)
  render_news_evidence_markdown(report)
  write_news_evidence_layer_report(report, base_dir)
  run_news_evidence_layer(base_dir, write_files)
  validate_news_evidence_safety(value)
  sanitize_news_evidence_text(value)
  sanitize_label(value)
  sanitize_nested_news_evidence_payload(payload)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    get_output_path,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safety constants
# ---------------------------------------------------------------------------

_OBSERVE_ONLY = True
_NO_TRADE = True
_NOT_RECOMMENDATION = True
_NO_DECISION_OVERRIDE = True
_NO_SCORE_MUTATION = True
_NO_ALLOCATION_MUTATION = True
_NO_WATCHLIST_MUTATION = True
_SOURCE_LABEL = "news_evidence_layer"
_INFLUENCE_CAP = "context_only"

_SAFETY_DISCLAIMER = (
    "This is context only, not a buy/sell/hold recommendation. "
    "The news evidence layer is capped at context_only and cannot alter "
    "official decisions, scoring, allocation, recommendations, watchlists, "
    "or portfolio state."
)

_DISCOVERY_DISCLAIMER = (
    "Discovery research is sandbox-only. "
    "No candidates are promoted or recommended."
)

# Allowed disclaimer wording for sanitizer carve-outs.
_DISCLAIMER_ALLOWED_SUBSTRINGS: tuple[str, ...] = (
    _SAFETY_DISCLAIMER,
    _DISCOVERY_DISCLAIMER,
)

_REDACTION_MARKER = "[REDACTED]"

_PROHIBITED_INSTRUCTION_PATTERNS: list[str] = [
    # explicit orders
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
    "execute trade",
    "execute order",
    "execute now",
    "place trade",
    "place order",
    # promotion/validation language
    "promote candidate",
    "promote to watchlist",
    "actionable buy",
    "actionable sell",
    "validated buy",
    "validated sell",
    # recommendation language
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

# Output filenames (relative to OutputNamespace.LATEST)
_JSON_NAME = "news_evidence_layer.json"
_MD_NAME = "news_evidence_layer.md"

# Evidence strength bands
_STRENGTH_NONE = "none"
_STRENGTH_WEAK = "weak"
_STRENGTH_MODERATE = "moderate"
_STRENGTH_STRONG = "strong"

# Context effect classifications
_EFFECT_INFORMATIONAL = "informational"
_EFFECT_RISK = "risk_context"
_EFFECT_CATALYST = "catalyst_context"
_EFFECT_CONFIDENCE = "confidence_context"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class NewsEvidenceInputSummary:
    artifact: str
    available: bool
    summary: str = ""


@dataclass
class TickerNewsEvidence:
    ticker: str
    source: str  # decision_plan | news_intelligence | discovery
    matched_article_count: int
    source_diversity: int
    themes: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    catalyst_flags: list[str] = field(default_factory=list)
    context_note: str = ""
    evidence_strength: str = _STRENGTH_NONE
    context_effect: str = _EFFECT_INFORMATIONAL


@dataclass
class NewsRiskEvidence:
    label: str
    tickers: list[str] = field(default_factory=list)
    article_count: int = 0
    description: str = ""


@dataclass
class NewsCatalystEvidence:
    label: str
    tickers: list[str] = field(default_factory=list)
    article_count: int = 0
    description: str = ""


@dataclass
class DecisionNewsContext:
    """Per-decision context surfaced for the decision explainer/memo layers.

    Strictly advisory; does not alter decisions.
    """
    ticker: str
    decision_action: str = ""           # the existing decision (read-only copy)
    decision_reason: str = ""           # the existing reason (read-only copy)
    news_evidence_strength: str = _STRENGTH_NONE
    news_context_effect: str = _EFFECT_INFORMATIONAL
    context_note: str = ""
    no_decision_override: bool = True


@dataclass
class NewsEvidenceLayerReport:
    generated_at: str
    observe_only: bool = True
    no_trade: bool = True
    not_recommendation: bool = True
    no_decision_override: bool = True
    no_score_mutation: bool = True
    no_allocation_mutation: bool = True
    no_watchlist_mutation: bool = True
    source: str = _SOURCE_LABEL
    influence_cap: str = _INFLUENCE_CAP

    data_available: bool = False
    inputs_used: list[NewsEvidenceInputSummary] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)

    portfolio_context: str = ""
    ticker_contexts: list[TickerNewsEvidence] = field(default_factory=list)
    decision_contexts: list[DecisionNewsContext] = field(default_factory=list)
    risk_evidence: list[NewsRiskEvidence] = field(default_factory=list)
    catalyst_evidence: list[NewsCatalystEvidence] = field(default_factory=list)
    discovery_context_summary: str = ""
    confidence_context: list[str] = field(default_factory=list)
    operator_review_flags: list[str] = field(default_factory=list)
    memo_bullets: list[str] = field(default_factory=list)

    prohibited_actions_detected: list[str] = field(default_factory=list)
    safety_disclaimer: str = _SAFETY_DISCLAIMER


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class UnsafeNewsEvidenceArtifactError(RuntimeError):
    """Raised when prohibited language remains after sanitization."""


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


def _load_input(path: Path, label: str) -> tuple[Any, NewsEvidenceInputSummary]:
    payload = _safe_load_json(path)
    if payload is None:
        return None, NewsEvidenceInputSummary(artifact=label, available=False)
    if not isinstance(payload, dict):
        return None, NewsEvidenceInputSummary(
            artifact=label, available=False, summary="non-object JSON"
        )
    return payload, NewsEvidenceInputSummary(artifact=label, available=True)


def load_all_inputs(base_dir: str | Path = "outputs") -> dict[str, Any]:
    """
    Load all input artifacts safely.

    Returns dict keyed by artifact label, each entry has "payload" and "summary".
    Missing, malformed, list-valued, or non-object inputs degrade silently.
    """
    base = Path(base_dir)

    def _latest(name: str) -> Path:
        return get_output_path(OutputNamespace.LATEST, name, base_dir=base)

    def _sandbox(name: str) -> Path:
        return get_output_path(OutputNamespace.SANDBOX, name, base_dir=base)

    paths: dict[str, Path] = {
        "news_intelligence":         _latest("news_intelligence.json"),
        "market_narrative_daily":    _latest("market_narrative_daily.json"),
        "market_narrative_weekly":   _latest("market_narrative_weekly.json"),
        "market_narrative_monthly":  _latest("market_narrative_monthly.json"),
        "decision_plan":             _latest("decision_plan.json"),
        "decision_explanations":     _latest("decision_explanations.json"),
        "system_decision_summary":   _latest("system_decision_summary.json"),
        "data_quality_report":       _latest("data_quality_report.json"),
        "confidence_calibration":    _latest("confidence_calibration.json"),
        "news_enriched_candidates":  _sandbox("discovery/news_enriched_candidates.json"),
        "emerging_candidates":       _sandbox("discovery/emerging_candidates.json"),
        "rejected_candidates":       _sandbox("discovery/rejected_candidates.json"),
        "replay_results":            _sandbox("discovery/replay_results.json"),
    }

    loaded: dict[str, Any] = {}
    for label, path in paths.items():
        payload, summary = _load_input(path, label)
        loaded[label] = {"payload": payload, "summary": summary}
    return loaded


# ---------------------------------------------------------------------------
# Sanitizer / validator
# ---------------------------------------------------------------------------

def _detect_prohibited_phrases(text: str) -> list[str]:
    if not text:
        return []
    lower = text.lower()
    return [p for p in _PROHIBITED_INSTRUCTION_PATTERNS if p in lower]


def _text_contains_only_allowed_disclaimer(text: str, violations: list[str]) -> bool:
    if not violations:
        return True
    sanitized = text
    for allowed in _DISCLAIMER_ALLOWED_SUBSTRINGS:
        sanitized = sanitized.replace(allowed, "")
    sanitized_lower = sanitized.lower()
    return not any(v in sanitized_lower for v in violations)


def validate_news_evidence_safety(value: Any) -> list[str]:
    """
    Walk a string, dict, list, tuple, set, or dataclass and return prohibited
    phrases detected.  Fixed safety disclaimers are excluded from violations.
    """
    violations: set[str] = set()

    def _walk(node: Any) -> None:
        if node is None or isinstance(node, (bool, int, float)):
            return
        if isinstance(node, str):
            detected = _detect_prohibited_phrases(node)
            if detected and not _text_contains_only_allowed_disclaimer(node, detected):
                stripped = node
                for allowed in _DISCLAIMER_ALLOWED_SUBSTRINGS:
                    stripped = stripped.replace(allowed, "")
                for p in _detect_prohibited_phrases(stripped):
                    violations.add(p)
            return
        if isinstance(node, dict):
            for v in node.values():
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


def sanitize_news_evidence_text(value: str) -> str:
    """
    Replace prohibited substrings with ``[REDACTED]`` while preserving the
    fixed safety disclaimer wording exactly.
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

    lower = out.lower()
    for pattern in _PROHIBITED_INSTRUCTION_PATTERNS:
        while pattern in lower:
            idx = lower.find(pattern)
            out = out[:idx] + _REDACTION_MARKER + out[idx + len(pattern):]
            lower = out.lower()

    for token, allowed in placeholders:
        out = out.replace(token, allowed)
    return out


def sanitize_label(value: Any) -> str:
    """Sanitize a label-style string; coerces non-strings to str."""
    if value is None:
        return ""
    return sanitize_news_evidence_text(str(value))


def sanitize_nested_news_evidence_payload(payload: Any) -> Any:
    """Recursively sanitize every string in a JSON-serializable payload."""
    if payload is None or isinstance(payload, (bool, int, float)):
        return payload
    if isinstance(payload, str):
        return sanitize_news_evidence_text(payload)
    if isinstance(payload, dict):
        return {k: sanitize_nested_news_evidence_payload(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [sanitize_nested_news_evidence_payload(v) for v in payload]
    if isinstance(payload, tuple):
        return tuple(sanitize_nested_news_evidence_payload(v) for v in payload)
    if isinstance(payload, set):
        return {sanitize_nested_news_evidence_payload(v) for v in payload}
    return payload


# ---------------------------------------------------------------------------
# Ticker discovery from inputs
# ---------------------------------------------------------------------------

def _normalize_ticker(value: Any) -> str:
    if not value:
        return ""
    return sanitize_label(str(value).upper().strip())


def _collect_decision_tickers(
    decision_plan: dict | None,
    explanations: dict | None,
) -> dict[str, dict[str, str]]:
    """
    Return a dict ticker → {decision_action, decision_reason} drawn from
    decision_plan / decision_explanations.  Both fields are sanitized.
    """
    out: dict[str, dict[str, str]] = {}

    if isinstance(decision_plan, dict):
        decisions = decision_plan.get("decisions") or []
        if isinstance(decisions, list):
            for d in decisions:
                if not isinstance(d, dict):
                    continue
                ticker = _normalize_ticker(d.get("ticker") or d.get("symbol"))
                if not ticker:
                    continue
                action = sanitize_label(d.get("decision") or d.get("action") or "")
                reason = sanitize_news_evidence_text(
                    str(d.get("decision_reason") or d.get("reason") or "")
                )
                out[ticker] = {
                    "decision_action": action,
                    "decision_reason": reason[:240],  # cap reason length
                }

    if isinstance(explanations, dict):
        items = explanations.get("explanations") or []
        if isinstance(items, list):
            for e in items:
                if not isinstance(e, dict):
                    continue
                ticker = _normalize_ticker(e.get("ticker") or e.get("symbol"))
                if not ticker:
                    continue
                rec = out.setdefault(ticker, {"decision_action": "", "decision_reason": ""})
                if not rec["decision_reason"]:
                    rec["decision_reason"] = sanitize_news_evidence_text(
                        str(e.get("explanation") or "")
                    )[:240]
    return out


def _index_news_packets(news_intel: dict | None) -> dict[str, list[dict]]:
    """Return ticker → list of evidence packets (entity_key + related_tickers)."""
    if not isinstance(news_intel, dict):
        return {}
    packets = news_intel.get("evidence_packets") or []
    if not isinstance(packets, list):
        return {}
    index: dict[str, list[dict]] = {}
    for p in packets:
        if not isinstance(p, dict):
            continue
        key = _normalize_ticker(p.get("entity_key"))
        if key:
            index.setdefault(key, []).append(p)
        for rel in (p.get("related_tickers") or []):
            rk = _normalize_ticker(rel)
            if rk and rk != key:
                index.setdefault(rk, []).append(p)
    return index


def _index_discovery_enriched(enriched: dict | None) -> dict[str, dict]:
    """Return ticker → enriched candidate record."""
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


# ---------------------------------------------------------------------------
# Evidence assembly
# ---------------------------------------------------------------------------

def _classify_evidence_strength(article_count: int, source_diversity: int) -> str:
    if article_count == 0:
        return _STRENGTH_NONE
    if article_count >= 8 and source_diversity >= 4:
        return _STRENGTH_STRONG
    if article_count >= 4 and source_diversity >= 2:
        return _STRENGTH_MODERATE
    return _STRENGTH_WEAK


def _classify_context_effect(
    risk_flags: list[str],
    catalyst_flags: list[str],
    strength: str,
) -> str:
    if strength == _STRENGTH_NONE:
        return _EFFECT_CONFIDENCE
    risk_count = len(risk_flags)
    cat_count = len(catalyst_flags)
    if risk_count >= 2 and risk_count > cat_count:
        return _EFFECT_RISK
    if cat_count >= 1 and cat_count > risk_count:
        return _EFFECT_CATALYST
    return _EFFECT_INFORMATIONAL


def _aggregate_ticker_evidence(
    ticker: str,
    source: str,
    packets: list[dict],
    enriched_record: dict | None,
) -> TickerNewsEvidence:
    article_count = sum(int(p.get("article_count") or 0) for p in packets)
    source_diversity = sum(int(p.get("source_count") or 0) for p in packets)

    themes_seen: list[str] = []
    risk_flags: list[str] = []
    catalyst_flags: list[str] = []
    for p in packets:
        for t in (p.get("themes") or []):
            if isinstance(t, str):
                clean = sanitize_label(t)
                if clean and clean not in themes_seen:
                    themes_seen.append(clean)
        for f in (p.get("risk_flags") or []):
            if isinstance(f, str):
                clean = sanitize_label(f)
                if clean and clean not in risk_flags:
                    risk_flags.append(clean)
        for f in (p.get("catalyst_flags") or []):
            if isinstance(f, str):
                clean = sanitize_label(f)
                if clean and clean not in catalyst_flags:
                    catalyst_flags.append(clean)

    # Pull discovery-side flags too (only sandbox lane is consumed for context).
    if enriched_record is not None:
        for f in (enriched_record.get("risk_flags") or []):
            if isinstance(f, str):
                clean = sanitize_label(f)
                if clean and clean not in risk_flags:
                    risk_flags.append(clean)
        for f in (enriched_record.get("catalyst_flags") or []):
            if isinstance(f, str):
                clean = sanitize_label(f)
                if clean and clean not in catalyst_flags:
                    catalyst_flags.append(clean)
        for t in (enriched_record.get("matched_themes") or []):
            if isinstance(t, str):
                clean = sanitize_label(t)
                if clean and clean not in themes_seen:
                    themes_seen.append(clean)
        article_count += int(enriched_record.get("matched_news_count") or 0)
        source_diversity += int(enriched_record.get("source_diversity") or 0)

    strength = _classify_evidence_strength(article_count, source_diversity)
    effect = _classify_context_effect(risk_flags, catalyst_flags, strength)

    note_parts: list[str] = []
    if article_count:
        note_parts.append(
            f"{article_count} matched article(s) across {source_diversity} source(s)"
        )
    if themes_seen[:3]:
        note_parts.append(f"themes: {', '.join(themes_seen[:3])}")
    if risk_flags[:2]:
        note_parts.append(f"risk: {', '.join(risk_flags[:2])}")
    if catalyst_flags[:2]:
        note_parts.append(f"catalysts: {', '.join(catalyst_flags[:2])}")
    note = sanitize_news_evidence_text(
        " | ".join(note_parts) if note_parts else "No matched news evidence."
    )

    return TickerNewsEvidence(
        ticker=ticker,
        source=source,
        matched_article_count=article_count,
        source_diversity=source_diversity,
        themes=themes_seen[:8],
        risk_flags=risk_flags[:5],
        catalyst_flags=catalyst_flags[:5],
        context_note=note,
        evidence_strength=strength,
        context_effect=effect,
    )


def _build_risk_evidence_aggregate(
    news_intel: dict | None,
    enriched: dict | None,
) -> list[NewsRiskEvidence]:
    risk_map: dict[str, dict[str, Any]] = {}

    def _process(packets: list, source: str) -> None:
        for p in packets:
            if not isinstance(p, dict):
                continue
            ticker = _normalize_ticker(p.get("entity_key") or p.get("ticker") or "")
            article_count = int(p.get("article_count") or p.get("matched_news_count") or 0)
            for f in (p.get("risk_flags") or []):
                if isinstance(f, str):
                    label = sanitize_label(f)
                    if not label:
                        continue
                    rec = risk_map.setdefault(label, {"tickers": [], "count": 0})
                    if ticker and ticker not in rec["tickers"]:
                        rec["tickers"].append(ticker)
                    rec["count"] += article_count

    if isinstance(news_intel, dict):
        _process(news_intel.get("evidence_packets") or [], "news_intelligence")
    if isinstance(enriched, dict):
        _process(enriched.get("enriched_candidates") or [], "discovery")

    out: list[NewsRiskEvidence] = []
    for label, rec in sorted(risk_map.items(), key=lambda x: len(x[1]["tickers"]), reverse=True)[:8]:
        desc = sanitize_news_evidence_text(
            f"Risk signal observed for: {', '.join(rec['tickers'][:5]) or 'unknown'}."
        )
        out.append(NewsRiskEvidence(
            label=label,
            tickers=rec["tickers"][:5],
            article_count=rec["count"],
            description=desc,
        ))
    return out


def _build_catalyst_evidence_aggregate(
    news_intel: dict | None,
    enriched: dict | None,
) -> list[NewsCatalystEvidence]:
    cat_map: dict[str, dict[str, Any]] = {}

    def _process(packets: list, source: str) -> None:
        for p in packets:
            if not isinstance(p, dict):
                continue
            ticker = _normalize_ticker(p.get("entity_key") or p.get("ticker") or "")
            article_count = int(p.get("article_count") or p.get("matched_news_count") or 0)
            for f in (p.get("catalyst_flags") or []):
                if isinstance(f, str):
                    label = sanitize_label(f)
                    if not label:
                        continue
                    rec = cat_map.setdefault(label, {"tickers": [], "count": 0})
                    if ticker and ticker not in rec["tickers"]:
                        rec["tickers"].append(ticker)
                    rec["count"] += article_count

    if isinstance(news_intel, dict):
        _process(news_intel.get("evidence_packets") or [], "news_intelligence")
    if isinstance(enriched, dict):
        _process(enriched.get("enriched_candidates") or [], "discovery")

    out: list[NewsCatalystEvidence] = []
    for label, rec in sorted(cat_map.items(), key=lambda x: len(x[1]["tickers"]), reverse=True)[:8]:
        desc = sanitize_news_evidence_text(
            f"Catalyst signal observed for: {', '.join(rec['tickers'][:5]) or 'unknown'}."
        )
        out.append(NewsCatalystEvidence(
            label=label,
            tickers=rec["tickers"][:5],
            article_count=rec["count"],
            description=desc,
        ))
    return out


def _build_confidence_context(
    dq_report: dict | None,
    cal: dict | None,
) -> list[str]:
    notes: list[str] = []
    if isinstance(dq_report, dict):
        issues = dq_report.get("issues") or []
        if isinstance(issues, list) and issues:
            sev: dict[str, int] = {}
            for issue in issues:
                if isinstance(issue, dict):
                    s = sanitize_label(issue.get("severity") or "unknown") or "unknown"
                    sev[s] = sev.get(s, 0) + 1
            parts = [f"{v} {k}" for k, v in sev.items() if v]
            notes.append(sanitize_news_evidence_text(
                f"Data quality limits confidence in evidence: {', '.join(parts)}."
            ))
    if isinstance(cal, dict):
        resolved = cal.get("resolved_decisions") or cal.get("total_resolved") or 0
        if isinstance(resolved, (int, float)) and resolved:
            notes.append(
                f"Confidence calibration: {int(resolved)} resolved decisions available."
            )
    return notes


def _build_discovery_summary(enriched: dict | None) -> str:
    if not isinstance(enriched, dict):
        return "No discovery research context available."
    cands = enriched.get("enriched_candidates") or []
    if not isinstance(cands, list) or not cands:
        return "No discovery research candidates in sandbox."
    total = len(cands)
    supported = sum(1 for c in cands if isinstance(c, dict)
                    and c.get("news_context") == "research_supported")
    caution = sum(1 for c in cands if isinstance(c, dict)
                  and c.get("news_context") == "research_caution")
    return sanitize_news_evidence_text(
        f"{total} sandbox research candidate(s); {supported} news-supported, "
        f"{caution} risk-heavy. {_DISCOVERY_DISCLAIMER}"
    )


def _build_portfolio_context(
    decision_plan: dict | None,
    sys_summary: dict | None,
) -> str:
    parts: list[str] = []
    if isinstance(decision_plan, dict):
        decisions = decision_plan.get("decisions") or []
        if isinstance(decisions, list) and decisions:
            tickers = [
                _normalize_ticker(d.get("ticker") or d.get("symbol"))
                for d in decisions if isinstance(d, dict)
            ]
            tickers = [t for t in tickers if t][:5]
            if tickers:
                parts.append(
                    f"Decision plan covers {len(decisions)} position(s); "
                    f"top: {', '.join(tickers)}."
                )
    if isinstance(sys_summary, dict):
        health = sanitize_label(sys_summary.get("system_health") or sys_summary.get("overall_health") or "")
        if health:
            parts.append(f"System health: {health}.")
    if not parts:
        return "No decision plan data available."
    return sanitize_news_evidence_text(" ".join(parts))


def _build_operator_review_flags(
    ticker_contexts: list[TickerNewsEvidence],
    risk_evidence: list[NewsRiskEvidence],
) -> list[str]:
    flags: list[str] = []
    risk_heavy = [t for t in ticker_contexts if t.context_effect == _EFFECT_RISK]
    if risk_heavy:
        labels = ", ".join(sorted({t.ticker for t in risk_heavy})[:5])
        flags.append(sanitize_news_evidence_text(
            f"Review risk-context tickers: {labels}."
        ))
    strong = [t for t in ticker_contexts
              if t.evidence_strength == _STRENGTH_STRONG]
    if strong:
        labels = ", ".join(sorted({t.ticker for t in strong})[:5])
        flags.append(sanitize_news_evidence_text(
            f"Tickers with strong news evidence: {labels}."
        ))
    if risk_evidence:
        top = ", ".join(r.label for r in risk_evidence[:3])
        flags.append(sanitize_news_evidence_text(
            f"Top risk themes to monitor: {top}."
        ))
    return flags


def _build_memo_bullets(
    ticker_contexts: list[TickerNewsEvidence],
    risk_evidence: list[NewsRiskEvidence],
    catalyst_evidence: list[NewsCatalystEvidence],
) -> list[str]:
    bullets: list[str] = []
    # Top-3 evidence-strength tickers (non-none)
    ranked = sorted(
        [t for t in ticker_contexts if t.evidence_strength != _STRENGTH_NONE],
        key=lambda t: (t.matched_article_count, t.source_diversity),
        reverse=True,
    )[:3]
    for t in ranked:
        bullets.append(sanitize_news_evidence_text(
            f"{t.ticker}: {t.evidence_strength} news evidence ({t.context_effect})."
        ))
    if risk_evidence:
        labels = ", ".join(r.label for r in risk_evidence[:2])
        bullets.append(sanitize_news_evidence_text(
            f"Risk context themes: {labels}."
        ))
    if catalyst_evidence:
        labels = ", ".join(c.label for c in catalyst_evidence[:2])
        bullets.append(sanitize_news_evidence_text(
            f"Catalyst context themes: {labels}."
        ))
    return bullets


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def build_news_evidence_layer_report(
    inputs: dict[str, Any],
    base_dir: str | Path = "outputs",
) -> NewsEvidenceLayerReport:
    """
    Build a structured NewsEvidenceLayerReport from loaded inputs.

    Parameters
    ----------
    inputs:
        Dict returned by ``load_all_inputs()``.
    base_dir:
        Not used directly; kept for API consistency.
    """
    generated_at = datetime.now(timezone.utc).isoformat()

    def _payload(key: str) -> Any:
        return (inputs.get(key) or {}).get("payload")

    def _summary(key: str) -> NewsEvidenceInputSummary:
        return (inputs.get(key) or {}).get(
            "summary", NewsEvidenceInputSummary(artifact=key, available=False)
        )

    news_intel = _payload("news_intelligence")
    decision_plan = _payload("decision_plan")
    explanations = _payload("decision_explanations")
    sys_summary = _payload("system_decision_summary")
    dq_report = _payload("data_quality_report")
    cal = _payload("confidence_calibration")
    enriched = _payload("news_enriched_candidates")

    all_summaries = [_summary(k) for k in inputs]
    used = [s for s in all_summaries if s.available]
    missing = [s.artifact for s in all_summaries if not s.available]
    data_available = bool(used)

    decision_tickers = _collect_decision_tickers(decision_plan, explanations)
    news_index = _index_news_packets(news_intel)
    enriched_index = _index_discovery_enriched(enriched)

    # Union of all tickers (decision-side + news-side + discovery-side)
    all_tickers: list[str] = []
    seen: set[str] = set()
    for src_iter in (
        list(decision_tickers.keys()),
        list(news_index.keys()),
        list(enriched_index.keys()),
    ):
        for t in src_iter:
            if t and t not in seen:
                seen.add(t)
                all_tickers.append(t)

    ticker_contexts: list[TickerNewsEvidence] = []
    decision_contexts: list[DecisionNewsContext] = []
    for ticker in all_tickers:
        packets = news_index.get(ticker, [])
        enriched_rec = enriched_index.get(ticker)
        if ticker in decision_tickers:
            source = "decision_plan"
        elif packets:
            source = "news_intelligence"
        else:
            source = "discovery"
        ev = _aggregate_ticker_evidence(ticker, source, packets, enriched_rec)
        ticker_contexts.append(ev)

        if ticker in decision_tickers:
            dec_rec = decision_tickers[ticker]
            decision_contexts.append(DecisionNewsContext(
                ticker=ticker,
                decision_action=dec_rec.get("decision_action", ""),
                decision_reason=dec_rec.get("decision_reason", ""),
                news_evidence_strength=ev.evidence_strength,
                news_context_effect=ev.context_effect,
                context_note=ev.context_note,
            ))

    risk_evidence = _build_risk_evidence_aggregate(news_intel, enriched)
    catalyst_evidence = _build_catalyst_evidence_aggregate(news_intel, enriched)
    discovery_summary = _build_discovery_summary(enriched)
    confidence_context = _build_confidence_context(dq_report, cal)
    portfolio_context = _build_portfolio_context(decision_plan, sys_summary)
    operator_flags = _build_operator_review_flags(ticker_contexts, risk_evidence)
    memo_bullets = _build_memo_bullets(ticker_contexts, risk_evidence, catalyst_evidence)

    report = NewsEvidenceLayerReport(
        generated_at=generated_at,
        data_available=data_available,
        inputs_used=all_summaries,
        missing_inputs=missing,
        portfolio_context=portfolio_context,
        ticker_contexts=ticker_contexts,
        decision_contexts=decision_contexts,
        risk_evidence=risk_evidence,
        catalyst_evidence=catalyst_evidence,
        discovery_context_summary=discovery_summary,
        confidence_context=confidence_context,
        operator_review_flags=operator_flags,
        memo_bullets=memo_bullets,
        safety_disclaimer=_SAFETY_DISCLAIMER,
    )

    report.prohibited_actions_detected = validate_news_evidence_safety(report)
    return report


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def render_news_evidence_markdown(report: NewsEvidenceLayerReport) -> str:
    lines: list[str] = []
    lines.append("# News Evidence Layer")
    lines.append("")
    lines.append(f"**Generated:** {report.generated_at}")
    lines.append(f"**Influence cap:** `{report.influence_cap}`")
    lines.append("")
    lines.append(f"> **{report.safety_disclaimer}**")
    lines.append("")

    lines.append("## Portfolio Context")
    lines.append("")
    lines.append(report.portfolio_context)
    lines.append("")

    if report.ticker_contexts:
        lines.append("## Ticker Evidence Context")
        lines.append("")
        for t in report.ticker_contexts[:15]:
            lines.append(
                f"- **{t.ticker}** _({t.evidence_strength} / {t.context_effect})_: "
                f"{t.context_note}"
            )
        lines.append("")

    if report.risk_evidence:
        lines.append("## Risks To Monitor")
        lines.append("")
        for r in report.risk_evidence[:6]:
            tickers = f" [{', '.join(r.tickers[:3])}]" if r.tickers else ""
            lines.append(f"- **{r.label}**{tickers}: {r.description}")
        lines.append("")

    if report.catalyst_evidence:
        lines.append("## Catalysts To Monitor")
        lines.append("")
        for c in report.catalyst_evidence[:6]:
            tickers = f" [{', '.join(c.tickers[:3])}]" if c.tickers else ""
            lines.append(f"- **{c.label}**{tickers}: {c.description}")
        lines.append("")

    if report.discovery_context_summary:
        lines.append("## Discovery Research Context _(Sandbox Only)_")
        lines.append("")
        lines.append(report.discovery_context_summary)
        lines.append("")

    if report.confidence_context:
        lines.append("## Confidence / Data Quality Context")
        lines.append("")
        for note in report.confidence_context:
            lines.append(f"- {note}")
        lines.append("")

    if report.operator_review_flags:
        lines.append("## Operator Review Flags")
        lines.append("")
        for flag in report.operator_review_flags:
            lines.append(f"- {flag}")
        lines.append("")

    if report.memo_bullets:
        lines.append("## Memo Bullets")
        lines.append("")
        for b in report.memo_bullets:
            lines.append(f"- {b}")
        lines.append("")

    # Coverage
    available_count = sum(1 for i in report.inputs_used if i.available)
    lines.append("## Safety Boundary & Coverage")
    lines.append("")
    lines.append(
        f"- Inputs available: {available_count} / {len(report.inputs_used)}"
    )
    if report.missing_inputs:
        lines.append(f"- Missing inputs: {', '.join(report.missing_inputs[:8])}")
    lines.append(f"- Influence cap: `{report.influence_cap}` "
                 "(no decision/score/allocation/watchlist mutation)")
    lines.append("")

    lines.append("---")
    lines.append(f"*Source: {report.source}*")
    lines.append(
        f"*observe_only: {report.observe_only} | "
        f"no_trade: {report.no_trade} | "
        f"not_recommendation: {report.not_recommendation} | "
        f"no_decision_override: {report.no_decision_override}*"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report → dict serializer
# ---------------------------------------------------------------------------

def _report_to_dict(report: NewsEvidenceLayerReport) -> dict[str, Any]:
    def _inp(i: NewsEvidenceInputSummary) -> dict:
        return {"artifact": i.artifact, "available": i.available, "summary": i.summary}

    def _tk(t: TickerNewsEvidence) -> dict:
        return {
            "ticker": t.ticker,
            "source": t.source,
            "matched_article_count": t.matched_article_count,
            "source_diversity": t.source_diversity,
            "themes": t.themes,
            "risk_flags": t.risk_flags,
            "catalyst_flags": t.catalyst_flags,
            "context_note": t.context_note,
            "evidence_strength": t.evidence_strength,
            "context_effect": t.context_effect,
        }

    def _dc(d: DecisionNewsContext) -> dict:
        return {
            "ticker": d.ticker,
            "decision_action": d.decision_action,
            "decision_reason": d.decision_reason,
            "news_evidence_strength": d.news_evidence_strength,
            "news_context_effect": d.news_context_effect,
            "context_note": d.context_note,
            "no_decision_override": d.no_decision_override,
        }

    def _r(r: NewsRiskEvidence) -> dict:
        return {
            "label": r.label, "tickers": r.tickers,
            "article_count": r.article_count, "description": r.description,
        }

    def _c(c: NewsCatalystEvidence) -> dict:
        return {
            "label": c.label, "tickers": c.tickers,
            "article_count": c.article_count, "description": c.description,
        }

    return {
        "generated_at": report.generated_at,
        "observe_only": report.observe_only,
        "no_trade": report.no_trade,
        "not_recommendation": report.not_recommendation,
        "no_decision_override": report.no_decision_override,
        "no_score_mutation": report.no_score_mutation,
        "no_allocation_mutation": report.no_allocation_mutation,
        "no_watchlist_mutation": report.no_watchlist_mutation,
        "source": report.source,
        "influence_cap": report.influence_cap,
        "data_available": report.data_available,
        "inputs_used": [_inp(i) for i in report.inputs_used],
        "missing_inputs": report.missing_inputs,
        "portfolio_context": report.portfolio_context,
        "ticker_contexts": [_tk(t) for t in report.ticker_contexts],
        "decision_contexts": [_dc(d) for d in report.decision_contexts],
        "risk_evidence": [_r(r) for r in report.risk_evidence],
        "catalyst_evidence": [_c(c) for c in report.catalyst_evidence],
        "discovery_context_summary": report.discovery_context_summary,
        "confidence_context": report.confidence_context,
        "operator_review_flags": report.operator_review_flags,
        "memo_bullets": report.memo_bullets,
        "prohibited_actions_detected": report.prohibited_actions_detected,
        "safety_disclaimer": report.safety_disclaimer,
    }


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def write_news_evidence_layer_report(
    report: NewsEvidenceLayerReport,
    base_dir: str | Path = "outputs",
) -> dict[str, str]:
    """
    Write the news evidence layer report to LATEST namespace.

    Sanitizes the JSON payload and rendered Markdown.  Validates both before
    writing.  Raises UnsafeNewsEvidenceArtifactError if any prohibited phrase
    remains after sanitization.
    """
    base = Path(base_dir)

    payload = _report_to_dict(report)
    payload = sanitize_nested_news_evidence_payload(payload)
    payload_violations = validate_news_evidence_safety(payload)
    if payload_violations:
        raise UnsafeNewsEvidenceArtifactError(
            f"Refusing to write {_JSON_NAME!r}: prohibited language remains: "
            f"{payload_violations!r}"
        )

    md_content = sanitize_news_evidence_text(render_news_evidence_markdown(report))
    md_violations = validate_news_evidence_safety(md_content)
    if md_violations:
        raise UnsafeNewsEvidenceArtifactError(
            f"Refusing to write {_MD_NAME!r}: prohibited language remains: "
            f"{md_violations!r}"
        )

    json_path = safe_write_json(
        OutputNamespace.LATEST, _JSON_NAME, payload, base_dir=base,
    )
    md_path = safe_write_text(
        OutputNamespace.LATEST, _MD_NAME, md_content, base_dir=base,
    )
    return {
        "news_evidence_layer_json": str(json_path),
        "news_evidence_layer_md": str(md_path),
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_news_evidence_layer(
    base_dir: str | Path = "outputs",
    write_files: bool = True,
) -> dict[str, Any]:
    """
    Load inputs, build report, optionally write artifacts.

    Returns a result dict with counts and artifact paths.  Catches
    UnsafeNewsEvidenceArtifactError and records ``blocked_unsafe_write``
    without crashing.
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    base = Path(base_dir)
    result: dict[str, Any] = {
        "generated_at": generated_at,
        "observe_only": _OBSERVE_ONLY,
        "no_trade": _NO_TRADE,
        "not_recommendation": _NOT_RECOMMENDATION,
        "no_decision_override": _NO_DECISION_OVERRIDE,
        "no_score_mutation": _NO_SCORE_MUTATION,
        "no_allocation_mutation": _NO_ALLOCATION_MUTATION,
        "no_watchlist_mutation": _NO_WATCHLIST_MUTATION,
        "influence_cap": _INFLUENCE_CAP,
        "artifacts": {},
    }

    try:
        inputs = load_all_inputs(base)
        report = build_news_evidence_layer_report(inputs, base)
        result["data_available"] = report.data_available
        result["ticker_context_count"] = len(report.ticker_contexts)
        result["decision_context_count"] = len(report.decision_contexts)
        result["risk_evidence_count"] = len(report.risk_evidence)
        result["catalyst_evidence_count"] = len(report.catalyst_evidence)
        result["safety_violations"] = report.prohibited_actions_detected
        if write_files:
            try:
                paths = write_news_evidence_layer_report(report, base)
                result["artifacts"] = paths
            except UnsafeNewsEvidenceArtifactError as exc:
                logger.error("Blocked unsafe news evidence artifact: %s", exc)
                result["blocked_unsafe_write"] = str(exc)
    except Exception as exc:
        logger.error("run_news_evidence_layer failed: %s", exc, exc_info=True)
        result["error"] = str(exc)

    return result
