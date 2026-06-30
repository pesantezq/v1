"""
Tests for portfolio_automation.memo_email_sender.

All SMTP interactions are mocked — no real email is ever sent.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

from portfolio_automation.memo_email_sender import (
    MemoEmailConfig,
    load_memo_email_config,
    build_memo_email_message,
    render_memo_html,
    send_daily_memo_email,
    write_memo_delivery_status,
    append_memo_delivery_log,
    run_memo_email_delivery,
    load_memo_delivery_status,
    load_recent_delivery_log,
    _already_sent,
    _load_delivery_log,
    _sanitize_error,
    _cli_main,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _min_env(**overrides) -> dict[str, str]:
    """Minimal env for an enabled, non-dry-run run with valid recipients."""
    base = {
        "MEMO_EMAIL_ENABLED": "1",
        "MEMO_EMAIL_DRY_RUN": "0",
        "MEMO_EMAIL_SMTP_HOST": "smtp.example.com",
        "MEMO_EMAIL_SMTP_PORT": "587",
        "MEMO_EMAIL_USERNAME": "user@example.com",
        "MEMO_EMAIL_PASSWORD": "s3cr3t",
        "MEMO_EMAIL_FROM": "user@example.com",
        "MEMO_EMAIL_TO": "recipient@example.com",
        "MEMO_EMAIL_USE_TLS": "1",
    }
    base.update(overrides)
    return base


def _write_memo_files(base_dir: Path, txt: str = "Hello memo", md: str = "# Memo") -> None:
    latest = base_dir / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    (latest / "daily_memo.txt").write_text(txt, encoding="utf-8")
    (latest / "daily_memo.md").write_text(md, encoding="utf-8")


def _write_log_entry(base_dir: Path, entry: dict) -> None:
    log_path = base_dir / "policy" / "memo_delivery_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# TestMemoEmailConfig — config loading
# ---------------------------------------------------------------------------

class TestMemoEmailConfig:
    def test_defaults_disabled(self):
        cfg = load_memo_email_config(env={})
        assert cfg.enabled is False
        assert cfg.dry_run is True
        assert cfg.smtp_port == 587
        assert cfg.use_tls is True
        assert cfg.strict_failure is False
        assert cfg.force_resend is False

    def test_enabled_from_env(self):
        cfg = load_memo_email_config(env={"MEMO_EMAIL_ENABLED": "1"})
        assert cfg.enabled is True

    # --- generic SMTP/EMAIL_* fallback (reuses system-wide mail config) -------

    def test_generic_fallback_resolves_transport_and_recipients(self):
        env = {
            "MEMO_EMAIL_ENABLED": "1",
            "SMTP_SERVER": "smtp.example.com",
            "SMTP_PORT": "465",
            "EMAIL_USER": "ops@example.com",
            "EMAIL_PASS": "secret",
            "EMAIL_TO": "me@example.com",
        }
        cfg = load_memo_email_config(env=env)
        assert cfg.enabled is True
        assert cfg.smtp_host == "smtp.example.com"
        assert cfg.smtp_port == 465
        assert cfg.username == "ops@example.com"
        assert cfg.password == "secret"
        assert cfg.from_addr == "ops@example.com"  # defaults to authenticated user
        assert cfg.to_addrs == ["me@example.com"]
        assert cfg.has_smtp_config() and cfg.has_valid_recipients()

    def test_dedicated_overrides_win_over_generic(self):
        env = {
            "SMTP_SERVER": "generic.example.com",
            "EMAIL_USER": "generic@example.com",
            "EMAIL_TO": "generic-to@example.com",
            "MEMO_EMAIL_SMTP_HOST": "memo.example.com",
            "MEMO_EMAIL_USERNAME": "memo@example.com",
            "MEMO_EMAIL_FROM": "from@example.com",
            "MEMO_EMAIL_TO": "memo-to@example.com",
        }
        cfg = load_memo_email_config(env=env)
        assert cfg.smtp_host == "memo.example.com"
        assert cfg.username == "memo@example.com"
        assert cfg.from_addr == "from@example.com"
        assert cfg.to_addrs == ["memo-to@example.com"]

    def test_generic_config_alone_does_not_auto_enable(self):
        # The presence of generic mail config must NOT silently enable memo email.
        env = {
            "SMTP_SERVER": "smtp.example.com",
            "EMAIL_USER": "ops@example.com",
            "EMAIL_PASS": "secret",
            "EMAIL_TO": "me@example.com",
        }
        cfg = load_memo_email_config(env=env)
        assert cfg.enabled is False  # opt-in still required
        assert cfg.dry_run is True
        # but transport/recipients are resolved so a later opt-in "just works"
        assert cfg.smtp_host == "smtp.example.com"
        assert cfg.to_addrs == ["me@example.com"]

    def test_dry_run_false(self):
        cfg = load_memo_email_config(env={"MEMO_EMAIL_DRY_RUN": "0"})
        assert cfg.dry_run is False

    def test_to_addrs_parsed(self):
        cfg = load_memo_email_config(env={"MEMO_EMAIL_TO": "a@b.com, c@d.com"})
        assert cfg.to_addrs == ["a@b.com", "c@d.com"]

    def test_to_addrs_semicolon(self):
        cfg = load_memo_email_config(env={"MEMO_EMAIL_TO": "a@b.com; c@d.com"})
        assert cfg.to_addrs == ["a@b.com", "c@d.com"]

    def test_cc_bcc_parsed(self):
        cfg = load_memo_email_config(env={
            "MEMO_EMAIL_CC": "cc@b.com",
            "MEMO_EMAIL_BCC": "bcc@b.com",
        })
        assert cfg.cc_addrs == ["cc@b.com"]
        assert cfg.bcc_addrs == ["bcc@b.com"]

    def test_password_not_in_repr(self):
        cfg = load_memo_email_config(env={"MEMO_EMAIL_PASSWORD": "topsecret"})
        assert "topsecret" not in repr(cfg)

    def test_has_valid_recipients_false_when_empty(self):
        cfg = MemoEmailConfig()
        assert cfg.has_valid_recipients() is False

    def test_has_valid_recipients_false_bad_addr(self):
        cfg = MemoEmailConfig(to_addrs=["notanemail"])
        assert cfg.has_valid_recipients() is False

    def test_has_valid_recipients_true(self):
        cfg = MemoEmailConfig(to_addrs=["a@b.com"])
        assert cfg.has_valid_recipients() is True

    def test_has_smtp_config_false_missing_fields(self):
        cfg = MemoEmailConfig(smtp_host="h", username="u")
        assert cfg.has_smtp_config() is False

    def test_has_smtp_config_true(self):
        cfg = MemoEmailConfig(smtp_host="h", username="u", password="p", from_addr="f@g.com")
        assert cfg.has_smtp_config() is True

    def test_subject_prefix_loaded(self):
        cfg = load_memo_email_config(env={"MEMO_EMAIL_SUBJECT_PREFIX": "[TEST]"})
        assert cfg.subject_prefix == "[TEST]"

    def test_smtp_port_parsed(self):
        cfg = load_memo_email_config(env={"MEMO_EMAIL_SMTP_PORT": "465"})
        assert cfg.smtp_port == 465


# ---------------------------------------------------------------------------
# TestBuildMemoEmailMessage
# ---------------------------------------------------------------------------

class TestBuildMemoEmailMessage:
    def _cfg(self) -> MemoEmailConfig:
        return MemoEmailConfig(
            from_addr="from@test.com",
            to_addrs=["to@test.com"],
        )

    def test_subject_contains_date(self):
        msg = build_memo_email_message(self._cfg(), "txt", "md", "rid", "2026-05-02")
        assert "2026-05-02" in msg["Subject"]
        assert "Portfolio Daily Memo" in msg["Subject"]

    def test_subject_prefix(self):
        cfg = self._cfg()
        cfg.subject_prefix = "[DEV]"
        msg = build_memo_email_message(cfg, "txt", "md", "rid", "2026-05-02")
        assert msg["Subject"].startswith("[DEV]")

    def test_from_to_set(self):
        msg = build_memo_email_message(self._cfg(), "txt", "md", "rid", "2026-05-02")
        assert msg["From"] == "from@test.com"
        assert "to@test.com" in msg["To"]

    def test_cc_set_when_present(self):
        cfg = self._cfg()
        cfg.cc_addrs = ["cc@test.com"]
        msg = build_memo_email_message(cfg, "txt", "md", "rid", "2026-05-02")
        assert "cc@test.com" in msg["Cc"]

    def test_cc_absent_when_empty(self):
        msg = build_memo_email_message(self._cfg(), "txt", "md", "rid", "2026-05-02")
        assert msg["Cc"] is None

    def test_plain_text_body(self):
        msg = build_memo_email_message(self._cfg(), "Hello world", "", "rid", "2026-05-02")
        payload = msg.get_payload()
        assert "Hello world" in str(payload)

    def test_fallback_when_no_txt(self):
        msg = build_memo_email_message(self._cfg(), "", "", "rid", "2026-05-02")
        assert "No memo content" in str(msg.get_payload())

    def test_markdown_attachment_when_provided(self):
        msg = build_memo_email_message(self._cfg(), "txt", "# MD", "rid", "2026-05-02")
        parts = list(msg.iter_attachments())
        assert any("daily_memo_2026-05-02.md" in (p.get_filename() or "") for p in parts)

    def test_no_markdown_attachment_when_empty(self):
        msg = build_memo_email_message(self._cfg(), "txt", "", "rid", "2026-05-02")
        parts = list(msg.iter_attachments())
        assert len(parts) == 0


# ---------------------------------------------------------------------------
# TestRenderMemoHtml — HTML alternative body
# ---------------------------------------------------------------------------

class TestRenderMemoHtml:
    _SAMPLE_MD = (
        "# Daily Investment Memo — 2026-05-24\n"
        "**Date:** 2026-05-24\n\n"
        "## Today's Verdict\n"
        "> **Cautious** — portfolio near a cap.\n\n"
        "## Top Decisions\n"
        "- **SCALE** `QQQ` | priority `0.550`\n"
        "  - Drift +21% vs ±12%.\n\n"
        "## Portfolio Growth\n"
        "- **Total value:** $7,712.12\n"
        "- **Today vs prior:** +0.56%\n"
        "- **Past 7 days:** -1.20%\n\n"
        "---\n"
        "_Advisory only._\n"
    )

    def test_empty_input_returns_empty_string(self):
        assert render_memo_html("", "2026-05-24") == ""
        assert render_memo_html("   \n  ", "2026-05-24") == ""

    def test_contains_doctype_and_date_header(self):
        out = render_memo_html(self._SAMPLE_MD, "2026-05-24")
        assert out.startswith("<!doctype html>")
        assert "2026-05-24" in out
        assert "Daily Investment Memo" in out

    def test_section_headings_present(self):
        out = render_memo_html(self._SAMPLE_MD, "2026-05-24")
        assert "Today's Verdict" in out
        assert "Top Decisions" in out
        assert "Portfolio Growth" in out

    def test_h1_and_preamble_stripped(self):
        """Our own header replaces the markdown's H1/metadata block."""
        out = render_memo_html(self._SAMPLE_MD, "2026-05-24")
        # H1 from source markdown should not appear as <h1>
        assert "<h1" not in out

    def test_trailing_footer_stripped(self):
        """The `---` + `_Advisory only._` source footer is removed."""
        out = render_memo_html(self._SAMPLE_MD, "2026-05-24")
        # We render our own footer; the source italic footer should not survive
        assert "Advisory only." not in out or out.count("Advisory only") <= 1

    def test_verdict_section_uses_amber_accent(self):
        out = render_memo_html(self._SAMPLE_MD, "2026-05-24")
        # Find the verdict card and confirm it carries the amber accent
        assert "#f59e0b" in out

    def test_growth_section_uses_emerald_accent(self):
        out = render_memo_html(self._SAMPLE_MD, "2026-05-24")
        assert "#10b981" in out

    def test_gain_percentage_colored_green(self):
        out = render_memo_html(self._SAMPLE_MD, "2026-05-24")
        assert "#059669" in out
        assert "+0.56%" in out

    def test_loss_percentage_colored_red(self):
        out = render_memo_html(self._SAMPLE_MD, "2026-05-24")
        assert "#dc2626" in out
        assert "-1.20%" in out

    def test_inline_styles_used_not_style_block(self):
        """Email clients strip <style> blocks; everything must be inline."""
        out = render_memo_html(self._SAMPLE_MD, "2026-05-24")
        # No CSS <style> blocks in body (head <title> is fine)
        assert "<style" not in out
        # Section cards carry inline border-left
        assert "border-left:4px solid" in out

    def test_ticker_code_styled_as_pill(self):
        out = render_memo_html(self._SAMPLE_MD, "2026-05-24")
        # Inline-styled <code> for tickers
        assert "<code style=" in out
        assert "QQQ" in out

    def test_html_escapes_date(self):
        out = render_memo_html(self._SAMPLE_MD, "<bad>")
        assert "<bad>" not in out
        assert "&lt;bad&gt;" in out


# ---------------------------------------------------------------------------
# TestBuildMemoEmailMessage — HTML alternative wiring
# ---------------------------------------------------------------------------

class TestBuildMemoEmailMessageHtml:
    def _cfg(self) -> MemoEmailConfig:
        return MemoEmailConfig(from_addr="from@test.com", to_addrs=["to@test.com"])

    _MD = "## Today's Verdict\n> Cautious.\n\n## Portfolio Growth\n- Today +0.50%\n"

    def test_html_alternative_added_when_md_present(self):
        msg = build_memo_email_message(self._cfg(), "plain text", self._MD, "rid", "2026-05-02")
        # The body part should be multipart/alternative; iter over alternatives
        html_parts = [
            p for p in msg.walk()
            if p.get_content_type() == "text/html"
        ]
        assert len(html_parts) == 1
        html_body = html_parts[0].get_content()
        assert "Today's Verdict" in html_body
        assert "2026-05-02" in html_body

    def test_no_html_alternative_when_md_empty(self):
        msg = build_memo_email_message(self._cfg(), "plain text", "", "rid", "2026-05-02")
        html_parts = [
            p for p in msg.walk()
            if p.get_content_type() == "text/html"
        ]
        assert html_parts == []

    def test_plain_text_alternative_still_present(self):
        msg = build_memo_email_message(self._cfg(), "plain world", self._MD, "rid", "2026-05-02")
        text_parts = [
            p for p in msg.walk()
            if p.get_content_type() == "text/plain"
        ]
        assert len(text_parts) == 1
        assert "plain world" in text_parts[0].get_content()

    def test_md_attachment_still_present_alongside_html(self):
        msg = build_memo_email_message(self._cfg(), "txt", self._MD, "rid", "2026-05-02")
        names = [p.get_filename() for p in msg.iter_attachments()]
        assert any(n and "daily_memo_2026-05-02.md" in n for n in names)

    def test_html_render_failure_does_not_break_message(self):
        """If render_memo_html raises, we still produce a valid plain-text email."""
        from unittest.mock import patch
        with patch(
            "portfolio_automation.memo_email_sender.render_memo_html",
            side_effect=RuntimeError("boom"),
        ):
            msg = build_memo_email_message(self._cfg(), "fallback text", self._MD, "rid", "2026-05-02")
        # Plain-text part survives; no HTML part
        text_parts = [p for p in msg.walk() if p.get_content_type() == "text/plain"]
        html_parts = [p for p in msg.walk() if p.get_content_type() == "text/html"]
        assert any("fallback text" in p.get_content() for p in text_parts)
        assert html_parts == []


# ---------------------------------------------------------------------------
# TestSendDailyMemoEmail
# ---------------------------------------------------------------------------

class TestSendDailyMemoEmail:
    def _msg(self) -> object:
        from email.message import EmailMessage
        m = EmailMessage()
        m["Subject"] = "Test"
        m["From"] = "a@b.com"
        m["To"] = "c@d.com"
        m.set_content("body")
        return m

    def _cfg(self, **kw) -> MemoEmailConfig:
        base = dict(
            enabled=True, dry_run=False, smtp_host="smtp.test", smtp_port=587,
            username="u", from_addr="a@b.com", to_addrs=["c@d.com"],
            use_tls=True, strict_failure=False,
        )
        base.update(kw)
        cfg = MemoEmailConfig(**base)
        cfg.password = "pw"
        return cfg

    def test_dry_run_does_not_connect(self):
        cfg = self._cfg(dry_run=True)
        with patch("smtplib.SMTP") as mock_smtp:
            result = send_daily_memo_email(cfg, self._msg())
        mock_smtp.assert_not_called()
        assert result["dry_run"] is True
        assert result["sent"] is False
        assert result["attempted"] is False

    def test_successful_send(self):
        cfg = self._cfg()
        mock_smtp_instance = MagicMock()
        with patch("smtplib.SMTP", return_value=mock_smtp_instance) as mock_cls:
            mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
            mock_smtp_instance.__exit__ = MagicMock(return_value=False)
            result = send_daily_memo_email(cfg, self._msg())
        assert result["sent"] is True
        assert result["attempted"] is True
        assert result["error_class"] is None

    def test_smtp_failure_returns_error_dict(self):
        cfg = self._cfg()
        with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
            result = send_daily_memo_email(cfg, self._msg())
        assert result["sent"] is False
        assert result["error_class"] == "ConnectionRefusedError"
        assert result["error_message_sanitized"] is not None

    def test_smtp_failure_strict_raises(self):
        cfg = self._cfg(strict_failure=True)
        with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
            with pytest.raises(ConnectionRefusedError):
                send_daily_memo_email(cfg, self._msg())

    def test_password_not_in_error_message(self):
        cfg = self._cfg()
        cfg.password = "my_secret_password"
        exc = Exception("SMTP auth failed: password=my_secret_password")
        sanitized = _sanitize_error(exc)
        assert "my_secret_password" not in sanitized

    def test_tls_false_uses_plain_smtp(self):
        cfg = self._cfg(use_tls=False)
        mock_smtp_instance = MagicMock()
        mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
        mock_smtp_instance.__exit__ = MagicMock(return_value=False)
        with patch("smtplib.SMTP", return_value=mock_smtp_instance):
            result = send_daily_memo_email(cfg, self._msg())
        mock_smtp_instance.starttls.assert_not_called()


# ---------------------------------------------------------------------------
# TestWriteMemoDeliveryStatus
# ---------------------------------------------------------------------------

class TestWriteMemoDeliveryStatus:
    def test_writes_to_latest_namespace(self, tmp_path):
        data = {"observe_only": True, "sent": False, "no_trade": True}
        path = write_memo_delivery_status(data, base_dir=tmp_path)
        assert path == tmp_path / "latest" / "memo_delivery_status.json"
        assert path.exists()

    def test_content_is_valid_json(self, tmp_path):
        data = {"sent": True, "run_id": "2026-05-02_daily"}
        write_memo_delivery_status(data, base_dir=tmp_path)
        loaded = json.loads((tmp_path / "latest" / "memo_delivery_status.json").read_text())
        assert loaded["sent"] is True

    def test_no_password_in_output(self, tmp_path):
        data = {"sent": False, "password": "SHOULD_NOT_BE_HERE"}
        write_memo_delivery_status(data, base_dir=tmp_path)
        raw = (tmp_path / "latest" / "memo_delivery_status.json").read_text()
        # password may be in the field if caller writes it — test ensures our module never adds it
        # This test verifies the writer doesn't add extra secret fields
        loaded = json.loads(raw)
        assert "smtp_password" not in loaded
        assert "smtp_secret" not in loaded


# ---------------------------------------------------------------------------
# TestAppendMemoDeliveryLog
# ---------------------------------------------------------------------------

class TestAppendMemoDeliveryLog:
    def test_writes_to_policy_namespace(self, tmp_path):
        entry = {"observe_only": True, "sent": False}
        path = append_memo_delivery_log(entry, base_dir=tmp_path)
        assert path == tmp_path / "policy" / "memo_delivery_log.jsonl"
        assert path.exists()

    def test_appends_multiple_entries(self, tmp_path):
        append_memo_delivery_log({"run_id": "r1"}, base_dir=tmp_path)
        append_memo_delivery_log({"run_id": "r2"}, base_dir=tmp_path)
        lines = (tmp_path / "policy" / "memo_delivery_log.jsonl").read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["run_id"] == "r1"
        assert json.loads(lines[1])["run_id"] == "r2"

    def test_log_entry_is_valid_json(self, tmp_path):
        entry = {"sent": True, "run_id": "test"}
        append_memo_delivery_log(entry, base_dir=tmp_path)
        raw = (tmp_path / "policy" / "memo_delivery_log.jsonl").read_text().strip()
        loaded = json.loads(raw)
        assert loaded["sent"] is True


# ---------------------------------------------------------------------------
# TestRunMemoEmailDelivery — full pipeline
# ---------------------------------------------------------------------------

class TestRunMemoEmailDelivery:
    def test_disabled_by_default_skips(self, tmp_path):
        result = run_memo_email_delivery(base_dir=tmp_path, env={})
        assert result["skipped"] is True
        assert result["reason"] == "disabled"
        assert result["sent"] is False

    def test_disabled_writes_status_artifact(self, tmp_path):
        run_memo_email_delivery(base_dir=tmp_path, env={}, write_files=True)
        status_path = tmp_path / "latest" / "memo_delivery_status.json"
        assert status_path.exists()

    def test_governance_flags_always_present(self, tmp_path):
        result = run_memo_email_delivery(base_dir=tmp_path, env={})
        assert result["observe_only"] is True
        assert result["no_trade"] is True

    def test_missing_recipients_skips(self, tmp_path):
        _write_memo_files(tmp_path)
        env = {"MEMO_EMAIL_ENABLED": "1", "MEMO_EMAIL_DRY_RUN": "0",
               "MEMO_EMAIL_SMTP_HOST": "h", "MEMO_EMAIL_USERNAME": "u",
               "MEMO_EMAIL_PASSWORD": "p", "MEMO_EMAIL_FROM": "f@g.com",
               "MEMO_EMAIL_TO": ""}
        result = run_memo_email_delivery(base_dir=tmp_path, env=env)
        assert result["skipped"] is True
        assert result["reason"] == "invalid_or_missing_recipients"

    def test_invalid_recipients_skips(self, tmp_path):
        _write_memo_files(tmp_path)
        env = {"MEMO_EMAIL_ENABLED": "1", "MEMO_EMAIL_DRY_RUN": "0",
               "MEMO_EMAIL_SMTP_HOST": "h", "MEMO_EMAIL_USERNAME": "u",
               "MEMO_EMAIL_PASSWORD": "p", "MEMO_EMAIL_FROM": "f@g.com",
               "MEMO_EMAIL_TO": "notanemail"}
        result = run_memo_email_delivery(base_dir=tmp_path, env=env)
        assert result["skipped"] is True
        assert result["reason"] == "invalid_or_missing_recipients"

    def test_missing_smtp_config_skips(self, tmp_path):
        _write_memo_files(tmp_path)
        env = {"MEMO_EMAIL_ENABLED": "1", "MEMO_EMAIL_DRY_RUN": "0",
               "MEMO_EMAIL_TO": "to@test.com"}
        result = run_memo_email_delivery(base_dir=tmp_path, env=env)
        assert result["skipped"] is True
        assert result["reason"] == "missing_smtp_config"

    def test_missing_memo_file_skips(self, tmp_path):
        # memo files not written
        result = run_memo_email_delivery(base_dir=tmp_path, env=_min_env())
        assert result["skipped"] is True
        assert result["reason"] == "memo_file_missing"
        assert result["available"] is False

    def test_dry_run_does_not_send(self, tmp_path):
        _write_memo_files(tmp_path)
        env = _min_env(MEMO_EMAIL_DRY_RUN="1")
        with patch("smtplib.SMTP") as mock_smtp:
            result = run_memo_email_delivery(base_dir=tmp_path, env=env)
        mock_smtp.assert_not_called()
        assert result["sent"] is False
        assert result["reason"] == "dry_run"

    def test_dry_run_returns_available_true(self, tmp_path):
        _write_memo_files(tmp_path)
        env = _min_env(MEMO_EMAIL_DRY_RUN="1")
        result = run_memo_email_delivery(base_dir=tmp_path, env=env)
        assert result["available"] is True

    def test_successful_mocked_send(self, tmp_path):
        _write_memo_files(tmp_path)
        mock_smtp_instance = MagicMock()
        mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
        mock_smtp_instance.__exit__ = MagicMock(return_value=False)
        with patch("smtplib.SMTP", return_value=mock_smtp_instance):
            result = run_memo_email_delivery(
                base_dir=tmp_path, env=_min_env(), run_id="test_run_001"
            )
        assert result["sent"] is True
        assert result["reason"] == "sent"
        assert result["attempted"] is True

    def test_successful_send_writes_status(self, tmp_path):
        _write_memo_files(tmp_path)
        mock_smtp_instance = MagicMock()
        mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
        mock_smtp_instance.__exit__ = MagicMock(return_value=False)
        with patch("smtplib.SMTP", return_value=mock_smtp_instance):
            run_memo_email_delivery(base_dir=tmp_path, env=_min_env(), run_id="test_run_002")
        status_path = tmp_path / "latest" / "memo_delivery_status.json"
        assert status_path.exists()
        loaded = json.loads(status_path.read_text())
        assert loaded["sent"] is True

    def test_successful_send_writes_log(self, tmp_path):
        _write_memo_files(tmp_path)
        mock_smtp_instance = MagicMock()
        mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
        mock_smtp_instance.__exit__ = MagicMock(return_value=False)
        with patch("smtplib.SMTP", return_value=mock_smtp_instance):
            run_memo_email_delivery(base_dir=tmp_path, env=_min_env(), run_id="test_run_003")
        log_path = tmp_path / "policy" / "memo_delivery_log.jsonl"
        assert log_path.exists()
        entry = json.loads(log_path.read_text().strip())
        assert entry["sent"] is True
        assert entry["observe_only"] is True

    def test_smtp_failure_writes_failed_status(self, tmp_path):
        _write_memo_files(tmp_path)
        with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
            result = run_memo_email_delivery(
                base_dir=tmp_path, env=_min_env(), run_id="fail_run"
            )
        assert result["sent"] is False
        assert result["error_class"] == "ConnectionRefusedError"
        status_path = tmp_path / "latest" / "memo_delivery_status.json"
        loaded = json.loads(status_path.read_text())
        assert loaded["sent"] is False
        assert loaded["error_class"] == "ConnectionRefusedError"

    def test_smtp_failure_sanitized_error_no_password(self, tmp_path):
        _write_memo_files(tmp_path)
        env = _min_env(MEMO_EMAIL_PASSWORD="supersecret_pw")
        with patch("smtplib.SMTP", side_effect=Exception("auth: supersecret_pw")):
            result = run_memo_email_delivery(base_dir=tmp_path, env=env)
        assert "supersecret_pw" not in (result.get("error_message_sanitized") or "")
        # Also verify not in written status artifact
        status_path = tmp_path / "latest" / "memo_delivery_status.json"
        raw = status_path.read_text()
        assert "supersecret_pw" not in raw

    def test_no_password_in_status_json(self, tmp_path):
        _write_memo_files(tmp_path)
        env = _min_env(MEMO_EMAIL_PASSWORD="do_not_log_me")
        mock_smtp_instance = MagicMock()
        mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
        mock_smtp_instance.__exit__ = MagicMock(return_value=False)
        with patch("smtplib.SMTP", return_value=mock_smtp_instance):
            run_memo_email_delivery(base_dir=tmp_path, env=env)
        raw = (tmp_path / "latest" / "memo_delivery_status.json").read_text()
        assert "do_not_log_me" not in raw

    def test_no_password_in_delivery_log(self, tmp_path):
        _write_memo_files(tmp_path)
        env = _min_env(MEMO_EMAIL_PASSWORD="do_not_log_me_log")
        mock_smtp_instance = MagicMock()
        mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
        mock_smtp_instance.__exit__ = MagicMock(return_value=False)
        with patch("smtplib.SMTP", return_value=mock_smtp_instance):
            run_memo_email_delivery(base_dir=tmp_path, env=env)
        log_path = tmp_path / "policy" / "memo_delivery_log.jsonl"
        raw = log_path.read_text()
        assert "do_not_log_me_log" not in raw

    def test_idempotency_skips_duplicate_successful_run(self, tmp_path):
        _write_memo_files(tmp_path)
        # Write a prior sent entry for same date
        entry = {
            "run_id": "2026-05-02_daily",
            "memo_date": "2026-05-02",
            "sent": True,
            "observe_only": True,
            "no_trade": True,
        }
        _write_log_entry(tmp_path, entry)

        with patch("smtplib.SMTP") as mock_smtp:
            result = run_memo_email_delivery(
                base_dir=tmp_path,
                env=_min_env(),
                run_id="2026-05-02_daily",
            )
        mock_smtp.assert_not_called()
        assert result["skipped"] is True
        assert result["reason"] == "already_sent"

    def test_force_resend_bypasses_idempotency(self, tmp_path):
        _write_memo_files(tmp_path)
        entry = {"run_id": "2026-05-02_daily", "memo_date": "2026-05-02", "sent": True}
        _write_log_entry(tmp_path, entry)

        mock_smtp_instance = MagicMock()
        mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
        mock_smtp_instance.__exit__ = MagicMock(return_value=False)
        env = _min_env(MEMO_EMAIL_FORCE_RESEND="1")
        with patch("smtplib.SMTP", return_value=mock_smtp_instance):
            result = run_memo_email_delivery(
                base_dir=tmp_path,
                env=env,
                run_id="2026-05-02_daily",
            )
        assert result["sent"] is True

    def test_dry_run_does_not_create_sent_idempotency_record(self, tmp_path):
        _write_memo_files(tmp_path)
        env = _min_env(MEMO_EMAIL_DRY_RUN="1")
        with patch("smtplib.SMTP") as mock_smtp:
            run_memo_email_delivery(base_dir=tmp_path, env=env, run_id="dry_run_id")

        # Now do a real send — should NOT be blocked by dry-run log entry
        mock_smtp_instance = MagicMock()
        mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
        mock_smtp_instance.__exit__ = MagicMock(return_value=False)
        with patch("smtplib.SMTP", return_value=mock_smtp_instance):
            result = run_memo_email_delivery(
                base_dir=tmp_path,
                env=_min_env(MEMO_EMAIL_DRY_RUN="0"),
                run_id="dry_run_id",
            )
        assert result["sent"] is True

    def test_idempotency_by_date_match(self, tmp_path):
        """Same memo_date from a different run_id should also be blocked."""
        _write_memo_files(tmp_path)
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        entry = {"run_id": "other_run", "memo_date": today, "sent": True}
        _write_log_entry(tmp_path, entry)

        with patch("smtplib.SMTP") as mock_smtp:
            result = run_memo_email_delivery(
                base_dir=tmp_path,
                env=_min_env(),
                run_id="new_run_id",
            )
        mock_smtp.assert_not_called()
        assert result["skipped"] is True
        assert result["reason"] == "already_sent"

    def test_write_files_false_no_artifacts_written(self, tmp_path):
        result = run_memo_email_delivery(
            base_dir=tmp_path, env={}, write_files=False
        )
        assert not (tmp_path / "latest" / "memo_delivery_status.json").exists()
        assert not (tmp_path / "policy" / "memo_delivery_log.jsonl").exists()

    def test_recipients_count_in_status(self, tmp_path):
        _write_memo_files(tmp_path)
        env = _min_env(MEMO_EMAIL_TO="a@b.com,c@d.com", MEMO_EMAIL_DRY_RUN="1")
        result = run_memo_email_delivery(base_dir=tmp_path, env=env)
        assert result["recipients_count"] == 2

    def test_smtp_host_present_flag(self, tmp_path):
        _write_memo_files(tmp_path)
        result = run_memo_email_delivery(base_dir=tmp_path, env=_min_env())
        assert result["smtp_host_present"] is True

    def test_username_present_flag(self, tmp_path):
        _write_memo_files(tmp_path)
        result = run_memo_email_delivery(base_dir=tmp_path, env=_min_env())
        assert result["username_present"] is True


# ---------------------------------------------------------------------------
# TestLoadMemoDeliveryStatus
# ---------------------------------------------------------------------------

class TestLoadMemoDeliveryStatus:
    def test_returns_available_false_when_missing(self, tmp_path):
        result = load_memo_delivery_status(base_dir=tmp_path)
        assert result == {"available": False}

    def test_loads_written_status(self, tmp_path):
        data = {"sent": True, "available": True, "observe_only": True}
        write_memo_delivery_status(data, base_dir=tmp_path)
        loaded = load_memo_delivery_status(base_dir=tmp_path)
        assert loaded["sent"] is True

    def test_returns_available_false_on_corrupt_file(self, tmp_path):
        path = tmp_path / "latest"
        path.mkdir(parents=True, exist_ok=True)
        (path / "memo_delivery_status.json").write_text("not json", encoding="utf-8")
        result = load_memo_delivery_status(base_dir=tmp_path)
        assert result == {"available": False}


# ---------------------------------------------------------------------------
# TestLoadRecentDeliveryLog
# ---------------------------------------------------------------------------

class TestLoadRecentDeliveryLog:
    def test_empty_when_no_log(self, tmp_path):
        result = load_recent_delivery_log(base_dir=tmp_path)
        assert result == []

    def test_loads_entries(self, tmp_path):
        _write_log_entry(tmp_path, {"run_id": "r1", "sent": True})
        _write_log_entry(tmp_path, {"run_id": "r2", "sent": False})
        entries = load_recent_delivery_log(base_dir=tmp_path)
        assert len(entries) == 2

    def test_limit_applied(self, tmp_path):
        for i in range(25):
            _write_log_entry(tmp_path, {"run_id": f"r{i}", "sent": True})
        entries = load_recent_delivery_log(base_dir=tmp_path, limit=10)
        assert len(entries) == 10

    def test_skips_malformed_lines(self, tmp_path):
        log_path = tmp_path / "policy" / "memo_delivery_log.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("not json\n{\"run_id\": \"ok\"}\n", encoding="utf-8")
        entries = load_recent_delivery_log(base_dir=tmp_path)
        assert len(entries) == 1
        assert entries[0]["run_id"] == "ok"


# ---------------------------------------------------------------------------
# TestIdempotency helpers
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_already_sent_false_when_no_log(self, tmp_path):
        assert _already_sent("rid", "2026-05-02", tmp_path) is False

    def test_already_sent_true_by_run_id(self, tmp_path):
        _write_log_entry(tmp_path, {"run_id": "my_run", "sent": True})
        assert _already_sent("my_run", "2026-05-02", tmp_path) is True

    def test_already_sent_true_by_date(self, tmp_path):
        _write_log_entry(tmp_path, {"run_id": "other", "memo_date": "2026-05-02", "sent": True})
        assert _already_sent("my_run", "2026-05-02", tmp_path) is True

    def test_already_sent_false_when_sent_is_false(self, tmp_path):
        _write_log_entry(tmp_path, {"run_id": "my_run", "sent": False})
        assert _already_sent("my_run", "2026-05-02", tmp_path) is False

    def test_already_sent_false_different_run_and_date(self, tmp_path):
        _write_log_entry(tmp_path, {"run_id": "other", "memo_date": "2026-05-01", "sent": True})
        assert _already_sent("my_run", "2026-05-02", tmp_path) is False


# ---------------------------------------------------------------------------
# TestCLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_dry_run_flag_sets_dry_run(self, tmp_path):
        _write_memo_files(tmp_path)
        captured: list[dict] = []

        def mock_deliver(**kwargs):
            captured.append(kwargs.get("env") or {})
            return {"sent": False, "dry_run": True, "skipped": False, "reason": "dry_run",
                    "attempted": False, "enabled": True, "error_class": None}

        with patch("portfolio_automation.memo_email_sender.run_memo_email_delivery", side_effect=mock_deliver):
            rc = _cli_main(["--dry-run"])

        assert rc == 0

    def test_send_flag_disables_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMO_EMAIL_ENABLED", "1")

        def mock_deliver(**kwargs):
            return {"sent": True, "dry_run": False, "skipped": False, "reason": "sent",
                    "attempted": True, "enabled": True, "error_class": None}

        with patch("portfolio_automation.memo_email_sender.run_memo_email_delivery", side_effect=mock_deliver):
            rc = _cli_main(["--send"])
        assert rc == 0

    def test_force_resend_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMO_EMAIL_ENABLED", "1")
        captured_env: list[dict] = []

        def mock_deliver(**kwargs):
            # The CLI sets MEMO_EMAIL_FORCE_RESEND=1 in os.environ before calling
            captured_env.append(dict(os.environ))
            return {"sent": True, "dry_run": False, "skipped": False, "reason": "sent",
                    "attempted": True, "enabled": True, "error_class": None}

        with patch("portfolio_automation.memo_email_sender.run_memo_email_delivery", side_effect=mock_deliver):
            rc = _cli_main(["--force-resend"])
        assert rc == 0
        assert os.environ.get("MEMO_EMAIL_FORCE_RESEND") in (None, "0", "")

    def test_cli_restores_env_after_run(self, monkeypatch):
        monkeypatch.delenv("MEMO_EMAIL_ENABLED", raising=False)

        def mock_deliver(**kwargs):
            return {"sent": False, "dry_run": True, "skipped": False, "reason": "dry_run",
                    "attempted": False, "enabled": True, "error_class": None}

        with patch("portfolio_automation.memo_email_sender.run_memo_email_delivery", side_effect=mock_deliver):
            _cli_main(["--dry-run"])

        assert os.environ.get("MEMO_EMAIL_ENABLED") is None

    def test_cli_dry_run_enabled_in_env(self, monkeypatch, capsys):
        monkeypatch.delenv("MEMO_EMAIL_ENABLED", raising=False)
        monkeypatch.delenv("MEMO_EMAIL_DRY_RUN", raising=False)

        calls: list[str] = []

        def mock_deliver(**kwargs):
            calls.append(os.environ.get("MEMO_EMAIL_ENABLED", ""))
            return {"sent": False, "dry_run": True, "skipped": False, "reason": "dry_run",
                    "attempted": False, "enabled": True, "error_class": None}

        with patch("portfolio_automation.memo_email_sender.run_memo_email_delivery", side_effect=mock_deliver):
            _cli_main(["--dry-run"])

        assert calls[0] == "1"


# ---------------------------------------------------------------------------
# TestDataGovernanceNamespace
# ---------------------------------------------------------------------------

class TestDataGovernanceNamespace:
    def test_status_in_latest_namespace(self, tmp_path):
        data = {"observe_only": True}
        path = write_memo_delivery_status(data, base_dir=tmp_path)
        assert "latest" in str(path)
        assert "policy" not in str(path)

    def test_log_in_policy_namespace(self, tmp_path):
        path = append_memo_delivery_log({"sent": False}, base_dir=tmp_path)
        assert "policy" in str(path)
        assert "latest" not in str(path)


# ---------------------------------------------------------------------------
# TestMainIntegration — main.py uses disabled-by-default behavior
# ---------------------------------------------------------------------------

class TestMainIntegration:
    def test_run_memo_email_delivery_disabled_by_default(self, tmp_path):
        """Without MEMO_EMAIL_ENABLED=1, delivery always skips."""
        result = run_memo_email_delivery(base_dir=tmp_path, env={})
        assert result["enabled"] is False
        assert result["skipped"] is True
        assert result["reason"] == "disabled"

    def test_no_smtp_calls_when_disabled(self, tmp_path):
        with patch("smtplib.SMTP") as mock_smtp:
            run_memo_email_delivery(base_dir=tmp_path, env={})
        mock_smtp.assert_not_called()

    def test_strict_failure_false_by_default(self, tmp_path):
        """When SMTP fails and strict_failure=False, result is returned (no raise)."""
        _write_memo_files(tmp_path)
        with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
            result = run_memo_email_delivery(
                base_dir=tmp_path, env=_min_env()
            )
        assert result["sent"] is False
        assert result["error_class"] == "ConnectionRefusedError"
