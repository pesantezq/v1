"""
Tests for portfolio_automation/env.py
"""
from __future__ import annotations

import json
import os

import pytest

from portfolio_automation import env as envmod


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_registry_non_empty(self):
        assert len(envmod.REGISTRY) > 0

    def test_no_duplicate_names(self):
        names = [v.name for v in envmod.REGISTRY]
        assert len(names) == len(set(names))

    def test_required_vars_have_no_default(self):
        for var in envmod.REGISTRY:
            if var.required:
                assert var.default is None, (
                    f"{var.name} is required but has a default — required + default "
                    "is contradictory."
                )

    def test_every_var_in_allowed_group(self):
        for var in envmod.REGISTRY:
            assert var.group in envmod.ALLOWED_GROUPS, var.name

    def test_known_required_set(self):
        # Locks the current required surface. New required vars must update this.
        required = {v.name for v in envmod.REGISTRY if v.required}
        assert "FMP_API_KEY" in required


class TestSchwabEnvRegistry:
    """Schwab read-only sync activation (2026-06-12): the OAuth env vars must be
    registered so preflight's env-check recognizes them, but never required —
    the layer self-reports `unconfigured` and must not block a pre-provisioning
    preflight."""

    _SCHWAB = ("SCHWAB_CLIENT_ID", "SCHWAB_CLIENT_SECRET", "SCHWAB_REDIRECT_URI",
               "SCHWAB_READ_ONLY_MODE", "TRADING_ENABLED")

    def test_schwab_vars_registered(self):
        names = {v.name for v in envmod.REGISTRY}
        for n in self._SCHWAB:
            assert n in names, f"{n} missing from env REGISTRY"

    def test_schwab_vars_not_required(self):
        for v in envmod.REGISTRY:
            if v.name in self._SCHWAB:
                assert v.required is False, f"{v.name} must be optional (layer self-reports unconfigured)"

    def test_schwab_client_secret_is_secret(self):
        by = {v.name: v for v in envmod.REGISTRY}
        assert by["SCHWAB_CLIENT_SECRET"].secret is True


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

class TestLookups:
    def test_find_var_returns_entry(self):
        v = envmod.find_var("FMP_API_KEY")
        assert v is not None
        assert v.required is True
        assert v.secret is True

    def test_find_var_unknown_returns_none(self):
        assert envmod.find_var("NEVER_EXISTS_XYZ") is None

    def test_vars_for_group_email(self):
        email = envmod.vars_for_group(envmod.GROUP_EMAIL)
        names = {v.name for v in email}
        assert "MEMO_EMAIL_ENABLED" in names
        assert "MEMO_EMAIL_PASSWORD" in names

    def test_vars_for_group_data(self):
        data = envmod.vars_for_group(envmod.GROUP_DATA)
        names = {v.name for v in data}
        assert "FMP_API_KEY" in names
        # AlphaVantage has been excised from the system; FMP is the sole
        # market-data provider, so ALPHA_VANTAGE_API_KEY is no longer registered.
        assert "ALPHA_VANTAGE_API_KEY" not in names


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

class TestGetRequired:
    def test_set_value_returned(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("FMP_API_KEY", "abc123")
        assert envmod.get_required("FMP_API_KEY") == "abc123"

    def test_missing_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("FMP_API_KEY", raising=False)
        with pytest.raises(envmod.MissingEnvVar):
            envmod.get_required("FMP_API_KEY")

    def test_empty_string_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("FMP_API_KEY", "   ")
        with pytest.raises(envmod.MissingEnvVar):
            envmod.get_required("FMP_API_KEY")

    def test_error_message_does_not_leak_value(self, monkeypatch: pytest.MonkeyPatch):
        # Even if the registry says secret, the message just names the var.
        monkeypatch.delenv("FMP_API_KEY", raising=False)
        try:
            envmod.get_required("FMP_API_KEY")
        except envmod.MissingEnvVar as exc:
            assert "FMP_API_KEY" in str(exc)
            # No suspicious leakage; we don't have a value to leak anyway, but
            # be explicit that the message is short.
            assert len(str(exc)) < 200


class TestGetOptional:
    def test_returns_env_when_set(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
        assert envmod.get_optional("OPENAI_MODEL") == "gpt-4o"

    def test_returns_registered_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        # registered default is "gpt-4o-mini"
        assert envmod.get_optional("OPENAI_MODEL") == "gpt-4o-mini"

    def test_explicit_default_overrides_registered(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        assert envmod.get_optional("OPENAI_MODEL", default="custom") == "custom"

    def test_unknown_var_with_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("NEVER_EXISTS_XYZ", raising=False)
        assert envmod.get_optional("NEVER_EXISTS_XYZ", default="x") == "x"

    def test_unknown_var_without_default_returns_none(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("NEVER_EXISTS_XYZ", raising=False)
        assert envmod.get_optional("NEVER_EXISTS_XYZ") is None


class TestIsTruthy:
    @pytest.mark.parametrize("raw", ["1", "true", "True", "YES", "yes", "y", "on", "ON"])
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, raw: str):
        monkeypatch.setenv("STOCKBOT_TESTING", raw)
        assert envmod.is_truthy("STOCKBOT_TESTING") is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "n", "off", ""])
    def test_falsey_values(self, monkeypatch: pytest.MonkeyPatch, raw: str):
        monkeypatch.setenv("STOCKBOT_TESTING", raw)
        assert envmod.is_truthy("STOCKBOT_TESTING") is False

    def test_unset_uses_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("STOCKBOT_TESTING", raising=False)
        assert envmod.is_truthy("STOCKBOT_TESTING") is False
        assert envmod.is_truthy("STOCKBOT_TESTING", default=True) is True


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

class TestRedactSecrets:
    def test_redacts_fmp_api_key(self):
        masked = envmod.redact_secrets("error connecting: FMP_API_KEY=abc123xyz")
        assert "FMP_API_KEY=<REDACTED>" in masked
        assert "abc123xyz" not in masked

    def test_redacts_anthropic_api_key(self):
        masked = envmod.redact_secrets("ANTHROPIC_API_KEY=sk-ant-12345")
        assert "ANTHROPIC_API_KEY=<REDACTED>" in masked
        assert "sk-ant-12345" not in masked

    def test_redacts_memo_password_aliases(self):
        masked = envmod.redact_secrets("auth failed: EMAIL_PASS=hunter2")
        assert "EMAIL_PASS=<REDACTED>" in masked
        assert "hunter2" not in masked

    def test_redacts_case_insensitive(self):
        masked = envmod.redact_secrets("fmp_api_key=abc123")
        assert "<REDACTED>" in masked
        assert "abc123" not in masked

    def test_does_not_touch_non_secret_var(self):
        masked = envmod.redact_secrets("OPENAI_MODEL=gpt-4o")
        assert "gpt-4o" in masked

    def test_handles_empty(self):
        assert envmod.redact_secrets("") == ""


# ---------------------------------------------------------------------------
# check_state
# ---------------------------------------------------------------------------

class TestCheckState:
    def test_required_missing_when_unset(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("FMP_API_KEY", raising=False)
        state = envmod.check_state()
        assert "FMP_API_KEY" in state["missing_required"]

    def test_required_present_when_set(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("FMP_API_KEY", "abc123")
        state = envmod.check_state()
        assert "FMP_API_KEY" not in state["missing_required"]
        # And value is redacted in the per-var record:
        data_group = state["groups"]["data"]
        fmp = next(v for v in data_group if v["name"] == "FMP_API_KEY")
        assert fmp["set"] is True
        assert fmp["secret"] is True
        assert fmp["value"] == "<REDACTED>"

    def test_optional_with_default_reports_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        state = envmod.check_state()
        llm = state["groups"]["llm"]
        var = next(v for v in llm if v["name"] == "OPENAI_MODEL")
        assert var["source"] == "default"
        assert var["value"] == "gpt-4o-mini"

    def test_secret_value_never_appears(self, monkeypatch: pytest.MonkeyPatch):
        secret_value = "ab-very-secret-token-xy"
        monkeypatch.setenv("FMP_API_KEY", secret_value)
        state = envmod.check_state()
        serialised = json.dumps(state)
        assert secret_value not in serialised

    def test_aliases_set_reported(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("MEMO_EMAIL_USERNAME", raising=False)
        monkeypatch.setenv("EMAIL_USER", "ops@example.com")
        state = envmod.check_state()
        email = state["groups"]["email"]
        memo_user = next(v for v in email if v["name"] == "MEMO_EMAIL_USERNAME")
        assert "EMAIL_USER" in memo_user["aliases_set"]

    def test_summary_includes_advisory_flags(self, monkeypatch: pytest.MonkeyPatch):
        state = envmod.check_state()
        assert state["advisory_only"] is True
        assert state["no_trade"] is True


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestRenderText:
    def test_lists_groups(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("FMP_API_KEY", "x")
        state = envmod.check_state()
        out = envmod.render_text(state)
        assert "Environment Variable Check" in out
        assert "[data]" in out
        assert "[llm]" in out
        assert "[email]" in out
        assert "Advisory only" in out

    def test_does_not_leak_secret_value(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("FMP_API_KEY", "very-secret-12345")
        out = envmod.render_text(envmod.check_state())
        assert "very-secret-12345" not in out
        assert "FMP_API_KEY" in out
        assert "<REDACTED>" in out

    def test_missing_required_section(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("FMP_API_KEY", raising=False)
        out = envmod.render_text(envmod.check_state())
        assert "MISSING REQUIRED" in out
        assert "FMP_API_KEY" in out


class TestRenderJson:
    def test_parseable(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("FMP_API_KEY", "x")
        s = envmod.render_json(envmod.check_state())
        parsed = json.loads(s)
        assert "groups" in parsed
        assert "summary" in parsed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_default_invocation_prints_state(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ):
        monkeypatch.setenv("FMP_API_KEY", "x")
        rc = envmod.main(["--no-dotenv"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Environment Variable Check" in out

    def test_check_json(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
        monkeypatch.setenv("FMP_API_KEY", "x")
        rc = envmod.main(["--check", "--format", "json", "--no-dotenv"])
        out = capsys.readouterr().out
        assert rc == 0
        parsed = json.loads(out)
        assert "missing_required" in parsed

    def test_strict_exits_one_when_required_missing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path,
    ):
        monkeypatch.chdir(tmp_path)  # ensure no .env in CWD
        monkeypatch.delenv("FMP_API_KEY", raising=False)
        rc = envmod.main(["--check", "--strict", "--no-dotenv"])
        assert rc == 1

    def test_strict_exits_zero_when_all_required_set(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ):
        monkeypatch.setenv("FMP_API_KEY", "x")
        rc = envmod.main(["--check", "--strict", "--no-dotenv"])
        assert rc == 0


# ---------------------------------------------------------------------------
# Dotenv auto-load (CLI only)
# ---------------------------------------------------------------------------

class TestDotenvAutoload:
    def test_loads_dotenv_from_cwd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path,
    ):
        # Build a .env in a tmp dir, run CLI from there, observe load.
        env_file = tmp_path / ".env"
        env_file.write_text("FMP_API_KEY=loaded-from-dotenv\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FMP_API_KEY", raising=False)
        loaded = envmod._load_dotenv_for_cli()
        assert loaded is not None
        assert os.environ.get("FMP_API_KEY") == "loaded-from-dotenv"

    def test_does_not_override_existing_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path,
    ):
        env_file = tmp_path / ".env"
        env_file.write_text("FMP_API_KEY=from-file\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FMP_API_KEY", "from-shell")
        envmod._load_dotenv_for_cli()
        assert os.environ.get("FMP_API_KEY") == "from-shell"

    def test_no_dotenv_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path,
    ):
        monkeypatch.chdir(tmp_path)  # tmp_path has no .env
        # Also avoid the repo-root fallback finding the real .env
        monkeypatch.setattr(envmod, "_find_dotenv_for_cli", lambda: None)
        assert envmod._load_dotenv_for_cli() is None

    def test_handles_comments_and_quoted_values(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path,
    ):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# comment line\n"
            "EMPTY_LINE_NEXT=\n"
            "\n"
            "QUOTED_VAR=\"hello world\"\n"
            "SINGLE_QUOTED='hi there'\n"
            "EXPORT_VAR=keep-it\n"
            "export EXPORT_PREFIXED=via-export\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        for k in ("QUOTED_VAR", "SINGLE_QUOTED", "EXPORT_PREFIXED", "EMPTY_LINE_NEXT"):
            monkeypatch.delenv(k, raising=False)
        # Force the manual parser path to ensure both implementations agree.
        # (python-dotenv if present handles all these too.)
        loaded = envmod._load_dotenv_for_cli()
        assert loaded is not None
        assert os.environ.get("QUOTED_VAR") == "hello world"
        assert os.environ.get("SINGLE_QUOTED") == "hi there"
        assert os.environ.get("EXPORT_PREFIXED") == "via-export"

    def test_cli_reports_loaded_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture,
    ):
        env_file = tmp_path / ".env"
        env_file.write_text("FMP_API_KEY=loaded-x\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FMP_API_KEY", raising=False)
        rc = envmod.main(["--check"])
        out = capsys.readouterr().out
        assert rc == 0
        assert ".env:" in out

    def test_cli_no_dotenv_flag_skips_load(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture,
    ):
        env_file = tmp_path / ".env"
        env_file.write_text("FMP_API_KEY=should-not-load\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FMP_API_KEY", raising=False)
        rc = envmod.main(["--check", "--strict", "--no-dotenv"])
        # With dotenv skipped and FMP_API_KEY unset, strict mode exits 1.
        assert rc == 1
        out = capsys.readouterr().out
        assert ".env:" not in out
