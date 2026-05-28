"""
Memo Email Sender
=================
Delivers the daily portfolio memo (outputs/latest/daily_memo.txt / .md)
by email.  Disabled by default (MEMO_EMAIL_ENABLED=0).  Non-blocking
unless MEMO_EMAIL_STRICT_FAILURE=1.

CLI::

    python -m portfolio_automation.memo_email_sender --dry-run
    python -m portfolio_automation.memo_email_sender --send
    python -m portfolio_automation.memo_email_sender --force-resend

Environment variables:

  MEMO_EMAIL_ENABLED          0|1  (default 0 — disabled)
  MEMO_EMAIL_DRY_RUN          0|1  (default 1 — dry-run; ignored by CLI)
  MEMO_EMAIL_SMTP_HOST        SMTP server hostname
  MEMO_EMAIL_SMTP_PORT        port number (default 587)
  MEMO_EMAIL_USERNAME         SMTP auth username
  MEMO_EMAIL_PASSWORD         SMTP auth password (never logged or stored in artifacts)
  MEMO_EMAIL_FROM             From address
  MEMO_EMAIL_TO               Comma-separated To recipients
  MEMO_EMAIL_CC               Comma-separated CC (optional)
  MEMO_EMAIL_BCC              Comma-separated BCC (optional)
  MEMO_EMAIL_USE_TLS          0|1  (default 1 — STARTTLS)
  MEMO_EMAIL_SUBJECT_PREFIX   Optional prefix prepended to the subject line
  MEMO_EMAIL_STRICT_FAILURE   0|1  (default 0 — non-blocking on error)
  MEMO_EMAIL_FORCE_RESEND     0|1  (default 0 — skip already-sent dates)

Artifacts:

  outputs/latest/memo_delivery_status.json   — per-run delivery status
  outputs/policy/memo_delivery_log.jsonl     — append-only audit log

Governance:

  observe_only: true, no_trade: true hard-coded in every artifact.
  No market-data APIs, no AI/LLM calls, no portfolio-state mutations.
"""
from __future__ import annotations

import html as _html
import json
import logging
import os
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import markdown as _markdown

from portfolio_automation.data_governance import (
    OutputNamespace,
    get_output_path,
    safe_write_json,
)

logger = logging.getLogger("portfolio_automation.memo_email_sender")

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_STATUS_FILENAME = "memo_delivery_status.json"
_LOG_FILENAME    = "memo_delivery_log.jsonl"

# Memo source files live at {base_dir}/latest/
_MEMO_TXT_NAME = "daily_memo.txt"
_MEMO_MD_NAME  = "daily_memo.md"


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool, env: dict[str, str] | None = None) -> bool:
    raw = (env or os.environ).get(name, "")
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


def _env_str(name: str, default: str = "", env: dict[str, str] | None = None) -> str:
    return (env or os.environ).get(name, default).strip()


def _parse_addrs(raw: str) -> list[str]:
    """Split comma- or semicolon-separated address string; drop blank entries."""
    if not raw:
        return []
    parts = [a.strip() for a in raw.replace(";", ",").split(",")]
    return [p for p in parts if p]


def _validate_addr(addr: str) -> bool:
    """Minimal sanity check: must contain exactly one '@' with non-empty parts."""
    parts = addr.split("@")
    return len(parts) == 2 and bool(parts[0]) and bool(parts[1])


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------

@dataclass
class MemoEmailConfig:
    enabled: bool = False
    dry_run: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    username: str = ""
    password: str = field(default="", repr=False)  # excluded from repr/logs
    from_addr: str = ""
    to_addrs: list[str] = field(default_factory=list)
    cc_addrs: list[str] = field(default_factory=list)
    bcc_addrs: list[str] = field(default_factory=list)
    use_tls: bool = True
    subject_prefix: str = ""
    strict_failure: bool = False
    force_resend: bool = False

    def has_valid_recipients(self) -> bool:
        return bool(self.to_addrs) and all(_validate_addr(a) for a in self.to_addrs)

    def has_smtp_config(self) -> bool:
        return bool(self.smtp_host and self.username and self.password and self.from_addr)


# ---------------------------------------------------------------------------
# Public: load config
# ---------------------------------------------------------------------------

def load_memo_email_config(
    env: dict[str, str] | None = None,
) -> MemoEmailConfig:
    """
    Load MemoEmailConfig from environment variables (or an explicit dict for testing).

    The password is read but never logged or written to any artifact.
    """
    cfg = MemoEmailConfig(
        enabled=_env_bool("MEMO_EMAIL_ENABLED", False, env),
        dry_run=_env_bool("MEMO_EMAIL_DRY_RUN", True, env),
        smtp_host=_env_str("MEMO_EMAIL_SMTP_HOST", "", env),
        smtp_port=int(_env_str("MEMO_EMAIL_SMTP_PORT", "587", env) or "587"),
        username=_env_str("MEMO_EMAIL_USERNAME", "", env),
        from_addr=_env_str("MEMO_EMAIL_FROM", "", env),
        to_addrs=_parse_addrs(_env_str("MEMO_EMAIL_TO", "", env)),
        cc_addrs=_parse_addrs(_env_str("MEMO_EMAIL_CC", "", env)),
        bcc_addrs=_parse_addrs(_env_str("MEMO_EMAIL_BCC", "", env)),
        use_tls=_env_bool("MEMO_EMAIL_USE_TLS", True, env),
        subject_prefix=_env_str("MEMO_EMAIL_SUBJECT_PREFIX", "", env),
        strict_failure=_env_bool("MEMO_EMAIL_STRICT_FAILURE", False, env),
        force_resend=_env_bool("MEMO_EMAIL_FORCE_RESEND", False, env),
    )
    cfg.password = _env_str("MEMO_EMAIL_PASSWORD", "", env)
    return cfg


# ---------------------------------------------------------------------------
# HTML rendering (email body alternative)
# ---------------------------------------------------------------------------

_EMAIL_FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
)

# Left-accent color per section. Matching is case-insensitive prefix.
_SECTION_ACCENTS: dict[str, str] = {
    "today's verdict":      "#f59e0b",   # amber
    "top insight":          "#6366f1",   # indigo
    "top decisions":        "#3b82f6",   # blue
    "capital actions":      "#3b82f6",   # blue
    "risk focus":           "#dc2626",   # red
    "what changed":         "#64748b",   # slate
    "portfolio pulse":      "#0ea5e9",   # sky
    "risk delta":           "#dc2626",   # red
    "advisor stack":        "#8b5cf6",   # violet
    "portfolio growth":     "#10b981",   # emerald
    "top movers":           "#10b981",   # emerald
    "decision hit rate":    "#3b82f6",   # blue
    "what to watch":        "#64748b",   # slate
    "system / data health": "#64748b",   # slate
    "discovery research":   "#64748b",   # slate
}
_DEFAULT_ACCENT = "#3b82f6"

_SECTION_RE = re.compile(r"^## +(.+)$", re.MULTILINE)
_PCT_GAIN_RE = re.compile(r"\+(\d+(?:\.\d+)?)%")
_PCT_LOSS_RE = re.compile(r"(?<![\d.])-(\d+(?:\.\d+)?)%")


def _accent_for_section(title: str) -> str:
    key = " ".join(title.strip().lower().split())
    for prefix, color in _SECTION_ACCENTS.items():
        if key.startswith(prefix):
            return color
    return _DEFAULT_ACCENT


def _colorize_percentages(html_str: str) -> str:
    """Color +x.xx% green and -x.xx% red. Runs after markdown render so HTML tags are untouched."""
    html_str = _PCT_GAIN_RE.sub(
        r'<span style="color:#059669;font-weight:600">+\1%</span>',
        html_str,
    )
    html_str = _PCT_LOSS_RE.sub(
        r'<span style="color:#dc2626;font-weight:600">-\1%</span>',
        html_str,
    )
    return html_str


def _style_inline_tags(body_html: str) -> str:
    """Apply inline styles to the tags produced by python-markdown."""
    replacements = {
        "<ul>": '<ul style="margin:6px 0 0 0;padding-left:20px;color:#334155;line-height:1.55;">',
        "<ol>": '<ol style="margin:6px 0 0 0;padding-left:20px;color:#334155;line-height:1.55;">',
        "<li>": '<li style="margin:4px 0;">',
        "<p>":  '<p style="margin:6px 0 0 0;color:#334155;line-height:1.55;font-size:14px;">',
        "<strong>": '<strong style="color:#0f172a;font-weight:600;">',
        "<em>": '<em style="color:#334155;">',
        "<code>": (
            '<code style="background:#f1f5f9;color:#0f172a;padding:1px 6px;'
            "border-radius:4px;font-family:ui-monospace,Menlo,Consolas,monospace;"
            'font-size:12.5px;">'
        ),
        "<blockquote>": (
            '<blockquote style="margin:6px 0 0 0;padding:10px 14px;background:#fefce8;'
            "border-left:3px solid #f59e0b;color:#1f2937;font-size:14px;line-height:1.5;"
            'border-radius:4px;">'
        ),
    }
    for raw, styled in replacements.items():
        body_html = body_html.replace(raw, styled)
    return body_html


def _render_section_body(md_body: str) -> str:
    if not md_body.strip():
        return ""
    raw_html = _markdown.markdown(md_body.strip(), extensions=["extra"])
    styled = _style_inline_tags(raw_html)
    return _colorize_percentages(styled)


def _split_sections(md_text: str) -> list[tuple[str, str]]:
    """Split markdown into [(heading, body), ...] using `## ` as the boundary."""
    parts = _SECTION_RE.split(md_text)
    # parts[0] is preamble (anything before first ## heading) — discarded.
    sections: list[tuple[str, str]] = []
    it = iter(parts[1:])
    for heading in it:
        body = next(it, "")
        sections.append((heading.strip(), body.strip()))
    return sections


def render_memo_html(memo_md: str, memo_date: str) -> str:
    """Render daily_memo.md as a self-contained, inline-styled HTML email body.

    Returns "" if memo_md is empty so callers can decide whether to add an
    HTML alternative at all.
    """
    if not memo_md or not memo_md.strip():
        return ""

    # Drop everything before the first `## ` heading (title + metadata block).
    lines = memo_md.splitlines()
    first_section_idx = next(
        (i for i, ln in enumerate(lines) if ln.startswith("## ")),
        len(lines),
    )
    body_text = "\n".join(lines[first_section_idx:])
    # Drop trailing horizontal-rule footer; we render our own.
    body_text = re.sub(r"\n---\s*\n.*$", "", body_text, flags=re.DOTALL)

    section_rows: list[str] = []
    for heading, body in _split_sections(body_text):
        accent = _accent_for_section(heading)
        body_html = _render_section_body(body)
        section_rows.append(
            '<tr><td style="padding:0 0 12px 0;">'
            '<div style="background:#ffffff;border:1px solid #e2e8f0;'
            f"border-left:4px solid {accent};border-radius:6px;padding:14px 18px;\">"
            '<div style="font-size:11px;letter-spacing:0.08em;text-transform:uppercase;'
            f'color:{accent};font-weight:700;">{_html.escape(heading, quote=False)}</div>'
            f'<div style="margin-top:4px;">{body_html}</div>'
            "</div></td></tr>"
        )

    safe_date = _html.escape(memo_date, quote=False)
    sections_html = "\n".join(section_rows)
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<title>Daily Investment Memo — {safe_date}</title></head>"
        f'<body style="margin:0;padding:0;background:#f5f7fa;font-family:{_EMAIL_FONT};color:#0f172a;">'
        '<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" '
        'style="background:#f5f7fa;padding:24px 12px;"><tr><td align="center">'
        '<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="640" '
        'style="max-width:640px;width:100%;">'
        '<tr><td style="padding:0 0 16px 0;">'
        '<div style="background:#0f172a;color:#f8fafc;border-radius:8px;padding:18px 22px;">'
        '<div style="font-size:20px;font-weight:700;letter-spacing:-0.01em;">Daily Investment Memo</div>'
        f'<div style="margin-top:4px;font-size:13px;color:#cbd5e1;">{safe_date} · Advisory only — no trades executed</div>'
        "</div></td></tr>"
        f"{sections_html}"
        '<tr><td style="padding:12px 4px 0 4px;font-size:11px;color:#94a3b8;text-align:center;">'
        "Generated by the Portfolio Automation System · Advisory output only"
        "</td></tr>"
        "</table></td></tr></table></body></html>"
    )


# ---------------------------------------------------------------------------
# Public: build message
# ---------------------------------------------------------------------------

def build_memo_email_message(
    config: MemoEmailConfig,
    memo_txt: str,
    memo_md: str,
    run_id: str,
    memo_date: str,
) -> EmailMessage:
    """
    Build an EmailMessage with plain-text body and optional Markdown attachment.

    The plain-text body comes from memo_txt.  memo_md (if non-empty) is
    attached as ``daily_memo_{memo_date}.md`` for clients that can render it.
    """
    msg = EmailMessage()
    prefix = f"{config.subject_prefix} " if config.subject_prefix else ""
    msg["Subject"] = f"{prefix}Portfolio Daily Memo — {memo_date}"
    msg["From"] = config.from_addr
    msg["To"] = ", ".join(config.to_addrs)
    if config.cc_addrs:
        msg["Cc"] = ", ".join(config.cc_addrs)

    msg.set_content(memo_txt or "(No memo content available)")

    if memo_md:
        try:
            html_body = render_memo_html(memo_md, memo_date)
        except Exception as exc:
            logger.warning("MEMO EMAIL: HTML render failed, plain-text only — %s", exc)
            html_body = ""
        if html_body:
            msg.add_alternative(html_body, subtype="html")

        msg.add_attachment(
            memo_md.encode("utf-8"),
            maintype="text",
            subtype="markdown",
            filename=f"daily_memo_{memo_date}.md",
        )
    return msg


# ---------------------------------------------------------------------------
# Public: send
# ---------------------------------------------------------------------------

def _sanitize_error(exc: Exception) -> str:
    """Return a short error string that cannot expose credentials."""
    raw = repr(exc)
    for hint in ("password", "passwd", "secret", "token", "auth", "pass"):
        idx = raw.lower().find(hint)
        if idx != -1:
            raw = raw[:idx] + "<redacted>"
            break
    return raw[:200]


def send_daily_memo_email(
    config: MemoEmailConfig,
    message: EmailMessage,
) -> dict[str, Any]:
    """
    Send the message via SMTP.

    Returns a result dict — never raises unless config.strict_failure=True.
    Dry-run: returns immediately without connecting to SMTP.
    """
    result: dict[str, Any] = {
        "sent": False,
        "dry_run": config.dry_run,
        "attempted": False,
        "error_class": None,
        "error_message_sanitized": None,
    }

    if config.dry_run:
        logger.info("MEMO EMAIL: dry-run — message built, not sent")
        result["reason"] = "dry_run"
        return result

    result["attempted"] = True
    try:
        all_rcpt = config.to_addrs + config.cc_addrs + config.bcc_addrs
        if config.use_tls:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(config.smtp_host, config.smtp_port) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ctx)
                smtp.login(config.username, config.password)
                smtp.send_message(message, to_addrs=all_rcpt)
        else:
            with smtplib.SMTP(config.smtp_host, config.smtp_port) as smtp:
                smtp.login(config.username, config.password)
                smtp.send_message(message, to_addrs=all_rcpt)
        result["sent"] = True
        logger.info("MEMO EMAIL: sent to %d recipient(s)", len(config.to_addrs))
    except Exception as exc:
        result["error_class"] = type(exc).__name__
        result["error_message_sanitized"] = _sanitize_error(exc)
        logger.warning(
            "MEMO EMAIL: send failed — %s: %s",
            result["error_class"],
            result["error_message_sanitized"],
        )
        if config.strict_failure:
            raise
    return result


# ---------------------------------------------------------------------------
# Public: artifact writers
# ---------------------------------------------------------------------------

def write_memo_delivery_status(
    status_data: dict[str, Any],
    base_dir: str | Path = "outputs",
) -> Path:
    """Write per-run delivery status to outputs/latest/memo_delivery_status.json."""
    return safe_write_json(
        OutputNamespace.LATEST,
        _STATUS_FILENAME,
        status_data,
        base_dir=base_dir,
    )


def append_memo_delivery_log(
    log_entry: dict[str, Any],
    base_dir: str | Path = "outputs",
) -> Path:
    """Append one delivery log entry to outputs/policy/memo_delivery_log.jsonl."""
    path = get_output_path(OutputNamespace.POLICY, _LOG_FILENAME, base_dir=base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(log_entry, default=str) + "\n")
    return path


# ---------------------------------------------------------------------------
# Internal: idempotency
# ---------------------------------------------------------------------------

def _load_delivery_log(base_dir: Path) -> list[dict[str, Any]]:
    """Load existing delivery log entries; tolerates missing/corrupt file."""
    path = get_output_path(OutputNamespace.POLICY, _LOG_FILENAME, base_dir=base_dir)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    entries.append(obj)
            except json.JSONDecodeError:
                pass
    except Exception:
        pass
    return entries


def _already_sent(run_id: str, memo_date: str, base_dir: Path) -> bool:
    """Return True if any log entry for this run_id or memo_date has sent=True."""
    for entry in _load_delivery_log(base_dir):
        if not entry.get("sent"):
            continue
        if entry.get("run_id") == run_id:
            return True
        if entry.get("memo_date") == memo_date:
            return True
    return False


# ---------------------------------------------------------------------------
# Internal: finalize (write artifacts)
# ---------------------------------------------------------------------------

def _build_log_entry(status: dict[str, Any]) -> dict[str, Any]:
    """Extract log-safe subset for the append-only JSONL delivery log."""
    return {
        "generated_at": status.get("generated_at"),
        "run_id": status.get("run_id"),
        "memo_date": status.get("memo_date"),
        "enabled": status.get("enabled"),
        "dry_run": status.get("dry_run"),
        "attempted": status.get("attempted"),
        "sent": status.get("sent"),
        "skipped": status.get("skipped"),
        "reason": status.get("reason"),
        "recipients_count": status.get("recipients_count", 0),
        "error_class": status.get("error_class"),
        "observe_only": True,
        "no_trade": True,
    }


def _write_artifacts(
    status: dict[str, Any],
    write_files: bool,
    base_dir: Path,
) -> None:
    if not write_files:
        return
    try:
        write_memo_delivery_status(status, base_dir=base_dir)
    except Exception as exc:
        logger.warning("MEMO EMAIL: could not write delivery status — %s", exc)
    try:
        append_memo_delivery_log(_build_log_entry(status), base_dir=base_dir)
    except Exception as exc:
        logger.warning("MEMO EMAIL: could not append delivery log — %s", exc)


# ---------------------------------------------------------------------------
# Public: main orchestrator
# ---------------------------------------------------------------------------

def run_memo_email_delivery(
    *,
    run_id: str | None = None,
    base_dir: str | Path = "outputs",
    write_files: bool = True,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Full delivery pipeline.

    Always returns a status dict — never raises unless
    config.strict_failure=True *and* SMTP raises.

    Governance fields hard-coded in every returned dict:
      observe_only: True, no_trade: True
    """
    now = datetime.now()
    base = Path(base_dir)
    memo_date = now.strftime("%Y-%m-%d")
    _run_id = run_id or f"{memo_date}_memo_delivery"

    status: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "observe_only": True,
        "no_trade": True,
        "available": False,
        "enabled": False,
        "dry_run": True,
        "attempted": False,
        "sent": False,
        "skipped": False,
        "reason": None,
        "run_id": _run_id,
        "memo_date": memo_date,
        "memo_source_txt": None,
        "memo_source_md": None,
        "recipients_count": 0,
        "cc_count": 0,
        "bcc_count": 0,
        "smtp_host_present": False,
        "username_present": False,
        "error_class": None,
        "error_message_sanitized": None,
    }

    # ── Load config ────────────────────────────────────────────────────────
    try:
        cfg = load_memo_email_config(env=env)
    except Exception as exc:
        status["reason"] = f"config_error: {type(exc).__name__}"
        logger.warning("MEMO EMAIL: config load failed — %s", exc)
        _write_artifacts(status, write_files, base)
        return status

    status["enabled"] = cfg.enabled
    status["dry_run"] = cfg.dry_run
    status["smtp_host_present"] = bool(cfg.smtp_host)
    status["username_present"] = bool(cfg.username)
    status["recipients_count"] = len(cfg.to_addrs)
    status["cc_count"] = len(cfg.cc_addrs)
    status["bcc_count"] = len(cfg.bcc_addrs)

    # ── Gate: disabled ─────────────────────────────────────────────────────
    if not cfg.enabled:
        status["skipped"] = True
        status["reason"] = "disabled"
        logger.debug("MEMO EMAIL: disabled (MEMO_EMAIL_ENABLED not set)")
        _write_artifacts(status, write_files, base)
        return status

    # ── Gate: missing/invalid recipients ──────────────────────────────────
    if not cfg.has_valid_recipients():
        status["skipped"] = True
        status["reason"] = "invalid_or_missing_recipients"
        logger.warning("MEMO EMAIL: no valid recipients configured")
        _write_artifacts(status, write_files, base)
        return status

    # ── Gate: missing SMTP config (non-dry-run only) ───────────────────────
    if not cfg.dry_run and not cfg.has_smtp_config():
        status["skipped"] = True
        status["reason"] = "missing_smtp_config"
        logger.warning("MEMO EMAIL: SMTP config incomplete — skipping")
        _write_artifacts(status, write_files, base)
        return status

    # ── Gate: idempotency (actual sends only) ──────────────────────────────
    if not cfg.dry_run and not cfg.force_resend:
        if _already_sent(_run_id, memo_date, base):
            status["skipped"] = True
            status["reason"] = "already_sent"
            logger.info(
                "MEMO EMAIL: already sent for run_id=%s date=%s — skipping",
                _run_id,
                memo_date,
            )
            _write_artifacts(status, write_files, base)
            return status

    # ── Load memo files ────────────────────────────────────────────────────
    txt_path = get_output_path(OutputNamespace.LATEST, _MEMO_TXT_NAME, base_dir=base)
    md_path  = get_output_path(OutputNamespace.LATEST, _MEMO_MD_NAME,  base_dir=base)
    memo_txt = ""
    memo_md  = ""

    status["memo_source_txt"] = str(txt_path)
    status["memo_source_md"]  = str(md_path)

    try:
        if txt_path.exists():
            memo_txt = txt_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("MEMO EMAIL: could not read %s — %s", txt_path, exc)

    try:
        if md_path.exists():
            memo_md = md_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("MEMO EMAIL: could not read %s — %s", md_path, exc)

    status["available"] = bool(memo_txt or memo_md)

    if not memo_txt and not memo_md:
        status["skipped"] = True
        status["reason"] = "memo_file_missing"
        logger.warning("MEMO EMAIL: no memo content found — skipping")
        _write_artifacts(status, write_files, base)
        return status

    # ── Build message ──────────────────────────────────────────────────────
    try:
        msg = build_memo_email_message(cfg, memo_txt, memo_md, _run_id, memo_date)
    except Exception as exc:
        status["error_class"] = type(exc).__name__
        status["error_message_sanitized"] = _sanitize_error(exc)
        status["reason"] = "message_build_error"
        logger.warning("MEMO EMAIL: message build failed — %s", exc)
        _write_artifacts(status, write_files, base)
        return status

    # ── Send (or dry-run pass-through) ─────────────────────────────────────
    try:
        send_result = send_daily_memo_email(cfg, msg)
    except Exception as exc:
        # strict_failure raised above; landing here means we caught it
        status["error_class"] = type(exc).__name__
        status["error_message_sanitized"] = _sanitize_error(exc)
        status["reason"] = "smtp_error"
        status["attempted"] = True
        _write_artifacts(status, write_files, base)
        return status

    status["attempted"] = send_result.get("attempted", False)
    status["sent"]      = send_result.get("sent", False)
    status["error_class"] = send_result.get("error_class")
    status["error_message_sanitized"] = send_result.get("error_message_sanitized")

    if cfg.dry_run:
        status["reason"] = "dry_run"
    elif status["sent"]:
        status["reason"] = "sent"
    else:
        status["reason"] = send_result.get("reason") or "send_failed"

    _write_artifacts(status, write_files, base)
    return status


# ---------------------------------------------------------------------------
# Public: GUI / dashboard loaders
# ---------------------------------------------------------------------------

def load_memo_delivery_status(
    base_dir: str | Path = "outputs",
) -> dict[str, Any]:
    """Load the latest memo_delivery_status.json for dashboard consumption."""
    path = get_output_path(OutputNamespace.LATEST, _STATUS_FILENAME, base_dir=base_dir)
    if not path.exists():
        return {"available": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return payload if isinstance(payload, dict) else {"available": False}
    except Exception:
        return {"available": False}


def load_recent_delivery_log(
    base_dir: str | Path = "outputs",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return the most recent delivery log entries (no secrets included)."""
    return _load_delivery_log(Path(base_dir))[-limit:]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m portfolio_automation.memo_email_sender",
        description="Deliver daily portfolio memo by email.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Build message but do not send")
    group.add_argument("--send", action="store_true", help="Send memo (requires SMTP env vars)")
    group.add_argument("--force-resend", action="store_true", help="Send even if already sent today")
    args = parser.parse_args(argv)

    # CLI always enables delivery; --dry-run overrides dry_run flag
    env_override: dict[str, str] = {"MEMO_EMAIL_ENABLED": "1"}
    if args.dry_run:
        env_override["MEMO_EMAIL_DRY_RUN"] = "1"
    elif args.force_resend:
        env_override["MEMO_EMAIL_DRY_RUN"] = "0"
        env_override["MEMO_EMAIL_FORCE_RESEND"] = "1"
    else:  # --send
        env_override["MEMO_EMAIL_DRY_RUN"] = "0"

    # Temporarily merge overrides into os.environ for this invocation
    original: dict[str, str | None] = {}
    for k, v in env_override.items():
        original[k] = os.environ.get(k)
        os.environ[k] = v

    try:
        result = run_memo_email_delivery(write_files=True)
    finally:
        for k, orig_v in original.items():
            if orig_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig_v

    print(f"enabled:   {result.get('enabled')}")
    print(f"dry_run:   {result.get('dry_run')}")
    print(f"attempted: {result.get('attempted')}")
    print(f"sent:      {result.get('sent')}")
    print(f"skipped:   {result.get('skipped')}")
    print(f"reason:    {result.get('reason')}")
    if result.get("error_class"):
        print(f"error:     {result['error_class']}: {result.get('error_message_sanitized')}")
    return 0 if (result.get("sent") or result.get("dry_run") or result.get("skipped")) else 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli_main())
