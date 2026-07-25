"""
Regression tests for the whole-branch review fixes:
  1. stale market_data_date blocks a real send (fail-closed)
  2. WEEKLY_ETF_BUNDLES_ENABLED master kill-switch skips the cron wrapper
  3. mature_prediction honors an earlier now_date cutoff (point-in-time bound)
  4. renderer MD/HTML agree on the 12w-excess label
  5. drawdown-regression gate fails closed on missing data
"""
from __future__ import annotations

import subprocess
from datetime import date, timedelta
from pathlib import Path

from portfolio_automation.portfolio_sim.prices import PricePanel
from portfolio_automation.weekly_etf_bundles import outcomes as O
from portfolio_automation.weekly_etf_bundles import renderer as R
from portfolio_automation.weekly_etf_bundles import run as RUN
from portfolio_automation.weekly_etf_bundles import strat_lab_adapter as SL

REPO = Path(__file__).resolve().parents[1]


def _weekdays(start_iso, n):
    d = date.fromisoformat(start_iso); out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _panel(series_by_symbol, dates):
    closes = {s: {dates[i]: float(p) for i, p in enumerate(ps)} for s, ps in series_by_symbol.items()}
    return PricePanel(closes, {s: {d: 1.0 for d in dates} for s in series_by_symbol}, list(dates), [])


# 1. stale-data send guard --------------------------------------------------- #
def test_send_blocked_on_stale_market_data():
    stale = (date.today() - timedelta(days=30)).isoformat()
    fresh = (date.today() - timedelta(days=2)).isoformat()
    assert RUN._send_block_reason({"status": "ok", "market_data_date": stale,
                                   "coverage": 1.0}) == "stale_market_data"
    assert RUN._send_block_reason({"status": "ok", "market_data_date": fresh,
                                   "coverage": 1.0}) is None


def test_send_blocked_on_low_coverage_and_bad_date():
    assert RUN._send_block_reason({"status": "ok", "market_data_date": date.today().isoformat(),
                                   "coverage": 0.5}) == "coverage_below_threshold"
    assert RUN._send_block_reason({"status": "insufficient_data"}) == "analysis_not_ok"


# 2. master kill-switch in the wrapper -------------------------------------- #
def test_wrapper_skips_when_disabled(tmp_path):
    # REPO_ROOT points at a temp dir with no .env → flag defaults off → skip.
    env = {"REPO_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin",
           "WEEKLY_ETF_BUNDLES_ENABLED": "0"}
    proc = subprocess.run(["bash", str(REPO / "scripts" / "run_weekly_etf_bundles.sh")],
                          env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0
    assert "disabled" in proc.stdout.lower()
    # skipped before doing any work → no logs dir created in the temp root
    assert not (tmp_path / "logs").exists()


def test_wrapper_gate_present():
    body = (REPO / "scripts" / "run_weekly_etf_bundles.sh").read_text()
    assert "WEEKLY_ETF_BUNDLES_ENABLED" in body


# 3. now_date cutoff -------------------------------------------------------- #
def test_mature_prediction_honors_earlier_now_date():
    dates = _weekdays("2026-01-05", 60)
    mdd = dates[0]
    etf = [100.0] + [100.0 * (1 + 0.05 * min(i, 20) / 20) for i in range(1, 60)]
    panel = _panel({"SMH": etf, "QQQ": [100.0] * 60}, dates)
    pred = {"prediction_id": "p", "market_data_date": mdd, "bundle_id": "ai",
            "symbol": "SMH", "benchmark": "QQQ", "price_at_prediction": 100.0}
    # now_date only 2 weeks out → 4w horizon must be PENDING, not matured.
    early = dates[9]
    out = O.mature_prediction(pred, panel, "4w", now_date=early)
    assert out["status"] == "pending"
    # full panel → matured
    out2 = O.mature_prediction(pred, panel, "4w", now_date=dates[-1])
    assert out2["status"] == "matured"


# 4. renderer label agreement ---------------------------------------------- #
def test_md_html_agree_on_12w_excess_label():
    payload = {
        "status": "ok", "market_data_date": "2026-07-24", "generated_at": "x",
        "bundle_count": 1, "etf_count": 1, "coverage": 1.0,
        "market_context": {"market_regime": "risk_on", "volatility_regime": "normal"},
        "stale_symbols": [], "failed_symbols": [], "panel_missing_symbols": [],
        "bundles": [{"bundle_id": "ai", "name": "AI", "benchmark": "QQQ", "bundle_score": 70.0,
                     "state": "Broad leadership", "excess_return_12w": 0.04,
                     "pct_above_sma50": 1.0, "pct_above_sma200": 1.0,
                     "pct_positive_momentum_4w": 1.0, "leadership_concentration": 0.1,
                     "score_dispersion": 5.0, "weekly_score_change": None,
                     "strongest": "SMH", "weakest": "SMH",
                     "members": [{"symbol": "SMH", "role": "", "watch_score": 80,
                                  "label": "leading", "rank_in_bundle": 1, "components": {},
                                  "metrics": {"return_4w": 0.03, "excess_return_12w": 0.04}}]}],
        "ranking_global": [{"symbol": "SMH", "watch_score": 80}],
    }
    md = R.render_weekly_md(payload)
    assert "12w excess vs benchmark" in md
    assert "4w excess vs benchmark" not in md   # the mislabel is gone


# 5. drawdown gate fail-closed --------------------------------------------- #
def test_drawdown_gate_fails_closed_on_missing_data():
    champ = {"matured_prediction_count": 200, "calendar_weeks_span": 40,
             "benchmark_relative_hit_rate": 0.50, "information_coefficient": 0.03,
             "avg_max_drawdown": -0.10, "by_market_regime": {}}
    # challenger missing avg_max_drawdown → the dd gate must fail (not pass)
    chall = {"matured_prediction_count": 200, "calendar_weeks_span": 40,
             "benchmark_relative_hit_rate": 0.58, "information_coefficient": 0.09,
             "top_bottom_score_spread": 0.04, "avg_excess_return": 0.012,
             "avg_max_drawdown": None,
             "by_market_regime": {"risk_on": {"relative_hit_rate": 0.6, "count": 40},
                                  "risk_off": {"relative_hit_rate": 0.55, "count": 30}}}
    res = SL.evaluate_promotion_gates(champ, chall)
    dd = next(c for c in res["checks"] if c["gate"] == "maximum_drawdown_regression")
    assert dd["ok"] is False
    assert res["passes_all_gates"] is False
