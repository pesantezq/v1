"""
Phase 2 tests — frozen prediction ledger (immutable + idempotent) and forward
outcome maturation (no future leak, missing != miss). Hermetic: tmp_path root,
synthetic panels.
"""
from __future__ import annotations

from datetime import date, timedelta

from portfolio_automation.portfolio_sim.prices import PricePanel
from portfolio_automation.weekly_etf_bundles import predictions as P
from portfolio_automation.weekly_etf_bundles import outcomes as O


def _weekdays(start_iso: str, n: int) -> list[str]:
    d = date.fromisoformat(start_iso)
    out: list[str] = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _panel(series_by_symbol, dates) -> PricePanel:
    closes = {s: {dates[i]: float(p) for i, p in enumerate(ps)} for s, ps in series_by_symbol.items()}
    volumes = {s: {d: 1.0 for d in dates} for s in series_by_symbol}
    return PricePanel(closes, volumes, list(dates), [])


def _payload(watch=86):
    return {
        "status": "ok",
        "market_data_date": "2026-07-24",
        "generated_at": "2026-07-27T08:00:00Z",
        "strategy_id": "weekly_etf_bundle_v1",
        "model_version": "weekly_etf_v1",
        "config_version": "sha256:abc123",
        "bundles": [{
            "bundle_id": "ai", "members": [
                {"symbol": "SMH", "metrics": {"available": True, "price": 100.0}},
                {"symbol": "IGV", "metrics": {"available": True, "price": 50.0}},
            ],
        }],
        "ranking_global": [
            {"symbol": "SMH", "bundle_id": "ai", "benchmark": "QQQ", "watch_score": watch,
             "label": "leading", "rank_in_bundle": 1, "rank_global": 1,
             "expected_direction": "outperform", "components": {"momentum_4w": 88}},
            {"symbol": "IGV", "bundle_id": "ai", "benchmark": "QQQ", "watch_score": 60,
             "label": "mixed", "rank_in_bundle": 2, "rank_global": 2,
             "expected_direction": "neutral", "components": {}},
        ],
    }


# --------------------------------------------------------------------------- #
# predictions
# --------------------------------------------------------------------------- #
def test_prediction_id_stable_and_market_data_keyed():
    a = P.make_prediction_id("2026-07-24", "ai", "SMH")
    b = P.make_prediction_id("2026-07-24", "ai", "smh")
    assert a == b == "2026-07-24:ai:SMH"


def test_build_predictions_stores_versions_and_price():
    recs = P.build_predictions(_payload())
    smh = next(r for r in recs if r["symbol"] == "SMH")
    assert smh["prediction_id"] == "2026-07-24:ai:SMH"
    assert smh["market_data_date"] == "2026-07-24"
    assert smh["strategy_id"] == "weekly_etf_bundle_v1"
    assert smh["model_version"] == "weekly_etf_v1"
    assert smh["config_version"] == "sha256:abc123"
    assert smh["price_at_prediction"] == 100.0
    assert smh["observe_only"] is True


def test_freeze_is_idempotent(tmp_path):
    r1 = P.freeze_predictions(_payload(), root=tmp_path)
    assert r1["status"] == "frozen"
    r2 = P.freeze_predictions(_payload(), root=tmp_path)
    assert r2["status"] == "idempotent_skip"
    assert r1["content_hash"] == r2["content_hash"]


def test_freeze_conflict_never_overwrites(tmp_path):
    P.freeze_predictions(_payload(watch=86), root=tmp_path)
    # Same market_data_date but different rankings → conflict, original preserved.
    r = P.freeze_predictions(_payload(watch=42), root=tmp_path)
    assert r["status"] == "conflict"
    loaded = P.load_predictions_for_date(tmp_path, "2026-07-24")
    smh = next(x for x in loaded if x["symbol"] == "SMH")
    assert smh["watch_score"] == 86        # original, not overwritten


def test_generated_at_does_not_trigger_conflict(tmp_path):
    P.freeze_predictions(_payload(), root=tmp_path)
    p2 = _payload()
    p2["generated_at"] = "2026-07-27T23:59:00Z"   # different wall-clock, same data
    r = P.freeze_predictions(p2, root=tmp_path)
    assert r["status"] == "idempotent_skip"


def test_champion_and_challenger_lanes_separate(tmp_path):
    P.freeze_predictions(_payload(), root=tmp_path, lane="champion")
    P.freeze_predictions(_payload(), root=tmp_path, lane="challenger", strategy_variant="v2_momentum_heavy")
    champ = P.load_predictions_for_date(tmp_path, "2026-07-24", lane="champion")
    chall = P.load_predictions_for_date(tmp_path, "2026-07-24", lane="challenger", variant="v2_momentum_heavy")
    assert champ and chall
    assert P.list_prediction_dates(tmp_path) == ["2026-07-24"]   # challengers not counted as champion dates


# --------------------------------------------------------------------------- #
# outcomes
# --------------------------------------------------------------------------- #
def _pred(symbol="SMH", benchmark="QQQ", mdd="2026-01-05", price=100.0, rank=1):
    return {
        "prediction_id": f"{mdd}:ai:{symbol}", "market_data_date": mdd,
        "bundle_id": "ai", "symbol": symbol, "benchmark": benchmark,
        "watch_score": 86, "label": "leading", "rank_in_bundle": rank,
        "rank_global": rank, "strategy_variant": "weekly_etf_bundle_v1",
        "config_version": "sha256:abc123", "price_at_prediction": price,
    }


def test_mature_4w_strong_hit_and_relative_hit():
    dates = _weekdays("2026-01-05", 40)
    mdd = dates[0]
    # ETF +5% by 4w, benchmark flat.
    etf = [100.0] + [100.0 * (1 + 0.05 * min(i, 20) / 20) for i in range(1, 40)]
    bm = [100.0] * 40
    panel = _panel({"SMH": etf, "QQQ": bm}, dates)
    out = O.mature_prediction(_pred(mdd=mdd), panel, "4w", now_date=dates[-1])
    assert out["status"] == "matured"
    assert out["forward_return"] > 0.04
    assert out["excess_return"] > 0.02
    assert out["strong_hit"] is True
    assert out["directional_hit"] is True
    assert out["relative_hit"] is True
    assert out["miss"] is False
    assert out["max_favorable_excursion"] >= out["forward_return"] - 1e-9
    assert out["max_adverse_excursion"] <= out["max_favorable_excursion"]


def test_mature_pending_when_horizon_not_elapsed():
    dates = _weekdays("2026-01-05", 10)   # only ~2 weeks of data
    panel = _panel({"SMH": [100.0] * 10, "QQQ": [100.0] * 10}, dates)
    out = O.mature_prediction(_pred(mdd=dates[0]), panel, "4w", now_date=dates[-1])
    assert out["status"] == "pending"
    assert "miss" not in out                # NOT scored as a miss
    assert out.get("strong_hit") is None or "strong_hit" not in out


def test_missing_entry_price_is_unresolvable_not_miss():
    dates = _weekdays("2026-01-05", 40)
    panel = _panel({"SMH": [100.0] * 40, "QQQ": [100.0] * 40}, dates)
    pred = _pred(mdd=dates[0])
    pred["price_at_prediction"] = None
    out = O.mature_prediction(pred, panel, "4w", now_date=dates[-1])
    assert out["status"] == "unresolvable"
    assert "miss" not in out


def test_mature_miss_classification():
    dates = _weekdays("2026-01-05", 40)
    mdd = dates[0]
    # ETF -5% by 4w, benchmark flat → excess < -2% → miss.
    etf = [100.0] + [100.0 * (1 - 0.05 * min(i, 20) / 20) for i in range(1, 40)]
    panel = _panel({"SMH": etf, "QQQ": [100.0] * 40}, dates)
    out = O.mature_prediction(_pred(mdd=mdd), panel, "4w", now_date=dates[-1])
    assert out["status"] == "matured"
    assert out["miss"] is True
    assert out["directional_hit"] is False
    assert out["result_class"] == "miss"


def test_no_future_leak_in_outcome_window():
    dates = _weekdays("2026-01-05", 60)
    mdd = dates[0]
    # Flat through 4w, then a spike far AFTER the 4w horizon that must not count.
    etf = [100.0] * 25 + [500.0] * 35
    panel = _panel({"SMH": etf, "QQQ": [100.0] * 60}, dates)
    out = O.mature_prediction(_pred(mdd=mdd), panel, "4w", now_date=dates[-1])
    assert out["status"] == "matured"
    assert abs(out["forward_return"]) < 0.01   # spike beyond 4w is invisible


def test_toprank_beats_median():
    matured = [
        {"status": "matured", "bundle_id": "ai", "horizon": "4w", "symbol": "SMH",
         "rank_in_bundle": 1, "forward_return": 0.08},
        {"status": "matured", "bundle_id": "ai", "horizon": "4w", "symbol": "IGV",
         "rank_in_bundle": 2, "forward_return": 0.02},
        {"status": "matured", "bundle_id": "ai", "horizon": "4w", "symbol": "CLOU",
         "rank_in_bundle": 3, "forward_return": -0.01},
    ]
    res = O.mature_bundle_toprank(matured)[0]
    assert res["top_symbol"] == "SMH"
    assert res["top_beat_median"] is True
