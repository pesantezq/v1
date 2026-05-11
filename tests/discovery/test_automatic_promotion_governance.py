"""
Tests for portfolio_automation/discovery/automatic_promotion_governance.py
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from portfolio_automation.discovery.automatic_promotion_governance import (
    PromotionGates,
    PromotionEligibilityResult,
    PromotionDecision,
    AutomaticPromotionReport,
    UnsafeAutomaticPromotionArtifactError,
    ALLOWED_STATUSES,
    FORBIDDEN_STATUSES,
    DEFAULT_GATES,
    load_automatic_promotion_inputs,
    evaluate_candidate_promotion,
    build_automatic_promotion_report,
    render_automatic_promotion_markdown,
    write_automatic_promotion_report,
    run_automatic_promotion_governance,
    validate_automatic_promotion_safety,
    sanitize_automatic_promotion_text,
    sanitize_label,
    sanitize_nested_automatic_promotion_payload,
    _SAFETY_DISCLAIMER,
    _DISCOVERY_DISCLAIMER,
    _PROHIBITED_INSTRUCTION_PATTERNS,
    _FORBIDDEN_STANDALONE_ACTIONS,
)
from portfolio_automation.run_mode_governance import RunMode, RunModeViolation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_sandbox(base: Path, relative: str, payload: dict) -> None:
    p = base / "sandbox" / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")


def _write_latest(base: Path, name: str, payload: dict) -> None:
    d = base / "latest"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(payload), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_disclaimers(text: str) -> str:
    return text.replace(_SAFETY_DISCLAIMER, "").replace(_DISCOVERY_DISCLAIMER, "")


def _watch_candidate(ticker="NVDA", corroboration=0.8) -> dict:
    return {
        "ticker": ticker,
        "status": "watch",
        "score": 0.8,
        "mention_count": 5,
        "unique_source_count": 3,
        "corroboration_score": corroboration,
        "corroboration_level": "moderate",
        "corroboration_met": True,
        "risk_flag": False,
        "first_seen": _now_iso(),
        "last_seen": _now_iso(),
    }


def _enriched_entry(ticker="NVDA",
                    news_relevance=0.6,
                    risk_flags=None,
                    catalyst_flags=None) -> dict:
    return {
        "ticker": ticker,
        "candidate_status": "watch",
        "news_context": "research_supported",
        "matched_news_count": 5,
        "source_diversity": 3,
        "matched_themes": ["ai_infrastructure"],
        "risk_flags": risk_flags or [],
        "catalyst_flags": catalyst_flags or ["beat estimates"],
        "news_relevance_score": news_relevance,
        "corroboration_news_score": 0.7,
    }


def _memory_entry(ticker="NVDA", runs=4, mentions=8, last_seen=None) -> dict:
    return {
        "ticker": ticker,
        "first_seen": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
        "last_seen": last_seen or _now_iso(),
        "mention_count": mentions,
        "source_count": 3,
        "seen_runs": runs,
        "status": "watch",
        "last_score": 0.8,
        "last_event_type": "earnings",
    }


def _write_full_qualifying_setup(base: Path, ticker="NVDA") -> None:
    """Write a candidate that should qualify for MONITOR."""
    _write_sandbox(base, "discovery/emerging_candidates.json", {
        "candidates": [_watch_candidate(ticker)],
    })
    _write_sandbox(base, "discovery/news_enriched_candidates.json", {
        "enriched_candidates": [_enriched_entry(ticker)],
    })
    _write_sandbox(base, "discovery/discovery_memory.json", {
        "entries": [_memory_entry(ticker)],
    })


_ADVERSARIAL_PHRASES = (
    "buy now",
    "sell now",
    "promote candidate",
    "actionable buy",
    "validated sell",
    "trim position",
    "rebalance now",
    "execute trade",
    "add to watchlist",
)


# ---------------------------------------------------------------------------
# 1. Safety constants — sanity
# ---------------------------------------------------------------------------

class TestSafetyConstants:
    def test_allowed_statuses_set(self):
        assert ALLOWED_STATUSES == {
            "DISCOVERED", "WATCH", "MONITOR",
            "REJECTED", "EXPIRED", "NEEDS_REVIEW",
        }

    def test_forbidden_statuses_set(self):
        assert "BUY" in FORBIDDEN_STATUSES
        assert "SELL" in FORBIDDEN_STATUSES
        assert "HOLD" in FORBIDDEN_STATUSES
        assert "ACTIONABLE" in FORBIDDEN_STATUSES
        assert "PROMOTED" in FORBIDDEN_STATUSES
        assert "VALIDATED" in FORBIDDEN_STATUSES
        assert "APPROVED" in FORBIDDEN_STATUSES
        assert "TRADE" in FORBIDDEN_STATUSES
        assert "RECOMMENDATION" in FORBIDDEN_STATUSES

    def test_allowed_and_forbidden_disjoint(self):
        assert ALLOWED_STATUSES.isdisjoint(FORBIDDEN_STATUSES)


# ---------------------------------------------------------------------------
# 2. Input loading
# ---------------------------------------------------------------------------

class TestInputLoading:
    def test_all_missing_degrades(self, tmp_path):
        inputs = load_automatic_promotion_inputs(tmp_path)
        for key, val in inputs.items():
            assert val["summary"].available is False

    def test_loads_valid_emerging(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/emerging_candidates.json", {
            "candidates": [_watch_candidate()]
        })
        inputs = load_automatic_promotion_inputs(tmp_path)
        assert inputs["emerging_candidates"]["summary"].available is True

    def test_malformed_json_degrades(self, tmp_path):
        (tmp_path / "sandbox" / "discovery").mkdir(parents=True)
        (tmp_path / "sandbox" / "discovery" / "emerging_candidates.json").write_text("NOT JSON")
        inputs = load_automatic_promotion_inputs(tmp_path)
        assert inputs["emerging_candidates"]["summary"].available is False

    def test_non_object_json_degrades(self, tmp_path):
        (tmp_path / "sandbox" / "discovery").mkdir(parents=True)
        (tmp_path / "sandbox" / "discovery" / "emerging_candidates.json").write_text("[1,2,3]")
        inputs = load_automatic_promotion_inputs(tmp_path)
        assert inputs["emerging_candidates"]["summary"].available is False

    def test_empty_file_degrades(self, tmp_path):
        (tmp_path / "sandbox" / "discovery").mkdir(parents=True)
        (tmp_path / "sandbox" / "discovery" / "emerging_candidates.json").write_text("")
        inputs = load_automatic_promotion_inputs(tmp_path)
        assert inputs["emerging_candidates"]["summary"].available is False

    def test_loads_approval_decisions_jsonl(self, tmp_path):
        path = tmp_path / "sandbox" / "discovery" / "approval_decisions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"ticker": "NVDA", "decision": "watch"}\n')
        inputs = load_automatic_promotion_inputs(tmp_path)
        assert inputs["approval_decisions"]["summary"].available is True


# ---------------------------------------------------------------------------
# 3. Sanitizer/validator
# ---------------------------------------------------------------------------

class TestSanitizationHelpers:
    def test_sanitize_label_pure_action_neutralized(self):
        # Pure action token becomes the neutral marker
        for token in ("BUY", "SELL", "HOLD", "ACTIONABLE", "PROMOTED", "VALIDATED"):
            assert sanitize_label(token) == "redacted_action_label_context_only"

    def test_sanitize_label_preserves_benign(self):
        assert sanitize_label("ai_infrastructure") == "ai_infrastructure"

    def test_sanitize_text_redacts_phrases(self):
        out = sanitize_automatic_promotion_text("Investors should buy now.")
        assert "buy now" not in out.lower()

    def test_sanitize_text_redacts_standalone(self):
        out = sanitize_automatic_promotion_text("Decision: BUY")
        assert re.search(r"\bBUY\b", out, re.IGNORECASE) is None

    def test_sanitize_text_preserves_disclaimer(self):
        out = sanitize_automatic_promotion_text(_SAFETY_DISCLAIMER)
        assert _SAFETY_DISCLAIMER in out

    def test_sanitize_text_preserves_substring(self):
        # "buyer" should not be redacted (substring of legitimate word)
        assert "buyer" in sanitize_automatic_promotion_text("Major buyer in tech")

    def test_validator_detects_buy(self):
        assert "BUY" in validate_automatic_promotion_safety("BUY")

    def test_validator_walks_dataclass(self):
        report = AutomaticPromotionReport(
            generated_at="2026-05-11T00:00:00Z",
            run_mode="discovery",
            run_id="t",
            decisions=[],
        )
        report.gate_summary = {"failed::buy now": 1}  # adversarial inject
        assert "buy now" in validate_automatic_promotion_safety(report)

    def test_validator_allows_safety_disclaimer(self):
        assert validate_automatic_promotion_safety(_SAFETY_DISCLAIMER) == []

    def test_validator_allows_discovery_disclaimer(self):
        assert validate_automatic_promotion_safety(_DISCOVERY_DISCLAIMER) == []

    def test_sanitize_nested_payload(self):
        payload = {
            "themes": ["buy now", "ai_infrastructure"],
            "label": "PROMOTED",
            "nested": {"x": "execute trade"},
            "count": 5,
        }
        clean = sanitize_nested_automatic_promotion_payload(payload)
        assert validate_automatic_promotion_safety(clean) == []
        assert clean["count"] == 5
        assert "ai_infrastructure" in clean["themes"]


# ---------------------------------------------------------------------------
# 4. Per-candidate eligibility evaluator
# ---------------------------------------------------------------------------

class TestEligibility:
    def test_watch_candidate_qualifies_for_monitor(self):
        cand = _watch_candidate()
        ctx = {
            "enriched": _enriched_entry(),
            "memory": _memory_entry(),
            "rejected": False,
            "approvals": [],
            "replay": {},
        }
        result = evaluate_candidate_promotion(cand, ctx)
        assert result.proposed_status == "MONITOR"
        assert result.decision_type == "promote_to_monitor"
        assert result.eligible_for_monitor is True

    def test_low_evidence_becomes_needs_review(self):
        cand = _watch_candidate(corroboration=0.4)  # below threshold
        ctx = {
            "enriched": _enriched_entry(news_relevance=0.2),
            "memory": _memory_entry(runs=1, mentions=1),
            "rejected": False, "approvals": [], "replay": {},
        }
        result = evaluate_candidate_promotion(cand, ctx)
        # Multiple gates fail but evidence partially present → NEEDS_REVIEW
        assert result.proposed_status in ("NEEDS_REVIEW", "WATCH")
        assert result.decision_type != "promote_to_monitor"

    def test_rejected_candidate_cannot_become_monitor(self):
        cand = _watch_candidate()
        ctx = {"enriched": _enriched_entry(), "memory": _memory_entry(),
               "rejected": True, "approvals": [], "replay": {}}
        result = evaluate_candidate_promotion(cand, ctx)
        assert result.proposed_status == "REJECTED"
        assert result.decision_type == "reject"

    def test_high_risk_rejected(self):
        cand = _watch_candidate()
        ctx = {
            "enriched": _enriched_entry(
                risk_flags=["fraud", "lawsuit", "fine", "scandal"]
            ),
            "memory": _memory_entry(),
            "rejected": False, "approvals": [], "replay": {},
        }
        result = evaluate_candidate_promotion(cand, ctx)
        assert result.proposed_status == "REJECTED"
        assert result.decision_type == "reject"

    def test_stale_candidate_becomes_expired(self):
        old_iso = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        cand = _watch_candidate()
        ctx = {
            "enriched": _enriched_entry(),
            "memory": _memory_entry(last_seen=old_iso),
            "rejected": False, "approvals": [], "replay": {},
        }
        result = evaluate_candidate_promotion(cand, ctx)
        assert result.proposed_status == "EXPIRED"
        assert result.decision_type == "expire"

    def test_forbidden_upstream_status_rejected(self):
        cand = _watch_candidate()
        cand["status"] = "PROMOTED"
        ctx = {
            "enriched": _enriched_entry(),
            "memory": _memory_entry(),
            "rejected": False, "approvals": [], "replay": {},
        }
        result = evaluate_candidate_promotion(cand, ctx)
        assert result.proposed_status == "REJECTED"
        assert "block_forbidden_statuses" in result.gates_failed

    def test_discovered_status_without_watch_holds(self):
        cand = _watch_candidate()
        cand["status"] = "discovered"
        ctx = {
            "enriched": _enriched_entry(),
            "memory": _memory_entry(),
            "rejected": False, "approvals": [], "replay": {},
        }
        result = evaluate_candidate_promotion(cand, ctx)
        # require_watch_status_for_monitor gate fails → not promoted
        assert result.proposed_status != "MONITOR"

    def test_replay_strongly_negative_blocks_monitor(self):
        cand = _watch_candidate()
        ctx = {
            "enriched": _enriched_entry(),
            "memory": _memory_entry(),
            "rejected": False, "approvals": [],
            "replay": {"outcome": "strongly_negative"},
        }
        result = evaluate_candidate_promotion(cand, ctx)
        assert result.proposed_status != "MONITOR"

    def test_missing_ticker_safe(self):
        result = evaluate_candidate_promotion({}, {})
        assert result.ticker == ""
        assert result.decision_type == "hold_status"

    def test_proposed_status_always_in_allowed_set(self):
        # Try many adversarial candidates and ensure status is in ALLOWED_STATUSES
        test_cases = [
            {"ticker": "T1", "status": "BUY"},
            {"ticker": "T2", "status": "watch"},
            {"ticker": "T3", "status": "promoted"},
            {"ticker": "T4", "status": "weird"},
            {},
        ]
        for cand in test_cases:
            result = evaluate_candidate_promotion(cand, {})
            assert result.proposed_status in ALLOWED_STATUSES


# ---------------------------------------------------------------------------
# 5. Report builder
# ---------------------------------------------------------------------------

class TestReportBuilding:
    def test_empty_inputs_safe(self, tmp_path):
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        assert report.data_available is False
        assert report.decisions == []

    def test_safety_flags_hardcoded(self, tmp_path):
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        for flag_name in (
            "observe_only", "no_trade", "not_recommendation", "discovery_only",
            "no_portfolio_mutation", "no_watchlist_mutation",
            "no_decision_override", "no_score_mutation", "no_allocation_mutation",
        ):
            assert getattr(report, flag_name) is True

    def test_qualifying_candidate_produces_monitor_decision(self, tmp_path):
        _write_full_qualifying_setup(tmp_path)
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        assert len(report.decisions) == 1
        d = report.decisions[0]
        assert d.proposed_status == "MONITOR"
        assert d.decision_type == "promote_to_monitor"

    def test_rejected_candidate_classified_as_rejected(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/emerging_candidates.json", {"candidates": []})
        _write_sandbox(tmp_path, "discovery/rejected_candidates.json", {
            "candidates": [_watch_candidate("ZZZZ")],
        })
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        # Should produce a decision with REJECTED proposed status
        assert any(d.proposed_status == "REJECTED" for d in report.decisions)

    def test_decision_count_correct(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/emerging_candidates.json", {
            "candidates": [_watch_candidate("NVDA"), _watch_candidate("AAPL")],
        })
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        assert len(report.decisions) == 2

    def test_gates_in_report(self, tmp_path):
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        assert "minimum_corrob_score" in report.gates
        assert "maximum_risk_flags" in report.gates

    def test_prohibited_actions_empty_on_clean_input(self, tmp_path):
        _write_full_qualifying_setup(tmp_path)
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        assert report.prohibited_actions_detected == []

    def test_no_forbidden_status_in_decisions(self, tmp_path):
        _write_full_qualifying_setup(tmp_path)
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        for d in report.decisions:
            assert d.proposed_status in ALLOWED_STATUSES
            assert d.proposed_status not in FORBIDDEN_STATUSES
            assert d.prior_status in ALLOWED_STATUSES

    def test_deterministic_decision_ordering(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/emerging_candidates.json", {
            "candidates": [_watch_candidate("MSFT"),
                           _watch_candidate("AAPL"),
                           _watch_candidate("NVDA")],
        })
        inputs = load_automatic_promotion_inputs(tmp_path)
        r1 = build_automatic_promotion_report(inputs)
        r2 = build_automatic_promotion_report(inputs)
        assert [d.ticker for d in r1.decisions] == [d.ticker for d in r2.decisions]
        # Sorted alphabetically: AAPL, MSFT, NVDA
        assert [d.ticker for d in r1.decisions] == ["AAPL", "MSFT", "NVDA"]


# ---------------------------------------------------------------------------
# 6. Markdown rendering
# ---------------------------------------------------------------------------

class TestMarkdownRendering:
    def test_disclaimer_present(self, tmp_path):
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        md = render_automatic_promotion_markdown(report)
        assert _SAFETY_DISCLAIMER in md

    def test_sandbox_disclaimer_present(self, tmp_path):
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        md = render_automatic_promotion_markdown(report)
        assert "sandbox" in md.lower()

    def test_markdown_no_violations(self, tmp_path):
        _write_full_qualifying_setup(tmp_path)
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        md = render_automatic_promotion_markdown(report)
        assert validate_automatic_promotion_safety(md) == []

    def test_markdown_sections_present(self, tmp_path):
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        md = render_automatic_promotion_markdown(report)
        assert "Candidates Moved To Monitor" in md
        assert "Candidates Needing Review" in md
        assert "Candidates Rejected" in md or "Expired" in md
        assert "Gate Summary" in md
        assert "Risk Notes" in md
        assert "Safety Boundary" in md

    def test_safety_boundary_lists_allowed_statuses(self, tmp_path):
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        md = render_automatic_promotion_markdown(report)
        for status in ("DISCOVERED", "WATCH", "MONITOR",
                       "REJECTED", "EXPIRED", "NEEDS_REVIEW"):
            assert status in md


# ---------------------------------------------------------------------------
# 7. Artifact writing & run-mode governance
# ---------------------------------------------------------------------------

class TestArtifactWriting:
    def test_writes_three_artifacts(self, tmp_path):
        _write_full_qualifying_setup(tmp_path)
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        paths = write_automatic_promotion_report(
            report, base_dir=tmp_path, run_mode="discovery", run_id="test"
        )
        for key in ("automatic_promotion_candidates_json",
                    "automatic_promotion_summary_md",
                    "automatic_promotion_decisions_jsonl"):
            assert Path(paths[key]).exists()

    def test_artifacts_in_sandbox_namespace(self, tmp_path):
        _write_full_qualifying_setup(tmp_path)
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        paths = write_automatic_promotion_report(
            report, base_dir=tmp_path, run_mode="discovery", run_id="test"
        )
        for path_str in paths.values():
            assert "sandbox" in path_str

    def test_safety_flags_in_json(self, tmp_path):
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        write_automatic_promotion_report(
            report, base_dir=tmp_path, run_mode="discovery", run_id="test"
        )
        path = tmp_path / "sandbox" / "discovery" / "automatic_promotion_candidates.json"
        payload = json.loads(path.read_text())
        for key in (
            "observe_only", "no_trade", "not_recommendation", "discovery_only",
            "no_portfolio_mutation", "no_watchlist_mutation",
            "no_decision_override", "no_score_mutation", "no_allocation_mutation",
        ):
            assert payload[key] is True

    def test_daily_mode_cannot_write(self, tmp_path):
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs, run_mode="discovery")
        with pytest.raises(RunModeViolation):
            write_automatic_promotion_report(
                report, base_dir=tmp_path, run_mode="daily"
            )

    def test_manual_update_cannot_write(self, tmp_path):
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs, run_mode="discovery")
        with pytest.raises(RunModeViolation):
            write_automatic_promotion_report(
                report, base_dir=tmp_path, run_mode="manual_update"
            )

    def test_weekly_review_cannot_write(self, tmp_path):
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs, run_mode="discovery")
        with pytest.raises(RunModeViolation):
            write_automatic_promotion_report(
                report, base_dir=tmp_path, run_mode="weekly_review"
            )

    def test_backtest_mode_can_write(self, tmp_path):
        _write_full_qualifying_setup(tmp_path)
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs, run_mode="backtest")
        paths = write_automatic_promotion_report(
            report, base_dir=tmp_path, run_mode="backtest"
        )
        assert Path(paths["automatic_promotion_candidates_json"]).exists()

    def test_no_official_namespace_writes(self, tmp_path):
        _write_full_qualifying_setup(tmp_path)
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        write_automatic_promotion_report(
            report, base_dir=tmp_path, run_mode="discovery"
        )
        # Check no latest/policy/portfolio writes occurred
        if (tmp_path / "latest").exists():
            assert not list((tmp_path / "latest").glob("automatic_promotion*"))
        assert not (tmp_path / "policy").exists()
        assert not (tmp_path / "portfolio").exists()

    def test_jsonl_appends_decisions(self, tmp_path):
        _write_full_qualifying_setup(tmp_path)
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        # First write
        write_automatic_promotion_report(report, base_dir=tmp_path, run_mode="discovery")
        # Second write
        write_automatic_promotion_report(report, base_dir=tmp_path, run_mode="discovery")
        jsonl = tmp_path / "sandbox" / "discovery" / "automatic_promotion_decisions.jsonl"
        # Should now contain at least 2 lines per candidate
        lines = jsonl.read_text(encoding="utf-8").splitlines()
        assert len(lines) >= 2

    def test_writer_raises_when_sanitizer_disabled(self, tmp_path, monkeypatch):
        from portfolio_automation.discovery import automatic_promotion_governance as apg

        # Inject an adversarial decision directly
        tampered = AutomaticPromotionReport(
            generated_at="2026-05-11T00:00:00Z",
            run_mode="discovery",
            run_id="test",
            decisions=[PromotionDecision(
                ticker="NVDA",
                prior_status="WATCH",
                proposed_status="BUY",  # forbidden!
                decision_type="hold_status",
                eligibility_result="",
                evidence_score=0.5,
                evidence_summary="",
                gates_passed=[], gates_failed=[],
                risk_flags=[], catalyst_flags=[],
                corroboration_score=0.0, news_relevance_score=0.0,
                source_diversity=0,
                replay_context="", memory_context="", operator_context="",
                safety_flags={}, created_at="", reason="",
            )],
        )
        monkeypatch.setattr(apg, "sanitize_nested_automatic_promotion_payload", lambda p: p)
        monkeypatch.setattr(apg, "sanitize_automatic_promotion_text", lambda s: s)
        with pytest.raises(UnsafeAutomaticPromotionArtifactError):
            write_automatic_promotion_report(
                tampered, base_dir=tmp_path, run_mode="discovery"
            )
        # No artifacts written
        assert not (tmp_path / "sandbox" / "discovery" / "automatic_promotion_candidates.json").exists()
        assert not (tmp_path / "sandbox" / "discovery" / "automatic_promotion_summary.md").exists()


# ---------------------------------------------------------------------------
# 8. Orchestrator
# ---------------------------------------------------------------------------

class TestOrchestrator:
    def test_empty_inputs_safe(self, tmp_path):
        result = run_automatic_promotion_governance(base_dir=tmp_path)
        assert result["observe_only"] is True
        assert result["no_decision_override"] is True

    def test_writes_files_in_discovery_mode(self, tmp_path):
        _write_full_qualifying_setup(tmp_path)
        result = run_automatic_promotion_governance(
            base_dir=tmp_path, run_mode="discovery"
        )
        assert result.get("artifacts")
        for k, v in result["artifacts"].items():
            assert Path(v).exists()

    def test_dry_run_skips_writes(self, tmp_path):
        _write_full_qualifying_setup(tmp_path)
        result = run_automatic_promotion_governance(
            base_dir=tmp_path, run_mode="discovery", dry_run=True
        )
        assert result["dry_run"] is True
        assert not (tmp_path / "sandbox" / "discovery" / "automatic_promotion_candidates.json").exists()

    def test_write_files_false_skips_writes(self, tmp_path):
        _write_full_qualifying_setup(tmp_path)
        result = run_automatic_promotion_governance(
            base_dir=tmp_path, run_mode="discovery", write_files=False
        )
        assert result["dry_run"] is True
        assert not (tmp_path / "sandbox" / "discovery" / "automatic_promotion_candidates.json").exists()

    def test_daily_mode_acts_as_dry_run(self, tmp_path):
        _write_full_qualifying_setup(tmp_path)
        result = run_automatic_promotion_governance(
            base_dir=tmp_path, run_mode="daily"
        )
        assert result["dry_run"] is True
        assert not (tmp_path / "sandbox" / "discovery" / "automatic_promotion_candidates.json").exists()

    def test_invalid_run_mode_returns_error(self, tmp_path):
        result = run_automatic_promotion_governance(
            base_dir=tmp_path, run_mode="not_a_mode"
        )
        assert "error" in result

    def test_deterministic_repeated_output(self, tmp_path):
        _write_full_qualifying_setup(tmp_path)
        r1 = run_automatic_promotion_governance(
            base_dir=tmp_path, run_mode="discovery",
            run_id="determ", write_files=False,
        )
        r2 = run_automatic_promotion_governance(
            base_dir=tmp_path, run_mode="discovery",
            run_id="determ", write_files=False,
        )
        assert r1["decision_count"] == r2["decision_count"]
        assert r1["monitor_count"] == r2["monitor_count"]
        assert r1["rejected_count"] == r2["rejected_count"]
        assert r1["safety_violations"] == r2["safety_violations"]

    def test_orchestrator_records_blocked_write(self, tmp_path, monkeypatch):
        from portfolio_automation.discovery import automatic_promotion_governance as apg

        def _raise(report, base_dir, run_mode, run_id=None):
            raise UnsafeAutomaticPromotionArtifactError("forced for test")
        monkeypatch.setattr(apg, "write_automatic_promotion_report", _raise)
        result = apg.run_automatic_promotion_governance(
            base_dir=tmp_path, run_mode="discovery"
        )
        assert "blocked_unsafe_write" in result


# ---------------------------------------------------------------------------
# 9. Adversarial input protection
# ---------------------------------------------------------------------------

def _adversarial_emerging() -> dict:
    """Inputs whose labels carry prohibited phrases / forbidden actions."""
    return {
        "candidates": [
            {
                "ticker": "NVDA",
                "status": "watch",
                "score": 0.8,
                "mention_count": 5,
                "unique_source_count": 3,
                "corroboration_score": 0.8,
                "risk_flag": False,
                "last_seen": _now_iso(),
            },
            {
                "ticker": "ZZZZ",
                "status": "PROMOTED",  # forbidden upstream status
                "score": 0.5,
                "last_seen": _now_iso(),
            },
        ]
    }


def _adversarial_enriched() -> dict:
    return {
        "enriched_candidates": [
            {
                "ticker": "NVDA",
                "candidate_status": "watch",
                "matched_themes": ["buy now", "ai_infrastructure"],
                "risk_flags": ["sell now"],
                "catalyst_flags": ["promote candidate"],
                "matched_news_count": 5,
                "source_diversity": 3,
                "news_relevance_score": 0.7,
            }
        ]
    }


class TestAdversarialInputProtection:
    def test_forbidden_upstream_status_rejected(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/emerging_candidates.json",
                       _adversarial_emerging())
        inputs = load_automatic_promotion_inputs(tmp_path)
        report = build_automatic_promotion_report(inputs)
        zzzz = next((d for d in report.decisions if d.ticker == "ZZZZ"), None)
        assert zzzz is not None
        assert zzzz.proposed_status == "REJECTED"

    def test_adversarial_phrases_not_in_json(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/emerging_candidates.json",
                       _adversarial_emerging())
        _write_sandbox(tmp_path, "discovery/news_enriched_candidates.json",
                       _adversarial_enriched())
        _write_sandbox(tmp_path, "discovery/discovery_memory.json", {
            "entries": [_memory_entry()],
        })
        run_automatic_promotion_governance(base_dir=tmp_path, run_mode="discovery")
        raw = (tmp_path / "sandbox" / "discovery" /
               "automatic_promotion_candidates.json").read_text()
        stripped = _strip_disclaimers(raw)
        for phrase in _ADVERSARIAL_PHRASES:
            assert phrase not in stripped.lower(), \
                f"Prohibited phrase {phrase!r} leaked into JSON output"

    def test_adversarial_phrases_not_in_markdown(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/emerging_candidates.json",
                       _adversarial_emerging())
        _write_sandbox(tmp_path, "discovery/news_enriched_candidates.json",
                       _adversarial_enriched())
        run_automatic_promotion_governance(base_dir=tmp_path, run_mode="discovery")
        md = (tmp_path / "sandbox" / "discovery" /
              "automatic_promotion_summary.md").read_text()
        stripped = _strip_disclaimers(md)
        for phrase in _ADVERSARIAL_PHRASES:
            assert phrase not in stripped.lower(), \
                f"Prohibited phrase {phrase!r} leaked into Markdown output"

    def test_no_forbidden_action_tokens_in_json(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/emerging_candidates.json",
                       _adversarial_emerging())
        run_automatic_promotion_governance(base_dir=tmp_path, run_mode="discovery")
        raw = (tmp_path / "sandbox" / "discovery" /
               "automatic_promotion_candidates.json").read_text()
        stripped = _strip_disclaimers(raw)
        for token in ("BUY", "SELL", "HOLD", "ACTIONABLE", "PROMOTED",
                      "VALIDATED", "APPROVED", "TRADE", "RECOMMENDATION"):
            assert re.search(rf"\b{token}\b", stripped, re.IGNORECASE) is None, \
                f"Forbidden action {token!r} leaked into JSON output"

    def test_no_forbidden_action_tokens_in_markdown(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/emerging_candidates.json",
                       _adversarial_emerging())
        run_automatic_promotion_governance(base_dir=tmp_path, run_mode="discovery")
        md = (tmp_path / "sandbox" / "discovery" /
              "automatic_promotion_summary.md").read_text()
        # Strip safety boundary section which lists the forbidden tokens
        # explicitly (as documentation of what is forbidden).
        # Use a narrower probe: actual emitted action values would be in
        # decision lines; documentation lines are inside backticks.
        # Check: no unadorned action value lines like "prior: BUY"
        for token in ("BUY", "SELL", "HOLD"):
            # Look for the token outside the documentation table.
            # Documentation lists them as `BUY`, `SELL`, etc. — keep those.
            # Outlawed pattern: prior=`BUY`, proposed=`BUY`, etc.
            assert f"prior=`{token}`" not in md
            assert f"proposed=`{token}`" not in md

    def test_no_mutation_fields_in_output(self, tmp_path):
        _write_full_qualifying_setup(tmp_path)
        run_automatic_promotion_governance(base_dir=tmp_path, run_mode="discovery")
        payload = json.loads(
            (tmp_path / "sandbox" / "discovery" /
             "automatic_promotion_candidates.json").read_text()
        )
        for forbidden_field in (
            "signal_score", "confidence_score", "effective_score",
            "conviction_score", "final_rank_score", "recommendation_score",
            "allocation", "allocations", "target_weight",
            "watchlist", "watchlist_changes", "watchlist_add",
        ):
            assert forbidden_field not in payload
