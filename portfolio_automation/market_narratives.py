"""
Market Narratives Layer
========================

Observe-only layer that turns existing structured artifacts into daily, weekly,
and monthly operator-readable market narratives.

Narratives explain what changed, what themes persisted, what risks/catalysts
matter, and how current news/discovery context relates to the portfolio.
They do NOT create recommendations, change official decision logic, or mutate
any official portfolio/watchlist/allocation state.

Safety invariants (hardcoded):
  - observe_only: true
  - no_trade: true
  - not_recommendation: true
  - No BUY/SELL/HOLD/PROMOTED/VALIDATED/ACTIONABLE language in generated text.
  - No writes to POLICY, PORTFOLIO, or SANDBOX namespaces.
  - Writes only to OutputNamespace.LATEST.
  - No LLM/AI calls — deterministic rules only.
  - All input artifacts are read-only.

AI support: deferred.  All narrative generation is deterministic.

Public API:
  build_market_narrative_report(period, inputs, base_dir)
  write_market_narrative_report(period, report, base_dir)
  run_market_narratives(base_dir, periods, write_files)
  validate_narrative_safety(text)
  render_market_narrative_markdown(report)
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
_SOURCE_LABEL = "market_narratives_layer"

_SAFETY_DISCLAIMER = (
    "This narrative is observe-only research context. "
    "It is not a buy/sell/hold recommendation and does not modify official "
    "portfolio, watchlist, allocation, scoring, or recommendation state. "
    "Discovery research context is sandbox-only and not actionable."
)

# Phrases that must not appear as instructions in narrative text.
# The validator checks for these to prevent narrative sections from sounding
# like trading commands.
_PROHIBITED_INSTRUCTION_PATTERNS: list[str] = [
    "buy now",
    "sell now",
    "hold now",
    "trim now",
    "add shares",
    "reduce shares",
    "rebalance now",
    "execute trade",
    "execute order",
    "execute now",
    "place trade",
    "place order",
    "promote candidate",
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

# Discovery status labels safe for narrative use
_SAFE_DISCOVERY_LABELS: dict[str, str] = {
    "research_supported": "news-supported research candidate",
    "research_caution": "risk-heavy research candidate",
    "research_neutral": "mixed-signal research candidate",
    "no_news": "needs more corroboration",
    "watch": "sandbox watch candidate",
    "discovered": "early-stage research candidate",
    "rejected": "rejected from sandbox",
    "news_only": "news-only signal — needs corroboration",
}

# Artifact filenames per period
_ARTIFACT_NAMES: dict[str, dict[str, str]] = {
    "daily":   {"json": "market_narrative_daily.json",   "md": "market_narrative_daily.md"},
    "weekly":  {"json": "market_narrative_weekly.json",  "md": "market_narrative_weekly.md"},
    "monthly": {"json": "market_narrative_monthly.json", "md": "market_narrative_monthly.md"},
}

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class NarrativeInputSummary:
    """Records which input artifacts were available and used."""
    artifact: str
    available: bool
    summary: str = ""


@dataclass
class NarrativeTheme:
    """A market theme identified across multiple inputs."""
    theme: str
    signal_count: int
    sources: list[str]
    description: str


@dataclass
class NarrativeRisk:
    """A risk flag surfaced from news or discovery evidence."""
    label: str
    tickers: list[str]
    sources: list[str]
    description: str


@dataclass
class NarrativeCatalyst:
    """A positive catalyst flag from news or discovery evidence."""
    label: str
    tickers: list[str]
    sources: list[str]
    description: str


@dataclass
class NarrativeDiscoveryContext:
    """Sandbox-only discovery research context for the narrative."""
    candidate_count: int
    watch_count: int
    news_supported: list[str]
    risk_heavy: list[str]
    news_only: list[str]
    top_themes: list[str]
    disclaimer: str = (
        "Discovery research is sandbox-only. "
        "No candidates are promoted or recommended."
    )


@dataclass
class MarketNarrativeReport:
    """Structured market narrative for one period."""
    narrative_period: str
    generated_at: str
    observe_only: bool = True
    no_trade: bool = True
    not_recommendation: bool = True
    source: str = _SOURCE_LABEL

    top_headline: str = ""
    executive_summary: str = ""

    key_themes: list[NarrativeTheme] = field(default_factory=list)
    portfolio_context: str = ""
    discovery_context: NarrativeDiscoveryContext | None = None

    risks_to_watch: list[NarrativeRisk] = field(default_factory=list)
    catalysts_to_watch: list[NarrativeCatalyst] = field(default_factory=list)

    data_quality_notes: list[str] = field(default_factory=list)
    confidence_notes: list[str] = field(default_factory=list)
    operator_watchlist: list[str] = field(default_factory=list)

    inputs_used: list[NarrativeInputSummary] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)
    data_available: bool = False

    prohibited_actions_detected: list[str] = field(default_factory=list)
    safety_disclaimer: str = _SAFETY_DISCLAIMER


# ---------------------------------------------------------------------------
# Input loading helpers
# ---------------------------------------------------------------------------

def _safe_load_json(path: Path) -> Any:
    """Load JSON from path; return None on missing, empty, or malformed."""
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


def _safe_load_text(path: Path) -> str:
    """Load text from path; return empty string on missing."""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Failed to load text %s: %s", path, exc)
        return ""


def _safe_load_jsonl(path: Path) -> list[dict]:
    """Load JSONL; return empty list on missing or malformed lines."""
    if not path.exists():
        return []
    results: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    results.append(obj)
            except Exception:
                pass
    except Exception as exc:
        logger.warning("Failed to load JSONL %s: %s", path, exc)
    return results


def _load_input(path: Path, label: str) -> tuple[Any, NarrativeInputSummary]:
    """Load a JSON input and return (payload, NarrativeInputSummary)."""
    payload = _safe_load_json(path)
    if payload is None:
        return None, NarrativeInputSummary(artifact=label, available=False)
    if not isinstance(payload, dict):
        return None, NarrativeInputSummary(
            artifact=label, available=False, summary="non-object JSON"
        )
    return payload, NarrativeInputSummary(artifact=label, available=True)


def load_all_inputs(base_dir: str | Path = "outputs") -> dict[str, Any]:
    """
    Load all narrative input artifacts safely.

    Returns a dict keyed by artifact label with payloads and availability flags.
    Missing, malformed, or non-object inputs degrade silently.
    """
    base = Path(base_dir)

    def _latest(name: str) -> Path:
        return get_output_path(OutputNamespace.LATEST, name, base_dir=base)

    def _sandbox(name: str) -> Path:
        return get_output_path(OutputNamespace.SANDBOX, name, base_dir=base)

    paths: dict[str, Path] = {
        "news_intelligence":         _latest("news_intelligence.json"),
        "decision_plan":             _latest("decision_plan.json"),
        "system_decision_summary":   _latest("system_decision_summary.json"),
        "data_quality_report":       _latest("data_quality_report.json"),
        "confidence_calibration":    _latest("confidence_calibration.json"),
        "ai_budget_summary":         _latest("ai_budget_summary.json"),
        "decision_explanations":     _latest("decision_explanations.json"),
        "news_enriched_candidates":  _sandbox("discovery/news_enriched_candidates.json"),
        "emerging_candidates":       _sandbox("discovery/emerging_candidates.json"),
        "rejected_candidates":       _sandbox("discovery/rejected_candidates.json"),
        "replay_results":            _sandbox("discovery/replay_results.json"),
    }

    loaded: dict[str, Any] = {}
    for label, path in paths.items():
        payload, summary = _load_input(path, label)
        loaded[label] = {"payload": payload, "summary": summary}

    # Approval decisions JSONL (optional)
    approval_path = _sandbox("discovery/approval_decisions.jsonl")
    approvals = _safe_load_jsonl(approval_path)
    loaded["approval_decisions"] = {
        "payload": approvals if approvals else None,
        "summary": NarrativeInputSummary(
            artifact="approval_decisions",
            available=bool(approvals),
        ),
    }

    return loaded


# ---------------------------------------------------------------------------
# Safety validator
# ---------------------------------------------------------------------------

def validate_narrative_safety(text: str) -> list[str]:
    """
    Check narrative text for prohibited instruction patterns.

    Returns a list of detected violations (empty = clean).
    Detection is case-insensitive.  Phrases are checked as substrings.
    """
    text_lower = text.lower()
    violations: list[str] = []
    for pattern in _PROHIBITED_INSTRUCTION_PATTERNS:
        if pattern in text_lower:
            violations.append(pattern)
    return violations


def _sanitize_text(text: str) -> str:
    """Strip or neutralize detected prohibited phrases from text."""
    lower = text.lower()
    for pattern in _PROHIBITED_INSTRUCTION_PATTERNS:
        if pattern in lower:
            idx = lower.find(pattern)
            text = text[:idx] + "[REDACTED]" + text[idx + len(pattern):]
            lower = text.lower()
    return text


# ---------------------------------------------------------------------------
# Theme / evidence extraction helpers
# ---------------------------------------------------------------------------

def _extract_themes_from_news(news_intel: dict | None) -> list[NarrativeTheme]:
    """Extract top themes from news_intelligence artifact."""
    if not isinstance(news_intel, dict):
        return []
    packets = news_intel.get("evidence_packets") or []
    if not isinstance(packets, list):
        return []

    theme_counts: dict[str, int] = {}
    theme_tickers: dict[str, list[str]] = {}
    for p in packets:
        if not isinstance(p, dict):
            continue
        ticker = str(p.get("entity_key") or "")
        for t in (p.get("themes") or []):
            if isinstance(t, str):
                theme_counts[t] = theme_counts.get(t, 0) + 1
                theme_tickers.setdefault(t, [])
                if ticker and ticker not in theme_tickers[t]:
                    theme_tickers[t].append(ticker)

    themes: list[NarrativeTheme] = []
    for theme, count in sorted(theme_counts.items(), key=lambda x: x[1], reverse=True)[:6]:
        themes.append(NarrativeTheme(
            theme=theme,
            signal_count=count,
            sources=theme_tickers.get(theme, [])[:5],
            description=f"Appeared in {count} news evidence packet(s).",
        ))
    return themes


def _extract_risks_catalysts(
    news_intel: dict | None,
    enriched: dict | None,
) -> tuple[list[NarrativeRisk], list[NarrativeCatalyst]]:
    """Extract aggregated risk and catalyst flags from news artifacts."""
    risk_map: dict[str, list[str]] = {}
    catalyst_map: dict[str, list[str]] = {}

    def _process_packets(packets: list) -> None:
        for p in packets:
            if not isinstance(p, dict):
                continue
            ticker = str(p.get("entity_key") or p.get("ticker") or "")
            for flag in (p.get("risk_flags") or []):
                if isinstance(flag, str):
                    risk_map.setdefault(flag, [])
                    if ticker and ticker not in risk_map[flag]:
                        risk_map[flag].append(ticker)
            for flag in (p.get("catalyst_flags") or []):
                if isinstance(flag, str):
                    catalyst_map.setdefault(flag, [])
                    if ticker and ticker not in catalyst_map[flag]:
                        catalyst_map[flag].append(ticker)

    if isinstance(news_intel, dict):
        _process_packets(news_intel.get("evidence_packets") or [])
    if isinstance(enriched, dict):
        _process_packets(enriched.get("enriched_candidates") or [])

    risks = [
        NarrativeRisk(
            label=flag,
            tickers=tickers[:5],
            sources=["news_intelligence"],
            description=f"Risk signal detected in news evidence for: {', '.join(tickers[:5]) or 'unknown'}",
        )
        for flag, tickers in sorted(risk_map.items(), key=lambda x: len(x[1]), reverse=True)[:5]
    ]

    catalysts = [
        NarrativeCatalyst(
            label=flag,
            tickers=tickers[:5],
            sources=["news_intelligence"],
            description=f"Catalyst signal in news evidence for: {', '.join(tickers[:5]) or 'unknown'}",
        )
        for flag, tickers in sorted(catalyst_map.items(), key=lambda x: len(x[1]), reverse=True)[:5]
    ]

    return risks, catalysts


def _build_discovery_context(
    enriched: dict | None,
    emerging: dict | None,
) -> NarrativeDiscoveryContext:
    """Build sandbox-only discovery context summary."""
    news_supported: list[str] = []
    risk_heavy: list[str] = []
    news_only: list[str] = []
    top_themes: list[str] = []
    total = 0
    watch_count = 0

    if isinstance(enriched, dict):
        candidates = enriched.get("enriched_candidates") or []
        if isinstance(candidates, list):
            total = len(candidates)
            theme_counts: dict[str, int] = {}
            for c in candidates:
                if not isinstance(c, dict):
                    continue
                ticker = str(c.get("ticker") or "")
                ctx = str(c.get("news_context") or "")
                status = str(c.get("candidate_status") or "")
                if status == "watch":
                    watch_count += 1
                if ctx == "research_supported" and ticker:
                    news_supported.append(ticker)
                elif ctx == "research_caution" and ticker:
                    risk_heavy.append(ticker)
                elif status == "news_only" and ticker:
                    news_only.append(ticker)
                for t in (c.get("matched_themes") or []):
                    if isinstance(t, str):
                        theme_counts[t] = theme_counts.get(t, 0) + 1
            top_themes = sorted(theme_counts, key=lambda x: theme_counts[x], reverse=True)[:5]

    elif isinstance(emerging, dict):
        candidates = emerging.get("candidates") or []
        if isinstance(candidates, list):
            total = len(candidates)
            for c in candidates:
                if not isinstance(c, dict):
                    continue
                if str(c.get("status") or "") == "watch":
                    watch_count += 1

    return NarrativeDiscoveryContext(
        candidate_count=total,
        watch_count=watch_count,
        news_supported=news_supported[:8],
        risk_heavy=risk_heavy[:8],
        news_only=news_only[:8],
        top_themes=top_themes,
    )


def _extract_data_quality_notes(dq: dict | None) -> list[str]:
    """Summarize data quality issues into narrative notes."""
    if not isinstance(dq, dict):
        return []
    notes: list[str] = []
    issues = dq.get("issues") or []
    if isinstance(issues, list) and issues:
        # Count by severity
        sev: dict[str, int] = {}
        for issue in issues:
            if isinstance(issue, dict):
                s = str(issue.get("severity") or "unknown")
                sev[s] = sev.get(s, 0) + 1
        parts = [f"{v} {k}" for k, v in sev.items() if v]
        notes.append(f"Data quality issues detected: {', '.join(parts)}.")
    overall = dq.get("overall_health") or dq.get("health_status") or ""
    if overall:
        notes.append(f"Overall data health: {overall}.")
    return notes


def _extract_confidence_notes(cal: dict | None) -> list[str]:
    """Extract confidence calibration context."""
    if not isinstance(cal, dict):
        return []
    notes: list[str] = []
    resolved = cal.get("resolved_decisions") or cal.get("total_resolved") or 0
    if resolved:
        notes.append(f"Confidence calibration: {resolved} resolved decisions available.")
    accuracy = cal.get("overall_accuracy") or cal.get("hit_rate")
    if accuracy is not None:
        notes.append(f"Decision accuracy: {accuracy:.1%}." if isinstance(accuracy, float) else f"Decision accuracy: {accuracy}.")
    return notes


def _build_portfolio_context(
    decision_plan: dict | None,
    sys_summary: dict | None,
    period: str,
) -> str:
    """Build a brief portfolio context string from decision plan artifacts."""
    parts: list[str] = []

    if isinstance(decision_plan, dict):
        decisions = decision_plan.get("decisions") or []
        if isinstance(decisions, list) and decisions:
            top = decisions[:3]
            tickers = [str(d.get("ticker") or d.get("symbol") or "") for d in top if isinstance(d, dict)]
            tickers = [t for t in tickers if t]
            if tickers:
                parts.append(f"Decision plan covers {len(decisions)} position(s); top: {', '.join(tickers)}.")

    if isinstance(sys_summary, dict):
        health = sys_summary.get("system_health") or sys_summary.get("overall_health") or ""
        if health:
            parts.append(f"System health: {health}.")
        run_mode = sys_summary.get("run_mode") or ""
        if run_mode:
            parts.append(f"Run mode: {run_mode}.")

    if not parts:
        return "No decision plan data available for this period."

    if period == "daily":
        return " ".join(parts)
    if period == "weekly":
        return "Weekly view — " + " ".join(parts)
    return "Monthly view — " + " ".join(parts)


# ---------------------------------------------------------------------------
# Headline and summary builders
# ---------------------------------------------------------------------------

def _build_daily_headline(
    themes: list[NarrativeTheme],
    risks: list[NarrativeRisk],
    catalysts: list[NarrativeCatalyst],
    data_available: bool,
) -> str:
    if not data_available:
        return "Daily market narrative — no structured input data available."
    if themes:
        top = themes[0].theme.replace("_", " ").title()
        if catalysts and not risks:
            return f"Markets show {top} momentum with notable catalyst signals."
        if risks and not catalysts:
            return f"Risk signals detected; top theme: {top}."
        if risks and catalysts:
            return f"Mixed signals — {top} theme active with both risk and catalyst flags."
        return f"Top theme today: {top} ({themes[0].signal_count} signal(s))."
    return "Daily market narrative — limited news signal today."


def _build_weekly_headline(
    themes: list[NarrativeTheme],
    discovery: NarrativeDiscoveryContext,
    data_available: bool,
) -> str:
    if not data_available:
        return "Weekly market narrative — no structured input data available."
    if themes:
        persistent = [t.theme.replace("_", " ").title() for t in themes[:2]]
        parts = " and ".join(persistent)
        return f"Weekly read: persistent themes — {parts}."
    if discovery.candidate_count:
        return f"Weekly read: {discovery.candidate_count} discovery candidate(s) in research pipeline."
    return "Weekly market narrative — limited accumulated signal."


def _build_monthly_headline(
    themes: list[NarrativeTheme],
    data_available: bool,
) -> str:
    if not data_available:
        return "Monthly market narrative — no structured input data available."
    if themes:
        top3 = [t.theme.replace("_", " ").title() for t in themes[:3]]
        return f"Monthly view: dominant themes — {', '.join(top3)}."
    return "Monthly narrative — low news signal accumulation this period."


def _build_executive_summary(
    period: str,
    themes: list[NarrativeTheme],
    risks: list[NarrativeRisk],
    catalysts: list[NarrativeCatalyst],
    discovery: NarrativeDiscoveryContext,
    inputs_used: list[NarrativeInputSummary],
) -> str:
    available_count = sum(1 for i in inputs_used if i.available)
    theme_str = (
        ", ".join(t.theme.replace("_", " ") for t in themes[:3])
        if themes else "none identified"
    )
    risk_str = f"{len(risks)} risk signal(s)" if risks else "no major risk signals"
    catalyst_str = f"{len(catalysts)} catalyst signal(s)" if catalysts else "no major catalysts"
    disc_str = (
        f"{discovery.candidate_count} research candidate(s) in sandbox pipeline"
        if discovery.candidate_count else "no discovery candidates"
    )
    scope = {"daily": "Today's", "weekly": "This week's", "monthly": "This month's"}.get(period, "")
    return (
        f"{scope} narrative draws on {available_count} available input artifact(s). "
        f"Top themes: {theme_str}. "
        f"Risk: {risk_str}. Catalysts: {catalyst_str}. "
        f"Discovery research context: {disc_str}. "
        "This narrative is observe-only and does not constitute an investment recommendation."
    )


def _build_operator_watchlist(
    period: str,
    themes: list[NarrativeTheme],
    risks: list[NarrativeRisk],
    catalysts: list[NarrativeCatalyst],
    discovery: NarrativeDiscoveryContext,
) -> list[str]:
    """Return actionable-but-safe review items for the operator."""
    items: list[str] = []
    if risks:
        tickers = sorted({t for r in risks for t in r.tickers})[:5]
        if tickers:
            items.append(f"Review risk context for: {', '.join(tickers)}.")
    if catalysts and period in ("daily", "weekly"):
        tickers = sorted({t for c in catalysts for t in c.tickers})[:5]
        if tickers:
            items.append(f"Note catalyst signals for: {', '.join(tickers)}.")
    if discovery.risk_heavy:
        items.append(
            f"Sandbox caution candidates: {', '.join(discovery.risk_heavy[:3])} — review risk evidence."
        )
    if discovery.news_only and period in ("weekly", "monthly"):
        items.append(
            f"News-only tickers need corroboration: {', '.join(discovery.news_only[:3])}."
        )
    if period == "monthly" and discovery.candidate_count > 0:
        items.append(
            f"Monthly review: {discovery.candidate_count} sandbox candidate(s) in research pipeline."
        )
    return items


# ---------------------------------------------------------------------------
# Narrative builder
# ---------------------------------------------------------------------------

def build_market_narrative_report(
    period: str,
    inputs: dict[str, Any],
    base_dir: str | Path = "outputs",
) -> MarketNarrativeReport:
    """
    Build a structured MarketNarrativeReport for the given period.

    Parameters
    ----------
    period:
        "daily", "weekly", or "monthly".
    inputs:
        Dict returned by load_all_inputs().  Each value has "payload" and "summary".
    base_dir:
        Not used directly here; kept for API consistency.

    Returns
    -------
    MarketNarrativeReport with all safety flags hardcoded.
    """
    if period not in ("daily", "weekly", "monthly"):
        raise ValueError(f"Invalid period {period!r}. Must be daily, weekly, or monthly.")

    generated_at = datetime.now(timezone.utc).isoformat()

    # Unpack payloads
    def _get(key: str) -> Any:
        return (inputs.get(key) or {}).get("payload")

    def _summary(key: str) -> NarrativeInputSummary:
        return (inputs.get(key) or {}).get(
            "summary", NarrativeInputSummary(artifact=key, available=False)
        )

    news_intel = _get("news_intelligence")
    decision_plan = _get("decision_plan")
    sys_summary = _get("system_decision_summary")
    dq_report = _get("data_quality_report")
    cal = _get("confidence_calibration")
    enriched = _get("news_enriched_candidates")
    emerging = _get("emerging_candidates")

    all_summaries = [_summary(k) for k in inputs]
    used = [s for s in all_summaries if s.available]
    missing = [s.artifact for s in all_summaries if not s.available]
    data_available = bool(used)

    # Build components
    themes = _extract_themes_from_news(news_intel)
    risks, catalysts = _extract_risks_catalysts(news_intel, enriched)
    discovery = _build_discovery_context(enriched, emerging)
    dq_notes = _extract_data_quality_notes(dq_report)
    conf_notes = _extract_confidence_notes(cal)
    portfolio_ctx = _build_portfolio_context(decision_plan, sys_summary, period)
    operator_wl = _build_operator_watchlist(period, themes, risks, catalysts, discovery)

    # Period-specific headline
    if period == "daily":
        headline = _build_daily_headline(themes, risks, catalysts, data_available)
    elif period == "weekly":
        headline = _build_weekly_headline(themes, discovery, data_available)
    else:
        headline = _build_monthly_headline(themes, data_available)

    exec_summary = _build_executive_summary(
        period, themes, risks, catalysts, discovery, all_summaries
    )

    report = MarketNarrativeReport(
        narrative_period=period,
        generated_at=generated_at,
        top_headline=headline,
        executive_summary=exec_summary,
        key_themes=themes,
        portfolio_context=portfolio_ctx,
        discovery_context=discovery,
        risks_to_watch=risks,
        catalysts_to_watch=catalysts,
        data_quality_notes=dq_notes,
        confidence_notes=conf_notes,
        operator_watchlist=operator_wl,
        inputs_used=all_summaries,
        missing_inputs=missing,
        data_available=data_available,
        safety_disclaimer=_SAFETY_DISCLAIMER,
    )

    # Safety check on generated text
    all_text = f"{headline} {exec_summary} {portfolio_ctx}"
    violations = validate_narrative_safety(all_text)
    report.prohibited_actions_detected = violations

    return report


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def render_market_narrative_markdown(report: MarketNarrativeReport) -> str:
    """Render a MarketNarrativeReport as a Markdown string."""
    period = report.narrative_period
    p = period.title()
    lines: list[str] = []

    # Header
    if period == "daily":
        lines.append(f"# Daily Market Read")
    elif period == "weekly":
        lines.append(f"# Weekly Market Read")
    else:
        lines.append(f"# Monthly Market Read")

    lines.append("")
    lines.append(f"**Generated:** {report.generated_at}")
    lines.append(f"**Period:** {p}")
    lines.append("")
    lines.append(f"> **{report.safety_disclaimer}**")
    lines.append("")

    # Headline
    lines.append(f"## {p} Headline")
    lines.append("")
    lines.append(report.top_headline)
    lines.append("")

    # Executive summary / What Changed
    section_name = {
        "daily": "What Changed",
        "weekly": "Persistent Themes Overview",
        "monthly": "Regime / Theme Context",
    }.get(period, "Summary")
    lines.append(f"## {section_name}")
    lines.append("")
    lines.append(report.executive_summary)
    lines.append("")

    # Key themes
    if report.key_themes:
        lines.append("## Themes")
        lines.append("")
        for t in report.key_themes[:6]:
            theme_label = t.theme.replace("_", " ").title()
            tickers = f" ({', '.join(t.sources[:3])})" if t.sources else ""
            lines.append(f"- **{theme_label}**{tickers}: {t.description}")
        lines.append("")

    # Portfolio context
    lines.append("## Portfolio Context")
    lines.append("")
    lines.append(report.portfolio_context)
    lines.append("")

    # Discovery research context
    disc = report.discovery_context
    if disc is not None:
        lines.append("## Discovery Research Context _(Sandbox Only)_")
        lines.append("")
        lines.append(f"> {disc.disclaimer}")
        lines.append("")
        lines.append(f"- Research candidates in pipeline: {disc.candidate_count}")
        if disc.news_supported:
            lines.append(f"- News-supported: {', '.join(disc.news_supported[:5])}")
        if disc.risk_heavy:
            lines.append(f"- Risk-heavy context: {', '.join(disc.risk_heavy[:5])}")
        if disc.news_only:
            lines.append(f"- News-only (needs corroboration): {', '.join(disc.news_only[:5])}")
        if disc.top_themes:
            lines.append(f"- Top sandbox themes: {', '.join(disc.top_themes[:5])}")
        lines.append("")

    # Risks
    if report.risks_to_watch:
        lines.append("## Risks to Watch")
        lines.append("")
        for r in report.risks_to_watch[:5]:
            tickers = f" [{', '.join(r.tickers[:3])}]" if r.tickers else ""
            lines.append(f"- **{r.label}**{tickers}: {r.description}")
        lines.append("")

    # Catalysts
    if report.catalysts_to_watch:
        lines.append("## Catalysts to Watch")
        lines.append("")
        for c in report.catalysts_to_watch[:5]:
            tickers = f" [{', '.join(c.tickers[:3])}]" if c.tickers else ""
            lines.append(f"- **{c.label}**{tickers}: {c.description}")
        lines.append("")

    # Data quality
    if report.data_quality_notes:
        lines.append("## Data Quality Notes")
        lines.append("")
        for note in report.data_quality_notes:
            lines.append(f"- {note}")
        lines.append("")

    # Confidence notes
    if report.confidence_notes:
        lines.append("## Confidence / Calibration Notes")
        lines.append("")
        for note in report.confidence_notes:
            lines.append(f"- {note}")
        lines.append("")

    # Operator review queue
    if report.operator_watchlist:
        queue_name = {
            "daily": "What to Watch Next",
            "weekly": "Operator Review Queue",
            "monthly": "Review Areas",
        }.get(period, "Operator Notes")
        lines.append(f"## {queue_name}")
        lines.append("")
        for item in report.operator_watchlist:
            lines.append(f"- {item}")
        lines.append("")

    # Inputs / coverage
    available_count = sum(1 for i in report.inputs_used if i.available)
    total_count = len(report.inputs_used)
    lines.append("## Input Coverage")
    lines.append("")
    lines.append(f"- Inputs available: {available_count} / {total_count}")
    if report.missing_inputs:
        lines.append(f"- Missing inputs: {', '.join(report.missing_inputs[:8])}")
    lines.append("")

    # Safety footer
    lines.append("---")
    lines.append(f"*Source: {report.source}*")
    lines.append(
        f"*observe_only: {report.observe_only} | "
        f"no_trade: {report.no_trade} | "
        f"not_recommendation: {report.not_recommendation}*"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report → dict serializer
# ---------------------------------------------------------------------------

def _report_to_dict(report: MarketNarrativeReport) -> dict[str, Any]:
    """Serialize a MarketNarrativeReport to a JSON-safe dict."""

    def _theme_d(t: NarrativeTheme) -> dict:
        return {
            "theme": t.theme,
            "signal_count": t.signal_count,
            "sources": t.sources,
            "description": t.description,
        }

    def _risk_d(r: NarrativeRisk) -> dict:
        return {
            "label": r.label,
            "tickers": r.tickers,
            "sources": r.sources,
            "description": r.description,
        }

    def _cat_d(c: NarrativeCatalyst) -> dict:
        return {
            "label": c.label,
            "tickers": c.tickers,
            "sources": c.sources,
            "description": c.description,
        }

    def _inp_d(i: NarrativeInputSummary) -> dict:
        return {"artifact": i.artifact, "available": i.available, "summary": i.summary}

    disc = report.discovery_context
    disc_d: dict | None = None
    if disc is not None:
        disc_d = {
            "candidate_count": disc.candidate_count,
            "watch_count": disc.watch_count,
            "news_supported": disc.news_supported,
            "risk_heavy": disc.risk_heavy,
            "news_only": disc.news_only,
            "top_themes": disc.top_themes,
            "disclaimer": disc.disclaimer,
        }

    return {
        "narrative_period": report.narrative_period,
        "generated_at": report.generated_at,
        "observe_only": report.observe_only,
        "no_trade": report.no_trade,
        "not_recommendation": report.not_recommendation,
        "source": report.source,
        "data_available": report.data_available,
        "top_headline": report.top_headline,
        "executive_summary": report.executive_summary,
        "key_themes": [_theme_d(t) for t in report.key_themes],
        "portfolio_context": report.portfolio_context,
        "discovery_context": disc_d,
        "risks_to_watch": [_risk_d(r) for r in report.risks_to_watch],
        "catalysts_to_watch": [_cat_d(c) for c in report.catalysts_to_watch],
        "data_quality_notes": report.data_quality_notes,
        "confidence_notes": report.confidence_notes,
        "operator_watchlist": report.operator_watchlist,
        "inputs_used": [_inp_d(i) for i in report.inputs_used],
        "missing_inputs": report.missing_inputs,
        "prohibited_actions_detected": report.prohibited_actions_detected,
        "safety_disclaimer": report.safety_disclaimer,
    }


# ---------------------------------------------------------------------------
# Artifact writer
# ---------------------------------------------------------------------------

def write_market_narrative_report(
    period: str,
    report: MarketNarrativeReport,
    base_dir: str | Path = "outputs",
) -> dict[str, str]:
    """
    Write a MarketNarrativeReport to LATEST namespace artifacts.

    Writes:
      outputs/latest/market_narrative_{period}.json
      outputs/latest/market_narrative_{period}.md

    Returns dict with artifact path strings.
    Raises ValueError for unknown period.
    """
    if period not in _ARTIFACT_NAMES:
        raise ValueError(f"Unknown period {period!r}")

    base = Path(base_dir)
    names = _ARTIFACT_NAMES[period]

    json_path = safe_write_json(
        OutputNamespace.LATEST,
        names["json"],
        _report_to_dict(report),
        base_dir=base,
    )
    md_content = render_market_narrative_markdown(report)
    md_path = safe_write_text(
        OutputNamespace.LATEST,
        names["md"],
        md_content,
        base_dir=base,
    )
    return {
        f"market_narrative_{period}_json": str(json_path),
        f"market_narrative_{period}_md": str(md_path),
    }


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def run_market_narratives(
    base_dir: str | Path = "outputs",
    periods: list[str] | None = None,
    write_files: bool = True,
) -> dict[str, Any]:
    """
    Orchestrate narrative generation for one or more periods.

    Parameters
    ----------
    base_dir:
        Output root directory (parent of outputs/).
    periods:
        List of "daily", "weekly", "monthly".  Defaults to ["daily"].
    write_files:
        If False, skip file writes (dry-run / test mode).

    Returns a summary dict with per-period results and artifact paths.
    On any per-period error, that period returns a safe degraded state.
    """
    _periods = periods if periods is not None else ["daily"]
    generated_at = datetime.now(timezone.utc).isoformat()
    base = Path(base_dir)

    inputs = load_all_inputs(base)

    results: dict[str, Any] = {
        "generated_at": generated_at,
        "periods_requested": _periods,
        "observe_only": _OBSERVE_ONLY,
        "no_trade": _NO_TRADE,
        "not_recommendation": _NOT_RECOMMENDATION,
        "artifacts": {},
    }

    for period in _periods:
        try:
            report = build_market_narrative_report(period, inputs, base)
            period_result: dict[str, Any] = {
                "data_available": report.data_available,
                "themes_found": len(report.key_themes),
                "risks_found": len(report.risks_to_watch),
                "catalysts_found": len(report.catalysts_to_watch),
                "safety_violations": report.prohibited_actions_detected,
                "artifacts": {},
            }
            if write_files:
                artifact_paths = write_market_narrative_report(period, report, base)
                period_result["artifacts"] = artifact_paths
                results["artifacts"].update(artifact_paths)
            results[period] = period_result
        except Exception as exc:
            logger.error("Narrative generation failed for period %r: %s", period, exc, exc_info=True)
            results[period] = {"error": str(exc)}

    return results
