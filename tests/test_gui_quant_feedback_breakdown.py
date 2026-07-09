"""GUI Phase 2 — surface quant_feedback regime/crowd/strategy breakdown.

quant_feedback.json carries by_regime / by_crowd_state / by_strategy dicts
(bucket -> {n_samples, hit_rate, mean_return, unresolved, sample_sufficient}).
The quant tab previously read only fallback_rate/n_resolved. Phase 2 surfaces the
per-dimension breakdown as tables so the operator can see regime-conditional
performance (the neutral-regime sample concentration that ties to the
regime-classifier-collapse watch concern).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path


def _collect(watch_payload: dict) -> dict:
    from gui_v2.data.dash_quant import collect_quant_view

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        latest = root / "outputs" / "latest"
        latest.mkdir(parents=True)
        (latest / "quant_feedback.json").write_text(json.dumps(watch_payload), encoding="utf-8")
        return collect_quant_view(root)


_PAYLOAD = {
    "generated_at": "2026-07-09T09:06:00+00:00",
    "observe_only": True,
    "n_resolved_outcomes": 40,
    "n_context_records": 482,
    "by_regime": {
        "neutral": {"n_samples": 341, "unresolved": 341, "hit_rate": None,
                    "mean_return": None, "sample_sufficient": False},
        "high_volatility": {"n_samples": 141, "unresolved": 100, "hit_rate": 0.62,
                            "mean_return": 0.011, "sample_sufficient": True},
    },
    "by_crowd_state": {
        "unknown": {"n_samples": 482, "unresolved": 482, "hit_rate": None,
                    "mean_return": None, "sample_sufficient": False},
    },
    "by_strategy": {
        "production": {"n_samples": 482, "unresolved": 442, "hit_rate": 0.55,
                       "mean_return": 0.004, "sample_sufficient": True},
    },
}


def test_breakdowns_exposed_with_three_dimensions():
    v = _collect(_PAYLOAD)
    bd = v.get("quant_feedback_breakdowns")
    assert bd, "collect_quant_view must expose quant_feedback_breakdowns"
    dims = {d["dimension"] for d in bd}
    assert dims == {"By Regime", "By Crowd State", "By Strategy"}


def test_breakdown_rows_carry_metrics_and_sort_by_samples():
    v = _collect(_PAYLOAD)
    regime = next(d for d in v["quant_feedback_breakdowns"] if d["dimension"] == "By Regime")
    rows = regime["rows"]
    # neutral (341) sorts before high_volatility (141)
    assert [r["bucket"] for r in rows] == ["neutral", "high_volatility"]
    neutral = rows[0]
    assert neutral["n_samples"] == 341
    assert neutral["hit_rate"] is None          # unresolved → no hit rate yet
    hv = rows[1]
    assert abs(hv["hit_rate"] - 0.62) < 1e-9
    assert hv["sample_sufficient"] is True


def test_empty_breakdowns_when_dimensions_absent():
    v = _collect({"generated_at": "2026-07-09T00:00:00+00:00", "n_resolved_outcomes": 0})
    assert v.get("quant_feedback_breakdowns") == []


def test_breakdown_section_renders():
    from gui_v2.app import templates

    ctx = {
        "persona": "quant", "observe_only": True, "cards": [],
        "quant_feedback_breakdowns": [
            {"dimension": "By Regime", "rows": [
                {"bucket": "neutral", "n_samples": 341, "unresolved": 341,
                 "hit_rate": None, "mean_return": None, "sample_sufficient": False},
            ]},
        ],
    }
    html = templates.env.get_template("dashboard/quant.html").render(**ctx)
    assert "Regime / Crowd / Strategy Breakdown" in html
    assert "By Regime" in html
    assert "neutral" in html


def test_breakdown_mean_return_uses_percent_scale_not_double_scaled():
    """quant_feedback mean_return is ALREADY in percent units (producer converts
    decimal->percent). The table must NOT multiply by 100 again."""
    from gui_v2.app import templates

    ctx = {
        "persona": "quant", "observe_only": True, "cards": [],
        "quant_feedback_breakdowns": [
            {"dimension": "By Regime", "rows": [
                {"bucket": "high_volatility", "n_samples": 141, "unresolved": 0,
                 "hit_rate": 0.62, "mean_return": 1.5, "sample_sufficient": True},
            ]},
        ],
    }
    html = templates.env.get_template("dashboard/quant.html").render(**ctx)
    assert "+1.5%" in html            # 1.5 percent, rendered as-is
    assert "+150" not in html         # NOT double-scaled to +150.0%
    assert "62%" in html              # hit_rate fraction -> percent
