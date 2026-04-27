"""
System Decision Summary.

Consolidates all pipeline artifacts into a single human-readable artifact
that answers, in order:

  1. What is happening in the market?       → top_theme
  2. What are the best opportunities?       → top_opportunity
  3. How do they fit the portfolio?         → best_portfolio_fit
  4. What is the system recommending?       → policy_insight
  5. What policies are active?              → system_state
  6. How is capital being sized?            → capital_preview
  7. Is the data healthy?                   → data_health
  8. What changed since the last run?       → changes

Reads (all optional — missing files degrade gracefully):
  outputs/latest/watchlist_signals.json
  outputs/latest/theme_opportunities.json
  outputs/portfolio/portfolio_snapshot.json
  outputs/performance/approved_ranking_config.json
  outputs/performance/approved_allocation_policy.json
  outputs/performance/allocation_policy_preview.json
  outputs/performance/allocation_policy_simulation.json
  outputs/performance/weight_tuning_suggestions.json

Writes:
  outputs/latest/system_decision_summary.json
  outputs/latest/system_decision_summary.md

CLI:
  python -m watchlist_scanner.system_summary
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("watchlist_scanner.system_summary")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SIGNALS_REL          = ("outputs", "latest", "watchlist_signals.json")
_THEMES_DISCOVERY_REL = ("outputs", "latest", "theme_opportunities.json")
_THEMES_ENGINE_REL    = ("outputs", "latest", "theme_signals.json")
_PORTFOLIO_REL        = ("outputs", "portfolio", "portfolio_snapshot.json")
_RANKING_CONFIG_REL   = ("outputs", "performance", "approved_ranking_config.json")
_ALLOC_POLICY_REL     = ("outputs", "performance", "approved_allocation_policy.json")
_ALLOC_PREVIEW_REL    = ("outputs", "performance", "allocation_policy_preview.json")
_ALLOC_SIMULATION_REL = ("outputs", "performance", "allocation_policy_simulation.json")
_WEIGHT_TUNING_REL    = ("outputs", "performance", "weight_tuning_suggestions.json")
_SUMMARY_JSON_REL     = ("outputs", "latest", "system_decision_summary.json")
_SUMMARY_MD_REL       = ("outputs", "latest", "system_decision_summary.md")


# ---------------------------------------------------------------------------
# Safe loaders
# ---------------------------------------------------------------------------

def _safe_load(path: Path) -> dict[str, Any]:
    """Load a JSON file safely. Returns {} on any error or missing file."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("system_summary: could not load %s — %s", path, exc)
        return {}


def _safe_load_list(path: Path) -> list[Any]:
    """Load a JSON file that may be a list. Returns [] on any error."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("system_summary: could not load list from %s — %s", path, exc)
        return []


def _normalize_theme_record(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Map a theme record from either source to the internal normalized shape:
      {name, type, score, persistence, acceleration, tickers}

    theme_discovery schema uses: score, persistence_score, acceleration_score, theme_type
    theme_engine schema uses:    confidence (=score), persistence_7d (days seen), catalog_match (=type)
    """
    # Score: theme_discovery has "score"; theme_engine has "confidence"
    score_raw = raw.get("score")
    if score_raw is None:
        score_raw = raw.get("confidence", 0.0)
    score = _flt(score_raw, 0.0)

    # Persistence: check most-specific names first
    if raw.get("persistence_score") is not None:
        persistence = _flt(raw["persistence_score"], 0.0)
    elif raw.get("persistence") is not None:
        persistence = _flt(raw["persistence"], 0.0)
    elif raw.get("persistence_7d") is not None:
        # persistence_7d is a count of days seen (0..N); normalize to 0..1 capped at 1
        persistence = min(1.0, _flt(raw["persistence_7d"], 0.0) / 7.0)
    else:
        persistence = 0.0

    # Acceleration: only theme_discovery emits this
    accel_raw = raw.get("acceleration_score")
    if accel_raw is None:
        accel_raw = raw.get("acceleration", 0.0)
    acceleration = _flt(accel_raw, 0.0)

    # Type: theme_discovery uses "theme_type"; theme_engine uses "catalog_match"
    theme_type = str(
        raw.get("theme_type") or raw.get("type") or raw.get("catalog_match") or "classified"
    )

    return {
        "name":         str(raw.get("name") or "Unknown"),
        "type":         theme_type,
        "score":        score,
        "persistence":  persistence,
        "acceleration": acceleration,
        "tickers":      list(raw.get("tickers") or []),
    }


def _merge_theme_sources(
    discovery: dict[str, Any],
    engine: dict[str, Any],
) -> dict[str, Any]:
    """
    Merge theme lists from theme_discovery (theme_opportunities.json) and
    theme_engine (theme_signals.json).  Both sources are normalized to the
    internal shape.  When both name the same theme, the higher-score record wins.
    Returns {} when both sources are empty or missing.
    """
    d_raw = (discovery.get("themes") or []) if isinstance(discovery, dict) else []
    e_raw = (engine.get("themes") or []) if isinstance(engine, dict) else []

    if not d_raw and not e_raw:
        return {}

    by_name: dict[str, dict[str, Any]] = {}
    for raw in (*d_raw, *e_raw):
        if not isinstance(raw, dict):
            continue
        norm = _normalize_theme_record(raw)
        name = norm["name"]
        if name not in by_name or norm["score"] > by_name[name]["score"]:
            by_name[name] = norm

    return {"themes": list(by_name.values())}


def _load_artifacts(root: Path) -> dict[str, Any]:
    """
    Load all source artifacts. Returns a dict keyed by artifact name.
    Each value is either a dict (or {} if missing/malformed).
    """
    discovery_themes = _safe_load(root.joinpath(*_THEMES_DISCOVERY_REL))
    engine_themes    = _safe_load(root.joinpath(*_THEMES_ENGINE_REL))
    return {
        "signals":          _safe_load(root.joinpath(*_SIGNALS_REL)),
        "themes":           _merge_theme_sources(discovery_themes, engine_themes),
        "portfolio":        _safe_load(root.joinpath(*_PORTFOLIO_REL)),
        "ranking_config":   _safe_load(root.joinpath(*_RANKING_CONFIG_REL)),
        "alloc_policy":     _safe_load(root.joinpath(*_ALLOC_POLICY_REL)),
        "alloc_preview":    _safe_load(root.joinpath(*_ALLOC_PREVIEW_REL)),
        "alloc_simulation": _safe_load(root.joinpath(*_ALLOC_SIMULATION_REL)),
        "weight_tuning":    _safe_load(root.joinpath(*_WEIGHT_TUNING_REL)),
    }


def _artifact_flags(root: Path) -> dict[str, bool]:
    """Return {artifact_name: True/False} existence flags."""
    has_discovery = root.joinpath(*_THEMES_DISCOVERY_REL).exists()
    has_engine    = root.joinpath(*_THEMES_ENGINE_REL).exists()
    return {
        "watchlist_signals":         root.joinpath(*_SIGNALS_REL).exists(),
        "theme_opportunities":       has_discovery,
        "theme_signals":             has_engine,
        "theme_data_available":      has_discovery or has_engine,
        "portfolio_snapshot":        root.joinpath(*_PORTFOLIO_REL).exists(),
        "approved_ranking_config":   root.joinpath(*_RANKING_CONFIG_REL).exists(),
        "approved_allocation_policy":root.joinpath(*_ALLOC_POLICY_REL).exists(),
        "allocation_preview":        root.joinpath(*_ALLOC_PREVIEW_REL).exists(),
        "allocation_simulation":     root.joinpath(*_ALLOC_SIMULATION_REL).exists(),
        "weight_tuning_suggestions": root.joinpath(*_WEIGHT_TUNING_REL).exists(),
    }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _flt(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _signal_list(signals: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the results list from a watchlist_signals dict."""
    results = signals.get("results") or signals.get("signals") or []
    return [r for r in results if isinstance(r, dict)]


def _theme_list(themes: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract and normalize a list of theme dicts.
    Handles {themes: [...]} lists and {name: {score|confidence:...}, ...} top-key shapes.
    Records produced by _merge_theme_sources are already normalized but remain idempotent.
    """
    if not themes:
        return []
    raw_themes = themes.get("themes")
    if isinstance(raw_themes, list):
        return [_normalize_theme_record(t) for t in raw_themes if isinstance(t, dict)]
    # Fallback: treat top-level keys as theme names when their values are dicts
    candidates = []
    for key, val in themes.items():
        if isinstance(val, dict) and ("score" in val or "confidence" in val):
            candidates.append(_normalize_theme_record({"name": key, **val}))
    return candidates


# ---------------------------------------------------------------------------
# Section A: Top Theme
# ---------------------------------------------------------------------------

def compute_top_theme(themes: dict[str, Any]) -> dict[str, Any]:
    """
    Return the highest-scoring theme from merged theme sources.
    Records are normalized by _theme_list — all have score, type, persistence, acceleration.
    Returns {} when no theme data is available.
    """
    theme_list = _theme_list(themes)
    if not theme_list:
        return {}
    best = max(theme_list, key=lambda t: _flt(t.get("score"), -1.0))
    return {
        "name":         str(best.get("name") or "Unknown"),
        "type":         str(best.get("type") or "classified"),
        "score":        round(_flt(best.get("score"), 0.0), 4),
        "persistence":  round(_flt(best.get("persistence"), 0.0), 4),
        "acceleration": round(_flt(best.get("acceleration"), 0.0), 4),
        "tickers":      list(best.get("tickers") or [])[:10],
    }


# ---------------------------------------------------------------------------
# Section B: Top Opportunity
# ---------------------------------------------------------------------------

def compute_top_opportunity(signals: dict[str, Any]) -> dict[str, Any]:
    """
    Return the highest final_rank_score signal among alert-eligible signals.
    If no filter_allowed signals exist, falls back to all signals.
    """
    all_sigs = _signal_list(signals)
    if not all_sigs:
        return {}

    eligible = [s for s in all_sigs if s.get("filter_allowed")]
    pool = eligible if eligible else all_sigs

    best = max(pool, key=lambda s: _flt(s.get("final_rank_score"), -1.0))
    return {
        "ticker":               str(best.get("ticker") or best.get("symbol") or "Unknown"),
        "final_rank_score":     round(_flt(best.get("final_rank_score"), 0.0), 4),
        "signal_score":         round(_flt(best.get("signal_score") or best.get("augmented_signal_score"), 0.0), 4),
        "confidence":           round(_flt(best.get("confidence_score") or best.get("confidence"), 0.0), 4),
        "theme_alignment_label":str(best.get("theme_alignment_label") or "none"),
        "portfolio_fit_label":  str(best.get("portfolio_fit_label") or "neutral"),
        "rank_multiplier":      round(_flt(best.get("rank_multiplier"), 1.0), 4),
        "conviction_band":      str(best.get("conviction_band") or "unknown"),
    }


# ---------------------------------------------------------------------------
# Section C: Best Portfolio Fit
# ---------------------------------------------------------------------------

def compute_best_portfolio_fit(signals: dict[str, Any]) -> dict[str, Any]:
    """Return the signal with the highest portfolio_fit_score."""
    all_sigs = _signal_list(signals)
    if not all_sigs:
        return {}

    scored = [s for s in all_sigs if s.get("portfolio_fit_score") is not None]
    if not scored:
        return {}

    best = max(scored, key=lambda s: _flt(s.get("portfolio_fit_score"), -1.0))
    return {
        "ticker":               str(best.get("ticker") or best.get("symbol") or "Unknown"),
        "portfolio_fit_score":  round(_flt(best.get("portfolio_fit_score"), 0.0), 4),
        "portfolio_fit_label":  str(best.get("portfolio_fit_label") or "neutral"),
        "portfolio_fit_reason": str(best.get("portfolio_fit_reason") or ""),
        "final_rank_score":     round(_flt(best.get("final_rank_score"), 0.0), 4),
    }


# ---------------------------------------------------------------------------
# Section D: System State
# ---------------------------------------------------------------------------

def compute_system_state(
    ranking_config: dict[str, Any],
    alloc_policy: dict[str, Any],
    alloc_simulation: dict[str, Any],
    alloc_preview: dict[str, Any],
) -> dict[str, Any]:
    """Summarise which policies are active and their safety flags."""
    # Ranking weights source
    if ranking_config and ranking_config.get("applied_to_live") is not True:
        weights_source = "approved"
        weights_candidate = str(ranking_config.get("recommended_candidate") or "unknown")
        weights_approved_at = str(ranking_config.get("approved_at") or "")
    else:
        weights_source = "default"
        weights_candidate = "current"
        weights_approved_at = ""

    # Allocation policy status
    policy_status = str(alloc_policy.get("activation_status") or "not_approved")
    applied_to_live = bool(alloc_policy.get("applied_to_live", False))
    policy_sample_size = int(alloc_policy.get("sample_size") or 0)
    policy_low_sample = bool(alloc_policy.get("low_sample_warning", False))

    # Simulation safety flags
    sim_observe_only = bool(alloc_simulation.get("observe_only", True))
    sim_not_applied = bool(alloc_simulation.get("not_applied", True))
    sim_sample_size = int(alloc_simulation.get("sample_size") or 0)

    # Preview safety flags
    preview_observe_only = bool(alloc_preview.get("observe_only", True))
    preview_not_applied = bool(alloc_preview.get("not_applied", True))

    return {
        "ranking_weights_source":    weights_source,
        "ranking_weights_candidate": weights_candidate,
        "ranking_weights_approved_at": weights_approved_at,
        "allocation_policy_status":  policy_status,
        "applied_to_live":           applied_to_live,
        "policy_sample_size":        policy_sample_size,
        "policy_low_sample_warning": policy_low_sample,
        "simulation_observe_only":   sim_observe_only,
        "simulation_not_applied":    sim_not_applied,
        "simulation_sample_size":    sim_sample_size,
        "preview_observe_only":      preview_observe_only,
        "preview_not_applied":       preview_not_applied,
    }


# ---------------------------------------------------------------------------
# Section E: Capital Preview
# ---------------------------------------------------------------------------

def compute_capital_preview(
    alloc_preview: dict[str, Any],
    alloc_simulation: dict[str, Any],
) -> dict[str, Any]:
    """Summarise baseline vs rank-aware capital allocation sizing."""
    total_baseline = _flt(alloc_preview.get("total_baseline_pct"), 0.0)
    total_preview  = _flt(alloc_preview.get("total_preview_pct"), 0.0)
    delta_preview  = round(total_preview - total_baseline, 4)
    candidates     = int(alloc_preview.get("candidate_count") or 0)

    sim_b   = dict(alloc_simulation.get("baseline") or {})
    sim_ra  = dict(alloc_simulation.get("rank_aware") or {})
    sim_d   = dict(alloc_simulation.get("delta") or {})
    sim_eff_delta = _flt(sim_d.get("efficiency_delta"), 0.0)
    sim_ret_delta = _flt(sim_d.get("total_return_delta"), 0.0)

    return {
        "candidate_count":          candidates,
        "total_baseline_pct":       round(total_baseline, 4),
        "total_preview_pct":        round(total_preview, 4),
        "preview_vs_baseline_delta":delta_preview,
        "simulation_sample_size":   int(alloc_simulation.get("sample_size") or 0),
        "simulation_efficiency_delta": round(sim_eff_delta, 4),
        "simulation_return_delta":  round(sim_ret_delta, 4),
        "baseline_capital_efficiency":  round(_flt(sim_b.get("capital_efficiency"), 0.0), 4),
        "rank_aware_capital_efficiency":round(_flt(sim_ra.get("capital_efficiency"), 0.0), 4),
    }


# ---------------------------------------------------------------------------
# Section F: Policy Insight
# ---------------------------------------------------------------------------

def compute_policy_insight(
    weight_tuning: dict[str, Any],
    ranking_config: dict[str, Any],
    alloc_simulation: dict[str, Any],
) -> dict[str, Any]:
    """Surface the key weight recommendation and simulation evidence."""
    best_candidate = str(
        weight_tuning.get("recommended_candidate")
        or ranking_config.get("recommended_candidate")
        or "current"
    )
    reason = str(
        weight_tuning.get("recommendation_reason") or ""
    )
    resolved_rows = int(weight_tuning.get("resolved_rows") or 0)
    total_rows    = int(weight_tuning.get("total_rows") or 0)
    low_sample    = resolved_rows < 20

    # Best candidate's metrics
    best_metrics: dict[str, Any] = {}
    for c in (weight_tuning.get("candidates") or []):
        if isinstance(c, dict) and c.get("name") == best_candidate:
            best_metrics = c
            break

    sim_d = dict(alloc_simulation.get("delta") or {})
    return {
        "best_weight_candidate":          best_candidate,
        "recommendation_reason":          reason,
        "total_feedback_rows":            total_rows,
        "resolved_feedback_rows":         resolved_rows,
        "low_sample_warning":             low_sample,
        "best_top_quartile_hit_rate":     best_metrics.get("top_quartile_hit_rate"),
        "best_top_quartile_avg_return":   best_metrics.get("top_quartile_avg_return"),
        "best_sample_size":               best_metrics.get("sample_size"),
        "simulation_efficiency_delta":    round(_flt(sim_d.get("efficiency_delta"), 0.0), 4),
        "simulation_total_return_delta":  round(_flt(sim_d.get("total_return_delta"), 0.0), 4),
    }


# ---------------------------------------------------------------------------
# Section G: Data Health
# ---------------------------------------------------------------------------

def compute_data_health(
    signals: dict[str, Any],
    artifact_flags: dict[str, bool],
) -> dict[str, Any]:
    """Report data coverage, degraded mode, and missing artifact flags."""
    all_sigs = _signal_list(signals)
    degraded_mode = bool(signals.get("degraded_mode") or signals.get("data_mode") == "degraded")
    data_mode     = str(signals.get("data_mode") or "unknown")

    eligible_count = sum(1 for s in all_sigs if s.get("filter_allowed"))
    missing = [name for name, exists in artifact_flags.items() if not exists]

    return {
        "degraded_mode":         degraded_mode,
        "data_mode":             data_mode,
        "total_signals":         len(all_sigs),
        "eligible_signals":      eligible_count,
        "missing_artifacts":     missing,
        "missing_artifact_count":len(missing),
        "all_artifacts_present": len(missing) == 0,
        "artifact_flags":        dict(artifact_flags),
    }


# ---------------------------------------------------------------------------
# Section H: Changes Since Last Run
# ---------------------------------------------------------------------------

def compute_changes(
    current: dict[str, Any],
    previous: dict[str, Any],
) -> dict[str, Any]:
    """
    Diff current summary against the previous one.
    Both inputs are the top-level summary dicts (not individual sections).
    """
    if not previous:
        return {
            "previous_available": False,
            "change_count": 0,
            "changes": [],
            "summary_line": "No previous summary to compare against.",
        }

    changes: list[str] = []

    prev_theme = (previous.get("top_theme") or {}).get("name")
    curr_theme = (current.get("top_theme") or {}).get("name")
    if prev_theme and curr_theme and prev_theme != curr_theme:
        changes.append(f"Top theme changed: {prev_theme} → {curr_theme}")

    prev_opp = (previous.get("top_opportunity") or {}).get("ticker")
    curr_opp = (current.get("top_opportunity") or {}).get("ticker")
    if prev_opp and curr_opp and prev_opp != curr_opp:
        changes.append(f"Top opportunity changed: {prev_opp} → {curr_opp}")

    prev_ws = (previous.get("system_state") or {}).get("ranking_weights_source")
    curr_ws = (current.get("system_state") or {}).get("ranking_weights_source")
    if prev_ws and curr_ws and prev_ws != curr_ws:
        changes.append(f"Ranking weights source changed: {prev_ws} → {curr_ws}")

    prev_wc = (previous.get("system_state") or {}).get("ranking_weights_candidate")
    curr_wc = (current.get("system_state") or {}).get("ranking_weights_candidate")
    if prev_wc and curr_wc and prev_wc != curr_wc:
        changes.append(f"Recommended weights changed: {prev_wc} → {curr_wc}")

    prev_ap = (previous.get("system_state") or {}).get("allocation_policy_status")
    curr_ap = (current.get("system_state") or {}).get("allocation_policy_status")
    if prev_ap and curr_ap and prev_ap != curr_ap:
        changes.append(f"Allocation policy changed: {prev_ap} → {curr_ap}")

    prev_fit = (previous.get("best_portfolio_fit") or {}).get("ticker")
    curr_fit = (current.get("best_portfolio_fit") or {}).get("ticker")
    if prev_fit and curr_fit and prev_fit != curr_fit:
        changes.append(f"Best portfolio fit changed: {prev_fit} → {curr_fit}")

    if not changes:
        summary_line = "No significant changes since last run."
    else:
        summary_line = f"{len(changes)} change{'s' if len(changes) != 1 else ''} detected."

    return {
        "previous_available": True,
        "previous_generated_at": previous.get("generated_at"),
        "change_count": len(changes),
        "changes": changes,
        "summary_line": summary_line,
    }


# ---------------------------------------------------------------------------
# Core builder (pure — no I/O)
# ---------------------------------------------------------------------------

def build_system_decision_summary(
    artifacts: dict[str, Any],
    artifact_flags: dict[str, bool],
    *,
    previous_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build the full system decision summary from pre-loaded artifacts.

    All computation is pure — no I/O, no mutations.
    previous_summary is used for change detection; pass None if unavailable.
    """
    signals          = dict(artifacts.get("signals") or {})
    themes           = dict(artifacts.get("themes") or {})
    ranking_config   = dict(artifacts.get("ranking_config") or {})
    alloc_policy     = dict(artifacts.get("alloc_policy") or {})
    alloc_preview    = dict(artifacts.get("alloc_preview") or {})
    alloc_simulation = dict(artifacts.get("alloc_simulation") or {})
    weight_tuning    = dict(artifacts.get("weight_tuning") or {})

    top_theme       = compute_top_theme(themes)
    top_opportunity = compute_top_opportunity(signals)
    best_fit        = compute_best_portfolio_fit(signals)
    system_state    = compute_system_state(ranking_config, alloc_policy, alloc_simulation, alloc_preview)
    capital_preview = compute_capital_preview(alloc_preview, alloc_simulation)
    policy_insight  = compute_policy_insight(weight_tuning, ranking_config, alloc_simulation)
    data_health     = compute_data_health(signals, artifact_flags)

    now_iso = datetime.now().isoformat()
    summary: dict[str, Any] = {
        "generated_at":      now_iso,
        "schema_version":    "1",
        "top_theme":         top_theme,
        "top_opportunity":   top_opportunity,
        "best_portfolio_fit":best_fit,
        "system_state":      system_state,
        "capital_preview":   capital_preview,
        "policy_insight":    policy_insight,
        "data_health":       data_health,
    }

    summary["changes"] = compute_changes(summary, previous_summary or {})
    return summary


# ---------------------------------------------------------------------------
# Markdown renderer (pure)
# ---------------------------------------------------------------------------

def _pct_str(val: float | None, places: int = 1) -> str:
    if val is None:
        return "—"
    return f"{val * 100:.{places}f}%"


def _delta_str(val: float | None, places: int = 4) -> str:
    if val is None:
        return "—"
    return f"{val:+.{places}f}"


def render_markdown(summary: dict[str, Any]) -> str:
    """
    Render the summary dict as a clean, human-readable Markdown document.

    Beginner-friendly: plain language for section headings and descriptions.
    Advanced-friendly: all numeric values and policy flags preserved verbatim.
    """
    lines: list[str] = []
    gen_at = str(summary.get("generated_at") or "")
    gen_display = gen_at[:19].replace("T", " ") if gen_at else "unknown"

    lines.append("# System Decision Summary")
    lines.append(f"_Generated {gen_display}_")
    lines.append("")

    # ── Top Theme ──
    lines.append("## Top Theme")
    tt = summary.get("top_theme") or {}
    if tt:
        score = tt.get("score", 0.0)
        lines.append(
            f"**{tt.get('name', '—')}** ({tt.get('type', 'classified').title()})"
            f"  —  Score: {score:.3f}"
        )
        persist   = tt.get("persistence", 0.0)
        accel     = tt.get("acceleration", 0.0)
        lines.append(
            f"Persistence: {persist:.3f}  ·  Acceleration: {accel:+.3f}"
        )
        tickers = tt.get("tickers") or []
        if tickers:
            lines.append(f"Tickers: {', '.join(str(t) for t in tickers)}")
    else:
        lines.append("_No theme data available. Run the theme discovery pipeline to populate._")
    lines.append("")

    # ── Top Opportunity ──
    lines.append("## Top Opportunity")
    to = summary.get("top_opportunity") or {}
    if to:
        lines.append(
            f"**{to.get('ticker', '—')}**  "
            f"—  Rank Score: {to.get('final_rank_score', 0.0):.3f}"
        )
        lines.append(
            f"Signal: {to.get('signal_score', 0.0):.3f}  "
            f"·  Confidence: {to.get('confidence', 0.0):.3f}  "
            f"·  Conviction Band: {to.get('conviction_band', '—')}"
        )
        lines.append(
            f"Theme: {to.get('theme_alignment_label', '—')}  "
            f"·  Portfolio Fit: {to.get('portfolio_fit_label', '—')}  "
            f"·  Rank Multiplier: ×{to.get('rank_multiplier', 1.0):.2f}"
        )
    else:
        lines.append("_No eligible signals found._")
    lines.append("")

    # ── Best Portfolio Fit ──
    lines.append("## Best Portfolio Fit")
    bf = summary.get("best_portfolio_fit") or {}
    if bf:
        lines.append(
            f"**{bf.get('ticker', '—')}**  "
            f"—  Fit Score: {bf.get('portfolio_fit_score', 0.0):.3f}  "
            f"({bf.get('portfolio_fit_label', '—').title()})"
        )
        reason = bf.get("portfolio_fit_reason", "")
        if reason:
            lines.append(f"Reason: {reason}")
    else:
        lines.append("_Portfolio fit scores not available._")
    lines.append("")

    # ── Capital Allocation Preview ──
    lines.append("## Capital Allocation Preview")
    cp = summary.get("capital_preview") or {}
    lines.append(
        f"Candidates: {cp.get('candidate_count', 0)}  "
        f"·  Baseline: {_pct_str(cp.get('total_baseline_pct'))}  "
        f"·  Rank-Aware Preview: {_pct_str(cp.get('total_preview_pct'))}  "
        f"·  Delta: {_delta_str(cp.get('preview_vs_baseline_delta'), 4)}"
    )
    sim_sample = cp.get("simulation_sample_size", 0)
    if sim_sample > 0:
        lines.append(
            f"Simulation ({sim_sample} signals):  "
            f"Efficiency Δ {_delta_str(cp.get('simulation_efficiency_delta'))}  "
            f"·  Return Δ {_delta_str(cp.get('simulation_return_delta'))}"
        )
        lines.append(
            f"Capital efficiency — Baseline: {cp.get('baseline_capital_efficiency', 0.0):.4f}  "
            f"·  Rank-Aware: {cp.get('rank_aware_capital_efficiency', 0.0):.4f}"
        )
    lines.append("")

    # ── Policy Status ──
    lines.append("## Policy Status")
    ss = summary.get("system_state") or {}
    ws = ss.get("ranking_weights_source", "default")
    wc = ss.get("ranking_weights_candidate", "current")
    ap = ss.get("allocation_policy_status", "not_approved")
    atl = ss.get("applied_to_live", False)
    lines.append(f"- Ranking weights: **{ws}** (candidate: {wc})")
    lines.append(f"- Allocation policy: **{ap}**  ·  applied_to_live: {atl}")
    lines.append(
        f"- Simulation: observe_only={ss.get('simulation_observe_only', True)}  "
        f"·  not_applied={ss.get('simulation_not_applied', True)}  "
        f"·  sample={ss.get('simulation_sample_size', 0)}"
    )
    lines.append(
        f"- Preview: observe_only={ss.get('preview_observe_only', True)}  "
        f"·  not_applied={ss.get('preview_not_applied', True)}"
    )
    if ss.get("policy_low_sample_warning"):
        lines.append(f"- ⚠ Low sample warning: policy_sample_size={ss.get('policy_sample_size', 0)}")
    lines.append("")

    # ── Policy Insight ──
    lines.append("## Policy Insight")
    pi = summary.get("policy_insight") or {}
    best = pi.get("best_weight_candidate", "current")
    reason = pi.get("recommendation_reason", "")
    lines.append(f"Best weight candidate: **{best}**")
    if reason:
        lines.append(f"Reason: {reason}")
    hit = pi.get("best_top_quartile_hit_rate")
    avg_ret = pi.get("best_top_quartile_avg_return")
    sample = pi.get("best_sample_size")
    if hit is not None:
        lines.append(
            f"Top-quartile hit rate: {hit:.1%}  "
            f"·  Avg return: {avg_ret:+.3f}%  "
            f"·  Sample: {sample}"
            if avg_ret is not None else
            f"Top-quartile hit rate: {hit:.1%}  ·  Sample: {sample}"
        )
    if pi.get("low_sample_warning"):
        lines.append(
            f"⚠ Low sample: {pi.get('resolved_feedback_rows', 0)} resolved "
            f"of {pi.get('total_feedback_rows', 0)} rows (need ≥20)"
        )
    sim_eff = pi.get("simulation_efficiency_delta", 0.0)
    sim_ret = pi.get("simulation_total_return_delta", 0.0)
    if sim_eff != 0.0 or sim_ret != 0.0:
        lines.append(
            f"Rank-aware simulation: efficiency Δ {sim_eff:+.4f}  "
            f"·  return Δ {sim_ret:+.4f}"
        )
    lines.append("")

    # ── Data Health ──
    lines.append("## Data Health")
    dh = summary.get("data_health") or {}
    degraded = dh.get("degraded_mode", False)
    lines.append(f"- Degraded mode: {'**Yes — data quality reduced**' if degraded else 'No'}")
    lines.append(f"- Data mode: {dh.get('data_mode', 'unknown')}")
    lines.append(
        f"- Signals: {dh.get('total_signals', 0)} total  "
        f"·  {dh.get('eligible_signals', 0)} alert-eligible"
    )
    missing = dh.get("missing_artifacts") or []
    if missing:
        lines.append(f"- Missing artifacts ({len(missing)}): {', '.join(missing)}")
    else:
        lines.append("- All expected artifacts present")
    lines.append("")

    # ── Changes Since Last Run ──
    lines.append("## Changes Since Last Run")
    ch = summary.get("changes") or {}
    lines.append(ch.get("summary_line", "No change data available."))
    change_list = ch.get("changes") or []
    for c in change_list:
        lines.append(f"- {c}")
    if not ch.get("previous_available"):
        lines.append("_This is the first summary or no previous summary was found._")
    prev_gen = ch.get("previous_generated_at", "")
    if prev_gen:
        lines.append(f"_Previous generated: {prev_gen[:19].replace('T', ' ')}_")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def generate_system_decision_summary(
    *,
    root: Path | str | None = None,
    write_files: bool = True,
) -> dict[str, Any]:
    """
    Load all artifacts, build the summary, write JSON + Markdown, return summary dict.

    write_files=False is useful for testing or dry-run inspection.
    """
    root_path = Path(root) if root is not None else Path(__file__).resolve().parents[2]

    json_path = root_path.joinpath(*_SUMMARY_JSON_REL)
    md_path   = root_path.joinpath(*_SUMMARY_MD_REL)

    # Load previous summary before we overwrite it
    previous_summary: dict[str, Any] = {}
    if json_path.exists():
        try:
            prev = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(prev, dict):
                previous_summary = prev
        except Exception:
            pass

    artifacts     = _load_artifacts(root_path)
    flags         = _artifact_flags(root_path)
    summary       = build_system_decision_summary(artifacts, flags, previous_summary=previous_summary)
    markdown      = render_markdown(summary)

    if write_files:
        out_dir = json_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        md_path.write_text(markdown, encoding="utf-8")
        logger.info(
            "system_summary: wrote summary to %s and %s",
            json_path,
            md_path,
        )

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m watchlist_scanner.system_summary",
        description=(
            "Build a unified System Decision Summary from all pipeline artifacts. "
            "Outputs JSON and Markdown to outputs/latest/. No live behavior changes."
        ),
    )
    parser.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="Project root (default: two levels above this module)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate and print without writing output files",
    )
    args = parser.parse_args()

    summary = generate_system_decision_summary(
        root=args.root,
        write_files=not args.dry_run,
    )

    print(f"Top theme:       {(summary.get('top_theme') or {}).get('name', '—')}")
    print(f"Top opportunity: {(summary.get('top_opportunity') or {}).get('ticker', '—')}")
    print(f"Weights source:  {(summary.get('system_state') or {}).get('ranking_weights_source', '—')}")
    print(f"Alloc policy:    {(summary.get('system_state') or {}).get('allocation_policy_status', '—')}")
    print(f"Changes:         {(summary.get('changes') or {}).get('summary_line', '—')}")

    if args.dry_run:
        print("\n[DRY-RUN] No files written.")
    else:
        print("\nFiles written:")
        print("  outputs/latest/system_decision_summary.json")
        print("  outputs/latest/system_decision_summary.md")


if __name__ == "__main__":
    _main()
