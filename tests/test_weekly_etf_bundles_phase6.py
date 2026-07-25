"""
Phase 6 tests — simulation-only bounded decision-engine overlay + production
isolation. Hermetic.
"""
from __future__ import annotations

import json

from portfolio_automation.weekly_etf_bundles import engine_overlay as EO


def _payload():
    return {
        "status": "ok", "market_data_date": "2026-07-24",
        "bundles": [{
            "bundle_id": "ai", "name": "AI", "benchmark": "QQQ",
            "bundle_score": 90.0, "pct_above_sma50": 1.0,
            "members": [{"symbol": "SMH"}, {"symbol": "IGV"}],
        }, {
            "bundle_id": "weak", "name": "Weak", "benchmark": "SPY",
            "bundle_score": 20.0, "pct_above_sma50": 0.2,
            "members": [{"symbol": "XYZ"}],
        }],
    }


def test_signal_bounded_and_simulation_only():
    sig = EO.bundle_context_signal("SMH", _payload())
    assert sig["simulation_only"] is True
    assert sig["source"] == "weekly_etf_bundle_context"
    assert abs(sig["context_modifier"]) <= EO.MAX_CONTEXT_MODIFIER
    assert sig["related_etf_bundles"] == ["ai"]
    assert sig["context_modifier"] > 0        # strong bundle → positive tilt


def test_weak_bundle_negative_modifier_bounded():
    sig = EO.bundle_context_signal("XYZ", _payload())
    assert sig["context_modifier"] < 0
    assert abs(sig["context_modifier"]) <= EO.MAX_CONTEXT_MODIFIER


def test_symbol_not_in_bundle_zero_modifier():
    sig = EO.bundle_context_signal("NOPE", _payload())
    assert sig["context_modifier"] == 0.0
    assert sig["related_etf_bundles"] == []


def test_clamp_enforced_even_if_requested_larger():
    assert EO.clamp_modifier(0.5) == EO.MAX_CONTEXT_MODIFIER
    assert EO.clamp_modifier(-0.5) == -EO.MAX_CONTEXT_MODIFIER
    # apply respects the cap regardless of requested modifier
    adj = EO.apply_context_modifier(100.0, 0.5)
    assert adj <= 100.0 * (1 + EO.MAX_CONTEXT_MODIFIER) + 1e-9


def test_apply_returns_number_not_action():
    out = EO.apply_context_modifier(80.0, 0.03)
    assert isinstance(out, float)
    # no action semantics leak out
    assert out == 80.0 * 1.03


def test_overlay_comparison_isolated_and_ab():
    sim_rows = [
        {"symbol": "SMH", "baseline_score": 0.80, "forward_return": 0.06},
        {"symbol": "IGV", "baseline_score": 0.79, "forward_return": 0.05},
        {"symbol": "XYZ", "baseline_score": 0.81, "forward_return": -0.04},
    ]
    comp = EO.run_overlay_comparison(sim_rows, _payload(), top_k=2)
    assert comp["simulation_only"] is True
    assert comp["feeds_decision_engine"] is False
    assert comp["baseline"]["avg_forward_return"] is not None
    assert comp["overlay"]["avg_forward_return"] is not None
    # overlay boosts AI names and damps the weak-bundle name → better top-2 selection
    assert "XYZ" not in comp["overlay"]["selected"]


def test_write_overlay_goes_to_simulation_namespace(tmp_path):
    comp = EO.run_overlay_comparison(
        [{"symbol": "SMH", "baseline_score": 0.8, "forward_return": 0.05}], _payload())
    path = EO.write_overlay_comparison(comp, root=tmp_path)
    assert "/simulation/" in path.replace("\\", "/")
    doc = json.loads((tmp_path / "outputs" / "simulation"
                      / "weekly_etf_bundle_engine_overlay.json").read_text())
    assert doc["feeds_decision_engine"] is False
    # never writes into latest/ (production decision namespace)
    assert not (tmp_path / "outputs" / "latest").exists()


def test_module_does_not_import_decision_engine():
    import ast
    import inspect
    import portfolio_automation.weekly_etf_bundles.engine_overlay as m

    tree = ast.parse(inspect.getsource(m))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {"decision_engine", "scoring",
                 "portfolio_automation.decision_engine"}
    assert not (imported & forbidden), f"unexpected imports: {imported & forbidden}"
