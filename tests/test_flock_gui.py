"""Flock Intelligence — GUI: Crowd page section, Portfolio per-pick context, fallbacks."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from gui_v2.data.dash_crowd_radar import collect_crowd_radar_view
from gui_v2.data.dash_flock_context import flock_context_for

_FORBIDDEN = ("buy now", "sell now", "place order", "rebalance now")


def _write_flock(root: Path, groups, by_symbol=None):
    sim = root / "outputs" / "simulation"
    sim.mkdir(parents=True, exist_ok=True)
    (sim / "flock_intelligence.json").write_text(json.dumps({
        "data_quality_status": "ok", "group_count": len(groups),
        "ticker_count": sum(len(g.get("tickers", [])) for g in groups),
        "groups": groups, "generated_at": "2026-06-16T00:00:00Z",
        "disclaimer": "Flock Intelligence is simulation-only research context.",
    }))
    (sim / "flock_advisory_context.json").write_text(json.dumps({
        "generated_at": "2026-06-16T00:00:00Z", "by_symbol": by_symbol or {}}))


def _group(name, state, **kw):
    base = {"group": name, "group_kind": "theme", "flock_state": state,
            "flock_score": 0.7, "dispersion_score": 0.3, "crowd_velocity": 1.5,
            "crowd_breadth": 0.6, "mention_concentration": 0.4,
            "price_correlation_to_group": 0.6, "confidence": 0.8,
            "explanation": f"{name} {state}", "tickers": ["NVDA", "AMD"]}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Crowd page section
# ---------------------------------------------------------------------------

def test_crowd_view_has_flock_fallback_when_absent(tmp_path):
    v = collect_crowd_radar_view(tmp_path)
    assert "flock" in v
    assert v["flock"]["has_data"] is False
    assert v["flock"]["sections"] == []


def test_crowd_view_renders_flock_sections(tmp_path):
    _write_flock(tmp_path, [_group("AI Infra", "flock_confirmed"),
                            _group("Energy", "flock_dispersing")])
    v = collect_crowd_radar_view(tmp_path)
    assert v["flock"]["has_data"] is True
    keys = {s["key"] for s in v["flock"]["sections"]}
    assert "flock_confirmed" in keys and "flock_dispersing" in keys
    row = v["flock"]["sections"][0]["rows"][0]
    # Spec columns present.
    for col in ("group", "state", "flock_score", "dispersion_score", "breadth",
                "velocity", "concentration", "confidence", "explanation"):
        assert col in row


def test_crowd_route_renders_flock_section_and_no_trade_verbs(tmp_path, monkeypatch):
    # Point the app loader at our fixture root.
    import gui_v2.app as app_mod
    _write_flock(app_mod.REPO_ROOT if False else tmp_path,
                 [_group("AI Infra", "flock_confirmed")])
    monkeypatch.setattr(app_mod, "REPO_ROOT", tmp_path, raising=False)

    client = TestClient(app_mod.app)
    r = client.get("/dashboard/crowd-radar")
    assert r.status_code == 200
    text = r.text.lower()
    assert "flock intelligence" in text
    assert "simulation-only research context" in text
    for verb in _FORBIDDEN:
        assert verb not in text


# ---------------------------------------------------------------------------
# Portfolio per-pick flock context
# ---------------------------------------------------------------------------

def test_flock_context_for_present_and_absent(tmp_path):
    _write_flock(tmp_path, [_group("AI Infra", "flock_confirmed")], by_symbol={
        "NVDA": {"flock_state": "flock_confirmed", "group": "AI Infra", "flock_score": 0.8,
                 "dispersion_score": 0.2, "confidence": 0.85, "label": "AI Infra: flock confirmed",
                 "meaning": "Broad structure supports monitoring."}})
    ctx = flock_context_for(tmp_path, ["NVDA", "TSLA"])
    assert ctx["status"]["available"] is True
    assert ctx["by_symbol"]["NVDA"]["present"] is True
    assert ctx["by_symbol"]["NVDA"]["severity"] == "green"
    # A symbol with no flock context degrades honestly.
    assert ctx["by_symbol"]["TSLA"]["present"] is False
    assert ctx["by_symbol"]["TSLA"]["severity"] == "gray"


def test_flock_context_missing_artifact_is_honest(tmp_path):
    ctx = flock_context_for(tmp_path, ["NVDA"])
    assert ctx["status"]["available"] is False
    assert ctx["status"]["banner"]
    assert ctx["by_symbol"]["NVDA"]["present"] is False


def test_portfolio_loader_attaches_flock_context(tmp_path):
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)
    (latest / "decision_plan.json").write_text(json.dumps({"decisions": [
        {"ticker": "NVDA", "action": "BUY"}]}))
    _write_flock(tmp_path, [_group("AI Infra", "flock_confirmed")], by_symbol={
        "NVDA": {"flock_state": "flock_confirmed", "group": "AI Infra", "flock_score": 0.8,
                 "dispersion_score": 0.2, "confidence": 0.85, "label": "AI Infra: flock confirmed",
                 "meaning": "Broad structure supports monitoring."}})
    from gui_v2.data.dash_portfolio import collect_portfolio_view
    v = collect_portfolio_view(tmp_path)
    nvda = next((d for d in v["decisions"] if d.get("ticker") == "NVDA"), None)
    assert nvda is not None
    assert nvda["flock_context"]["present"] is True
    assert nvda["flock_context"]["state"] == "flock_confirmed"
    assert v["flock_context_status"]["available"] is True
