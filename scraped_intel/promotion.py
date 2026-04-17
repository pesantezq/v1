"""
Promotion-review layer for scraped intelligence tuning results.

Given the current scraped-intel config and a recommended candidate from
``scraped_intel/tuning.py``, this module evaluates whether the recommendation
is eligible for promotion under explicit, configurable safety and quality gates
— without touching any live config or watchlist data.

IMPORTANT
---------
This module is strictly read-only with respect to all persistent state.
It never modifies ``config.json``, comparison snapshots, soft_signals, or any
WatchlistRow field.  The output is a review report that a human must act on
manually.

Off by default
--------------
Enable with ``scraped_intel.promotion_review_enabled: true`` in config.json.
Runs after ``run_tuning()`` if tuning produces a recommendation.

Promotion gates
---------------
Eight configurable gates are evaluated in order:

1. min_sample_size          — enough historical events to trust the result
2. min_window_sample_size   — each required window has enough boosted rows
3. required_windows_present — all required return windows have data
4. min_win_rate_lift        — recommended config achieves minimum lift per window
5. min_avg_return_lift      — recommended config achieves minimum return improvement
6. max_instability_gap      — 5d vs 20d win-rate-lift gap is within tolerance
7. max_feature_concentration — no single feature weight dominates the blend
8. dual_window_outperformance — recommended beats current on BOTH 5d and 20d
   (optional; skipped when ``promotion_require_dual_window_outperformance: false``)

The overall ``eligible`` flag is True only when ALL active gates pass.

Output files
------------
``scraped_intel_promotion_review.json``  — structured review for downstream tools
``scraped_intel_promotion_review.md``    — human-readable gate-by-gate report
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("scraped_intel.promotion")

# ---------------------------------------------------------------------------
# Defaults (mirrored into config.json via run_promotion_review)
# ---------------------------------------------------------------------------

_PROMO_NOTE = (
    "NO CONFIG WAS AUTO-APPLIED. "
    "Review this report carefully and apply the config snippet manually "
    "if you decide to trial the recommended settings."
)

_DEFAULT_MIN_SAMPLE_SIZE:              int   = 30
_DEFAULT_MIN_WINDOW_SAMPLE_SIZE:       int   = 10
_DEFAULT_REQUIRED_WINDOWS:             List  = [5, 20]
_DEFAULT_MIN_WIN_RATE_LIFT:            float = 0.05
_DEFAULT_MIN_AVG_RETURN_LIFT:          float = 0.0
_DEFAULT_MAX_INSTABILITY_GAP:          float = 0.20
_DEFAULT_MAX_FEATURE_CONCENTRATION:    float = 0.60
_DEFAULT_REQUIRE_DUAL_OUTPERFORMANCE:  bool  = False


# ---------------------------------------------------------------------------
# Gate result
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    """Outcome of a single promotion gate check."""
    name:   str
    passed: bool
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


# ---------------------------------------------------------------------------
# Individual gate functions (pure — accept plain dicts / scalars)
# ---------------------------------------------------------------------------

def _gate_min_sample_size(
    unique_event_count: int,
    threshold:          int,
) -> GateResult:
    """
    Gate 1: Enough unique historical comparison events (symbol × date pairs).

    ``unique_event_count`` is the number of distinct (symbol, as_of_date) pairs
    across all resolved outcome rows.
    """
    passed = unique_event_count >= threshold
    detail = f"{unique_event_count} unique events {'≥' if passed else '<'} {threshold}"
    return GateResult("min_sample_size", passed, detail)


def _gate_min_window_sample_size(
    rec_metrics:      Dict[int, Dict[str, Any]],
    required_windows: List[int],
    threshold:        int,
) -> GateResult:
    """
    Gate 2: Each required window has at least ``threshold`` boosted rows in the
    recommended config.
    """
    failures: List[str] = []
    for w in required_windows:
        m = rec_metrics.get(w, {})
        n_boosted = m.get("n_boosted", 0)
        if n_boosted < threshold:
            failures.append(f"{w}d: {n_boosted} < {threshold}")
    if failures:
        return GateResult(
            "min_window_sample_size", False,
            "insufficient boosted rows: " + ", ".join(failures),
        )
    passing = [f"{w}d: {rec_metrics.get(w, {}).get('n_boosted', 0)} ≥ {threshold}"
               for w in required_windows]
    return GateResult("min_window_sample_size", True, ", ".join(passing))


def _gate_required_windows_present(
    rec_metrics:      Dict[int, Dict[str, Any]],
    required_windows: List[int],
) -> GateResult:
    """
    Gate 3: All required return windows have metrics (i.e., were evaluated by
    the tuner with at least one row).
    """
    missing = [w for w in required_windows if w not in rec_metrics]
    if missing:
        return GateResult(
            "required_windows_present", False,
            "missing windows: " + ", ".join(f"{w}d" for w in sorted(missing)),
        )
    return GateResult(
        "required_windows_present", True,
        "all required windows present: " + ", ".join(f"{w}d" for w in sorted(required_windows)),
    )


def _gate_min_win_rate_lift(
    rec_metrics:      Dict[int, Dict[str, Any]],
    required_windows: List[int],
    threshold:        float,
) -> GateResult:
    """
    Gate 4: Recommended config achieves at least ``threshold`` win-rate lift on
    every required window.
    """
    failures: List[str] = []
    for w in required_windows:
        m   = rec_metrics.get(w, {})
        wrl = m.get("win_rate_lift")
        if wrl is None:
            failures.append(f"{w}d: no data")
        elif wrl < threshold:
            failures.append(f"{w}d: {wrl:.4f} < {threshold:.4f}")
    if failures:
        return GateResult(
            "min_win_rate_lift", False,
            "below threshold: " + ", ".join(failures),
        )
    passing = [
        f"{w}d: {rec_metrics.get(w, {}).get('win_rate_lift', 0.0):.4f} ≥ {threshold:.4f}"
        for w in required_windows
    ]
    return GateResult("min_win_rate_lift", True, ", ".join(passing))


def _gate_min_avg_return_lift(
    rec_metrics:      Dict[int, Dict[str, Any]],
    required_windows: List[int],
    threshold:        float,
) -> GateResult:
    """
    Gate 5: Recommended config achieves at least ``threshold`` average return
    lift (percentage points) on every required window.
    """
    failures: List[str] = []
    for w in required_windows:
        m  = rec_metrics.get(w, {})
        rl = m.get("return_lift")
        if rl is None:
            failures.append(f"{w}d: no data")
        elif rl < threshold:
            failures.append(f"{w}d: {rl:.4f}% < {threshold:.4f}%")
    if failures:
        return GateResult(
            "min_avg_return_lift", False,
            "below threshold: " + ", ".join(failures),
        )
    passing = [
        f"{w}d: {rec_metrics.get(w, {}).get('return_lift', 0.0):.4f}%"
        for w in required_windows
    ]
    return GateResult("min_avg_return_lift", True, ", ".join(passing))


def _gate_max_instability_gap(
    rec_metrics:  Dict[int, Dict[str, Any]],
    threshold:    float,
    check_windows: Optional[List[int]] = None,
) -> GateResult:
    """
    Gate 6: The gap between win-rate lift at 5d and 20d (or the two primary
    windows in ``check_windows``) does not exceed ``threshold``.

    When fewer than two windows have valid lift data, the gate passes by
    default (cannot compute gap).
    """
    _windows = check_windows if check_windows is not None else [5, 20]
    lifts = [
        (w, rec_metrics[w]["win_rate_lift"])
        for w in _windows
        if w in rec_metrics and rec_metrics[w].get("win_rate_lift") is not None
    ]
    if len(lifts) < 2:
        return GateResult(
            "max_instability_gap", True,
            "only one window with valid lift — gap not computable; gate passes by default",
        )
    max_lift = max(v for _, v in lifts)
    min_lift = min(v for _, v in lifts)
    gap = round(max_lift - min_lift, 4)
    passed = gap <= threshold
    direction = "≤" if passed else ">"
    return GateResult(
        "max_instability_gap", passed,
        f"gap={gap:.4f} {direction} {threshold:.4f} "
        f"({', '.join(f'{w}d={v:.4f}' for w, v in lifts)})",
    )


def _gate_max_feature_concentration(
    weights:   Dict[str, float],
    threshold: float,
) -> GateResult:
    """
    Gate 7: No single feature weight in the recommended config exceeds
    ``threshold``.  Protects against single-feature-dominated blends.
    """
    if not weights:
        return GateResult("max_feature_concentration", False, "no weights provided")
    max_feat  = max(weights, key=weights.__getitem__)
    max_wt    = weights[max_feat]
    passed    = max_wt <= threshold
    direction = "≤" if passed else ">"
    return GateResult(
        "max_feature_concentration", passed,
        f"max weight: {max_feat}={max_wt:.4f} {direction} {threshold:.4f}",
    )


def _gate_dual_window_outperformance(
    rec_metrics: Dict[int, Dict[str, Any]],
    cur_metrics: Dict[int, Dict[str, Any]],
    check_windows: Optional[List[int]] = None,
) -> GateResult:
    """
    Gate 8 (optional): Recommended config must outperform the current config
    on win-rate lift for ALL ``check_windows``.

    Skips gracefully (passes) if the current config lacks data for a window.
    """
    _windows = check_windows if check_windows is not None else [5, 20]
    failures: List[str] = []
    passing:  List[str] = []
    for w in _windows:
        rec_wrl = rec_metrics.get(w, {}).get("win_rate_lift")
        cur_wrl = cur_metrics.get(w, {}).get("win_rate_lift")
        if rec_wrl is None:
            failures.append(f"{w}d: recommended has no lift data")
            continue
        if cur_wrl is None:
            # No baseline to compare against — treat as a pass with a caveat
            passing.append(f"{w}d: recommended={rec_wrl:.4f} (no current baseline)")
            continue
        if rec_wrl > cur_wrl:
            passing.append(f"{w}d: {rec_wrl:.4f} > {cur_wrl:.4f}")
        else:
            failures.append(f"{w}d: {rec_wrl:.4f} ≤ {cur_wrl:.4f} (current)")
    if failures:
        return GateResult(
            "dual_window_outperformance", False,
            "recommended does not outperform current on: " + ", ".join(failures),
        )
    return GateResult("dual_window_outperformance", True, ", ".join(passing) or "no windows to check")


# ---------------------------------------------------------------------------
# Side-by-side comparison
# ---------------------------------------------------------------------------

def compare_configs(
    current_metrics:     Dict[int, Dict[str, Any]],
    recommended_metrics: Dict[int, Dict[str, Any]],
    windows:             List[int],
) -> Dict[int, Dict[str, Any]]:
    """
    Build a per-window side-by-side comparison between the current and
    recommended candidate metrics.

    Each window entry contains:
        current_win_rate_lift      — float or None
        recommended_win_rate_lift  — float or None
        win_rate_lift_delta        — rec − cur (None if either is missing)
        current_return_lift        — float or None
        recommended_return_lift    — float or None
        return_lift_delta          — rec − cur (None if either is missing)
        recommended_better_win_rate — bool (True if rec strictly > cur)
        recommended_better_return   — bool (True if rec strictly > cur)
    """
    result: Dict[int, Dict[str, Any]] = {}
    for w in windows:
        c = current_metrics.get(w, {})
        r = recommended_metrics.get(w, {})

        cur_wrl = c.get("win_rate_lift")
        rec_wrl = r.get("win_rate_lift")
        cur_rl  = c.get("return_lift")
        rec_rl  = r.get("return_lift")

        wrl_delta = round(rec_wrl - cur_wrl, 4) if (rec_wrl is not None and cur_wrl is not None) else None
        rl_delta  = round(rec_rl  - cur_rl,  4) if (rec_rl  is not None and cur_rl  is not None) else None

        result[w] = {
            "current_win_rate_lift":     cur_wrl,
            "recommended_win_rate_lift": rec_wrl,
            "win_rate_lift_delta":       wrl_delta,
            "current_return_lift":       cur_rl,
            "recommended_return_lift":   rec_rl,
            "return_lift_delta":         rl_delta,
            "recommended_better_win_rate": (
                rec_wrl is not None and cur_wrl is not None and rec_wrl > cur_wrl
            ),
            "recommended_better_return": (
                rec_rl is not None and cur_rl is not None and rec_rl > cur_rl
            ),
        }
    return result


# ---------------------------------------------------------------------------
# Gate orchestrator
# ---------------------------------------------------------------------------

def evaluate_promotion_gates(
    recommended_candidate: Dict[str, Any],
    current_candidate:     Dict[str, Any],
    raw_rows:              List[Dict[str, Any]],
    gate_config:           Dict[str, Any],
) -> Dict[str, Any]:
    """
    Evaluate all promotion gates for the recommended candidate.

    This function is the core of the promotion review.  It:
    1. Evaluates the recommended and current candidates against ``raw_rows``.
    2. Runs each gate on the resulting metrics.
    3. Returns a structured result including per-window metrics, gate outcomes,
       side-by-side comparison, and the overall ``eligible`` flag.

    Args:
        recommended_candidate: Dict with ``weights``, ``max_signal_boost``,
                               ``max_conf_boost`` (from ``tuning.run_tuning()``).
        current_candidate:     Same structure for the currently active config.
        raw_rows:              Output of
                               ``ScrapedIntelStore.get_resolved_outcomes_with_raw_signals()``.
        gate_config:           Dict of gate threshold values (see ``run_promotion_review``
                               for the full list of keys).

    Returns:
        Dict with keys: ``rec_metrics``, ``cur_metrics``, ``comparison``,
        ``gates``, ``eligible``, ``passed_gates``, ``failed_gates``,
        ``unique_event_count``, ``total_rows``.
    """
    from scraped_intel.tuning import evaluate_candidate

    # Gate parameters
    min_sample      = int(gate_config.get("promotion_min_sample_size",         _DEFAULT_MIN_SAMPLE_SIZE))
    min_win_sample  = int(gate_config.get("promotion_min_window_sample_size",   _DEFAULT_MIN_WINDOW_SAMPLE_SIZE))
    req_windows     = list(gate_config.get("promotion_required_windows",        _DEFAULT_REQUIRED_WINDOWS))
    min_wrl         = float(gate_config.get("promotion_min_win_rate_lift",      _DEFAULT_MIN_WIN_RATE_LIFT))
    min_rl          = float(gate_config.get("promotion_min_avg_return_lift",    _DEFAULT_MIN_AVG_RETURN_LIFT))
    max_gap         = float(gate_config.get("promotion_max_instability_gap",    _DEFAULT_MAX_INSTABILITY_GAP))
    max_conc        = float(gate_config.get("promotion_max_feature_concentration", _DEFAULT_MAX_FEATURE_CONCENTRATION))
    req_dual        = bool(gate_config.get("promotion_require_dual_window_outperformance", _DEFAULT_REQUIRE_DUAL_OUTPERFORMANCE))

    # Evaluate both candidates against the same raw rows
    all_windows = sorted(set(req_windows) | {1})   # include 1d for completeness but don't require it

    rec_eval = evaluate_candidate(
        recommended_candidate, raw_rows,
        windows=all_windows, min_sample_size=min_win_sample,
    )
    cur_eval = evaluate_candidate(
        current_candidate, raw_rows,
        windows=all_windows, min_sample_size=min_win_sample,
    )

    rec_metrics: Dict[int, Dict[str, Any]] = rec_eval["per_window"]
    cur_metrics: Dict[int, Dict[str, Any]] = cur_eval["per_window"]

    # Unique historical events (distinct symbol × as_of_date pairs)
    unique_events = len(set((r.get("symbol", ""), r.get("as_of_date", "")) for r in raw_rows))

    # Run gates
    gates: List[GateResult] = [
        _gate_min_sample_size(unique_events, min_sample),
        _gate_min_window_sample_size(rec_metrics, req_windows, min_win_sample),
        _gate_required_windows_present(rec_metrics, req_windows),
        _gate_min_win_rate_lift(rec_metrics, req_windows, min_wrl),
        _gate_min_avg_return_lift(rec_metrics, req_windows, min_rl),
        _gate_max_instability_gap(rec_metrics, max_gap, check_windows=[w for w in [5, 20] if w in rec_metrics]),
        _gate_max_feature_concentration(recommended_candidate.get("weights", {}), max_conc),
    ]

    if req_dual:
        gates.append(_gate_dual_window_outperformance(rec_metrics, cur_metrics, req_windows))

    comparison = compare_configs(cur_metrics, rec_metrics, windows=all_windows)

    eligible     = all(g.passed for g in gates)
    passed_gates = [g.name for g in gates if g.passed]
    failed_gates = [g.name for g in gates if not g.passed]

    return {
        "rec_metrics":        rec_metrics,
        "cur_metrics":        cur_metrics,
        "comparison":         comparison,
        "gates":              gates,
        "eligible":           eligible,
        "passed_gates":       passed_gates,
        "failed_gates":       failed_gates,
        "unique_event_count": unique_events,
        "total_rows":         len(raw_rows),
        "rec_objective_score":  rec_eval.get("objective_score"),
        "rec_stability_score":  rec_eval.get("stability_score"),
    }


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _candidate_summary(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "weights":          candidate.get("weights", {}),
        "max_signal_boost": candidate.get("max_signal_boost"),
        "max_conf_boost":   candidate.get("max_conf_boost"),
    }


def build_promotion_review(
    recommended_candidate: Dict[str, Any],
    current_candidate:     Dict[str, Any],
    eval_result:           Dict[str, Any],
    gate_config:           Dict[str, Any],
) -> Dict[str, Any]:
    """
    Assemble the full promotion review dict from gate evaluation results.

    Args:
        recommended_candidate: The top-ranked tuning candidate.
        current_candidate:     The currently active config candidate.
        eval_result:           Return value of ``evaluate_promotion_gates()``.
        gate_config:           Gate threshold parameters.

    Returns:
        Dict suitable for JSON serialisation or Markdown rendering.
    """
    eligible = eval_result["eligible"]

    config_snippet: Optional[Dict[str, Any]] = None
    if eligible:
        config_snippet = {
            "scraped_intel": {
                "comparison_max_signal_boost": recommended_candidate.get("max_signal_boost"),
                "comparison_max_conf_boost":   recommended_candidate.get("max_conf_boost"),
            },
            "blend_weights": recommended_candidate.get("weights", {}),
        }

    # Stringify window keys for JSON compatibility
    comparison_str = {
        str(w): v for w, v in eval_result["comparison"].items()
    }
    rec_metrics_str = {
        str(w): v for w, v in eval_result["rec_metrics"].items()
    }
    cur_metrics_str = {
        str(w): v for w, v in eval_result["cur_metrics"].items()
    }

    return {
        "generated_at":           datetime.now().isoformat(),
        "note":                   _PROMO_NOTE,
        "eligible":               eligible,
        "unique_event_count":     eval_result["unique_event_count"],
        "total_rows":             eval_result["total_rows"],
        "current_config":         {
            **_candidate_summary(current_candidate),
            "per_window": cur_metrics_str,
        },
        "recommended_config":     {
            **_candidate_summary(recommended_candidate),
            "per_window":      rec_metrics_str,
            "objective_score": eval_result.get("rec_objective_score"),
            "stability_score": eval_result.get("rec_stability_score"),
        },
        "comparison":             comparison_str,
        "gate_config_used":       gate_config,
        "gates":                  [g.to_dict() for g in eval_result["gates"]],
        "passed_gates":           eval_result["passed_gates"],
        "failed_gates":           eval_result["failed_gates"],
        "reasons_passed":         [g.detail for g in eval_result["gates"] if g.passed],
        "reasons_failed":         [
            f"{g.name}: {g.detail}" for g in eval_result["gates"] if not g.passed
        ],
        "config_snippet":         config_snippet,
    }


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------

def write_promotion_review_json(
    review:     Dict[str, Any],
    output_dir: Path,
) -> Path:
    """Write the promotion review as JSON to ``output_dir``."""
    path = output_dir / "scraped_intel_promotion_review.json"
    path.write_text(json.dumps(review, indent=2), encoding="utf-8")
    logger.info(
        "scraped_intel_promotion_review.json written (eligible=%s, gates=%d passed / %d failed)",
        review.get("eligible"),
        len(review.get("passed_gates", [])),
        len(review.get("failed_gates", [])),
    )
    return path


def _fmt_pct(v: Optional[float]) -> str:
    return f"{v * 100:.1f}%" if v is not None else "—"


def _fmt_f(v: Optional[float], prec: int = 4) -> str:
    return f"{v:.{prec}f}" if v is not None else "—"


def _tick(passed: bool) -> str:
    return "✓  PASS" if passed else "✗  FAIL"


def write_promotion_review_md(
    review:     Dict[str, Any],
    output_dir: Path,
) -> Path:
    """Write human-readable promotion review as Markdown to ``output_dir``."""
    eligible = review.get("eligible", False)
    lines = [
        "# Scraped Intelligence — Promotion Review",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
        f"Unique historical events: {review.get('unique_event_count', 0)}  ",
        f"Total resolved rows: {review.get('total_rows', 0)}  ",
        "",
        f"## {'✅ ELIGIBLE FOR TRIAL' if eligible else '🚫 NOT ELIGIBLE — DO NOT PROMOTE'}",
        "",
        f"> ⚠️  {review.get('note', '')}",
        "",
    ]

    # Failed gates summary (at the top if any)
    failed = review.get("failed_gates", [])
    if failed:
        lines += [
            "### Failed Gates",
            "",
        ]
        for reason in review.get("reasons_failed", []):
            lines.append(f"- ✗ {reason}")
        lines.append("")

    # Gate-by-gate table
    gates = review.get("gates", [])
    if gates:
        lines += [
            "## Gate Results",
            "",
            "| Gate | Result | Detail |",
            "|------|--------|--------|",
        ]
        for g in gates:
            result_str = "✓ PASS" if g["passed"] else "✗ FAIL"
            lines.append(f"| `{g['name']}` | {result_str} | {g['detail']} |")
        lines.append("")

    # Side-by-side comparison per window
    comparison = review.get("comparison", {})
    req_windows_raw = review.get("gate_config_used", {}).get(
        "promotion_required_windows", _DEFAULT_REQUIRED_WINDOWS
    )
    all_windows = sorted(
        set(int(k) for k in comparison.keys())
    )
    if comparison:
        lines += [
            "## Config Comparison (Current vs Recommended)",
            "",
            "| Window | Cur WR Lift | Rec WR Lift | Δ WR Lift | Cur Ret Lift | Rec Ret Lift | Δ Ret Lift | Rec Better |",
            "|--------|------------:|------------:|----------:|-------------:|-------------:|-----------:|:----------:|",
        ]
        for w in all_windows:
            c = comparison.get(str(w), comparison.get(w, {}))
            better = "✓" if c.get("recommended_better_win_rate") else ""
            lines.append(
                f"| {w}d "
                f"| {_fmt_pct(c.get('current_win_rate_lift'))} "
                f"| {_fmt_pct(c.get('recommended_win_rate_lift'))} "
                f"| {_fmt_pct(c.get('win_rate_lift_delta'))} "
                f"| {_fmt_f(c.get('current_return_lift'), 2)}% "
                f"| {_fmt_f(c.get('recommended_return_lift'), 2)}% "
                f"| {_fmt_f(c.get('return_lift_delta'), 2)}% "
                f"| {better} |"
            )
        lines.append("")

    # Config summaries
    cur  = review.get("current_config", {})
    rec  = review.get("recommended_config", {})
    lines += [
        "## Config Summaries",
        "",
        "### Current Config",
        "",
        "| Feature | Weight |",
        "|---------|--------|",
    ]
    for feat, wt in (cur.get("weights") or {}).items():
        lines.append(f"| `{feat}` | `{wt:.4f}` |")
    lines += [
        "",
        f"**max_signal_boost:** `{cur.get('max_signal_boost')}` · "
        f"**max_conf_boost:** `{cur.get('max_conf_boost')}`  ",
        "",
        "### Recommended Config",
        "",
        f"Objective score: `{_fmt_f(rec.get('objective_score'))}` · "
        f"Stability score: `{_fmt_f(rec.get('stability_score'))}`  ",
        "",
        "| Feature | Weight |",
        "|---------|--------|",
    ]
    for feat, wt in (rec.get("weights") or {}).items():
        lines.append(f"| `{feat}` | `{wt:.4f}` |")
    lines += [
        "",
        f"**max_signal_boost:** `{rec.get('max_signal_boost')}` · "
        f"**max_conf_boost:** `{rec.get('max_conf_boost')}`  ",
        "",
    ]

    # Config snippet if eligible
    snippet = review.get("config_snippet")
    if eligible and snippet:
        lines += [
            "## Ready-to-Paste Config Snippet",
            "",
            "Apply these changes to `config.json` to trial the recommended settings:",
            "",
            "```json",
            json.dumps(snippet, indent=2),
            "```",
            "",
        ]

    lines.append(
        "_This promotion review is read-only.  "
        "No config file, comparison snapshot, or WatchlistRow was modified._"
    )

    path = output_dir / "scraped_intel_promotion_review.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("scraped_intel_promotion_review.md written")
    return path


# ---------------------------------------------------------------------------
# Current-config extractor
# ---------------------------------------------------------------------------

def _extract_current_candidate(scraped_intel_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a candidate dict from the currently active scraped-intel config.

    Falls back to the default blend weights and boost caps from comparison.py
    when the config does not explicitly set them.
    """
    from scraped_intel.comparison import (
        _DEFAULT_BLEND_WEIGHTS,
        _DEFAULT_MAX_SIGNAL_BOOST,
        _DEFAULT_MAX_CONF_BOOST,
    )
    weights = dict(scraped_intel_cfg.get("blend_weights") or _DEFAULT_BLEND_WEIGHTS)
    return {
        "weights":          weights,
        "max_signal_boost": float(
            scraped_intel_cfg.get("comparison_max_signal_boost", _DEFAULT_MAX_SIGNAL_BOOST)
        ),
        "max_conf_boost":   float(
            scraped_intel_cfg.get("comparison_max_conf_boost", _DEFAULT_MAX_CONF_BOOST)
        ),
    }


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def run_promotion_review(
    db_path:    str | Path = "data/portfolio.db",
    output_dir: str | Path = "outputs/latest",
    config:     Optional[Dict[str, Any]] = None,
    *,
    tuning_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Full promotion-review pipeline.

    1. Load (or accept) the tuning report to get the recommended candidate.
    2. Reconstruct the current candidate from ``config``.
    3. Load resolved outcome rows with raw soft signal values from the store.
    4. Evaluate all promotion gates.
    5. Write ``scraped_intel_promotion_review.json`` and
       ``scraped_intel_promotion_review.md`` to ``output_dir``.

    Args:
        db_path:       Path to portfolio.db.
        output_dir:    Directory to write output files into.
        config:        ``scraped_intel`` config sub-dict.  Reads:
                       ``promotion_*`` gate keys,
                       ``comparison_outcome_windows``,
                       ``comparison_since_date``,
                       ``comparison_max_signal_boost``,
                       ``comparison_max_conf_boost``,
                       ``blend_weights`` (optional).
        tuning_report: Pre-computed tuning report dict.  When absent the
                       function attempts to load
                       ``{promotion_review_output_dir}/scraped_intel_tuning_results.json``.

    Returns:
        The full promotion review dict.
    """
    from scraped_intel.store import ScrapedIntelStore

    cfg    = config or {}
    _out   = Path(cfg.get("promotion_review_output_dir") or output_dir)
    _out.mkdir(parents=True, exist_ok=True)
    _since = cfg.get("comparison_since_date")

    # Gate config dict (passed through to evaluate_promotion_gates)
    gate_config = {k: v for k, v in cfg.items() if k.startswith("promotion_")}

    # ------------------------------------------------------------------
    # Resolve tuning report
    # ------------------------------------------------------------------
    _report = tuning_report
    if _report is None:
        tuning_json = _out / "scraped_intel_tuning_results.json"
        if not tuning_json.exists():
            # Also try the default output_dir
            tuning_json = Path(output_dir) / "scraped_intel_tuning_results.json"
        if tuning_json.exists():
            try:
                _report = json.loads(tuning_json.read_text(encoding="utf-8"))
                logger.info("promotion_review: loaded tuning report from %s", tuning_json)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("promotion_review: failed to load tuning report — %s", exc)

    if not _report or not _report.get("recommended"):
        logger.warning(
            "promotion_review: no tuning report or no recommended candidate — "
            "writing a 'no recommendation' review"
        )
        review = {
            "generated_at":       datetime.now().isoformat(),
            "note":               _PROMO_NOTE,
            "eligible":           False,
            "unique_event_count": 0,
            "total_rows":         0,
            "current_config":     {},
            "recommended_config": {},
            "comparison":         {},
            "gate_config_used":   gate_config,
            "gates":              [],
            "passed_gates":       [],
            "failed_gates":       [],
            "reasons_passed":     [],
            "reasons_failed":     ["no tuning report or no recommended candidate available"],
            "config_snippet":     None,
        }
        write_promotion_review_json(review, _out)
        write_promotion_review_md(review, _out)
        return review

    recommended_candidate: Dict[str, Any] = _report["recommended"]["candidate"]
    current_candidate     = _extract_current_candidate(cfg)

    # ------------------------------------------------------------------
    # Load raw resolved rows
    # ------------------------------------------------------------------
    store    = ScrapedIntelStore(db_path=db_path)
    raw_rows = store.get_resolved_outcomes_with_raw_signals(
        since_date=_since,
        limit=5000,
    )
    logger.info(
        "promotion_review: loaded %d resolved rows (since=%s)", len(raw_rows), _since
    )

    # ------------------------------------------------------------------
    # Evaluate gates + build review
    # ------------------------------------------------------------------
    eval_result = evaluate_promotion_gates(
        recommended_candidate=recommended_candidate,
        current_candidate=current_candidate,
        raw_rows=raw_rows,
        gate_config=gate_config,
    )

    review = build_promotion_review(
        recommended_candidate=recommended_candidate,
        current_candidate=current_candidate,
        eval_result=eval_result,
        gate_config=gate_config,
    )

    write_promotion_review_json(review, _out)
    write_promotion_review_md(review, _out)

    logger.info(
        "run_promotion_review complete: eligible=%s, %d/%d gates passed",
        review["eligible"],
        len(review["passed_gates"]),
        len(review["gates"]),
    )
    return review
