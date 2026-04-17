"""
Shadow-mode comparison report for scraped intelligence.

Computes hypothetical "enriched" signal and confidence scores by blending
scraped soft signals into the baseline watchlist scores, then reports per-symbol
deltas and ranking changes.

IMPORTANT — contamination guard
--------------------------------
This module is strictly read-only with respect to WatchlistRow data.  It never
modifies ``signal_score``, ``confidence_score``, or any existing field on a
result row.  All enriched values live exclusively in ComparisonRow outputs and
the two artifact files written to disk.

Usage
-----
Enabled by setting ``scraped_intel.comparison_mode: true`` in config.json.
Produces two files in the output directory:
    scraped_intel_comparison.json   — full structured report
    scraped_intel_comparison.md     — human-readable table + top-mover section

Blend model
-----------
soft_composite (in [0, 1]) is a weighted sum of four normalised soft features:

    scraped_confidence      × 0.40  (overall evidence quality)
    recency_score           × 0.30  (freshness of evidence)
    theme_alignment_score   × 0.20  (match with known investment themes)
    mention_accel_norm      × 0.10  (acceleration in [0,1], centred at 0.5)

Enriched scores:
    enriched_signal_score     = min(1.0, baseline + soft_composite × max_signal_boost)
    enriched_confidence_score = min(1.0, baseline + scraped_confidence × max_conf_boost)

All boost caps are configurable via ``comparison_max_signal_boost`` and
``comparison_max_conf_boost`` in the scraped_intel config section.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from scraped_intel.models import IntelBundle, SoftSignals

logger = logging.getLogger("scraped_intel.comparison")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

#: Weights for each soft feature in the composite.  Must sum to 1.0.
_DEFAULT_BLEND_WEIGHTS: dict[str, float] = {
    "scraped_confidence":    0.40,
    "recency_score":         0.30,
    "theme_alignment_score": 0.20,
    "mention_accel_norm":    0.10,
}

#: Maximum uplift applied to signal_score when soft_composite = 1.0.
_DEFAULT_MAX_SIGNAL_BOOST: float = 0.12

#: Maximum uplift applied to confidence_score when scraped_confidence = 1.0.
_DEFAULT_MAX_CONF_BOOST: float = 0.10


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ComparisonRow:
    """Per-symbol shadow comparison result."""

    symbol: str

    # Scores
    baseline_signal_score: float
    enriched_signal_score: float
    signal_delta: float

    baseline_confidence_score: float
    enriched_confidence_score: float
    confidence_delta: float

    # Ranking
    baseline_rank: int
    enriched_rank: int
    rank_change: int        # positive → moved up (improved position)

    # Soft signal summary
    soft_composite: float   # [0, 1] — weighted blend before boost
    top_features: list[dict]  # [{feature, value, weight, contribution}, ...]

    # Evidence provenance
    source_count: int
    evidence_count: int     # headline_count_30d
    scraped_confidence: float
    soft_signals_available: bool

    def to_dict(self) -> dict:
        return {
            "symbol":                   self.symbol,
            "baseline_signal_score":    self.baseline_signal_score,
            "enriched_signal_score":    self.enriched_signal_score,
            "signal_delta":             self.signal_delta,
            "baseline_confidence_score": self.baseline_confidence_score,
            "enriched_confidence_score": self.enriched_confidence_score,
            "confidence_delta":         self.confidence_delta,
            "baseline_rank":            self.baseline_rank,
            "enriched_rank":            self.enriched_rank,
            "rank_change":              self.rank_change,
            "soft_composite":           self.soft_composite,
            "top_features":             self.top_features,
            "source_count":             self.source_count,
            "evidence_count":           self.evidence_count,
            "scraped_confidence":       self.scraped_confidence,
            "soft_signals_available":   self.soft_signals_available,
        }


# ---------------------------------------------------------------------------
# Soft composite computation (pure function, no I/O)
# ---------------------------------------------------------------------------

def _compute_soft_composite(
    signals: SoftSignals,
    weights: Optional[dict[str, float]] = None,
) -> tuple[float, list[dict]]:
    """
    Weighted composite of four normalised soft features.

    mention_acceleration is in [-1, +1]; it is re-centred to [0, 1] before
    weighting so that accelerating coverage adds positive weight and fading
    coverage adds a mild reduction.

    Returns:
        (composite_score, feature_list)

        composite_score  — float in [0, 1]
        feature_list     — top-3 contributors, each dict with keys:
                           feature, value, weight, contribution
    """
    w = weights or _DEFAULT_BLEND_WEIGHTS

    # Normalise mention_acceleration from [-1, +1] → [0, 1]
    accel_norm = (signals.mention_acceleration + 1.0) / 2.0

    raw_values = {
        "scraped_confidence":    signals.scraped_confidence,
        "recency_score":         signals.recency_score,
        "theme_alignment_score": signals.theme_alignment_score,
        "mention_accel_norm":    accel_norm,
    }

    features: list[dict] = []
    total = 0.0
    for feat, weight in w.items():
        val = raw_values.get(feat, 0.0)
        contrib = round(val * weight, 4)
        total += contrib
        features.append({
            "feature":      feat,
            "value":        round(val, 4),
            "weight":       weight,
            "contribution": contrib,
        })

    features.sort(key=lambda x: x["contribution"], reverse=True)
    composite = round(min(1.0, max(0.0, total)), 4)
    return composite, features[:3]


# ---------------------------------------------------------------------------
# Core comparison computation (pure function, no I/O)
# ---------------------------------------------------------------------------

def compute_comparison(
    scan_results: list[dict],
    bundles: dict[str, IntelBundle],
    max_signal_boost: float = _DEFAULT_MAX_SIGNAL_BOOST,
    max_conf_boost: float = _DEFAULT_MAX_CONF_BOOST,
    blend_weights: Optional[dict[str, float]] = None,
) -> list[ComparisonRow]:
    """
    Compute enriched scores and rank changes for all scanned symbols.

    Args:
        scan_results:     List of WatchlistRow dicts from ``scanner.run()``.
        bundles:          Dict of ``symbol → IntelBundle`` from
                          ``run_scraped_intel()``.  Missing symbols → no boost.
        max_signal_boost: Maximum signal_score increase (when soft_composite=1).
        max_conf_boost:   Maximum confidence_score increase (when scraped_conf=1).
        blend_weights:    Optional override for feature blend weights.

    Returns:
        List of ComparisonRow sorted by |signal_delta| descending, then symbol.
        Enriched ranks are assigned before sorting.
    """
    if not scan_results:
        return []

    # ── Baseline ranks by signal_score descending ───────────────────────────
    sorted_baseline = sorted(
        scan_results,
        key=lambda r: float(r.get("signal_score") or 0.0),
        reverse=True,
    )
    baseline_rank_map: dict[str, int] = {}
    for i, r in enumerate(sorted_baseline):
        sym = (r.get("ticker") or "").upper()
        if sym:
            baseline_rank_map[sym] = i + 1

    # ── Per-symbol enrichment ────────────────────────────────────────────────
    rows: list[ComparisonRow] = []

    for r in scan_results:
        symbol = (r.get("ticker") or "").upper()
        if not symbol:
            continue

        baseline_sig  = round(float(r.get("signal_score") or 0.0), 4)
        baseline_conf = round(float(r.get("confidence_score") or 0.0), 4)
        baseline_rank = baseline_rank_map.get(symbol, 0)

        bundle  = bundles.get(symbol)
        signals: Optional[SoftSignals] = bundle.signals if bundle else None

        if signals and signals.scraped_confidence > 0:
            soft_composite, top_features = _compute_soft_composite(
                signals, blend_weights
            )
            enriched_sig  = round(
                min(1.0, baseline_sig  + soft_composite * max_signal_boost), 4
            )
            enriched_conf = round(
                min(1.0, baseline_conf + signals.scraped_confidence * max_conf_boost), 4
            )
            source_count   = signals.source_count
            evidence_count = signals.headline_count_30d
            scraped_conf   = round(signals.scraped_confidence, 4)
            has_signals    = True
        else:
            soft_composite = 0.0
            top_features   = []
            enriched_sig   = baseline_sig
            enriched_conf  = baseline_conf
            source_count   = 0
            evidence_count = 0
            scraped_conf   = 0.0
            has_signals    = False

        rows.append(ComparisonRow(
            symbol=symbol,
            baseline_signal_score=baseline_sig,
            enriched_signal_score=enriched_sig,
            signal_delta=round(enriched_sig - baseline_sig, 4),
            baseline_confidence_score=baseline_conf,
            enriched_confidence_score=enriched_conf,
            confidence_delta=round(enriched_conf - baseline_conf, 4),
            baseline_rank=baseline_rank,
            enriched_rank=0,    # filled below
            rank_change=0,      # filled below
            soft_composite=soft_composite,
            top_features=top_features,
            source_count=source_count,
            evidence_count=evidence_count,
            scraped_confidence=scraped_conf,
            soft_signals_available=has_signals,
        ))

    # ── Assign enriched ranks ────────────────────────────────────────────────
    sorted_enriched = sorted(
        rows, key=lambda row: row.enriched_signal_score, reverse=True
    )
    for i, row in enumerate(sorted_enriched):
        row.enriched_rank = i + 1

    # ── Rank change: positive = moved up (lower rank number = better) ────────
    for row in rows:
        row.rank_change = row.baseline_rank - row.enriched_rank

    # ── Sort output: largest |delta| first, then alphabetical ───────────────
    rows.sort(key=lambda r: (-abs(r.signal_delta), r.symbol))
    return rows


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------

def write_comparison_json(
    rows: list[ComparisonRow],
    output_dir: Path,
    config: Optional[dict] = None,
    max_signal_boost: float = _DEFAULT_MAX_SIGNAL_BOOST,
    max_conf_boost: float = _DEFAULT_MAX_CONF_BOOST,
) -> Path:
    """Write full comparison data as JSON to ``output_dir``."""
    n_with_signals  = sum(1 for r in rows if r.soft_signals_available)
    n_rank_changed  = sum(1 for r in rows if r.rank_change != 0)
    max_sig_delta   = max((r.signal_delta for r in rows), default=0.0)

    report = {
        "generated_at":              datetime.now().isoformat(),
        "mode":                      "shadow_comparison",
        "blend_weights":             _DEFAULT_BLEND_WEIGHTS,
        "max_signal_boost":          max_signal_boost,
        "max_conf_boost":            max_conf_boost,
        "symbols_total":             len(rows),
        "symbols_with_soft_signals": n_with_signals,
        "symbols_rank_changed":      n_rank_changed,
        "max_signal_delta":          round(max_sig_delta, 4),
        "comparison":                [r.to_dict() for r in rows],
    }

    path = output_dir / "scraped_intel_comparison.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    logger.info(
        "scraped_intel_comparison.json written (%d rows, %d with signals, "
        "%d rank changes, max delta %.4f)",
        len(rows), n_with_signals, n_rank_changed, max_sig_delta,
    )
    return path


def write_comparison_md(
    rows: list[ComparisonRow],
    output_dir: Path,
    max_signal_boost: float = _DEFAULT_MAX_SIGNAL_BOOST,
    max_conf_boost: float = _DEFAULT_MAX_CONF_BOOST,
) -> Path:
    """Write human-readable comparison table + top-movers to ``output_dir``."""
    lines = [
        "# Scraped Intelligence — Shadow Comparison Report",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
        "Blend weights: `scraped_confidence×0.40` · `recency×0.30` · "
        "`theme_alignment×0.20` · `mention_accel×0.10`  ",
        f"Max signal boost: `{max_signal_boost:+.2f}` · "
        f"Max confidence boost: `{max_conf_boost:+.2f}`  ",
        "",
        "> Symbols marked **✓** have scraped soft-signal data.  "
        "Unmarked symbols are unchanged from baseline.",
        "",
        "## Score & Rank Deltas",
        "",
        "| Symbol | Base Sig | Enr Sig | Δ Sig | Base Conf | Enr Conf | Δ Conf | Rank Δ | Evidence |",
        "|--------|---------|--------|------|----------|---------|-------|--------|---------|",
    ]

    for r in rows:
        mark     = " ✓" if r.soft_signals_available else ""
        sig_d    = f"{r.signal_delta:+.4f}" if r.signal_delta != 0 else "—"
        conf_d   = f"{r.confidence_delta:+.4f}" if r.confidence_delta != 0 else "—"
        rank_d   = f"{r.rank_change:+d}" if r.rank_change != 0 else "—"
        evidence = f"{r.evidence_count} art / {r.source_count} src"
        lines.append(
            f"| **{r.symbol}**{mark} "
            f"| {r.baseline_signal_score:.4f} | {r.enriched_signal_score:.4f} | {sig_d} "
            f"| {r.baseline_confidence_score:.4f} | {r.enriched_confidence_score:.4f} | {conf_d} "
            f"| {rank_d} | {evidence} |"
        )

    # ── Top signal movers ────────────────────────────────────────────────────
    movers = [r for r in rows if r.signal_delta > 0]
    if movers:
        lines += ["", "## Top Signal Movers", ""]
        for r in movers[:5]:
            rank_tag = f"rank {r.baseline_rank}→{r.enriched_rank} ({r.rank_change:+d})"
            lines.append(
                f"### {r.symbol} &nbsp; "
                f"`+{r.signal_delta:.4f}` signal · {rank_tag}"
            )
            lines.append(
                f"- Soft composite: `{r.soft_composite:.4f}` · "
                f"Evidence: {r.evidence_count} articles, {r.source_count} sources · "
                f"Scraped confidence: `{r.scraped_confidence:.4f}`"
            )
            if r.top_features:
                lines.append("- Top drivers:")
                for feat in r.top_features:
                    lines.append(
                        f"  - `{feat['feature']}` = {feat['value']:.4f} "
                        f"→ contributes `{feat['contribution']:.4f}` "
                        f"(weight {feat['weight']})"
                    )
            lines.append("")

    # ── Symbols with no soft data ────────────────────────────────────────────
    no_data = [r.symbol for r in rows if not r.soft_signals_available]
    if no_data:
        lines += [
            "## No Soft Signal Data",
            "",
            f"Scores unchanged for {len(no_data)} symbol(s): "
            + ", ".join(f"`{s}`" for s in sorted(no_data)),
            "",
        ]

    lines.append(
        "_This report is generated in shadow mode.  "
        "No live signal_score or confidence_score fields were modified._"
    )

    path = output_dir / "scraped_intel_comparison.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("scraped_intel_comparison.md written")
    return path


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def run_comparison(
    scan_results: list[dict],
    bundles: dict[str, IntelBundle],
    output_dir: str | Path,
    config: Optional[dict] = None,
) -> list[ComparisonRow]:
    """
    Full shadow-comparison pipeline: compute enriched scores → write reports.

    Args:
        scan_results: ``result["results"]`` list from ``scanner.run()``.
        bundles:      Return value of ``run_scraped_intel()``.
        output_dir:   Directory to write reports into (created if absent).
        config:       ``scraped_intel`` config dict.  Reads keys:
                      ``comparison_max_signal_boost`` (default 0.12) and
                      ``comparison_max_conf_boost`` (default 0.10).

    Returns:
        List of ComparisonRow (sorted by |signal_delta| desc).
        Also writes ``scraped_intel_comparison.json`` and
        ``scraped_intel_comparison.md`` to ``output_dir``.
    """
    cfg = config or {}
    max_signal_boost = float(
        cfg.get("comparison_max_signal_boost", _DEFAULT_MAX_SIGNAL_BOOST)
    )
    max_conf_boost = float(
        cfg.get("comparison_max_conf_boost", _DEFAULT_MAX_CONF_BOOST)
    )

    rows = compute_comparison(
        scan_results=scan_results,
        bundles=bundles,
        max_signal_boost=max_signal_boost,
        max_conf_boost=max_conf_boost,
    )

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    write_comparison_json(
        rows, out,
        config=cfg,
        max_signal_boost=max_signal_boost,
        max_conf_boost=max_conf_boost,
    )
    write_comparison_md(
        rows, out,
        max_signal_boost=max_signal_boost,
        max_conf_boost=max_conf_boost,
    )

    # ── Optional outcome-tracking persistence ────────────────────────────────
    if cfg.get("comparison_outcome_tracking", False):
        try:
            from scraped_intel.store import ScrapedIntelStore
            _db_path = cfg.get("comparison_outcome_db_path", "data/portfolio.db")
            _windows = list(cfg.get("comparison_outcome_windows", [1, 5, 20]))
            _as_of = datetime.now().strftime("%Y-%m-%d")
            _store = ScrapedIntelStore(db_path=_db_path)
            _ids = _store.save_comparison_snapshots(
                row_dicts=[r.to_dict() for r in rows],
                as_of_date=_as_of,
                windows=_windows,
            )
            logger.info(
                "comparison outcome tracking: persisted %d snapshots for %s "
                "(windows=%s, db=%s)",
                len(_ids), _as_of, _windows, _db_path,
            )
        except Exception as _oc_err:
            logger.warning(
                "comparison outcome tracking: non-fatal persistence error — %s", _oc_err
            )

    n_with  = sum(1 for r in rows if r.soft_signals_available)
    n_moved = sum(1 for r in rows if r.rank_change != 0)
    logger.info(
        "Comparison complete: %d symbols, %d with soft signals, "
        "%d rank changes, max Δsig=%.4f",
        len(rows), n_with, n_moved,
        max((r.signal_delta for r in rows), default=0.0),
    )
    return rows
