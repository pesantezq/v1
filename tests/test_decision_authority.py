"""Regression suite for the capital-authority consistency gate.

Reproduces the live 2026-08-08 defect class: `outputs/latest/decision_plan.json`
carried `capital_action: "Scale existing position — add about $1,588."` for VFH
(recommended_amount=1587.696) while `outputs/latest/daily_capital_plan.json`
carried `funded_actions: []` and `bottom_line: "No capital is funded for
deployment today ($0 available after pacing)"`.

Both artifacts were internally coherent. Nothing reconciled them, so any
investor-facing consumer that rendered `capital_action` verbatim issued a
funded-sounding dollar instruction the capital layer had already denied.
"""
import json

import pytest

from portfolio_automation import decision_authority as da


def _plan(*decisions) -> dict:
    return {"run_id": "run-1", "generated_at": "2026-08-08T09:00:00+00:00",
            "decisions": list(decisions)}


def _decision(symbol, decision, amount, capital_action=""):
    return {"symbol": symbol, "decision": decision,
            "recommended_amount": amount, "capital_action": capital_action}


def _capital(funded=(), bottom_line="", available=True) -> dict:
    return {"available": available, "generated_at": "2026-08-08T09:13:00+00:00",
            "bottom_line": bottom_line, "funded_actions": list(funded)}


def _funded(symbol, amount, decision="BUY"):
    return {"symbol": symbol, "decision": decision, "funded_capital": amount,
            "funding_source": "contribution"}


# ---------------------------------------------------------------------------
# The live defect
# ---------------------------------------------------------------------------
def test_vfh_dollar_instruction_against_zero_funded_is_blocked():
    """The exact 2026-08-08 conflict must not be reported as consistent."""
    result = da.reconcile_capital_authority(
        _plan(_decision("VFH", "SCALE", 1587.696,
                        "Scale existing position — add about $1,588.")),
        _capital(funded=[], bottom_line="No capital is funded for deployment today"),
    )
    assert result["status"] == "BLOCKED_BY_CONSISTENCY"
    assert [c["symbol"] for c in result["conflicts"]] == ["VFH"]
    conflict = result["conflicts"][0]
    assert conflict["decision_plan_amount"] == pytest.approx(1587.696)
    assert conflict["capital_plan_funded"] == 0.0
    assert conflict["kind"] == "unfunded_capital_instruction"
    # the investor-facing sentence is carried so the operator sees what leaked
    assert "add about $1,588" in conflict["instruction"]


def test_blocked_status_is_not_overridden_by_other_consistent_rows():
    """One unfunded instruction is enough to fail closed."""
    result = da.reconcile_capital_authority(
        _plan(_decision("PLTR", "BUY", 106.0, "Open new position — deploy about $106."),
              _decision("VFH", "SCALE", 1587.696, "add about $1,588.")),
        _capital(funded=[_funded("PLTR", 106.0)]),
    )
    assert result["status"] == "BLOCKED_BY_CONSISTENCY"
    assert [c["symbol"] for c in result["conflicts"]] == ["VFH"]


# ---------------------------------------------------------------------------
# What must NOT trip the gate
# ---------------------------------------------------------------------------
def test_wait_rows_carrying_a_sizing_amount_are_not_instructions():
    """Live data has WAIT rows with recommended_amount=105.85 whose capital_action
    is 'Stand by — do not deploy capital until conditions improve.' A sizing hint
    on a stand-down decision is not a funded instruction and must not fail the
    gate — otherwise the gate cries wolf on every run."""
    result = da.reconcile_capital_authority(
        _plan(_decision("AAPL", "WAIT", 105.85,
                        "Stand by — do not deploy capital until conditions improve."),
              _decision("MSFT", "HOLD", 105.85, "Hold current position."),
              _decision("NOC", "AVOID", 105.85, "Pass — no capital action warranted.")),
        _capital(funded=[]),
    )
    assert result["status"] == "CONSISTENT"
    assert result["conflicts"] == []


def test_funded_instruction_matching_the_capital_plan_is_consistent():
    result = da.reconcile_capital_authority(
        _plan(_decision("PLTR", "BUY", 106.0, "deploy about $106.")),
        _capital(funded=[_funded("PLTR", 106.0)]),
    )
    assert result["status"] == "CONSISTENT"
    assert result["funded_symbols"] == ["PLTR"]


def test_zero_and_null_amounts_are_not_instructions():
    result = da.reconcile_capital_authority(
        _plan(_decision("QQQ", "SCALE", 0, "Scale existing position."),
              _decision("GLD", "BUY", None, "Open new position.")),
        _capital(funded=[]),
    )
    assert result["status"] == "CONSISTENT"


def test_sell_is_not_a_deployment_instruction():
    """SELL releases capital rather than consuming deployable capital; the
    deployment gate must not claim it needs a funding source."""
    result = da.reconcile_capital_authority(
        _plan(_decision("CHAT", "SELL", 400.0, "Reduce CHAT exposure — trim about $400.")),
        _capital(funded=[]),
    )
    assert result["status"] == "CONSISTENT"


# ---------------------------------------------------------------------------
# Amount disagreement between two authorities that BOTH fund the symbol
# ---------------------------------------------------------------------------
def test_material_amount_mismatch_is_a_conflict():
    result = da.reconcile_capital_authority(
        _plan(_decision("PLTR", "BUY", 1000.0, "deploy about $1,000.")),
        _capital(funded=[_funded("PLTR", 106.0)]),
    )
    assert result["status"] == "BLOCKED_BY_CONSISTENCY"
    assert result["conflicts"][0]["kind"] == "amount_disagreement"


def test_rounding_scale_difference_is_tolerated():
    """capital_action renders whole dollars; a sub-dollar delta is presentation."""
    result = da.reconcile_capital_authority(
        _plan(_decision("PLTR", "BUY", 105.85, "deploy about $106.")),
        _capital(funded=[_funded("PLTR", 106.0)]),
    )
    assert result["status"] == "CONSISTENT"


# ---------------------------------------------------------------------------
# Fail closed — absence of an authority is never agreement
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("dp,cp", [
    (None, _capital()),
    (_plan(), None),
    ({}, _capital()),
    (_plan(_decision("VFH", "SCALE", 1587.7)), {"available": False}),
])
def test_missing_or_unavailable_authority_is_insufficient_not_consistent(dp, cp):
    result = da.reconcile_capital_authority(dp, cp)
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["conflicts"] == []


def test_insufficient_data_states_which_authority_was_missing():
    result = da.reconcile_capital_authority(_plan(_decision("VFH", "SCALE", 10.0)), None)
    assert "capital_plan" in result["insufficient_reason"]


# ---------------------------------------------------------------------------
# Governance invariants
# ---------------------------------------------------------------------------
def test_gate_is_observe_only_and_never_emits_a_trade():
    result = da.reconcile_capital_authority(_plan(), _capital())
    assert result["observe_only"] is True
    assert result["no_trade"] is True
    assert "decision_plan" not in result.get("writes", [])


def test_provenance_carries_both_authority_identities():
    result = da.reconcile_capital_authority(
        _plan(_decision("PLTR", "BUY", 106.0)), _capital(funded=[_funded("PLTR", 106.0)]))
    prov = result["provenance"]
    assert prov["decision_plan_run_id"] == "run-1"
    assert prov["decision_plan_generated_at"] == "2026-08-08T09:00:00+00:00"
    assert prov["capital_plan_generated_at"] == "2026-08-08T09:13:00+00:00"


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------
def test_write_emits_json_under_latest(tmp_path):
    result = da.reconcile_capital_authority(
        _plan(_decision("VFH", "SCALE", 1587.696, "add about $1,588.")), _capital(funded=[]))
    path = da.write_decision_authority(result, str(tmp_path))
    written = json.loads((tmp_path / "outputs" / "latest"
                          / "decision_authority.json").read_text())
    assert str(path).endswith("decision_authority.json")
    assert written["status"] == "BLOCKED_BY_CONSISTENCY"
    assert written["observe_only"] is True


def test_run_from_root_reads_the_real_artifact_names(tmp_path):
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (latest / "decision_plan.json").write_text(json.dumps(
        _plan(_decision("VFH", "SCALE", 1587.696, "add about $1,588."))))
    (latest / "daily_capital_plan.json").write_text(json.dumps(_capital(funded=[])))
    result = da.run_decision_authority(str(tmp_path))
    assert result["status"] == "BLOCKED_BY_CONSISTENCY"


def test_run_from_root_fails_closed_when_artifacts_absent(tmp_path):
    assert da.run_decision_authority(str(tmp_path))["status"] == "INSUFFICIENT_DATA"
