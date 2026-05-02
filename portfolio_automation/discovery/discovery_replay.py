"""
Discovery Replay — Sandbox evaluation of historical discovery candidates.

Evaluates whether sandbox discovery candidates have predictive value over time,
using injected price/outcome data (no external API calls).

SANDBOX-ONLY: All outputs go to outputs/sandbox/discovery/.
OBSERVE-ONLY: No official watchlist, portfolio, or recommendation mutations.
NO TRADE: Never produces buy/sell/actionable/promoted/validated signals.

Run mode governance:
  DISCOVERY and BACKTEST modes may write replay artifacts.
  All other modes raise RunModeViolation.

No external API calls are made. Price/outcome data must be injected by the caller.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.run_mode_governance import (
    RunMode,
    assert_can_write_namespace,
    normalize_run_mode,
)
from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)
from portfolio_automation.discovery.approval_workflow import (
    is_valid_loaded_approval_record,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path constants (sandbox-only)
# ---------------------------------------------------------------------------

_DISCOVERY_SUBDIR = "discovery"

_REPLAY_PATHS = {
    "results_json":   f"{_DISCOVERY_SUBDIR}/replay_results.json",
    "results_md":     f"{_DISCOVERY_SUBDIR}/replay_results.md",
    "outcomes_jsonl": f"{_DISCOVERY_SUBDIR}/replay_candidate_outcomes.jsonl",
}

_DEFAULT_WINDOWS: tuple[int, ...] = (1, 3, 5, 10, 20)

_DISCLAIMER = (
    "Discovery replay results are sandbox research only. "
    "They do not constitute buy/sell recommendations and do not update "
    "the official watchlist, portfolio, or any official recommendation. "
    "No official recommendation or watchlist change is made by this report."
)

_METHODOLOGY = (
    "Candidates are loaded from sandbox discovery artifacts "
    "(emerging_candidates.json, rejected_candidates.json). "
    "Outcome metrics are computed from injected price/outcome data only — "
    "no external API calls are made. "
    "Candidates without price data are marked insufficient_data=True. "
    "Aggregates compare WATCH vs DISCOVERED status, corroboration levels, "
    "approval decisions, and risk flags. "
    "Results are advisory calibration data for the research lane only."
)

_FORBIDDEN_STATUSES: frozenset[str] = frozenset({
    "buy", "sell", "actionable", "promoted", "validated",
})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_load_json(path: Path) -> dict[str, Any] | None:
    """Load JSON from path; return None on missing or corrupt file."""
    try:
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return None
        return json.loads(text)
    except Exception as exc:
        logger.warning("discovery_replay: failed to load %s — %s", path, exc)
        return None


def _load_approval_decisions(path: Path) -> list[dict[str, Any]]:
    """Load and validate approval decisions from JSONL; skip invalid records."""
    decisions: list[dict[str, Any]] = []
    try:
        if not path.exists():
            return decisions
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if is_valid_loaded_approval_record(rec):
                decisions.append(rec)
    except Exception as exc:
        logger.warning("discovery_replay: approval load failed — %s", exc)
    return decisions


def _window_key(days: int) -> str:
    return f"window_{days}"


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _hit_rate(correct: list[bool]) -> float | None:
    if not correct:
        return None
    return round(sum(1 for v in correct if v) / len(correct), 4)


def _group_stats(
    outcomes: list[dict[str, Any]],
    windows: tuple[int, ...],
) -> dict[str, Any]:
    """Aggregate window metrics for a group of candidate outcomes."""
    result: dict[str, Any] = {"count": len(outcomes)}
    for w in windows:
        key = _window_key(w)
        returns = [
            o[key]["forward_return_pct"]
            for o in outcomes
            if key in o and o[key].get("forward_return_pct") is not None
        ]
        corrects = [
            o[key]["direction_correct"]
            for o in outcomes
            if key in o and o[key].get("direction_correct") is not None
        ]
        result[key] = {
            "resolved": len(returns),
            "avg_forward_return_pct": _avg(returns),
            "hit_rate": _hit_rate(corrects),
        }
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_discovery_replay_inputs(
    *,
    base_dir: str | Path = "outputs",
) -> dict[str, Any]:
    """
    Load and validate all discovery sandbox artifacts for replay analysis.

    Handles missing or corrupt files gracefully — returns partial data
    and sets ``available=False`` when nothing could be loaded.

    Returns
    -------
    dict with keys:
      ``emerging``, ``rejected``, ``memory``, ``approval_decisions``,
      ``candidates`` (merged list from emerging + rejected), ``available`` (bool).
    """
    root = Path(base_dir) / "sandbox" / "discovery"

    emerging = _safe_load_json(root / "emerging_candidates.json")
    rejected = _safe_load_json(root / "rejected_candidates.json")
    memory = _safe_load_json(root / "discovery_memory.json")
    approval_decisions = _load_approval_decisions(root / "approval_decisions.jsonl")

    candidates: list[dict[str, Any]] = []
    if emerging and isinstance(emerging.get("candidates"), list):
        candidates.extend(emerging["candidates"])
    if rejected and isinstance(rejected.get("candidates"), list):
        candidates.extend(rejected["candidates"])

    available = bool(
        emerging is not None
        or rejected is not None
        or memory is not None
        or approval_decisions
    )
    return {
        "emerging": emerging,
        "rejected": rejected,
        "memory": memory,
        "approval_decisions": approval_decisions,
        "candidates": candidates,
        "available": available,
    }


def evaluate_discovery_candidate_outcomes(
    candidates: list[dict[str, Any]],
    price_outcomes: dict[str, dict[str, Any]],
    *,
    windows: tuple[int, ...] = _DEFAULT_WINDOWS,
) -> list[dict[str, Any]]:
    """
    Compute outcome metrics for candidates given injected price/outcome data.

    Parameters
    ----------
    candidates:
        List of candidate dicts from emerging/rejected artifacts.
    price_outcomes:
        Injected price data keyed by ticker. Format::

            {
                "NVDA": {
                    "window_1": {
                        "forward_return_pct": 2.5,
                        "direction_correct": True,
                        "max_drawdown_pct": -0.5,
                        "max_runup_pct": 3.0,
                    },
                    "window_3": {...},
                }
            }

        Pass ``{}`` when no price data is available — all candidates will
        be marked ``insufficient_data=True``.
    windows:
        Forward return windows in trading days.

    Returns
    -------
    List of enriched candidate outcome dicts. All are observe-only sandbox records.
    """
    results: list[dict[str, Any]] = []

    for cand in candidates:
        ticker = cand.get("ticker", "")
        status = str(cand.get("status", "")).lower()

        # Safety: never emit forbidden statuses
        if status in _FORBIDDEN_STATUSES:
            logger.warning(
                "discovery_replay: skipping candidate %s with forbidden status %r",
                ticker, status,
            )
            continue

        outcome: dict[str, Any] = {
            "ticker": ticker,
            "status": status,
            "corroboration_score": cand.get("corroboration_score", 0.0),
            "corroboration_level": cand.get("corroboration_level", "none"),
            "corroboration_met": cand.get("corroboration_met", False),
            "risk_flag": cand.get("risk_flag", False),
            "event_type": cand.get("event_type", "unknown"),
            "mention_count": cand.get("mention_count", 0),
            "unique_source_count": cand.get("unique_source_count", 0),
            "first_seen": cand.get("first_seen"),
            "last_seen": cand.get("last_seen"),
            # Governance flags — always set and always True
            "observe_only": True,
            "sandbox_only": True,
            "no_trade": True,
            "discovery_only": True,
        }

        ticker_price = price_outcomes.get(ticker, {})
        has_any_data = False

        for w in windows:
            key = _window_key(w)
            window_data = ticker_price.get(key, {})
            if window_data:
                has_any_data = True
                outcome[key] = {
                    "forward_return_pct": window_data.get("forward_return_pct"),
                    "direction_correct": window_data.get("direction_correct"),
                    "max_drawdown_pct": window_data.get("max_drawdown_pct"),
                    "max_runup_pct": window_data.get("max_runup_pct"),
                }
            else:
                outcome[key] = {
                    "forward_return_pct": None,
                    "direction_correct": None,
                    "max_drawdown_pct": None,
                    "max_runup_pct": None,
                }

        outcome["insufficient_data"] = not has_any_data
        results.append(outcome)

    return results


def summarize_discovery_replay_results(
    candidate_outcomes: list[dict[str, Any]],
    approval_decisions: list[dict[str, Any]] | None = None,
    *,
    windows: tuple[int, ...] = _DEFAULT_WINDOWS,
) -> dict[str, Any]:
    """
    Aggregate candidate outcomes into summary metrics.

    Compares WATCH vs DISCOVERED, corroboration levels, approval decisions,
    and risk flags. All outputs carry observe-only / sandbox-only flags.

    Parameters
    ----------
    candidate_outcomes:
        From :func:`evaluate_discovery_candidate_outcomes`.
    approval_decisions:
        Validated approval decision dicts (from JSONL). Optional.
    windows:
        Forward return windows to include in aggregates.
    """
    generated_at = datetime.now(timezone.utc).isoformat()

    total = len(candidate_outcomes)
    resolved = [o for o in candidate_outcomes if not o.get("insufficient_data")]
    insufficient = [o for o in candidate_outcomes if o.get("insufficient_data")]

    watch_outcomes = [o for o in candidate_outcomes if o.get("status") == "watch"]
    discovered_outcomes = [o for o in candidate_outcomes if o.get("status") == "discovered"]
    rejected_outcomes = [o for o in candidate_outcomes if o.get("status") == "rejected"]

    high_corr = [
        o for o in candidate_outcomes if o.get("corroboration_level") == "strong"
    ]
    low_corr = [
        o for o in candidate_outcomes
        if o.get("corroboration_level") in ("none", "weak", "moderate")
    ]

    risk_flagged = [o for o in candidate_outcomes if o.get("risk_flag")]
    non_risk = [o for o in candidate_outcomes if not o.get("risk_flag")]

    # Build approval lookup: last decision per symbol wins
    approval_by_symbol: dict[str, str] = {}
    for dec in (approval_decisions or []):
        sym = dec.get("symbol", "")
        if sym:
            approval_by_symbol[sym] = dec.get("decision", "")

    approved_outcomes = [
        o for o in candidate_outcomes
        if approval_by_symbol.get(o.get("ticker", "")) == "approve_for_research_review"
    ]
    keep_watching_outcomes = [
        o for o in candidate_outcomes
        if approval_by_symbol.get(o.get("ticker", "")) == "keep_watching"
    ]
    needs_more_outcomes = [
        o for o in candidate_outcomes
        if approval_by_symbol.get(o.get("ticker", "")) == "needs_more_evidence"
    ]
    reject_decision_outcomes = [
        o for o in candidate_outcomes
        if approval_by_symbol.get(o.get("ticker", "")) == "reject_candidate"
    ]
    no_decision_outcomes = [
        o for o in candidate_outcomes
        if o.get("ticker", "") not in approval_by_symbol
    ]

    # Window metrics (overall)
    window_metrics: dict[str, Any] = {}
    for w in windows:
        key = _window_key(w)
        returns = [
            o[key]["forward_return_pct"]
            for o in candidate_outcomes
            if key in o and o[key].get("forward_return_pct") is not None
        ]
        corrects = [
            o[key]["direction_correct"]
            for o in candidate_outcomes
            if key in o and o[key].get("direction_correct") is not None
        ]
        window_metrics[key] = {
            "resolved": len(returns),
            "avg_forward_return_pct": _avg(returns),
            "hit_rate": _hit_rate(corrects),
        }

    insufficient_data_flag = total == 0 or len(resolved) == 0

    return {
        "generated_at": generated_at,
        "observe_only": True,
        "sandbox_only": True,
        "no_trade": True,
        "no_official_promotion": True,
        "insufficient_data": insufficient_data_flag,
        "disclaimer": _DISCLAIMER,
        "methodology": _METHODOLOGY,
        "disclaimers": [
            _DISCLAIMER,
            "No official recommendation or watchlist change is made by this report.",
            "Discovery replay results are observe-only sandbox research.",
        ],
        "candidate_count": total,
        "resolved_count": len(resolved),
        "insufficient_data_count": len(insufficient),
        "summary": {
            "total_candidates": total,
            "resolved_candidates": len(resolved),
            "insufficient_data_count": len(insufficient),
            "watch_count": len(watch_outcomes),
            "discovered_count": len(discovered_outcomes),
            "rejected_count": len(rejected_outcomes),
        },
        "window_metrics": window_metrics,
        "status_comparison": {
            "watch": _group_stats(watch_outcomes, windows),
            "discovered": _group_stats(discovered_outcomes, windows),
            "rejected": _group_stats(rejected_outcomes, windows),
        },
        "corroboration_comparison": {
            "high_corroboration": _group_stats(high_corr, windows),
            "low_corroboration": _group_stats(low_corr, windows),
        },
        "approval_decision_comparison": {
            "approve_for_research_review": _group_stats(approved_outcomes, windows),
            "keep_watching": _group_stats(keep_watching_outcomes, windows),
            "needs_more_evidence": _group_stats(needs_more_outcomes, windows),
            "reject_candidate": _group_stats(reject_decision_outcomes, windows),
            "no_decision": _group_stats(no_decision_outcomes, windows),
        },
        "risk_comparison": {
            "risk_flagged": _group_stats(risk_flagged, windows),
            "non_risk": _group_stats(non_risk, windows),
        },
        "rejected_candidate_review": {
            "count": len(rejected_outcomes),
            "with_price_data": sum(
                1 for o in rejected_outcomes if not o.get("insufficient_data")
            ),
            "candidates": [
                {
                    "ticker": o.get("ticker"),
                    "corroboration_level": o.get("corroboration_level"),
                    "risk_flag": o.get("risk_flag"),
                    "insufficient_data": o.get("insufficient_data"),
                }
                for o in rejected_outcomes[:20]
            ],
        },
    }


def _build_replay_markdown(
    summary: dict[str, Any],
    candidate_outcomes: list[dict[str, Any]],
    run_id: str,
) -> str:
    """Build the Markdown replay report."""

    def _fmt_ret(v: float | None) -> str:
        return f"{v:+.2f}%" if v is not None else "—"

    def _fmt_rate(v: float | None) -> str:
        return f"{v:.0%}" if v is not None else "—"

    lines = [
        "# Discovery Replay — Sandbox Research Report",
        "",
        f"> **SANDBOX ONLY** — {_DISCLAIMER}",
        "",
        f"**Generated:** {summary.get('generated_at', 'unknown')}  ",
        f"**Run ID:** {run_id}  ",
        "**observe_only:** true  ",
        "**sandbox_only:** true  ",
        "**no_trade:** true  ",
        "",
        "No official recommendation or watchlist change is made by this report.",
        "",
    ]

    s = summary.get("summary", {})
    lines += [
        "## Executive Summary",
        "",
        f"- Total candidates evaluated: {summary.get('candidate_count', 0)}",
        f"- Resolved (price data available): {summary.get('resolved_count', 0)}",
        f"- Insufficient data: {summary.get('insufficient_data_count', 0)}",
        f"- WATCH: {s.get('watch_count', 0)}",
        f"- DISCOVERED: {s.get('discovered_count', 0)}",
        f"- REJECTED: {s.get('rejected_count', 0)}",
        "",
    ]

    if summary.get("insufficient_data"):
        lines += [
            "**Note:** Insufficient price data to compute outcome metrics.",
            "This report shows candidate metadata only.",
            "",
        ]

    lines += ["## Data Coverage", ""]
    win_metrics = summary.get("window_metrics", {})
    for key, stats in win_metrics.items():
        days = key.replace("window_", "")
        lines.append(f"- {days}-day window: {stats.get('resolved', 0)} candidates resolved")
    lines.append("")

    if not summary.get("insufficient_data") and win_metrics:
        lines += [
            "## Outcome Metrics by Window",
            "",
            "| Window | Resolved | Avg Return | Hit Rate |",
            "|--------|----------|------------|----------|",
        ]
        for key, stats in win_metrics.items():
            days = key.replace("window_", "")
            lines.append(
                f"| {days}d | {stats.get('resolved', 0)} "
                f"| {_fmt_ret(stats.get('avg_forward_return_pct'))} "
                f"| {_fmt_rate(stats.get('hit_rate'))} |"
            )
        lines.append("")

    lines += ["## WATCH vs DISCOVERED Comparison", ""]
    sc = summary.get("status_comparison", {})
    for status in ("watch", "discovered"):
        group = sc.get(status, {})
        lines.append(f"### {status.upper()} ({group.get('count', 0)} candidates)")
        for key, stats in group.items():
            if not key.startswith("window_"):
                continue
            days = key.replace("window_", "")
            lines.append(
                f"- {days}d: resolved={stats.get('resolved', 0)}, "
                f"avg_return={_fmt_ret(stats.get('avg_forward_return_pct'))}, "
                f"hit_rate={_fmt_rate(stats.get('hit_rate'))}"
            )
        lines.append("")

    lines += ["## Corroboration Analysis", ""]
    cc = summary.get("corroboration_comparison", {})
    for group_name in ("high_corroboration", "low_corroboration"):
        group = cc.get(group_name, {})
        display = group_name.replace("_", " ").title()
        lines.append(f"### {display} ({group.get('count', 0)} candidates)")
        for key, stats in group.items():
            if not key.startswith("window_"):
                continue
            days = key.replace("window_", "")
            lines.append(
                f"- {days}d: resolved={stats.get('resolved', 0)}, "
                f"avg_return={_fmt_ret(stats.get('avg_forward_return_pct'))}"
            )
        lines.append("")
    lines += [
        "_Higher corroboration scores are expected to correlate with better candidates._",
        "_Statistical significance requires more data (recommended: 20+ resolved per group)._",
        "",
    ]

    lines += ["## Approval Decision Analysis", ""]
    adc = summary.get("approval_decision_comparison", {})
    for dec_key in (
        "approve_for_research_review",
        "keep_watching",
        "needs_more_evidence",
        "reject_candidate",
        "no_decision",
    ):
        group = adc.get(dec_key, {})
        if group.get("count", 0) == 0:
            continue
        display = dec_key.replace("_", " ").title()
        lines.append(f"### {display} ({group.get('count', 0)} candidates)")
        for key, stats in group.items():
            if not key.startswith("window_"):
                continue
            days = key.replace("window_", "")
            lines.append(
                f"- {days}d: resolved={stats.get('resolved', 0)}, "
                f"avg_return={_fmt_ret(stats.get('avg_forward_return_pct'))}"
            )
        lines.append("")

    rc = summary.get("rejected_candidate_review", {})
    rik = summary.get("risk_comparison", {})
    lines += [
        "## Rejected and Risk-Flagged Candidates",
        "",
        f"- Rejected candidates: {rc.get('count', 0)}",
        f"- With price data: {rc.get('with_price_data', 0)}",
        f"- Risk-flagged: {rik.get('risk_flagged', {}).get('count', 0)}",
        f"- Non-risk: {rik.get('non_risk', {}).get('count', 0)}",
        "",
    ]

    lines += [
        "## Insufficient Data Notes",
        "",
        (
            "Candidates marked `insufficient_data=True` lack price/outcome data for analysis. "
            "Replay results improve as price history is injected into the system."
        ),
        "",
    ]

    lines += [
        "## Recommended Future Research Thresholds",
        "",
        "Based on v1 discovery design:",
        "- WATCH requires corroboration_score >= 0.65 (strong level)",
        "- Candidates with 4+ unique sources and 3+ seen_runs are highest priority",
        "- Risk-flagged candidates with low event confidence are most likely to underperform",
        "- Recommended minimum data: 20+ resolved candidates per comparison group",
        "",
    ]

    lines += [
        "---",
        "",
        "_Discovery Replay v1 — sandbox research only._",
        "_No official recommendation or watchlist change is made by this report._",
    ]
    return "\n".join(lines)


def write_discovery_replay_report(
    summary: dict[str, Any],
    candidate_outcomes: list[dict[str, Any]],
    *,
    run_mode: str | RunMode = "discovery",
    run_id: str | None = None,
    base_dir: str | Path = "outputs",
) -> dict[str, Path]:
    """
    Write sandbox replay artifacts.

    Enforces run mode governance — only DISCOVERY or BACKTEST modes may write.

    Artifacts written (all under ``outputs/sandbox/discovery/``):
      - ``replay_results.json``
      - ``replay_results.md``
      - ``replay_candidate_outcomes.jsonl``

    Parameters
    ----------
    summary: From :func:`summarize_discovery_replay_results`.
    candidate_outcomes: From :func:`evaluate_discovery_candidate_outcomes`.
    run_mode: Must be sandbox-writable (DISCOVERY or BACKTEST).
    run_id: Identifier stamped in artifact metadata.
    base_dir: Root outputs directory.

    Returns
    -------
    Dict mapping artifact name → :class:`Path` written.
    """
    mode = normalize_run_mode(run_mode)
    assert_can_write_namespace(mode, OutputNamespace.SANDBOX)

    _run_id = run_id or f"replay_{datetime.now(timezone.utc).isoformat()}"
    base = str(base_dir)
    written: dict[str, Path] = {}

    p = safe_write_json(
        OutputNamespace.SANDBOX,
        _REPLAY_PATHS["results_json"],
        summary,
        base_dir=base,
    )
    written["replay_results_json"] = p
    logger.info("discovery_replay: wrote replay_results.json → %s", p)

    md_content = _build_replay_markdown(summary, candidate_outcomes, _run_id)
    p = safe_write_text(
        OutputNamespace.SANDBOX,
        _REPLAY_PATHS["results_md"],
        md_content,
        base_dir=base,
    )
    written["replay_results_md"] = p
    logger.info("discovery_replay: wrote replay_results.md → %s", p)

    # JSONL is overwritten on each run (not append-only)
    jsonl_lines = [json.dumps(o) for o in candidate_outcomes]
    jsonl_content = "\n".join(jsonl_lines)
    if jsonl_content:
        jsonl_content += "\n"
    p = safe_write_text(
        OutputNamespace.SANDBOX,
        _REPLAY_PATHS["outcomes_jsonl"],
        jsonl_content,
        base_dir=base,
    )
    written["replay_candidate_outcomes_jsonl"] = p
    logger.info("discovery_replay: wrote replay_candidate_outcomes.jsonl → %s", p)

    return written


def run_discovery_replay(
    *,
    price_outcomes: dict[str, dict[str, Any]] | None = None,
    run_mode: str | RunMode = "discovery",
    run_id: str | None = None,
    base_dir: str | Path = "outputs",
    windows: tuple[int, ...] = _DEFAULT_WINDOWS,
    write_files: bool = True,
) -> dict[str, Any]:
    """
    Orchestrate a full discovery replay analysis run.

    No external API calls. No official portfolio mutations. No trade execution.

    Parameters
    ----------
    price_outcomes:
        Injected price/outcome data keyed by ticker symbol. See
        :func:`evaluate_discovery_candidate_outcomes` for the expected format.
        Pass ``None`` or ``{}`` when no price data is available.
    run_mode:
        Must be sandbox-writable (``"discovery"`` or ``"backtest"``).
    run_id:
        Identifier for this replay run (used in artifact metadata).
    base_dir:
        Root outputs directory.
    windows:
        Forward return windows in trading days.
    write_files:
        Set ``False`` for dry-run / test mode — skips all file writes.

    Returns
    -------
    Summary dict with governance flags, aggregate metrics, and artifact paths.
    """
    mode = normalize_run_mode(run_mode)
    generated_at = datetime.now(timezone.utc).isoformat()
    _run_id = run_id or f"replay_{generated_at}"
    _price_outcomes = price_outcomes or {}

    inputs = load_discovery_replay_inputs(base_dir=base_dir)

    candidate_outcomes = evaluate_discovery_candidate_outcomes(
        inputs["candidates"],
        _price_outcomes,
        windows=windows,
    )

    summary = summarize_discovery_replay_results(
        candidate_outcomes,
        approval_decisions=inputs["approval_decisions"],
        windows=windows,
    )
    summary["generated_at"] = generated_at
    summary["run_id"] = _run_id
    summary["run_mode"] = mode.value

    written: dict[str, Path] = {}
    if write_files:
        written = write_discovery_replay_report(
            summary,
            candidate_outcomes,
            run_mode=mode,
            run_id=_run_id,
            base_dir=base_dir,
        )

    s = summary.get("summary", {})
    return {
        "generated_at": generated_at,
        "run_id": _run_id,
        "run_mode": mode.value,
        "observe_only": True,
        "sandbox_only": True,
        "no_trade": True,
        "no_official_promotion": True,
        "discovery_only": True,
        "disclaimer": _DISCLAIMER,
        "insufficient_data": summary.get("insufficient_data", True),
        "candidate_count": summary.get("candidate_count", 0),
        "resolved_count": summary.get("resolved_count", 0),
        "watch_count": s.get("watch_count", 0),
        "discovered_count": s.get("discovered_count", 0),
        "rejected_count": s.get("rejected_count", 0),
        "window_metrics": summary.get("window_metrics", {}),
        "artifacts_written": {k: str(v) for k, v in written.items()},
        "official_watchlist_modified": False,
        "official_recommendations_modified": False,
        "can_execute_trades": False,
    }
