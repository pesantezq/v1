"""
WS13 fix — container-key mismatch in `_enrich_baseline` + gated opt-in for
`experiment_watchlist_discovery_adds`, plus per-experiment input diagnostics.

Background (see .superpowers/audit/ws-13-15-16-universe-experiments.md):
`_enrich_baseline` (daily_governance_run.py) read
`automatic_promotion_candidates.json` via `.get("candidates", [])`, but the
real container key the producer writes is `"decisions"`. This has silently
returned 0 candidates for `experiment_watchlist_discovery_adds` since the
function's original commit (66218b39, 2026-06-16) — never a regression, it
never worked. Fixing the key makes a previously-dead experiment start
emitting real candidates, which is gated behind an explicit, default-OFF
config flag with a kill-switch (operator opt-in required).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from portfolio_automation.sim_governance import daily_governance_run as DGR
from portfolio_automation.sim_governance import simulation_lane as LANE


def _real_shaped_artifact(decisions: list[dict]) -> dict:
    """Shape matches automatic_promotion_governance._report_to_dict exactly
    (real container key is "decisions", never "candidates")."""
    return {
        "generated_at": "2026-07-28T10:31:54+00:00",
        "run_mode": "discovery",
        "decision_count": len(decisions),
        "decisions": decisions,
    }


def _monitor_decision(ticker: str, score: float = 0.82, risk: bool = False) -> dict:
    return {
        "ticker": ticker,
        "proposed_status": "MONITOR",
        "corroboration_score": score,
        "catalyst_flags": ["earnings_beat"],
        "risk_flags": ["high_volatility"] if risk else [],
    }


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "outputs" / "sandbox" / "discovery").mkdir(parents=True)
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_promo(root: Path, payload: dict) -> None:
    (root / "outputs" / "sandbox" / "discovery" / "automatic_promotion_candidates.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Container-key fix + gate default-OFF behavior
# ---------------------------------------------------------------------------


def test_default_gate_is_off(root: Path):
    cfg = DGR.load_sim_governance_config(root)
    assert cfg["experiments"]["watchlist_discovery_adds_enabled"] is False


def test_disabled_reproduces_prior_always_empty_behavior(root: Path):
    """Gate OFF (default) must byte-for-byte reproduce the historical (buggy)
    behavior: discovery_candidates == [] regardless of real decisions present."""
    _write_promo(root, _real_shaped_artifact([_monitor_decision("ABCD"), _monitor_decision("WXYZ")]))
    cfg = DGR.load_sim_governance_config(root)
    baseline = DGR._enrich_baseline(root, {"watchlist": [], "advisory": [], "crowd": {}}, cfg=cfg)
    assert baseline["discovery_candidates"] == []


def test_enabled_reads_real_decisions_key_not_candidates(root: Path):
    """The core fix: read 'decisions' (the real key), not 'candidates'."""
    _write_promo(root, _real_shaped_artifact([_monitor_decision("ABCD", score=0.82, risk=True)]))
    cfg = DGR.load_sim_governance_config(root)
    cfg["experiments"]["watchlist_discovery_adds_enabled"] = True
    baseline = DGR._enrich_baseline(root, {"watchlist": [], "advisory": [], "crowd": {}}, cfg=cfg)
    cands = baseline["discovery_candidates"]
    assert len(cands) == 1
    assert cands[0]["symbol"] == "ABCD"
    assert cands[0]["score"] == 0.82
    assert cands[0]["risk_impact"] == "medium"
    assert cands[0]["tags"] == ["earnings_beat"]


def test_enabled_filters_out_non_monitor_decisions(root: Path):
    """REJECTED/EXPIRED/hold_status decisions must never become watchlist-add
    candidates — only a decision actually promoted to MONITOR counts."""
    _write_promo(root, _real_shaped_artifact([
        _monitor_decision("GOOD"),
        {"ticker": "REJC", "proposed_status": "REJECTED", "corroboration_score": 0.95},
        {"ticker": "HOLD", "proposed_status": "DISCOVERED", "corroboration_score": 0.95},
        {"ticker": "EXPD", "proposed_status": "EXPIRED", "corroboration_score": 0.95},
        {"ticker": "REVU", "proposed_status": "NEEDS_REVIEW", "corroboration_score": 0.95},
    ]))
    cfg = DGR.load_sim_governance_config(root)
    cfg["experiments"]["watchlist_discovery_adds_enabled"] = True
    baseline = DGR._enrich_baseline(root, {"watchlist": [], "advisory": [], "crowd": {}}, cfg=cfg)
    symbols = {c["symbol"] for c in baseline["discovery_candidates"]}
    assert symbols == {"GOOD"}


def test_enabled_end_to_end_produces_watchlist_add_candidates(root: Path):
    """Full wiring: enrich_baseline -> run_simulation_lane actually emits
    watchlist_add SimulationCandidates when enabled, proving the previously
    dead experiment is now live."""
    _write_promo(root, _real_shaped_artifact([_monitor_decision("ABCD"), _monitor_decision("WXYZ")]))
    cfg = DGR.load_sim_governance_config(root)
    cfg["experiments"]["watchlist_discovery_adds_enabled"] = True
    baseline = DGR._enrich_baseline(
        root, LANE.load_production_baseline(root), cfg=cfg)
    lane = LANE.run_simulation_lane(root, "2026-07-28T00:00:00+00:00",
                                     baseline=baseline, write_files=False)
    adds = [c for c in lane["candidates"] if c["proposal_type"] == "watchlist_add"]
    assert {c["symbol"] for c in adds} == {"ABCD", "WXYZ"}


def test_missing_artifact_degrades_to_empty_not_raise(root: Path):
    """Tolerant: no automatic_promotion_candidates.json at all -> []."""
    cfg = DGR.load_sim_governance_config(root)
    cfg["experiments"]["watchlist_discovery_adds_enabled"] = True
    baseline = DGR._enrich_baseline(root, {"watchlist": [], "advisory": [], "crowd": {}}, cfg=cfg)
    assert baseline["discovery_candidates"] == []


def test_malformed_artifact_degrades_to_empty_not_raise(root: Path):
    """Tolerant: decisions is not a list (or file isn't a dict) -> []."""
    (root / "outputs" / "sandbox" / "discovery" / "automatic_promotion_candidates.json").write_text(
        "not json at all {{{", encoding="utf-8"
    )
    cfg = DGR.load_sim_governance_config(root)
    cfg["experiments"]["watchlist_discovery_adds_enabled"] = True
    baseline = DGR._enrich_baseline(root, {"watchlist": [], "advisory": [], "crowd": {}}, cfg=cfg)
    assert baseline["discovery_candidates"] == []

    _write_promo(root, {"decisions": "not-a-list"})
    baseline2 = DGR._enrich_baseline(root, {"watchlist": [], "advisory": [], "crowd": {}}, cfg=cfg)
    assert baseline2["discovery_candidates"] == []


# ---------------------------------------------------------------------------
# Kill switches (fail-closed; win over config even when enabled=true)
# ---------------------------------------------------------------------------


def test_env_kill_switch_overrides_enabled_true(root: Path, monkeypatch):
    _write_promo(root, _real_shaped_artifact([_monitor_decision("ABCD")]))
    cfg = DGR.load_sim_governance_config(root)
    cfg["experiments"]["watchlist_discovery_adds_enabled"] = True
    monkeypatch.setenv("STOCKBOT_SIM_GOV_DISCOVERY_ADDS_DISABLED", "1")
    baseline = DGR._enrich_baseline(root, {"watchlist": [], "advisory": [], "crowd": {}}, cfg=cfg)
    assert baseline["discovery_candidates"] == []
    assert baseline["_experiment_gates"]["watchlist_discovery_adds"]["reason"] == "env_kill_switch"


def test_file_kill_switch_overrides_enabled_true(root: Path, monkeypatch):
    monkeypatch.delenv("STOCKBOT_SIM_GOV_DISCOVERY_ADDS_DISABLED", raising=False)
    _write_promo(root, _real_shaped_artifact([_monitor_decision("ABCD")]))
    (root / DGR._DISCOVERY_ADDS_KILL_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / DGR._DISCOVERY_ADDS_KILL_FILE).write_text("", encoding="utf-8")
    cfg = DGR.load_sim_governance_config(root)
    cfg["experiments"]["watchlist_discovery_adds_enabled"] = True
    baseline = DGR._enrich_baseline(root, {"watchlist": [], "advisory": [], "crowd": {}}, cfg=cfg)
    assert baseline["discovery_candidates"] == []
    assert baseline["_experiment_gates"]["watchlist_discovery_adds"]["reason"] == "file_kill_switch"


# ---------------------------------------------------------------------------
# Per-experiment diagnostics (WS13 item 5): a zero must never be structurally
# invisible again.
# ---------------------------------------------------------------------------


def _base_diag_baseline() -> dict:
    return {
        "watchlist": [], "advisory": [{"symbol": "AAPL", "decision": "HOLD"}],
        "crowd": {"AAPL": {"state": "rising", "velocity": 1.6, "confidence": 0.9}},
        "discovery_candidates": [], "watchlist_ranked": [],
        "flock": {},
    }


def test_rerank_is_marked_inert_no_producer():
    bl = _base_diag_baseline()
    lane = LANE.run_simulation_lane(".", "2026-07-28T00:00:00+00:00", baseline=bl,
                                     write_files=False,
                                     experiments=[LANE.experiment_watchlist_rerank])
    diag = lane["experiment_diagnostics"][0]
    assert diag["experiment"] == "experiment_watchlist_rerank"
    assert diag["classification"] == "INERT_NO_PRODUCER"
    assert diag["zero_expected"] is True
    assert diag["actual_input_count"] == 0


def test_discovery_adds_marked_inert_gated_off_when_disabled():
    bl = _base_diag_baseline()
    bl["_experiment_gates"] = {
        "watchlist_discovery_adds": {"enabled": False, "reason": "config_disabled_default"},
    }
    lane = LANE.run_simulation_lane(".", "2026-07-28T00:00:00+00:00", baseline=bl,
                                     write_files=False,
                                     experiments=[LANE.experiment_watchlist_discovery_adds])
    diag = lane["experiment_diagnostics"][0]
    assert diag["classification"] == "INERT_GATED_OFF"
    assert diag["zero_expected"] is True


def test_operational_experiment_with_input_but_zero_output_is_distinguished():
    """A distinct classification path: input present, but this run's own logic
    produced zero candidates — must NOT look like 'no admissible input'."""
    bl = _base_diag_baseline()
    bl["discovery_candidates"] = [{"symbol": "AAPL", "score": 0.9}]  # already on watchlist below
    bl["watchlist"] = ["AAPL"]  # dedup makes the experiment fire with 0 output
    bl["_experiment_gates"] = {
        "watchlist_discovery_adds": {"enabled": True, "reason": "config_enabled"},
    }
    lane = LANE.run_simulation_lane(".", "2026-07-28T00:00:00+00:00", baseline=bl,
                                     write_files=False,
                                     experiments=[LANE.experiment_watchlist_discovery_adds])
    diag = lane["experiment_diagnostics"][0]
    assert diag["actual_input_count"] == 1
    assert diag["candidate_count"] == 0
    assert diag["classification"] == "OPERATIONAL"
    assert diag["zero_expected"] is False
    assert "no candidate passed" in diag["reason"]


def test_broken_experiment_is_classified_broken():
    def _boom(baseline):
        raise RuntimeError("kaboom")
    _boom.__name__ = "experiment_watchlist_discovery_adds"
    bl = _base_diag_baseline()
    lane = LANE.run_simulation_lane(".", "2026-07-28T00:00:00+00:00", baseline=bl,
                                     write_files=False, experiments=[_boom])
    diag = lane["experiment_diagnostics"][0]
    assert diag["classification"] == "BROKEN"
    assert "kaboom" in diag["reason"]


def test_operational_experiment_with_candidates_is_healthy():
    bl = _base_diag_baseline()
    lane = LANE.run_simulation_lane(".", "2026-07-28T00:00:00+00:00", baseline=bl,
                                     write_files=False,
                                     experiments=[LANE.experiment_advisory_crowd_context])
    diag = lane["experiment_diagnostics"][0]
    assert diag["classification"] == "OPERATIONAL"
    assert diag["candidate_count"] >= 1
    assert diag["zero_expected"] is False
