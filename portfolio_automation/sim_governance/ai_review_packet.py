"""
Consolidated AI/Product Review Packet (spec §3 Step 4).

Compresses the daily simulation bundle into ONE packet that covers BOTH the
advisory and watchlist workflows together (the daily review must review them in
a single call). Writes:

  * outputs/promotion_review/daily_ai_review_packet.json
  * outputs/promotion_review/daily_ai_review_packet.md

The packet is intentionally compact — one line of decision-relevant evidence per
candidate — so the single daily AI call stays well under the $0.50 cap.
"""
from __future__ import annotations

import json
import logging

from portfolio_automation.data_governance import OutputNamespace, safe_write_json, safe_write_text
from portfolio_automation.sim_governance import schemas as S

logger = logging.getLogger("stockbot.sim_governance.ai_review_packet")

_PACKET_JSON = "daily_ai_review_packet.json"
_PACKET_MD = "daily_ai_review_packet.md"

# Rough token estimate: ~4 characters per token for English+JSON.
_CHARS_PER_TOKEN = 4


def _candidate_line(c: dict) -> dict:
    """One compact, review-ready record per candidate."""
    return {
        "candidate_id": c.get("candidate_id"),
        "workflow": c.get("workflow"),
        "proposal_type": c.get("proposal_type"),
        "symbol": c.get("symbol"),
        "what_changed": c.get("what_changed"),
        "why_changed": c.get("why_changed"),
        "before": c.get("before"),
        "after": c.get("after"),
        "risk_impact": c.get("risk_impact"),
        "confidence": c.get("confidence"),
        "data_quality": c.get("data_quality"),
        "evidence": c.get("source_evidence", []),
        "sim_ready_hint": c.get("ready_for_production_review"),
    }


def build_review_packet(bundle: dict, now: str) -> dict:
    """Build the consolidated review packet dict from a daily simulation bundle."""
    candidates = (
        (bundle.get("advisory_experiment_results") or [])
        + (bundle.get("watchlist_experiment_results") or [])
    )
    # De-dup by candidate_id while preserving order (advisory first).
    seen: set[str] = set()
    lines: list[dict] = []
    for c in candidates:
        cid = c.get("candidate_id")
        if cid in seen:
            continue
        seen.add(cid)
        lines.append(_candidate_line(c))

    advisory_lines = [l for l in lines if l["workflow"] == S.WORKFLOW_ADVISORY]
    watchlist_lines = [l for l in lines if l["workflow"] == S.WORKFLOW_WATCHLIST]

    packet = {
        "generated_at": now,
        "schema": "daily_ai_review_packet.v1",
        "instruction": (
            "Review BOTH workflows together. For each candidate, classify as one "
            f"of {sorted(S.REVIEW_DECISIONS)}. You may RECOMMEND readiness; you "
            "cannot approve production. Human approval is the production gate."
        ),
        "covers_workflows": [S.WORKFLOW_ADVISORY, S.WORKFLOW_WATCHLIST],
        "candidate_count": len(lines),
        "advisory_candidates": advisory_lines,
        "watchlist_candidates": watchlist_lines,
        "risk_governance_checks": {
            "risk_summary": bundle.get("risk_summary", {}),
            "data_quality": bundle.get("data_quality", {}),
            "confidence_summary": bundle.get("confidence_summary", {}),
            "production_safe": True,
            "decision_engine_untouched": True,
        },
        "comparison_vs_production_baseline": bundle.get("comparison_vs_production_baseline", {}),
        # Unified crowd evidence: compact context for the reviewer (no extra AI
        # call). The reviewer may RECOMMEND production-readiness from it; it can
        # never approve. Cost rides the single consolidated review under the cap.
        "unified_crowd_summary": bundle.get("unified_crowd_summary", {}),
        "artifact_refs": bundle.get("artifact_refs", []),
    }
    packet["estimated_prompt_tokens"] = estimate_packet_tokens(packet)
    return packet


def estimate_packet_tokens(packet: dict) -> int:
    """Estimate the prompt-token cost of sending this packet to the model."""
    try:
        chars = len(json.dumps(packet, default=str))
    except Exception:
        chars = 4000
    return max(1, chars // _CHARS_PER_TOKEN)


def render_packet_md(packet: dict) -> str:
    lines: list[str] = []
    lines.append("# Daily AI / Product Review Packet")
    lines.append("")
    lines.append(f"**Generated:** {packet.get('generated_at')}  ")
    lines.append(f"**Candidates:** {packet.get('candidate_count', 0)} "
                 f"(advisory + watchlist, reviewed together)")
    lines.append("")
    lines.append("> " + packet.get("instruction", ""))
    lines.append("")
    for title, key in (("Advisory candidates", "advisory_candidates"),
                       ("Watchlist candidates", "watchlist_candidates")):
        rows = packet.get(key, [])
        lines.append(f"## {title} ({len(rows)})")
        lines.append("")
        if not rows:
            lines.append("_None this run._")
            lines.append("")
            continue
        lines.append("| Candidate | Symbol | Type | What changed | Risk | Conf | Data | Sim-ready |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in rows:
            lines.append(
                f"| `{r['candidate_id']}` | {r.get('symbol') or '—'} | {r.get('proposal_type')} "
                f"| {r.get('what_changed')} | {r.get('risk_impact')} | {r.get('confidence')} "
                f"| {r.get('data_quality')} | {'yes' if r.get('sim_ready_hint') else 'no'} |"
            )
        lines.append("")
    lines.append("---")
    lines.append("*AI/product review may recommend readiness only. "
                 "Production changes require human approval.*")
    return "\n".join(lines)


def write_review_packet(packet: dict, *, base_dir: str) -> dict:
    """Write the packet JSON + Markdown to the PROMOTION_REVIEW namespace."""
    try:
        safe_write_json(OutputNamespace.PROMOTION_REVIEW, _PACKET_JSON, packet, base_dir=base_dir)
        safe_write_text(OutputNamespace.PROMOTION_REVIEW, _PACKET_MD,
                        render_packet_md(packet), base_dir=base_dir)
    except Exception as exc:
        logger.warning("ai_review_packet: write failed: %s", exc)
        packet = {**packet, "write_error": str(exc)}
    return packet
