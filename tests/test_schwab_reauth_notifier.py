import json

from portfolio_automation.brokers import schwab_reauth_notifier as rn
from portfolio_automation.data_governance import OutputNamespace, get_output_path


def _write_bss(base, **fields):
    p = get_output_path(OutputNamespace.LATEST, "broker_sync_status.json", base_dir=base)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"source": "schwab", **fields}), encoding="utf-8")


_SMTP_ENV = {
    "SCHWAB_REAUTH_EMAIL_ENABLED": "1", "SCHWAB_REAUTH_EMAIL_DRY_RUN": "0",
    "MEMO_EMAIL_SMTP_HOST": "smtp.test", "MEMO_EMAIL_USERNAME": "u",
    "MEMO_EMAIL_PASSWORD": "secret-pw", "MEMO_EMAIL_FROM": "from@test.com",
    "MEMO_EMAIL_TO": "to@test.com",
}


class _FakeSender:
    def __init__(self):
        self.calls = []

    def __call__(self, cfg, msg):
        self.calls.append((cfg, msg))
        return {"attempted": True, "sent": True, "error_class": None, "error_message_sanitized": None}


def test_ok_status_no_send(tmp_path):
    _write_bss(tmp_path, reauth_status="ok", reauth_expires_at="2026-06-19T00:00:00+00:00",
               reauth_days_remaining=5.0)
    sender = _FakeSender()
    st = rn.run_reauth_notification(base_dir=tmp_path, env=_SMTP_ENV, sender=sender)
    assert st["skipped"] is True and st["reason"] == "no_action_needed"
    assert sender.calls == []  # healthy state -> no email


def test_due_soon_sends_once(tmp_path):
    _write_bss(tmp_path, reauth_status="due_soon", reauth_expires_at="2026-06-19T00:00:00+00:00",
               reauth_days_remaining=1.5)
    sender = _FakeSender()
    st = rn.run_reauth_notification(base_dir=tmp_path, env=_SMTP_ENV, sender=sender)
    assert st["sent"] is True and st["reason"] == "sent"
    assert len(sender.calls) == 1
    msg = sender.calls[0][1]
    assert "re-auth due" in msg["Subject"].lower()
    assert "exchange_code" in msg.get_content()  # carries the bootstrap command


def test_idempotent_same_window(tmp_path):
    _write_bss(tmp_path, reauth_status="due_soon", reauth_expires_at="2026-06-19T00:00:00+00:00",
               reauth_days_remaining=1.5)
    sender = _FakeSender()
    rn.run_reauth_notification(base_dir=tmp_path, env=_SMTP_ENV, sender=sender)
    st2 = rn.run_reauth_notification(base_dir=tmp_path, env=_SMTP_ENV, sender=sender)
    assert st2["skipped"] is True and st2["reason"] == "already_notified"
    assert len(sender.calls) == 1  # second run does NOT re-send the same window


def test_expired_renotifies_after_due_soon(tmp_path):
    # due_soon already emailed for this window...
    _write_bss(tmp_path, reauth_status="due_soon", reauth_expires_at="2026-06-19T00:00:00+00:00",
               reauth_days_remaining=1.0)
    sender = _FakeSender()
    rn.run_reauth_notification(base_dir=tmp_path, env=_SMTP_ENV, sender=sender)
    # ...now it actually expired (distinct kind) -> one more alarm
    _write_bss(tmp_path, reauth_status="expired", reauth_expires_at="2026-06-19T00:00:00+00:00",
               reauth_days_remaining=-0.1)
    st = rn.run_reauth_notification(base_dir=tmp_path, env=_SMTP_ENV, sender=sender)
    assert st["sent"] is True and len(sender.calls) == 2
    assert "expired" in sender.calls[1][1]["Subject"].lower()


def test_disabled_gate(tmp_path):
    _write_bss(tmp_path, reauth_status="due_soon", reauth_expires_at="2026-06-19T00:00:00+00:00",
               reauth_days_remaining=1.0)
    sender = _FakeSender()
    env = dict(_SMTP_ENV, SCHWAB_REAUTH_EMAIL_ENABLED="0")
    st = rn.run_reauth_notification(base_dir=tmp_path, env=env, sender=sender)
    assert st["skipped"] is True and st["reason"] == "disabled"
    assert sender.calls == []


def test_no_secret_in_artifacts(tmp_path):
    _write_bss(tmp_path, reauth_status="due_soon", reauth_expires_at="2026-06-19T00:00:00+00:00",
               reauth_days_remaining=1.0)
    rn.run_reauth_notification(base_dir=tmp_path, env=_SMTP_ENV, sender=_FakeSender())
    blob = get_output_path(OutputNamespace.LATEST, "schwab_reauth_notification_status.json",
                           base_dir=tmp_path).read_text()
    blob += get_output_path(OutputNamespace.POLICY, "schwab_reauth_notification_log.jsonl",
                            base_dir=tmp_path).read_text()
    assert "secret-pw" not in blob and "password" not in blob.lower()


def test_missing_smtp_config_skips(tmp_path):
    _write_bss(tmp_path, reauth_status="expired", reauth_expires_at="2026-06-19T00:00:00+00:00",
               reauth_days_remaining=-1.0)
    env = {"SCHWAB_REAUTH_EMAIL_ENABLED": "1", "SCHWAB_REAUTH_EMAIL_DRY_RUN": "0",
           "MEMO_EMAIL_TO": "to@test.com"}  # recipients valid but no SMTP host/user/pass
    st = rn.run_reauth_notification(base_dir=tmp_path, env=env, sender=_FakeSender())
    assert st["skipped"] is True and st["reason"] == "missing_smtp_config"
