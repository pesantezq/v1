"""
Discovery Engine — Sandbox Report Writer and Orchestration Entry Point.

Writes research-lane artifacts to outputs/sandbox/discovery/ ONLY.
Never writes to outputs/latest, outputs/policy, outputs/portfolio, or outputs/users.

Every artifact clearly states:
  "Discovery candidates are not buy/sell recommendations."

Run mode governance is enforced: only modes with can_write_sandbox=True
(i.e. DISCOVERY or BACKTEST) may invoke write_discovery_reports.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.discovery.news_ticker_discovery import (
    DiscoveredTicker,
    extract_tickers,
)
from portfolio_automation.discovery.event_classifier import (
    ClassificationResult,
    classify_record,
)
from portfolio_automation.discovery.candidate_promotion_engine import (
    CandidateStatus,
    DiscoveryCandidate,
    evaluate_candidates,
)
from portfolio_automation.discovery.discovery_memory import DiscoveryMemory
from portfolio_automation.run_mode_governance import (
    RunMode,
    RunModeViolation,
    assert_can_write_namespace,
    normalize_run_mode,
)
from portfolio_automation.data_governance import OutputNamespace, safe_write_json, safe_write_text

logger = logging.getLogger(__name__)

# Sandbox sub-path for all discovery artifacts
_DISCOVERY_SUBDIR = "discovery"

_SANDBOX_PATHS = {
    "emerging": f"{_DISCOVERY_SUBDIR}/emerging_candidates.json",
    "rejected": f"{_DISCOVERY_SUBDIR}/rejected_candidates.json",
    "memory":   f"{_DISCOVERY_SUBDIR}/discovery_memory.json",
    "memo":     f"{_DISCOVERY_SUBDIR}/discovery_memo_section.md",
}

_DISCLAIMER = (
    "Discovery candidates are not buy/sell recommendations. "
    "These are research-lane candidates pending corroboration. "
    "No official portfolio action has been taken."
)


# ---------------------------------------------------------------------------
# Internal builders
# ---------------------------------------------------------------------------

def _candidate_to_dict(cand: DiscoveryCandidate) -> dict:
    return {
        "ticker": cand.ticker,
        "status": cand.status.value,
        "score": cand.score,
        "mention_count": cand.mention_count,
        "unique_source_count": cand.unique_source_count,
        "event_type": cand.event_type.value,
        "event_confidence": cand.event_confidence,
        "risk_flag": cand.risk_flag,
        "rejection_reason": cand.rejection_reason,
        "discovery_only": cand.discovery_only,
        "sandbox_only": cand.sandbox_only,
        "corroboration_required": cand.corroboration_required,
        "corroboration_met": cand.corroboration_met,
        "corroboration_sources": cand.corroboration_sources,
        "first_seen": cand.first_seen,
        "last_seen": cand.last_seen,
        "evidence_snippets": cand.evidence_snippets[:3],
    }


def _build_emerging_payload(
    candidates: list[DiscoveryCandidate],
    run_id: str,
    generated_at: str,
) -> dict:
    emerging = [c for c in candidates if c.status != CandidateStatus.REJECTED]
    return {
        "generated_at": generated_at,
        "run_id": run_id,
        "observe_only": True,
        "discovery_only": True,
        "sandbox_only": True,
        "disclaimer": _DISCLAIMER,
        "total_candidates": len(emerging),
        "watch_count": sum(1 for c in emerging if c.status == CandidateStatus.WATCH),
        "discovered_count": sum(1 for c in emerging if c.status == CandidateStatus.DISCOVERED),
        "candidates": [_candidate_to_dict(c) for c in emerging],
    }


def _build_rejected_payload(
    candidates: list[DiscoveryCandidate],
    run_id: str,
    generated_at: str,
) -> dict:
    rejected = [c for c in candidates if c.status == CandidateStatus.REJECTED]
    return {
        "generated_at": generated_at,
        "run_id": run_id,
        "observe_only": True,
        "discovery_only": True,
        "sandbox_only": True,
        "disclaimer": _DISCLAIMER,
        "total_rejected": len(rejected),
        "candidates": [_candidate_to_dict(c) for c in rejected],
    }


def _build_memo_markdown(
    candidates: list[DiscoveryCandidate],
    run_id: str,
    generated_at: str,
) -> str:
    watch = [c for c in candidates if c.status == CandidateStatus.WATCH]
    rejected = [c for c in candidates if c.status == CandidateStatus.REJECTED]
    discovered = [c for c in candidates if c.status == CandidateStatus.DISCOVERED]

    event_counts: dict[str, int] = {}
    for c in candidates:
        event_counts[c.event_type.value] = event_counts.get(c.event_type.value, 0) + 1

    lines = [
        "## Discovery Research Section",
        "",
        f"**Generated:** {generated_at}  ",
        f"**Run ID:** {run_id}  ",
        f"**discovery_only:** true  ",
        f"**sandbox_only:** true  ",
        "",
        f"> {_DISCLAIMER}",
        "",
        "**Official watchlist and recommendations were not modified.**",
        "",
    ]

    # WATCH candidates
    lines += ["### WATCH Candidates", ""]
    if watch:
        for c in watch[:10]:
            lines.append(
                f"- **{c.ticker}** — score {c.score:.2f}, "
                f"event: {c.event_type.value}, "
                f"mentions: {c.mention_count}, "
                f"sources: {c.unique_source_count}"
                + (" ⚠ risk flag" if c.risk_flag else "")
            )
    else:
        lines.append("*No WATCH candidates this run.*")
    lines.append("")

    # DISCOVERED candidates
    lines += ["### DISCOVERED Candidates", ""]
    if discovered:
        for c in discovered[:10]:
            lines.append(
                f"- **{c.ticker}** — score {c.score:.2f}, event: {c.event_type.value}"
            )
    else:
        lines.append("*No DISCOVERED candidates this run.*")
    lines.append("")

    # Event type summary
    lines += ["### Event Type Summary", ""]
    for etype, count in sorted(event_counts.items(), key=lambda x: -x[1]):
        lines.append(f"- {etype}: {count}")
    lines.append("")

    # Rejected candidates
    lines += ["### Rejected Candidates", ""]
    if rejected:
        for c in rejected[:10]:
            lines.append(f"- **{c.ticker}** — {c.rejection_reason or 'below threshold'}")
    else:
        lines.append("*No rejections this run.*")
    lines.append("")

    lines += [
        "---",
        "",
        "_Discovery Engine v1 — research lane only._",
        "_No corroboration has been performed. No official action may be taken based on this output._",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public write function
# ---------------------------------------------------------------------------

def write_discovery_reports(
    candidates: list[DiscoveryCandidate],
    memory: DiscoveryMemory,
    *,
    run_mode: str | RunMode = "discovery",
    run_id: str | None = None,
    base_dir: str | Path = "outputs",
) -> dict[str, Path]:
    """
    Write all discovery sandbox artifacts.

    Enforces run mode governance — only modes with ``can_write_sandbox=True``
    (DISCOVERY or BACKTEST) may write. Raises :exc:`RunModeViolation` otherwise.

    Parameters
    ----------
    candidates: Scored candidates from :func:`evaluate_candidates`.
    memory: Updated :class:`DiscoveryMemory` instance.
    run_mode: Active run mode. Must be sandbox-writable.
    run_id: Identifier for this run (used in artifact metadata).
    base_dir: Root outputs directory.

    Returns
    -------
    Dict of artifact name → :class:`Path` written.
    """
    mode = normalize_run_mode(run_mode)
    assert_can_write_namespace(mode, OutputNamespace.SANDBOX)

    generated_at = datetime.now(timezone.utc).isoformat()
    _run_id = run_id or f"discovery_{generated_at}"
    base = str(base_dir)
    written: dict[str, Path] = {}

    # emerging_candidates.json
    emerging_payload = _build_emerging_payload(candidates, _run_id, generated_at)
    p = safe_write_json(OutputNamespace.SANDBOX, _SANDBOX_PATHS["emerging"], emerging_payload, base_dir=base)
    written["emerging_candidates"] = p
    logger.info("discovery: wrote emerging_candidates → %s", p)

    # rejected_candidates.json
    rejected_payload = _build_rejected_payload(candidates, _run_id, generated_at)
    p = safe_write_json(OutputNamespace.SANDBOX, _SANDBOX_PATHS["rejected"], rejected_payload, base_dir=base)
    written["rejected_candidates"] = p
    logger.info("discovery: wrote rejected_candidates → %s", p)

    # discovery_memory.json
    memory_payload = memory.to_dict()
    p = safe_write_json(OutputNamespace.SANDBOX, _SANDBOX_PATHS["memory"], memory_payload, base_dir=base)
    written["discovery_memory"] = p
    logger.info("discovery: wrote discovery_memory → %s", p)

    # discovery_memo_section.md
    memo_md = _build_memo_markdown(candidates, _run_id, generated_at)
    p = safe_write_text(OutputNamespace.SANDBOX, _SANDBOX_PATHS["memo"], memo_md, base_dir=base)
    written["discovery_memo_section"] = p
    logger.info("discovery: wrote discovery_memo_section → %s", p)

    return written


# ---------------------------------------------------------------------------
# Orchestration entry point
# ---------------------------------------------------------------------------

def run_discovery_engine(
    records: list[dict],
    *,
    run_mode: str | RunMode = "discovery",
    run_id: str | None = None,
    memory_path: Path | str | None = None,
    base_dir: str | Path = "outputs",
    known_universe: set[str] | frozenset[str] | None = None,
    watch_threshold: float = 2.0,
    reject_risk_below: float = 0.3,
    write_files: bool = True,
) -> dict[str, Any]:
    """
    Orchestrate a full discovery run from raw records to sandbox artifacts.

    No live API calls. No official portfolio mutations. No auto-trading.

    Parameters
    ----------
    records:
        News/event records. Each may have: title, summary, source,
        published_at, symbols, tickers.
    run_mode:
        Must be a sandbox-writable mode (default: ``"discovery"``).
    run_id:
        Run identifier for artifact metadata.
    memory_path:
        Path to existing ``discovery_memory.json`` for incremental updates.
        Pass ``None`` to start with empty memory.
    base_dir:
        Root outputs directory.
    known_universe:
        Optional allowlist; tickers not in this set are filtered out.
    watch_threshold:
        Minimum score for WATCH status.
    reject_risk_below:
        Confidence floor below which a risk-flagged event triggers REJECTED.
    write_files:
        Set to False to run in dry-run / test mode — skips file writes.

    Returns
    -------
    Summary dict with candidate counts, artifact paths, and governance metadata.
    """
    mode = normalize_run_mode(run_mode)
    generated_at = datetime.now(timezone.utc).isoformat()
    _run_id = run_id or f"discovery_{generated_at}"

    # 1. Ticker extraction
    discovered_tickers: list[DiscoveredTicker] = extract_tickers(
        records, known_universe=known_universe
    )

    # 2. Per-record event classification
    record_classifications: list[ClassificationResult] = [
        classify_record(r) for r in records
    ]

    # 3. Candidate scoring and status
    candidates: list[DiscoveryCandidate] = evaluate_candidates(
        discovered_tickers,
        record_classifications,
        watch_threshold=watch_threshold,
        reject_risk_below=reject_risk_below,
    )

    # 4. Memory update
    memory = DiscoveryMemory()
    if memory_path is not None:
        memory = DiscoveryMemory.load_from_path(memory_path)
    memory.update(candidates)

    # 5. Write sandbox artifacts
    written: dict[str, Path] = {}
    if write_files:
        written = write_discovery_reports(
            candidates,
            memory,
            run_mode=mode,
            run_id=_run_id,
            base_dir=base_dir,
        )

    watch_list = [c for c in candidates if c.status == CandidateStatus.WATCH]
    rejected_list = [c for c in candidates if c.status == CandidateStatus.REJECTED]
    discovered_list = [c for c in candidates if c.status == CandidateStatus.DISCOVERED]

    return {
        "generated_at": generated_at,
        "run_id": _run_id,
        "run_mode": mode.value,
        "observe_only": True,
        "discovery_only": True,
        "sandbox_only": True,
        "disclaimer": _DISCLAIMER,
        "records_processed": len(records),
        "tickers_extracted": len(discovered_tickers),
        "total_candidates": len(candidates),
        "watch_count": len(watch_list),
        "discovered_count": len(discovered_list),
        "rejected_count": len(rejected_list),
        "watch_tickers": [c.ticker for c in watch_list],
        "corroboration_required": True,
        "official_watchlist_modified": False,
        "official_recommendations_modified": False,
        "can_execute_trades": False,
        "artifacts_written": {k: str(v) for k, v in written.items()},
    }
