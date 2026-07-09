"""GUI Phase 2 — surface the current-gauge attribution on the Retune Impact card.

The Retune Impact card previously showed only counts ("3 tracked changes, 5
versions"). retune_impact.json's outcome_attribution.by_fingerprint carries the
current gauge's actual 1d hit-rate / mean-return / resolved count — the raw
evidence the memo's verdict is built from. Phase 2 puts that evidence on the
card and explicitly defers the vs-prior-gauge comparison verdict to the memo
(the producer that owns the baseline-comparison logic).

Scale note: hit_rate_1d is a FRACTION (0.66 -> 66%); mean_return_1d is already a
PERCENT value (0.998 -> +1.00%). The two must not be scaled the same way.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path


def _retune_card(payload: dict) -> dict:
    from gui_v2.data.dash_quant import collect_quant_view

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        latest = root / "outputs" / "latest"
        latest.mkdir(parents=True)
        (latest / "retune_impact.json").write_text(json.dumps(payload), encoding="utf-8")
        v = collect_quant_view(root)
    return next(c for c in v["cards"] if c["title"] == "Retune Impact")


_PAYLOAD = {
    "generated_at": "2026-07-09T09:03:00+00:00",
    "observe_only": True,
    "changes_count": 3,
    "history_size": 5,
    "outcome_attribution": {
        "available": True,
        "fingerprint_count": 2,
        "total_signals": 1781,
        "by_fingerprint": {
            "old000011112222": {"last_signal_time": "2026-06-20T09:00:00",
                                 "hit_rate_1d": 0.40, "mean_return_1d": -0.5,
                                 "resolved_1d": 120},
            "5687885c755dd6c9": {"last_signal_time": "2026-07-09T09:02:53",
                                 "hit_rate_1d": 0.6614, "mean_return_1d": 0.998228,
                                 "resolved_1d": 316},
        },
    },
}


def test_card_surfaces_current_gauge_metrics_with_correct_scales():
    c = _retune_card(_PAYLOAD)
    s = c["summary"]
    assert "5687885c" in s                 # current gauge = latest last_signal_time
    assert "66.1%" in s                    # hit_rate fraction -> percent (memo-matching precision)
    assert "n=316" in s                    # resolved sample size
    assert "+1.00%" in s                   # mean_return already percent-valued
    # must NOT mis-scale mean_return as if it were a fraction
    assert "+100" not in s and "+99" not in s


def test_card_defers_verdict_to_memo():
    c = _retune_card(_PAYLOAD)
    assert "memo" in c["summary"].lower()


def test_card_without_attribution_keeps_counts_only():
    c = _retune_card({
        "generated_at": "2026-07-09T09:03:00+00:00",
        "changes_count": 3, "history_size": 5,
        "outcome_attribution": {"available": False},
    })
    assert "tracked changes" in c["summary"]
    assert "hit-rate" not in c["summary"].lower()
