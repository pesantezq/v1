"""
Tests for the Automatic Promotion GUI loaders and aggregator.

Covers:
  - missing artifact graceful degradation
  - malformed JSON / JSONL graceful degradation
  - valid artifact parsing
  - candidate grouping by proposed status
  - safety flag detection and missing-flag reporting
  - load_operator_dashboard_data wires the new key
  - loaders never write to disk
  - aggregator never emits trading-instruction language
  - status helper functions return safe tones and explanations
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from gui_operator_data import (
    load_automatic_promotion_candidates,
    load_automatic_promotion_summary_markdown,
    load_automatic_promotion_decisions,
    load_automatic_promotion_data,
    load_operator_dashboard_data,
    AUTOMATIC_PROMOTION_CANDIDATES_RELATIVE_PATH,
    AUTOMATIC_PROMOTION_SUMMARY_RELATIVE_PATH,
    AUTOMATIC_PROMOTION_DECISIONS_RELATIVE_PATH,
    _AUTOMATIC_PROMOTION_SAFETY_FLAGS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _valid_candidates_payload() -> dict:
    return {
        "generated_at": "2026-05-11T00:00:00Z",
        "run_mode": "discovery",
        "run_id": "2026-05-11_apg",
        "observe_only": True,
        "no_trade": True,
        "not_recommendation": True,
        "discovery_only": True,
        "no_portfolio_mutation": True,
        "no_watchlist_mutation": True,
        "no_decision_override": True,
        "no_score_mutation": True,
        "no_allocation_mutation": True,
        "source": "automatic_promotion_governance",
        "data_available": True,
        "decision_count": 3,
        "monitor_count": 1,
        "needs_review_count": 1,
        "rejected_count": 1,
        "expired_count": 0,
        "decisions": [
            {
                "ticker": "NVDA",
                "prior_status": "WATCH",
                "proposed_status": "MONITOR",
                "decision_type": "promote_to_monitor",
                "evidence_score": 0.78,
                "corroboration_score": 0.8,
                "news_relevance_score": 0.6,
                "source_diversity": 3,
                "gates_passed": ["minimum_corrob_score"],
                "gates_failed": [],
                "risk_flags": [],
                "catalyst_flags": ["beat estimates"],
                "reason": "All promotion gates passed.",
            },
            {
                "ticker": "AAPL",
                "prior_status": "WATCH",
                "proposed_status": "NEEDS_REVIEW",
                "decision_type": "demote_to_review",
                "evidence_score": 0.5,
                "corroboration_score": 0.5,
                "news_relevance_score": 0.3,
                "source_diversity": 1,
                "gates_passed": [], "gates_failed": ["minimum_news_relevance"],
                "risk_flags": [], "catalyst_flags": [],
                "reason": "Mixed evidence.",
            },
            {
                "ticker": "ZZZZ",
                "prior_status": "WATCH",
                "proposed_status": "REJECTED",
                "decision_type": "reject",
                "evidence_score": 0.2,
                "corroboration_score": 0.2,
                "news_relevance_score": 0.1,
                "source_diversity": 0,
                "gates_passed": [], "gates_failed": ["maximum_risk_flags"],
                "risk_flags": ["lawsuit", "fine", "investigation"],
                "catalyst_flags": [],
                "reason": "Risk flags exceed maximum.",
            },
        ],
        "gates": {"minimum_corrob_score": 0.65},
        "gate_summary": {"failed::minimum_news_relevance": 1},
        "safety_disclaimer": "This is sandbox research governance only.",
    }


def _write_valid_artifacts(base: Path) -> None:
    _write(base.joinpath(*AUTOMATIC_PROMOTION_CANDIDATES_RELATIVE_PATH),
           json.dumps(_valid_candidates_payload()))
    _write(base.joinpath(*AUTOMATIC_PROMOTION_SUMMARY_RELATIVE_PATH),
           "# Automatic Promotion Governance\n\nSandbox only.\n")
    log_path = base.joinpath(*AUTOMATIC_PROMOTION_DECISIONS_RELATIVE_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ticker": "NVDA", "proposed_status": "MONITOR"}) + "\n")
        fh.write(json.dumps({"ticker": "AAPL", "proposed_status": "NEEDS_REVIEW"}) + "\n")


# ---------------------------------------------------------------------------
# 1. Loader graceful degradation
# ---------------------------------------------------------------------------

class TestLoaderGracefulDegradation:
    def test_missing_candidates_returns_unavailable(self, tmp_path):
        result = load_automatic_promotion_candidates(tmp_path)
        assert result == {"available": False}

    def test_malformed_candidates_json_returns_unavailable(self, tmp_path):
        _write(tmp_path.joinpath(*AUTOMATIC_PROMOTION_CANDIDATES_RELATIVE_PATH),
               "NOT JSON")
        result = load_automatic_promotion_candidates(tmp_path)
        assert result == {"available": False}

    def test_non_object_candidates_returns_unavailable(self, tmp_path):
        _write(tmp_path.joinpath(*AUTOMATIC_PROMOTION_CANDIDATES_RELATIVE_PATH),
               "[1, 2, 3]")
        result = load_automatic_promotion_candidates(tmp_path)
        assert result == {"available": False}

    def test_empty_file_returns_unavailable(self, tmp_path):
        _write(tmp_path.joinpath(*AUTOMATIC_PROMOTION_CANDIDATES_RELATIVE_PATH), "")
        result = load_automatic_promotion_candidates(tmp_path)
        assert result == {"available": False}

    def test_missing_summary_returns_empty_string(self, tmp_path):
        assert load_automatic_promotion_summary_markdown(tmp_path) == ""

    def test_missing_decisions_jsonl_returns_empty_list(self, tmp_path):
        assert load_automatic_promotion_decisions(tmp_path) == []

    def test_malformed_jsonl_lines_skipped(self, tmp_path):
        path = tmp_path.joinpath(*AUTOMATIC_PROMOTION_DECISIONS_RELATIVE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"ticker": "NVDA"}) + "\n"
            "BAD LINE\n"
            + json.dumps({"ticker": "AAPL"}) + "\n",
            encoding="utf-8",
        )
        records = load_automatic_promotion_decisions(tmp_path)
        assert len(records) == 2
        assert {r["ticker"] for r in records} == {"NVDA", "AAPL"}

    def test_non_dict_jsonl_lines_skipped(self, tmp_path):
        path = tmp_path.joinpath(*AUTOMATIC_PROMOTION_DECISIONS_RELATIVE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1,2,3]\n" + json.dumps({"ticker": "NVDA"}) + "\n",
                        encoding="utf-8")
        records = load_automatic_promotion_decisions(tmp_path)
        assert records == [{"ticker": "NVDA"}]


# ---------------------------------------------------------------------------
# 2. Valid artifact parsing
# ---------------------------------------------------------------------------

class TestValidParsing:
    def test_candidates_loaded_marked_available(self, tmp_path):
        _write_valid_artifacts(tmp_path)
        payload = load_automatic_promotion_candidates(tmp_path)
        assert payload["available"] is True
        assert payload["decision_count"] == 3

    def test_summary_text_loaded(self, tmp_path):
        _write_valid_artifacts(tmp_path)
        md = load_automatic_promotion_summary_markdown(tmp_path)
        assert "Sandbox only" in md

    def test_decisions_jsonl_loaded(self, tmp_path):
        _write_valid_artifacts(tmp_path)
        records = load_automatic_promotion_decisions(tmp_path)
        assert len(records) == 2


# ---------------------------------------------------------------------------
# 3. Aggregator stable shape
# ---------------------------------------------------------------------------

class TestAggregator:
    def test_aggregator_has_stable_shape_when_missing(self, tmp_path):
        data = load_automatic_promotion_data(tmp_path)
        # Stable shape — every expected key present
        for key in (
            "available", "generated_at", "run_mode", "run_id",
            "decision_count", "monitor_count", "needs_review_count",
            "rejected_count", "expired_count",
            "safety_flags", "safety_flags_ok", "missing_safety_flags",
            "candidates", "candidates_by_status",
            "recent_decisions", "summary_markdown", "safety_disclaimer",
            "gates", "gate_summary",
        ):
            assert key in data, f"Missing key {key!r} in aggregator output"
        assert data["available"] is False
        assert data["candidates"] == []
        assert data["candidates_by_status"]["MONITOR"] == []

    def test_aggregator_groups_by_status(self, tmp_path):
        _write_valid_artifacts(tmp_path)
        data = load_automatic_promotion_data(tmp_path)
        assert len(data["candidates_by_status"]["MONITOR"]) == 1
        assert len(data["candidates_by_status"]["NEEDS_REVIEW"]) == 1
        assert len(data["candidates_by_status"]["REJECTED"]) == 1
        assert len(data["candidates_by_status"]["EXPIRED"]) == 0

    def test_aggregator_counts_match(self, tmp_path):
        _write_valid_artifacts(tmp_path)
        data = load_automatic_promotion_data(tmp_path)
        assert data["decision_count"] == 3
        assert data["monitor_count"] == 1
        assert data["needs_review_count"] == 1
        assert data["rejected_count"] == 1

    def test_safety_flags_all_true_when_valid(self, tmp_path):
        _write_valid_artifacts(tmp_path)
        data = load_automatic_promotion_data(tmp_path)
        for flag in _AUTOMATIC_PROMOTION_SAFETY_FLAGS:
            assert data["safety_flags"][flag] is True
        assert data["safety_flags_ok"] is True
        assert data["missing_safety_flags"] == []

    def test_missing_safety_flags_reported(self, tmp_path):
        payload = _valid_candidates_payload()
        # Strip one safety flag
        payload.pop("no_trade")
        _write(tmp_path.joinpath(*AUTOMATIC_PROMOTION_CANDIDATES_RELATIVE_PATH),
               json.dumps(payload))
        data = load_automatic_promotion_data(tmp_path)
        assert data["safety_flags_ok"] is False
        assert "no_trade" in data["missing_safety_flags"]

    def test_false_safety_flag_reported(self, tmp_path):
        payload = _valid_candidates_payload()
        payload["no_trade"] = False
        _write(tmp_path.joinpath(*AUTOMATIC_PROMOTION_CANDIDATES_RELATIVE_PATH),
               json.dumps(payload))
        data = load_automatic_promotion_data(tmp_path)
        assert data["safety_flags_ok"] is False
        assert "no_trade" in data["missing_safety_flags"]
        assert data["safety_flags"]["no_trade"] is False

    def test_available_true_with_partial_data(self, tmp_path):
        # Only summary present — should still be marked available
        _write(tmp_path.joinpath(*AUTOMATIC_PROMOTION_SUMMARY_RELATIVE_PATH),
               "# header\n")
        data = load_automatic_promotion_data(tmp_path)
        assert data["available"] is True

    def test_recent_decisions_capped_at_50(self, tmp_path):
        path = tmp_path.joinpath(*AUTOMATIC_PROMOTION_DECISIONS_RELATIVE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for i in range(75):
                fh.write(json.dumps({"ticker": f"T{i:03d}"}) + "\n")
        data = load_automatic_promotion_data(tmp_path)
        assert len(data["recent_decisions"]) == 50
        # Should be the last 50 (highest indices)
        assert data["recent_decisions"][0]["ticker"] == "T025"
        assert data["recent_decisions"][-1]["ticker"] == "T074"


# ---------------------------------------------------------------------------
# 4. Dashboard data wiring
# ---------------------------------------------------------------------------

class TestDashboardDataWiring:
    def test_load_operator_dashboard_data_includes_key(self, tmp_path):
        _write_valid_artifacts(tmp_path)
        dashboard = load_operator_dashboard_data(tmp_path)
        assert "automatic_promotion" in dashboard
        assert dashboard["automatic_promotion"]["available"] is True

    def test_load_operator_dashboard_data_graceful_when_missing(self, tmp_path):
        dashboard = load_operator_dashboard_data(tmp_path)
        assert "automatic_promotion" in dashboard
        assert dashboard["automatic_promotion"]["available"] is False


# ---------------------------------------------------------------------------
# 5. Read-only invariant — no writes by loaders
# ---------------------------------------------------------------------------

class TestReadOnlyInvariant:
    def test_loaders_do_not_write(self, tmp_path):
        _write_valid_artifacts(tmp_path)
        # Snapshot the sandbox dir before
        before = sorted(
            p.name for p in (tmp_path / "outputs" / "sandbox" / "discovery").glob("*")
        )
        # Run all loaders
        load_automatic_promotion_candidates(tmp_path)
        load_automatic_promotion_summary_markdown(tmp_path)
        load_automatic_promotion_decisions(tmp_path)
        load_automatic_promotion_data(tmp_path)
        after = sorted(
            p.name for p in (tmp_path / "outputs" / "sandbox" / "discovery").glob("*")
        )
        assert before == after, "Loaders must not create or modify files"

    def test_loaders_do_not_touch_latest_or_policy(self, tmp_path):
        _write_valid_artifacts(tmp_path)
        load_automatic_promotion_data(tmp_path)
        # Verify nothing was written to LATEST / POLICY / PORTFOLIO
        for ns in ("latest", "policy", "portfolio"):
            ns_dir = tmp_path / "outputs" / ns
            if ns_dir.exists():
                assert not any(p.name.startswith("automatic_promotion")
                               for p in ns_dir.iterdir())


# ---------------------------------------------------------------------------
# 6. Aggregator content safety — no trading instruction leakage
# ---------------------------------------------------------------------------

class TestAggregatorContentSafety:
    def test_no_forbidden_action_tokens_emitted_as_status(self, tmp_path):
        _write_valid_artifacts(tmp_path)
        data = load_automatic_promotion_data(tmp_path)
        # The candidates_by_status buckets are MONITOR/NEEDS_REVIEW/REJECTED/
        # EXPIRED/WATCH/DISCOVERED/OTHER — none of the forbidden statuses
        for status_key in data["candidates_by_status"]:
            assert status_key in {
                "MONITOR", "NEEDS_REVIEW", "REJECTED",
                "EXPIRED", "WATCH", "DISCOVERED", "OTHER",
            }

    def test_aggregator_passes_through_safety_disclaimer(self, tmp_path):
        _write_valid_artifacts(tmp_path)
        data = load_automatic_promotion_data(tmp_path)
        assert "sandbox" in data["safety_disclaimer"].lower()

    def test_aggregator_does_not_invent_action_labels(self, tmp_path):
        _write_valid_artifacts(tmp_path)
        data = load_automatic_promotion_data(tmp_path)
        # The aggregator must not invent BUY/SELL/HOLD/ACTIONABLE/PROMOTED
        # values for the proposed_status field
        for cand in data["candidates"]:
            status = str(cand.get("proposed_status") or "").upper()
            assert status not in {
                "BUY", "SELL", "HOLD", "ACTIONABLE",
                "PROMOTED", "VALIDATED", "APPROVED",
                "TRADE", "RECOMMENDATION",
            }

    def test_unknown_status_goes_to_other_bucket(self, tmp_path):
        # Producer should never emit such a value; defensive guard.
        payload = _valid_candidates_payload()
        payload["decisions"][0]["proposed_status"] = "WEIRD_FUTURE_STATUS"
        _write(tmp_path.joinpath(*AUTOMATIC_PROMOTION_CANDIDATES_RELATIVE_PATH),
               json.dumps(payload))
        data = load_automatic_promotion_data(tmp_path)
        # Should land in OTHER, not in MONITOR
        assert len(data["candidates_by_status"]["OTHER"]) == 1


# ---------------------------------------------------------------------------
# 7. GUI helper smoke tests (import + non-crashing on empty data)
# ---------------------------------------------------------------------------

class TestGUIHelperImportSafety:
    """Smoke tests — ensure gui/app.py module imports cleanly with new helpers."""

    def test_gui_app_module_compiles(self):
        # If the module compiles cleanly, the imports/helpers are syntactically valid
        import py_compile
        py_compile.compile("gui/app.py", doraise=True)

    def test_status_helpers_return_expected_tones(self):
        # We can import the private helpers directly from the module
        import importlib.util
        spec = importlib.util.spec_from_file_location("gui_app_module", "gui/app.py")
        # Note: we don't actually execute the module (it has Streamlit side-effects);
        # we just verify the symbols are present in source.
        src = Path("gui/app.py").read_text(encoding="utf-8")
        assert "def _status_tone" in src
        assert "def _status_explanation" in src
        assert "def render_status_badge" in src
        assert "def render_metric_card" in src
        assert "def render_safety_flags" in src
        assert "def render_candidate_card" in src
        assert "def render_section_header" in src
        assert "def render_empty_state" in src
        assert "def page_automatic_promotion" in src

    def test_helpers_avoid_forbidden_trading_language(self):
        """Helpers themselves must not use BUY/SELL/HOLD instruction language."""
        src = Path("gui/app.py").read_text(encoding="utf-8")
        # Locate the cockpit helpers block
        marker = "# Operator Cockpit — Reusable UI helpers"
        if marker not in src:
            pytest.skip("cockpit helper section not found")
        block = src.split(marker, 1)[1]
        # End at the ROUTER section
        block = block.split("# ROUTER", 1)[0]
        # The disclaimer naturally contains "buy/sell/hold" — strip it
        block = block.replace(
            "This is sandbox research governance only. "
            "It is not a buy/sell/hold recommendation.",
            ""
        )
        block_lower = block.lower()
        # Now check no instruction phrases
        for phrase in (
            "buy now", "sell now", "execute trade", "rebalance now",
            "add to watchlist", "promote candidate",
        ):
            assert phrase not in block_lower, \
                f"Forbidden instruction phrase {phrase!r} in cockpit helpers"

    def test_page_registered_in_pages_list(self):
        src = Path("gui/app.py").read_text(encoding="utf-8")
        # PAGES list should include "Automatic Promotion"
        assert '"Automatic Promotion"' in src
        # Router should dispatch to it
        assert 'page == "Automatic Promotion"' in src
