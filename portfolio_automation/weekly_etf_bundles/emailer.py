"""
Standalone weekly ETF bundle email.

Reuses the memo transport (memo_email_sender.send_daily_memo_email) — a generic,
dry-run-aware SMTP sender — WITHOUT changing the daily email's public API. Gates
are independent and default INERT:

  WEEKLY_ETF_BUNDLES_EMAIL_ENABLED   (default false)
  WEEKLY_ETF_BUNDLES_EMAIL_DRY_RUN   (default true)
  WEEKLY_ETF_BUNDLES_EMAIL_TO        (override recipients; else memo/generic)
  WEEKLY_ETF_BUNDLES_EMAIL_FORCE     (bypass duplicate-send suppression)

Sending requires BOTH enabled=true (env) AND dry_run=false (via --send-email).
Duplicate sends are suppressed by a deterministic key
(message_type + market_data_date + recipient_set + content_hash); a rerun
regenerates artifacts but does not resend identical content unless forced.
Fail-closed: config invalid / no recipients / missing SMTP / hash failure → no send.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

from portfolio_automation.data_governance import OutputNamespace, get_output_path, safe_write_json
from portfolio_automation.memo_email_sender import (
    MemoEmailConfig,
    load_memo_email_config,
    send_daily_memo_email,
)
from portfolio_automation.weekly_etf_bundles import renderer

logger = logging.getLogger("stockbot.weekly_etf_bundles.emailer")

_MESSAGE_TYPE = "weekly_etf_bundle_watchlist"
_LOG_REL = ("policy", "weekly_etf_email_log.jsonl")
_RECEIPT_REL = "email_receipt.json"


def _env_bool(name: str, default: bool, env: dict[str, str] | None = None) -> bool:
    raw = (env or os.environ).get(name, "")
    return default if not raw else raw.strip().lower() in {"1", "true", "yes", "y"}


def load_weekly_email_config(env: dict[str, str] | None = None) -> MemoEmailConfig:
    """Reuse the memo transport (host/port/creds/from) but gate + target
    independently. The daily memo's own enabled flag is never consulted."""
    cfg = load_memo_email_config(env)   # transport + generic fallbacks
    cfg.enabled = _env_bool("WEEKLY_ETF_BUNDLES_EMAIL_ENABLED", False, env)
    cfg.dry_run = _env_bool("WEEKLY_ETF_BUNDLES_EMAIL_DRY_RUN", True, env)
    cfg.force_resend = _env_bool("WEEKLY_ETF_BUNDLES_EMAIL_FORCE", False, env)
    override_to = (env or os.environ).get("WEEKLY_ETF_BUNDLES_EMAIL_TO", "").strip()
    if override_to:
        from portfolio_automation.memo_email_sender import _parse_addrs
        cfg.to_addrs = _parse_addrs(override_to)
    return cfg


def _content_hash(subject: str, text_body: str) -> str:
    return hashlib.sha256((subject + "\n" + text_body).encode("utf-8")).hexdigest()[:16]


def _dedup_key(market_data_date: str, recipients: list[str], content_hash: str) -> str:
    rcpt = ",".join(sorted(recipients))
    raw = f"{_MESSAGE_TYPE}|{market_data_date}|{rcpt}|{content_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _log_path(root: Path) -> Path:
    return get_output_path(OutputNamespace.POLICY, "weekly_etf_email_log.jsonl",
                           base_dir=root / "outputs")


def _already_sent(root: Path, dedup_key: str) -> bool:
    path = _log_path(root)
    if not path.exists():
        return False
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("dedup_key") == dedup_key and rec.get("sent"):
                return True
    except Exception:
        return False
    return False


def _append_log(root: Path, entry: dict[str, Any]) -> None:
    path = _log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def build_message(subject: str, text_body: str, html_body: str,
                  config: MemoEmailConfig) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = (config.subject_prefix + subject) if config.subject_prefix else subject
    msg["From"] = config.from_addr
    msg["To"] = ", ".join(config.to_addrs)
    if config.cc_addrs:
        msg["Cc"] = ", ".join(config.cc_addrs)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


def send_weekly_etf_bundle_email(
    *,
    analysis_payload: dict[str, Any],
    scorecard: dict[str, Any] | None = None,
    root: str | Path = ".",
    env: dict[str, str] | None = None,
    force: bool = False,
    write_files: bool = True,
    sender: Callable[[MemoEmailConfig, EmailMessage], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Render + (conditionally) send the weekly ETF email. Never raises for a
    delivery problem — returns a result dict with a reason. Observe-only."""
    root_path = Path(root).resolve()
    send_fn = sender or send_daily_memo_email
    result: dict[str, Any] = {"message_type": _MESSAGE_TYPE, "sent": False,
                              "observe_only": True}

    # Fail-closed input checks.
    if not analysis_payload or analysis_payload.get("status") != "ok":
        result.update(reason="analysis_not_ok"); return result
    mdd = analysis_payload.get("market_data_date")
    if not mdd:
        result.update(reason="no_market_data_date"); return result

    try:
        subject = renderer.render_subject(analysis_payload)
        text_body = renderer.render_weekly_md(analysis_payload, scorecard)
        html_body = renderer.render_weekly_html(analysis_payload, scorecard)
        content_hash = _content_hash(subject, text_body)
    except Exception as exc:
        logger.error("weekly_etf email render failed: %s", exc, exc_info=True)
        result.update(reason="render_failed", error=str(exc)); return result

    cfg = load_weekly_email_config(env)
    force = force or cfg.force_resend
    dedup_key = _dedup_key(mdd, cfg.to_addrs, content_hash)
    result.update(market_data_date=mdd, content_hash=content_hash, dedup_key=dedup_key,
                  dry_run=cfg.dry_run, enabled=cfg.enabled, recipients=list(cfg.to_addrs))

    if not cfg.enabled:
        result.update(reason="disabled"); _receipt(root_path, result, write_files); return result
    if not cfg.has_valid_recipients():
        result.update(reason="no_valid_recipients"); _receipt(root_path, result, write_files); return result
    if not cfg.dry_run and not cfg.has_smtp_config():
        result.update(reason="missing_smtp_config"); _receipt(root_path, result, write_files); return result

    # Duplicate-send suppression (unless forced).
    if not force and _already_sent(root_path, dedup_key):
        result.update(reason="duplicate_suppressed", duplicate_suppressed=True)
        _receipt(root_path, result, write_files); return result

    msg = build_message(subject, text_body, html_body, cfg)
    send_result = send_fn(cfg, msg)
    result.update(send_result=send_result, sent=bool(send_result.get("sent")),
                  dry_run=bool(send_result.get("dry_run", cfg.dry_run)),
                  duplicate_suppressed=False)
    if not result.get("reason"):
        result["reason"] = "dry_run" if result["dry_run"] else ("sent" if result["sent"] else "send_failed")

    # Log only a real (non-dry) successful send for dedup purposes.
    if write_files:
        _append_log(root_path, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message_type": _MESSAGE_TYPE, "market_data_date": mdd,
            "dedup_key": dedup_key, "content_hash": content_hash,
            "recipients": list(cfg.to_addrs), "dry_run": result["dry_run"],
            "sent": bool(result["sent"] and not result["dry_run"]),
            "reason": result.get("reason"),
        })
        _receipt(root_path, result, write_files)
    return result


def _receipt(root: Path, result: dict[str, Any], write_files: bool) -> None:
    if not write_files:
        return
    try:
        safe_write_json(OutputNamespace.WEEKLY_ETF_BUNDLES, _RECEIPT_REL,
                        {**result, "generated_at": datetime.now(timezone.utc).isoformat()},
                        base_dir=root / "outputs")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("weekly_etf email receipt write failed: %s", exc)
