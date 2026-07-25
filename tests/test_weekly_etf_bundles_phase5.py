"""
Phase 5 tests — renderer (MD/HTML agreement, observe-only, missing != zero,
track-record gating) and emailer (dry-run/disabled never touch SMTP, duplicate
suppression, force bypass, receipt). Hermetic.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from portfolio_automation.weekly_etf_bundles import emailer as EM
from portfolio_automation.weekly_etf_bundles import renderer as R


def _payload(*, missing=False):
    member = {
        "symbol": "SMH", "role": "Semiconductors", "watch_score": 86, "label": "leading",
        "rank_in_bundle": 1, "components": {"momentum_4w": 88},
        "metrics": {"available": True, "price": 100.0, "return_4w": 0.05, "excess_return_12w": 0.08},
    }
    if missing:
        member = {
            "symbol": "SMH", "role": "Semiconductors", "watch_score": None, "label": "insufficient_data",
            "rank_in_bundle": None, "components": {},
            "metrics": {"available": True, "price": 100.0, "return_4w": None, "excess_return_12w": None},
        }
    return {
        "status": "ok", "market_data_date": "2026-07-24",
        "generated_at": "2026-07-27T08:00:00Z",
        "bundle_count": 1, "etf_count": 1, "coverage": 1.0,
        "market_context": {"market_regime": "risk_on", "volatility_regime": "normal"},
        "stale_symbols": [], "failed_symbols": [], "panel_missing_symbols": [],
        "bundles": [{
            "bundle_id": "ai", "name": "AI Infrastructure", "benchmark": "QQQ",
            "bundle_score": 78.0 if not missing else None, "state": "Broad leadership",
            "excess_return_12w": 0.04, "pct_above_sma50": 1.0, "pct_above_sma200": 1.0,
            "pct_positive_momentum_4w": 1.0, "leadership_concentration": 0.1,
            "score_dispersion": 5.0, "weekly_score_change": 2.0,
            "strongest": "SMH", "weakest": "SMH", "members": [member],
        }],
        "ranking_global": [{"symbol": "SMH", "bundle_id": "ai", "watch_score": member["watch_score"]}],
    }


# --------------------------------------------------------------------------- #
# renderer
# --------------------------------------------------------------------------- #
def test_md_and_html_agree_on_key_facts():
    p = _payload()
    md = R.render_weekly_md(p)
    htmlout = R.render_weekly_html(p)
    for token in ("2026-07-24", "AI Infrastructure", "SMH"):
        assert token in md and token in htmlout
    # observe-only language in both
    assert "OBSERVE-ONLY" in md and "OBSERVE-ONLY" in htmlout
    assert "do not represent live portfolio returns" in md
    assert "do not represent live portfolio returns" in htmlout or "not\nrepresent" in htmlout


def test_subject_line_format():
    assert R.render_subject(_payload()) == "Weekly ETF Bundle Watchlist — 2026-07-24"


def test_missing_values_render_as_na_not_zero():
    p = _payload(missing=True)
    md = R.render_weekly_md(p)
    assert "n/a" in md
    # the None 4w return must not be printed as +0.00%
    assert "| n/a |" in md or " n/a " in md


def test_track_record_withheld_when_insufficient():
    md = R.render_weekly_md(_payload(), scorecard={"sample_status": "provisional",
                                                   "matured_prediction_count": 30})
    assert "withheld" in md


def test_track_record_shown_when_sufficient():
    sc = {"sample_status": "sufficient", "primary_horizon": "4w",
          "matured_prediction_count": 184, "benchmark_relative_hit_rate": 0.576,
          "precision_at_3": 0.611, "avg_excess_return": 0.0128,
          "top_bottom_score_spread": 0.031, "information_coefficient": 0.07}
    md = R.render_weekly_md(_payload(), scorecard=sc)
    assert "Matured predictions: 184" in md
    assert "57.6%" in md


# --------------------------------------------------------------------------- #
# emailer
# --------------------------------------------------------------------------- #
_TRANSPORT = {
    "MEMO_EMAIL_SMTP_HOST": "smtp.example.com", "MEMO_EMAIL_USERNAME": "u",
    "MEMO_EMAIL_PASSWORD": "p", "MEMO_EMAIL_FROM": "from@example.com",
    "WEEKLY_ETF_BUNDLES_EMAIL_TO": "ops@example.com",
}


def test_disabled_never_calls_smtp(tmp_path):
    env = dict(_TRANSPORT, WEEKLY_ETF_BUNDLES_EMAIL_ENABLED="false")
    with patch("smtplib.SMTP") as mock_smtp:
        res = EM.send_weekly_etf_bundle_email(analysis_payload=_payload(), root=tmp_path, env=env)
        mock_smtp.assert_not_called()
    assert res["reason"] == "disabled"
    assert res["sent"] is False


def test_dry_run_never_calls_smtp(tmp_path):
    env = dict(_TRANSPORT, WEEKLY_ETF_BUNDLES_EMAIL_ENABLED="true",
               WEEKLY_ETF_BUNDLES_EMAIL_DRY_RUN="true")
    with patch("smtplib.SMTP") as mock_smtp:
        res = EM.send_weekly_etf_bundle_email(analysis_payload=_payload(), root=tmp_path, env=env)
        mock_smtp.assert_not_called()
    assert res["dry_run"] is True
    assert res["sent"] is False


def test_real_send_then_duplicate_suppressed_then_force(tmp_path):
    env = dict(_TRANSPORT, WEEKLY_ETF_BUNDLES_EMAIL_ENABLED="true",
               WEEKLY_ETF_BUNDLES_EMAIL_DRY_RUN="false")
    calls = {"n": 0}

    def fake_sender(cfg, msg):
        calls["n"] += 1
        return {"sent": True, "dry_run": False}

    r1 = EM.send_weekly_etf_bundle_email(analysis_payload=_payload(), root=tmp_path, env=env, sender=fake_sender)
    assert r1["sent"] is True and calls["n"] == 1
    r2 = EM.send_weekly_etf_bundle_email(analysis_payload=_payload(), root=tmp_path, env=env, sender=fake_sender)
    assert r2["reason"] == "duplicate_suppressed" and calls["n"] == 1   # not re-sent
    r3 = EM.send_weekly_etf_bundle_email(analysis_payload=_payload(), root=tmp_path, env=env,
                                         sender=fake_sender, force=True)
    assert r3["sent"] is True and calls["n"] == 2                       # forced resend


def test_receipt_written(tmp_path):
    env = dict(_TRANSPORT, WEEKLY_ETF_BUNDLES_EMAIL_ENABLED="true",
               WEEKLY_ETF_BUNDLES_EMAIL_DRY_RUN="true")
    EM.send_weekly_etf_bundle_email(analysis_payload=_payload(), root=tmp_path, env=env)
    receipt = tmp_path / "outputs" / "weekly_etf_bundles" / "email_receipt.json"
    assert receipt.exists()
    doc = json.loads(receipt.read_text())
    assert doc["message_type"] == "weekly_etf_bundle_watchlist"
    assert doc["observe_only"] is True
