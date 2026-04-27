"""
Persistence and acceleration tracking across theme_discovery runs.

History is stored as a rolling JSON log in outputs/history/theme_history.json.
Each run appends a lightweight snapshot; the file is capped at max_runs entries.

Acceleration uses a sigmoid-normalized ratio of recent vs prior average mention
counts so scores stay in [0, 1] and are interpretable:
  ratio = recent_avg / max(prior_avg, 1.0)
  acceleration = ratio / (ratio + 1.0)
  → 0.5 when flat, approaching 1.0 when strongly accelerating, 0.0 when faded
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_EMPTY: dict = {"runs": []}


def load_theme_history(path: Path) -> dict:
    """
    Load run history from disk.

    Returns {"runs": []} on missing file, read error, or schema mismatch.
    Never raises.
    """
    if not path.exists():
        return {"runs": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
            logger.warning("theme_history: malformed schema at %s — resetting", path)
            return {"runs": []}
        return data
    except Exception as exc:
        logger.warning("theme_history: could not load %s: %s — using empty history", path, exc)
        return {"runs": []}


def update_theme_history(
    path: Path,
    generated_at: str,
    opportunities: list,   # list[ThemeOpportunity] — typed loosely to avoid circular import
    max_runs: int = 60,
) -> None:
    """
    Append the current run snapshot to history and write to disk.

    The snapshot stores only the lightweight fields needed for future
    persistence/acceleration computations.
    """
    history = load_theme_history(path)

    snapshot = {
        "generated_at": generated_at,
        "themes": [
            {
                "name": opp.name,
                "theme_type": opp.theme_type,
                "score": float(opp.score),
                "mention_count": int(opp.mention_count),
                "source_count": int(opp.source_count),
            }
            for opp in opportunities
        ],
    }

    history["runs"].append(snapshot)

    if len(history["runs"]) > max_runs:
        history["runs"] = history["runs"][-max_runs:]

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("theme_history: could not write %s: %s", path, exc)


def compute_history_metrics(
    name: str,
    theme_type: str,
    history: dict,
    persistence_window: int = 10,
    recent_window: int = 3,
    prior_window: int = 5,
) -> dict:
    """
    Return persistence/acceleration metrics for one (name, theme_type) pair.

    Keys returned:
        persistence_score   float [0, 1]
        acceleration_score  float [0, 1]
        history_runs_seen   int
        first_seen          str | None
        last_seen           str | None
    """
    runs: list[dict] = history.get("runs", [])
    if not runs:
        return _neutral()

    def _theme_entry(run: dict) -> dict | None:
        for t in run.get("themes", []):
            if t.get("name") == name and t.get("theme_type") == theme_type:
                return t
        return None

    n = len(runs)

    # --- persistence ---
    window = runs[-persistence_window:] if n >= persistence_window else runs
    hits = sum(1 for r in window if _theme_entry(r) is not None)
    persistence_score = hits / len(window) if window else 0.0

    # --- history metadata ---
    history_runs_seen = sum(1 for r in runs if _theme_entry(r) is not None)
    matching_ts = [r["generated_at"] for r in runs if _theme_entry(r) is not None]
    first_seen = matching_ts[0] if matching_ts else None
    last_seen = matching_ts[-1] if matching_ts else None

    # --- acceleration ---
    if history_runs_seen == 0:
        # Brand-new theme not yet in history — neutral
        acceleration_score = 0.5
    else:
        recent_runs = runs[-recent_window:] if n >= recent_window else runs
        end_of_prior = max(0, n - recent_window)
        prior_runs = runs[max(0, end_of_prior - prior_window):end_of_prior]

        def _mentions(run: dict) -> float:
            e = _theme_entry(run)
            return float(e["mention_count"]) if e is not None else 0.0

        recent_avg = (
            sum(_mentions(r) for r in recent_runs) / len(recent_runs)
            if recent_runs else 0.0
        )
        prior_avg = (
            sum(_mentions(r) for r in prior_runs) / len(prior_runs)
            if prior_runs else 0.0
        )

        if not prior_runs:
            # Not enough history for a prior window — neutral
            acceleration_score = 0.5
        else:
            ratio = recent_avg / max(prior_avg, 1.0)
            # sigmoid-like: ratio=0→0.0, ratio=1→0.5, ratio=2→0.67, ratio=4→0.80
            acceleration_score = ratio / (ratio + 1.0)

    return {
        "persistence_score": round(min(persistence_score, 1.0), 4),
        "acceleration_score": round(min(acceleration_score, 1.0), 4),
        "history_runs_seen": history_runs_seen,
        "first_seen": first_seen,
        "last_seen": last_seen,
    }


def _neutral() -> dict:
    return {
        "persistence_score": 0.0,
        "acceleration_score": 0.5,
        "history_runs_seen": 0,
        "first_seen": None,
        "last_seen": None,
    }
