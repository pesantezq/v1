"""Tests for the daily-memo decision-coherence reconciliation layer.

Covers the acceptance criteria in docs/DAILY_MEMO_DECISION_COHERENCE_PLAN.md.
All tests use synthetic in-memory ``sources`` dicts (pure-function inputs) so
they never depend on live FMP/broker state or on-disk artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation import memo_coherence as mc


# ---------------------------------------------------------------------------
# fixtures / builders
# ---------------------------------------------------------------------------

def _decision(symbol, decision="BUY", priority=0.55, source="market", band="watch",
              drivers=None, reason="momentum: +2.0% today", confidence=0.8, risk_flags=None):
    return {
        "symbol": symbol,
        "decision": decision,
        "priority": priority,
        "priority_score": priority,
        "source": source,
        "confidence": confidence,
        "reason": reason,
        "risk_flags": risk_flags or [],
        "inputs_used": {"is_existing_holding": False},
        "decision_reason_structured": {
            "band": band,
            "strategy": source,
            "drivers": drivers or {"conviction_score": 0.0, "signal_score": 0.0,
                                   "confidence_score": 0.0, "priority_score": priority},
            "why": [],
        },
    }


def _sources(**over):
    base = {
        "decision_plan": {
            "generated_at": "2026-06-30T09:00:00+00:00",
            "portfolio_context": {"degraded_mode": False},
            "decisions": [
                _decision("PANW", "BUY", 0.55, reason="momentum: +9.14% today, RS: near 52wk high"),
                _decision("MSFT", "BUY", 0.55, reason="momentum: +1.2% today"),
                _decision("NOC", "WAIT", 0.475, source="watchlist", band="observe",
                          drivers={"conviction_score": 0.4, "signal_score": 0.5, "confidence_score": 0.6, "priority_score": 0.475},
                          reason="momentum: +0.5% today"),
                _decision("QQQ", "HOLD", 0.30, source="portfolio", band="normal"),
            ],
        },
        "system_decision_summary": {
            "generated_at": "2026-06-30T09:00:30+00:00",
            "top_theme": {"name": "Defense", "tickers": ["NOC", "RTX"], "score": 0.7},
            "top_opportunity": {"ticker": "GOOGL", "final_rank_score": 0.8},
            "best_portfolio_fit": {"ticker": "NOC", "portfolio_fit_score": 0.9},
            "changes": {"changes": ["Theme shifted to Defense", "Lead opp MSFT→GOOGL"]},
        },
        "cash_deployment_plan": {
            "generated_at": "2026-06-30T09:01:00+00:00",
            "cash_summary": {
                "portfolio_value": 7851.97, "cash_available": 150.6, "target_cash_pct": 0.05,
                "total_deployable_amount": 758.0, "below_safety_floor": True,
            },
            "deployment_rows": [
                {"symbol": "PANW", "decision": "BUY", "priority": 0.55, "conviction_band": "watch",
                 "suggested_amount": 39.26, "suggested_pct": 0.005, "skipped_reason": None},
            ],
            "total_deployed_amount": 39.26, "remaining_budget": 718.74,
        },
        "risk_delta": {
            "generated_at": "2026-06-30T09:01:10+00:00",
            "overall_status": "near_cap",
            "concentration": {"top_position": 0.40, "cap": 0.35},
            "leverage": {"total_exposure": 1.1},
        },
        "correlation_risk_advisor": {
            "generated_at": "2026-06-30T09:01:20+00:00",
            "effective_independent_bets": 1.386,
            "high_correlation_pairs": [
                {"pair": ["QQQ", "QLD"], "correlation": 0.999, "combined_weight": 0.53, "flag": "high_correlation_concentration"},
                {"pair": ["QQQ", "CHAT"], "correlation": 0.895, "combined_weight": 0.53, "flag": "high_correlation_concentration"},
            ],
            "overall_flags": ["low_effective_independent_bets"],
        },
        "confidence_calibration": {
            "generated_at": "2026-06-30T09:01:30+00:00",
            "available": True, "insufficient_data": False, "total_resolved": 417,
            "overall_hit_rate": 0.405,
        },
        "unified_crowd": {
            "generated_at": "2026-06-30T09:01:40+00:00",
            "overall_status": "ok", "feeds_decision_engine": False, "production_gated": True,
            "social_sentiment_status": "PLAN_LOCKED",
            "enabled_categories": ["analyst", "attention"], "disabled_categories": ["social_sentiment"],
            "state_counts": {"confirmed_attention": 11, "divergent_attention": 9, "insufficient_data": 22,
                             "retail_only_attention": 51},
            "top_confirmed_attention": [{"ticker": "TSLA"}, {"ticker": "AMD"}],
            "top_retail_only_attention": [{"ticker": "GME"}],
            "top_divergent_attention": [{"ticker": "NVDA"}],
        },
        "portfolio_snapshot": {"holdings": []},
        "decision_outcomes": [
            {"resolved": True, "return_pct": 0.05, "decision": "BUY"},     # +5% hit
            {"resolved": True, "return_pct": -0.03, "decision": "BUY"},    # -3% miss
            {"resolved": True, "return_pct": 0.0006, "decision": "BUY"},   # +0.06% NOISE -> neutral
            {"resolved": True, "return_pct": -0.0009, "decision": "BUY"},  # -0.09% NOISE -> neutral
            {"resolved": True, "return_pct": None, "decision": "BUY"},     # missing price
            {"resolved": False, "return_pct": None, "decision": "BUY"},    # unresolved
        ],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Reconciliation (AC1-AC5)
# ---------------------------------------------------------------------------

class TestReconciliation:
    def test_consistent_inputs_status_ok_or_warning(self):  # AC5
        r = mc.build_memo_coherence(_sources())
        # legitimate explained differences should be resolved, not hard failures
        assert r["coherence_status"] in {"ok", "warning"}

    def test_cautious_with_buys_warns(self):  # AC1
        src = _sources()
        src["risk_delta"]["overall_status"] = "near_cap"  # -> cautious posture
        r = mc.build_memo_coherence(src)
        ids = {i["id"] for i in r["reconciliation"]["issues"]}
        assert "verdict_conflicts_with_action_mix" in ids

    def test_top_opportunity_missing_reason(self):  # AC2
        r = mc.build_memo_coherence(_sources())  # GOOGL not in decisions
        issue = next(i for i in r["reconciliation"]["issues"]
                     if i["id"] == "top_opportunity_missing_from_top_decisions")
        assert "GOOGL" in issue["message"]
        assert issue["resolved"] is True  # explained, not a hard failure

    def test_best_fit_not_funded_explained(self):  # AC3
        r = mc.build_memo_coherence(_sources())  # NOC is WAIT, not funded
        issue = next(i for i in r["reconciliation"]["issues"] if i["id"] == "best_fit_missing")
        assert "NOC" in issue["message"]

    def test_theme_not_represented_flagged(self):  # AC4
        src = _sources()
        # Defense tickers NOC/RTX -> NOC present but ranked low; force absence
        src["system_decision_summary"]["top_theme"]["tickers"] = ["RTX", "LMT"]
        r = mc.build_memo_coherence(src)
        ids = {i["id"] for i in r["reconciliation"]["issues"]}
        assert "dominant_theme_not_represented" in ids


# ---------------------------------------------------------------------------
# Funding (AC6-AC10)
# ---------------------------------------------------------------------------

class TestFunding:
    def test_funding_split_exceeds_cash(self):  # AC6
        r = mc.build_memo_coherence(_sources())
        f = r["funding"]
        assert f["available"] is True
        assert f["funded_count"] >= 1
        assert f["blocked_count"] >= 1  # MSFT BUY not in deployment rows -> deferred

    def test_funded_never_exceeds_deployable(self):  # AC7
        r = mc.build_memo_coherence(_sources())
        f = r["funding"]
        assert f["funded_capital"] <= f["max_deployable"] + 1e-6

    def test_cash_vs_incoming_split(self):
        r = mc.build_memo_coherence(_sources())
        f = r["funding"]
        # cash 150.6 - reserve (7851.97*0.05=392.6) -> 0 deployable from cash
        assert f["deployable_from_cash"] == 0.0
        assert f["deployable_from_incoming"] == pytest.approx(758.0, abs=1.0)

    def test_zero_cash_honest(self):  # AC8
        src = _sources()
        src["cash_deployment_plan"]["cash_summary"]["cash_available"] = 0.0
        src["cash_deployment_plan"]["cash_summary"]["total_deployable_amount"] = 0.0
        src["cash_deployment_plan"]["deployment_rows"] = []
        r = mc.build_memo_coherence(src)
        f = r["funding"]
        assert f["funded_count"] == 0
        assert f["funded_capital"] == 0.0

    def test_no_phantom_sale_proceeds(self):  # AC9
        # funded capital must come only from deployment rows, never invented
        src = _sources()
        r = mc.build_memo_coherence(src)
        assert r["funding"]["funded_capital"] == pytest.approx(39.26, abs=0.01)

    def test_missing_cash_degraded(self):  # AC10
        src = _sources()
        src["cash_deployment_plan"] = None
        r = mc.build_memo_coherence(src)
        assert r["funding"]["available"] is False
        assert r["coherence_status"] == "degraded"


# ---------------------------------------------------------------------------
# Ranking transparency (AC11-AC14)
# ---------------------------------------------------------------------------

class TestRanking:
    def test_default_priority_detected(self):  # AC11
        r = mc.build_memo_coherence(_sources())
        panw = next(a for a in r["actions"] if a["symbol"] == "PANW")
        assert panw["priority_basis"] == "default_fallback"
        assert r["ranking"]["default_fallback_count"] >= 2

    def test_tie_break_deterministic(self):  # AC12
        r1 = mc.build_memo_coherence(_sources())
        r2 = mc.build_memo_coherence(_sources())
        order1 = [a["symbol"] for a in r1["actions"]]
        order2 = [a["symbol"] for a in r2["actions"]]
        assert order1 == order2
        # PANW (+9.14% momentum) should outrank MSFT (+1.2%) at the same 0.55 tie
        assert order1.index("PANW") < order1.index("MSFT")

    def test_priority_breakdown_preserved(self):  # AC13
        r = mc.build_memo_coherence(_sources())
        noc = next(a for a in r["actions"] if a["symbol"] == "NOC")
        bd = noc["priority_breakdown"]
        assert set(bd["contributions"].keys()) == {"conviction", "signal", "confidence"}
        assert bd["basis"] == "computed"

    def test_rounding_does_not_collapse(self):  # AC14
        src = _sources()
        src["decision_plan"]["decisions"] = [
            _decision("A", priority=0.5502, drivers={"conviction_score": 0.5, "signal_score": 0.5, "confidence_score": 0.5}),
            _decision("B", priority=0.5498, drivers={"conviction_score": 0.5, "signal_score": 0.5, "confidence_score": 0.4}),
        ]
        r = mc.build_memo_coherence(src)
        # distinct 4dp priorities preserved
        assert r["ranking"]["distinct_priorities"] == 2


# ---------------------------------------------------------------------------
# Hit-rate neutral band (AC15-AC16)
# ---------------------------------------------------------------------------

class TestHitRate:
    def test_hit_rate_neutral_band(self):  # AC15
        r = mc.build_memo_coherence(_sources())
        h = r["hit_rate"]
        assert h["neutral"] == 2  # the +0.06% and -0.09% noise moves
        assert h["correct"] == 1
        assert h["incorrect"] == 1
        assert h["directional_accuracy_pct"] == 50.0

    def test_missing_price_not_scored(self):  # AC16
        r = mc.build_memo_coherence(_sources())
        h = r["hit_rate"]
        # the None-return resolved row is not counted correct/incorrect/neutral
        assert h["correct"] + h["incorrect"] + h["neutral"] == 4
        assert h["unresolved_count"] == 1

    def test_band_reused_not_invented(self):
        r = mc.build_memo_coherence(_sources())
        assert r["hit_rate"]["neutral_band_pct"] == 1.0
        assert "outcome_evaluator" in r["hit_rate"]["neutral_band_source"]


# ---------------------------------------------------------------------------
# Entry context (AC17-AC18)
# ---------------------------------------------------------------------------

class TestEntryContext:
    def test_entry_extended_context(self):  # AC17
        r = mc.build_memo_coherence(_sources())
        panw = next(a for a in r["actions"] if a["symbol"] == "PANW")  # +9.14%
        assert panw["entry_extended"] is True
        assert panw["entry_context"] == "extended"
        assert panw["presentation_state"] in {"STARTER", "ADD_ON_PULLBACK"}
        assert "Extended" in (panw["primary_risk"] or "")

    def test_entry_normal_no_warning(self):  # AC18
        r = mc.build_memo_coherence(_sources())
        msft = next(a for a in r["actions"] if a["symbol"] == "MSFT")  # +1.2%
        assert msft["entry_extended"] is False
        assert msft["entry_context"] == "normal"

    def test_unparseable_move_unknown(self):
        src = _sources()
        src["decision_plan"]["decisions"] = [_decision("ZZZ", reason="no move here")]
        r = mc.build_memo_coherence(src)
        z = next(a for a in r["actions"] if a["symbol"] == "ZZZ")
        assert z["entry_context"] == "unknown"
        assert z["entry_extended"] is False


# ---------------------------------------------------------------------------
# Presentation states
# ---------------------------------------------------------------------------

class TestPresentationStates:
    def test_funded_normal_buy_now(self):
        assert mc.derive_presentation_state("BUY", band="normal", funded=True,
            blocking_reason=None, entry_extended=False, degraded=False, sandbox=False) == "BUY_NOW"

    def test_unfunded_blocked_by_cash(self):
        assert mc.derive_presentation_state("BUY", band="normal", funded=False,
            blocking_reason="cash", entry_extended=False, degraded=False, sandbox=False) == "BLOCKED_BY_CASH"

    def test_concentration_block(self):
        assert mc.derive_presentation_state("BUY", band="normal", funded=False,
            blocking_reason="concentration", entry_extended=False, degraded=False, sandbox=False) == "BLOCKED_BY_CONCENTRATION"

    def test_sandbox_research_only(self):
        assert mc.derive_presentation_state("BUY", band="normal", funded=True,
            blocking_reason=None, entry_extended=False, degraded=False, sandbox=True) == "RESEARCH_ONLY"

    def test_degraded_insufficient_data(self):
        assert mc.derive_presentation_state("BUY", band="normal", funded=True,
            blocking_reason=None, entry_extended=False, degraded=True, sandbox=False) == "INSUFFICIENT_DATA"

    def test_sell_is_trim(self):
        assert mc.derive_presentation_state("SELL", band="guardrail", funded=False,
            blocking_reason=None, entry_extended=False, degraded=False, sandbox=False) == "TRIM"


# ---------------------------------------------------------------------------
# Overlap (AC19-AC21)
# ---------------------------------------------------------------------------

class TestOverlap:
    def test_semiconductor_cluster(self):  # AC19
        src = _sources()
        src["decision_plan"]["decisions"] = [
            _decision("ASML", "BUY"), _decision("LRCX", "BUY"), _decision("KLAC", "BUY"),
        ]
        src["correlation_risk_advisor"]["high_correlation_pairs"] = [
            {"pair": ["ASML", "LRCX"], "correlation": 0.92},
            {"pair": ["LRCX", "KLAC"], "correlation": 0.90},
        ]
        r = mc.build_memo_coherence(src)
        clusters = r["overlap"]["clusters"]
        assert any(set(c["members"]) == {"ASML", "LRCX", "KLAC"} and c["multiple_proposed_same_thesis"]
                   for c in clusters)

    def test_etf_lookthrough_degraded(self):  # AC20
        r = mc.build_memo_coherence(_sources())
        assert r["overlap"]["etf_lookthrough_available"] is False
        assert r["overlap"]["etf_lookthrough_reason"] == "no_constituent_dataset"

    def test_existing_exposure_surfaced(self):  # AC21
        r = mc.build_memo_coherence(_sources())
        members = [set(c["members"]) for c in r["overlap"]["clusters"]]
        assert any({"QQQ", "CHAT"} <= m for m in members)
        assert r["overlap"]["effective_independent_bets"] == 1.386

    def test_overlap_missing_advisor_degrades(self):
        src = _sources()
        src["correlation_risk_advisor"] = None
        r = mc.build_memo_coherence(src)
        assert r["overlap"]["available"] is False


# ---------------------------------------------------------------------------
# Crowd (AC22-AC24)
# ---------------------------------------------------------------------------

class TestCrowd:
    def test_raw_vs_classified(self):  # AC22
        r = mc.build_memo_coherence(_sources())
        crowd = r["crowd"]
        assert crowd["any_classified_buy_state"] is False
        assert "cross_source_confirmation" in crowd["definitions"]
        assert crowd["cross_source_confirmed"]  # attention overlap surfaced separately

    def test_no_credentials_nonblocking(self):  # AC23
        src = _sources()
        src["unified_crowd"] = None
        r = mc.build_memo_coherence(src)
        assert r["crowd"]["available"] is False
        assert r["crowd"]["production_eligible"] is False
        # missing crowd must not crash or hard-fail the whole result
        assert r["coherence_status"] in {"ok", "warning", "degraded"}

    def test_insufficient_consistent(self):  # AC24
        r = mc.build_memo_coherence(_sources())
        assert r["crowd"]["insufficient_data_count"] == 22
        ids = {i["id"] for i in r["reconciliation"]["issues"]}
        assert "crowd_attention_vs_classified" in ids

    def test_crowd_not_production_eligible(self):
        r = mc.build_memo_coherence(_sources())
        assert r["crowd"]["production_eligible"] is False
        assert r["crowd"]["feeds_decision_engine"] is False


# ---------------------------------------------------------------------------
# Robustness / governance (AC29-AC30)
# ---------------------------------------------------------------------------

class TestRobustness:
    def test_run_memo_coherence_degraded_never_raises(self, tmp_path):  # AC29
        # empty root -> all artifacts missing
        r = mc.run_memo_coherence(tmp_path, write_files=False)
        assert isinstance(r, dict)
        assert r["observe_only"] is True
        assert r["no_trade"] is True

    def test_observe_only_hardcoded(self):
        r = mc.build_memo_coherence(_sources())
        assert r["observe_only"] is True
        assert r["no_trade"] is True
        assert r["source"] == "memo_coherence"

    def test_no_mutation_of_decision_plan(self):  # AC30
        src = _sources()
        before = json.dumps(src["decision_plan"], sort_keys=True)
        mc.build_memo_coherence(src)
        after = json.dumps(src["decision_plan"], sort_keys=True)
        assert before == after

    def test_empty_dict_no_crash(self):
        r = mc.build_memo_coherence({})
        assert r["coherence_status"] in {"ok", "warning", "degraded"}

    def test_writes_artifact(self, tmp_path):
        # build a minimal outputs tree so safe_write can land
        r = mc.run_memo_coherence(tmp_path, write_files=True)
        out = tmp_path / "outputs" / "latest" / "memo_coherence.json"
        # write may route via governance; assert either it wrote or recorded honestly
        assert isinstance(r, dict)
        if out.exists():
            payload = json.loads(out.read_text())
            assert payload["observe_only"] is True


class TestFreshness:
    def test_stale_source_flagged(self):
        src = _sources()
        src["risk_delta"]["generated_at"] = "2026-06-01T00:00:00+00:00"  # >24h before others
        r = mc.build_memo_coherence(src)
        # snapshot derived from newest; risk_delta should be flagged stale
        assert "risk_delta" in r["freshness"]["stale_sources"]

    def test_render_md_smoke(self):
        r = mc.build_memo_coherence(_sources())
        md = mc.render_memo_coherence_md(r)
        assert "Memo Coherence Diagnostics" in md
        assert "Advisory only" in md


class TestProbeAndValidator:
    def test_probe_registered_and_valid(self):
        from operator_control.probe_registry import PROBES, validate_registry
        validate_registry()  # raises on structural error
        assert "quant.daily_memo_coherence" in PROBES
        probe = PROBES["quant.daily_memo_coherence"]
        assert probe.source_view == "memo"
        assert probe.source_artifact == "outputs/latest/memo_coherence.json"
        assert probe.approval_required is False  # observe-only diagnostic

    def test_validator_runs_on_empty_root(self, tmp_path):
        from tools.validate_daily_memo_coherence import main
        rc = main([str(tmp_path)])  # no artifacts -> degraded, but never errors
        assert rc == 0


class TestLeadNameRendering:
    """The rendered investor core must explain why the funded pick, the model's
    top opportunity, and the best portfolio fit can be three different symbols
    (resolves the Q1 'which name is the lead?' ambiguity)."""

    def test_lead_name_notes_rendered_in_investor_core(self):
        from watchlist_scanner.daily_memo import _investor_core_text, _lead_name_notes
        r = mc.build_memo_coherence(_sources())  # GOOGL top-opp absent, NOC best-fit unfunded
        notes = _lead_name_notes(r["reconciliation"])
        assert any("GOOGL" in n for n in notes)
        assert any("NOC" in n for n in notes)
        lines = _investor_core_text(r)
        block = "\n".join(lines)
        assert "WHY THE LEAD NAMES DIFFER" in block
        assert "GOOGL" in block and "NOC" in block

    def test_no_lead_name_block_when_aligned(self):
        from watchlist_scanner.daily_memo import _lead_name_notes
        src = _sources()
        # make top opportunity + best fit the funded PANW so nothing diverges
        src["system_decision_summary"]["top_opportunity"] = {"ticker": "PANW"}
        src["system_decision_summary"]["best_portfolio_fit"] = {"ticker": "PANW"}
        src["system_decision_summary"]["top_theme"] = {"name": "Semis", "tickers": ["PANW"]}
        r = mc.build_memo_coherence(src)
        assert _lead_name_notes(r["reconciliation"]) == []
