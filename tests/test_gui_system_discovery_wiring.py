"""GUI backlog — surface pipeline_wiring_status + discovery funnel on System tab.

Two dev/ops artifacts had no GUI consumer:
  - pipeline_wiring_status.json: a producer→consumer wiring audit (healthy /
    unwired / idle / not-audited counts).
  - discovery_pulse_status.json (+ theme_signals / watch_candidates): the
    universe-discovery funnel — budget caps/usage + tier funnel counts.
Both become System-tab cards.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path


def _system(files: dict[str, dict]) -> dict:
    from gui_v2.data.dash_system import collect_system_view

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        latest = root / "outputs" / "latest"
        latest.mkdir(parents=True)
        for name, data in files.items():
            (latest / name).write_text(json.dumps(data), encoding="utf-8")
        return collect_system_view(root)


def _card(view, title):
    return next((c for c in view["cards"] if c["title"] == title), None)


def test_pipeline_wiring_card_healthy():
    v = _system({"pipeline_wiring_status.json": {
        "generated_at": "2026-07-09T09:00:00", "observe_only": True,
        "overall_status": "green",
        "summary": {"total_audited": 111, "healthy": 99, "unwired": 0,
                    "event_log_idle": 11, "not_audited": 18, "disabled": 1},
    }})
    c = _card(v, "Pipeline Wiring")
    assert c is not None
    assert c["severity"] == "green"
    assert "99" in c["summary"] and "111" in c["summary"]
    assert "0 unwired" in c["summary"]


def test_pipeline_wiring_card_red_when_unwired():
    v = _system({"pipeline_wiring_status.json": {
        "overall_status": "red",
        "summary": {"total_audited": 100, "healthy": 90, "unwired": 5},
    }})
    assert _card(v, "Pipeline Wiring")["severity"] == "red"


def test_discovery_pulse_card():
    v = _system({"discovery_pulse_status.json": {
        "generated_at": "2026-07-09T15:00:00", "observe_only": True,
        "month": "2026-07",
        "caps": {"fmp_calls_max": 5000, "openai_cost_usd_max": 20.0},
        "usage": {"fmp_calls_month": 31, "openai_cost_usd_month": 0.0},
        "tier_a": {"themes_count": 5, "watch_candidates_count": 10},
        "skipped": False,
    }})
    c = _card(v, "Discovery Pulse")
    assert c is not None
    assert "5 themes" in c["summary"]
    assert "10" in c["summary"]                     # watch candidates
    assert "31" in c["summary"] and "5000" in c["summary"]  # FMP usage/cap


def test_discovery_pulse_card_warning_when_skipped():
    v = _system({"discovery_pulse_status.json": {
        "skipped": True, "skip_reason": "budget exhausted",
        "caps": {}, "usage": {}, "tier_a": {},
    }})
    assert _card(v, "Discovery Pulse")["severity"] in ("yellow", "gray")


def test_cards_absent_when_artifacts_missing():
    v = _system({})
    # graceful: either omitted or an explicit info/empty card — never a crash
    wc = _card(v, "Pipeline Wiring")
    dc = _card(v, "Discovery Pulse")
    assert (wc is None or wc["status"] in ("info", "unknown"))
    assert (dc is None or dc["status"] in ("info", "unknown"))
