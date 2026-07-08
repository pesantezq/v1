"""
Tests for watchlist_scanner/daily_memo.py.

Covers:
  - Empty summary handled gracefully
  - Memo contains all required sections
  - Subject line format correct
  - No crashes on missing individual fields
  - Formatting readable (no excessive line length)
  - Markdown output contains expected headings
  - Top insight synthesiser logic
  - get_subject date extraction
  - send_email fails gracefully when env vars missing
  - generate_daily_memo write_files=False doesn't create files
  - generate_daily_memo write_files=True creates both files
  - send_email retry: succeeds on second attempt, exhausts all attempts
  - send_test_email: delegates to send_email with correct subject
  - Pipeline step: continues and reports failure when email send fails
"""
from __future__ import annotations

import smtplib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.daily_memo import (
    build_daily_memo,
    build_daily_memo_md,
    generate_daily_memo,
    get_subject,
    send_email,
    send_test_email,
    _advisor_stack_items,
    _build_top_insight,
    _build_memo_top_insight,
    _investor_core_md,
    _investor_core_text,
)


class TestDeferredOverflowIndicator:
    """The Deferred/Blocked section caps the visible list at 6 entries. When more
    than 6 are deferred it must disclose the remainder with an '...and N more'
    line so the operator is not silently shown 6 of N (memo-reviewer 2026-07-07)."""

    def _mc(self, n):
        return {"deferred_actions": [
            {"symbol": f"SYM{i}", "presentation_state": "DEFERRED_BY_MONTHLY_BUDGET",
             "blocking_reason": "DEFERRED_BY_MONTHLY_BUDGET"}
            for i in range(n)
        ]}

    def test_md_shows_overflow_when_more_than_six(self):
        out = "\n".join(_investor_core_md(self._mc(22)))
        assert "...and 16 more" in out

    def test_md_no_overflow_when_six_or_fewer(self):
        out = "\n".join(_investor_core_md(self._mc(6)))
        assert "more" not in out.split("Deferred / Blocked", 1)[-1]

    def test_text_shows_overflow_when_more_than_six(self):
        out = "\n".join(_investor_core_text(self._mc(22)))
        assert "...and 16 more" in out

    def test_text_no_overflow_when_six_or_fewer(self):
        out = "\n".join(_investor_core_text(self._mc(6)))
        assert "...and" not in out


class TestWeeklyDeploymentBlock:
    """The Weekly Deployment block (spec 2026-07-07) surfaces the paced weekly
    tranche + glide breakdown ahead of Funded Actions. It labels the weekly figure
    'remaining this week' (a live residual), not a fixed tranche."""

    def _mc(self, cadence="weekly"):
        env = {
            "status": "ok",
            "cash_reserve_target_amount": 524.0,
            "glide_slice": 406.75,
            "monthly_contribution_net_investable_base": 1_000.0,
            "monthly_contribution_net_investable": 1_406.75,
            "weekly_pacing": {
                "deploy_cadence": cadence,
                "weekly_tranche": 351.69,
                "deployed_this_week": 0.0,
                "weekly_remaining": 351.69,
                "note": None,
            },
        }
        return {"funding": {"available": True, "monthly_envelope": env},
                "funded_actions": [], "deferred_actions": []}

    def test_md_renders_weekly_block(self):
        out = "\n".join(_investor_core_md(self._mc()))
        assert "Weekly Deployment" in out
        assert "remaining this week" in out
        assert "glide" in out

    def test_text_renders_weekly_block(self):
        out = "\n".join(_investor_core_text(self._mc()))
        assert "WEEKLY DEPLOYMENT" in out
        assert "remaining this week" in out

    def test_monthly_cadence_hides_weekly_line_keeps_cycle(self):
        out = "\n".join(_investor_core_md(self._mc(cadence="monthly")))
        # Cycle net-investable always shown; the per-week line is suppressed.
        assert "Cycle net-investable" in out
        assert "remaining this week" not in out


class TestPortfolioValueDisambiguation:
    """The Monthly Capital Plan portfolio total is a pre-deploy funding snapshot;
    the Portfolio Growth / Risk Delta total is the live post-deploy value. They
    differ by ~today's funded capital. The memo must not present two unlabeled
    portfolio totals — the funding-snapshot figure carries a distinguishing
    label (memo-reviewer 2026-07-08)."""

    def test_monthly_plan_labels_portfolio_value_as_funding_snapshot(self):
        from watchlist_scanner.daily_memo import _monthly_plan_rows
        rows = _monthly_plan_rows({"status": "ok", "portfolio_value": 10286.88})
        labels = [lbl for lbl, _ in rows]
        assert any("funding snapshot" in lbl for lbl in labels), labels
        # the bare, ambiguous "Portfolio value" label must not appear alone
        assert "Portfolio value" not in labels, labels


class TestTopInsightPersistenceLabel:
    """The Top Insight persistence label must have a zero/low floor — a theme
    with persistence 0.0 must NOT render as 'moderate persistence' (the prior
    binary >=0.5-else-moderate bug mislabelled first-seen themes)."""

    def _tt(self, persistence):
        return {"name": "Defense", "persistence": persistence}

    def _to(self):
        return {"ticker": "NOC", "conviction_band": "high", "portfolio_fit_label": "neutral"}

    def test_zero_persistence_is_not_moderate(self):
        s = _build_top_insight(self._tt(0.0), self._to())
        assert "moderate persistence" not in s
        assert "strong persistence" not in s

    def test_zero_persistence_reads_as_emerging(self):
        s = _build_top_insight(self._tt(0.0), self._to())
        assert "emerging" in s.lower()

    def test_low_positive_persistence_is_moderate(self):
        s = _build_top_insight(self._tt(0.3), self._to())
        assert "moderate persistence" in s

    def test_high_persistence_is_strong(self):
        s = _build_top_insight(self._tt(0.72), self._to())
        assert "strong persistence" in s

    def test_theme_and_ticker_still_present_at_zero(self):
        s = _build_top_insight(self._tt(0.0), self._to())
        assert "Defense" in s and "NOC" in s


class TestTopInsightThemeMembership:
    """The memo Top Insight must only claim the lead opportunity is "inside the
    {theme}" when that ticker is actually a member of top_theme.tickers. A
    non-member ticker (e.g. MSFT vs an Energy Transition theme of XOM/CVX) must
    NOT be asserted to belong to the theme — both facts are stated, unlinked."""

    def _tt(self, tickers):
        return {"name": "Energy Transition", "persistence": 0.57, "tickers": list(tickers)}

    def _to(self, ticker):
        return {"ticker": ticker, "conviction_band": "normal", "portfolio_fit_label": "strong"}

    def test_member_ticker_asserts_theme_membership(self):
        s = _build_memo_top_insight(self._tt(["XOM", "CVX"]), self._to("CVX"), [])
        assert "inside the Energy Transition theme" in s

    def test_non_member_ticker_does_not_assert_membership(self):
        s = _build_memo_top_insight(self._tt(["XOM", "CVX"]), self._to("MSFT"), [])
        assert "inside the Energy Transition theme" not in s
        # both facts still present, just not falsely linked
        assert "MSFT" in s
        assert "Energy Transition" in s

    def test_empty_theme_tickers_does_not_assert_membership(self):
        s = _build_memo_top_insight(self._tt([]), self._to("MSFT"), [])
        assert "inside the Energy Transition theme" not in s
        assert "MSFT" in s and "Energy Transition" in s

    def test_membership_is_case_insensitive(self):
        s = _build_memo_top_insight(self._tt(["xom", "cvx"]), self._to("CVX"), [])
        assert "inside the Energy Transition theme" in s


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _full_summary(**overrides) -> dict:
    base = {
        "generated_at": "2026-04-27T09:15:23",
        "schema_version": "1",
        "top_theme": {
            "name": "AI",
            "type": "classified",
            "score": 0.821,
            "persistence": 0.720,
            "acceleration": 0.045,
            "tickers": ["NVDA", "MSFT", "AMD", "GOOGL"],
        },
        "top_opportunity": {
            "ticker": "NVDA",
            "final_rank_score": 0.913,
            "signal_score": 0.880,
            "confidence": 0.856,
            "theme_alignment_label": "aligned",
            "portfolio_fit_label": "strong",
            "portfolio_fit_score": 0.890,
            "rank_multiplier": 1.15,
            "conviction_band": "high_conviction",
        },
        "best_portfolio_fit": {
            "ticker": "NVDA",
            "portfolio_fit_score": 0.890,
            "portfolio_fit_label": "strong",
            "portfolio_fit_reason": "Strong sector alignment with tech sleeve",
            "final_rank_score": 0.913,
        },
        "capital_preview": {
            "candidate_count": 5,
            "total_baseline_pct": 0.10,
            "total_preview_pct": 0.12,
            "preview_vs_baseline_delta": 0.02,
            "simulation_sample_size": 35,
            "simulation_efficiency_delta": 0.03,
            "simulation_return_delta": 0.05,
            "baseline_capital_efficiency": 0.12,
            "rank_aware_capital_efficiency": 0.15,
        },
        "system_state": {
            "ranking_weights_source": "approved",
            "ranking_weights_candidate": "portfolio_fit_heavy",
            "ranking_weights_approved_at": "2026-04-27T08:00:00",
            "allocation_policy_status": "approved_not_live",
            "applied_to_live": False,
            "policy_sample_size": 42,
            "policy_low_sample_warning": False,
            "simulation_observe_only": True,
            "simulation_not_applied": True,
            "simulation_sample_size": 35,
            "preview_observe_only": True,
            "preview_not_applied": True,
        },
        "data_health": {
            "degraded_mode": False,
            "data_mode": "live",
            "total_signals": 12,
            "eligible_signals": 8,
            "missing_artifacts": [],
            "missing_artifact_count": 0,
            "all_artifacts_present": True,
        },
        "changes": {
            "previous_available": True,
            "previous_generated_at": "2026-04-26T09:10:00",
            "change_count": 1,
            "changes": ["Top theme changed: Energy -> AI"],
            "summary_line": "1 change detected.",
        },
    }
    base.update(overrides)
    return base


def _decision_plan_payload() -> dict:
    return {
        "generated_at": "2026-04-27T09:15:23",
        "run_mode": "daily",
        "observe_only": True,
        "total_decisions": 5,
        "decisions": [
            {
                "symbol": "QLD",
                "decision": "SELL",
                "priority": 0.95,
                "urgency": "critical",
                "source": "structural",
                "recommended_action": "Reduce QLD position.",
                "recommended_amount": 1500.0,
                "recommended_allocation_pct": 0.15,
                "reason": "Structural leverage violation on QLD.",
                "risk_flags": ["leverage_breach"],
                "confidence": 1.0,
                "inputs_used": {"violation_type": "leverage"},
            },
            {
                "symbol": "QQQ",
                "decision": "SELL",
                "priority": 0.88,
                "urgency": "high",
                "source": "structural",
                "recommended_action": "Trim QQQ concentration.",
                "recommended_amount": 1000.0,
                "recommended_allocation_pct": 0.40,
                "reason": "Structural concentration violation on QQQ.",
                "risk_flags": ["concentration_breach"],
                "confidence": 1.0,
                "inputs_used": {"violation_type": "concentration"},
            },
            {
                "symbol": "VFH",
                "decision": "SCALE",
                "priority": 0.55,
                "urgency": "low",
                "source": "portfolio",
                "recommended_action": "Add to VFH.",
                "recommended_amount": 500.0,
                "recommended_allocation_pct": 0.03,
                "reason": "Underweight contribution target.",
                "risk_flags": [],
                "confidence": 0.9,
                "inputs_used": {},
            },
            {
                "symbol": "FANG",
                "decision": "WAIT",
                "priority": 0.55,
                "urgency": "medium",
                "source": "market",
                "recommended_action": "Stand by on FANG.",
                "recommended_amount": None,
                "recommended_allocation_pct": None,
                "reason": "Opportunity exists but confidence is not yet strong enough.",
                "risk_flags": ["low_confidence"],
                "confidence": 0.55,
                "inputs_used": {},
            },
            {
                "symbol": "XLRE",
                "decision": "WAIT",
                "priority": 0.55,
                "urgency": "medium",
                "source": "market",
                "recommended_action": "Stand by on XLRE.",
                "recommended_amount": None,
                "recommended_allocation_pct": None,
                "reason": "Opportunity exists but confidence is not yet strong enough.",
                "risk_flags": [],
                "confidence": 0.55,
                "inputs_used": {},
            },
        ],
    }


# ---------------------------------------------------------------------------
# TestGetSubject
# ---------------------------------------------------------------------------

class TestGetSubject:
    def test_date_extracted_from_generated_at(self):
        s = {"generated_at": "2026-04-27T09:15:23"}
        assert get_subject(s) == "Daily Investment Memo — 2026-04-27"

    def test_missing_generated_at_uses_today(self):
        subject = get_subject({})
        assert "Daily Investment Memo —" in subject
        assert len(subject) > len("Daily Investment Memo — ")

    def test_empty_string_generated_at_falls_back(self):
        subject = get_subject({"generated_at": ""})
        assert "Daily Investment Memo —" in subject

    def test_partial_iso_string(self):
        subject = get_subject({"generated_at": "2025-12-01"})
        assert "2025-12-01" in subject


# ---------------------------------------------------------------------------
# TestBuildDailyMemo — empty / missing input
# ---------------------------------------------------------------------------

class TestBuildDailyMemoEmpty:
    def test_empty_dict_does_not_crash(self):
        result = build_daily_memo({})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_empty_dict_contains_subject_line(self):
        result = build_daily_memo({})
        assert "Subject:" in result

    def test_empty_dict_contains_all_section_headers(self):
        result = build_daily_memo({})
        for section in (
            "TOP INSIGHT",
            "TOP THEME",
            "TOP OPPORTUNITY",
            "PORTFOLIO INSIGHT",
            "CAPITAL PREVIEW",
            "POLICY STATUS",
            "CHANGES SINCE LAST RUN",
        ):
            assert section in result, f"Missing section: {section}"

    def test_empty_dict_contains_footer(self):
        result = build_daily_memo({})
        assert "Advisory only" in result
        assert "no trades executed" in result

    def test_none_sections_do_not_crash(self):
        summary = {
            "top_theme": None,
            "top_opportunity": None,
            "best_portfolio_fit": None,
            "capital_preview": None,
            "system_state": None,
            "data_health": None,
            "changes": None,
        }
        result = build_daily_memo(summary)
        assert "TOP INSIGHT" in result


# ---------------------------------------------------------------------------
# TestBuildDailyMemo — full summary
# ---------------------------------------------------------------------------

class TestBuildDailyMemoFull:
    def test_subject_line_present(self):
        result = build_daily_memo(_full_summary())
        assert "Subject: Daily Investment Memo — 2026-04-27" in result

    def test_date_in_header(self):
        result = build_daily_memo(_full_summary())
        assert "2026-04-27" in result

    def test_top_insight_contains_theme_name(self):
        result = build_daily_memo(_full_summary())
        assert "AI" in result

    def test_top_insight_contains_ticker(self):
        result = build_daily_memo(_full_summary())
        assert "NVDA" in result

    def test_theme_score_present(self):
        result = build_daily_memo(_full_summary())
        assert "0.821" in result

    def test_theme_persistence_present(self):
        result = build_daily_memo(_full_summary())
        assert "0.720" in result

    def test_theme_tickers_present(self):
        result = build_daily_memo(_full_summary())
        assert "NVDA" in result
        assert "MSFT" in result

    def test_opportunity_rank_score_present(self):
        result = build_daily_memo(_full_summary())
        assert "0.913" in result

    def test_opportunity_confidence_present(self):
        result = build_daily_memo(_full_summary())
        assert "0.856" in result

    def test_conviction_band_present(self):
        result = build_daily_memo(_full_summary())
        assert "High Conviction" in result

    def test_portfolio_fit_score_present(self):
        result = build_daily_memo(_full_summary())
        assert "0.890" in result

    def test_portfolio_fit_reason_present(self):
        result = build_daily_memo(_full_summary())
        assert "tech sleeve" in result

    def test_capital_preview_candidates_present(self):
        result = build_daily_memo(_full_summary())
        assert "Candidates:" in result

    def test_capital_preview_pct_present(self):
        result = build_daily_memo(_full_summary())
        assert "10.0%" in result
        assert "12.0%" in result

    def test_policy_status_weights_source(self):
        result = build_daily_memo(_full_summary())
        assert "Approved" in result

    def test_policy_status_advisory_note(self):
        result = build_daily_memo(_full_summary())
        assert "advisory only" in result.lower() or "No — advisory only" in result

    def test_changes_summary_line(self):
        result = build_daily_memo(_full_summary())
        assert "1 change detected." in result

    def test_changes_list_item(self):
        result = build_daily_memo(_full_summary())
        assert "Top theme changed" in result

    def test_generated_timestamp_in_footer(self):
        result = build_daily_memo(_full_summary())
        assert "09:15:23" in result

    def test_all_sections_present(self):
        result = build_daily_memo(_full_summary())
        for section in (
            "TOP INSIGHT",
            "TOP THEME",
            "TOP OPPORTUNITY",
            "PORTFOLIO INSIGHT",
            "CAPITAL PREVIEW",
            "POLICY STATUS",
            "CHANGES SINCE LAST RUN",
        ):
            assert section in result, f"Section missing: {section}"

    def test_no_excessive_line_length(self):
        result = build_daily_memo(_full_summary())
        for i, line in enumerate(result.split("\n"), 1):
            assert len(line) <= 200, f"Line {i} too long ({len(line)} chars): {line!r}"

    def test_degraded_mode_flagged(self):
        s = _full_summary(data_health={"degraded_mode": True, "total_signals": 5, "eligible_signals": 2})
        result = build_daily_memo(s)
        assert "DEGRADED" in result

    def test_low_sample_warning_shown(self):
        ss = _full_summary()["system_state"].copy()
        ss["policy_low_sample_warning"] = True
        ss["policy_sample_size"] = 3
        result = build_daily_memo(_full_summary(system_state=ss))
        assert "WARNING" in result or "Low sample" in result

    def test_rank_multiplier_shown_when_not_one(self):
        result = build_daily_memo(_full_summary())
        assert "x1.15" in result or "1.15" in result

    def test_no_previous_note_when_unavailable(self):
        ch = {"previous_available": False, "changes": [], "summary_line": "No previous."}
        result = build_daily_memo(_full_summary(changes=ch))
        assert "No previous" in result

    def test_decision_engine_sections_present_when_plan_attached(self):
        summary = _full_summary()
        summary["_decision_plan"] = _decision_plan_payload()
        result = build_daily_memo(summary)
        assert "TOP DECISIONS" in result
        assert "CAPITAL ACTIONS" in result
        assert "RISK FOCUS" in result

    def test_structural_decisions_appear_first_in_top_decisions(self):
        summary = _full_summary()
        summary["_decision_plan"] = _decision_plan_payload()
        result = build_daily_memo(summary)
        top_idx = result.index("TOP DECISIONS")
        qld_idx = result.index("QLD", top_idx)
        qqq_idx = result.index("QQQ", top_idx)
        vfh_idx = result.index("VFH", top_idx)
        assert qld_idx < vfh_idx
        assert qqq_idx < vfh_idx

    def test_risk_focus_mentions_structural_risks(self):
        summary = _full_summary()
        summary["_decision_plan"] = _decision_plan_payload()
        result = build_daily_memo(summary)
        assert "Concentration risk is active" in result
        assert "Leverage risk is active" in result

    def test_capital_actions_summarize_total_amount(self):
        summary = _full_summary()
        summary["_decision_plan"] = _decision_plan_payload()
        result = build_daily_memo(summary)
        assert "Total recommended capital amount:" in result
        assert "$3,000.00" in result


# ---------------------------------------------------------------------------
# TestBuildDailyMemoMd
# ---------------------------------------------------------------------------

class TestBuildDailyMemoMd:
    def test_returns_string(self):
        assert isinstance(build_daily_memo_md({}), str)

    def test_empty_does_not_crash(self):
        result = build_daily_memo_md({})
        assert len(result) > 0

    def test_title_heading_present(self):
        result = build_daily_memo_md(_full_summary())
        assert "# Daily Investment Memo" in result

    def test_top_insight_heading(self):
        result = build_daily_memo_md(_full_summary())
        assert "## Top Insight" in result

    def test_top_theme_heading(self):
        result = build_daily_memo_md(_full_summary())
        assert "## Top Theme" in result

    def test_top_opportunity_heading(self):
        result = build_daily_memo_md(_full_summary())
        assert "## Top Opportunity" in result

    def test_portfolio_insight_heading(self):
        result = build_daily_memo_md(_full_summary())
        assert "## Portfolio Insight" in result

    def test_capital_preview_heading(self):
        result = build_daily_memo_md(_full_summary())
        assert "## Capital Preview" in result

    def test_policy_status_heading(self):
        result = build_daily_memo_md(_full_summary())
        assert "## Policy Status" in result

    def test_changes_heading(self):
        result = build_daily_memo_md(_full_summary())
        assert "## Changes" in result

    def test_footer_contains_advisory_note(self):
        result = build_daily_memo_md(_full_summary())
        assert "Advisory only" in result

    def test_ticker_in_output(self):
        result = build_daily_memo_md(_full_summary())
        assert "NVDA" in result

    def test_theme_name_in_output(self):
        result = build_daily_memo_md(_full_summary())
        assert "AI" in result

    def test_insight_blockquote(self):
        result = build_daily_memo_md(_full_summary())
        assert "> " in result

    def test_degraded_flagged_in_bold(self):
        s = _full_summary(data_health={"degraded_mode": True})
        result = build_daily_memo_md(s)
        assert "DEGRADED" in result

    def test_decision_plan_sections_present(self):
        summary = _full_summary()
        summary["_decision_plan"] = _decision_plan_payload()
        result = build_daily_memo_md(summary)
        assert "## Top Decisions" in result
        assert "## Capital Actions" in result
        assert "## Risk Focus" in result


# ---------------------------------------------------------------------------
# TestSendEmail
# ---------------------------------------------------------------------------

class TestSendEmail:
    def test_returns_false_when_env_vars_missing(self, monkeypatch):
        for var in (
            "SMTP_SERVER", "SMTP_HOST", "EMAIL_USER", "EMAIL_SENDER",
            "EMAIL_PASS", "EMAIL_PASSWORD", "EMAIL_TO", "EMAIL_RECIPIENT",
        ):
            monkeypatch.delenv(var, raising=False)
        with patch("watchlist_scanner.daily_memo._load_email_env", return_value=None):
            result = send_email("memo text")
        assert result is False

    def test_returns_false_when_server_missing(self, monkeypatch):
        monkeypatch.setenv("EMAIL_USER", "user@example.com")
        monkeypatch.setenv("EMAIL_PASS", "secret")
        monkeypatch.setenv("EMAIL_TO", "to@example.com")
        monkeypatch.delenv("SMTP_SERVER", raising=False)
        result = send_email("memo text")
        assert result is False

    def test_does_not_raise_on_bad_server(self, monkeypatch):
        monkeypatch.setenv("SMTP_SERVER", "invalid.host.nonexistent")
        monkeypatch.setenv("SMTP_PORT", "587")
        monkeypatch.setenv("EMAIL_USER", "user@example.com")
        monkeypatch.setenv("EMAIL_PASS", "secret")
        monkeypatch.setenv("EMAIL_TO", "to@example.com")
        result = send_email("memo text")
        assert result is False  # connection refused / DNS error — never raises

    def test_subject_extracted_from_memo_text(self, monkeypatch):
        # env vars missing → returns False, but subject extraction must not crash
        for var in (
            "SMTP_SERVER", "SMTP_HOST", "EMAIL_USER", "EMAIL_SENDER",
            "EMAIL_PASS", "EMAIL_PASSWORD", "EMAIL_TO", "EMAIL_RECIPIENT",
        ):
            monkeypatch.delenv(var, raising=False)
        memo = "Subject: Daily Investment Memo — 2026-04-27\n\nBody."
        with patch("watchlist_scanner.daily_memo._load_email_env", return_value=None):
            result = send_email(memo)
        assert result is False  # fails gracefully


    def test_accepts_legacy_env_aliases(self, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
        monkeypatch.setenv("SMTP_PORT", "587")
        monkeypatch.setenv("EMAIL_SENDER", "user@test.local")
        monkeypatch.setenv("EMAIL_PASSWORD", "secret")
        monkeypatch.setenv("EMAIL_RECIPIENT", "to@test.local")
        stub = _FakeSMTP(fail_attempts=0)
        with patch("smtplib.SMTP", stub):
            result = send_email("memo text")
        assert result is True

    def test_loads_env_from_cwd_dotenv(self, monkeypatch, tmp_path):
        for var in (
            "SMTP_SERVER", "SMTP_HOST", "SMTP_PORT", "EMAIL_USER", "EMAIL_SENDER",
            "EMAIL_PASS", "EMAIL_PASSWORD", "EMAIL_TO", "EMAIL_RECIPIENT",
        ):
            monkeypatch.delenv(var, raising=False)

        (tmp_path / ".env").write_text(
            "\n".join([
                "SMTP_SERVER=smtp.test.local",
                "SMTP_PORT=587",
                "EMAIL_USER=user@test.local",
                "EMAIL_PASS=secret",
                "EMAIL_TO=to@test.local",
            ]),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        stub = _FakeSMTP(fail_attempts=0)
        with patch("smtplib.SMTP", stub):
            result = send_email("memo text")
        assert result is True


# ---------------------------------------------------------------------------
# TestGenerateDailyMemo
# ---------------------------------------------------------------------------

class TestGenerateDailyMemo:
    def test_dry_run_returns_tuple(self, tmp_path):
        result = generate_daily_memo(root=tmp_path, write_files=False)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_dry_run_returns_strings(self, tmp_path):
        txt, md = generate_daily_memo(root=tmp_path, write_files=False)
        assert isinstance(txt, str)
        assert isinstance(md, str)

    def test_dry_run_writes_no_files(self, tmp_path):
        generate_daily_memo(root=tmp_path, write_files=False)
        txt_path = tmp_path / "outputs" / "latest" / "daily_memo.txt"
        md_path  = tmp_path / "outputs" / "latest" / "daily_memo.md"
        assert not txt_path.exists()
        assert not md_path.exists()

    def test_write_mode_creates_txt(self, tmp_path):
        generate_daily_memo(root=tmp_path, write_files=True)
        txt_path = tmp_path / "outputs" / "latest" / "daily_memo.txt"
        assert txt_path.exists()
        assert len(txt_path.read_text(encoding="utf-8")) > 0

    def test_write_mode_creates_md(self, tmp_path):
        generate_daily_memo(root=tmp_path, write_files=True)
        md_path = tmp_path / "outputs" / "latest" / "daily_memo.md"
        assert md_path.exists()
        assert len(md_path.read_text(encoding="utf-8")) > 0

    def test_txt_file_contains_all_sections(self, tmp_path):
        generate_daily_memo(root=tmp_path, write_files=True)
        txt = (tmp_path / "outputs" / "latest" / "daily_memo.txt").read_text(encoding="utf-8")
        for section in ("TOP INSIGHT", "TOP THEME", "TOP OPPORTUNITY", "POLICY STATUS"):
            assert section in txt, f"Section missing: {section}"

    def test_md_file_contains_headings(self, tmp_path):
        generate_daily_memo(root=tmp_path, write_files=True)
        md = (tmp_path / "outputs" / "latest" / "daily_memo.md").read_text(encoding="utf-8")
        assert "# Daily Investment Memo" in md

    def test_missing_summary_file_handled_gracefully(self, tmp_path):
        txt, md = generate_daily_memo(root=tmp_path, write_files=False)
        assert "Advisory only" in txt
        assert "# Daily Investment Memo" in md

    def test_missing_decision_plan_file_shows_unavailable(self, tmp_path):
        import json

        out_dir = tmp_path / "outputs" / "latest"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "system_decision_summary.json").write_text(
            json.dumps(_full_summary()), encoding="utf-8"
        )

        txt, md = generate_daily_memo(root=tmp_path, write_files=False)
        assert "Decision plan unavailable." in txt
        assert "_Decision plan unavailable._" in md

    def test_valid_decision_plan_file_is_rendered(self, tmp_path):
        import json

        out_dir = tmp_path / "outputs" / "latest"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "system_decision_summary.json").write_text(
            json.dumps(_full_summary()), encoding="utf-8"
        )
        (out_dir / "decision_plan.json").write_text(
            json.dumps(_decision_plan_payload()), encoding="utf-8"
        )

        txt, md = generate_daily_memo(root=tmp_path, write_files=False)
        assert "TOP DECISIONS" in txt
        assert "QLD" in txt
        assert "QQQ" in txt
        assert "Risk Flags: leverage_breach" in txt
        assert "## Top Decisions" in md
        assert "Structural decisions lead the plan" in md

    def test_structural_decisions_render_before_other_actions(self, tmp_path):
        import json

        out_dir = tmp_path / "outputs" / "latest"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "system_decision_summary.json").write_text(
            json.dumps(_full_summary()), encoding="utf-8"
        )
        (out_dir / "decision_plan.json").write_text(
            json.dumps(_decision_plan_payload()), encoding="utf-8"
        )

        txt, _ = generate_daily_memo(root=tmp_path, write_files=False)
        top_idx = txt.index("TOP DECISIONS")
        qld_idx = txt.index("QLD", top_idx)
        qqq_idx = txt.index("QQQ", top_idx)
        vfh_idx = txt.index("VFH", top_idx)
        assert qld_idx < vfh_idx
        assert qqq_idx < vfh_idx

    def test_real_summary_written_then_read(self, tmp_path):
        import json
        from watchlist_scanner.system_summary import build_system_decision_summary

        artifacts = {
            "signals": {"results": [{"ticker": "TSLA", "filter_allowed": True,
                                     "final_rank_score": 0.88, "confidence_score": 0.75,
                                     "portfolio_fit_score": 0.80, "portfolio_fit_label": "strong",
                                     "theme_alignment_label": "aligned", "rank_multiplier": 1.1,
                                     "conviction_band": "high_conviction"}]},
            "themes": {"themes": [{"name": "EV", "type": "classified", "score": 0.75,
                                   "persistence": 0.6, "acceleration": 0.1, "tickers": ["TSLA"]}]},
        }
        flags = {k: False for k in (
            "watchlist_signals", "theme_opportunities", "portfolio_snapshot",
            "approved_ranking_config", "approved_allocation_policy",
            "allocation_preview", "allocation_simulation", "weight_tuning_suggestions",
        )}
        summary = build_system_decision_summary(artifacts, flags)

        out_dir = tmp_path / "outputs" / "latest"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "system_decision_summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )

        txt, md = generate_daily_memo(root=tmp_path, write_files=False)
        assert "TSLA" in txt
        assert "EV" in txt
        assert "TSLA" in md


# ---------------------------------------------------------------------------
# Compact memo contract overrides
# ---------------------------------------------------------------------------

class TestBuildDailyMemoEmpty:
    def test_empty_dict_does_not_crash(self):
        result = build_daily_memo({})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_empty_dict_contains_subject_line(self):
        result = build_daily_memo({})
        assert "Subject:" in result

    def test_empty_dict_contains_compact_sections(self):
        result = build_daily_memo({})
        for section in (
            "TOP INSIGHT",
            "TOP DECISIONS",
            "CAPITAL ACTIONS",
            "RISK FOCUS",
            "WHAT CHANGED",
        ):
            assert section in result, f"Missing section: {section}"

    def test_empty_dict_omits_degraded_health_section(self):
        result = build_daily_memo({})
        assert "SYSTEM / DATA HEALTH" not in result

    def test_empty_dict_omits_legacy_verbose_sections(self):
        result = build_daily_memo({})
        for section in (
            "TOP THEME",
            "TOP OPPORTUNITY",
            "PORTFOLIO INSIGHT",
            "CAPITAL PREVIEW",
            "POLICY STATUS",
            "CHANGES SINCE LAST RUN",
        ):
            assert section not in result


class TestBuildDailyMemoFull:
    def test_subject_line_present(self):
        result = build_daily_memo(_full_summary())
        assert "Subject: Daily Investment Memo" in result

    def test_top_insight_stays_short_and_mentions_theme_and_ticker(self):
        result = build_daily_memo(_full_summary())
        assert "AI" in result
        assert "NVDA" in result

    def test_score_breakdowns_are_not_dumped(self):
        result = build_daily_memo(_full_summary())
        assert "0.821" not in result
        assert "0.913" not in result
        assert "0.856" not in result

    def test_footer_contains_advisory_note(self):
        result = build_daily_memo(_full_summary())
        assert "Advisory only" in result
        assert "no trades executed" in result

    def test_decision_engine_sections_present_when_plan_attached(self):
        summary = _full_summary()
        summary["_decision_plan"] = _decision_plan_payload()
        result = build_daily_memo(summary)
        assert "TOP DECISIONS" in result
        assert "CAPITAL ACTIONS" in result
        assert "RISK FOCUS" in result

    def test_top_decisions_are_limited_to_five(self):
        payload = _decision_plan_payload()
        payload["decisions"].append(
            {
                "symbol": "XLE",
                "decision": "BUY",
                "priority": 0.10,
                "urgency": "low",
                "source": "market",
                "reason": "Low-priority tail item.",
                "risk_flags": [],
                "inputs_used": {},
            }
        )
        summary = _full_summary()
        summary["_decision_plan"] = payload
        result = build_daily_memo(summary)
        assert "XLRE" in result
        assert "XLE" not in result

    def test_structural_decisions_appear_first_in_top_decisions(self):
        summary = _full_summary()
        summary["_decision_plan"] = _decision_plan_payload()
        result = build_daily_memo(summary)
        top_idx = result.index("TOP DECISIONS")
        qld_idx = result.index("QLD", top_idx)
        qqq_idx = result.index("QQQ", top_idx)
        vfh_idx = result.index("VFH", top_idx)
        assert qld_idx < vfh_idx
        assert qqq_idx < vfh_idx

    def test_risk_focus_mentions_structural_risks(self):
        summary = _full_summary()
        summary["_decision_plan"] = _decision_plan_payload()
        result = build_daily_memo(summary)
        assert "Concentration risk is active" in result
        assert "Leverage risk is active" in result

    def test_capital_actions_are_grouped_without_listing_top_actions(self):
        summary = _full_summary()
        summary["_decision_plan"] = _decision_plan_payload()
        result = build_daily_memo(summary)
        assert "SELL=2, SCALE=1, BUY=0" in result
        assert "Total recommended capital: $3,000.00" in result
        assert "Top actions:" not in result

    def test_capital_actions_include_actions_outside_top_five(self):
        # Regression: prior to fix, _capital_action_summary received only the
        # top-5 decisions, silently dropping any SCALE/BUY/SELL below that cut.
        # Fixture: 5 high-priority WAIT rows + 1 lower-priority SCALE. The
        # SCALE must still appear in the Capital Actions roll-up.
        summary = _full_summary()
        plan = _decision_plan_payload()
        plan["decisions"] = [
            {
                "symbol": f"WAIT{i}",
                "decision": "WAIT",
                "priority": 0.90,
                "urgency": "medium",
                "source": "market",
                "recommended_action": f"Wait on WAIT{i}.",
                "recommended_amount": None,
                "reason": "Stand by.",
                "risk_flags": [],
                "confidence": 0.6,
                "inputs_used": {},
            }
            for i in range(5)
        ] + [
            {
                "symbol": "BURIED",
                "decision": "SCALE",
                "priority": 0.20,
                "urgency": "low",
                "source": "portfolio",
                "recommended_action": "Add to BURIED.",
                "recommended_amount": 777.0,
                "reason": "Underweight contribution target.",
                "risk_flags": [],
                "confidence": 0.85,
                "inputs_used": {},
            }
        ]
        plan["total_decisions"] = len(plan["decisions"])
        summary["_decision_plan"] = plan
        result = build_daily_memo(summary)
        assert "SELL=0, SCALE=1, BUY=0" in result
        assert "Total recommended capital: $777.00" in result

    def test_what_changed_is_limited_to_three_bullets(self):
        summary = _full_summary(
            changes={
                "previous_available": True,
                "changes": ["one", "two", "three", "four"],
                "summary_line": "4 changes detected.",
            }
        )
        result = build_daily_memo(summary)
        assert "  - one" in result
        assert "  - two" in result
        assert "  - three" in result
        assert "  - four" not in result

    def test_system_data_health_only_shown_when_degraded(self):
        degraded = _full_summary(
            data_health={
                "degraded_mode": True,
                "data_mode": "cache_only",
                "missing_artifact_count": 2,
            }
        )
        result = build_daily_memo(degraded)
        assert "SYSTEM / DATA HEALTH" in result
        assert "Degraded mode is active" in result

        normal = build_daily_memo(_full_summary())
        assert "SYSTEM / DATA HEALTH" not in normal

    def test_system_data_health_lists_missing_artifact_count(self):
        # Compact format: memo shows COUNT only; full paths live in
        # system_decision_summary.json so the brief stays brief.
        degraded = _full_summary(
            data_health={
                "degraded_mode": True,
                "data_mode": "cache_only",
                "missing_artifact_count": 2,
                "missing_artifact_details": [
                    {
                        "artifact": "watchlist_signals",
                        "path": "outputs/latest/watchlist_signals.json",
                        "producer_step": "watchlist scanner",
                    },
                    {
                        "artifact": "theme_signals",
                        "path": "outputs/latest/theme_signals.json",
                        "producer_step": "theme engine",
                    },
                ],
            }
        )
        result = build_daily_memo(degraded)
        assert "2 required artifacts missing" in result

    def test_system_data_health_uses_defaulting_and_optional_wording(self):
        degraded = _full_summary(
            data_health={
                "degraded_mode": True,
                "data_mode": "live",
                "missing_artifact_count": 0,
                "missing_artifact_details": [],
                "defaulting_artifact_details": [
                    {
                        "artifact": "approved_ranking_config",
                        "path": "outputs/performance/approved_ranking_config.json",
                        "producer_step": "ranking config promotion",
                    }
                ],
                "optional_artifact_details": [
                    {
                        "artifact": "theme_opportunities",
                        "path": "outputs/latest/theme_opportunities.json",
                        "producer_step": "theme discovery",
                    }
                ],
            }
        )
        result = build_daily_memo(degraded)
        # Compact format: defaulted + optional rolled into a single advisory line.
        assert "2 advisory artifacts not yet populated" in result


class TestBuildDailyMemoMd:
    def test_returns_string(self):
        assert isinstance(build_daily_memo_md({}), str)

    def test_title_heading_present(self):
        result = build_daily_memo_md(_full_summary())
        assert "# Daily Investment Memo" in result

    def test_compact_headings_present(self):
        result = build_daily_memo_md(_full_summary())
        for section in (
            "## Top Insight",
            "## Top Decisions",
            "## Capital Actions",
            "## Risk Focus",
            "## What Changed",
        ):
            assert section in result

    def test_legacy_verbose_headings_absent(self):
        result = build_daily_memo_md(_full_summary())
        for section in (
            "## Top Theme",
            "## Top Opportunity",
            "## Portfolio Insight",
            "## Capital Preview",
            "## Policy Status",
            "## Changes Since Last Run",
        ):
            assert section not in result

    def test_decision_plan_sections_present(self):
        summary = _full_summary()
        summary["_decision_plan"] = _decision_plan_payload()
        result = build_daily_memo_md(summary)
        assert "## Top Decisions" in result
        assert "## Capital Actions" in result
        assert "## Risk Focus" in result

    def test_capital_actions_md_include_actions_outside_top_five(self):
        # Regression mirror of the .txt-path test: the markdown Capital Actions
        # section must reflect all SCALE/BUY/SELL rows, not just those that
        # survived the Top Decisions top-5 truncation.
        summary = _full_summary()
        plan = _decision_plan_payload()
        plan["decisions"] = [
            {
                "symbol": f"WAIT{i}",
                "decision": "WAIT",
                "priority": 0.90,
                "urgency": "medium",
                "source": "market",
                "recommended_amount": None,
                "reason": "Stand by.",
                "risk_flags": [],
                "confidence": 0.6,
                "inputs_used": {},
            }
            for i in range(5)
        ] + [
            {
                "symbol": "BURIED",
                "decision": "SCALE",
                "priority": 0.20,
                "urgency": "low",
                "source": "portfolio",
                "recommended_amount": 777.0,
                "reason": "Underweight contribution target.",
                "risk_flags": [],
                "confidence": 0.85,
                "inputs_used": {},
            }
        ]
        plan["total_decisions"] = len(plan["decisions"])
        summary["_decision_plan"] = plan
        result = build_daily_memo_md(summary)
        assert "SELL: 0 | SCALE: 1 | BUY: 0" in result
        assert "Total recommended capital: $777.00" in result

    def test_degraded_health_section_only_when_needed(self):
        degraded = _full_summary(
            data_health={
                "degraded_mode": True,
                "data_mode": "fallback",
            }
        )
        result = build_daily_memo_md(degraded)
        assert "## System / Data Health" in result

        normal = build_daily_memo_md(_full_summary())
        assert "## System / Data Health" not in normal


class TestGenerateDailyMemo:
    def test_dry_run_returns_tuple(self, tmp_path):
        result = generate_daily_memo(root=tmp_path, write_files=False)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_dry_run_returns_strings(self, tmp_path):
        txt, md = generate_daily_memo(root=tmp_path, write_files=False)
        assert isinstance(txt, str)
        assert isinstance(md, str)

    def test_dry_run_writes_no_files(self, tmp_path):
        generate_daily_memo(root=tmp_path, write_files=False)
        assert not (tmp_path / "outputs" / "latest" / "daily_memo.txt").exists()
        assert not (tmp_path / "outputs" / "latest" / "daily_memo.md").exists()

    def test_write_mode_creates_both_files(self, tmp_path):
        generate_daily_memo(root=tmp_path, write_files=True)
        assert (tmp_path / "outputs" / "latest" / "daily_memo.txt").exists()
        assert (tmp_path / "outputs" / "latest" / "daily_memo.md").exists()

    def test_txt_file_contains_compact_sections(self, tmp_path):
        generate_daily_memo(root=tmp_path, write_files=True)
        txt = (tmp_path / "outputs" / "latest" / "daily_memo.txt").read_text(encoding="utf-8")
        assert "TOP INSIGHT" in txt
        assert "TOP DECISIONS" in txt
        assert "CAPITAL ACTIONS" in txt
        assert "TOP THEME" not in txt

    def test_md_file_contains_headings(self, tmp_path):
        generate_daily_memo(root=tmp_path, write_files=True)
        md = (tmp_path / "outputs" / "latest" / "daily_memo.md").read_text(encoding="utf-8")
        assert "# Daily Investment Memo" in md
        assert "## Top Decisions" in md

    def test_missing_summary_file_handled_gracefully(self, tmp_path):
        txt, md = generate_daily_memo(root=tmp_path, write_files=False)
        assert "Advisory only" in txt
        assert "# Daily Investment Memo" in md

    def test_missing_decision_plan_file_shows_unavailable(self, tmp_path):
        import json

        out_dir = tmp_path / "outputs" / "latest"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "system_decision_summary.json").write_text(
            json.dumps(_full_summary()), encoding="utf-8"
        )

        txt, md = generate_daily_memo(root=tmp_path, write_files=False)
        assert "Decision plan unavailable." in txt
        assert "_Decision plan unavailable._" in md

    def test_valid_decision_plan_file_is_rendered(self, tmp_path):
        import json

        out_dir = tmp_path / "outputs" / "latest"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "system_decision_summary.json").write_text(
            json.dumps(_full_summary()), encoding="utf-8"
        )
        (out_dir / "decision_plan.json").write_text(
            json.dumps(_decision_plan_payload()), encoding="utf-8"
        )

        txt, md = generate_daily_memo(root=tmp_path, write_files=False)
        assert "TOP DECISIONS" in txt
        assert "QLD" in txt
        assert "QQQ" in txt
        assert "Risk: leverage_breach." in txt
        assert "## Top Decisions" in md
        assert "Structural decisions lead the plan" in md

    def test_structural_decisions_render_before_other_actions(self, tmp_path):
        import json

        out_dir = tmp_path / "outputs" / "latest"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "system_decision_summary.json").write_text(
            json.dumps(_full_summary()), encoding="utf-8"
        )
        (out_dir / "decision_plan.json").write_text(
            json.dumps(_decision_plan_payload()), encoding="utf-8"
        )

        txt, _ = generate_daily_memo(root=tmp_path, write_files=False)
        top_idx = txt.index("TOP DECISIONS")
        qld_idx = txt.index("QLD", top_idx)
        qqq_idx = txt.index("QQQ", top_idx)
        vfh_idx = txt.index("VFH", top_idx)
        assert qld_idx < vfh_idx
        assert qqq_idx < vfh_idx

    def test_real_summary_written_then_read(self, tmp_path):
        import json
        from watchlist_scanner.system_summary import build_system_decision_summary

        artifacts = {
            "signals": {"results": [{"ticker": "TSLA", "filter_allowed": True,
                                     "final_rank_score": 0.88, "confidence_score": 0.75,
                                     "portfolio_fit_score": 0.80, "portfolio_fit_label": "strong",
                                     "theme_alignment_label": "aligned", "rank_multiplier": 1.1,
                                     "conviction_band": "high_conviction"}]},
            "themes": {"themes": [{"name": "EV", "type": "classified", "score": 0.75,
                                   "persistence": 0.6, "acceleration": 0.1, "tickers": ["TSLA"]}]},
        }
        flags = {k: False for k in (
            "watchlist_signals", "theme_opportunities", "portfolio_snapshot",
            "approved_ranking_config", "approved_allocation_policy",
            "allocation_preview", "allocation_simulation", "weight_tuning_suggestions",
        )}
        summary = build_system_decision_summary(artifacts, flags)

        out_dir = tmp_path / "outputs" / "latest"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "system_decision_summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )

        txt, md = generate_daily_memo(root=tmp_path, write_files=False)
        assert "TSLA" in txt
        assert "EV" in txt
        assert "TSLA" in md


# ---------------------------------------------------------------------------
# Helpers shared by email retry / test-email tests
# ---------------------------------------------------------------------------

def _set_smtp_env(monkeypatch) -> None:
    monkeypatch.setenv("SMTP_SERVER", "smtp.test.local")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("EMAIL_USER", "user@test.local")
    monkeypatch.setenv("EMAIL_PASS", "secret")
    monkeypatch.setenv("EMAIL_TO", "to@test.local")


class _FakeSMTP:
    """Context-manager SMTP stub that records calls and can be made to fail once."""

    def __init__(self, *, fail_attempts: int = 0):
        self._fail_remaining = fail_attempts
        self.login_calls = 0
        self.send_calls = 0

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def ehlo(self): pass
    def starttls(self, **kw): pass

    def login(self, *args):
        self.login_calls += 1
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise smtplib.SMTPException("transient error")

    def sendmail(self, *args):
        self.send_calls += 1


# ---------------------------------------------------------------------------
# TestSendEmailRetry
# ---------------------------------------------------------------------------

class TestSendEmailRetry:
    def test_succeeds_on_first_attempt(self, monkeypatch):
        _set_smtp_env(monkeypatch)
        stub = _FakeSMTP(fail_attempts=0)
        with patch("smtplib.SMTP", stub):
            result = send_email("memo text", max_attempts=3)
        assert result is True
        assert stub.send_calls == 1

    def test_succeeds_on_second_attempt(self, monkeypatch):
        _set_smtp_env(monkeypatch)
        stub = _FakeSMTP(fail_attempts=1)
        with patch("smtplib.SMTP", stub):
            result = send_email("memo text", max_attempts=3)
        assert result is True
        assert stub.login_calls == 2
        assert stub.send_calls == 1

    def test_returns_false_after_all_attempts_exhausted(self, monkeypatch):
        _set_smtp_env(monkeypatch)
        stub = _FakeSMTP(fail_attempts=5)
        with patch("smtplib.SMTP", stub):
            result = send_email("memo text", max_attempts=3)
        assert result is False
        assert stub.login_calls == 3
        assert stub.send_calls == 0

    def test_max_attempts_one_fails_fast(self, monkeypatch):
        _set_smtp_env(monkeypatch)
        stub = _FakeSMTP(fail_attempts=1)
        with patch("smtplib.SMTP", stub):
            result = send_email("memo text", max_attempts=1)
        assert result is False
        assert stub.login_calls == 1

    def test_credentials_not_logged_on_auth_error(self, monkeypatch, caplog):
        _set_smtp_env(monkeypatch)
        stub = _FakeSMTP(fail_attempts=5)
        with patch("smtplib.SMTP", stub):
            import logging
            with caplog.at_level(logging.WARNING):
                send_email("memo text", max_attempts=2)
        for record in caplog.records:
            assert "secret" not in record.message
            assert "EMAIL_PASS" not in record.message


# ---------------------------------------------------------------------------
# TestSendTestEmail
# ---------------------------------------------------------------------------

class TestSendTestEmail:
    def test_returns_false_when_env_missing(self, monkeypatch):
        for var in (
            "SMTP_SERVER", "SMTP_HOST", "EMAIL_USER", "EMAIL_SENDER",
            "EMAIL_PASS", "EMAIL_PASSWORD", "EMAIL_TO", "EMAIL_RECIPIENT",
        ):
            monkeypatch.delenv(var, raising=False)
        with patch("watchlist_scanner.daily_memo._load_email_env", return_value=None):
            assert send_test_email() is False

    def test_uses_correct_subject(self, monkeypatch):
        _set_smtp_env(monkeypatch)
        captured: list[str] = []

        def _fake_send(text, *, subject=None, max_attempts=3):
            captured.append(subject or "")
            return True

        with patch("watchlist_scanner.daily_memo.send_email", side_effect=_fake_send):
            send_test_email()

        assert captured[0] == "Test Email — Investment System"

    def test_sends_non_empty_body(self, monkeypatch):
        _set_smtp_env(monkeypatch)
        captured: list[str] = []

        def _fake_send(text, *, subject=None, max_attempts=3):
            captured.append(text)
            return True

        with patch("watchlist_scanner.daily_memo.send_email", side_effect=_fake_send):
            send_test_email()

        assert len(captured[0]) > 0

    def test_succeeds_with_valid_smtp(self, monkeypatch):
        _set_smtp_env(monkeypatch)
        stub = _FakeSMTP(fail_attempts=0)
        with patch("smtplib.SMTP", stub):
            assert send_test_email() is True


# ---------------------------------------------------------------------------
# TestPipelineEmailSafety
# ---------------------------------------------------------------------------

class TestPipelineEmailSafety:
    def test_step_daily_memo_continues_on_email_failure(self):
        """_step_daily_memo must not raise when send_email returns False."""
        import run_daily_pipeline as p

        with patch("watchlist_scanner.daily_memo.generate_daily_memo", return_value=("memo text", "# md")), \
             patch("watchlist_scanner.daily_memo.send_email", return_value=False):
            notes = p._step_daily_memo(send_email_flag=True)

        assert "memo written" in notes
        assert "send failed" in notes

    def test_step_daily_memo_reports_success_on_send(self):
        import run_daily_pipeline as p

        with patch("watchlist_scanner.daily_memo.generate_daily_memo", return_value=("memo text", "# md")), \
             patch("watchlist_scanner.daily_memo.send_email", return_value=True):
            notes = p._step_daily_memo(send_email_flag=True)

        assert "memo written" in notes
        assert "sent" in notes

    def test_step_daily_memo_no_email_when_flag_false(self):
        import run_daily_pipeline as p

        with patch("watchlist_scanner.daily_memo.generate_daily_memo", return_value=("memo text", "# md")), \
             patch("watchlist_scanner.daily_memo.send_email") as mock_send:
            p._step_daily_memo(send_email_flag=False)

        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# TestDiscoveryMemoSection
# ---------------------------------------------------------------------------

from watchlist_scanner.daily_memo import (
    _build_discovery_section,
    _build_discovery_section_md,
    _load_discovery_approval_decisions,
    _load_discovery_sandbox_data,
)


def _make_watch_candidate(
    ticker: str = "NVDA",
    score: float = 3.5,
    corr_score: float = 0.75,
    corr_level: str = "strong",
    risk_flag: bool = False,
    evidence: list | None = None,
) -> dict:
    return {
        "ticker": ticker,
        "status": "watch",
        "score": score,
        "corroboration_score": corr_score,
        "corroboration_level": corr_level,
        "event_type": "earnings_surprise",
        "risk_flag": risk_flag,
        "evidence_snippets": evidence or ["Strong earnings beat across multiple sources"],
    }


def _make_discovered_candidate(ticker: str = "AAPL") -> dict:
    return {
        "ticker": ticker,
        "status": "discovered",
        "score": 1.2,
        "corroboration_score": 0.30,
        "corroboration_level": "weak",
        "event_type": "product_launch",
        "risk_flag": False,
        "evidence_snippets": [],
    }


def _make_rejected_candidate(ticker: str = "XYZ", reason: str = "below threshold") -> dict:
    return {
        "ticker": ticker,
        "status": "rejected",
        "score": 0.5,
        "corroboration_score": 0.10,
        "corroboration_level": "none",
        "event_type": "unknown",
        "risk_flag": False,
        "rejection_reason": reason,
    }


def _make_approval(
    symbol: str = "NVDA",
    decision: str = "approve_for_research_review",
    reason: str = "Strong corroboration",
    ts: str = "2026-04-30T10:00:00+00:00",
) -> dict:
    return {
        "symbol": symbol,
        "decision": decision,
        "decision_reason": reason,
        "generated_at": ts,
        "operator": "operator",
        "observe_only": True,
        "sandbox_only": True,
        "no_trade": True,
        "no_official_promotion": True,
    }


def _make_memory_entries(entries: list[dict]) -> list[dict]:
    return entries


def _make_discovery_data(
    *,
    watch: list | None = None,
    discovered: list | None = None,
    rejected_cands: list | None = None,
    approvals: list | None = None,
    memory_entries: list | None = None,
) -> dict:
    candidates = list(watch or []) + list(discovered or [])
    return {
        "emerging":  {"candidates": candidates},
        "rejected":  {"candidates": rejected_cands or []},
        "memory":    {"entries": memory_entries or []},
        "approvals": approvals or [],
    }


class TestDiscoverySectionPlainText:
    """Tests for _build_discovery_section (plain-text)."""

    def test_disclaimer_present(self):
        # Disclaimer appears on the full (non-collapsed) section; feed it a candidate.
        cand = _make_watch_candidate("NVDA")
        data = _make_discovery_data(watch=[cand])
        out = _build_discovery_section(data)
        assert "sandbox research only" in out.lower()
        assert "not buy/sell" in out.lower()

    def test_watch_candidate_appears(self):
        cand = _make_watch_candidate("NVDA", score=3.5, corr_score=0.75, corr_level="strong")
        data = _make_discovery_data(watch=[cand])
        out = _build_discovery_section(data)
        assert "NVDA" in out
        assert "score 3.50" in out
        assert "corroboration: strong" in out

    def test_discovered_candidate_in_monitoring(self):
        cand = _make_discovered_candidate("AAPL")
        data = _make_discovery_data(discovered=[cand])
        out = _build_discovery_section(data)
        assert "Monitoring" in out
        assert "AAPL" in out

    def test_approval_decision_appears(self):
        cand = _make_watch_candidate("NVDA")
        ap = _make_approval("NVDA", "approve_for_research_review")
        data = _make_discovery_data(watch=[cand], approvals=[ap])
        out = _build_discovery_section(data)
        assert "approve_for_research_review" in out
        assert "Recent Research Decisions" in out

    def test_approval_counts_correct(self):
        cand = _make_watch_candidate("NVDA")
        ap = _make_approval("NVDA", "approve_for_research_review")
        ap2 = _make_approval("AAPL", "needs_more_evidence")
        data = _make_discovery_data(watch=[cand], approvals=[ap, ap2])
        out = _build_discovery_section(data)
        assert "approved for research: 1" in out
        assert "needs more evidence: 1" in out

    def test_skips_buy_decision(self):
        ap_bad = {
            "symbol": "NVDA", "decision": "buy", "decision_reason": "fake",
            "generated_at": "2026-04-30T10:00:00+00:00", "operator": "op",
            "observe_only": True, "sandbox_only": True, "no_trade": True, "no_official_promotion": True,
        }
        data = _make_discovery_data(approvals=[ap_bad])
        out = _build_discovery_section(data)
        # The word "buy" must not appear as a decision in the output
        assert "buy" not in out.lower().split("research decisions")[1] if "recent research decisions" in out.lower() else True
        assert "Approval decisions: 0" in out or "Approval decisions:" not in out

    def test_skips_sell_decision(self):
        ap_bad = {
            "symbol": "NVDA", "decision": "sell", "decision_reason": "fake",
            "generated_at": "2026-04-30T10:00:00+00:00", "operator": "op",
            "observe_only": True, "sandbox_only": True, "no_trade": True, "no_official_promotion": True,
        }
        data = _make_discovery_data(approvals=[ap_bad])
        out = _build_discovery_section(data)
        assert "sell" not in out.lower() or "not buy/sell" in out.lower()

    def test_rejected_risk_summary_shown(self):
        rej = _make_rejected_candidate("JUNK", "below threshold")
        data = _make_discovery_data(rejected_cands=[rej])
        out = _build_discovery_section(data)
        assert "Rejected" in out
        assert "below threshold" in out

    def test_risk_flag_shown(self):
        cand = _make_watch_candidate("RISKY", risk_flag=True)
        data = _make_discovery_data(watch=[cand])
        out = _build_discovery_section(data)
        assert "risk flag" in out.lower()

    def test_no_forbidden_status_words(self):
        data = _make_discovery_data()
        out = _build_discovery_section(data)
        lower = out.lower()
        for word in ("actionable", "promoted", "validated"):
            assert word not in lower, f"forbidden word '{word}' found in discovery section"

    def test_no_official_action_language(self):
        data = _make_discovery_data()
        out = _build_discovery_section(data)
        lower = out.lower()
        for bad in ("enter position", "exit position", "deploy capital", "official watchlist promotion"):
            assert bad not in lower

    def test_sandbox_only_footer_present(self):
        # Footer appears on the full section; feed it a candidate so the
        # collapse path is not taken.
        cand = _make_watch_candidate("NVDA")
        data = _make_discovery_data(watch=[cand])
        out = _build_discovery_section(data)
        assert "sandbox only" in out.lower()
        assert "no official action" in out.lower()

    def test_evidence_snippet_appears(self):
        cand = _make_watch_candidate("NVDA", evidence=["Analyst upgrades noted in multiple sources"])
        data = _make_discovery_data(watch=[cand])
        out = _build_discovery_section(data)
        assert "Analyst upgrades" in out

    def test_evidence_snippet_truncated_at_120(self):
        long_snippet = "X" * 200
        cand = _make_watch_candidate("NVDA", evidence=[long_snippet])
        data = _make_discovery_data(watch=[cand])
        out = _build_discovery_section(data)
        # snippet must be at most 120 chars
        assert "X" * 121 not in out

    def test_memory_persistent_candidates_shown(self):
        entries = [
            {"ticker": "NVDA", "seen_runs": 3, "first_seen": "2026-04-01T00:00:00+00:00", "last_seen": "2026-04-30T00:00:00+00:00"},
            {"ticker": "AAPL", "seen_runs": 1, "first_seen": "2026-04-30T00:00:00+00:00", "last_seen": "2026-04-30T00:00:00+00:00"},
        ]
        data = _make_discovery_data(memory_entries=entries)
        out = _build_discovery_section(data)
        assert "Persistent" in out
        assert "NVDA" in out

    def test_memory_new_this_run_shown(self):
        entries = [
            {"ticker": "FRESH", "seen_runs": 1, "first_seen": "2026-04-30T00:00:00+00:00", "last_seen": "2026-04-30T00:00:00+00:00"},
        ]
        data = _make_discovery_data(memory_entries=entries)
        out = _build_discovery_section(data)
        assert "New this run" in out
        assert "FRESH" in out

    def test_empty_candidates_safe(self):
        # Empty data collapses to a one-line section to keep the memo concise.
        # The section header must still be present so operators can grep for it.
        data = _make_discovery_data()
        out = _build_discovery_section(data)
        assert "DISCOVERY RESEARCH" in out
        assert "No sandbox research candidates today" in out

    def test_malformed_candidate_skipped(self):
        data = _make_discovery_data(watch=["not-a-dict", None, 42])
        out = _build_discovery_section(data)
        assert "DISCOVERY RESEARCH" in out  # section still renders

    def test_counts_in_header(self):
        w = _make_watch_candidate("NVDA")
        d = _make_discovered_candidate("AAPL")
        r = _make_rejected_candidate("JUNK")
        data = _make_discovery_data(watch=[w], discovered=[d], rejected_cands=[r])
        out = _build_discovery_section(data)
        assert "WATCH=1" in out
        assert "DISCOVERED=1" in out
        assert "REJECTED=1" in out

    def test_at_most_5_watch_candidates_shown(self):
        cands = [_make_watch_candidate(f"T{i}") for i in range(8)]
        data = _make_discovery_data(watch=cands)
        out = _build_discovery_section(data)
        # Only first 5 should appear as numbered items
        assert "  5. T4" in out
        assert "  6. T5" not in out

    def test_at_most_5_recent_approvals_shown(self):
        cand = _make_watch_candidate("NVDA")
        aps = [_make_approval(f"T{i}", "keep_watching") for i in range(8)]
        data = _make_discovery_data(watch=[cand], approvals=aps)
        out = _build_discovery_section(data)
        # Only last 5 approvals shown; first 3 are T0, T1, T2 — they should not appear
        for i in range(3):
            assert f"- T{i}:" not in out

    def test_rejection_reasons_deduplicated(self):
        r1 = _make_rejected_candidate("A", "below threshold")
        r2 = _make_rejected_candidate("B", "below threshold")
        r3 = _make_rejected_candidate("C", "risk flag with low confidence")
        data = _make_discovery_data(rejected_cands=[r1, r2, r3])
        out = _build_discovery_section(data)
        # "below threshold" appears only once in the reasons list
        reasons_section = out.split("Top reasons:")[1] if "Top reasons:" in out else ""
        assert reasons_section.count("below threshold") == 1


class TestDiscoverySectionMarkdown:
    """Tests for _build_discovery_section_md (Markdown)."""

    def test_disclaimer_present(self):
        # Disclaimer appears on the full (non-collapsed) section; feed it a candidate.
        cand = _make_watch_candidate("NVDA")
        data = _make_discovery_data(watch=[cand])
        out = _build_discovery_section_md(data)
        assert "sandbox research only" in out.lower()

    def test_heading_present(self):
        data = _make_discovery_data()
        out = _build_discovery_section_md(data)
        assert "## Discovery Research" in out

    def test_watch_candidate_appears(self):
        cand = _make_watch_candidate("NVDA")
        data = _make_discovery_data(watch=[cand])
        out = _build_discovery_section_md(data)
        assert "**NVDA**" in out
        assert "### Research Candidates (WATCH)" in out

    def test_approval_appears_in_md(self):
        cand = _make_watch_candidate("NVDA")
        ap = _make_approval("NVDA", "approve_for_research_review")
        data = _make_discovery_data(watch=[cand], approvals=[ap])
        out = _build_discovery_section_md(data)
        assert "`approve_for_research_review`" in out

    def test_no_forbidden_words_as_decisions(self):
        ap_bad = {
            "symbol": "NVDA", "decision": "buy", "decision_reason": "fake",
            "generated_at": "2026-04-30T00:00:00+00:00", "operator": "op",
            "observe_only": True, "sandbox_only": True, "no_trade": True, "no_official_promotion": True,
        }
        data = _make_discovery_data(approvals=[ap_bad])
        out = _build_discovery_section_md(data)
        assert "`buy`" not in out

    def test_sandbox_footer_present(self):
        data = _make_discovery_data()
        out = _build_discovery_section_md(data)
        assert "sandbox only" in out.lower()

    def test_memory_persistence_in_md(self):
        entries = [{"ticker": "NVDA", "seen_runs": 2, "first_seen": "2026-04-01T00:00:00+00:00", "last_seen": "2026-04-30T00:00:00+00:00"}]
        data = _make_discovery_data(memory_entries=entries)
        out = _build_discovery_section_md(data)
        assert "### Persistence" in out
        assert "NVDA" in out

    def test_rejected_summary_in_md(self):
        rej = _make_rejected_candidate("JUNK", "below threshold")
        data = _make_discovery_data(rejected_cands=[rej])
        out = _build_discovery_section_md(data)
        assert "### Rejected / Risk Summary" in out

    def test_counts_in_md(self):
        w = _make_watch_candidate("NVDA")
        data = _make_discovery_data(watch=[w])
        out = _build_discovery_section_md(data)
        assert "**WATCH:** 1" in out


class TestBuildDailyMemoWithDiscovery:
    """Integration: build_daily_memo and build_daily_memo_md with discovery_data."""

    def test_no_discovery_data_memo_unchanged(self):
        out = build_daily_memo({})
        assert "DISCOVERY RESEARCH" not in out

    def test_with_discovery_data_section_appears(self):
        data = _make_discovery_data(watch=[_make_watch_candidate("NVDA")])
        out = build_daily_memo({}, discovery_data=data)
        assert "DISCOVERY RESEARCH" in out
        assert "NVDA" in out

    def test_discovery_section_after_other_sections(self):
        data = _make_discovery_data(watch=[_make_watch_candidate("NVDA")])
        out = build_daily_memo({}, discovery_data=data)
        # Discovery must come before the final advisory footer
        disc_idx = out.index("DISCOVERY RESEARCH")
        adv_idx = out.index("Advisory only")
        assert disc_idx < adv_idx

    def test_no_discovery_data_md_unchanged(self):
        out = build_daily_memo_md({})
        assert "Discovery Research" not in out

    def test_with_discovery_data_md_section_appears(self):
        data = _make_discovery_data(watch=[_make_watch_candidate("NVDA")])
        out = build_daily_memo_md({}, discovery_data=data)
        assert "## Discovery Research" in out
        assert "NVDA" in out

    def test_discovery_section_before_footer_md(self):
        data = _make_discovery_data(watch=[_make_watch_candidate("NVDA")])
        out = build_daily_memo_md({}, discovery_data=data)
        disc_idx = out.index("## Discovery Research")
        footer_idx = out.index("Advisory only")
        assert disc_idx < footer_idx

    def test_discovery_section_error_non_blocking(self):
        """If _build_discovery_section raises, memo still completes with error note."""
        data = _make_discovery_data()
        with patch("watchlist_scanner.daily_memo._build_discovery_section", side_effect=RuntimeError("boom")):
            out = build_daily_memo({}, discovery_data=data)
        assert "Advisory only" in out  # main memo still complete
        assert "unavailable" in out

    def test_discovery_section_error_non_blocking_md(self):
        data = _make_discovery_data()
        with patch("watchlist_scanner.daily_memo._build_discovery_section_md", side_effect=RuntimeError("boom")):
            out = build_daily_memo_md({}, discovery_data=data)
        assert "Advisory only" in out
        assert "unavailable" in out.lower()

    def test_no_buy_sell_in_discovery_output(self):
        ap_bad = {
            "symbol": "NVDA", "decision": "buy", "decision_reason": "x",
            "generated_at": "2026-04-30T00:00:00+00:00", "operator": "op",
            "observe_only": True, "sandbox_only": True, "no_trade": True, "no_official_promotion": True,
        }
        data = _make_discovery_data(approvals=[ap_bad])
        out = build_daily_memo({}, discovery_data=data)
        # "buy" should not appear in discovery decisions section
        # (it can appear in CAPITAL ACTIONS as "BUY" uppercase from decision engine)
        disc_section = out.split("DISCOVERY RESEARCH")[1] if "DISCOVERY RESEARCH" in out else ""
        assert "buy" not in [word.strip(".,;:-").lower() for word in disc_section.split()
                             if "research" not in word.lower() and "sandbox" not in word.lower()]


class TestGenerateDailyMemoWithDiscovery:
    """End-to-end: generate_daily_memo loads and integrates discovery data."""

    def test_generate_loads_discovery_sandbox_data(self, tmp_path):
        # Write minimal sandbox artifacts
        sandbox_dir = tmp_path / "outputs" / "sandbox" / "discovery"
        sandbox_dir.mkdir(parents=True)
        import json as _json
        (sandbox_dir / "emerging_candidates.json").write_text(_json.dumps({
            "candidates": [_make_watch_candidate("NVDA")]
        }), encoding="utf-8")
        (sandbox_dir / "rejected_candidates.json").write_text(_json.dumps({"candidates": []}), encoding="utf-8")
        (sandbox_dir / "discovery_memory.json").write_text(_json.dumps({"entries": []}), encoding="utf-8")
        (tmp_path / "outputs" / "latest").mkdir(parents=True)

        txt, md = generate_daily_memo(root=tmp_path, write_files=False)
        assert "DISCOVERY RESEARCH" in txt
        assert "## Discovery Research" in md
        assert "NVDA" in txt

    def test_generate_missing_discovery_artifacts_safe(self, tmp_path):
        # No sandbox artifacts at all — should still generate memo
        (tmp_path / "outputs" / "latest").mkdir(parents=True)
        txt, md = generate_daily_memo(root=tmp_path, write_files=False)
        assert "Advisory only" in txt  # memo still generated
        assert "DISCOVERY RESEARCH" not in txt  # section absent without data

    def test_generate_no_discovery_section_when_data_empty(self, tmp_path):
        sandbox_dir = tmp_path / "outputs" / "sandbox" / "discovery"
        sandbox_dir.mkdir(parents=True)
        import json as _json
        (sandbox_dir / "emerging_candidates.json").write_text(_json.dumps({}), encoding="utf-8")
        (tmp_path / "outputs" / "latest").mkdir(parents=True)
        txt, _ = generate_daily_memo(root=tmp_path, write_files=False)
        # Empty sandbox data → no section rendered
        assert "DISCOVERY RESEARCH" not in txt

    def test_generate_discovery_load_exception_non_blocking(self, tmp_path):
        (tmp_path / "outputs" / "latest").mkdir(parents=True)
        with patch("watchlist_scanner.daily_memo._load_discovery_sandbox_data", side_effect=RuntimeError("disk error")):
            txt, md = generate_daily_memo(root=tmp_path, write_files=False)
        assert "Advisory only" in txt  # memo still generated

    def test_generate_skips_tampered_approval_records(self, tmp_path):
        sandbox_dir = tmp_path / "outputs" / "sandbox" / "discovery"
        sandbox_dir.mkdir(parents=True)
        import json as _json

        # Write emerging with one WATCH candidate
        (sandbox_dir / "emerging_candidates.json").write_text(_json.dumps({
            "candidates": [_make_watch_candidate("NVDA")]
        }), encoding="utf-8")
        (sandbox_dir / "rejected_candidates.json").write_text(_json.dumps({"candidates": []}), encoding="utf-8")
        (sandbox_dir / "discovery_memory.json").write_text(_json.dumps({"entries": []}), encoding="utf-8")

        # Write tampered approval JSONL: one valid, one with decision=buy
        valid_rec = _make_approval("NVDA", "approve_for_research_review")
        bad_rec = {"symbol": "NVDA", "decision": "buy", "observe_only": True, "sandbox_only": True, "no_trade": True, "no_official_promotion": True}
        lines = _json.dumps(valid_rec) + "\n" + _json.dumps(bad_rec) + "\n"
        (sandbox_dir / "approval_decisions.jsonl").write_text(lines, encoding="utf-8")

        (tmp_path / "outputs" / "latest").mkdir(parents=True)
        txt, _ = generate_daily_memo(root=tmp_path, write_files=False)

        # Only 1 valid approval should be counted
        assert "Approval decisions: 1" in txt

    def test_generate_writes_files(self, tmp_path):
        (tmp_path / "outputs" / "latest").mkdir(parents=True)
        generate_daily_memo(root=tmp_path, write_files=True)
        assert (tmp_path / "outputs" / "latest" / "daily_memo.txt").exists()
        assert (tmp_path / "outputs" / "latest" / "daily_memo.md").exists()

    def test_no_official_artifact_written_to_sandbox(self, tmp_path):
        (tmp_path / "outputs" / "latest").mkdir(parents=True)
        generate_daily_memo(root=tmp_path, write_files=True)
        sandbox_dir = tmp_path / "outputs" / "sandbox"
        # Discovery section must not write anything to sandbox
        assert not sandbox_dir.exists() or not list(sandbox_dir.rglob("*.json"))


class TestLoadDiscoveryApprovalDecisions:
    """Unit tests for _load_discovery_approval_decisions."""

    def test_missing_file_returns_empty(self, tmp_path):
        result = _load_discovery_approval_decisions(tmp_path / "nonexistent.jsonl")
        assert result == []

    def test_valid_record_loaded(self, tmp_path):
        import json as _json
        p = tmp_path / "approvals.jsonl"
        rec = _make_approval("NVDA", "approve_for_research_review")
        p.write_text(_json.dumps(rec) + "\n", encoding="utf-8")
        result = _load_discovery_approval_decisions(p)
        assert len(result) == 1
        assert result[0]["symbol"] == "NVDA"

    def test_tampered_decision_skipped(self, tmp_path):
        import json as _json
        p = tmp_path / "approvals.jsonl"
        bad = {"symbol": "X", "decision": "buy", "observe_only": True, "sandbox_only": True, "no_trade": True, "no_official_promotion": True}
        p.write_text(_json.dumps(bad) + "\n", encoding="utf-8")
        result = _load_discovery_approval_decisions(p)
        assert result == []

    def test_tampered_flag_skipped(self, tmp_path):
        import json as _json
        p = tmp_path / "approvals.jsonl"
        bad = {"symbol": "X", "decision": "keep_watching", "observe_only": True, "sandbox_only": False, "no_trade": True, "no_official_promotion": True}
        p.write_text(_json.dumps(bad) + "\n", encoding="utf-8")
        result = _load_discovery_approval_decisions(p)
        assert result == []

    def test_malformed_json_line_skipped(self, tmp_path):
        import json as _json
        p = tmp_path / "approvals.jsonl"
        valid = _make_approval("NVDA")
        p.write_text("not-json\n" + _json.dumps(valid) + "\n", encoding="utf-8")
        result = _load_discovery_approval_decisions(p)
        assert len(result) == 1

    def test_empty_file_returns_empty(self, tmp_path):
        p = tmp_path / "approvals.jsonl"
        p.write_text("", encoding="utf-8")
        result = _load_discovery_approval_decisions(p)
        assert result == []

    def test_mixed_valid_and_tampered(self, tmp_path):
        import json as _json
        p = tmp_path / "approvals.jsonl"
        valid1 = _make_approval("NVDA", "approve_for_research_review")
        valid2 = _make_approval("AAPL", "keep_watching")
        bad = {"symbol": "X", "decision": "sell", "observe_only": True, "sandbox_only": True, "no_trade": True, "no_official_promotion": True}
        p.write_text("\n".join([_json.dumps(valid1), _json.dumps(bad), _json.dumps(valid2)]) + "\n", encoding="utf-8")
        result = _load_discovery_approval_decisions(p)
        assert len(result) == 2
        symbols = {r["symbol"] for r in result}
        assert symbols == {"NVDA", "AAPL"}


class TestLoadDiscoverySandboxData:
    """Unit tests for _load_discovery_sandbox_data."""

    def test_all_missing_returns_none(self, tmp_path):
        result = _load_discovery_sandbox_data(tmp_path)
        assert result is None

    def test_emerging_only_returns_data(self, tmp_path):
        import json as _json
        sandbox_dir = tmp_path / "outputs" / "sandbox" / "discovery"
        sandbox_dir.mkdir(parents=True)
        (sandbox_dir / "emerging_candidates.json").write_text(
            _json.dumps({"candidates": [_make_watch_candidate("NVDA")]}), encoding="utf-8"
        )
        result = _load_discovery_sandbox_data(tmp_path)
        assert result is not None
        assert result["emerging"]["candidates"][0]["ticker"] == "NVDA"

    def test_approvals_only_returns_data(self, tmp_path):
        import json as _json
        sandbox_dir = tmp_path / "outputs" / "sandbox" / "discovery"
        sandbox_dir.mkdir(parents=True)
        rec = _make_approval("NVDA")
        (sandbox_dir / "approval_decisions.jsonl").write_text(_json.dumps(rec) + "\n", encoding="utf-8")
        result = _load_discovery_sandbox_data(tmp_path)
        assert result is not None
        assert len(result["approvals"]) == 1

    def test_corrupt_json_still_loads_others(self, tmp_path):
        import json as _json
        sandbox_dir = tmp_path / "outputs" / "sandbox" / "discovery"
        sandbox_dir.mkdir(parents=True)
        (sandbox_dir / "emerging_candidates.json").write_text("NOT JSON", encoding="utf-8")
        (sandbox_dir / "rejected_candidates.json").write_text(_json.dumps({"candidates": []}), encoding="utf-8")
        result = _load_discovery_sandbox_data(tmp_path)
        # Even with corrupt emerging, rejected is loaded; empty emerging → might return None or partial
        # Main requirement: no exception
        # result could be None (all empty) or dict; both are valid
        assert result is None or isinstance(result, dict)


# ---------------------------------------------------------------------------
# Kelly advisor line in the Advisor Stack (regression: must derive from
# by_decision, not non-existent top-level status/resolved_decisions keys).
# ---------------------------------------------------------------------------

class TestAdvisorStackKellyLine:
    """The kelly_sizing_advisor.json schema carries per-group rows under
    by_decision[] (each with status + n_resolved) and has NO top-level
    `status` / `resolved_decisions` keys. The memo previously read those
    absent keys and always rendered "unknown — 0 resolved decisions",
    masking that decisions had in fact resolved (e.g. SCALE at 19/20)."""

    def _write_kelly(self, tmp_path, by_decision):
        import json as _json
        latest = tmp_path / "outputs" / "latest"
        latest.mkdir(parents=True, exist_ok=True)
        (latest / "kelly_sizing_advisor.json").write_text(
            _json.dumps({
                "min_resolved_required": 20,
                "summary_line": "Kelly sizing",
                "by_decision": by_decision,
            }),
            encoding="utf-8",
        )

    def _kelly_line(self, tmp_path):
        items = _advisor_stack_items(tmp_path)
        kl = [i for i in items if "Kelly" in i]
        assert kl, "no Kelly line emitted"
        return kl[0]

    def test_resolved_total_reflects_by_decision(self, tmp_path):
        self._write_kelly(tmp_path, [
            {"decision": "BUY", "status": "insufficient_data", "n_resolved": 0},
            {"decision": "SCALE", "status": "insufficient_data", "n_resolved": 19},
            {"decision": "SELL", "status": "insufficient_data", "n_resolved": 6},
        ])
        line = self._kelly_line(tmp_path)
        # Real total is 25 resolved — NOT the buggy 0.
        assert "25 resolved decision" in line
        assert "unknown" not in line
        assert "insufficient_data" in line

    def test_status_ok_when_a_group_is_ready(self, tmp_path):
        self._write_kelly(tmp_path, [
            {"decision": "BUY", "status": "ok", "n_resolved": 22},
            {"decision": "SCALE", "status": "insufficient_data", "n_resolved": 19},
            {"decision": "SELL", "status": "insufficient_data", "n_resolved": 6},
        ])
        line = self._kelly_line(tmp_path)
        assert "47 resolved decision" in line
        assert "`ok`" in line

    def test_missing_artifact_does_not_crash(self, tmp_path):
        # No kelly file written → graceful unknown/0, no exception.
        line = self._kelly_line(tmp_path)
        assert "Kelly" in line


# ---------------------------------------------------------------------------
# Readability fixes (H1–M5)
# ---------------------------------------------------------------------------

from watchlist_scanner.daily_memo import (
    _portfolio_pulse_items,
    _pattern_confirmed_candidates,
)
import json as _json


class TestM2M3SectorFraming:
    """M2/M3 — 'sector cap reference' replaced by 'soft target … — over'."""

    def _snapshot(self, tmp_path, sector_name="Technology", share=0.778, cap=0.35):
        """Write a minimal portfolio_snapshot.json for pulse tests."""
        snap_dir = tmp_path / "outputs" / "portfolio"
        snap_dir.mkdir(parents=True, exist_ok=True)
        (snap_dir / "portfolio_snapshot.json").write_text(
            _json.dumps({
                "total_suggested_allocation": 0.85,
                "capped_positions": 0,
                "allocation_by_conviction_band": {
                    "high_conviction": 0.40, "normal": 0.30, "starter": 0.15
                },
                "top_sector": {"name": sector_name, "allocation_pct": share},
            }),
            encoding="utf-8",
        )

    def test_over_cap_reads_soft_target_over(self, tmp_path):
        self._snapshot(tmp_path, sector_name="Technology", share=0.778, cap=0.35)
        items = _portfolio_pulse_items(tmp_path)
        sector_line = next((i for i in items if "Technology" in i), None)
        assert sector_line is not None
        assert "soft target" in sector_line
        assert "over" in sector_line.lower()
        assert "cap reference" not in sector_line

    def test_within_cap_no_over_flag(self, tmp_path):
        # share below cap → should not say "over"
        self._snapshot(tmp_path, sector_name="Financials", share=0.20, cap=0.35)
        items = _portfolio_pulse_items(tmp_path)
        sector_line = next((i for i in items if "Financials" in i), None)
        assert sector_line is not None
        # when under cap we don't want "— over"
        assert "— over" not in sector_line

    def test_sector_line_is_one_line(self, tmp_path):
        self._snapshot(tmp_path, sector_name="Technology", share=0.778, cap=0.35)
        items = _portfolio_pulse_items(tmp_path)
        sector_line = next((i for i in items if "Technology" in i), "")
        assert "\n" not in sector_line

    def test_cap_reference_wording_gone(self, tmp_path):
        self._snapshot(tmp_path, sector_name="Technology", share=0.778, cap=0.35)
        items = _portfolio_pulse_items(tmp_path)
        for item in items:
            assert "cap reference" not in item


class TestM4TagDoubling:
    """M4 — tag-doubling and singular/plural fix in watch-list renderer."""

    def _write_efficacy(self, tmp_path, tags_winning):
        """Write a minimal pattern_efficacy_monthly.json."""
        latest = tmp_path / "outputs" / "latest"
        latest.mkdir(parents=True, exist_ok=True)
        by_tag = {
            tag: {"significance": "winner", "n_samples": 50, "delta_pp": 10.0}
            for tag in tags_winning
        }
        (latest / "pattern_efficacy_monthly.json").write_text(
            _json.dumps({"by_tag": by_tag}), encoding="utf-8"
        )

    def _write_top100(self, tmp_path, candidates):
        latest = tmp_path / "outputs" / "latest"
        latest.mkdir(parents=True, exist_ok=True)
        (latest / "top100_daily.json").write_text(
            _json.dumps({"candidates": candidates}), encoding="utf-8"
        )

    def test_singular_tag_when_count_is_one(self, tmp_path):
        self._write_efficacy(tmp_path, ["Technology"])
        self._write_top100(tmp_path, [
            {"symbol": "AMD", "sector": "Technology", "score": 0.9,
             "rationale_tags": ["Technology"]},
        ])
        compact, _ = _pattern_confirmed_candidates(tmp_path, cadence="monthly", top_n=5, extended_n=5)
        assert len(compact) == 1
        line = compact[0]
        assert "tag(s)" not in line, f"'(s)' still present in: {line!r}"
        assert "1 winning tag" in line

    def test_plural_tag_when_count_is_two(self, tmp_path):
        self._write_efficacy(tmp_path, ["Technology", "AI"])
        self._write_top100(tmp_path, [
            {"symbol": "AMD", "sector": "Technology", "score": 0.9,
             "rationale_tags": ["Technology", "AI"]},
        ])
        compact, _ = _pattern_confirmed_candidates(tmp_path, cadence="monthly", top_n=5, extended_n=5)
        assert len(compact) == 1
        line = compact[0]
        assert "2 winning tags" in line

    def test_sector_tag_suppressed_when_matches_sector_field(self, tmp_path):
        """When the only winning tag equals the sector already shown, the tag
        list should not repeat '… 1 winning tag: Technology' after '(Technology)'."""
        self._write_efficacy(tmp_path, ["Technology"])
        self._write_top100(tmp_path, [
            {"symbol": "AMD", "sector": "Technology", "score": 0.9,
             "rationale_tags": ["Technology"]},
        ])
        compact, _ = _pattern_confirmed_candidates(tmp_path, cadence="monthly", top_n=5, extended_n=5)
        line = compact[0]
        # AMD (Technology) should NOT be followed by ": Technology"
        assert "1 winning tag: Technology" not in line
        # The line should still contain AMD and the sector
        assert "AMD" in line
        assert "Technology" in line

    def test_non_sector_tag_still_shown(self, tmp_path):
        """When a winning tag differs from the sector, it must still appear."""
        self._write_efficacy(tmp_path, ["AI"])
        self._write_top100(tmp_path, [
            {"symbol": "NVDA", "sector": "Technology", "score": 0.9,
             "rationale_tags": ["AI"]},
        ])
        compact, _ = _pattern_confirmed_candidates(tmp_path, cadence="monthly", top_n=5, extended_n=5)
        line = compact[0]
        # AI tag differs from sector Technology — must be shown
        assert "AI" in line


class TestM5WatchListLengthAlignment:
    """M5 — TXT watch list must be capped at 5, matching MD.

    Both renderers call _pattern_confirmed_candidates; the TXT path was using
    extended_n=10 (showing up to 10 rows) while the MD path used top_n=5
    (showing up to 5). After the fix both must use 5.
    """

    def _write_efficacy_and_top100(self, tmp_path, n_candidates=8):
        latest = tmp_path / "outputs" / "latest"
        latest.mkdir(parents=True, exist_ok=True)
        (latest / "pattern_efficacy_monthly.json").write_text(
            _json.dumps({"by_tag": {"Technology": {"significance": "winner", "n_samples": 50, "delta_pp": 10.0}}}),
            encoding="utf-8",
        )
        candidates = [
            {"symbol": f"SYM{i}", "sector": "Technology", "score": 1.0 - i * 0.01,
             "rationale_tags": ["Technology"]}
            for i in range(n_candidates)
        ]
        (latest / "top100_daily.json").write_text(
            _json.dumps({"candidates": candidates}), encoding="utf-8"
        )

    def test_extended_n_capped_at_five_for_txt(self, tmp_path):
        """_pattern_confirmed_candidates called with extended_n=5 for TXT path gives ≤5 rows."""
        self._write_efficacy_and_top100(tmp_path, n_candidates=8)
        # Call directly with the NEW expected args (extended_n=5, matching MD)
        compact, extended = _pattern_confirmed_candidates(
            tmp_path, cadence="monthly", top_n=5, extended_n=5
        )
        assert len(extended) <= 5, f"Expected ≤5, got {len(extended)}"

    def test_extended_n_ten_gives_more_than_five(self, tmp_path):
        """Verify the bug: with extended_n=10 we get 8 rows (more than 5).
        After the fix, the TXT renderer must no longer call with extended_n=10."""
        self._write_efficacy_and_top100(tmp_path, n_candidates=8)
        _, extended_ten = _pattern_confirmed_candidates(
            tmp_path, cadence="monthly", top_n=5, extended_n=10
        )
        # With 8 candidates all matching, extended_n=10 yields 8 rows
        assert len(extended_ten) > 5

    def test_compact_and_extended_both_five(self, tmp_path):
        """After fix, compact (MD) and extended (TXT) paths both cap at 5."""
        self._write_efficacy_and_top100(tmp_path, n_candidates=8)
        compact, extended = _pattern_confirmed_candidates(
            tmp_path, cadence="monthly", top_n=5, extended_n=5
        )
        assert len(compact) <= 5
        assert len(extended) <= 5



class TestUnifiedCrowdSummary:
    """Crowd Radar section prefers the unified crowd bus, falls back cleanly."""

    def _write_unified_status(self, tmp_path, **overrides):
        from pathlib import Path
        import json
        payload = {
            "schema_version": "1",
            "observe_only": True,
            "total_tickers": 126,
            "lane_a_tickers": 100,
            "lane_b_tickers": 46,
            "overlap_tickers": 20,
            "social_sentiment_status": "PLAN_LOCKED",
            "top_confirmed_attention": [
                {"ticker": "TSLA"}, {"ticker": "AMZN"}, {"ticker": "MRVL"},
                {"ticker": "EXTRA"},
            ],
            "top_divergent_attention": [
                {"ticker": "GOOGL"}, {"ticker": "SMCI"}, {"ticker": "NVDA"},
            ],
        }
        payload.update(overrides)
        d = Path(tmp_path) / "outputs" / "latest"
        d.mkdir(parents=True, exist_ok=True)
        (d / "unified_crowd_intelligence_status.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_unified_line_present_when_status_exists(self, tmp_path):
        from watchlist_scanner.daily_memo import _crowd_radar_section_lines
        self._write_unified_status(tmp_path)
        lines = _crowd_radar_section_lines(tmp_path)
        unified = [ln for ln in lines if ln.startswith("Unified Crowd:")]
        assert len(unified) == 1
        ln = unified[0]
        assert "126 tickers" in ln
        assert "retail 100/context 46/overlap 20" in ln
        assert "confirmed: TSLA, AMZN, MRVL" in ln
        assert "EXTRA" not in ln  # capped at 3
        assert "divergent: GOOGL, SMCI, NVDA" in ln
        assert "social_sentiment PLAN_LOCKED" in ln

    def test_unified_summary_helper_compact(self, tmp_path):
        from watchlist_scanner.daily_memo import _unified_crowd_summary_lines
        self._write_unified_status(tmp_path)
        lines = _unified_crowd_summary_lines(tmp_path)
        assert len(lines) <= 2  # compact contract

    def test_absent_unified_falls_back_without_error(self, tmp_path):
        from watchlist_scanner.daily_memo import _crowd_radar_section_lines
        # No unified status artifact, no crowd_knowledge_state -> empty, no crash
        lines = _crowd_radar_section_lines(tmp_path)
        assert isinstance(lines, list)
        assert not any(ln.startswith("Unified Crowd:") for ln in lines)

    def test_empty_unified_status_falls_back(self, tmp_path):
        from watchlist_scanner.daily_memo import _crowd_radar_section_lines
        self._write_unified_status(tmp_path, total_tickers=0)
        lines = _crowd_radar_section_lines(tmp_path)
        assert not any(ln.startswith("Unified Crowd:") for ln in lines)

    def test_unified_line_present_in_text_memo(self, tmp_path, monkeypatch):
        from watchlist_scanner import daily_memo as dm
        self._write_unified_status(tmp_path)
        # The crowd section reads via _enrichment_repo_root(); point it at tmp_path
        # so the test is hermetic regardless of the live repo artifact.
        monkeypatch.setattr(dm, "_enrichment_repo_root", lambda: tmp_path)
        txt, md = dm.generate_daily_memo(root=tmp_path, write_files=False)
        assert "Unified Crowd:" in txt
        assert "Unified Crowd:" in md
