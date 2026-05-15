"""Tests for tools/notify_status.py."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools import notify_status as tool


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "stockbot"
    repo.mkdir()
    (repo / "main.py").write_text("# marker\n", encoding="utf-8")
    (repo / "outputs" / "policy").mkdir(parents=True)
    return repo


@pytest.fixture
def disable_smtp(monkeypatch: pytest.MonkeyPatch):
    """Replace _send_smtp with a recorder so tests never hit a real server."""
    sent: list[dict] = []

    def fake_send(*, subject: str, body: str, recipients: list[str]):
        sent.append({"subject": subject, "body": body, "recipients": list(recipients)})
        return True, None

    monkeypatch.setattr(tool, "_send_smtp", fake_send)
    return sent


def _seed_status(monkeypatch: pytest.MonkeyPatch, severity: str = "OK") -> None:
    """Stub tools.status.collect_status to return a known severity."""
    class FakeCheck:
        def __init__(self, name, sev, msg):
            self.name = name
            self.severity = sev
            self.message = msg
            self.details = {}

    class FakeReport:
        overall_severity = severity

        def to_dict(self):
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "repo_root": "/fake",
                "overall_severity": severity,
                "severity_counts": {"OK": 0, "INFO": 0, "WARN": 0, "FAIL": 0},
                "checks": [
                    {"name": "x", "severity": severity, "message": "stub", "details": {}}
                ],
                "advisory_only": True,
                "no_trade": True,
            }

    import tools.status as status_mod
    monkeypatch.setattr(status_mod, "collect_status", lambda repo_root: FakeReport())


# ---------------------------------------------------------------------------
# Gate / disabled-by-default
# ---------------------------------------------------------------------------

class TestDisabled:
    def test_disabled_by_default(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch, disable_smtp,
    ):
        monkeypatch.delenv("STATUS_ALERT_ENABLED", raising=False)
        _seed_status(monkeypatch, "FAIL")
        o = tool.alert(repo_root=fake_repo)
        assert o.success is True
        assert o.sent is False
        assert "STATUS_ALERT_ENABLED=0" in (o.skipped_reason or "")
        assert disable_smtp == []

    def test_blank_value_treated_as_disabled(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch, disable_smtp,
    ):
        monkeypatch.setenv("STATUS_ALERT_ENABLED", "  ")
        _seed_status(monkeypatch, "FAIL")
        o = tool.alert(repo_root=fake_repo)
        assert o.sent is False


# ---------------------------------------------------------------------------
# Threshold + severity gating
# ---------------------------------------------------------------------------

class TestThreshold:
    def _enable(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("STATUS_ALERT_ENABLED", "1")
        monkeypatch.setenv("MEMO_EMAIL_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("MEMO_EMAIL_USERNAME", "u")
        monkeypatch.setenv("MEMO_EMAIL_PASSWORD", "p")
        monkeypatch.setenv("MEMO_EMAIL_FROM", "ops@example.com")
        monkeypatch.setenv("STATUS_ALERT_TO", "alerts@example.com")

    def test_ok_below_fail_threshold_skipped(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch, disable_smtp,
    ):
        self._enable(monkeypatch)
        _seed_status(monkeypatch, "OK")
        o = tool.alert(repo_root=fake_repo)
        assert o.sent is False
        assert "below threshold" in (o.skipped_reason or "")

    def test_warn_below_fail_threshold_skipped(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch, disable_smtp,
    ):
        self._enable(monkeypatch)
        _seed_status(monkeypatch, "WARN")
        o = tool.alert(repo_root=fake_repo, threshold="FAIL")
        assert o.sent is False

    def test_warn_at_warn_threshold_fires(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch, disable_smtp,
    ):
        self._enable(monkeypatch)
        _seed_status(monkeypatch, "WARN")
        o = tool.alert(repo_root=fake_repo, threshold="WARN")
        assert o.sent is True
        assert disable_smtp[0]["subject"].endswith("WARN — " + disable_smtp[0]["subject"].split("— ")[1])

    def test_fail_fires(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch, disable_smtp,
    ):
        self._enable(monkeypatch)
        _seed_status(monkeypatch, "FAIL")
        o = tool.alert(repo_root=fake_repo)
        assert o.sent is True
        assert "FAIL" in (o.subject or "")

    def test_invalid_threshold_normalises_to_FAIL(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch, disable_smtp,
    ):
        self._enable(monkeypatch)
        monkeypatch.setenv("STATUS_ALERT_THRESHOLD", "garbage")
        _seed_status(monkeypatch, "WARN")
        o = tool.alert(repo_root=fake_repo)
        assert o.sent is False  # WARN < FAIL


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------

class TestThrottle:
    def _enable_and_fire_once(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch, disable_smtp,
    ):
        monkeypatch.setenv("STATUS_ALERT_ENABLED", "1")
        monkeypatch.setenv("MEMO_EMAIL_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("MEMO_EMAIL_USERNAME", "u")
        monkeypatch.setenv("MEMO_EMAIL_PASSWORD", "p")
        monkeypatch.setenv("MEMO_EMAIL_FROM", "ops@example.com")
        monkeypatch.setenv("STATUS_ALERT_TO", "alerts@example.com")
        _seed_status(monkeypatch, "FAIL")
        o = tool.alert(repo_root=fake_repo)
        assert o.sent is True

    def test_second_fire_within_window_throttled(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch, disable_smtp,
    ):
        self._enable_and_fire_once(fake_repo, monkeypatch, disable_smtp)
        # Same severity, same minute -> throttle
        o = tool.alert(repo_root=fake_repo)
        assert o.sent is False
        assert "throttled" in (o.skipped_reason or "")
        # Only one email actually sent
        assert len(disable_smtp) == 1

    def test_force_overrides_throttle(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch, disable_smtp,
    ):
        self._enable_and_fire_once(fake_repo, monkeypatch, disable_smtp)
        o = tool.alert(repo_root=fake_repo, force=True)
        assert o.sent is True
        assert len(disable_smtp) == 2

    def test_different_severity_not_throttled(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch, disable_smtp,
    ):
        self._enable_and_fire_once(fake_repo, monkeypatch, disable_smtp)
        # Now severity drops to WARN at the WARN threshold
        _seed_status(monkeypatch, "WARN")
        o = tool.alert(repo_root=fake_repo, threshold="WARN")
        assert o.sent is True
        assert len(disable_smtp) == 2

    def test_aged_state_re_fires(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch, disable_smtp,
    ):
        # Pre-seed an old state file (5 hours ago, threshold is 4)
        state_path = fake_repo / "outputs" / "policy" / "status_alert_state.json"
        state_path.write_text(json.dumps({
            "last_alert_severity": "FAIL",
            "last_alert_at": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),
        }), encoding="utf-8")
        # Enable + fire FAIL
        monkeypatch.setenv("STATUS_ALERT_ENABLED", "1")
        monkeypatch.setenv("MEMO_EMAIL_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("MEMO_EMAIL_USERNAME", "u")
        monkeypatch.setenv("MEMO_EMAIL_PASSWORD", "p")
        monkeypatch.setenv("MEMO_EMAIL_FROM", "ops@example.com")
        monkeypatch.setenv("STATUS_ALERT_TO", "alerts@example.com")
        _seed_status(monkeypatch, "FAIL")
        o = tool.alert(repo_root=fake_repo)
        assert o.sent is True  # outside throttle window


# ---------------------------------------------------------------------------
# Recipients + missing-config + dry-run
# ---------------------------------------------------------------------------

class TestRecipientsAndConfig:
    def test_no_recipients_fails_cleanly(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch, disable_smtp,
    ):
        monkeypatch.setenv("STATUS_ALERT_ENABLED", "1")
        monkeypatch.setenv("MEMO_EMAIL_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("MEMO_EMAIL_USERNAME", "u")
        monkeypatch.setenv("MEMO_EMAIL_PASSWORD", "p")
        monkeypatch.setenv("MEMO_EMAIL_FROM", "ops@example.com")
        monkeypatch.delenv("STATUS_ALERT_TO", raising=False)
        monkeypatch.delenv("MEMO_EMAIL_TO", raising=False)
        _seed_status(monkeypatch, "FAIL")
        o = tool.alert(repo_root=fake_repo)
        assert o.success is False
        assert "no recipients" in (o.error or "")

    def test_falls_back_to_memo_email_to(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch, disable_smtp,
    ):
        monkeypatch.setenv("STATUS_ALERT_ENABLED", "1")
        monkeypatch.setenv("MEMO_EMAIL_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("MEMO_EMAIL_USERNAME", "u")
        monkeypatch.setenv("MEMO_EMAIL_PASSWORD", "p")
        monkeypatch.setenv("MEMO_EMAIL_FROM", "ops@example.com")
        monkeypatch.delenv("STATUS_ALERT_TO", raising=False)
        monkeypatch.setenv("MEMO_EMAIL_TO", "operator@example.com")
        _seed_status(monkeypatch, "FAIL")
        o = tool.alert(repo_root=fake_repo)
        assert o.sent is True
        assert disable_smtp[0]["recipients"] == ["operator@example.com"]

    def test_dry_run_does_not_send_or_persist_state(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch, disable_smtp,
    ):
        monkeypatch.setenv("STATUS_ALERT_ENABLED", "1")
        monkeypatch.setenv("MEMO_EMAIL_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("MEMO_EMAIL_USERNAME", "u")
        monkeypatch.setenv("MEMO_EMAIL_PASSWORD", "p")
        monkeypatch.setenv("MEMO_EMAIL_FROM", "ops@example.com")
        monkeypatch.setenv("STATUS_ALERT_TO", "ops@example.com")
        _seed_status(monkeypatch, "FAIL")
        o = tool.alert(repo_root=fake_repo, dry_run=True)
        assert o.sent is False
        assert o.subject is not None
        assert "dry-run" in (o.skipped_reason or "")
        assert disable_smtp == []
        # State NOT advanced — a subsequent real call should fire
        state_path = fake_repo / "outputs" / "policy" / "status_alert_state.json"
        assert not state_path.exists()


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_logs_each_call(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch, disable_smtp,
    ):
        monkeypatch.setenv("STATUS_ALERT_ENABLED", "1")
        monkeypatch.setenv("MEMO_EMAIL_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("MEMO_EMAIL_USERNAME", "u")
        monkeypatch.setenv("MEMO_EMAIL_PASSWORD", "p")
        monkeypatch.setenv("MEMO_EMAIL_FROM", "ops@example.com")
        monkeypatch.setenv("STATUS_ALERT_TO", "ops@example.com")
        _seed_status(monkeypatch, "FAIL")
        tool.alert(repo_root=fake_repo)
        tool.alert(repo_root=fake_repo)  # second call: throttled
        log = fake_repo / "outputs" / "policy" / "status_alert_log.jsonl"
        rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(rows) == 2
        assert rows[0]["sent"] is True
        assert rows[1]["sent"] is False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_disabled_exits_zero(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ):
        monkeypatch.delenv("STATUS_ALERT_ENABLED", raising=False)
        _seed_status(monkeypatch, "FAIL")
        rc = tool.main(["--repo-root", str(fake_repo)])
        out = capsys.readouterr().out
        assert rc == 0
        parsed = json.loads(out)
        assert parsed["sent"] is False

    def test_missing_marker_exits_one_with_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        rc = tool.main(["--repo-root", str(tmp_path)])
        # alert() returns success=False with error → exit 1
        assert rc == 1
