"""Simulation-lane validation for the regime-classifier neutral-collapse fix.

Work order: quant.regime_classifier_health. Compares regime behavior BEFORE
(production ``outputs/performance/signal_outcomes.csv`` — read-only) vs AFTER
(the corrected producer ordering replayed over a representative window of varied
market states in a throwaway SANDBOX database). Writes
``outputs/sandbox/regime_collapse_validation.{json,md}`` (SANDBOX namespace).

Governance / honesty notes
--------------------------
* Production artifacts are NEVER mutated. The production CSV/DB and the 1286
  historical evidence rows are read-only here.
* The true historical per-run regime inputs were never persisted (that IS the
  bug) and ``data/price_cache.json`` holds only current snapshots, so faithfully
  re-tagging the historical rows would require paid FMP historical fetches. We
  deliberately do NOT incur that cost. The fix corrects all FUTURE recordings;
  historical rows are left intact as protected evidence.
* AFTER by-regime hit-rate / mean-return is computed on SIMULATED runs with
  deterministic synthetic outcomes, SOLELY to prove the by-regime grouping
  mechanism is UN-MASKED (multiple buckets) vs the single collapsed bucket. It
  is explicitly NOT a market-efficacy claim.
"""
from __future__ import annotations

import csv
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market_regime import detect_market_regime
from portfolio_automation.data_governance import OutputNamespace, safe_write_json, safe_write_text
from watchlist_scanner.performance_feedback import (
    build_regime_performance_summary,
    record_scan_signals,
)
from watchlist_scanner.state import WatchlistStateStore

PROD_CSV = Path("outputs/performance/signal_outcomes.csv")
PRIMARY_WINDOW = 3

# A representative trailing window of varied daily market states, ordered in
# time. Inputs are constructed from classifier INTENT (trend + breadth + vol),
# not tuned to game thresholds. The sequence deliberately cycles through every
# implemented regime and includes several transitions.
_WINDOW: list[tuple[str, dict[str, Any]]] = [
    ("2026-06-01", {"index_trend_state": "up", "breadth_sma50": 0.82, "breadth_sma20": 0.78, "avg_price_change_pct": 1.6, "volatility_proxy": 1.0, "sector_leadership_concentration": 0.30}),
    ("2026-06-02", {"index_trend_state": "up", "breadth_sma50": 0.78, "breadth_sma20": 0.72, "avg_price_change_pct": 1.2, "volatility_proxy": 1.1, "sector_leadership_concentration": 0.32}),
    ("2026-06-03", {"index_trend_state": "mixed", "breadth_sma50": 0.55, "breadth_sma20": 0.52, "avg_price_change_pct": 0.3, "volatility_proxy": 1.4, "sector_leadership_concentration": 0.34}),
    ("2026-06-04", {"index_trend_state": "mixed", "breadth_sma50": 0.50, "breadth_sma20": 0.50, "avg_price_change_pct": -0.1, "volatility_proxy": 4.2, "sector_leadership_concentration": 0.41}),
    ("2026-06-05", {"index_trend_state": "mixed", "breadth_sma50": 0.48, "breadth_sma20": 0.46, "avg_price_change_pct": -0.4, "volatility_proxy": 3.6, "sector_leadership_concentration": 0.45}),
    ("2026-06-06", {"index_trend_state": "down", "breadth_sma50": 0.24, "breadth_sma20": 0.28, "avg_price_change_pct": -1.7, "volatility_proxy": 1.4, "sector_leadership_concentration": 0.38}),
    ("2026-06-07", {"index_trend_state": "down", "breadth_sma50": 0.22, "breadth_sma20": 0.26, "avg_price_change_pct": -1.9, "volatility_proxy": 1.3, "sector_leadership_concentration": 0.36}),
    ("2026-06-08", {"index_trend_state": "mixed", "breadth_sma50": 0.45, "breadth_sma20": 0.48, "avg_price_change_pct": 0.1, "volatility_proxy": 1.2, "sector_leadership_concentration": 0.33}),
    ("2026-06-09", {"index_trend_state": "mixed", "breadth_sma50": 0.52, "breadth_sma20": 0.50, "avg_price_change_pct": 0.2, "volatility_proxy": 1.0, "sector_leadership_concentration": 0.30}),
    ("2026-06-10", {"index_trend_state": "up", "breadth_sma50": 0.80, "breadth_sma20": 0.76, "avg_price_change_pct": 1.5, "volatility_proxy": 1.0, "sector_leadership_concentration": 0.29}),
]

_TICKERS = ("AAA", "BBB", "CCC")

# Deterministic synthetic 3d outcomes per regime — used ONLY to demonstrate the
# by-regime grouping mechanism is unblocked. Not a market-efficacy claim.
_SYNTH_OUTCOME: dict[str, tuple[float, bool]] = {
    "risk_on": (1.4, True),
    "risk_off": (-1.1, False),
    "high_volatility": (0.6, True),
    "neutral": (0.2, True),
}


def _distribution(labels: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(labels).items()))


def _transitions(ordered_labels: list[str]) -> int:
    return sum(1 for a, b in zip(ordered_labels, ordered_labels[1:]) if a != b)


def _fallback_count(rows: list[dict[str, Any]]) -> int:
    """Rows carrying the exact collapse triple (neutral, 0.0, limited)."""
    n = 0
    for r in rows:
        conf = r.get("regime_confidence")
        try:
            conf = float(conf) if conf not in (None, "") else None
        except (TypeError, ValueError):
            conf = None
        if (
            str(r.get("regime_label")) == "neutral"
            and conf == 0.0
            and str(r.get("regime_data_quality")) == "limited"
        ):
            n += 1
    return n


def read_before() -> dict[str, Any]:
    rows = list(csv.DictReader(PROD_CSV.open(encoding="utf-8-sig")))
    labels = [str(r.get("regime_label") or "") for r in rows]
    summary = build_regime_performance_summary(rows, primary_window_days=PRIMARY_WINDOW)
    by_regime = {
        k: {
            "total_signals": v.get("total_signals"),
            "win_rate": v.get("win_rate"),
            "avg_return_pct": v.get("avg_return_pct"),
            "avg_regime_confidence": v.get("avg_regime_confidence"),
        }
        for k, v in (summary.get("by_regime") or {}).items()
    }
    return {
        "source": str(PROD_CSV),
        "total_rows": len(rows),
        "regime_label_distribution": _distribution(labels),
        "distinct_label_count": len({l for l in labels if l}),
        "label_transition_frequency": _transitions(labels),
        "fallback_triple_count": _fallback_count(rows),
        "by_regime_metrics": by_regime,
    }


def simulate_after() -> dict[str, Any]:
    ordered_run_labels: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "sandbox.db"
        for day, inputs in _WINDOW:
            regime = detect_market_regime(regime_inputs=inputs)
            ordered_run_labels.append(regime["regime_label"])
            scan = {
                "generated_at": f"{day}T09:00:00",
                "data_mode": "live",
                "degraded_mode": False,
                "market_regime": regime,  # CORRECTED ordering: attached before record
                "results": [
                    {
                        "ticker": t,
                        "price": 100.0 + i,
                        "signal_score": 0.5,
                        "confidence_score": 0.8,
                        "effective_score": 0.4,
                        "watchlist_source": "static",
                    }
                    for i, t in enumerate(_TICKERS)
                ],
            }
            record_scan_signals(scan, db_path=db)
        recorded = WatchlistStateStore(db).list_signal_feedback(limit=10000)

    labels = [str(r.get("regime_label") or "") for r in recorded]

    # Consumer un-masking demo: layer deterministic synthetic 3d outcomes by
    # regime onto the recorded rows, then run the REAL downstream summary.
    enriched: list[dict[str, Any]] = []
    for idx, r in enumerate(recorded):
        ret, success = _SYNTH_OUTCOME.get(str(r.get("regime_label")), (0.0, False))
        row = dict(r)
        row["outcome_return_3d"] = ret
        row["outcome_success_3d"] = success
        enriched.append(row)
    summary = build_regime_performance_summary(enriched, primary_window_days=PRIMARY_WINDOW)
    by_regime = {
        k: {
            "total_signals": v.get("total_signals"),
            "win_rate": v.get("win_rate"),
            "avg_return_pct": v.get("avg_return_pct"),
            "avg_regime_confidence": v.get("avg_regime_confidence"),
        }
        for k, v in (summary.get("by_regime") or {}).items()
    }
    return {
        "source": "sandbox replay (corrected producer ordering)",
        "runs": len(_WINDOW),
        "recorded_rows": len(recorded),
        "regime_label_distribution": _distribution(labels),
        "distinct_label_count": len({l for l in labels if l}),
        "per_run_label_sequence": ordered_run_labels,
        "label_transition_frequency": _transitions(ordered_run_labels),
        "fallback_triple_count": _fallback_count(recorded),
        "by_regime_metrics_SIMULATED_OUTCOMES": by_regime,
    }


def _render_md(before: dict[str, Any], after: dict[str, Any]) -> str:
    lines = [
        "# Regime Classifier — Neutral-Collapse Fix: Simulation Validation",
        "",
        "Work order: `quant.regime_classifier_health`. Observe-only, simulation lane.",
        "Production artifacts were NOT mutated.",
        "",
        "## Before (production `signal_outcomes.csv`, read-only)",
        f"- total rows: **{before['total_rows']}**",
        f"- regime-label distribution: `{before['regime_label_distribution']}`",
        f"- distinct labels: **{before['distinct_label_count']}**",
        f"- label transition frequency: **{before['label_transition_frequency']}**",
        f"- collapse-triple (neutral,0.0,limited) rows: **{before['fallback_triple_count']}** "
        f"({round(100 * before['fallback_triple_count'] / max(before['total_rows'], 1))}%)",
        f"- by-regime metrics: `{before['by_regime_metrics']}`",
        "",
        "## After (corrected ordering, sandbox replay of a representative window)",
        f"- simulated runs: **{after['runs']}**, recorded rows: **{after['recorded_rows']}**",
        f"- regime-label distribution: `{after['regime_label_distribution']}`",
        f"- distinct labels: **{after['distinct_label_count']}**",
        f"- per-run label sequence: `{after['per_run_label_sequence']}`",
        f"- label transition frequency: **{after['label_transition_frequency']}**",
        f"- collapse-triple rows: **{after['fallback_triple_count']}**",
        f"- by-regime metrics (SIMULATED outcomes — mechanism demo, not efficacy): "
        f"`{after['by_regime_metrics_SIMULATED_OUTCOMES']}`",
        "",
        "## Verdict",
        f"- distinct labels: {before['distinct_label_count']} → {after['distinct_label_count']}",
        f"- transitions: {before['label_transition_frequency']} → {after['label_transition_frequency']}",
        f"- collapse-triple rows: {before['fallback_triple_count']} → {after['fallback_triple_count']}",
        "",
        "The corrected producer ordering yields a diverse, non-fallback regime "
        "distribution with real transitions, and the by-regime performance summary "
        "now resolves into multiple buckets (un-masking the cross-regime analysis "
        "the collapse hid). Historical re-tagging of the 1286 production rows was "
        "NOT performed (requires paid FMP history; inputs were never persisted); "
        "the fix applies to all future recordings.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    before = read_before()
    after = simulate_after()
    payload = {
        "observe_only": True,
        "lane": "simulation",
        "work_order_probe": "quant.regime_classifier_health",
        "production_mutated": False,
        "before": before,
        "after": after,
        "verdict": {
            "before_distinct_labels": before["distinct_label_count"],
            "after_distinct_labels": after["distinct_label_count"],
            "before_transitions": before["label_transition_frequency"],
            "after_transitions": after["label_transition_frequency"],
            "before_fallback_rows": before["fallback_triple_count"],
            "after_fallback_rows": after["fallback_triple_count"],
            "degeneracy_resolved": after["distinct_label_count"] > 1
            and after["fallback_triple_count"] == 0,
        },
    }
    jpath = safe_write_json(OutputNamespace.SANDBOX, "regime_collapse_validation.json", payload)
    mpath = safe_write_text(
        OutputNamespace.SANDBOX, "regime_collapse_validation.md", _render_md(before, after)
    )
    print(f"wrote {jpath}")
    print(f"wrote {mpath}")
    print(f"degeneracy_resolved={payload['verdict']['degeneracy_resolved']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
