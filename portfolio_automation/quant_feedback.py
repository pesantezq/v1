"""Phase 5 — quant feedback attribution (observe-only).

Joins the Phase 4 decision-time context (regime / crowd-state / strategy at
decision) with matured outcomes and attributes performance by dimension using
the standardized taxonomy (Phase 4) + honest denominators + sample sufficiency.

Produces EVIDENCE only — it never changes confidence scores, weights, or any
production state (Iron rule 3). Insufficient evidence is reported distinctly
from poor performance. Cost-adjusted return + MAE/MFE are declared by contract
and computed only where the source supports them (else null/insufficient — no
fabrication).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.next_stage.contracts import observe_only_envelope
from portfolio_automation.decision_context_capture import (
    classify_outcome, is_counted,
)

_MIN_N_SUFFICIENT = 30
_DIMENSIONS = {
    "by_regime": "regime_at_decision",
    "by_crowd_state": "crowd_state_at_decision",
    "by_strategy": "strategy_id",
    "by_action": "action",
}

__all__ = ["attribute_outcomes", "build_quant_feedback", "run_quant_feedback"]


def attribute_outcomes(
    context_records: list[dict[str, Any]],
    outcome_map: dict[str, float | None],
    *,
    dimension: str,
    neutral_band_pct: float = 1.0,
) -> dict[str, dict[str, Any]]:
    """Group classified outcomes by ``dimension`` -> per-group stats. Pure.

    ``outcome_map`` maps symbol -> resolution return_pct (percent units) or None
    (unresolved). Missing dimension values route to an ``"unknown"`` bucket.
    Only hit/miss enter the hit-rate denominator.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for rec in context_records:
        key = rec.get(dimension) or "unknown"
        groups.setdefault(str(key), []).append(rec)

    out: dict[str, dict[str, Any]] = {}
    for key, recs in groups.items():
        labels, returns = [], []
        for rec in recs:
            sym = str(rec.get("symbol") or "").upper()
            ret = outcome_map.get(sym)
            label = classify_outcome(
                rec.get("action") or "", ret, resolved=ret is not None,
                data_quality=rec.get("data_quality_state", "ok"),
                neutral_band_pct=neutral_band_pct)
            labels.append(label)
            if label in ("hit", "miss") and ret is not None:
                returns.append(ret)
        judgeable = [l for l in labels if is_counted(l)]
        hits = sum(1 for l in judgeable if l == "hit")
        out[key] = {
            "n_samples": len(recs),
            "judgeable": len(judgeable),
            "hits": hits,
            "hit_rate": (hits / len(judgeable)) if judgeable else None,
            "mean_return": round(sum(returns) / len(returns), 4) if returns else None,
            "neutral": sum(1 for l in labels if l == "neutral"),
            "unresolved": sum(1 for l in labels if l == "unresolved"),
            "insufficient_data": sum(1 for l in labels if l == "insufficient_data"),
            "invalidated": sum(1 for l in labels if l == "invalidated"),
            "sample_sufficient": len(judgeable) >= _MIN_N_SUFFICIENT,
            # declared-by-contract, computed only where source supports (else null)
            "mae": None,
            "mfe": None,
            "cost_adjusted_mean_return": None,
        }
    return out


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except Exception:
        pass
    return rows


def _outcome_map(root: Path) -> dict[str, float | None]:
    """Best-effort symbol -> 1d resolution return (percent) from matured rows."""
    rows = _read_jsonl(root / "outputs" / "performance" / "decision_outcomes.jsonl")
    out: dict[str, float | None] = {}
    for r in rows:
        sym = str(r.get("symbol") or "").upper()
        if not sym:
            continue
        ret = r.get("return_pct") if r.get("resolved") else None
        # decision_outcomes return_pct is a decimal fraction -> percent
        if isinstance(ret, (int, float)):
            out[sym] = round(ret * 100.0, 4)
        else:
            out.setdefault(sym, None)
    return out


def build_quant_feedback(root: Path | str, *, now: str | None = None) -> dict[str, Any]:
    """Read the Phase 4 context log + matured outcomes, attribute by every
    dimension. Never raises; degrades to insufficient evidence honestly."""
    root = Path(root)
    now = now or datetime.now(timezone.utc).isoformat()
    ctx = _read_jsonl(root / "outputs" / "policy" / "decision_context_log.jsonl")
    outcomes = _outcome_map(root)

    payload: dict[str, Any] = dict(observe_only_envelope(now))
    payload["source"] = "quant_feedback"
    payload["schema_version"] = "1"
    for out_key, dim in _DIMENSIONS.items():
        payload[out_key] = attribute_outcomes(ctx, outcomes, dimension=dim)

    n_ctx = len(ctx)
    n_resolved = sum(1 for v in outcomes.values() if v is not None)
    fallback = sum(1 for r in ctx
                   if str(r.get("data_quality_state", "ok")).lower()
                   not in ("ok", "fresh"))
    payload["n_context_records"] = n_ctx
    payload["n_resolved_outcomes"] = n_resolved
    payload["fallback_rate"] = round(fallback / n_ctx, 4) if n_ctx else 0.0
    payload["evidence_status"] = "ok" if n_resolved >= _MIN_N_SUFFICIENT else "insufficient"
    payload["disclaimer"] = (
        "Observe-only quant evidence. Attributes matured outcomes to decision-time "
        "regime/crowd/strategy/action using the Phase 4 taxonomy. Produces evidence "
        "+ proposals only; never changes confidence, weights, or any production state."
    )
    return payload


def run_quant_feedback(root: Path | str = ".", now: str | None = None) -> dict[str, Any]:
    root = Path(root)
    payload = build_quant_feedback(root, now=now)
    try:
        safe_write_json(OutputNamespace.LATEST, "quant_feedback.json", payload,
                        base_dir=str(root / "outputs"))
    except Exception:
        pass
    return payload
