# portfolio_automation/brokers/schwab_reauth_notifier.py
"""Schwab re-auth email heads-up. Observe-only; no-trade; non-blocking.

Reads ``broker_sync_status.json``; when the Schwab 7-day refresh token is
``due_soon`` or ``expired`` it sends ONE email per expiry window (per kind) via
the existing ``memo_email_sender`` SMTP transport — same creds, same TLS, same
credential redaction. Disabled by default (``SCHWAB_REAUTH_EMAIL_ENABLED=0``).

The in-system daily-memo AMBER already surfaces this; this module is the optional
out-of-band push so an unattended operator gets a real email before the weekly
browser re-auth is due.

Environment variables (transport is shared with the memo sender):

  SCHWAB_REAUTH_EMAIL_ENABLED   0|1  (default 0 — disabled)
  SCHWAB_REAUTH_EMAIL_DRY_RUN   0|1  (default 0 — real send when enabled)
  SCHWAB_REAUTH_EMAIL_TO        Comma-separated To (default: MEMO_EMAIL_TO)
  SCHWAB_REAUTH_EMAIL_FORCE     0|1  (default 0 — re-send even if already notified)
  MEMO_EMAIL_SMTP_HOST / _PORT / _USERNAME / _PASSWORD / _FROM / _USE_TLS  (shared)

Artifacts (both hard-code observe_only=true, no_trade=true):

  outputs/latest/schwab_reauth_notification_status.json   — per-run status
  outputs/policy/schwab_reauth_notification_log.jsonl     — append-only audit
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

from portfolio_automation.data_governance import OutputNamespace, get_output_path, safe_write_json
from portfolio_automation import memo_email_sender as mes

logger = logging.getLogger("portfolio_automation.brokers.schwab_reauth_notifier")

_STATUS_FILENAME = "schwab_reauth_notification_status.json"
_LOG_FILENAME = "schwab_reauth_notification_log.jsonl"
_BROKER_STATUS_FILENAME = "broker_sync_status.json"

# Only these reauth states warrant an out-of-band email.
_NOTIFY_STATES = ("due_soon", "expired")


def _env_bool(name: str, default: bool, env: dict[str, str]) -> bool:
    raw = env.get(name, "")
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


def should_notify(reauth_status: str | None) -> bool:
    return reauth_status in _NOTIFY_STATES


def _read_broker_status(base: Path) -> dict[str, Any]:
    path = get_output_path(OutputNamespace.LATEST, _BROKER_STATUS_FILENAME, base_dir=base)
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def build_reauth_message(cfg: mes.MemoEmailConfig, *, reauth_status: str,
                         days_remaining: float | None, expires_at: str | None) -> EmailMessage:
    """Plain-text re-auth heads-up. No secrets; carries the bootstrap commands."""
    msg = EmailMessage()
    prefix = f"{cfg.subject_prefix} " if cfg.subject_prefix else ""
    if reauth_status == "expired":
        msg["Subject"] = f"{prefix}Schwab re-auth EXPIRED — broker sync is unauthenticated"
        lede = ("The Schwab 7-day refresh token has EXPIRED. The daily read-only sync is now "
                "unauthenticated and will stay degraded until you re-authorize.")
    else:
        days = f"{days_remaining:g}" if days_remaining is not None else "≤2"
        msg["Subject"] = f"{prefix}Schwab re-auth due in {days} day(s)"
        lede = (f"The Schwab 7-day refresh token expires in ~{days} day(s) "
                f"({expires_at or 'soon'}). Re-authorize before then to keep the daily "
                "read-only sync alive — Schwab issues no rolling replacement.")
    msg["From"] = cfg.from_addr
    msg["To"] = ", ".join(cfg.to_addrs)
    body = (
        f"{lede}\n\n"
        "One-time browser re-auth (~30s; clears Schwab MFA, so you're notified of the login):\n\n"
        "  cd /opt/stockbot && set -a; . ./.env; set +a\n"
        "  .venv/bin/python3 -c \"from portfolio_automation.brokers import schwab_oauth as oa; "
        "print(oa.build_authorize_url())\"\n"
        "  # open the URL, log in, copy the ?code=... from the redirect, then:\n"
        "  .venv/bin/python3 -c \"from portfolio_automation.brokers import schwab_oauth as oa; "
        "oa.exchange_code('PASTE_CODE_HERE')\"\n\n"
        "See docs/schwab_integration.md → 'Re-authentication: the 7-day refresh-token clock'.\n\n"
        "Advisory only — this system never executes trades.\n"
    )
    msg.set_content(body)
    return msg


def _load_transport(env: dict[str, str]) -> mes.MemoEmailConfig:
    """Reuse the memo sender's SMTP transport; override gate/dry-run/recipients
    with the re-auth-specific envs so the alert is enabled independently."""
    cfg = mes.load_memo_email_config(env=env)
    cfg.dry_run = _env_bool("SCHWAB_REAUTH_EMAIL_DRY_RUN", False, env)
    to_raw = env.get("SCHWAB_REAUTH_EMAIL_TO", "").strip()
    if to_raw:
        cfg.to_addrs = [a.strip() for a in to_raw.replace(";", ",").split(",") if a.strip()]
    return cfg


def _dedup_key(reauth_status: str, expires_at: str | None) -> str:
    # One email per (kind, expiry window). A fresh re-auth mints a new expires_at
    # → the notifier re-arms automatically; expired re-notifies once (distinct kind).
    return f"{reauth_status}|{expires_at or 'none'}"


def _load_log(base: Path) -> list[dict[str, Any]]:
    path = get_output_path(OutputNamespace.POLICY, _LOG_FILENAME, base_dir=base)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            except json.JSONDecodeError:
                pass
    except Exception:
        pass
    return out


def _already_notified(reauth_status: str, expires_at: str | None, base: Path) -> bool:
    key = _dedup_key(reauth_status, expires_at)
    return any(e.get("sent") and e.get("dedup_key") == key for e in _load_log(base))


def _append_log(entry: dict[str, Any], base: Path) -> None:
    path = get_output_path(OutputNamespace.POLICY, _LOG_FILENAME, base_dir=base)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")


def _write_artifacts(status: dict[str, Any], write_files: bool, base: Path) -> None:
    if not write_files:
        return
    try:
        safe_write_json(OutputNamespace.LATEST, _STATUS_FILENAME, status, base_dir=base)
    except Exception as exc:
        logger.warning("REAUTH NOTIFY: status write failed — %s", exc)
    try:
        _append_log({
            "generated_at": status.get("generated_at"), "reauth_status": status.get("reauth_status"),
            "reauth_expires_at": status.get("reauth_expires_at"), "dedup_key": status.get("dedup_key"),
            "enabled": status.get("enabled"), "dry_run": status.get("dry_run"),
            "attempted": status.get("attempted"), "sent": status.get("sent"),
            "skipped": status.get("skipped"), "reason": status.get("reason"),
            "error_class": status.get("error_class"),
            "observe_only": True, "no_trade": True,
        }, base)
    except Exception as exc:
        logger.warning("REAUTH NOTIFY: log append failed — %s", exc)


def run_reauth_notification(*, base_dir: str | Path = "outputs",
                            env: dict[str, str] | None = None, write_files: bool = True,
                            sender: Callable[[mes.MemoEmailConfig, EmailMessage], dict] | None = None,
                            ) -> dict[str, Any]:
    """Send a re-auth heads-up email when warranted. Always returns a status dict;
    never raises (observe-only, non-blocking)."""
    env = dict(env if env is not None else os.environ)
    base = Path(base_dir)
    now = datetime.now(timezone.utc)
    bss = _read_broker_status(base)
    reauth_status = bss.get("reauth_status", "unknown")
    expires_at = bss.get("reauth_expires_at")
    days = bss.get("reauth_days_remaining")

    status: dict[str, Any] = {
        "generated_at": now.isoformat(), "observe_only": True, "no_trade": True,
        "enabled": _env_bool("SCHWAB_REAUTH_EMAIL_ENABLED", False, env),
        "reauth_status": reauth_status, "reauth_expires_at": expires_at,
        "reauth_days_remaining": days, "dedup_key": _dedup_key(reauth_status, expires_at),
        "dry_run": False, "attempted": False, "sent": False, "skipped": False,
        "reason": None, "recipients_count": 0, "error_class": None,
        "error_message_sanitized": None,
    }

    if not status["enabled"]:
        status.update(skipped=True, reason="disabled")
        _write_artifacts(status, write_files, base)
        return status
    if not should_notify(reauth_status):
        status.update(skipped=True, reason="no_action_needed")
        _write_artifacts(status, write_files, base)
        return status

    cfg = _load_transport(env)
    status["dry_run"] = cfg.dry_run
    status["recipients_count"] = len(cfg.to_addrs)
    if not cfg.has_valid_recipients():
        status.update(skipped=True, reason="invalid_or_missing_recipients")
        _write_artifacts(status, write_files, base)
        return status
    if not cfg.dry_run and not cfg.has_smtp_config():
        status.update(skipped=True, reason="missing_smtp_config")
        _write_artifacts(status, write_files, base)
        return status
    if not _env_bool("SCHWAB_REAUTH_EMAIL_FORCE", False, env) and \
            _already_notified(reauth_status, expires_at, base):
        status.update(skipped=True, reason="already_notified")
        _write_artifacts(status, write_files, base)
        return status

    try:
        msg = build_reauth_message(cfg, reauth_status=reauth_status,
                                   days_remaining=days, expires_at=expires_at)
    except Exception as exc:
        status.update(reason="message_build_error", error_class=type(exc).__name__)
        _write_artifacts(status, write_files, base)
        return status

    send = sender or mes.send_daily_memo_email
    try:
        res = send(cfg, msg)
    except Exception as exc:  # strict transport never enabled here, but stay non-blocking
        status.update(attempted=True, reason="smtp_error", error_class=type(exc).__name__,
                      error_message_sanitized=mes._sanitize_error(exc))
        _write_artifacts(status, write_files, base)
        return status

    status["attempted"] = res.get("attempted", False)
    status["sent"] = res.get("sent", False)
    status["error_class"] = res.get("error_class")
    status["error_message_sanitized"] = res.get("error_message_sanitized")
    status["reason"] = "dry_run" if cfg.dry_run else ("sent" if status["sent"] else "send_failed")
    _write_artifacts(status, write_files, base)
    return status


def _cli_main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m portfolio_automation.brokers.schwab_reauth_notifier",
                                 description="Schwab re-auth email heads-up (observe-only).")
    ap.add_argument("--dry-run", action="store_true", help="Build + gate but do not send")
    ap.add_argument("--send", action="store_true", help="Send if due_soon/expired")
    args = ap.parse_args(argv)
    env = dict(os.environ)
    env["SCHWAB_REAUTH_EMAIL_ENABLED"] = "1"
    env["SCHWAB_REAUTH_EMAIL_DRY_RUN"] = "0" if args.send else "1"
    r = run_reauth_notification(env=env)
    print(f"enabled={r['enabled']} reauth_status={r['reauth_status']} "
          f"attempted={r['attempted']} sent={r['sent']} skipped={r['skipped']} reason={r['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
