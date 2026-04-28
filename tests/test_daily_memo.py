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
)


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


# ---------------------------------------------------------------------------
# TestSendEmail
# ---------------------------------------------------------------------------

class TestSendEmail:
    def test_returns_false_when_env_vars_missing(self, monkeypatch):
        for var in ("SMTP_SERVER", "EMAIL_USER", "EMAIL_PASS", "EMAIL_TO"):
            monkeypatch.delenv(var, raising=False)
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
        for var in ("SMTP_SERVER", "EMAIL_USER", "EMAIL_PASS", "EMAIL_TO"):
            monkeypatch.delenv(var, raising=False)
        memo = "Subject: Daily Investment Memo — 2026-04-27\n\nBody."
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
        for var in ("SMTP_SERVER", "EMAIL_USER", "EMAIL_PASS", "EMAIL_TO"):
            monkeypatch.delenv(var, raising=False)
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
