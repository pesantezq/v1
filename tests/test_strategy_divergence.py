"""
Tests for the WS5 active-strategy vs. top-ranked-tactic divergence artifact
(portfolio_automation/strategy/strategy_divergence.py).

Covers: the pure classify_divergence() decision function across all five labels,
the end-to-end compute_strategy_divergence() reader (including the real-shape
regression -- top tactic untested -> INSUFFICIENT_EVIDENCE, never tuned to a more
flattering label), the structural-unpromotability fact, and degraded-input safety.
Never touches decision_plan.json / config.json / signal_registry.yaml; never calls
record_strategy_decision / record_auto_strategy_anchor.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.portfolio_sim.oos_state import OOSState
from portfolio_automation.strategy.strategy_divergence import (
    CLASSIFICATIONS,
    classify_divergence,
    compute_strategy_divergence,
    write_strategy_divergence,
)


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data)
    else:
        path.write_text(json.dumps(data))


def _leaderboard_row(tactic_id, name, score, *, turnover=0.5, tax_drag=0.0,
                      drawdown=-0.2, flags=None):
    return {
        "tactic_id": tactic_id,
        "name": name,
        "strategy_score": score,
        "flags": flags or [],
        "worst_max_drawdown": drawdown,
        "score_decomposition": {
            "components": {
                "turnover": {"raw": turnover},
                "tax_drag": {"raw": tax_drag},
            }
        },
    }


def _seed_repo(root: Path, *, active_id="defensive_capital_preservation",
               leaderboard_rows=None, wf_results=None, queue_ids=None,
               decisions=None):
    outputs = root / "outputs"
    _write(outputs / "policy" / "active_strategy_selection.json", {
        "observe_only": True, "no_trade": True,
        "active_strategy_id": active_id,
        "name": "Defensive / Capital Preservation",
        "approved_by": "pesantez", "approved_at": "2026-07-07T19:01:31.446752+00:00",
        "status": "approved", "supersedes": "income_dividend",
    })
    if leaderboard_rows is not None:
        _write(outputs / "sandbox" / "strategy_leaderboard.json",
               {"status": "ok", "leaderboard": leaderboard_rows})
    _write(outputs / "sandbox" / "walk_forward_results.json",
           {"status": "ok", "results": wf_results or {}})
    if queue_ids is not None:
        _write(outputs / "latest" / "strategy_review_queue.json",
               {"queue": [{"strategy_id": sid} for sid in queue_ids]})
    lines = []
    for d in (decisions or []):
        lines.append(json.dumps(d))
    _write(outputs / "policy" / "strategy_decisions.jsonl", "\n".join(lines) + ("\n" if lines else ""))


# ---------------------------------------------------------------------------
# classify_divergence -- pure decision function
# ---------------------------------------------------------------------------

def _classify(**over):
    base = dict(rank_difference=5, active_in_queue=True,
                top_tactic_oos_state=OOSState.OOS_NOT_TESTED.value,
                top_tactic_in_queue=False, has_pending_promotion_proposal=False,
                explicit_policy_reason=None)
    base.update(over)
    return classify_divergence(**base)


def test_classify_stale_active_strategy_wins_precedence():
    label, reasons = _classify(active_in_queue=False)
    assert label == "STALE_ACTIVE_STRATEGY"
    assert reasons


def test_classify_no_divergence_is_expected_policy_divergence():
    label, _ = _classify(rank_difference=0)
    assert label == "EXPECTED_POLICY_DIVERGENCE"


def test_classify_explicit_policy_reason_is_expected():
    label, reasons = _classify(explicit_policy_reason="retirement-tranche mandate overrides rank")
    assert label == "EXPECTED_POLICY_DIVERGENCE"
    assert "retirement-tranche" in reasons[0]


def test_classify_top_oos_failed_is_expected():
    label, _ = _classify(top_tactic_oos_state=OOSState.OOS_FAILED.value)
    assert label == "EXPECTED_POLICY_DIVERGENCE"


@pytest.mark.parametrize("state", [
    OOSState.OOS_NOT_TESTED.value, OOSState.OOS_DATA_BLOCKED.value,
    OOSState.OOS_INSUFFICIENT.value, OOSState.OOS_MIXED.value,
])
def test_classify_untested_or_ambiguous_is_insufficient_evidence(state):
    label, reasons = _classify(top_tactic_oos_state=state)
    assert label == "INSUFFICIENT_EVIDENCE"
    assert state in reasons[0]


def test_classify_supported_but_not_in_queue_is_unexplained():
    label, reasons = _classify(top_tactic_oos_state=OOSState.OOS_SUPPORTED.value,
                                top_tactic_in_queue=False)
    assert label == "UNEXPLAINED_DIVERGENCE"
    assert "structural" in reasons[0] or "cannot" in reasons[0]


def test_classify_supported_in_queue_pending_is_pending_review():
    label, _ = _classify(top_tactic_oos_state=OOSState.OOS_SUPPORTED.value,
                          top_tactic_in_queue=True, has_pending_promotion_proposal=True)
    assert label == "PENDING_REVIEW"


def test_classify_supported_in_queue_no_pending_is_unexplained():
    label, _ = _classify(top_tactic_oos_state=OOSState.OOS_SUPPORTED.value,
                          top_tactic_in_queue=True, has_pending_promotion_proposal=False)
    assert label == "UNEXPLAINED_DIVERGENCE"


def test_classify_always_returns_one_of_the_five_labels():
    label, _ = _classify()
    assert label in CLASSIFICATIONS
    assert len(CLASSIFICATIONS) == 5


# ---------------------------------------------------------------------------
# compute_strategy_divergence -- end-to-end reader
# ---------------------------------------------------------------------------

def test_real_shape_regression_is_insufficient_evidence(tmp_path):
    """Reproduces today's real repo shape: active strategy ranked far below an
    untested #1 tactic that isn't in the review queue. Must classify
    INSUFFICIENT_EVIDENCE -- NOT a more flattering label."""
    _seed_repo(
        tmp_path,
        leaderboard_rows=[
            _leaderboard_row("research_vol_managed", "Volatility-Managed", 1.7474,
                              turnover=0.7, flags=["overfit_unknown"]),
            *[_leaderboard_row(f"filler_{i}", f"Filler {i}", 1.0 - i * 0.01) for i in range(20)],
            _leaderboard_row("profile_defensive_capital_preservation",
                              "Defensive / Capital Preservation", 0.4919, turnover=0.3),
        ],
        wf_results={},  # research_vol_managed never walk-forward tested
        queue_ids=["defensive_capital_preservation", "income_dividend", "boom_bucket"],
        decisions=[{"ts": "2026-07-07T19:01:31.446752+00:00",
                    "strategy_id": "defensive_capital_preservation",
                    "decision": "approve", "approver": "pesantez"}],
    )
    result = compute_strategy_divergence(root=tmp_path)
    assert result["status"] == "ok"
    assert result["classification"] == "INSUFFICIENT_EVIDENCE"
    assert result["top_tactic_oos"]["state"] == "OOS_NOT_TESTED"
    assert result["active_strategy"]["rank"] == 22
    assert result["top_ranked_tactic"]["rank"] == 1
    assert result["rank_difference"] == 21
    assert result["score_difference"] == pytest.approx(1.2555, abs=1e-4)
    assert result["structural_unpromotability"]["blocked"] is True
    assert "research_vol_managed" not in result["structural_unpromotability"]["review_queue_profiles"]
    assert result["last_human_decision"]["approver"] == "pesantez"
    assert result["last_human_decision"]["strategy_id"] == "defensive_capital_preservation"
    assert result["promotion_consideration"]["should_consider"] is False
    # Never touches decision_plan / config / registry.
    assert not (tmp_path / "outputs" / "latest" / "decision_plan.json").exists()
    assert not (tmp_path / "config.json").exists()


def test_top_tactic_equals_active_is_no_divergence(tmp_path):
    _seed_repo(
        tmp_path,
        leaderboard_rows=[
            _leaderboard_row("profile_defensive_capital_preservation",
                              "Defensive / Capital Preservation", 1.5),
            _leaderboard_row("other", "Other", 1.0),
        ],
        wf_results={},
        queue_ids=["defensive_capital_preservation"],
    )
    result = compute_strategy_divergence(root=tmp_path)
    assert result["rank_difference"] == 0
    assert result["classification"] == "EXPECTED_POLICY_DIVERGENCE"


def test_stale_active_strategy_not_in_queue(tmp_path):
    _seed_repo(
        tmp_path,
        active_id="defensive_capital_preservation",
        leaderboard_rows=[
            _leaderboard_row("research_vol_managed", "Volatility-Managed", 1.7),
            _leaderboard_row("profile_defensive_capital_preservation",
                              "Defensive / Capital Preservation", 0.5),
        ],
        wf_results={},
        queue_ids=["income_dividend", "boom_bucket"],  # active id NOT present
    )
    result = compute_strategy_divergence(root=tmp_path)
    assert result["classification"] == "STALE_ACTIVE_STRATEGY"


def test_supported_top_tactic_in_queue_pending_review(tmp_path):
    _seed_repo(
        tmp_path,
        leaderboard_rows=[
            _leaderboard_row("profile_boom_bucket", "Boom Bucket", 1.7),
            _leaderboard_row("profile_defensive_capital_preservation",
                              "Defensive / Capital Preservation", 0.5),
        ],
        wf_results={
            "profile_boom_bucket": {"status": "ok", "splits": 6, "oos_mean_excess": 0.1,
                                     "oos_hit_rate": 0.6, "one_fold_controls_result": False},
        },
        queue_ids=["boom_bucket", "defensive_capital_preservation"],
        decisions=[{"ts": "2026-06-24T13:41:33", "strategy_id": "long_term_compounding",
                    "decision": "approve", "approver": "pesantez"}],
    )
    result = compute_strategy_divergence(root=tmp_path)
    assert result["top_tactic_oos"]["state"] == "OOS_SUPPORTED"
    assert result["structural_unpromotability"]["blocked"] is False
    assert result["classification"] == "PENDING_REVIEW"
    assert result["promotion_consideration"]["should_consider"] is True


def test_degraded_when_leaderboard_absent(tmp_path):
    _seed_repo(tmp_path, leaderboard_rows=None)
    result = compute_strategy_divergence(root=tmp_path)
    assert result["status"] == "degraded"
    assert "leaderboard" in result["reason"]
    assert result["observe_only"] is True


def test_degraded_when_no_active_selection(tmp_path):
    outputs = tmp_path / "outputs"
    _write(outputs / "sandbox" / "strategy_leaderboard.json",
           {"status": "ok", "leaderboard": [_leaderboard_row("t1", "T1", 1.0)]})
    result = compute_strategy_divergence(root=tmp_path)
    assert result["status"] == "degraded"
    assert "active" in result["reason"]


def test_write_strategy_divergence_persists_artifact_only(tmp_path):
    _seed_repo(
        tmp_path,
        leaderboard_rows=[
            _leaderboard_row("research_vol_managed", "Volatility-Managed", 1.7),
            _leaderboard_row("profile_defensive_capital_preservation",
                              "Defensive / Capital Preservation", 0.5),
        ],
        wf_results={},
        queue_ids=["defensive_capital_preservation"],
    )
    result = write_strategy_divergence(root=tmp_path)
    out_path = tmp_path / "outputs" / "sandbox" / "strategy_divergence.json"
    assert out_path.exists()
    on_disk = json.loads(out_path.read_text())
    assert on_disk["classification"] == result["classification"]
    assert on_disk["observe_only"] is True
    assert on_disk["sandbox_only"] is True
    assert on_disk["no_trade"] is True
    # Never wrote anything outside outputs/sandbox.
    assert not (tmp_path / "outputs" / "policy" / "strategy_divergence.json").exists()
    assert not (tmp_path / "outputs" / "latest" / "decision_plan.json").exists()
