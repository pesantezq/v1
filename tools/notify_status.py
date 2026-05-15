"""
Production status alerter.

Runs tools.status, classifies overall severity, and sends an email when
the system is unhealthy. Cron-ready, idempotent (throttled so repeated
runs do not spam), opt-in (disabled by default).

Cron suggestion (every 15 minutes):

    */15 * * * *  cd /opt/stockbot && /opt/stockbot/.venv/bin/python -m tools.notify_status \
                  >> logs/notify_status.log 2>&1

Environment:

    STATUS_ALERT_ENABLED         0|1  (default 0 — disabled, no-op)
    STATUS_ALERT_THRESHOLD       WARN|FAIL  (default FAIL — only FAIL fires)
    STATUS_ALERT_RESEND_HOURS    int       (default 4 — throttle window)
    STATUS_ALERT_TO              comma-separated recipients
                                  (default: reuse MEMO_EMAIL_TO)
    STATUS_ALERT_SUBJECT_PREFIX  optional prefix (default "[stockbot]")

    Reuses MEMO_EMAIL_* SMTP settings:
        MEMO_EMAIL_SMTP_HOST
        MEMO_EMAIL_SMTP_PORT
        MEMO_EMAIL_USERNAME
        MEMO_EMAIL_PASSWORD
        MEMO_EMAIL_FROM
        MEMO_EMAIL_USE_TLS   (default 1)

Files written:

    outputs/policy/status_alert_state.json    last-alert state (throttle key)
    outputs/policy/status_alert_log.jsonl     append-only audit trail

Safety:

    - Disabled by default; no env var, no email sent.
    - Throttle: the same severity is not re-sent within
      STATUS_ALERT_RESEND_HOURS unless --force.
    - Email body never contains secret values; the env redactor in
      portfolio_automation.env.redact_secrets is applied to the rendered
      status text before send.
    - --dry-run prints the planned message and skips SMTP entirely.

Exit codes:
    0  success / disabled / nothing to send
    1  SMTP error or status read failure
    2  invalid CLI arguments
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import ssl
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_REPO_ROOT_MARKER = "main.py"

_SEV_ORDER = {"OK": 0, "INFO": 1, "WARN": 2, "FAIL": 3}


@dataclass
class AlertOutcome:
    success: bool
    severity: str
    threshold: str
    sent: bool
    skipped_reason: str | None = None
    recipients: list[str] = field(default_factory=list)
    subject: str | None = None
    body_preview: str = ""
    dry_run: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tool": "notify_status",
            "success": self.success,
            "severity": self.severity,
            "threshold": self.threshold,
            "sent": self.sent,
            "skipped_reason": self.skipped_reason,
            "recipients_count": len(self.recipients),
            "subject": self.subject,
            "dry_run": self.dry_run,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Repo + env helpers
# ---------------------------------------------------------------------------

def detect_repo_root(explicit: Path | str | None = None) -> Path:
    if explicit is not None:
        candidate = Path(explicit).resolve()
    else:
        candidate = Path(__file__).resolve().parents[1]
    if not (candidate / _REPO_ROOT_MARKER).exists():
        raise FileNotFoundError(
            f"Repo root marker {_REPO_ROOT_MARKER!r} not found in {candidate}. "
            "Pass --repo-root explicitly."
        )
    return candidate


def _env_bool(name: str, default: bool, env: dict[str, str] | None = None) -> bool:
    raw = (env or os.environ).get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


def _env_str(name: str, default: str = "", env: dict[str, str] | None = None) -> str:
    return (env or os.environ).get(name, default).strip()


def _state_path(repo: Path) -> Path:
    return repo / "outputs" / "policy" / "status_alert_state.json"


def _log_path(repo: Path) -> Path:
    return repo / "outputs" / "policy" / "status_alert_log.jsonl"


def _recipients_from_env() -> list[str]:
    raw = _env_str("STATUS_ALERT_TO") or _env_str("MEMO_EMAIL_TO")
    return [r.strip() for r in raw.split(",") if r.strip()]


# ---------------------------------------------------------------------------
# State (last-sent throttle)
# ---------------------------------------------------------------------------

def _load_state(repo: Path) -> dict[str, Any]:
    p = _state_path(repo)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(repo: Path, state: dict[str, Any]) -> None:
    p = _state_path(repo)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not save state to %s: %s", p, exc)


def _append_log(repo: Path, outcome: AlertOutcome) -> None:
    log = _log_path(repo)
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(outcome.to_dict(), default=str) + "\n")
    except OSError as exc:
        logger.warning("could not append log %s: %s", log, exc)


def _should_skip_for_throttle(
    state: dict[str, Any], severity: str, resend_hours: int,
) -> tuple[bool, str | None]:
    """
    Decide whether to suppress a same-severity alert based on the throttle
    window. Returns (skip, reason).

    Logic:
      - If the previous alert was a DIFFERENT severity (or no prior), don't skip.
      - If same severity and within resend_hours, skip.
      - If same severity and outside resend_hours, send (acts like a re-ping).
    """
    last_sev = state.get("last_alert_severity")
    last_at = state.get("last_alert_at")
    if last_sev != severity:
        return False, None
    if not last_at:
        return False, None
    try:
        last_dt = datetime.fromisoformat(last_at)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return False, None
    age = datetime.now(timezone.utc) - last_dt
    if age < timedelta(hours=resend_hours):
        remaining = (timedelta(hours=resend_hours) - age).total_seconds() / 3600.0
        return True, f"throttled ({remaining:.1f}h remaining)"
    return False, None


# ---------------------------------------------------------------------------
# Body rendering
# ---------------------------------------------------------------------------

def _render_body(report_dict: dict[str, Any]) -> str:
    """
    Render the status report as plain text suitable for email.  Reuses the
    same shape as tools.status.render_text; uses the redactor from
    portfolio_automation.env so secret-like substrings cannot leak.
    """
    try:
        from tools.status import render_text  # type: ignore
        from tools.status import StatusReport, HealthCheck
        report = StatusReport(
            generated_at=report_dict.get("generated_at", ""),
            repo_root=report_dict.get("repo_root", ""),
            checks=[
                HealthCheck(
                    name=c.get("name", "?"),
                    severity=c.get("severity", "INFO"),
                    message=c.get("message", ""),
                    details=c.get("details", {}),
                )
                for c in report_dict.get("checks", [])
            ],
        )
        body = render_text(report, verbose=True)
    except Exception:
        # Fall back to a minimal serialisation if anything import-related breaks
        body = json.dumps(report_dict, indent=2, default=str)
    try:
        from portfolio_automation.env import redact_secrets
        body = redact_secrets(body)
    except Exception:
        pass
    return body


# ---------------------------------------------------------------------------
# SMTP send
# ---------------------------------------------------------------------------

def _send_smtp(
    *,
    subject: str,
    body: str,
    recipients: list[str],
) -> tuple[bool, str | None]:
    host = _env_str("MEMO_EMAIL_SMTP_HOST")
    port_raw = _env_str("MEMO_EMAIL_SMTP_PORT") or "587"
    username = _env_str("MEMO_EMAIL_USERNAME")
    password = _env_str("MEMO_EMAIL_PASSWORD")
    sender = _env_str("MEMO_EMAIL_FROM")
    use_tls = _env_bool("MEMO_EMAIL_USE_TLS", True)

    if not (host and username and password and sender and recipients):
        missing = []
        if not host: missing.append("MEMO_EMAIL_SMTP_HOST")
        if not username: missing.append("MEMO_EMAIL_USERNAME")
        if not password: missing.append("MEMO_EMAIL_PASSWORD")
        if not sender: missing.append("MEMO_EMAIL_FROM")
        if not recipients: missing.append("STATUS_ALERT_TO or MEMO_EMAIL_TO")
        return False, f"missing SMTP config: {', '.join(missing)}"

    try:
        port = int(port_raw)
    except ValueError:
        return False, f"invalid MEMO_EMAIL_SMTP_PORT: {port_raw!r}"

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            if use_tls:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            smtp.login(username, password)
            smtp.sendmail(sender, recipients, msg.as_string())
    except (smtplib.SMTPException, OSError) as exc:
        return False, f"smtp: {type(exc).__name__}: {exc}"
    return True, None


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def alert(
    *,
    repo_root: Path | str | None = None,
    threshold: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> AlertOutcome:
    """Run status, classify, send email if warranted. Never raises."""
    try:
        repo = detect_repo_root(repo_root)
    except FileNotFoundError as exc:
        return AlertOutcome(
            success=False, severity="UNKNOWN", threshold="FAIL", sent=False,
            error=str(exc), dry_run=dry_run,
        )

    if not _env_bool("STATUS_ALERT_ENABLED", False):
        return AlertOutcome(
            success=True, severity="OK", threshold=threshold or "FAIL",
            sent=False, skipped_reason="STATUS_ALERT_ENABLED=0", dry_run=dry_run,
        )

    # Read status
    try:
        from tools.status import collect_status
        report = collect_status(repo)
        severity = report.overall_severity
        report_dict = report.to_dict()
    except Exception as exc:
        return AlertOutcome(
            success=False, severity="UNKNOWN", threshold=threshold or "FAIL",
            sent=False, error=f"status read failed: {exc}", dry_run=dry_run,
        )

    threshold = (threshold or _env_str("STATUS_ALERT_THRESHOLD", "FAIL") or "FAIL").upper()
    if threshold not in ("WARN", "FAIL"):
        threshold = "FAIL"

    outcome = AlertOutcome(
        success=True, severity=severity, threshold=threshold, sent=False,
        dry_run=dry_run,
    )

    if _SEV_ORDER.get(severity, 0) < _SEV_ORDER.get(threshold, 3):
        outcome.skipped_reason = f"severity {severity} below threshold {threshold}"
        _append_log(repo, outcome)
        return outcome

    state = _load_state(repo)
    resend_hours = 4
    try:
        resend_hours = int(_env_str("STATUS_ALERT_RESEND_HOURS", "4") or "4")
    except ValueError:
        resend_hours = 4

    if not force:
        skip, reason = _should_skip_for_throttle(state, severity, resend_hours)
        if skip:
            outcome.skipped_reason = reason
            _append_log(repo, outcome)
            return outcome

    recipients = _recipients_from_env()
    outcome.recipients = recipients
    if not recipients:
        outcome.success = False
        outcome.error = "no recipients (STATUS_ALERT_TO and MEMO_EMAIL_TO both empty)"
        _append_log(repo, outcome)
        return outcome

    subject_prefix = _env_str("STATUS_ALERT_SUBJECT_PREFIX", "[stockbot]") or "[stockbot]"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"{subject_prefix} status {severity} — {today}"
    body = _render_body(report_dict)

    outcome.subject = subject
    outcome.body_preview = "\n".join(body.splitlines()[:5])

    if dry_run:
        outcome.skipped_reason = "dry-run"
        _append_log(repo, outcome)
        return outcome

    ok, err = _send_smtp(subject=subject, body=body, recipients=recipients)
    if not ok:
        outcome.success = False
        outcome.error = err
        _append_log(repo, outcome)
        return outcome

    outcome.sent = True
    state.update({
        "last_alert_severity": severity,
        "last_alert_at": datetime.now(timezone.utc).isoformat(),
        "last_alert_subject": subject,
        "last_alert_recipients_count": len(recipients),
    })
    _save_state(repo, state)
    _append_log(repo, outcome)
    return outcome


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.notify_status",
        description=(
            "Send an email when production status is unhealthy. Disabled "
            "by default; set STATUS_ALERT_ENABLED=1 to activate. Throttled "
            "to avoid spamming; --force overrides."
        ),
    )
    p.add_argument("--repo-root", default=None)
    p.add_argument("--dry-run", action="store_true",
                   help="Prepare the message and print it; do not call SMTP.")
    p.add_argument("--force", action="store_true",
                   help="Send regardless of throttle.")
    p.add_argument("--threshold", choices=("WARN", "FAIL"), default=None,
                   help="Override STATUS_ALERT_THRESHOLD.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    outcome = alert(
        repo_root=args.repo_root,
        threshold=args.threshold,
        force=args.force,
        dry_run=args.dry_run,
    )
    print(json.dumps(outcome.to_dict(), indent=2, default=str))
    return 0 if outcome.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
