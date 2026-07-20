"""Phase 15 tests — GUI institutional view + compact memo section.

Asserts the memo renders only when material, always states filing age + a
context-only disclaimer (never implies a live "just bought"), and the GUI view
is null-tolerant, display-only, feeds_decision_engine=false, and surfaces the
delayed/incomplete limitations.
"""

from __future__ import annotations

import json

from gui_v2.data.dash_institutional import collect_institutional_view
from portfolio_automation.institutional_intelligence.institutional_memo import (
    render_institutional_memo_lines,
)


def _artifact(records):
    return {"source": "institutional_intelligence", "records": records}


def _rec(symbol, state="moderate_accumulation", conf=0.7, eff=2.4, age=24, crowd=0.3):
    return {"symbol": symbol, "consensus_state": state, "consensus_confidence": conf,
            "effective_independent_managers": eff, "filing_age_days": age,
            "crowding_score": crowd, "warnings": []}


# --- memo ----------------------------------------------------------------

def test_memo_empty_when_no_artifact():
    assert render_institutional_memo_lines(None) == []
    assert render_institutional_memo_lines({"records": []}) == []


def test_memo_empty_when_no_material_records():
    # neutral / low-confidence -> not material -> no section
    art = _artifact([_rec("X", state="neutral"), _rec("Y", conf=0.3)])
    assert render_institutional_memo_lines(art) == []


def test_memo_renders_material_with_age_and_disclaimer():
    art = _artifact([_rec("BE", eff=2.4, age=24)])
    lines = render_institutional_memo_lines(art, markdown=True)
    text = "\n".join(lines)
    assert "Institutional context" in text
    assert "BE" in text and "moderate accumulation" in text
    assert "24 days old" in text                     # filing age always stated
    assert "effective independent managers" in text
    # honest disclaimer — never implies a live buy
    assert "no funded-action override" in text.lower()
    assert "evidence, not a live trade instruction" in text.lower()


def test_memo_caps_symbols():
    art = _artifact([_rec(f"S{i}", conf=0.9 - i * 0.01) for i in range(10)])
    lines = render_institutional_memo_lines(art, max_symbols=3)
    symbol_lines = [l for l in lines if l.startswith("- S")]
    assert len(symbol_lines) == 3


def test_memo_crowded_flags_caution():
    art = _artifact([_rec("BE", state="crowded_accumulation", crowd=0.8)])
    text = "\n".join(render_institutional_memo_lines(art))
    assert "caution" in text.lower()


# --- GUI view ------------------------------------------------------------

def test_gui_view_no_artifact_inert(tmp_path):
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    v = collect_institutional_view(tmp_path)
    assert v["has_data"] is False
    assert v["feeds_decision_engine"] is False and v["observe_only"] is True
    assert v["limitations"] and any("delayed" in s for s in v["limitations"])
    assert v["cards"]


def test_gui_view_with_artifact(tmp_path):
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (latest / "institutional_intelligence.json").write_text(
        json.dumps(_artifact([_rec("BE"), _rec("XOM", conf=0.8)])), encoding="utf-8")
    (latest / "institutional_intelligence_status.json").write_text(
        json.dumps({"overall_status": "ok", "symbols_covered": 2,
                    "stale_symbols": 0, "unresolved_symbols": 0,
                    "live_ingestion_ready": False}), encoding="utf-8")
    v = collect_institutional_view(tmp_path)
    assert v["has_data"] is True and v["overall_status"] == "ok"
    assert v["feeds_decision_engine"] is False
    syms = {row["symbol"] for row in v["consensus_rows"]}
    assert syms == {"BE", "XOM"}
    # filing age surfaced so the delay is explicit
    assert all("filing_age_days" in row for row in v["consensus_rows"])
    assert any("delayed" in s for s in v["limitations"])
