"""
Weight tuning and auto-evaluation loop for the scraped intelligence comparison
system.

Uses historical comparison snapshots and resolved comparison outcomes to
evaluate multiple candidate blend configurations and recommend better
scraped-intel comparison weights — without changing production behaviour.

Off by default
--------------
Enable with ``scraped_intel.tuning_enabled: true`` in config.json.
All functions are additive and never mutate production WatchlistRow fields,
existing comparison snapshots, or soft_signals rows.

Tuning design
-------------
For each *candidate* configuration (a set of blend weights + boost caps):

1. Raw soft feature values are loaded from ``soft_signals`` joined to the
   resolved ``comparison_outcomes`` rows.
2. The enriched signal score is *recomputed* from scratch using the
   candidate's weights / boost caps (mirrors comparison.py exactly).
3. The recomputed ``signal_delta`` determines whether the symbol was
   "boosted" under this config (delta > 0).
4. Per-window metrics compare the boosted group's return outcomes to the
   unboosted group.  When no unboosted group exists, a neutral 50% win-rate
   baseline is used.
5. An objective score (weighted 5d + 20d win-rate lift + return lift) ranks
   the candidates.  A stability penalty discounts configs whose 5d and 20d
   results are inconsistent.
6. The top-ranked candidate is written out as a ready-to-paste config snippet.

Search space
------------
Weight grid: all 4-tuples summing to 1.0 at ``tuning_weight_step`` resolution,
each weight >= step (no zero-weight features).  For step=0.10 this is 84
combinations.

Cross-joined with ``tuning_signal_boost_grid`` × ``tuning_conf_boost_grid``
(defaults: 5 × 4 = 20 boost pairs).  Total: 84 × 20 = 1 680 candidates;
capped to ``tuning_max_candidates`` (default 500) via reproducible shuffling.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("scraped_intel.tuning")

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

_WEIGHT_KEYS: List[str] = [
    "scraped_confidence",
    "recency_score",
    "theme_alignment_score",
    "mention_accel_norm",
]

_DEFAULT_SIGNAL_BOOST_GRID: List[float] = [0.08, 0.10, 0.12, 0.14, 0.16]
_DEFAULT_CONF_BOOST_GRID:   List[float] = [0.06, 0.08, 0.10, 0.12]
_DEFAULT_WEIGHT_STEP:        float       = 0.10
_DEFAULT_MAX_CANDIDATES:     int         = 500
_DEFAULT_MIN_SAMPLE_SIZE:    int         = 10
_DEFAULT_WINDOWS:            List[int]   = [1, 5, 20]

# Importance weights for the objective function per return window.
# 1d is mostly noise; 5d and 20d carry the actionable signal.
_WINDOW_OBJ_WEIGHTS: Dict[int, float] = {1: 0.10, 5: 0.45, 20: 0.45}


# ---------------------------------------------------------------------------
# Weight grid generation
# ---------------------------------------------------------------------------

def generate_weight_grid(step: float = _DEFAULT_WEIGHT_STEP) -> List[Dict[str, float]]:
    """
    Enumerate all 4-weight combinations (one per ``_WEIGHT_KEYS`` feature)
    where:
    - each weight is a positive integer multiple of ``step``
    - weights sum to exactly 1.0

    Args:
        step: Grid step size (e.g. 0.10 → weights are multiples of 0.1).

    Returns:
        List of weight dicts keyed by ``_WEIGHT_KEYS``.
    """
    n = round(1.0 / step)   # total units to distribute across 4 features
    combos: List[Dict[str, float]] = []
    for a in range(1, n - 2):
        for b in range(1, n - a - 1):
            for c in range(1, n - a - b):
                d = n - a - b - c
                if d >= 1:
                    combos.append({
                        _WEIGHT_KEYS[0]: round(a * step, 6),
                        _WEIGHT_KEYS[1]: round(b * step, 6),
                        _WEIGHT_KEYS[2]: round(c * step, 6),
                        _WEIGHT_KEYS[3]: round(d * step, 6),
                    })
    return combos


def normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
    """
    Normalise a weight dict so values sum to 1.0.

    Raises:
        ValueError: if any weight is negative or the total is zero.
    """
    if any(v < 0 for v in weights.values()):
        raise ValueError("All weights must be non-negative")
    total = sum(weights.values())
    if total <= 0.0:
        raise ValueError(f"Weight total must be positive, got {total}")
    return {k: round(v / total, 6) for k, v in weights.items()}


def generate_candidates(
    signal_boost_grid: Optional[List[float]] = None,
    conf_boost_grid:   Optional[List[float]] = None,
    weight_step:       float = _DEFAULT_WEIGHT_STEP,
    max_candidates:    int   = _DEFAULT_MAX_CANDIDATES,
    seed:              int   = 42,
) -> List[Dict[str, Any]]:
    """
    Generate candidate blend configurations: weight grid × boost grids.

    If the full cross-product exceeds ``max_candidates``, a reproducible
    random sample is taken (controlled by ``seed``).

    Returns:
        List of candidate dicts, each with keys:
        ``weights``, ``max_signal_boost``, ``max_conf_boost``.
    """
    sb_grid = signal_boost_grid if signal_boost_grid is not None else _DEFAULT_SIGNAL_BOOST_GRID
    cb_grid = conf_boost_grid   if conf_boost_grid   is not None else _DEFAULT_CONF_BOOST_GRID

    weight_combos = generate_weight_grid(step=weight_step)

    all_candidates: List[Dict[str, Any]] = []
    for w in weight_combos:
        for sb in sb_grid:
            for cb in cb_grid:
                all_candidates.append({
                    "weights":          dict(w),
                    "max_signal_boost": round(float(sb), 4),
                    "max_conf_boost":   round(float(cb), 4),
                })

    if len(all_candidates) > max_candidates:
        rng = random.Random(seed)
        rng.shuffle(all_candidates)
        all_candidates = all_candidates[:max_candidates]

    logger.debug(
        "generate_candidates: %d weight combos × %d sb × %d cb = %d total; "
        "returning %d (max_candidates=%d)",
        len(weight_combos), len(sb_grid), len(cb_grid),
        len(weight_combos) * len(sb_grid) * len(cb_grid),
        len(all_candidates), max_candidates,
    )
    return all_candidates


# ---------------------------------------------------------------------------
# Score recomputation (pure function — mirrors comparison.py exactly)
# ---------------------------------------------------------------------------

def recompute_enriched_score(
    baseline_signal_score:     float,
    baseline_confidence_score: float,
    raw_scraped_confidence:    float,
    raw_recency_score:         float,
    raw_theme_alignment_score: float,
    raw_mention_acceleration:  float,
    weights:                   Dict[str, float],
    max_signal_boost:          float,
    max_conf_boost:            float,
) -> Tuple[float, float, float]:
    """
    Recompute enriched signal and confidence scores for a single snapshot row
    using the given candidate weights and boost caps.

    Mirrors ``comparison._compute_soft_composite`` + enrichment formula.

    Returns:
        ``(soft_composite, enriched_signal_score, enriched_confidence_score)``
    """
    # Re-centre mention_acceleration from [-1, +1] → [0, 1]
    accel_norm = (raw_mention_acceleration + 1.0) / 2.0

    raw_values: Dict[str, float] = {
        "scraped_confidence":    raw_scraped_confidence,
        "recency_score":         raw_recency_score,
        "theme_alignment_score": raw_theme_alignment_score,
        "mention_accel_norm":    accel_norm,
    }

    composite = sum(raw_values.get(k, 0.0) * weights.get(k, 0.0) for k in _WEIGHT_KEYS)
    composite = round(min(1.0, max(0.0, composite)), 6)

    enriched_sig  = round(min(1.0, baseline_signal_score  + composite * max_signal_boost), 6)
    enriched_conf = round(
        min(1.0, baseline_confidence_score + raw_scraped_confidence * max_conf_boost), 6
    )
    return composite, enriched_sig, enriched_conf


# ---------------------------------------------------------------------------
# Per-window metrics (pure function)
# ---------------------------------------------------------------------------

def _compute_window_metrics(
    rows:            List[Dict[str, Any]],
    min_sample_size: int = _DEFAULT_MIN_SAMPLE_SIZE,
) -> Dict[str, Any]:
    """
    Compute evaluation metrics for a set of annotated outcome rows for a
    single return window.

    Each row must have:
        ``return_pct``               — float, the resolved return
        ``recomputed_signal_delta``  — float, delta under the candidate config

    Lift computation:
    - If both boosted and unboosted groups have data: lift = boosted − unboosted.
    - If only boosted group exists: lift = boosted win_rate − 0.5 (neutral
      baseline); return_lift = avg_return_boosted − 0 (vs 0% baseline).
    - If neither has data: lift = None.

    Returns:
        Dict with keys: n_total, n_boosted, n_unboosted, resolved_count,
        win_rate_boosted, win_rate_unboosted, win_rate_lift, avg_return_boosted,
        avg_return_unboosted, return_lift, sample_ok.
    """
    boosted   = [r for r in rows if (r.get("recomputed_signal_delta") or 0.0) > 0.0]
    unboosted = [r for r in rows if (r.get("recomputed_signal_delta") or 0.0) <= 0.0]

    def _grp_stats(group: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
        rets = [float(r["return_pct"]) for r in group if r.get("return_pct") is not None]
        if not rets:
            return None, None
        win_rate   = sum(1 for x in rets if x > 0) / len(rets)
        avg_return = sum(rets) / len(rets)
        return round(win_rate, 4), round(avg_return, 4)

    wr_b, ar_b = _grp_stats(boosted)
    wr_u, ar_u = _grp_stats(unboosted)

    if wr_b is not None and wr_u is not None:
        win_rate_lift = round(wr_b - wr_u, 4)
        return_lift   = round(ar_b - ar_u, 4) if ar_b is not None and ar_u is not None else None
    elif wr_b is not None:
        # No unboosted comparison group — compare against neutral baselines
        win_rate_lift = round(wr_b - 0.5, 4)
        return_lift   = ar_b   # vs 0% return baseline
    else:
        win_rate_lift = None
        return_lift   = None

    return {
        "n_total":             len(rows),
        "n_boosted":           len(boosted),
        "n_unboosted":         len(unboosted),
        "resolved_count":      len(rows),
        "win_rate_boosted":    wr_b,
        "win_rate_unboosted":  wr_u,
        "win_rate_lift":       win_rate_lift,
        "avg_return_boosted":  ar_b,
        "avg_return_unboosted": ar_u,
        "return_lift":         return_lift,
        "sample_ok":           len(boosted) >= min_sample_size,
    }


# ---------------------------------------------------------------------------
# Objective and stability scoring (pure functions)
# ---------------------------------------------------------------------------

def compute_objective_score(
    per_window_metrics: Dict[int, Dict[str, Any]],
    min_sample_size:    int            = _DEFAULT_MIN_SAMPLE_SIZE,
    windows:            Optional[List[int]] = None,
) -> float:
    """
    Compute a single objective score for a candidate configuration.

    The objective is a window-weighted sum of win_rate_lift and return_lift.
    Windows with fewer than ``min_sample_size`` boosted rows contribute 0.

    Stability penalty: if the win_rate_lift between 5d and 20d windows differs
    by more than 0.15, a proportional discount is applied.

    Returns a float (higher is better; approximately in [-1, 1]).
    """
    _windows = windows if windows is not None else _DEFAULT_WINDOWS
    wt_map = {w: _WINDOW_OBJ_WEIGHTS.get(w, 1.0 / len(_windows)) for w in _windows}
    total_wt = sum(wt_map.values()) or 1.0
    wt_map = {w: v / total_wt for w, v in wt_map.items()}

    score     = 0.0
    any_ok    = False
    lifts_primary: List[float] = []   # 5d and 20d win_rate_lift for stability check

    for w in _windows:
        m = per_window_metrics.get(w, {})
        if not m.get("sample_ok", False):
            continue
        any_ok = True

        wrl    = m.get("win_rate_lift") or 0.0
        rl     = m.get("return_lift")   or 0.0
        rl_norm = max(-1.0, min(1.0, rl / 20.0))   # normalise: 20% return → 1.0

        score += wt_map[w] * (0.6 * wrl + 0.4 * rl_norm)

        if w in (5, 20):
            lifts_primary.append(wrl)

    if not any_ok:
        return 0.0

    # Stability penalty for inconsistency between 5d and 20d
    if len(lifts_primary) == 2:
        gap = abs(lifts_primary[0] - lifts_primary[1])
        if gap > 0.15:
            score -= 0.3 * (gap - 0.15)

    return round(score, 6)


def compute_stability_score(
    per_window_metrics: Dict[int, Dict[str, Any]],
    windows:            Optional[List[int]] = None,
) -> float:
    """
    Compute a stability score in [0, 1] measuring win_rate_lift consistency
    across the given windows (default: 5d and 20d).

    Score = 1.0 → identical lift across all windows.
    Score = 0.0 → extreme inconsistency (gap >= 0.50).
    Returns 1.0 if fewer than 2 windows have valid lift data.
    """
    _windows = windows if windows is not None else [5, 20]
    lifts = [
        per_window_metrics[w]["win_rate_lift"]
        for w in _windows
        if w in per_window_metrics
        and per_window_metrics[w].get("win_rate_lift") is not None
    ]
    if len(lifts) < 2:
        return 1.0
    gap = max(lifts) - min(lifts)
    return round(max(0.0, 1.0 - gap / 0.5), 4)


# ---------------------------------------------------------------------------
# Candidate evaluation
# ---------------------------------------------------------------------------

def evaluate_candidate(
    candidate:       Dict[str, Any],
    raw_rows:        List[Dict[str, Any]],
    windows:         Optional[List[int]] = None,
    min_sample_size: int = _DEFAULT_MIN_SAMPLE_SIZE,
) -> Dict[str, Any]:
    """
    Evaluate a single candidate configuration against resolved outcome rows.

    ``raw_rows`` must be the output of
    ``ScrapedIntelStore.get_resolved_outcomes_with_raw_signals()``.

    For each row the enriched signal score is recomputed using the candidate's
    weights and boost caps.  Rows whose LEFT JOIN returned NULL soft signal
    values are treated as "not boosted" (delta = 0.0).

    Returns:
        Dict with keys:
        ``candidate``, ``per_window``, ``objective_score``, ``stability_score``.
    """
    weights = candidate["weights"]
    max_sb  = candidate["max_signal_boost"]
    max_cb  = candidate["max_conf_boost"]
    _windows = windows if windows is not None else _DEFAULT_WINDOWS

    # Annotate each row with the recomputed signal_delta under this candidate
    annotated: List[Dict[str, Any]] = []
    for r in raw_rows:
        raw_sc = r.get("raw_scraped_confidence")
        raw_re = r.get("raw_recency_score")
        raw_ta = r.get("raw_theme_alignment_score")
        raw_ma = r.get("raw_mention_acceleration")

        if raw_sc is None:
            # No soft signals for this snapshot — cannot be boosted under any config
            recomputed_delta = 0.0
        else:
            _, new_sig, _ = recompute_enriched_score(
                baseline_signal_score=float(r.get("baseline_signal_score") or 0.0),
                baseline_confidence_score=float(r.get("baseline_confidence_score") or 0.0),
                raw_scraped_confidence=float(raw_sc or 0.0),
                raw_recency_score=float(raw_re or 0.0),
                raw_theme_alignment_score=float(raw_ta or 0.0),
                raw_mention_acceleration=float(raw_ma or 0.0),
                weights=weights,
                max_signal_boost=max_sb,
                max_conf_boost=max_cb,
            )
            recomputed_delta = round(
                new_sig - float(r.get("baseline_signal_score") or 0.0), 6
            )

        annotated.append({**r, "recomputed_signal_delta": recomputed_delta})

    # Group by window_days and compute metrics for each
    per_window: Dict[int, Dict[str, Any]] = {}
    for w in _windows:
        window_rows = [a for a in annotated if int(a.get("window_days") or 0) == w]
        per_window[w] = _compute_window_metrics(window_rows, min_sample_size)

    obj  = compute_objective_score(per_window, min_sample_size=min_sample_size, windows=_windows)
    stab = compute_stability_score(per_window, windows=[w for w in _windows if w in (5, 20)])

    return {
        "candidate":       candidate,
        "per_window":      per_window,
        "objective_score": obj,
        "stability_score": stab,
    }


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def rank_candidates(evaluated: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sort evaluated candidates by (objective_score, stability_score) descending
    and add a 1-based ``rank`` field.

    Returns a new sorted list; the input is not mutated.
    """
    ranked = sorted(
        evaluated,
        key=lambda x: (x["objective_score"], x["stability_score"]),
        reverse=True,
    )
    for i, item in enumerate(ranked):
        item["rank"] = i + 1
    return ranked


# ---------------------------------------------------------------------------
# Report builders
# ---------------------------------------------------------------------------

def build_tuning_report(
    ranked:        List[Dict[str, Any]],
    config_used:   Dict[str, Any],
    raw_row_count: int,
    warnings:      Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Build the full tuning report dict from ranked candidates.

    Args:
        ranked:        Output of ``rank_candidates()``.
        config_used:   The tuning config fields used for this run.
        raw_row_count: Total resolved outcome rows evaluated.
        warnings:      Optional list of warning strings.

    Returns:
        Dict suitable for JSON serialisation via ``write_tuning_results_json()``.
    """
    top5        = ranked[:5]
    recommended = ranked[0] if ranked else None

    report: Dict[str, Any] = {
        "generated_at":            datetime.now().isoformat(),
        "config_used":             config_used,
        "total_candidates_tested": len(ranked),
        "total_resolved_rows":     raw_row_count,
        "warnings":                warnings or [],
        "top_candidates":          top5,
        "recommended":             recommended,
        "all_candidates":          ranked,
    }

    if recommended:
        report["recommended_config_snippet"] = {
            "scraped_intel": {
                "comparison_max_signal_boost": recommended["candidate"]["max_signal_boost"],
                "comparison_max_conf_boost":   recommended["candidate"]["max_conf_boost"],
            },
            "blend_weights": recommended["candidate"]["weights"],
        }
    else:
        report["recommended_config_snippet"] = None

    return report


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------

def _prepare_for_json(obj: Any) -> Any:
    """Recursively convert int dict keys to strings for JSON compatibility."""
    if isinstance(obj, dict):
        return {str(k): _prepare_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_prepare_for_json(item) for item in obj]
    return obj


def write_tuning_results_json(
    report:     Dict[str, Any],
    output_dir: Path,
) -> Path:
    """Write tuning results as JSON to ``output_dir``."""
    path = output_dir / "scraped_intel_tuning_results.json"
    path.write_text(json.dumps(_prepare_for_json(report), indent=2), encoding="utf-8")
    logger.info(
        "scraped_intel_tuning_results.json written (%d candidates, %d resolved rows)",
        report.get("total_candidates_tested", 0),
        report.get("total_resolved_rows", 0),
    )
    return path


def _fmt_pct(v: Optional[float]) -> str:
    return f"{v * 100:.1f}%" if v is not None else "—"


def _fmt_f(v: Optional[float], prec: int = 4) -> str:
    return f"{v:.{prec}f}" if v is not None else "—"


def write_tuning_results_md(
    report:     Dict[str, Any],
    output_dir: Path,
) -> Path:
    """Write human-readable tuning results as Markdown to ``output_dir``."""
    lines = [
        "# Scraped Intelligence — Weight Tuning Results",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
        f"Candidates tested: {report.get('total_candidates_tested', 0)}  ",
        f"Resolved outcome rows: {report.get('total_resolved_rows', 0)}  ",
        "",
    ]

    warnings = report.get("warnings", [])
    if warnings:
        lines += ["> **Warnings**", ""]
        for w in warnings:
            lines.append(f"> - {w}")
        lines.append("")

    rec = report.get("recommended")
    if rec:
        lines += [
            "## Recommended Configuration",
            "",
            f"**Objective score:** {rec['objective_score']:.4f}  ",
            f"**Stability score:** {rec['stability_score']:.4f}  ",
            "",
            "### Blend Weights",
            "",
            "| Feature | Weight |",
            "|---------|--------|",
        ]
        for feat, wt in rec["candidate"]["weights"].items():
            lines.append(f"| `{feat}` | `{wt:.4f}` |")
        lines += [
            "",
            f"**max_signal_boost:** `{rec['candidate']['max_signal_boost']}`  ",
            f"**max_conf_boost:** `{rec['candidate']['max_conf_boost']}`  ",
            "",
            "### Ready-to-paste config snippet",
            "",
            "```json",
            json.dumps(report.get("recommended_config_snippet", {}), indent=2),
            "```",
            "",
            "### Window Metrics",
            "",
            "| Window | Boosted N | Unboosted N | WR Boost | WR Lift | Ret Lift | Sample OK |",
            "|--------|----------:|------------:|---------:|--------:|---------:|:---------:|",
        ]
        for w_key, m in sorted(rec.get("per_window", {}).items(),
                                key=lambda kv: int(kv[0])):
            lines.append(
                f"| {w_key}d "
                f"| {m.get('n_boosted', 0)} "
                f"| {m.get('n_unboosted', 0)} "
                f"| {_fmt_pct(m.get('win_rate_boosted'))} "
                f"| {_fmt_pct(m.get('win_rate_lift'))} "
                f"| {_fmt_f(m.get('return_lift'), 2)} "
                f"| {'✓' if m.get('sample_ok') else '✗'} |"
            )
        lines.append("")

    top5 = report.get("top_candidates", [])
    if top5:
        lines += [
            "## Top 5 Candidates",
            "",
            "| Rank | Obj | Stab | sc | rec | ta | ma | SigB | ConfB |",
            "|-----:|----:|-----:|---:|----:|---:|---:|-----:|------:|",
        ]
        for c in top5:
            w = c["candidate"]["weights"]
            lines.append(
                f"| {c['rank']} "
                f"| {c['objective_score']:.4f} "
                f"| {c['stability_score']:.4f} "
                f"| {w.get('scraped_confidence', 0):.2f} "
                f"| {w.get('recency_score', 0):.2f} "
                f"| {w.get('theme_alignment_score', 0):.2f} "
                f"| {w.get('mention_accel_norm', 0):.2f} "
                f"| {c['candidate']['max_signal_boost']:.2f} "
                f"| {c['candidate']['max_conf_boost']:.2f} |"
            )
        lines.append("")

    lines.append(
        "_Tuning runs in shadow mode.  "
        "No production WatchlistRow fields or existing snapshots were modified._"
    )

    path = output_dir / "scraped_intel_tuning_results.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("scraped_intel_tuning_results.md written")
    return path


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def _build_config_used(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "tuning_windows":             cfg.get("tuning_windows", _DEFAULT_WINDOWS),
        "tuning_since_date":          cfg.get("tuning_since_date"),
        "tuning_min_sample_size":     cfg.get("tuning_min_sample_size", _DEFAULT_MIN_SAMPLE_SIZE),
        "tuning_signal_boost_grid":   cfg.get("tuning_signal_boost_grid", _DEFAULT_SIGNAL_BOOST_GRID),
        "tuning_conf_boost_grid":     cfg.get("tuning_conf_boost_grid", _DEFAULT_CONF_BOOST_GRID),
        "tuning_weight_step":         cfg.get("tuning_weight_step", _DEFAULT_WEIGHT_STEP),
        "tuning_max_candidates":      cfg.get("tuning_max_candidates", _DEFAULT_MAX_CANDIDATES),
        "tuning_require_all_windows": cfg.get("tuning_require_all_windows", False),
    }


def run_tuning(
    db_path:    str | Path = "data/portfolio.db",
    output_dir: str | Path = "outputs/latest",
    config:     Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Full weight-tuning pipeline:

    1. Load resolved comparison outcome rows with raw soft-signal feature values.
    2. Generate candidate blend configurations (weight grid × boost grids).
    3. Evaluate each candidate: recompute enriched scores, compute per-window metrics.
    4. Rank candidates by objective score.
    5. Write ``scraped_intel_tuning_results.json`` and
       ``scraped_intel_tuning_results.md`` to ``output_dir``.

    Args:
        db_path:    Path to portfolio.db.
        output_dir: Directory to write output files into.
        config:     ``scraped_intel`` config sub-dict.  Reads tuning_* keys.

    Returns:
        The full tuning report dict.
    """
    from scraped_intel.store import ScrapedIntelStore

    cfg          = config or {}
    _windows     = list(cfg.get("tuning_windows",         _DEFAULT_WINDOWS))
    _since       = cfg.get("tuning_since_date")
    _min_sample  = int(cfg.get("tuning_min_sample_size",  _DEFAULT_MIN_SAMPLE_SIZE))
    _sb_grid     = list(cfg.get("tuning_signal_boost_grid", _DEFAULT_SIGNAL_BOOST_GRID))
    _cb_grid     = list(cfg.get("tuning_conf_boost_grid", _DEFAULT_CONF_BOOST_GRID))
    _weight_step = float(cfg.get("tuning_weight_step",    _DEFAULT_WEIGHT_STEP))
    _max_cands   = int(cfg.get("tuning_max_candidates",   _DEFAULT_MAX_CANDIDATES))
    _require_all = bool(cfg.get("tuning_require_all_windows", False))
    _out         = Path(cfg.get("tuning_output_dir") or output_dir)
    _out.mkdir(parents=True, exist_ok=True)

    store    = ScrapedIntelStore(db_path=db_path)
    warnings: List[str] = []

    # Step 1: Load resolved outcomes with raw signal values
    raw_rows = store.get_resolved_outcomes_with_raw_signals(
        since_date=_since,
        limit=5000,
    )
    logger.info(
        "run_tuning: loaded %d resolved outcome rows (since=%s, windows=%s)",
        len(raw_rows), _since, _windows,
    )

    if not raw_rows:
        warnings.append(
            "No resolved comparison outcomes found.  Enable "
            "comparison_outcome_tracking and wait for outcomes to resolve "
            "before running tuning."
        )
        report = build_tuning_report([], _build_config_used(cfg), 0, warnings)
        write_tuning_results_json(report, _out)
        write_tuning_results_md(report, _out)
        return report

    min_recommended = _min_sample * len(_windows)
    if len(raw_rows) < min_recommended:
        warnings.append(
            f"Only {len(raw_rows)} resolved rows available — results may be "
            f"unreliable (recommended minimum: {min_recommended})."
        )

    # Step 2: Generate candidates
    candidates = generate_candidates(
        signal_boost_grid=_sb_grid,
        conf_boost_grid=_cb_grid,
        weight_step=_weight_step,
        max_candidates=_max_cands,
    )
    logger.info("run_tuning: evaluating %d candidates", len(candidates))

    # Step 3: Evaluate
    evaluated: List[Dict[str, Any]] = []
    for cand in candidates:
        result = evaluate_candidate(
            candidate=cand,
            raw_rows=raw_rows,
            windows=_windows,
            min_sample_size=_min_sample,
        )
        if _require_all and not all(
            result["per_window"].get(w, {}).get("sample_ok", False)
            for w in _windows
        ):
            continue
        evaluated.append(result)

    if not evaluated:
        warnings.append(
            "No candidates passed evaluation — all windows below "
            "min_sample_size.  Lower tuning_min_sample_size or collect more "
            "outcome data."
        )

    # Step 4: Rank
    ranked = rank_candidates(evaluated)

    # Step 5: Write reports
    config_used = _build_config_used(cfg)
    report = build_tuning_report(ranked, config_used, len(raw_rows), warnings)
    write_tuning_results_json(report, _out)
    write_tuning_results_md(report, _out)

    best_obj = ranked[0]["objective_score"] if ranked else None
    logger.info(
        "run_tuning complete: %d candidates ranked, best objective=%.4f",
        len(ranked),
        best_obj if best_obj is not None else 0.0,
    )
    return report
