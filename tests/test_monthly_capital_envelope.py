"""Tests for the monthly capital envelope (cash_deployment_plan extension).

Covers reserve restoration, net-investable, contribution-cycle accounting,
position sizing, theme cap, precise statuses, and honest degraded states.
All pure-function / temp-dir based — no network, no broker, no production state.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from portfolio_automation import cash_deployment_plan as cdp


TOL = 0.01


# ---------------------------------------------------------------------------
# compute_monthly_envelope — formulas
# ---------------------------------------------------------------------------

def _env(**over):
    base = dict(
        portfolio_value=7851.97, cash_on_hand=151.00, monthly_contribution_gross=758.00,
        reserve_pct=0.05, deployed_before_today=0.0, capital_funded_today=195.00,
        cycle_id="2026-06", cycle_start="2026-06-01", cycle_end="2026-06-30",
        monthly_history_status="ok",
    )
    base.update(over)
    return cdp.compute_monthly_envelope(**base)


class TestEnvelopeFormulas:
    def test_spec_fixture(self):  # canonical example
        e = _env()
        assert e["cash_reserve_target_amount"] == pytest.approx(392.60, abs=TOL)
        assert e["cash_reserve_shortfall"] == pytest.approx(241.60, abs=TOL)
        assert e["monthly_contribution_net_investable"] == pytest.approx(516.40, abs=TOL)
        assert e["capital_funded_today"] == pytest.approx(195.00, abs=TOL)
        assert e["monthly_capital_remaining"] == pytest.approx(321.40, abs=TOL)
        assert e["monthly_utilization_pct"] == pytest.approx(37.8, abs=0.1)

    def test_reserve_already_satisfied(self):
        e = _env(cash_on_hand=1000.0)  # well above 5% target
        assert e["cash_reserve_shortfall"] == 0.0
        assert e["monthly_contribution_net_investable"] == pytest.approx(758.00, abs=TOL)

    def test_reserve_partially_underfunded(self):
        e = _env(cash_on_hand=300.0)  # target 392.6 -> shortfall 92.6
        assert e["cash_reserve_shortfall"] == pytest.approx(92.60, abs=TOL)
        assert e["monthly_contribution_net_investable"] == pytest.approx(665.40, abs=TOL)

    def test_contribution_smaller_than_shortfall(self):
        e = _env(monthly_contribution_gross=100.0, cash_on_hand=0.0)  # shortfall 392.6
        assert e["monthly_contribution_net_investable"] == 0.0  # clamped at 0

    def test_zero_contribution(self):
        e = _env(monthly_contribution_gross=0.0)
        assert e["monthly_contribution_net_investable"] == 0.0

    def test_zero_cash(self):
        e = _env(cash_on_hand=0.0)
        # full reserve target must be restored from contribution
        assert e["cash_reserve_shortfall"] == pytest.approx(392.60, abs=TOL)
        assert e["monthly_contribution_net_investable"] == pytest.approx(365.40, abs=TOL)

    def test_missing_portfolio_value(self):
        e = _env(portfolio_value=None)
        assert e["status"] == cdp.STATUS_INSUFFICIENT_CAPITAL_DATA
        e2 = _env(portfolio_value=0.0)
        assert e2["status"] == cdp.STATUS_INSUFFICIENT_CAPITAL_DATA

    def test_prior_deployment_within_cycle(self):
        e = _env(deployed_before_today=200.0, capital_funded_today=100.0)
        assert e["monthly_capital_deployed_total"] == pytest.approx(300.0, abs=TOL)
        assert e["monthly_capital_remaining"] == pytest.approx(216.40, abs=TOL)  # 516.4-300

    def test_prior_deployment_exhausts_envelope(self):
        e = _env(deployed_before_today=516.40, capital_funded_today=0.0)
        assert e["monthly_capital_remaining"] == 0.0
        assert e["monthly_utilization_pct"] == pytest.approx(100.0, abs=0.1)

    def test_history_unavailable_remaining_is_none(self):
        e = _env(monthly_history_status="unavailable", deployed_before_today=None)
        assert e["monthly_capital_remaining"] is None
        assert e["capital_held_for_future_entries"] is None
        assert e["monthly_history_status"] == "unavailable"

    def test_decimal_rounding(self):
        e = _env(portfolio_value=12345.67, cash_on_hand=100.0, monthly_contribution_gross=1000.0)
        # reserve target = round(0.05*12345.67,2) = 617.28; shortfall=517.28; net=482.72
        assert e["cash_reserve_target_amount"] == pytest.approx(617.28, abs=TOL)
        assert e["monthly_contribution_net_investable"] == pytest.approx(482.72, abs=TOL)


class TestContributionCycle:
    def test_mid_month(self):
        cid, start, end = cdp.contribution_cycle(date(2026, 6, 30))
        assert (cid, start, end) == ("2026-06", "2026-06-01", "2026-06-30")

    def test_december_rollover(self):
        cid, start, end = cdp.contribution_cycle(date(2026, 12, 15))
        assert (cid, start, end) == ("2026-12", "2026-12-01", "2026-12-31")

    def test_february(self):
        cid, start, end = cdp.contribution_cycle(date(2026, 2, 10))
        assert end == "2026-02-28"


# ---------------------------------------------------------------------------
# Ledger — idempotency + degraded states
# ---------------------------------------------------------------------------

class TestLedger:
    def test_absent_ledger_partial_midcycle(self, tmp_path):
        before, status = cdp.resolve_prior_deployment(tmp_path, "2026-06", "2026-06-01", "2026-06-30")
        assert before == 0.0
        assert status == "partial"  # ledger created mid-cycle -> honest partial

    def test_absent_ledger_ok_on_cycle_start(self, tmp_path):
        before, status = cdp.resolve_prior_deployment(tmp_path, "2026-06", "2026-06-01", "2026-06-01")
        assert status == "ok"

    def test_prior_deployment_summed(self, tmp_path):
        cdp.append_deployment_ledger(tmp_path, cycle_id="2026-06", today_iso="2026-06-10", capital_funded=120.0)
        cdp.append_deployment_ledger(tmp_path, cycle_id="2026-06", today_iso="2026-06-20", capital_funded=80.0)
        before, status = cdp.resolve_prior_deployment(tmp_path, "2026-06", "2026-06-01", "2026-06-30")
        assert before == pytest.approx(200.0, abs=TOL)

    def test_idempotent_same_day_lastwins(self, tmp_path):
        # two appends for the same day -> last wins; does not double count next day
        cdp.append_deployment_ledger(tmp_path, cycle_id="2026-06", today_iso="2026-06-10", capital_funded=120.0)
        cdp.append_deployment_ledger(tmp_path, cycle_id="2026-06", today_iso="2026-06-10", capital_funded=120.0)
        before, _ = cdp.resolve_prior_deployment(tmp_path, "2026-06", "2026-06-01", "2026-06-20")
        assert before == pytest.approx(120.0, abs=TOL)  # not 240

    def test_unavailable_on_corrupt_ledger(self, tmp_path):
        p = tmp_path / "policy" / cdp._LEDGER_FILENAME
        p.parent.mkdir(parents=True)
        p.write_bytes(b"\xff\xfe not json")
        before, status = cdp.resolve_prior_deployment(tmp_path, "2026-06", "2026-06-01", "2026-06-30")
        assert status == "unavailable"
        assert before is None  # never silently assume zero


# ---------------------------------------------------------------------------
# allocate_within_envelope — sizing, statuses, caps
# ---------------------------------------------------------------------------

_BANDS = {
    "starter_position_pct": 0.005, "standard_position_pct": 0.01,
    "max_new_position_pct_per_cycle": 0.015, "theme_cap_pct_of_net_investable": 0.40,
}


def _dec(symbol, reason="momentum: +2.0% today", band="normal"):
    return {"symbol": symbol, "decision": "BUY", "priority": 0.6, "reason": reason,
            "conviction_band": band}


class TestAllocation:
    def test_starter_size_is_half_pct(self):
        pv = 10000.0
        rows = cdp.allocate_within_envelope(
            monthly_capital_remaining_before_today=1000.0, net_investable=1000.0,
            portfolio_value=pv, ranked_decisions=[_dec("AAA", "momentum: +12% today")],
            bands=_BANDS,
        )
        # extended -> starter tranche 0.5% of pv = 50
        assert rows[0]["suggested_amount"] == pytest.approx(50.0, abs=TOL)
        assert rows[0]["pct_of_portfolio"] == pytest.approx(0.5, abs=0.01)
        assert rows[0]["status"] == cdp.STATUS_FUNDED_STARTER

    def test_standard_size_is_one_pct(self):
        rows = cdp.allocate_within_envelope(
            monthly_capital_remaining_before_today=1000.0, net_investable=1000.0,
            portfolio_value=10000.0, ranked_decisions=[_dec("BBB", "momentum: +1% today")],
            bands=_BANDS,
        )
        assert rows[0]["suggested_amount"] == pytest.approx(100.0, abs=TOL)
        assert rows[0]["status"] == cdp.STATUS_FUNDED_STANDARD

    def test_no_capital_blocks_by_cash(self):
        rows = cdp.allocate_within_envelope(
            monthly_capital_remaining_before_today=0.0, net_investable=0.0,
            portfolio_value=10000.0, ranked_decisions=[_dec("CCC")], bands=_BANDS,
        )
        assert rows[0]["status"] == cdp.STATUS_BLOCKED_BY_CASH

    def test_monthly_budget_exhausted_defers_not_blocks(self):
        # net_investable > 0 but remaining budget tiny -> deferred by budget, NOT cash
        rows = cdp.allocate_within_envelope(
            monthly_capital_remaining_before_today=60.0, net_investable=1000.0,
            portfolio_value=10000.0,
            ranked_decisions=[_dec("AAA", "momentum: +1% today"), _dec("BBB", "momentum: +1% today")],
            bands=_BANDS,
        )
        # first funds 100? capped by budget 60 -> 60; second has 0 budget -> deferred_by_budget
        statuses = [r["status"] for r in rows]
        assert cdp.STATUS_DEFERRED_BY_MONTHLY_BUDGET in statuses
        assert cdp.STATUS_BLOCKED_BY_CASH not in statuses  # no false cash blockage

    def test_theme_cap_enforced(self):
        # theme cap = 0.40 * net_investable(1000) = 400. Three semis @100 -> third deferred by theme.
        smap = {"AAA": "Semiconductors", "BBB": "Semiconductors", "CCC": "Semiconductors",
                "DDD": "Semiconductors", "EEE": "Semiconductors"}
        rows = cdp.allocate_within_envelope(
            monthly_capital_remaining_before_today=1000.0, net_investable=1000.0,
            portfolio_value=10000.0,
            ranked_decisions=[_dec(s, "momentum: +1% today") for s in ["AAA", "BBB", "CCC", "DDD", "EEE"]],
            bands=_BANDS, sector_map=smap,
        )
        funded_semis = sum(r["suggested_amount"] for r in rows if r["suggested_amount"] > 0)
        assert funded_semis <= 400.0 + TOL  # theme cap respected
        assert cdp.STATUS_DEFERRED_BY_THEME_CAP in [r["status"] for r in rows]

    def test_extended_holds_remainder_for_pullback(self):
        rows = cdp.allocate_within_envelope(
            monthly_capital_remaining_before_today=1000.0, net_investable=1000.0,
            portfolio_value=10000.0, ranked_decisions=[_dec("AAA", "momentum: +20% today")],
            bands=_BANDS,
        )
        # starter 50, standard would be 100 -> held_for_pullback = 50
        assert rows[0]["held_for_pullback"] == pytest.approx(50.0, abs=TOL)
        assert rows[0]["entry_extended"] is True


# ---------------------------------------------------------------------------
# Concentration
# ---------------------------------------------------------------------------

class TestConcentration:
    def test_unavailable_without_classification(self):
        rows = [{"symbol": "AAA", "suggested_amount": 100.0, "sector": None}]
        c = cdp.compute_concentration(rows, net_investable=1000.0, theme_cap_pct=0.40, sector_map=None)
        assert c["available"] is False
        assert "no_canonical" in c["reason"]

    def test_grouped_by_theme(self):
        rows = [
            {"symbol": "AAA", "suggested_amount": 60.0, "sector": "Semiconductors"},
            {"symbol": "BBB", "suggested_amount": 57.0, "sector": "Semiconductors"},
            {"symbol": "CCC", "suggested_amount": 40.0, "sector": "Software"},
        ]
        c = cdp.compute_concentration(rows, net_investable=516.40, theme_cap_pct=0.40,
                                      sector_map={"AAA": "Semiconductors"})
        assert c["available"] is True
        semis = next(t for t in c["themes"] if t["theme"] == "Semiconductors")
        assert semis["funded_today"] == pytest.approx(117.0, abs=TOL)
        # 117 of (60+57+40)=157 total funded -> 74.5%
        assert semis["pct_of_today_funded"] == pytest.approx(74.5, abs=0.5)


# ---------------------------------------------------------------------------
# End-to-end run + idempotency + governance
# ---------------------------------------------------------------------------

def _setup_outputs(tmp_path, *, contribution=758.0, cash=151.0, pv=7851.97):
    base = tmp_path / "outputs"
    (base / "latest").mkdir(parents=True)
    (tmp_path / "config.json").write_text(json.dumps({
        "portfolio": {"monthly_contribution": contribution, "cash_available": cash,
                      "target_cash_weight": 0.05},
        "daily_memo_capital": {"starter_position_pct": 0.005, "standard_position_pct": 0.01,
                               "max_new_position_pct_per_cycle": 0.015,
                               "theme_cap_pct_of_net_investable": 0.40},
    }))
    (base / "latest" / "decision_plan.json").write_text(json.dumps({
        "portfolio_context": {"total_portfolio_value": pv, "degraded_mode": False},
        "decisions": [
            {"symbol": "AAA", "decision": "BUY", "priority": 0.7, "reason": "momentum: +2% today"},
            {"symbol": "BBB", "decision": "BUY", "priority": 0.6, "reason": "momentum: +3% today"},
            {"symbol": "CCC", "decision": "WAIT", "priority": 0.3, "reason": "momentum: +1% today"},
        ],
    }))
    return base


class TestCapitalBasis:
    """portfolio_value + cash_on_hand prefer the live read-only Schwab snapshot."""

    def _write_schwab(self, base, *, ts, authed=True, mv=10544.53, cash=3150.6):
        latest = base / "latest"
        latest.mkdir(parents=True, exist_ok=True)
        (latest / "schwab_portfolio_snapshot.json").write_text(json.dumps({
            "snapshot_timestamp": ts, "generated_at": ts,
            "totals": {"market_value": mv, "cash": cash},
        }))
        (latest / "broker_sync_status.json").write_text(json.dumps({
            "authenticated": authed, "overall_status": "ok" if authed else "error",
        }))

    def test_prefers_fresh_authed_schwab(self):
        import tempfile
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "outputs"
            now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
            self._write_schwab(base, ts="2026-06-30T09:00:00+00:00")
            dp = {"portfolio_context": {"total_portfolio_value": 7851.97, "cash": 464.16}}
            pv, cash, pv_src, cash_src = cdp.resolve_capital_basis(base, dp, {}, now)
            assert pv == pytest.approx(10544.53, abs=TOL)
            assert cash == pytest.approx(3150.6, abs=TOL)
            assert pv_src == "schwab_snapshot" and cash_src == "schwab_snapshot"

    def test_stale_schwab_falls_to_context(self):
        import tempfile
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "outputs"
            now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
            self._write_schwab(base, ts="2026-06-01T00:00:00+00:00")  # >24h stale
            dp = {"portfolio_context": {"total_portfolio_value": 7851.97, "cash": 464.16}}
            pv, cash, pv_src, cash_src = cdp.resolve_capital_basis(base, dp, {}, now)
            assert pv == pytest.approx(7851.97, abs=TOL)
            assert cash == pytest.approx(464.16, abs=TOL)
            assert pv_src == "decision_plan.portfolio_context"

    def test_unauthed_schwab_falls_to_context(self):
        import tempfile
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "outputs"
            now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
            self._write_schwab(base, ts="2026-06-30T09:00:00+00:00", authed=False)
            dp = {"portfolio_context": {"total_portfolio_value": 7851.97, "cash": 464.16}}
            pv, _, pv_src, _ = cdp.resolve_capital_basis(base, dp, {}, now)
            assert pv == pytest.approx(7851.97, abs=TOL)
            assert pv_src == "decision_plan.portfolio_context"

    def test_no_broker_no_context_uses_config(self):
        import tempfile
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "outputs"
            (base / "latest").mkdir(parents=True)
            now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
            cfg = {"portfolio": {"cash_available": 150.6}}
            pv, cash, pv_src, cash_src = cdp.resolve_capital_basis(base, {}, cfg, now)
            assert cash == pytest.approx(150.6, abs=TOL)
            assert cash_src == "config.portfolio.cash_available"


class TestEndToEnd:
    def test_run_writes_envelope_and_is_idempotent(self, tmp_path):
        base = _setup_outputs(tmp_path)
        p1 = cdp.run_cash_deployment_plan(tmp_path, base_dir=base, as_of_date=date(2026, 6, 15), run_id="a")
        p2 = cdp.run_cash_deployment_plan(tmp_path, base_dir=base, as_of_date=date(2026, 6, 15), run_id="b")
        e1, e2 = p1["monthly_capital_envelope"], p2["monthly_capital_envelope"]
        # same-day re-run: deployed_before_today + remaining unchanged (idempotent)
        assert e1["monthly_capital_deployed_before_today"] == e2["monthly_capital_deployed_before_today"]
        assert e1["capital_funded_today"] == e2["capital_funded_today"]
        assert e1["monthly_capital_remaining"] == e2["monthly_capital_remaining"]
        assert p1["observe_only"] is True and p1["no_trade"] is True

    def test_next_day_counts_prior_deployment(self, tmp_path):
        base = _setup_outputs(tmp_path)
        cdp.run_cash_deployment_plan(tmp_path, base_dir=base, as_of_date=date(2026, 6, 15), run_id="d1")
        p2 = cdp.run_cash_deployment_plan(tmp_path, base_dir=base, as_of_date=date(2026, 6, 16), run_id="d2")
        e2 = p2["monthly_capital_envelope"]
        assert (e2["monthly_capital_deployed_before_today"] or 0) > 0  # day-1 deployment counted

    def test_funded_exceeding_envelope_capped(self, tmp_path):
        # tiny contribution -> small net; ensure funded never exceeds net investable
        base = _setup_outputs(tmp_path, contribution=300.0, cash=151.0)
        p = cdp.run_cash_deployment_plan(tmp_path, base_dir=base, as_of_date=date(2026, 6, 1), run_id="x")
        e = p["monthly_capital_envelope"]
        assert e["capital_funded_today"] <= e["monthly_contribution_net_investable"] + TOL

    def test_no_net_investable_no_false_block(self, tmp_path):
        # contribution fully consumed by reserve restoration -> net 0
        base = _setup_outputs(tmp_path, contribution=100.0, cash=0.0)
        p = cdp.run_cash_deployment_plan(tmp_path, base_dir=base, as_of_date=date(2026, 6, 1), run_id="z")
        e = p["monthly_capital_envelope"]
        assert e["monthly_contribution_net_investable"] == 0.0

    def test_does_not_mutate_decision_plan(self, tmp_path):
        base = _setup_outputs(tmp_path)
        dp_path = base / "latest" / "decision_plan.json"
        before = dp_path.read_text()
        cdp.run_cash_deployment_plan(tmp_path, base_dir=base, as_of_date=date(2026, 6, 1))
        assert dp_path.read_text() == before
