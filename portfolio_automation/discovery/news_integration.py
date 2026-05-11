"""
Discovery News Integration
===========================

Enriches sandbox discovery candidates with structured news evidence from the
FMP News Intelligence layer.  Sandbox-only, observe-only, rules-first.

Safety invariants (hardcoded):
  - observe_only: true
  - no_trade: true
  - not_recommendation: true
  - discovery_only: true
  - No BUY/SELL/HOLD/PROMOTED/VALIDATED/ACTIONABLE statuses.
  - No official portfolio, watchlist, allocation, or recommendation mutation.
  - No discovery candidate promotion.
  - Writes only to OutputNamespace.SANDBOX.
  - Reads outputs/latest/news_intelligence.json as read-only input only.
  - No LLM/AI calls — deterministic rules only.

Run mode governance:
  Only DISCOVERY and BACKTEST modes may write sandbox outputs.
  All other modes return results without writing (equivalent to dry_run=True).

Public API:
  load_news_intelligence(base_dir)
  load_news_candidate_evidence(base_dir)
  load_emerging_candidates(base_dir)
  load_rejected_candidates(base_dir)
  match_evidence_to_candidates(evidence_packets, candidates)
  enrich_candidates(candidates, matched_evidence, all_evidence_packets)
  build_integration_summary(enriched, run_mode, generated_at)
  write_news_integration_artifacts(base_dir, enriched, summary_md, run_mode, run_id)
  run_discovery_news_integration(base_dir, run_mode, run_id, dry_run)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
    get_output_path,
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
# Constants
# ---------------------------------------------------------------------------

_OBSERVE_ONLY = True
_NO_TRADE = True
_NOT_RECOMMENDATION = True
_DISCOVERY_ONLY = True
_SOURCE_LABEL = "discovery_news_integration"

_DISCLAIMER = (
    "Discovery news integration is sandbox research only. "
    "It is not a buy/sell recommendation and does not modify official "
    "portfolio, watchlist, allocation, or recommendation state. "
    "Discovery candidates are not promoted by this layer."
)

# Sandbox artifact paths (relative to OutputNamespace.SANDBOX)
_ENRICHED_PATH = "discovery/news_enriched_candidates.json"
_SUMMARY_MD_PATH = "discovery/news_integration_summary.md"

# Input paths (relative to respective namespaces)
_NEWS_INTELLIGENCE_PATH = "news_intelligence.json"          # LATEST
_NEWS_CANDIDATE_EVIDENCE_PATH = "discovery/news_candidate_evidence.json"  # SANDBOX
_EMERGING_CANDIDATES_PATH = "discovery/emerging_candidates.json"          # SANDBOX
_REJECTED_CANDIDATES_PATH = "discovery/rejected_candidates.json"          # SANDBOX

# Forbidden statuses — never emitted
_FORBIDDEN_STATUSES: frozenset[str] = frozenset({
    "PROMOTED", "VALIDATED", "ACTIONABLE", "BUY", "SELL",
    "promoted", "validated", "actionable", "buy", "sell",
})

# Modes that are allowed to write sandbox artifacts
_SANDBOX_WRITE_MODES: frozenset[RunMode] = frozenset({
    RunMode.DISCOVERY,
    RunMode.BACKTEST,
})

# Max headlines to include per enriched candidate
_MAX_HEADLINES = 5
# Max risk/catalyst flags per candidate
_MAX_FLAGS = 5

# ---------------------------------------------------------------------------
# Data loading helpers — all graceful on missing/malformed input
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


def load_news_intelligence(base_dir: str | Path = "outputs") -> dict[str, Any]:
    """
    Load outputs/latest/news_intelligence.json.

    Returns empty dict with available=False on missing/malformed.
    Treats artifact as read-only input.
    """
    path = get_output_path(OutputNamespace.LATEST, _NEWS_INTELLIGENCE_PATH, base_dir=base_dir)
    payload = _safe_load_json(path)
    if not isinstance(payload, dict):
        return {"available": False, "evidence_packets": []}
    payload["available"] = True
    return payload


def load_news_candidate_evidence(base_dir: str | Path = "outputs") -> dict[str, Any]:
    """
    Load outputs/sandbox/discovery/news_candidate_evidence.json.

    Returns empty dict with available=False on missing/malformed.
    """
    path = get_output_path(OutputNamespace.SANDBOX, _NEWS_CANDIDATE_EVIDENCE_PATH, base_dir=base_dir)
    payload = _safe_load_json(path)
    if not isinstance(payload, dict):
        return {"available": False, "evidence_packets": []}
    payload["available"] = True
    return payload


def load_emerging_candidates(base_dir: str | Path = "outputs") -> list[dict]:
    """
    Load outputs/sandbox/discovery/emerging_candidates.json.

    Returns empty list on missing/malformed.
    """
    path = get_output_path(OutputNamespace.SANDBOX, _EMERGING_CANDIDATES_PATH, base_dir=base_dir)
    payload = _safe_load_json(path)
    if not isinstance(payload, dict):
        return []
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [c for c in candidates if isinstance(c, dict)]


def load_rejected_candidates(base_dir: str | Path = "outputs") -> list[dict]:
    """
    Load outputs/sandbox/discovery/rejected_candidates.json.

    Returns empty list on missing/malformed.
    """
    path = get_output_path(OutputNamespace.SANDBOX, _REJECTED_CANDIDATES_PATH, base_dir=base_dir)
    payload = _safe_load_json(path)
    if not isinstance(payload, dict):
        return []
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [c for c in candidates if isinstance(c, dict)]


def _extract_evidence_packets(payload: dict[str, Any]) -> list[dict]:
    """Pull evidence_packets list from a news intelligence payload dict."""
    packets = payload.get("evidence_packets")
    if not isinstance(packets, list):
        return []
    return [p for p in packets if isinstance(p, dict)]


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

def _normalize_ticker(ticker: Any) -> str:
    if not ticker:
        return ""
    return str(ticker).upper().strip()


def match_evidence_to_candidates(
    evidence_packets: list[dict],
    candidates: list[dict],
) -> dict[str, list[dict]]:
    """
    Match evidence packets to candidates by ticker.

    Returns a dict mapping candidate ticker → list of matching evidence packets.
    Evidence packets may match multiple candidates.  Matching is by primary
    entity_key and related_tickers fields.

    Parameters
    ----------
    evidence_packets:
        List of evidence packet dicts (from news_intelligence.json or
        news_candidate_evidence.json).
    candidates:
        List of discovery candidate dicts.

    Returns
    -------
    Mapping: ticker (uppercase) → [evidence packet dicts]
    """
    # Build index: ticker → list of packets
    evidence_index: dict[str, list[dict]] = {}
    for packet in evidence_packets:
        if not isinstance(packet, dict):
            continue
        key = _normalize_ticker(packet.get("entity_key"))
        if key:
            evidence_index.setdefault(key, []).append(packet)
        for rel in (packet.get("related_tickers") or []):
            rk = _normalize_ticker(rel)
            if rk and rk != key:
                evidence_index.setdefault(rk, []).append(packet)

    matches: dict[str, list[dict]] = {}
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        ticker = _normalize_ticker(cand.get("ticker"))
        if not ticker:
            continue
        matching = evidence_index.get(ticker, [])
        matches[ticker] = matching

    return matches


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

def _compute_news_relevance_score(packets: list[dict]) -> float:
    """
    Simple deterministic relevance score based on article count and source diversity.

    Score = min(1.0, (article_count_weight + source_diversity_weight) / 2)
    """
    if not packets:
        return 0.0
    total_articles = sum(p.get("article_count", 0) for p in packets)
    unique_sources: set[str] = set()
    for p in packets:
        count = p.get("source_count", 0)
        unique_sources.add(str(count))  # proxy; we don't have source names here
    # Simple heuristic: scale log of articles, cap at 1.0
    import math
    article_score = min(1.0, math.log1p(total_articles) / math.log1p(20))
    source_score = min(1.0, len(packets) / 5.0)
    return round((article_score + source_score) / 2, 3)


def _compute_corroboration_news_score(packets: list[dict]) -> float:
    """
    Bonus corroboration score from news diversity.

    Returns 0.0–1.0 based on distinct sources across packets.
    """
    if not packets:
        return 0.0
    import math
    source_counts = sum(p.get("source_count", 0) for p in packets)
    return round(min(1.0, math.log1p(source_counts) / math.log1p(10)), 3)


def _collect_flags(packets: list[dict], key: str, max_items: int) -> list[str]:
    """Aggregate flag strings across packets, deduplicated, capped."""
    seen: set[str] = set()
    result: list[str] = []
    for p in packets:
        for flag in (p.get(key) or []):
            if isinstance(flag, str) and flag not in seen:
                seen.add(flag)
                result.append(flag)
                if len(result) >= max_items:
                    return result
    return result


def _collect_themes(packets: list[dict]) -> list[str]:
    """Aggregate and deduplicate themes across packets."""
    seen: set[str] = set()
    result: list[str] = []
    for p in packets:
        for t in (p.get("themes") or []):
            if isinstance(t, str) and t not in seen:
                seen.add(t)
                result.append(t)
    return result[:8]


def _collect_headlines(packets: list[dict], max_headlines: int) -> list[str]:
    """Collect top article headlines from packets."""
    headlines: list[str] = []
    for p in packets:
        for ref in (p.get("article_refs") or []):
            if isinstance(ref, dict):
                title = str(ref.get("title") or "").strip()
                if title and title not in headlines:
                    headlines.append(title)
                    if len(headlines) >= max_headlines:
                        return headlines
        for bullet in (p.get("summary_bullets") or []):
            if isinstance(bullet, str) and bullet.strip() and bullet not in headlines:
                headlines.append(bullet.strip())
                if len(headlines) >= max_headlines:
                    return headlines
    return headlines


def _classify_news_context(
    risk_flags: list[str],
    catalyst_flags: list[str],
    matched_count: int,
) -> str:
    """
    Classify the overall news context for a candidate.

    Returns: "research_caution" | "research_supported" | "research_neutral" | "no_news"
    Does NOT return PROMOTED/VALIDATED/ACTIONABLE/BUY/SELL.
    """
    if matched_count == 0:
        return "no_news"
    if len(risk_flags) > len(catalyst_flags) and len(risk_flags) >= 2:
        return "research_caution"
    if len(catalyst_flags) > 0 and len(risk_flags) == 0:
        return "research_supported"
    if len(catalyst_flags) > len(risk_flags):
        return "research_supported"
    return "research_neutral"


def enrich_candidates(
    candidates: list[dict],
    matched_evidence: dict[str, list[dict]],
    all_evidence_packets: list[dict],
) -> list[dict]:
    """
    Build enriched candidate records by combining discovery state with news evidence.

    Parameters
    ----------
    candidates:
        List of discovery candidate dicts (from emerging or rejected artifacts).
    matched_evidence:
        Mapping: ticker → [evidence packets] from match_evidence_to_candidates().
    all_evidence_packets:
        Full evidence packet list for finding news-only tickers.

    Returns
    -------
    List of enriched candidate dicts.  All safety flags hardcoded.
    Never emits PROMOTED/VALIDATED/ACTIONABLE/BUY/SELL statuses.
    """
    # Build set of known candidate tickers
    known_tickers: set[str] = {
        _normalize_ticker(c.get("ticker")) for c in candidates if isinstance(c, dict)
    }

    enriched: list[dict] = []

    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        ticker = _normalize_ticker(cand.get("ticker"))
        if not ticker:
            continue

        status = str(cand.get("status") or "discovered").lower()
        # Hard guard: never propagate forbidden statuses
        if status in _FORBIDDEN_STATUSES:
            status = "discovered"

        packets = matched_evidence.get(ticker, [])
        risk_flags = _collect_flags(packets, "risk_flags", _MAX_FLAGS)
        catalyst_flags = _collect_flags(packets, "catalyst_flags", _MAX_FLAGS)
        themes = _collect_themes(packets)
        headlines = _collect_headlines(packets, _MAX_HEADLINES)
        news_relevance = _compute_news_relevance_score(packets)
        corroboration_news = _compute_corroboration_news_score(packets)
        news_context = _classify_news_context(risk_flags, catalyst_flags, len(packets))
        source_diversity = sum(p.get("source_count", 0) for p in packets)

        enriched.append({
            "ticker": ticker,
            "candidate_status": status,
            "discovery_only": _DISCOVERY_ONLY,
            "observe_only": _OBSERVE_ONLY,
            "no_trade": _NO_TRADE,
            "not_recommendation": _NOT_RECOMMENDATION,
            "matched_news_count": sum(p.get("article_count", 0) for p in packets),
            "matched_evidence_packets": len(packets),
            "source_diversity": source_diversity,
            "matched_themes": themes,
            "catalyst_flags": catalyst_flags,
            "risk_flags": risk_flags,
            "news_relevance_score": news_relevance,
            "corroboration_news_score": corroboration_news,
            "news_context": news_context,
            "latest_news_headlines": headlines,
            "integration_reason": (
                f"Matched {len(packets)} news evidence packet(s) "
                f"with {source_diversity} source(s)."
                if packets else "No matching news evidence found."
            ),
            "safety_disclaimer": _DISCLAIMER,
            # Preserve original candidate fields for traceability
            "original_score": cand.get("score"),
            "original_mention_count": cand.get("mention_count"),
            "original_corroboration_score": cand.get("corroboration_score"),
            "first_seen": cand.get("first_seen"),
            "last_seen": cand.get("last_seen"),
        })

    # Append news-only tickers (in evidence but not in any candidate)
    for packet in all_evidence_packets:
        if not isinstance(packet, dict):
            continue
        ticker = _normalize_ticker(packet.get("entity_key"))
        if not ticker or ticker in known_tickers:
            continue
        # Only include sandbox_discovery_research lane items
        if packet.get("evidence_lane") != "sandbox_discovery_research":
            continue
        risk_flags = list(packet.get("risk_flags") or [])[:_MAX_FLAGS]
        catalyst_flags = list(packet.get("catalyst_flags") or [])[:_MAX_FLAGS]
        themes = list(packet.get("themes") or [])[:8]
        headlines = _collect_headlines([packet], _MAX_HEADLINES)
        news_context = _classify_news_context(risk_flags, catalyst_flags, 1)
        enriched.append({
            "ticker": ticker,
            "candidate_status": "news_only",  # not yet a discovery candidate
            "discovery_only": _DISCOVERY_ONLY,
            "observe_only": _OBSERVE_ONLY,
            "no_trade": _NO_TRADE,
            "not_recommendation": _NOT_RECOMMENDATION,
            "matched_news_count": packet.get("article_count", 0),
            "matched_evidence_packets": 1,
            "source_diversity": packet.get("source_count", 0),
            "matched_themes": themes,
            "catalyst_flags": catalyst_flags,
            "risk_flags": risk_flags,
            "news_relevance_score": _compute_news_relevance_score([packet]),
            "corroboration_news_score": _compute_corroboration_news_score([packet]),
            "news_context": news_context,
            "latest_news_headlines": headlines,
            "integration_reason": (
                "News-only ticker: appeared in news evidence but has no "
                "existing discovery candidate record. Needs corroboration."
            ),
            "safety_disclaimer": _DISCLAIMER,
            "original_score": None,
            "original_mention_count": None,
            "original_corroboration_score": None,
            "first_seen": None,
            "last_seen": None,
        })
        known_tickers.add(ticker)

    return enriched


# ---------------------------------------------------------------------------
# Summary markdown builder
# ---------------------------------------------------------------------------

def build_integration_summary(
    enriched: list[dict],
    run_mode: str,
    generated_at: str,
) -> str:
    """
    Build a human-readable Markdown summary of the news integration results.

    Always includes explicit sandbox-only disclaimer.
    No BUY/SELL/HOLD/PROMOTED language.
    """
    total = len(enriched)
    with_news = [e for e in enriched if e.get("matched_news_count", 0) > 0]
    caution = [e for e in enriched if e.get("news_context") == "research_caution"]
    supported = [e for e in enriched if e.get("news_context") == "research_supported"]
    news_only = [e for e in enriched if e.get("candidate_status") == "news_only"]

    lines: list[str] = []
    lines.append("# Discovery News Integration Summary")
    lines.append("")
    lines.append(f"**Generated:** {generated_at}")
    lines.append(f"**Run mode:** {run_mode}")
    lines.append(f"**Total enriched records:** {total}")
    lines.append(f"**With news evidence:** {len(with_news)}")
    lines.append("")
    lines.append(f"> **{_DISCLAIMER}**")
    lines.append("")

    if supported:
        lines.append("## News-Supported Candidates")
        lines.append("")
        lines.append("_Candidates with more catalyst signals than risk signals in recent news._")
        lines.append("_Still sandbox research only — no promotion or recommendation implied._")
        lines.append("")
        for e in supported[:10]:
            lines.append(f"### {e['ticker']}")
            lines.append(f"- Status: `{e['candidate_status']}` | News context: `{e['news_context']}`")
            lines.append(f"- Articles: {e['matched_news_count']} | Sources: {e['source_diversity']}")
            lines.append(f"- Themes: {', '.join(e['matched_themes']) if e['matched_themes'] else 'none'}")
            if e.get("catalyst_flags"):
                lines.append(f"- Catalysts: {', '.join(e['catalyst_flags'][:3])}")
            for h in e.get("latest_news_headlines", [])[:2]:
                lines.append(f"  - {h}")
            lines.append("")

    if caution:
        lines.append("## Candidates with Risk-Heavy News")
        lines.append("")
        lines.append("_Candidates with more risk signals than catalyst signals in recent news._")
        lines.append("_Context only — no change to official state._")
        lines.append("")
        for e in caution[:10]:
            lines.append(f"### {e['ticker']} _(research caution)_")
            lines.append(f"- Status: `{e['candidate_status']}`")
            lines.append(f"- Articles: {e['matched_news_count']} | Sources: {e['source_diversity']}")
            if e.get("risk_flags"):
                lines.append(f"- Risk signals: {', '.join(e['risk_flags'][:3])}")
            lines.append("")

    if news_only:
        lines.append("## News-Only Tickers (Needs Corroboration)")
        lines.append("")
        lines.append(
            "_These tickers appeared in news evidence but have no existing discovery "
            "candidate record. They require independent corroboration before any "
            "discovery consideration._"
        )
        lines.append("")
        for e in news_only[:10]:
            lines.append(f"- **{e['ticker']}**: {e['matched_news_count']} articles | "
                         f"Themes: {', '.join(e['matched_themes'][:3]) if e['matched_themes'] else 'none'}")
        lines.append("")

    lines.append("---")
    lines.append(f"*Source: {_SOURCE_LABEL}*")
    lines.append(
        f"*observe_only: {_OBSERVE_ONLY} | no_trade: {_NO_TRADE} | "
        f"not_recommendation: {_NOT_RECOMMENDATION}*"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Artifact writer
# ---------------------------------------------------------------------------

def write_news_integration_artifacts(
    base_dir: str | Path,
    enriched: list[dict],
    summary_md: str,
    run_mode: RunMode,
    run_id: str,
) -> dict[str, str]:
    """
    Write sandbox artifacts for news-enriched discovery candidates.

    Only writes if run_mode permits sandbox writes.
    Raises RunModeViolation if the mode does not allow sandbox writes.

    Returns dict of artifact path strings.
    """
    assert_can_write_namespace(run_mode, OutputNamespace.SANDBOX)

    generated_at = datetime.now(timezone.utc).isoformat()
    base = Path(base_dir)

    payload: dict[str, Any] = {
        "generated_at": generated_at,
        "run_id": run_id,
        "run_mode": run_mode.value,
        "observe_only": _OBSERVE_ONLY,
        "no_trade": _NO_TRADE,
        "not_recommendation": _NOT_RECOMMENDATION,
        "discovery_only": _DISCOVERY_ONLY,
        "source": _SOURCE_LABEL,
        "disclaimer": _DISCLAIMER,
        "total_enriched": len(enriched),
        "with_news_count": sum(1 for e in enriched if e.get("matched_news_count", 0) > 0),
        "research_caution_count": sum(1 for e in enriched if e.get("news_context") == "research_caution"),
        "research_supported_count": sum(1 for e in enriched if e.get("news_context") == "research_supported"),
        "news_only_count": sum(1 for e in enriched if e.get("candidate_status") == "news_only"),
        "enriched_candidates": enriched,
    }

    json_path = safe_write_json(
        OutputNamespace.SANDBOX,
        _ENRICHED_PATH,
        payload,
        base_dir=base,
    )
    md_path = safe_write_text(
        OutputNamespace.SANDBOX,
        _SUMMARY_MD_PATH,
        summary_md,
        base_dir=base,
    )

    return {
        "news_enriched_candidates_json": str(json_path),
        "news_integration_summary_md": str(md_path),
    }


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def run_discovery_news_integration(
    base_dir: str | Path = "outputs",
    run_mode: str | RunMode = "discovery",
    run_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Orchestrate loading, matching, enriching, and writing discovery news evidence.

    Parameters
    ----------
    base_dir:
        Output root directory (parent of outputs/).
    run_mode:
        Run mode string or RunMode enum.  Only DISCOVERY and BACKTEST may write.
        Other modes behave as dry_run=True.
    run_id:
        Optional run identifier (defaults to timestamp-based string).
    dry_run:
        If True, skip all file writes and return results only.

    Returns a summary dict with counts and artifact paths.
    On any error, returns a safe degraded state.
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    _run_id = run_id or f"{generated_at[:10]}_discovery_news_integration"
    base = Path(base_dir)

    try:
        mode = normalize_run_mode(run_mode)
    except RunModeViolation as exc:
        logger.error("Invalid run mode: %s", exc)
        return _error_result(str(exc), generated_at)

    # Modes outside DISCOVERY/BACKTEST cannot write sandbox; treat as dry_run
    can_write = validate_output_write(mode, OutputNamespace.SANDBOX) and not dry_run

    try:
        # Load inputs
        news_intel = load_news_intelligence(base)
        news_evidence = load_news_candidate_evidence(base)
        emerging = load_emerging_candidates(base)
        rejected = load_rejected_candidates(base)

        # Combine all evidence packets
        all_packets: list[dict] = []
        all_packets.extend(_extract_evidence_packets(news_intel))
        all_packets.extend(_extract_evidence_packets(news_evidence))

        # Deduplicate packets by entity_key (prefer first occurrence)
        seen_keys: set[str] = set()
        deduped_packets: list[dict] = []
        for p in all_packets:
            if not isinstance(p, dict):
                continue
            key = _normalize_ticker(p.get("entity_key"))
            if key and key not in seen_keys:
                seen_keys.add(key)
                deduped_packets.append(p)

        # All candidates combined
        all_candidates = list(emerging) + list(rejected)

        # Match and enrich
        matched = match_evidence_to_candidates(deduped_packets, all_candidates)
        enriched = enrich_candidates(all_candidates, matched, deduped_packets)

        # Build summary markdown
        summary_md = build_integration_summary(enriched, mode.value, generated_at)

        artifacts: dict[str, str] = {}
        if can_write:
            artifacts = write_news_integration_artifacts(
                base_dir=base,
                enriched=enriched,
                summary_md=summary_md,
                run_mode=mode,
                run_id=_run_id,
            )

        return {
            "generated_at": generated_at,
            "run_id": _run_id,
            "run_mode": mode.value,
            "dry_run": not can_write,
            "candidate_count": len(all_candidates),
            "evidence_packet_count": len(deduped_packets),
            "enriched_count": len(enriched),
            "with_news_count": sum(1 for e in enriched if e.get("matched_news_count", 0) > 0),
            "research_caution_count": sum(
                1 for e in enriched if e.get("news_context") == "research_caution"
            ),
            "research_supported_count": sum(
                1 for e in enriched if e.get("news_context") == "research_supported"
            ),
            "news_only_count": sum(
                1 for e in enriched if e.get("candidate_status") == "news_only"
            ),
            "artifacts": artifacts,
            "observe_only": _OBSERVE_ONLY,
            "no_trade": _NO_TRADE,
            "not_recommendation": _NOT_RECOMMENDATION,
            "discovery_only": _DISCOVERY_ONLY,
        }

    except Exception as exc:
        logger.error("run_discovery_news_integration failed: %s", exc, exc_info=True)
        return _error_result(str(exc), generated_at)


def _error_result(error: str, generated_at: str) -> dict[str, Any]:
    return {
        "error": error,
        "generated_at": generated_at,
        "observe_only": _OBSERVE_ONLY,
        "no_trade": _NO_TRADE,
        "not_recommendation": _NOT_RECOMMENDATION,
        "discovery_only": _DISCOVERY_ONLY,
    }
