"""
Retune Suggestions — consume pattern_efficacy outputs and propose
sanitation weight rebalances + ExtendedWatchlist promotion gate
adjustments. Each suggestion is annotated `auto_applicable: bool` per
guardrails defined here; an external apply step (auto_apply.py) reads
the artifact and acts only on auto_applicable=true rows.

Hard guarantees:
  - observe_only=True hardcoded for the artifact itself
  - This module never mutates config.json — it only writes suggestions.
    The separate auto_apply module is the only mutator and respects
    audit + magnitude + sample-size + 2-run-confirmation guardrails.
  - Suggestions degrade safely when efficacy inputs are missing.

Public API:
  build_retune_suggestions(root, *, efficacy_payload=None) -> dict
  run_retune_suggestions(root, write_files=True) -> dict

Guardrails (see module-level constants):
  - Auto-applicable weight changes: |Δw| ≤ 0.03 AND n ≥ 200
  - Auto-applicable threshold changes: |Δθ| ≤ 0.05 AND n ≥ 200
  - 2-consecutive-run confirmation required (tracked in state file)
  - Monthly cap on cumulative drift: ±0.25 for any single parameter
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
)

logger = logging.getLogger("stockbot.portfolio_automation.retune_suggestions")

_SCHEMA_VERSION = "1"
_SOURCE_LABEL = "retune_suggestions"
_OBSERVE_ONLY = True

# Auto-applicability guardrails
_AUTO_APPLY_WEIGHT_MAX_DELTA = 0.03
_AUTO_APPLY_THRESHOLD_MAX_DELTA = 0.05
_AUTO_APPLY_MIN_N = 200
_AUTO_APPLY_MONTHLY_DRIFT_CAP = 0.25     # cumulative |Δ| per param per month

# Suggestion magnitudes — how big a Δ the engine proposes per pp of efficacy delta
_WEIGHT_DELTA_PER_PP = 0.005             # 1pp efficacy delta → 0.5% weight shift
_THRESHOLD_DELTA_PER_PP = 0.005

_DISCLAIMER = (
    "Observe-only retune suggestion artifact. Recommends weight and gate "
    "adjustments based on per-tag efficacy. Auto-applicable rows are "
    "subject to magnitude + sample-size + 2-run-confirmation guardrails; "
    "the apply step is logged for audit and reversible."
)


# ---------------------------------------------------------------------------
# Suggestion builders — pure
# ---------------------------------------------------------------------------


def _load_efficacy(root: Path, cadence: str = "monthly") -> dict[str, Any] | None:
    """Read pattern_efficacy_<cadence>.json. Prefer monthly for proposals
    (balances signal vs noise)."""
    p = root / "outputs" / "latest" / f"pattern_efficacy_{cadence}.json"
    if not p.exists():
        # Fall back to weekly if monthly hasn't run
        p = root / "outputs" / "latest" / "pattern_efficacy_weekly.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


_SOURCE_TAG_TO_WEIGHT_KEY = {
    "source:static":             None,            # no direct weight; baseline
    "source:extended_watchlist": None,
    "source:theme_candidate":    "theme",
    "source:recent_signal":      "hit_rate",
    "source:fmp_top100":         "fmp",
    "multi_source_confluence":   "sources",
}


_CURRENT_WEIGHTS = {
    "sources":  0.40,
    "theme":    0.30,
    "hit_rate": 0.20,
    "fmp":      0.10,
}


def _propose_weight_changes(by_tag: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate per-tag efficacy deltas into proposed score-weight changes."""
    proposals: list[dict[str, Any]] = []
    for tag, weight_key in _SOURCE_TAG_TO_WEIGHT_KEY.items():
        if not weight_key:
            continue
        stats = by_tag.get(tag) or {}
        delta_pp = stats.get("vs_baseline_pp")
        n = stats.get("n_samples", 0)
        if delta_pp is None or n == 0:
            continue
        proposed_delta = round(delta_pp * _WEIGHT_DELTA_PER_PP, 4)
        current = _CURRENT_WEIGHTS.get(weight_key, 0.0)
        proposed = round(max(0.0, min(1.0, current + proposed_delta)), 4)
        auto_applicable = (
            abs(proposed_delta) <= _AUTO_APPLY_WEIGHT_MAX_DELTA
            and n >= _AUTO_APPLY_MIN_N
            and stats.get("significance") not in (None, "insufficient_sample")
        )
        proposals.append({
            "parameter": f"sanitation_weight.{weight_key}",
            "source_tag": tag,
            "current_value": current,
            "proposed_value": proposed,
            "delta": proposed_delta,
            "n_samples": n,
            "evidence_delta_pp": delta_pp,
            "significance": stats.get("significance"),
            "auto_applicable": auto_applicable,
            "rationale": (
                f"Tag {tag} carried Δ {delta_pp:+.1f}pp vs baseline over n={n} "
                f"samples ({stats.get('significance')}). Proposed weight shift "
                f"{current:.3f} → {proposed:.3f} (Δ {proposed_delta:+.4f})."
            ),
        })
    return proposals


def _propose_promotion_gate(by_tag: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Translate efficacy of `promoted_to_extended` + `high_theme_confidence`
    into a proposed confidence_threshold adjustment."""
    promo = by_tag.get("promoted_to_extended") or {}
    high_conf = by_tag.get("high_theme_confidence") or {}

    current_threshold = 0.80  # config.json default
    proposal: dict[str, Any] = {
        "parameter": "extended_watchlist.confidence_threshold",
        "current_value": current_threshold,
        "proposed_value": current_threshold,
        "delta": 0.0,
        "auto_applicable": False,
        "rationale": "Insufficient samples; no change recommended.",
    }

    # If high_theme_confidence outperforms baseline AND has good n: raise threshold
    delta_pp = high_conf.get("vs_baseline_pp")
    n = high_conf.get("n_samples", 0)
    if delta_pp is not None and n >= 50:
        proposed_delta = round(delta_pp * _THRESHOLD_DELTA_PER_PP, 4)
        # Clamp to a sensible range
        proposed_delta = max(-0.10, min(0.10, proposed_delta))
        proposed = round(max(0.50, min(0.95, current_threshold + proposed_delta)), 4)
        proposal.update({
            "proposed_value": proposed,
            "delta": round(proposed - current_threshold, 4),
            "n_samples": n,
            "evidence_delta_pp": delta_pp,
            "significance": high_conf.get("significance"),
            "auto_applicable": (
                abs(proposed - current_threshold) <= _AUTO_APPLY_THRESHOLD_MAX_DELTA
                and n >= _AUTO_APPLY_MIN_N
                and high_conf.get("significance") not in (None, "insufficient_sample")
            ),
            "rationale": (
                f"high_theme_confidence carries Δ {delta_pp:+.1f}pp vs baseline "
                f"over n={n} samples. Proposed threshold "
                f"{current_threshold:.2f} → {proposed:.2f}. "
                f"({high_conf.get('significance')})"
            ),
        })

    # Sanity: if promoted_to_extended itself is a strong loser, hold threshold up
    promo_sig = promo.get("significance")
    if promo_sig in ("loser", "strong_loser") and promo.get("n_samples", 0) >= 30:
        proposal["caveat"] = (
            f"promoted_to_extended is classified {promo_sig} "
            f"({promo.get('vs_baseline_pp')}pp on n={promo.get('n_samples')}); "
            "consider tightening reinforcement gate before lowering threshold."
        )

    return proposal


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------


def build_retune_suggestions(
    *,
    root: str | Path = ".",
    efficacy_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the suggestion artifact. Reads pattern_efficacy_monthly if
    no payload passed. Returns the full payload dict."""
    root_path = Path(root).resolve()
    ts = datetime.now(timezone.utc).isoformat()

    payload = efficacy_payload or _load_efficacy(root_path, cadence="monthly")
    if not payload:
        return {
            "generated_at": ts,
            "observe_only": _OBSERVE_ONLY,
            "schema_version": _SCHEMA_VERSION,
            "source": _SOURCE_LABEL,
            "available": False,
            "reason": "no_efficacy_input",
            "weight_proposals": [],
            "gate_proposal": None,
            "disclaimer": _DISCLAIMER,
        }

    by_tag = payload.get("by_tag") or {}
    weight_proposals = _propose_weight_changes(by_tag)
    gate_proposal = _propose_promotion_gate(by_tag)

    auto_applicable_count = sum(1 for p in weight_proposals if p.get("auto_applicable"))
    if gate_proposal.get("auto_applicable"):
        auto_applicable_count += 1

    return {
        "generated_at": ts,
        "observe_only": _OBSERVE_ONLY,
        "schema_version": _SCHEMA_VERSION,
        "source": _SOURCE_LABEL,
        "available": True,
        "based_on_efficacy_generated_at": payload.get("generated_at"),
        "based_on_lookback_days": payload.get("lookback_days"),
        "universe_baseline_n": (payload.get("universe_baseline") or {}).get("n_samples", 0),
        "weight_proposals": weight_proposals,
        "gate_proposal": gate_proposal,
        "auto_applicable_count": auto_applicable_count,
        "guardrails": {
            "auto_apply_weight_max_delta": _AUTO_APPLY_WEIGHT_MAX_DELTA,
            "auto_apply_threshold_max_delta": _AUTO_APPLY_THRESHOLD_MAX_DELTA,
            "auto_apply_min_n": _AUTO_APPLY_MIN_N,
            "auto_apply_monthly_drift_cap": _AUTO_APPLY_MONTHLY_DRIFT_CAP,
        },
        "disclaimer": _DISCLAIMER,
    }


def render_retune_suggestions_md(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append
    a(f"# Retune Suggestions — {payload.get('generated_at', '')[:19]}")
    a("")
    if not payload.get("available"):
        a(f"_Not available: {payload.get('reason', 'unknown')}_")
        return "\n".join(lines)
    a(f"**Based on efficacy generated at:** {payload.get('based_on_efficacy_generated_at')}  ")
    a(f"**Lookback:** {payload.get('based_on_lookback_days', '?')} days  ")
    a(f"**Universe baseline n:** {payload.get('universe_baseline_n', 0)}  ")
    a(f"**Auto-applicable suggestions:** {payload.get('auto_applicable_count', 0)}")
    a("")
    a(f"> {payload.get('disclaimer', _DISCLAIMER)}")
    a("")
    a("## Weight proposals")
    a("")
    wp = payload.get("weight_proposals") or []
    if wp:
        a("| Parameter | Current | Proposed | Δ | n | Δ vs baseline | Significance | Auto-apply? |")
        a("|---|---|---|---|---|---|---|---|")
        for p in wp:
            edpp = p.get("evidence_delta_pp")
            edpp_str = f"{edpp:+.1f}pp" if edpp is not None else "—"
            auto = "✓" if p.get("auto_applicable") else "—"
            a(
                f"| `{p['parameter']}` | {p['current_value']:.3f} | {p['proposed_value']:.3f} | "
                f"{p['delta']:+.4f} | {p.get('n_samples', 0)} | {edpp_str} | "
                f"{p.get('significance', '—')} | {auto} |"
            )
        a("")
    else:
        a("_No weight proposals from this efficacy window._")
        a("")
    a("## Promotion gate proposal")
    a("")
    gp = payload.get("gate_proposal") or {}
    a(f"- Parameter: `{gp.get('parameter')}`")
    a(f"- Current: {gp.get('current_value')}")
    a(f"- Proposed: {gp.get('proposed_value')}")
    a(f"- Auto-applicable: {'✓' if gp.get('auto_applicable') else '—'}")
    a(f"- Rationale: {gp.get('rationale')}")
    if gp.get("caveat"):
        a(f"- ⚠ Caveat: {gp['caveat']}")
    a("")
    a("---")
    a("_Observe-only suggestion artifact. Auto-apply step is gated externally._")
    return "\n".join(lines)


def run_retune_suggestions(
    *,
    root: str | Path = ".",
    write_files: bool = True,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    try:
        payload = build_retune_suggestions(root=root_path)
        artifacts: dict[str, str] = {}
        if write_files:
            safe_write_json(
                OutputNamespace.LATEST,
                "gate_retune_suggestions.json",
                payload,
                base_dir=root_path / "outputs",
            )
            safe_write_text(
                OutputNamespace.LATEST,
                "gate_retune_suggestions.md",
                render_retune_suggestions_md(payload),
                base_dir=root_path / "outputs",
            )
            artifacts = {
                "gate_retune_suggestions_json": str(root_path / "outputs" / "latest" / "gate_retune_suggestions.json"),
                "gate_retune_suggestions_md": str(root_path / "outputs" / "latest" / "gate_retune_suggestions.md"),
            }
        return {
            "status": "ok",
            "available": payload.get("available", False),
            "auto_applicable_count": payload.get("auto_applicable_count", 0),
            "artifacts": artifacts,
        }
    except Exception as exc:
        logger.error("retune_suggestions: %s", exc, exc_info=True)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    import sys
    r = run_retune_suggestions(root=Path(__file__).resolve().parents[1])
    print(
        f"retune_suggestions: status={r.get('status')} "
        f"available={r.get('available')} "
        f"auto_applicable={r.get('auto_applicable_count', 0)}"
    )
    sys.exit(0 if r.get("status") == "ok" else 1)
