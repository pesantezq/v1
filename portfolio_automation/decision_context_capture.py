"""Phase 4 — decision-time context capture + outcome taxonomy.

Observe-only complement to ``decision_outcome_tracker``. It records each
decision's IMMUTABLE at-decision context (regime / crowd / factor / confidence /
data-quality + the evaluation horizons + source refs + the frozen input
snapshot hash) so later outcome maturation can attribute results to the
conditions that produced them — and it provides the explicit outcome taxonomy
with a return neutral-band.

Boundary: this NEVER mutates the protected stored win-rate
(``decision_outcome_tracker`` / ``performance_feedback``). The neutral band is
applied only here, exactly as ``memo_coherence`` does, so historical
compatibility is preserved (Iron rules 3, 6 — no protected mutation, no outcome
overwrite). Append-only; pure except injected ``now``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace, get_output_path, safe_write_text,
)

# 1/3/7 are resolved today; 21/63 are declared by contract but not forced
# (the source data / current design does not yet support them safely).
RESOLVED_HORIZONS: list[int] = [1, 3, 7]
CONTRACT_HORIZONS: list[int] = [1, 3, 7, 21, 63]

TAXONOMY = ("hit", "miss", "neutral", "unresolved", "insufficient_data", "invalidated")

# ±1% return neutral band — sub-band moves are noise, not hit/miss. Matches the
# memo_coherence convention (decision_outcomes return_pct is in percent units
# at this layer; callers pass percent).
NEUTRAL_BAND_PCT = 1.0

_WANT_UP = {"BUY", "SCALE", "ADD", "ACCUMULATE"}
_WANT_DOWN = {"SELL", "AVOID", "TRIM", "REDUCE"}
_INVALID_DQ = {"invalid", "invalidated", "bad", "error"}
_INSUFFICIENT_DQ = {"insufficient", "insufficient_data", "missing", "degraded", "unknown"}

_LOG_RELATIVE = ("outputs", "policy", "decision_context_log.jsonl")

__all__ = [
    "RESOLVED_HORIZONS", "CONTRACT_HORIZONS", "TAXONOMY", "NEUTRAL_BAND_PCT",
    "classify_outcome", "is_counted", "counted_hit_rate",
    "capture_decision_context", "write_decision_context",
    "run_decision_context_capture",
]


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------


def classify_outcome(
    decision: str,
    return_pct: float | None,
    *,
    resolved: bool,
    data_quality: str = "ok",
    neutral_band_pct: float = NEUTRAL_BAND_PCT,
) -> str:
    """Classify one decision outcome into the explicit taxonomy.

    Data-quality dominates (a bad observation can't be a hit/miss); then
    resolution; then the neutral band; then direction.
    """
    dq = (data_quality or "ok").lower()
    if dq in _INVALID_DQ:
        return "invalidated"
    if dq in _INSUFFICIENT_DQ:
        return "insufficient_data"
    if not resolved or return_pct is None:
        return "unresolved"
    if abs(return_pct) < neutral_band_pct:
        return "neutral"
    d = (decision or "").upper()
    if d in _WANT_UP:
        return "hit" if return_pct > 0 else "miss"
    if d in _WANT_DOWN:
        return "hit" if return_pct < 0 else "miss"
    if d in ("WAIT", "HOLD"):
        # directionless: a sub-band move (handled above) is the "correct" case;
        # a beyond-band move is a miss for a wait/hold thesis.
        return "miss"
    return "neutral"


def is_counted(label: str) -> bool:
    """Only hit/miss enter a win-rate denominator (neutral/unresolved/
    insufficient/invalidated are excluded — Iron rule on honest denominators)."""
    return label in ("hit", "miss")


def counted_hit_rate(labels: list[str]) -> dict[str, Any]:
    judgeable = [l for l in labels if is_counted(l)]
    hits = sum(1 for l in judgeable if l == "hit")
    return {
        "judgeable": len(judgeable),
        "hits": hits,
        "hit_rate": (hits / len(judgeable)) if judgeable else None,
        "excluded": len(labels) - len(judgeable),
    }


# ---------------------------------------------------------------------------
# Capture (pure)
# ---------------------------------------------------------------------------


def capture_decision_context(
    decision_plan: dict[str, Any],
    *,
    run_id: str,
    now: str,
    regime: str | None = None,
    crowd: dict[str, Any] | None = None,
    factor_state: dict[str, Any] | None = None,
    data_quality: str = "ok",
    snapshot_hash: str | None = None,
    strategy_id: str = "production",
    source_refs: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build immutable at-decision context records (no I/O)."""
    crowd = crowd or {}
    out: list[dict[str, Any]] = []
    for row in (decision_plan.get("decisions") or []):
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "UNKNOWN").upper()
        amount = row.get("suggested_amount")
        if amount is None:
            amount = row.get("amount") or row.get("target_weight")
        out.append({
            "run_id": run_id,
            "strategy_id": strategy_id,
            "symbol": sym,
            "action": str(row.get("decision") or "UNKNOWN").upper(),
            "amount_or_weight": amount,
            "reference_price": row.get("price"),
            "timestamp": now,
            "horizons": list(CONTRACT_HORIZONS),
            "resolved_horizons": list(RESOLVED_HORIZONS),
            # immutable decision-time context
            "regime_at_decision": regime,
            "crowd_state_at_decision": crowd.get(sym),
            "factor_state_at_decision": factor_state,
            "confidence_at_decision": row.get("confidence"),
            "data_quality_state": data_quality,
            "snapshot_hash": snapshot_hash,
            "source_refs": list(source_refs or ["outputs/latest/decision_plan.json"]),
            # outcome fields filled later by maturation — never overwrite context
            "resolved": False,
        })
    return out


# ---------------------------------------------------------------------------
# Append-only persistence (rule 6: decision-time evidence never overwritten)
# ---------------------------------------------------------------------------


def write_decision_context(root: Path | str, records: list[dict[str, Any]]) -> Path:
    """Append context records; idempotent per run_id (a run already captured is
    not re-appended). Append-only — existing rows are never rewritten."""
    root = Path(root)
    base = str(root / "outputs")
    path = get_output_path(OutputNamespace.POLICY, "decision_context_log.jsonl", base_dir=base)
    existing_lines: list[str] = []
    seen_runs: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            existing_lines.append(line)
            try:
                seen_runs.add(json.loads(line).get("run_id"))
            except Exception:
                pass
    fresh = [r for r in records if r.get("run_id") not in seen_runs]
    if not fresh:
        return path
    all_lines = existing_lines + [json.dumps(r, default=str) for r in fresh]
    safe_write_text(OutputNamespace.POLICY, "decision_context_log.jsonl",
                    "\n".join(all_lines) + "\n", base_dir=base)
    return path


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:
        return None


def run_decision_context_capture(root: Path | str = ".", now: str | None = None) -> dict[str, Any]:
    """Capture today's at-decision context from the live decision plan + the
    frozen Phase 2 snapshot + regime/crowd artifacts. Never raises."""
    from datetime import datetime, timezone
    from portfolio_automation.run_manifest import read_manifest
    from portfolio_automation.daily_input_snapshot import read_input_snapshot

    root = Path(root)
    now = now or datetime.now(timezone.utc).isoformat()
    manifest = read_manifest(root) or {}
    run_id = manifest.get("run_id") or f"{now[:10]}_daily_official"

    plan = _read_json(root / "outputs" / "latest" / "decision_plan.json") or {}
    snap = read_input_snapshot(root) or {}

    regime_doc = _read_json(root / "outputs" / "regime" / "regime_performance.json") or {}
    by_regime = regime_doc.get("by_regime") or {}
    regime = next(iter(by_regime), None) if by_regime else regime_doc.get("current_regime")

    crowd_doc = _read_json(root / "outputs" / "latest" / "unified_crowd_intelligence.json") or {}
    crowd: dict[str, Any] = {}
    for rec in (crowd_doc.get("tickers") or crowd_doc.get("records") or []):
        if isinstance(rec, dict) and rec.get("symbol"):
            crowd[str(rec["symbol"]).upper()] = rec.get("crowd_state") or rec.get("state")

    records = capture_decision_context(
        plan, run_id=run_id, now=now, regime=regime, crowd=crowd,
        factor_state=None, data_quality="ok",
        snapshot_hash=snap.get("snapshot_hash"))
    try:
        write_decision_context(root, records)
    except Exception:
        pass
    return {"run_id": run_id, "captured": len(records),
            "snapshot_hash": snap.get("snapshot_hash")}
