"""Task 3 (Milestone 3): /dashboard/quant — caution-labeled quant evidence view.

Tests:
  - collect_quant_view returns expected card titles and structure
  - every card has non-empty source_artifacts
  - route renders 200 with observe-only banner
  - thin-sample fixture (pattern_efficacy with snapshots_consumed < 30) yields
    "Thin sample" or "Insufficient history" label
  - gate/retune proposals yield "Proposal only" label
  - quant_watch_status present yields "Observe only" label
  - calibration with insufficient data yields "Insufficient history"
  - empty states when all artifacts absent (tmp_path with no files)
  - no buy/sell/hold verbs in any card summary or label
  - no forbidden action labels (belt-and-suspenders)
  - mobile card stacks present (md:hidden equivalent)
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Expected card titles
# ---------------------------------------------------------------------------

EXPECTED_CARD_TITLES = {
    "Confidence Calibration",
    "Pattern Efficacy (Weekly)",
    "Pattern Efficacy (Monthly)",
    "Pattern Efficacy (Yearly)",
    "Retune Impact",
    "Gate / Retune Suggestions",
    "Alpha Attribution",
    "Quant Watch",
    "Kelly Sizing (Advisory)",
}

_FORBIDDEN_LABELS = (
    "execute trade",
    "buy now",
    "sell now",
    "place order",
    "auto-trade",
    "auto trade",
    "auto-approve",
)

# Verbs that must NOT appear in quant card summaries/labels
# (quant cards are evidence only — no advisory actions)
_TRADE_VERBS = re.compile(r"\b(buy|sell|hold|execute|trade)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_latest(tmp_path: Path) -> Path:
    d = tmp_path / "outputs" / "latest"
    d.mkdir(parents=True)
    return d


def _write(directory: Path, filename: str, data: dict) -> None:
    (directory / filename).write_text(json.dumps(data), encoding="utf-8")


def _make_calibration(latest: Path, total_resolved: int = 100) -> None:
    _write(latest, "confidence_calibration.json", {
        "generated_at": "2026-06-08T00:00:00",
        "observe_only": True,
        "insufficient_data": total_resolved < 20,
        "total_resolved": total_resolved,
        "min_required": 20,
        "overall_hit_rate": 0.5,
        "overall_calibration_gap": 0.1,
        "summary_line": f"{total_resolved} resolved decisions.",
    })


def _make_efficacy(latest: Path, period: str, snapshots_consumed: int = 50,
                   rows_matched: int = 40, lookback_days: int = 7) -> None:
    fname = f"pattern_efficacy_{period}.json"
    _write(latest, fname, {
        "generated_at": "2026-06-08T00:00:00",
        "observe_only": True,
        "schema_version": "1.0",
        "source": "pattern_learning",
        "lookback_days": lookback_days,
        "snapshots_consumed": snapshots_consumed,
        "rows_consumed": rows_matched + 10,
        "rows_matched_to_outcomes": rows_matched,
        "match_rate": rows_matched / (rows_matched + 10),
        "universe_baseline": 0.5,
        "by_tag": {},
        "partitioned_by_fingerprint_regime": {},
        "thresholds": {},
        "disclaimer": "Observe-only.",
    })


def _make_retune_impact(latest: Path, history_size: int = 5) -> None:
    _write(latest, "retune_impact.json", {
        "generated_at": "2026-06-08T00:00:00",
        "observe_only": True,
        "schema_version": "1.0",
        "source": "retune_tracker",
        "baseline_label": "pre_retune",
        "changes_count": 3,
        "history_size": history_size,
        "outcome_attribution": {"available": False},
        "disclaimer": "Observe-only.",
    })


def _make_gate_suggestions(latest: Path, available: bool = True,
                            n_samples: int = 50) -> None:
    proposals = []
    if available:
        proposals.append({
            "parameter": "sanitation_weight.theme",
            "source_tag": "source:theme_candidate",
            "current_value": 0.3,
            "proposed_value": 0.27,
            "delta": -0.03,
            "n_samples": n_samples,
            "evidence_delta_pp": -6.0,
            "significance": "loser",
            "auto_applicable": False,
            "rationale": "Proposal rationale.",
        })
    _write(latest, "gate_retune_suggestions.json", {
        "generated_at": "2026-06-08T00:00:00",
        "observe_only": True,
        "schema_version": "1.0",
        "source": "gate_retune",
        "available": available,
        "based_on_efficacy_generated_at": "2026-06-08T00:00:00",
        "based_on_lookback_days": 7,
        "universe_baseline_n": n_samples,
        "weight_proposals": proposals,
        "gate_proposal": {
            "parameter": "extended_watchlist.confidence_threshold",
            "current_value": 0.8,
            "proposed_value": 0.8,
            "delta": 0.0,
            "auto_applicable": False,
            "rationale": "No change recommended.",
        },
        "auto_applicable_count": 0,
        "guardrails": {},
        "disclaimer": "Observe-only.",
    })


def _make_alpha_attribution(latest: Path, sufficient_sources: int = 2) -> None:
    by_source: dict = {}
    for i in range(3):
        n_returns = 25 if i < sufficient_sources else 5
        by_source[f"source_{i}"] = {
            "n_returns": n_returns,
            "sharpe": 0.5,
            "mean_return": 0.01,
        }
    _write(latest, "alpha_attribution_report.json", {
        "generated_at": "2026-06-08T00:00:00",
        "observe_only": True,
        "schema_version": "1.0",
        "summary_line": f"Alpha attribution: {sufficient_sources}/3 sources have sufficient data",
        "min_n_required": 20,
        "by_source": by_source,
        "best_sharpe_source": "source_0",
        "worst_sharpe_source": "source_2",
        "notes": [],
        "advisory_disclaimer": "Observe-only.",
    })


def _make_quant_watch(latest: Path, overall_status: str = "amber",
                      active_count: int = 2) -> None:
    _write(latest, "quant_watch_status.json", {
        "generated_at": "2026-06-08T00:00:00",
        "observe_only": True,
        "schema_version": "1.0",
        "source": "quant_watch",
        "overall_status": overall_status,
        "active_count": active_count,
        "active": [],
        "registered_today": 0,
        "resolved_today": 0,
        "escalated_today": 0,
        "ledger_liveness": {},
        "disclaimer": "Observe-only.",
    })


def _make_kelly(latest: Path, sufficient_groups: int = 2) -> None:
    by_decision: dict = {}
    for i in range(3):
        n_resolved = 25 if i < sufficient_groups else 5
        by_decision[f"group_{i}"] = {"n_resolved": n_resolved, "kelly_fraction": 0.1}
    _write(latest, "kelly_sizing_advisor.json", {
        "generated_at": "2026-06-08T00:00:00",
        "observe_only": True,
        "schema_version": "1.0",
        "min_resolved_required": 20,
        "half_kelly": True,
        "summary_line": f"Kelly sizing: {sufficient_groups}/3 decision groups have sufficient data",
        "by_decision": by_decision,
        "notes": [],
        "advisory_disclaimer": "Observe-only.",
    })


def _make_all_artifacts(latest: Path) -> None:
    _make_calibration(latest)
    for period in ("weekly", "monthly", "yearly"):
        _make_efficacy(latest, period)
    _make_retune_impact(latest)
    _make_gate_suggestions(latest)
    _make_alpha_attribution(latest)
    _make_quant_watch(latest)
    _make_kelly(latest)


# ---------------------------------------------------------------------------
# Unit tests: collect_quant_view
# ---------------------------------------------------------------------------


def test_quant_view_has_all_expected_card_titles(tmp_path):
    """All nine card domains are present even with no artifacts."""
    from gui_v2.data.dash_quant import collect_quant_view

    _make_latest(tmp_path)
    v = collect_quant_view(tmp_path)
    titles = {c["title"] for c in v["cards"]}
    assert EXPECTED_CARD_TITLES <= titles, f"Missing cards: {EXPECTED_CARD_TITLES - titles}"


def test_every_card_has_non_empty_source_artifacts(tmp_path):
    """source_artifacts must be non-empty for every card."""
    from gui_v2.data.dash_quant import collect_quant_view

    _make_latest(tmp_path)
    v = collect_quant_view(tmp_path)
    bad = [c["title"] for c in v["cards"] if not c.get("source_artifacts")]
    assert bad == [], f"Cards missing source_artifacts: {bad}"


def test_quant_view_persona_field(tmp_path):
    from gui_v2.data.dash_quant import collect_quant_view

    _make_latest(tmp_path)
    v = collect_quant_view(tmp_path)
    assert v["persona"] == "quant"


def test_quant_view_observe_only_flag(tmp_path):
    from gui_v2.data.dash_quant import collect_quant_view

    _make_latest(tmp_path)
    v = collect_quant_view(tmp_path)
    assert v.get("observe_only") is True


# ---------------------------------------------------------------------------
# Thin-sample / insufficient history labels
# ---------------------------------------------------------------------------


def test_thin_sample_efficacy_yields_caution_label(tmp_path):
    """pattern_efficacy with snapshots_consumed < 30 must yield thin-sample or
    insufficient-history label."""
    from gui_v2.data.dash_quant import collect_quant_view

    latest = _make_latest(tmp_path)
    # Use snapshots_consumed=5 (< 10 → Insufficient history) and 15 (< 30 → Thin sample)
    for snapshots, expected_fragment in ((5, "Insufficient history"), (15, "Thin sample")):
        # Fresh tmp dir for each sub-case
        sub = tmp_path / f"case_{snapshots}"
        sub_latest = sub / "outputs" / "latest"
        sub_latest.mkdir(parents=True)
        _make_efficacy(sub_latest, "weekly", snapshots_consumed=snapshots)
        v = collect_quant_view(sub)
        weekly_card = next(
            (c for c in v["cards"] if c["title"] == "Pattern Efficacy (Weekly)"), None
        )
        assert weekly_card is not None
        assert expected_fragment in weekly_card["label"], (
            f"Expected '{expected_fragment}' in label for snapshots={snapshots}, "
            f"got: {weekly_card['label']!r}"
        )


def test_calibration_insufficient_data_yields_insufficient_history(tmp_path):
    """confidence_calibration with total_resolved < 10 → 'Insufficient history'."""
    from gui_v2.data.dash_quant import collect_quant_view

    latest = _make_latest(tmp_path)
    _make_calibration(latest, total_resolved=5)
    v = collect_quant_view(tmp_path)
    calib_card = next(c for c in v["cards"] if c["title"] == "Confidence Calibration")
    assert "Insufficient history" in calib_card["label"], (
        f"Expected 'Insufficient history', got: {calib_card['label']!r}"
    )


def test_calibration_thin_sample_yields_thin_sample(tmp_path):
    """confidence_calibration with 10 <= total_resolved < 30 → 'Thin sample'."""
    from gui_v2.data.dash_quant import collect_quant_view

    latest = _make_latest(tmp_path)
    _make_calibration(latest, total_resolved=20)
    v = collect_quant_view(tmp_path)
    calib_card = next(c for c in v["cards"] if c["title"] == "Confidence Calibration")
    assert "Thin sample" in calib_card["label"], (
        f"Expected 'Thin sample', got: {calib_card['label']!r}"
    )


def test_calibration_absent_yields_insufficient_history(tmp_path):
    """Missing confidence_calibration.json → 'Insufficient history' label."""
    from gui_v2.data.dash_quant import collect_quant_view

    _make_latest(tmp_path)
    v = collect_quant_view(tmp_path)
    calib_card = next(c for c in v["cards"] if c["title"] == "Confidence Calibration")
    assert "Insufficient history" in calib_card["label"]


# ---------------------------------------------------------------------------
# Proposal only labels
# ---------------------------------------------------------------------------


def test_gate_suggestions_available_yields_proposal_only(tmp_path):
    """gate_retune_suggestions available=True → 'Proposal only'."""
    from gui_v2.data.dash_quant import collect_quant_view

    latest = _make_latest(tmp_path)
    _make_gate_suggestions(latest, available=True, n_samples=50)
    v = collect_quant_view(tmp_path)
    gate_card = next(c for c in v["cards"] if c["title"] == "Gate / Retune Suggestions")
    assert "Proposal only" in gate_card["label"], (
        f"Expected 'Proposal only', got: {gate_card['label']!r}"
    )


def test_gate_suggestions_unavailable_yields_insufficient_history(tmp_path):
    """gate_retune_suggestions available=False → 'Insufficient history'."""
    from gui_v2.data.dash_quant import collect_quant_view

    latest = _make_latest(tmp_path)
    _make_gate_suggestions(latest, available=False)
    v = collect_quant_view(tmp_path)
    gate_card = next(c for c in v["cards"] if c["title"] == "Gate / Retune Suggestions")
    assert "Insufficient history" in gate_card["label"]


def test_kelly_sufficient_data_yields_proposal_only(tmp_path):
    """kelly_sizing_advisor with sufficient groups → 'Proposal only'."""
    from gui_v2.data.dash_quant import collect_quant_view

    latest = _make_latest(tmp_path)
    _make_kelly(latest, sufficient_groups=3)
    v = collect_quant_view(tmp_path)
    kelly_card = next(c for c in v["cards"] if c["title"] == "Kelly Sizing (Advisory)")
    assert "Proposal only" in kelly_card["label"], (
        f"Expected 'Proposal only', got: {kelly_card['label']!r}"
    )


# ---------------------------------------------------------------------------
# Observe only labels
# ---------------------------------------------------------------------------


def test_quant_watch_present_yields_observe_only(tmp_path):
    """quant_watch_status present → 'Observe only' label."""
    from gui_v2.data.dash_quant import collect_quant_view

    latest = _make_latest(tmp_path)
    _make_quant_watch(latest, overall_status="amber", active_count=2)
    v = collect_quant_view(tmp_path)
    qwatch_card = next(c for c in v["cards"] if c["title"] == "Quant Watch")
    assert "Observe only" in qwatch_card["label"], (
        f"Expected 'Observe only', got: {qwatch_card['label']!r}"
    )


def test_quant_watch_absent_yields_observe_only(tmp_path):
    """quant_watch_status absent → 'Observe only' label (explicit empty state)."""
    from gui_v2.data.dash_quant import collect_quant_view

    _make_latest(tmp_path)
    v = collect_quant_view(tmp_path)
    qwatch_card = next(c for c in v["cards"] if c["title"] == "Quant Watch")
    assert "Observe only" in qwatch_card["label"]


def test_retune_impact_yields_observe_only_when_sufficient(tmp_path):
    """retune_impact with sufficient history_size → 'Observe only'."""
    from gui_v2.data.dash_quant import collect_quant_view

    latest = _make_latest(tmp_path)
    _make_retune_impact(latest, history_size=50)
    v = collect_quant_view(tmp_path)
    retune_card = next(c for c in v["cards"] if c["title"] == "Retune Impact")
    assert "Observe only" in retune_card["label"], (
        f"Expected 'Observe only', got: {retune_card['label']!r}"
    )


# ---------------------------------------------------------------------------
# No forbidden / buy/sell/hold verbs
# ---------------------------------------------------------------------------


def test_no_buy_sell_hold_in_card_summaries_or_labels(tmp_path):
    """No buy/sell/hold/execute/trade verbs in any quant card summary or label."""
    from gui_v2.data.dash_quant import collect_quant_view

    latest = _make_latest(tmp_path)
    _make_all_artifacts(latest)
    v = collect_quant_view(tmp_path)

    violations: list[str] = []
    for c in v["cards"]:
        for field in ("summary", "label"):
            text = c.get(field, "") or ""
            m = _TRADE_VERBS.search(text)
            if m:
                violations.append(
                    f"Card '{c['title']}' {field}={text!r} contains forbidden verb '{m.group()}'"
                )
    assert violations == [], f"Forbidden verbs found:\n" + "\n".join(violations)


def test_no_buy_sell_hold_in_card_summaries_empty_state(tmp_path):
    """No buy/sell/hold verbs in empty-state card summaries."""
    from gui_v2.data.dash_quant import collect_quant_view

    _make_latest(tmp_path)
    v = collect_quant_view(tmp_path)

    violations: list[str] = []
    for c in v["cards"]:
        for field in ("summary", "label"):
            text = c.get(field, "") or ""
            m = _TRADE_VERBS.search(text)
            if m:
                violations.append(
                    f"Card '{c['title']}' {field}={text!r} contains forbidden verb '{m.group()}'"
                )
    assert violations == [], f"Forbidden verbs found:\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# Efficacy rows
# ---------------------------------------------------------------------------


def test_efficacy_rows_present_when_artifacts_present(tmp_path):
    """efficacy_rows in view when pattern_efficacy artifacts are present."""
    from gui_v2.data.dash_quant import collect_quant_view

    latest = _make_latest(tmp_path)
    for period in ("weekly", "monthly", "yearly"):
        _make_efficacy(latest, period)
    v = collect_quant_view(tmp_path)
    assert len(v["efficacy_rows"]) == 3
    periods = {r["period"] for r in v["efficacy_rows"]}
    assert {"Weekly", "Monthly", "Yearly"} == periods


def test_efficacy_rows_empty_when_artifacts_absent(tmp_path):
    """efficacy_rows is empty when pattern_efficacy artifacts absent."""
    from gui_v2.data.dash_quant import collect_quant_view

    _make_latest(tmp_path)
    v = collect_quant_view(tmp_path)
    assert v["efficacy_rows"] == []


# ---------------------------------------------------------------------------
# Route / integration tests
# ---------------------------------------------------------------------------


def test_quant_route_renders_200():
    """GET /dashboard/quant returns 200."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/quant")
    assert r.status_code == 200


def test_quant_route_has_observe_only_banner():
    """Page contains the global observe-only banner."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/quant")
    assert r.status_code == 200
    assert "Observe-only" in r.text


def test_quant_route_has_persona_notice():
    """Page contains the quant evidence notice."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/quant")
    assert r.status_code == 200
    assert "observe/proposal-only evidence" in r.text.lower() or "observe-only" in r.text.lower()


def test_quant_route_no_forbidden_labels():
    """Rendered /dashboard/quant HTML must not contain forbidden action labels."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/quant")
    assert r.status_code == 200
    text = r.text.lower()
    offenders = [label for label in _FORBIDDEN_LABELS if label in text]
    assert offenders == [], f"Forbidden labels in /dashboard/quant: {offenders}"


def test_quant_route_mobile_card_stack_present():
    """Template has md:hidden mobile card stack sibling to the desktop table."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/quant")
    assert r.status_code == 200
    # The efficacy summary section has both a hidden md:block table and a md:hidden stack
    assert "hidden md:block" in r.text
    assert "md:hidden" in r.text


# ---------------------------------------------------------------------------
# Template grep: no forbidden labels in any template (belt-and-suspenders)
# ---------------------------------------------------------------------------


def test_no_forbidden_action_labels_in_quant_template():
    """quant.html must not contain forbidden action label strings."""
    template_path = Path("gui_v2/templates/dashboard/quant.html")
    text = template_path.read_text(encoding="utf-8").lower()
    offenders = [label for label in _FORBIDDEN_LABELS if label in text]
    assert offenders == [], f"Forbidden labels in quant.html: {offenders}"


# ---------------------------------------------------------------------------
# Calibration Trend card (history-aware + bucket annotation)
# ---------------------------------------------------------------------------

def _make_calibration_full(latest: Path, gap: float = 0.10,
                           buckets: list[dict] | None = None) -> None:
    """Latest calibration with overall gap and optional per-bucket gaps."""
    _write(latest, "confidence_calibration.json", {
        "generated_at": "2026-06-22T00:00:00",
        "observe_only": True,
        "insufficient_data": False,
        "total_resolved": 300,
        "min_required": 20,
        "overall_hit_rate": 0.45,
        "overall_average_confidence": 0.45 + gap,
        "overall_calibration_gap": gap,
        "buckets_5": buckets if buckets is not None else [],
        "summary_line": "300 resolved decisions.",
    })


def _make_calibration_history(tmp_path: Path, dated_gaps: dict[str, float]) -> None:
    """Write outputs/history/<date>/confidence_calibration.json snapshots."""
    for date, gap in dated_gaps.items():
        d = tmp_path / "outputs" / "history" / date
        d.mkdir(parents=True, exist_ok=True)
        _write(d, "confidence_calibration.json", {
            "generated_at": f"{date}T00:00:00",
            "observe_only": True,
            "total_resolved": 200,
            "overall_calibration_gap": gap,
        })


def _trend_card(view: dict) -> dict | None:
    return next((c for c in view["cards"] if c["title"] == "Calibration Trend"), None)


def test_calibration_trend_card_emitted(tmp_path):
    from gui_v2.data.dash_quant import collect_quant_view
    latest = _make_latest(tmp_path)
    _make_calibration_full(latest, gap=0.20)
    _make_calibration_history(tmp_path, {
        "2026-06-08": 0.30, "2026-06-15": 0.25, "2026-06-22": 0.20,
    })
    card = _trend_card(collect_quant_view(tmp_path))
    assert card is not None
    assert card["source_artifacts"]  # non-empty


def test_calibration_trend_improving_when_gap_shrinks(tmp_path):
    from gui_v2.data.dash_quant import collect_quant_view
    latest = _make_latest(tmp_path)
    _make_calibration_full(latest, gap=0.20)
    _make_calibration_history(tmp_path, {
        "2026-06-08": 0.35, "2026-06-15": 0.28, "2026-06-22": 0.20,
    })
    card = _trend_card(collect_quant_view(tmp_path))
    assert "Improving" in card["label"]


def test_calibration_trend_worsening_when_gap_grows(tmp_path):
    from gui_v2.data.dash_quant import collect_quant_view
    latest = _make_latest(tmp_path)
    _make_calibration_full(latest, gap=0.35)
    _make_calibration_history(tmp_path, {
        "2026-06-08": 0.15, "2026-06-15": 0.25, "2026-06-22": 0.35,
    })
    card = _trend_card(collect_quant_view(tmp_path))
    assert "Worsening" in card["label"]


def test_calibration_trend_annotates_overconfident_buckets(tmp_path):
    from gui_v2.data.dash_quant import collect_quant_view
    latest = _make_latest(tmp_path)
    _make_calibration_full(latest, gap=0.20, buckets=[
        {"label": "low", "count": 20, "calibration_gap": 0.02},
        {"label": "high", "count": 100, "calibration_gap": 0.30},
        {"label": "very_high", "count": 80, "calibration_gap": 0.40},
    ])
    _make_calibration_history(tmp_path, {"2026-06-22": 0.20})
    card = _trend_card(collect_quant_view(tmp_path))
    # overconfident buckets named in the summary
    assert "high" in card["summary"].lower()


def test_calibration_trend_insufficient_history(tmp_path):
    from gui_v2.data.dash_quant import collect_quant_view
    latest = _make_latest(tmp_path)
    _make_calibration_full(latest, gap=0.20)
    # no history snapshots written
    card = _trend_card(collect_quant_view(tmp_path))
    assert card is not None
    assert "Insufficient history" in card["label"]


def test_calibration_trend_no_trade_verbs(tmp_path):
    from gui_v2.data.dash_quant import collect_quant_view
    latest = _make_latest(tmp_path)
    _make_calibration_full(latest, gap=0.20, buckets=[
        {"label": "high", "count": 100, "calibration_gap": 0.30},
    ])
    _make_calibration_history(tmp_path, {
        "2026-06-15": 0.25, "2026-06-22": 0.20,
    })
    card = _trend_card(collect_quant_view(tmp_path))
    blob = f"{card['label']} {card['summary']}"
    assert not _TRADE_VERBS.search(blob), f"trade verb in trend card: {blob!r}"
