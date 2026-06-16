"""Flock Intelligence — producer + data-source fallbacks + namespace isolation."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.flock_intelligence.producer import run_flock_intelligence


def _seed(root: Path, *, themes=None, crowd=None, outcomes=None, config=None):
    (root / "outputs" / "latest").mkdir(parents=True, exist_ok=True)
    (root / "outputs" / "performance").mkdir(parents=True, exist_ok=True)
    (root / "outputs" / "sandbox" / "discovery").mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps(config or {"portfolio": {"watchlist": []}}))
    if themes is not None:
        (root / "outputs" / "latest" / "theme_signals.json").write_text(json.dumps({"themes": themes}))
    if crowd is not None:
        (root / "outputs" / "sandbox" / "discovery" / "crowd_multi_source_velocity.json").write_text(
            json.dumps({"records": crowd}))
    if outcomes is not None:
        lines = ["ticker,signal_time,outcome_return_1d"]
        for tk, series in outcomes.items():
            for i, r in enumerate(series):
                lines.append(f"{tk},2026-06-{10 + i:02d}T09:00:00,{r}")
        (root / "outputs" / "performance" / "signal_outcomes.csv").write_text("\n".join(lines))


def test_missing_everything_degrades_not_raises(tmp_path):
    _seed(tmp_path)
    out = run_flock_intelligence(tmp_path, "2026-06-16T00:00:00Z", write_files=True)
    r = out["report"]
    assert r["observe_only"] is True and r["simulation_only"] is True
    assert r["group_count"] == 0
    assert r["data_quality_status"] == "insufficient_data"


def test_insufficient_group_size_classifies_insufficient(tmp_path):
    # A theme with <2 tickers is dropped by grouping; a 2-ticker theme with no
    # crowd/returns classifies insufficient_data, never raising.
    _seed(tmp_path, themes=[{"name": "Solo", "tickers": ["AAA", "BBB"]}], crowd=[], outcomes={})
    out = run_flock_intelligence(tmp_path, "2026-06-16T00:00:00Z", write_files=False)
    states = {g["group"]: g["flock_state"] for g in out["report"]["groups"]}
    assert states.get("Solo") == "insufficient_data"


def test_missing_prices_still_uses_crowd(tmp_path):
    # No price history -> correlation None, but crowd velocity still drives metrics.
    crowd = [{"ticker": "NVDA", "mention_velocity": 2.0, "source_breadth": 2},
             {"ticker": "AMD", "mention_velocity": 1.8, "source_breadth": 2}]
    _seed(tmp_path, themes=[{"name": "AI", "tickers": ["NVDA", "AMD"]}], crowd=crowd, outcomes={})
    out = run_flock_intelligence(tmp_path, "2026-06-16T00:00:00Z", write_files=False)
    g = out["report"]["groups"][0]
    assert g["crowd_velocity"] > 0
    assert g["price_correlation_to_group"] is None  # no returns to correlate


def test_writes_only_to_simulation_namespace(tmp_path):
    _seed(tmp_path, themes=[{"name": "AI", "tickers": ["NVDA", "AMD"]}],
          crowd=[{"ticker": "NVDA", "mention_velocity": 1.0, "source_breadth": 1}])
    run_flock_intelligence(tmp_path, "2026-06-16T00:00:00Z", write_files=True)
    sim = tmp_path / "outputs" / "simulation"
    assert (sim / "flock_intelligence.json").exists()
    assert (sim / "flock_watchlist_candidates.json").exists()
    assert (sim / "flock_advisory_context.json").exists()
    # Nothing leaked into production namespaces.
    assert not (tmp_path / "outputs" / "latest" / "flock_intelligence.json").exists()
    assert not (tmp_path / "outputs" / "portfolio" / "flock_intelligence.json").exists()
    assert not (tmp_path / "outputs" / "policy" / "flock_intelligence.json").exists()


def test_forming_group_yields_watchlist_add_candidate(tmp_path):
    # Correlated rising returns + crowd velocity, ticker NOT on the watchlist -> add.
    crowd = [{"ticker": "AAA", "mention_velocity": 0.8, "source_breadth": 1},
             {"ticker": "BBB", "mention_velocity": 0.7, "source_breadth": 1}]
    out = run_flock_intelligence(
        tmp_path, "2026-06-16T00:00:00Z", write_files=False,
        watchlist=[],  # nothing on the watchlist -> forming members are adds
        groups_override=[("Newco", "theme", ["AAA", "BBB"])],
        crowd_override={"AAA": {"velocity": 0.8, "breadth": 1, "mentions": 5},
                        "BBB": {"velocity": 0.7, "breadth": 1, "mentions": 5}},
        returns_override={"AAA": {"2026-06-12": 1.0, "2026-06-13": 2.0, "2026-06-14": 3.0},
                          "BBB": {"2026-06-12": 1.1, "2026-06-13": 2.1, "2026-06-14": 3.1}})
    g = out["report"]["groups"][0]
    assert g["flock_state"] == "flock_forming"
    actions = {(c["ticker"], c["action"]) for c in out["watchlist_candidates"]["candidates"]}
    assert ("AAA", "add") in actions and ("BBB", "add") in actions


def test_dispersing_detected_with_prior_state(tmp_path):
    # Seed a prior flock-state history so falling correlation -> dispersing.
    sim = tmp_path / "outputs" / "simulation"
    sim.mkdir(parents=True)
    (sim / "flock_state_history.json").write_text(json.dumps({"groups": {
        "AI": {"state": "flock_confirmed", "avg_correlation": 0.95, "volatility": 0.5}}}))
    out = run_flock_intelligence(
        tmp_path, "2026-06-16T00:00:00Z", write_files=False,
        groups_override=[("AI", "theme", ["AAA", "BBB"])],
        crowd_override={"AAA": {"velocity": 1.2, "breadth": 1, "mentions": 5},
                        "BBB": {"velocity": 1.1, "breadth": 1, "mentions": 5}},
        # now anti-correlated -> correlation collapses from prior 0.95
        returns_override={"AAA": {"2026-06-12": 1.0, "2026-06-13": 5.0, "2026-06-14": -2.0},
                          "BBB": {"2026-06-12": 4.0, "2026-06-13": -3.0, "2026-06-14": 6.0}})
    g = out["report"]["groups"][0]
    assert g["flock_state"] in ("flock_dispersing", "flock_broken")
