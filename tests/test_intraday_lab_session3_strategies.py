"""Session 3.1D preregistration invariants."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import date
import json

import pytest

from portfolio_automation.intraday_lab import calendar as CAL
from portfolio_automation.intraday_lab import features as FT
from portfolio_automation.intraday_lab import irregular_sessions as IR
from portfolio_automation.intraday_lab import population_audit as PA
from portfolio_automation.intraday_lab import storage as ST
from portfolio_automation.intraday_lab.models import IntradayBar
from portfolio_automation.intraday_lab import strategy_definitions as SD
from portfolio_automation.intraday_lab import signal_evaluator as SE


def _green_session3_0():
    return {
        "status": PA.SESSION_3_0_POLICY_READY,
        "session_3_1_gate": PA.SESSION_3_1_GO,
        "population_fingerprint": "population-v2-test-evidence",
        "strategy_validation_allowed": False,
        "blockers": [],
    }


def _red_session3_0():
    return {
        "status": PA.SESSION_3_0_LIMITED,
        "session_3_1_gate": PA.SESSION_3_1_NO_GO,
        "population_fingerprint": None,
        "strategy_validation_allowed": False,
        "blockers": ["population_evidence_available"],
    }


def _bar(symbol, start, *, close=100.0, high=101.0, low=99.0, open_=100.0):
    return IntradayBar(
        symbol=symbol, timeframe="5min", bar_start_at=start,
        open=open_, high=max(high, open_, close), low=min(low, open_, close),
        close=close, volume=1000, source="fixture", adjustment_state="split_back_adjusted",
    )


def _bars_for_session(d: date, symbol="SPY", n=8, slope=0.1):
    session = CAL.resolve_session(d)
    out = []
    for i, start in enumerate(session.expected_bar_starts[:n]):
        px = 100.0 + slope * i
        out.append(_bar(symbol, start, open_=px - 0.03, close=px,
                        high=px + 0.05, low=px - 0.05))
    return tuple(out)


def test_generation1_names_and_research_burden_are_frozen():
    ids = {s.strategy_id for s in SD.generation1_strategies()}
    assert ids == {
        SD.SHORT_HORIZON_MEAN_REVERSION_V1,
        SD.OPENING_RANGE_BREAKOUT_CONTINUATION_V1,
        SD.EARLY_TO_LATE_INTRADAY_MOMENTUM_V1,
    }
    assert {h.hypothesis_id for h in SD.generation1_hypotheses()} == ids
    assert SD.research_burden() == {
        "schema": "intraday_research_burden_v1",
        "strategy_families": 3,
        "registered_hypotheses": 3,
        "parameter_sets": 3,
        "directional_subhypotheses": 6,
        "optimization_trials": 0,
        "post_result_amendments": 0,
        "optimization_performed": False,
    }


def test_definitions_and_registrations_are_immutable_and_complete():
    strategy = SD.generation1_strategies()[0]
    registration = SD.generation1_hypotheses()[0]
    with pytest.raises(FrozenInstanceError):
        strategy.strategy_id = "MUTATED"
    with pytest.raises(FrozenInstanceError):
        registration.claim = "changed after results"

    for s in SD.generation1_strategies():
        assert s.formula
        assert s.parameters and all(p.unit and p.semantic_meaning and p.ex_ante_rationale for p in s.parameters)
        assert s.observation_window.bars > 0
        assert s.prediction_known_time
        assert s.evaluation_window.bars > 0
        assert s.required_primitives
        assert s.invalidation_conditions
        assert s.optimization_performed is False
        assert s.parameter_set_fingerprint
        assert s.fingerprint

    for h in SD.generation1_hypotheses():
        assert h.primary_outcome
        assert h.future_evaluation_window.bars > 0
        assert h.optimization_performed is False
        assert h.fingerprint


def test_parameter_change_mints_new_parameter_and_strategy_fingerprints():
    original = SD.generation1_strategy_by_id(SD.SHORT_HORIZON_MEAN_REVERSION_V1)
    params = list(original.parameters)
    threshold = next(i for i, p in enumerate(params) if p.name == "displacement_threshold")
    params[threshold] = replace(params[threshold], value=0.006)
    changed = replace(original, parameters=tuple(params))
    assert changed.parameter_set_fingerprint != original.parameter_set_fingerprint
    assert changed.fingerprint != original.fingerprint


def test_population_policy_change_mints_new_strategy_fingerprint(monkeypatch):
    before = SD.generation1_strategy_by_id(SD.OPENING_RANGE_BREAKOUT_CONTINUATION_V1).fingerprint
    monkeypatch.setattr(IR, "policy_fingerprint", lambda: "different-population-policy")
    after = SD.generation1_strategy_by_id(SD.OPENING_RANGE_BREAKOUT_CONTINUATION_V1).fingerprint
    assert after != before


def test_halt_policy_change_mints_new_strategy_fingerprint(monkeypatch):
    before = SD.generation1_strategy_by_id(SD.OPENING_RANGE_BREAKOUT_CONTINUATION_V1).fingerprint
    monkeypatch.setattr(IR, "HALT_BOUNDARY_POLICY_VERSION", "intraday_halt_boundary_policy_v999")
    after = SD.generation1_strategy_by_id(SD.OPENING_RANGE_BREAKOUT_CONTINUATION_V1).fingerprint
    assert after != before


def test_feature_semantics_change_mints_mean_reversion_fingerprint(monkeypatch):
    before = SD.generation1_strategy_by_id(SD.SHORT_HORIZON_MEAN_REVERSION_V1).fingerprint
    monkeypatch.setitem(FT.FEATURE_REGISTRY["return_nbar"], "version", "999")
    after = SD.generation1_strategy_by_id(SD.SHORT_HORIZON_MEAN_REVERSION_V1).fingerprint
    assert after != before


def test_custom_primitive_change_mints_strategy_fingerprint(monkeypatch):
    before = SD.generation1_strategy_by_id(SD.OPENING_RANGE_BREAKOUT_CONTINUATION_V1).fingerprint
    monkeypatch.setitem(SD.CUSTOM_PRIMITIVE_VERSIONS,
                        "opening_range_construction", "intraday_opening_range_construction_v2")
    after = SD.generation1_strategy_by_id(SD.OPENING_RANGE_BREAKOUT_CONTINUATION_V1).fingerprint
    assert after != before


def test_opening_range_is_feature_unavailable_when_halt_interrupts_window():
    d = date(2020, 3, 9)
    strategy = SD.generation1_strategy_by_id(SD.OPENING_RANGE_BREAKOUT_CONTINUATION_V1)
    compat = SE.halt_boundary_compatibility(strategy, d)
    assert compat["compatible"] is False
    assert "opening_range_construction" in compat["blocked_primitives"]

    bars = _bars_for_session(d, n=8)
    as_of = bars[-1].known_at
    view = SE.SessionView("SPY", d, IR.VALID_MARKET_WIDE_HALT_SESSION, bars)
    signal = SE.evaluate_signal(strategy.strategy_id, view, as_of)
    assert signal.state == SE.FEATURE_UNAVAILABLE
    assert IR.HALT_BOUNDARY_POLICY_VERSION in signal.reason


def test_normal_opening_range_is_not_blocked_by_halt_policy():
    d = date(2020, 3, 17)
    strategy = SD.generation1_strategy_by_id(SD.OPENING_RANGE_BREAKOUT_CONTINUATION_V1)
    compat = SE.halt_boundary_compatibility(strategy, d)
    assert compat["compatible"] is True


def test_mean_reversion_resets_at_a_gap():
    d = date(2020, 3, 9)
    session = CAL.resolve_session(d)
    starts = (session.expected_bar_starts[0],
              session.expected_bar_starts[3],
              session.expected_bar_starts[4],
              session.expected_bar_starts[5])
    bars = tuple(_bar("SPY", s, close=100 + i) for i, s in enumerate(starts))
    view = SE.SessionView("SPY", d, IR.VALID_MARKET_WIDE_HALT_SESSION, bars)
    signal = SE.evaluate_signal(SD.SHORT_HORIZON_MEAN_REVERSION_V1, view, bars[-1].known_at)
    assert signal.state == SE.NOT_ENOUGH_HISTORY


def test_early_to_late_uses_session_open_not_previous_close():
    s = SD.generation1_strategy_by_id(SD.EARLY_TO_LATE_INTRADAY_MOMENTUM_V1)
    assert "open(first_5m_bar)" in s.formula
    assert "previous" not in s.formula.lower()
    assert any("previous-close" in reason for reason in s.invalidation_conditions)
    assert s.evaluation_window.anchor == "CERTIFIED_SESSION_CLOSE"


def test_orb_zero_buffer_is_explicit_and_strict():
    s = SD.generation1_strategy_by_id(SD.OPENING_RANGE_BREAKOUT_CONTINUATION_V1)
    p = {x.name: x.value for x in s.parameters}
    assert p["break_threshold"] == 0.0
    assert "close >" in s.formula and "close <" in s.formula


def test_legacy_artifacts_are_preserved_and_explicitly_superseded(tmp_path, monkeypatch):
    monkeypatch.setattr(PA, "session3_0_status", lambda *, root=".": _green_session3_0())
    base = ST.intraday_root(str(tmp_path)) / "session3"
    base.mkdir(parents=True)
    legacy = base / "strategy_registry.json"
    original = json.dumps({
        "strategies": [{
            "strategy_id": "OPENING_RANGE_BEHAVIOR_V1",
            "strategy_fingerprint": "legacy-orb-fingerprint",
        }]
    }, sort_keys=True).encode()
    legacy.write_bytes(original)

    fp = SD.persist_preregistration_set(root=str(tmp_path))
    assert legacy.read_bytes() == original

    body = ST.read_snapshot(SD.PREREGISTRATIONS, fp, "preregistration.json",
                            root=str(tmp_path))
    assert body["supersedes_legacy_artifacts"][0]["status"] == SD.DRAFT_PRE_FOUNDATION
    assert body["supersedes_legacy_artifacts"][0]["authority"] == SD.NON_AUTHORITATIVE
    line = body["registration_lineage"][0]
    assert line["relation"] == "supersedes"
    assert line["legacy_strategy_id"] == "OPENING_RANGE_BEHAVIOR_V1"
    assert line["authoritative_hypothesis_id"] == SD.OPENING_RANGE_BREAKOUT_CONTINUATION_V1
    assert SD.verify_preregistration_set(fp, root=str(tmp_path))["verified"] is True


def test_session3_1_gate_ignores_rendered_report_false_no_go(tmp_path, monkeypatch):
    monkeypatch.setattr(PA, "session3_0_status", lambda *, root=".": _green_session3_0())
    fp = SD.persist_preregistration_set(root=str(tmp_path))
    SD.set_session3_1_preregistration_evidence(fp, root=str(tmp_path))

    rendered = ST.intraday_root(str(tmp_path)) / "session3" / "irregular_session_population.json"
    rendered.parent.mkdir(parents=True, exist_ok=True)
    rendered.write_text(json.dumps({
        "session3_0_status": {"status": PA.SESSION_3_0_LIMITED}
    }))
    st = SD.session3_1_status(root=str(tmp_path))
    assert st["status"] == SD.HYPOTHESIS_PREREGISTRATION_READY
    assert st["session_3_2_gate"] == SD.SESSION_3_2_GO


def test_session3_1_gate_fails_closed_when_durable_gate_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(PA, "session3_0_status", lambda *, root=".": _green_session3_0())
    fp = SD.persist_preregistration_set(root=str(tmp_path))
    SD.set_session3_1_preregistration_evidence(fp, root=str(tmp_path))

    rendered = ST.intraday_root(str(tmp_path)) / "session3" / "irregular_session_population.json"
    rendered.parent.mkdir(parents=True, exist_ok=True)
    rendered.write_text(json.dumps({
        "session3_0_status": {"status": PA.SESSION_3_0_POLICY_READY}
    }))

    monkeypatch.setattr(PA, "session3_0_status", lambda *, root=".": _red_session3_0())
    st = SD.session3_1_status(root=str(tmp_path))
    assert st["status"] == SD.HYPOTHESIS_PREREGISTRATION_LIMITED
    assert st["session_3_2_gate"] == SD.SESSION_3_2_NO_GO
    assert "durable_session3_0_gate_ready" in st["blockers"]


def test_preregistration_content_is_immutable_and_pointer_is_not_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(PA, "session3_0_status", lambda *, root=".": _green_session3_0())
    fp = SD.persist_preregistration_set(root=str(tmp_path))
    SD.set_session3_1_preregistration_evidence(fp, root=str(tmp_path))

    path = ST.intraday_root(str(tmp_path)) / SD.PREREGISTRATIONS / fp / "preregistration.json"
    body = json.loads(path.read_text())
    body["research_burden"]["optimization_trials"] = 1
    path.write_text(json.dumps(body))
    st = SD.session3_1_status(root=str(tmp_path))
    assert st["session_3_2_gate"] == SD.SESSION_3_2_NO_GO
    assert "preregistration_evidence_verifies" in st["blockers"]


def _write_legacy(tmp_path, payload: dict, name="strategy_registry.json") -> "object":
    """A pre-foundation prototype artifact, exactly as the 05:18 prototype left it."""
    base = ST.intraday_root(str(tmp_path)) / "session3"
    base.mkdir(parents=True, exist_ok=True)
    path = base / name
    path.write_bytes(json.dumps(payload, sort_keys=True).encode())
    return path


def _legacy_payload() -> dict:
    return {"strategies": [{"strategy_id": "OPENING_RANGE_BEHAVIOR_V1",
                            "strategy_fingerprint": "legacy-orb-fingerprint"}]}


# ── A. the certification entrypoint exists and composes the flow ─────────────
def test_freeze_helper_persists_verifies_and_selects_the_pointer(tmp_path, monkeypatch):
    monkeypatch.setattr(PA, "session3_0_status", lambda *, root=".": _green_session3_0())
    _write_legacy(tmp_path, _legacy_payload())

    result = SD.freeze_session3_1_preregistration(root=str(tmp_path))

    fp = result["preregistration_fingerprint"]
    assert fp
    assert result["verified"] is True
    # The pointer on disk names exactly the object that was persisted+verified.
    pointer = json.loads(
        (ST.intraday_root(str(tmp_path)) / SD.PREREGISTRATION_POINTER).read_text())
    assert pointer["preregistration_fingerprint"] == fp
    # The gate is not invented by the wrapper; it comes from session3_1_status.
    assert result["status"] == SD.HYPOTHESIS_PREREGISTRATION_READY
    assert result["session_3_2_gate"] == SD.SESSION_3_2_GO
    assert result["session_3_1_status"] == SD.session3_1_status(root=str(tmp_path))
    assert result["strategy_validation_allowed"] is False


# ── B. the entrypoint fails closed instead of selecting a bad pointer ────────
def test_freeze_helper_refuses_to_select_a_pointer_when_verification_fails(
        tmp_path, monkeypatch):
    monkeypatch.setattr(PA, "session3_0_status", lambda *, root=".": _green_session3_0())
    # A REAL verification failure: the live definitions no longer match what was
    # persisted, which is exactly what verify_preregistration_set exists to catch.
    mutated = [dict(d, strategy_id="TAMPERED_V1") for d in SD._current_strategy_payloads()]
    monkeypatch.setattr(SD, "_current_strategy_payloads", lambda: mutated)

    with pytest.raises(ValueError):
        SD.freeze_session3_1_preregistration(root=str(tmp_path))

    assert not (ST.intraday_root(str(tmp_path)) / SD.PREREGISTRATION_POINTER).exists()
    st = SD.session3_1_status(root=str(tmp_path))
    assert st["session_3_2_gate"] == SD.SESSION_3_2_NO_GO


# ── C/D. the superseded prototype must still be there, and unchanged ─────────
def test_deleting_a_superseded_legacy_artifact_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(PA, "session3_0_status", lambda *, root=".": _green_session3_0())
    legacy = _write_legacy(tmp_path, _legacy_payload())
    fp = SD.freeze_session3_1_preregistration(root=str(tmp_path))["preregistration_fingerprint"]
    assert SD.verify_preregistration_set(fp, root=str(tmp_path))["verified"] is True

    legacy.unlink()

    v = SD.verify_preregistration_set(fp, root=str(tmp_path))
    assert v["verified"] is False
    assert "legacy" in (v["reason"] or "").lower()
    st = SD.session3_1_status(root=str(tmp_path))
    assert st["session_3_2_gate"] == SD.SESSION_3_2_NO_GO
    assert "preregistration_evidence_verifies" in st["blockers"]


def test_mutating_a_superseded_legacy_artifact_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(PA, "session3_0_status", lambda *, root=".": _green_session3_0())
    legacy = _write_legacy(tmp_path, _legacy_payload())
    fp = SD.freeze_session3_1_preregistration(root=str(tmp_path))["preregistration_fingerprint"]
    assert SD.verify_preregistration_set(fp, root=str(tmp_path))["verified"] is True

    # Rewriting history so it matches the new world is exactly what must not pass.
    legacy.write_bytes(json.dumps({"strategies": [{
        "strategy_id": "OPENING_RANGE_BEHAVIOR_V1",
        "strategy_fingerprint": "rewritten-to-look-authoritative",
    }]}, sort_keys=True).encode())

    v = SD.verify_preregistration_set(fp, root=str(tmp_path))
    assert v["verified"] is False
    assert "legacy" in (v["reason"] or "").lower()
    assert SD.session3_1_status(root=str(tmp_path))["session_3_2_gate"] == SD.SESSION_3_2_NO_GO


# ── E. the frozen contract hashes MEANING, not byte formatting ───────────────
def test_reformatted_but_semantically_identical_legacy_json_still_verifies(
        tmp_path, monkeypatch):
    monkeypatch.setattr(PA, "session3_0_status", lambda *, root=".": _green_session3_0())
    legacy = _write_legacy(tmp_path, _legacy_payload())
    fp = SD.freeze_session3_1_preregistration(root=str(tmp_path))["preregistration_fingerprint"]

    # Same JSON meaning, different bytes: reordered keys, indentation, trailing newline.
    legacy.write_text(json.dumps(_legacy_payload(), indent=4, sort_keys=False) + "\n")
    assert legacy.read_bytes() != json.dumps(_legacy_payload(), sort_keys=True).encode()

    v = SD.verify_preregistration_set(fp, root=str(tmp_path))
    assert v["verified"] is True, v.get("reason")
    assert SD.session3_1_status(root=str(tmp_path))["session_3_2_gate"] == SD.SESSION_3_2_GO


def test_non_json_legacy_artifact_is_fingerprinted_by_raw_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(PA, "session3_0_status", lambda *, root=".": _green_session3_0())
    legacy = _write_legacy(tmp_path, _legacy_payload())
    legacy.write_bytes(b"{not valid json at all")
    fp = SD.freeze_session3_1_preregistration(root=str(tmp_path))["preregistration_fingerprint"]
    assert SD.verify_preregistration_set(fp, root=str(tmp_path))["verified"] is True

    legacy.write_bytes(b"{not valid json at all!")
    assert SD.verify_preregistration_set(fp, root=str(tmp_path))["verified"] is False


# ── F. immutable evidence may not read outside the Intraday historical tree ──
@pytest.mark.parametrize("escaping", ["../../escape.json", "/etc/passwd"])
def test_legacy_path_escaping_the_intraday_root_fails_closed(
        tmp_path, monkeypatch, escaping):
    monkeypatch.setattr(PA, "session3_0_status", lambda *, root=".": _green_session3_0())
    # Mint a CORRECTLY-HASHED preregistration whose legacy record points outside
    # the tree, so verification must reject on containment, not on a hash mismatch.
    monkeypatch.setattr(SD, "legacy_prototype_lineage", lambda root=".": [{
        "path": escaping,
        "content_fingerprint": "whatever",
        "status": SD.DRAFT_PRE_FOUNDATION,
        "authority": SD.NON_AUTHORITATIVE,
        "relation": "superseded_by_this_authoritative_preregistration_set",
        "declared_strategy_refs": [],
        "preserved": True,
    }])
    fp = SD.persist_preregistration_set(root=str(tmp_path))

    v = SD.verify_preregistration_set(fp, root=str(tmp_path))
    assert v["verified"] is False
    assert v["reason"] and "path" in v["reason"].lower()
    assert SD.session3_1_status(root=str(tmp_path))["session_3_2_gate"] == SD.SESSION_3_2_NO_GO


def test_no_performance_execution_or_production_authority_fields():
    forbidden = {
        "pnl", "sharpe", "fill_price", "execution_cost", "position_size",
        "winner", "decision_plan", "broker",
    }
    for s in SD.generation1_strategies():
        blob = json.dumps(s.to_dict()).lower()
        for word in forbidden:
            assert word not in blob
    for h in SD.generation1_hypotheses():
        assert h.optimization_performed is False
