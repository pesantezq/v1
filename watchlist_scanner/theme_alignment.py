"""
Theme alignment: loads discovered-theme artifacts and computes per-symbol theme signals.

Read-only enrichment layer (except for theme boost which intentionally modifies signal
inputs when both alignment and LLM strength thresholds are met).
All public functions are safe to call when the theme artifact is absent or malformed.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("watchlist_scanner.theme_alignment")

_THEME_WEIGHT_DEFAULT: float = 0.15
_LABEL_STRONG: float = 0.65
_LABEL_MODERATE: float = 0.35
_BOOST_ALIGNMENT_THRESHOLD: float = 0.6
_BOOST_STRENGTH_THRESHOLD: float = 0.6


def load_theme_opportunities(root: Path | str) -> list[dict]:
    """
    Load outputs/latest/theme_opportunities.json relative to *root*.

    Returns empty list when file is absent, malformed, or has no themes.
    """
    path = Path(root) / "outputs" / "latest" / "theme_opportunities.json"
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("theme_alignment: could not load %s — %s", path, exc)
        return []
    themes = data.get("themes")
    if not isinstance(themes, list):
        return []
    return [t for t in themes if isinstance(t, dict)]


def load_theme_signals(root: Path | str) -> list[dict]:
    """
    Load outputs/latest/theme_signals.json relative to *root*.

    Returns enriched LLM-detected themes (with 'tickers' from ThemeMapper).
    Returns empty list when file is absent, malformed, stale, or has no themes.
    """
    path = Path(root) / "outputs" / "latest" / "theme_signals.json"
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("theme_alignment: could not load %s — %s", path, exc)
        return []
    themes = data.get("themes")
    if not isinstance(themes, list):
        return []
    return [t for t in themes if isinstance(t, dict)]


def match_symbol_themes(symbol: str, themes: list[dict]) -> list[dict]:
    """
    Return all theme dicts whose 'tickers' list contains *symbol*.

    Deterministically ordered: score descending, name ascending within ties.
    """
    matched = [t for t in themes if symbol in (t.get("tickers") or [])]
    matched.sort(key=lambda t: (-float(t.get("score") or 0.0), str(t.get("name") or "")))
    return matched


def compute_theme_alignment(matched_themes: list[dict]) -> dict[str, Any]:
    """
    Compute per-symbol theme alignment fields from matched theme dicts.

    Formula (conservative, explainable):
      strongest_component  = top_theme_score * top_theme_confidence
      persistence_component = max(persistence_score across matched)
      acceleration_component = max(acceleration_score across matched)
      breadth_component     = min(match_count / 3, 1.0)

      raw_alignment = (
          0.50 * strongest_component
        + 0.20 * persistence_component
        + 0.20 * acceleration_component
        + 0.10 * breadth_component
      )
      theme_alignment_score = clamp(raw_alignment, 0.0, 1.0)

    Labels: >=0.65 → strong, >=0.35 → moderate, >0 → weak, 0 → none
    """
    if not matched_themes:
        return _empty_theme_fields()

    top = matched_themes[0]  # already sorted score desc
    names = list(dict.fromkeys(str(t.get("name") or "") for t in matched_themes))
    types = list(dict.fromkeys(str(t.get("theme_type") or "") for t in matched_themes))

    max_score = max(float(t.get("score") or 0.0) for t in matched_themes)
    max_confidence = max(float(t.get("confidence") or 0.0) for t in matched_themes)
    max_persistence = max(float(t.get("persistence_score") or 0.0) for t in matched_themes)
    max_acceleration = max(float(t.get("acceleration_score") or 0.0) for t in matched_themes)
    max_source_count = max(int(t.get("source_count") or 0) for t in matched_themes)
    count = len(matched_themes)

    strongest_component = float(top.get("score") or 0.0) * float(top.get("confidence") or 0.0)
    persistence_component = max_persistence
    acceleration_component = max_acceleration
    breadth_component = min(count / 3.0, 1.0)

    raw_alignment = (
        0.50 * strongest_component
        + 0.20 * persistence_component
        + 0.20 * acceleration_component
        + 0.10 * breadth_component
    )
    alignment_score = round(min(max(raw_alignment, 0.0), 1.0), 4)
    alignment_label = _alignment_label(alignment_score)

    top_name = str(top.get("name") or "")
    top_type = str(top.get("theme_type") or "")
    if count == 1:
        reason = f"Supported by {top_type} theme '{top_name}'"
        if max_persistence >= 0.5:
            reason += " with persistent multi-run strength"
    else:
        reason = f"Matched {count} themes; strongest support from {top_type} theme '{top_name}'"

    return {
        "theme_support_present": True,
        "theme_match_count": count,
        "theme_names": names,
        "theme_types": types,
        "theme_source_count": max_source_count,
        "theme_max_score": round(max_score, 4),
        "theme_max_confidence": round(max_confidence, 4),
        "theme_max_persistence_score": round(max_persistence, 4),
        "theme_max_acceleration_score": round(max_acceleration, 4),
        "theme_alignment_score": alignment_score,
        "theme_alignment_label": alignment_label,
        "theme_top_name": top_name,
        "theme_top_type": top_type,
        "theme_top_score": round(float(top.get("score") or 0.0), 4),
        "theme_top_confidence": round(float(top.get("confidence") or 0.0), 4),
        "theme_top_persistence_score": round(float(top.get("persistence_score") or 0.0), 4),
        "theme_top_acceleration_score": round(float(top.get("acceleration_score") or 0.0), 4),
        "theme_reason": reason,
        "theme_context": {
            "names": names,
            "types": types,
            "alignment_score": alignment_score,
            "strongest_component": round(strongest_component, 4),
            "persistence_component": round(persistence_component, 4),
            "acceleration_component": round(acceleration_component, 4),
            "breadth_component": round(breadth_component, 4),
        },
    }


def compute_theme_alignment_from_lm(symbol: str, lm_themes: list[dict]) -> dict[str, Any]:
    """
    Compute alignment fields from LLM theme_signals.json data.

    Used when theme_opportunities.json is absent or yields no keyword-based match.
    Matches symbol against themes[].tickers and derives alignment from LLM
    confidence.  The formula weights confidence heavily since it is the primary
    LLM quality signal; persistence_7d is normalised to [0, 1] over a 7-day window.

    Formula:
      raw_alignment = 0.70 * max_confidence
                    + 0.20 * min(max_persistence_7d / 7, 1.0)
                    + 0.10 * min(match_count / 3, 1.0)
    """
    matched = [t for t in lm_themes if symbol in (t.get("tickers") or [])]
    if not matched:
        return _empty_theme_fields()

    matched.sort(key=lambda t: -float(t.get("confidence") or 0.0))
    top = matched[0]
    names = list(dict.fromkeys(str(t.get("name") or "") for t in matched))
    count = len(matched)

    max_confidence = max(float(t.get("confidence") or 0.0) for t in matched)
    max_persistence_7d = max(int(t.get("persistence_7d") or 0) for t in matched)
    persistence_component = min(max_persistence_7d / 7.0, 1.0)
    breadth_component = min(count / 3.0, 1.0)
    max_evidence = max(len(t.get("evidence_items") or []) for t in matched)

    raw_alignment = (
        0.70 * max_confidence
        + 0.20 * persistence_component
        + 0.10 * breadth_component
    )
    alignment_score = round(min(max(raw_alignment, 0.0), 1.0), 4)
    top_name = str(top.get("name") or "")
    top_confidence = round(float(top.get("confidence") or 0.0), 4)
    top_persistence = round(min(int(top.get("persistence_7d") or 0) / 7.0, 1.0), 4)

    return {
        "theme_support_present": True,
        "theme_match_count": count,
        "theme_names": names,
        "theme_types": ["llm"],
        "theme_source_count": max_evidence,
        "theme_max_score": round(max_confidence, 4),
        "theme_max_confidence": round(max_confidence, 4),
        "theme_max_persistence_score": round(persistence_component, 4),
        "theme_max_acceleration_score": 0.0,
        "theme_alignment_score": alignment_score,
        "theme_alignment_label": _alignment_label(alignment_score),
        "theme_top_name": top_name,
        "theme_top_type": "llm",
        "theme_top_score": top_confidence,
        "theme_top_confidence": top_confidence,
        "theme_top_persistence_score": top_persistence,
        "theme_top_acceleration_score": 0.0,
        "theme_reason": (
            f"LLM-detected theme '{top_name}'"
            if count == 1
            else f"LLM-detected {count} themes; strongest: '{top_name}'"
        ),
        "theme_context": {
            "names": names,
            "types": ["llm"],
            "alignment_score": alignment_score,
            "strongest_component": round(max_confidence, 4),
            "persistence_component": round(persistence_component, 4),
            "acceleration_component": 0.0,
            "breadth_component": round(breadth_component, 4),
            "source": "theme_signals",
        },
    }


def enrich_row_with_theme(
    row: dict[str, Any],
    themes: list[dict],
    theme_weight: float = _THEME_WEIGHT_DEFAULT,
    lm_themes: list[dict] | None = None,
) -> None:
    """
    Add theme alignment and boost fields to *row* in-place.

    Alignment source priority:
      1. Keyword themes from theme_opportunities.json (when present and ticker matched)
      2. LLM themes from theme_signals.json (fallback when keyword themes absent/no match)

    Sets all theme_* explainability fields, theme_component, theme_strength_score,
    and augmented_signal_score.  When both theme_alignment_score and
    theme_strength_score meet their thresholds (≥0.6 each), applies a
    multiplicative boost to signal_score and confidence_score before computing
    augmented_signal_score, and records theme_boost_applied/theme_boost_factor.

    Never raises — all errors are silently logged.
    """
    try:
        symbol = str(row.get("ticker") or "")
        matched = match_symbol_themes(symbol, themes)
        if matched:
            fields = compute_theme_alignment(matched)
        else:
            # theme_opportunities.json absent or no keyword match for this ticker —
            # fall back to LLM themes from theme_signals.json
            fields = compute_theme_alignment_from_lm(symbol, lm_themes or [])
        row.update(fields)

        signal_score = float(row.get("signal_score") or 0.0)
        theme_component = round(fields["theme_alignment_score"] * theme_weight, 4)
        row["theme_component"] = theme_component

        # Compute LLM theme strength from theme_signals.json
        theme_strength_score = _compute_theme_strength(symbol, lm_themes or [])
        row["theme_strength_score"] = round(theme_strength_score, 4)

        # Apply boost when both alignment and LLM strength pass thresholds
        alignment_score = fields["theme_alignment_score"]
        if (
            alignment_score >= _BOOST_ALIGNMENT_THRESHOLD
            and theme_strength_score >= _BOOST_STRENGTH_THRESHOLD
        ):
            boost_factor = round(1.0 + 0.15 * theme_strength_score, 4)
            conf_factor = round(1.0 + 0.10 * theme_strength_score, 4)
            signal_score = round(min(signal_score * boost_factor, 1.0), 4)
            confidence_score = float(row.get("confidence_score") or 0.0)
            row["signal_score"] = signal_score
            row["confidence_score"] = round(min(confidence_score * conf_factor, 1.0), 4)
            row["theme_boost_applied"] = True
            row["theme_boost_factor"] = boost_factor
        else:
            row["theme_boost_applied"] = False
            row["theme_boost_factor"] = 1.0

        row["augmented_signal_score"] = round(min(signal_score + theme_component, 1.0), 4)
    except Exception as exc:
        logger.warning("theme_alignment: error enriching %s — %s", row.get("ticker"), exc)
        _apply_empty_fallback(row)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _compute_theme_strength(symbol: str, lm_themes: list[dict]) -> float:
    """Return max LLM confidence among themes whose 'tickers' list contains *symbol*."""
    best = 0.0
    for t in lm_themes:
        if symbol in (t.get("tickers") or []):
            conf = float(t.get("confidence") or 0.0)
            if conf > best:
                best = conf
    return best


def _alignment_label(score: float) -> str:
    if score <= 0.0:
        return "none"
    if score < _LABEL_MODERATE:
        return "weak"
    if score < _LABEL_STRONG:
        return "moderate"
    return "strong"


def _empty_theme_fields() -> dict[str, Any]:
    return {
        "theme_support_present": False,
        "theme_match_count": 0,
        "theme_names": [],
        "theme_types": [],
        "theme_source_count": 0,
        "theme_max_score": 0.0,
        "theme_max_confidence": 0.0,
        "theme_max_persistence_score": 0.0,
        "theme_max_acceleration_score": 0.0,
        "theme_alignment_score": 0.0,
        "theme_alignment_label": "none",
        "theme_top_name": None,
        "theme_top_type": None,
        "theme_top_score": 0.0,
        "theme_top_confidence": 0.0,
        "theme_top_persistence_score": 0.0,
        "theme_top_acceleration_score": 0.0,
        "theme_reason": "No matching themes",
        "theme_context": {},
    }


def _apply_empty_fallback(row: dict[str, Any]) -> None:
    for k, v in _empty_theme_fields().items():
        row.setdefault(k, v)
    signal_score = float(row.get("signal_score") or 0.0)
    row.setdefault("theme_component", 0.0)
    row.setdefault("augmented_signal_score", round(signal_score, 4))
    row.setdefault("theme_strength_score", 0.0)
    row.setdefault("theme_boost_applied", False)
    row.setdefault("theme_boost_factor", 1.0)
