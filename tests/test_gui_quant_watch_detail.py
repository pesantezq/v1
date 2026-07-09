"""GUI Phase 2 — surface Quant Watch active-concern detail.

`quant_watch_status.json` carries an `active[]` list where each entry has a rich
`concern` narrative (e.g. the regime-classifier-neutral-collapse ledger entry).
The quant tab previously rendered only a one-line count ("2 active concerns"),
hiding the actual concerns from the operator. Phase 2 surfaces each active probe
as its own card in a dedicated section, built with the Phase-1 status_card
primitive.
"""

from __future__ import annotations

import json
from pathlib import Path


def _latest(tmp_path: Path) -> Path:
    d = tmp_path / "outputs" / "latest"
    d.mkdir(parents=True)
    return d


def _write_watch(latest: Path) -> None:
    (latest / "quant_watch_status.json").write_text(json.dumps({
        "generated_at": "2026-07-09T09:06:41+00:00",
        "observe_only": True,
        "schema_version": "1",
        "source": "quant_watch_probes",
        "overall_status": "amber",
        "active_count": 2,
        "active": [
            {
                "id": "manual:regime_classifier_neutral_collapse",
                "detector": "manual",
                "concern": "Regime classifier collapsed to a constant neutral bucket.",
                "severity": "amber",
                "age_days": 15,
                "last_observation": {"at": "2026-06-26T13:39:05+00:00"},
            },
            {
                "id": "sector_drag:Communication_Services",
                "detector": "sector_drag",
                "concern": "sector Communication_Services is a loser (-12.28pp vs baseline) at n=62",
                "severity": "amber",
                "age_days": 3,
                "last_observation": {"at": "2026-07-09T00:00:00+00:00"},
            },
        ],
    }), encoding="utf-8")


def test_active_probes_surfaced_as_cards():
    import tempfile
    from gui_v2.data.dash_quant import collect_quant_view

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_watch(_latest(root))
        v = collect_quant_view(root)

    active = v.get("quant_watch_active")
    assert active, "collect_quant_view must expose quant_watch_active"
    assert len(active) == 2

    # Each active probe becomes a card carrying its concern text + a real severity.
    concerns = {c["summary"] for c in active}
    assert any("regime classifier collapsed" in s.lower() for s in concerns)
    assert any("communication_services is a loser" in s.lower() for s in concerns)
    for c in active:
        assert c["severity"] == "yellow"  # amber → yellow → amber rail
        assert c["source_artifacts"] == ["quant_watch_status.json"]


def test_no_active_probes_yields_empty_list():
    import tempfile
    from gui_v2.data.dash_quant import collect_quant_view

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        latest = _latest(root)
        (latest / "quant_watch_status.json").write_text(json.dumps({
            "generated_at": "2026-07-09T09:06:41+00:00",
            "observe_only": True,
            "overall_status": "green",
            "active_count": 0,
            "active": [],
        }), encoding="utf-8")
        v = collect_quant_view(root)

    assert v.get("quant_watch_active") == []


def test_active_concerns_section_renders():
    from gui_v2.app import templates

    ctx = {
        "persona": "quant",
        "observe_only": True,
        "cards": [],
        "quant_watch_active": [
            {
                "title": "Regime Classifier Neutral Collapse",
                "status": "warning",
                "severity": "yellow",
                "label": "manual · 15d",
                "summary": "Regime classifier collapsed to a constant neutral bucket.",
                "source_artifacts": ["quant_watch_status.json"],
                "updated_at": "2026-06-26T13:39:05+00:00",
            }
        ],
    }
    html = templates.env.get_template("dashboard/quant.html").render(**ctx)
    assert "Active Quant Concerns" in html
    assert "Regime Classifier Neutral Collapse" in html
    assert "constant neutral bucket" in html
