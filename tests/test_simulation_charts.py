"""Tests for the Simulation Charts producer + dashboard loader.

Covers: pure normalization, honest degraded states, the always-empty
allocation_drift chart, observe-only invariants, no decision_plan mutation,
the forbidden-phrase guard, and the loader's missing/malformed/stable-default
behavior.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from portfolio_automation.simulation_charts import (
    build_simulation_charts,
    run_simulation_charts,
)
from gui_v2.data.dash_simulation_charts import collect_simulation_charts_view

# Mirrors the dashboard safety guards (test_gui_dashboard_*.py) + the spec's
# forbidden-language list. Bare trade verbs are checked separately by regex.
_FORBIDDEN = (
    "execute trade", "buy now", "sell now", "place order", "auto-trade",
    "auto trade", "auto-approve", "rebalance now", "promotion approved",
    "official recommendation", "trade now",
)
_TRADE_VERBS = re.compile(r"\b(buy|sell|hold|execute)\b", re.IGNORECASE)


def _comparison():
    return {"comparison": [
        {"strategy_id": "a", "name": "Alpha", "after_tax_return_estimate": 0.12,
         "expected_volatility": 0.30, "max_drawdown_estimate": 0.18, "final_strategy_rank": 0.81},
        {"strategy_id": "b", "name": "Bravo", "after_tax_return_estimate": 0.06,
         "expected_volatility": 0.12, "max_drawdown_estimate": 0.09, "final_strategy_rank": 0.74},
        {"strategy_id": "c", "name": "Charlie", "after_tax_return_estimate": 0.20,
         "expected_volatility": 0.55, "max_drawdown_estimate": 0.42, "final_strategy_rank": 0.66},
    ]}


def _backtest():
    return {
        "status": "ok",
        "leaderboard": {
            "trailing_3y": [
                {"tactic_id": "a", "name": "Alpha", "cagr": 0.15, "max_drawdown": -0.18, "excess_vs_spy": 0.04},
                {"tactic_id": "b", "name": "Bravo", "cagr": 0.08, "max_drawdown": -0.09, "excess_vs_spy": -0.01},
            ],
            "trailing_1y": [
                {"tactic_id": "a", "name": "Alpha", "cagr": 0.22, "max_drawdown": -0.12, "excess_vs_spy": 0.06},
                {"tactic_id": "b", "name": "Bravo", "cagr": 0.05, "max_drawdown": -0.07, "excess_vs_spy": 0.00},
            ],
            "ytd": [
                {"tactic_id": "a", "name": "Alpha", "cagr": 0.10, "max_drawdown": -0.05, "excess_vs_spy": 0.02},
                {"tactic_id": "b", "name": "Bravo", "cagr": 0.03, "max_drawdown": -0.04, "excess_vs_spy": -0.02},
            ],
        },
        "contribution_sensitivity": {"tactic_id": "a", "by_window": {
            "trailing_3y": {
                "500": {"final_balance_dca": 21000.0, "total_contributed": 18000.0, "net_gain_dca": 3000.0},
                "1000": {"final_balance_dca": 42000.0, "total_contributed": 36000.0, "net_gain_dca": 6000.0},
            }
        }},
    }


def _projection():
    return {"status": "ok", "horizons": ["1y"], "anchor_fan": {"1y": [
        {"month": 0, "p5": 1.0, "p50": 1.0, "p95": 1.0},
        {"month": 6, "p5": 0.95, "p50": 1.07, "p95": 1.22},
        {"month": 12, "p5": 0.88, "p50": 1.14, "p95": 1.58},
    ]}}


# ── producer: full-data path ────────────────────────────────────────────────

def test_build_full_contract_shape():
    out = build_simulation_charts(comparison=_comparison(), backtest=_backtest(), projection=_projection())
    assert out["observe_only"] is True
    assert out["sandbox_only"] is True
    assert out["safety"]["can_execute_trades"] is False
    assert out["safety"]["official_advisory_source"] == "decision_plan.json"
    assert set(out["charts"]) == {
        "growth_over_time", "drawdown", "risk_return",
        "rolling_outperformance", "contribution_sensitivity", "allocation_drift",
    }


def test_five_charts_have_real_data():
    out = build_simulation_charts(comparison=_comparison(), backtest=_backtest(), projection=_projection())
    for key in ("growth_over_time", "drawdown", "risk_return", "rolling_outperformance", "contribution_sensitivity"):
        assert out["charts"][key]["available"] is True, key
        assert out["charts"][key]["takeaway"], f"{key} should have a plain-English takeaway"


def test_risk_return_uses_real_values():
    out = build_simulation_charts(comparison=_comparison(), backtest={}, projection={})
    pts = out["charts"]["risk_return"]["points"]
    alpha = next(p for p in pts if p["label"] == "Alpha")
    assert alpha["return_pct"] == 12.0 and alpha["risk_pct"] == 30.0


def test_drawdown_is_positive_depth_sorted():
    out = build_simulation_charts(comparison=_comparison(), backtest={}, projection={})
    bars = out["charts"]["drawdown"]["bars"]
    assert all(b["value_pct"] >= 0 for b in bars)
    assert bars == sorted(bars, key=lambda b: b["value_pct"])  # gentlest first


def test_allocation_drift_always_empty_with_reason():
    out = build_simulation_charts(comparison=_comparison(), backtest=_backtest(), projection=_projection())
    ad = out["charts"]["allocation_drift"]
    assert ad["available"] is False
    assert ad["missing_reason"]  # honest reason present


def test_summary_picks_best_and_worst():
    out = build_simulation_charts(comparison=_comparison(), backtest=_backtest(), projection=_projection())
    s = out["summary"]
    assert s["best_growth"]["strategy"] == "Charlie"        # highest return
    assert s["best_risk_control"]["strategy"] == "Bravo"    # smallest drawdown
    assert s["biggest_pain_point"]["strategy"] == "Charlie" # deepest drawdown


# ── producer: degraded paths ─────────────────────────────────────────────────

def test_build_empty_inputs_degrade_honestly():
    out = build_simulation_charts(comparison={}, backtest={}, projection={})
    for key, chart in out["charts"].items():
        assert chart["available"] is False, key
        assert chart["missing_reason"], key
    assert out["summary"]["best_growth"]["strategy"] is None


def test_build_never_raises_on_garbage():
    out = build_simulation_charts(comparison={"comparison": "nope"}, backtest=[], projection="x")  # type: ignore[arg-type]
    assert out["observe_only"] is True
    assert out["charts"]["risk_return"]["available"] is False


# ── producer: no-instruction language guard ──────────────────────────────────

def test_no_forbidden_language_in_payload():
    out = build_simulation_charts(comparison=_comparison(), backtest=_backtest(), projection=_projection())
    blob = json.dumps(out).lower()
    for phrase in _FORBIDDEN:
        assert phrase not in blob, f"forbidden phrase '{phrase}' in simulation_charts payload"
    # plain-English prose (takeaways + summaries) must not read as a trade instruction
    prose = " ".join(
        [c.get("plain_english", "") for c in out["summary"].values()]
        + [c.get("takeaway", "") for c in out["charts"].values()]
    )
    assert not _TRADE_VERBS.search(prose), f"bare trade verb in prose: {_TRADE_VERBS.search(prose)}"


# ── producer: run_* IO + no decision_plan mutation ──────────────────────────

def _seed_sandbox(root: Path):
    sb = root / "outputs" / "sandbox"; sb.mkdir(parents=True)
    (sb / "strategy_comparison.json").write_text(json.dumps(_comparison()))
    (sb / "portfolio_backtest.json").write_text(json.dumps(_backtest()))
    (sb / "portfolio_projection.json").write_text(json.dumps(_projection()))


def test_run_writes_artifact_to_latest(tmp_path):
    _seed_sandbox(tmp_path)
    r = run_simulation_charts(tmp_path)
    out = tmp_path / "outputs" / "latest" / "simulation_charts.json"
    assert out.exists()
    assert r.get("status") != "error"
    assert r["source_files_present"]  # at least one upstream present
    assert json.loads(out.read_text())["observe_only"] is True


def test_run_does_not_touch_decision_plan(tmp_path):
    _seed_sandbox(tmp_path)
    latest = tmp_path / "outputs" / "latest"; latest.mkdir(parents=True)
    dp = latest / "decision_plan.json"
    sentinel = json.dumps({"sentinel": "do-not-touch", "decisions": []})
    dp.write_text(sentinel)
    run_simulation_charts(tmp_path)
    assert dp.read_text() == sentinel  # byte-identical — untouched


def test_run_missing_sandbox_is_nonfatal(tmp_path):
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    r = run_simulation_charts(tmp_path)
    assert r.get("status") != "error"  # builds an all-empty (but valid) contract
    assert r["charts"]["growth_over_time"]["available"] is False


# ── loader ───────────────────────────────────────────────────────────────────

def test_loader_reads_written_artifact(tmp_path):
    _seed_sandbox(tmp_path)
    run_simulation_charts(tmp_path)
    v = collect_simulation_charts_view(tmp_path)
    assert v["available"] is True
    assert v["status"] == "ok"
    assert len(v["summary"]) == 4
    assert v["charts"]["growth_over_time"]["kind"] == "line"
    assert v["charts"]["growth_over_time"]["geometry"]["polylines"]
    assert v["charts"]["risk_return"]["kind"] == "scatter"
    assert v["charts"]["allocation_drift"]["available"] is False


def test_loader_fallback_from_comparison_only(tmp_path):
    # no persisted artifact, but sandbox strategy_comparison exists → limited view
    sb = tmp_path / "outputs" / "sandbox"; sb.mkdir(parents=True)
    (sb / "strategy_comparison.json").write_text(json.dumps(_comparison()))
    v = collect_simulation_charts_view(tmp_path)
    assert v["available"] is True
    assert v["status"] == "limited"
    assert v["charts"]["risk_return"]["available"] is True


def test_loader_absent_returns_empty_state(tmp_path):
    v = collect_simulation_charts_view(tmp_path)
    assert v["available"] is False
    assert v["status"] == "absent"
    assert "not available yet" in v["empty_message"].lower()


def test_loader_malformed_json_does_not_crash(tmp_path):
    latest = tmp_path / "outputs" / "latest"; latest.mkdir(parents=True)
    (latest / "simulation_charts.json").write_text("{bad json", encoding="utf-8")
    v = collect_simulation_charts_view(tmp_path)
    assert v["available"] is False  # falls through to empty, no exception


def test_loader_returns_stable_defaults(tmp_path):
    v = collect_simulation_charts_view(tmp_path)
    for key in ("available", "status", "summary", "charts", "safety"):
        assert key in v
    assert v["safety"]["can_execute_trades"] is False


# ── simulation_context_preview (Portfolio card + memo source) ───────────────

from gui_v2.data.dash_simulation_charts import simulation_context_preview


def test_preview_from_written_artifact(tmp_path):
    _seed_sandbox(tmp_path)
    run_simulation_charts(tmp_path)
    p = simulation_context_preview(tmp_path)
    assert p["available"] is True
    assert p["best_balanced"]["strategy"]
    assert p["best_growth"]["strategy"]
    assert p["biggest_pain_point"]["strategy"]
    assert p["main_lesson"]
    assert p["official_advisory_source"] == "decision_plan.json"


def test_preview_fallback_from_comparison(tmp_path):
    sb = tmp_path / "outputs" / "sandbox"; sb.mkdir(parents=True)
    (sb / "strategy_comparison.json").write_text(json.dumps(_comparison()))
    p = simulation_context_preview(tmp_path)
    assert p["available"] is True
    assert p["best_growth"]["strategy"] == "Charlie"  # highest return in _comparison()


def test_preview_missing_is_graceful(tmp_path):
    p = simulation_context_preview(tmp_path)
    assert p["available"] is False
    assert p["observe_only"] is True
    assert p["official_advisory_source"] == "decision_plan.json"


def test_preview_malformed_does_not_crash(tmp_path):
    latest = tmp_path / "outputs" / "latest"; latest.mkdir(parents=True)
    (latest / "simulation_charts.json").write_text("{bad json")
    p = simulation_context_preview(tmp_path)
    assert p["available"] is False  # no exception


def test_preview_no_forbidden_language(tmp_path):
    _seed_sandbox(tmp_path); run_simulation_charts(tmp_path)
    blob = json.dumps(simulation_context_preview(tmp_path)).lower()
    for phrase in _FORBIDDEN:
        assert phrase not in blob


# ── daily memo Simulation Review section ────────────────────────────────────

from watchlist_scanner.daily_memo import (
    _build_simulation_review_section,
    _build_simulation_review_section_md,
    _simulation_review_bullets,
)

_MEMO_DATA = {"summary": {
    "best_balance": {"strategy": "Long-Term Compounding", "plain_english": "x"},
    "best_growth": {"strategy": "Boom Bucket", "return_pct": 20.0},
    "biggest_pain_point": {"strategy": "Boom Bucket", "max_drawdown_pct": 41.5},
}}


def test_memo_section_at_most_three_bullets():
    assert len(_simulation_review_bullets(_MEMO_DATA)) <= 3
    assert len(_simulation_review_bullets(_MEMO_DATA)) == 3


def test_memo_section_has_sandbox_disclaimer():
    txt = _build_simulation_review_section(_MEMO_DATA)
    low = txt.lower()
    assert "sandbox" in low
    assert "research context only" in low
    assert "does not change decision_plan.json" in low
    assert "not buy/sell" in low  # sanctioned disclaimer phrasing (mirrors discovery)


def test_memo_section_no_forbidden_or_bare_verbs():
    blob = (_build_simulation_review_section(_MEMO_DATA) + _build_simulation_review_section_md(_MEMO_DATA)).lower()
    for w in ("actionable", "promoted", "validated", "enter position", "exit position",
              "deploy capital", "official watchlist promotion", "official recommendation"):
        assert w not in blob
    # no bare trade verbs except the sanctioned 'not buy/sell' negation
    stripped = blob.replace("not buy/sell", "")
    assert not re.search(r"\b(buy|sell|hold|execute|rebalance)\b", stripped)


def test_memo_section_empty_when_no_data():
    assert _build_simulation_review_section({"summary": {}}) == ""
    assert _build_simulation_review_section_md({"summary": {}}) == ""
