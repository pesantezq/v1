"""
Evening governance digest — builder + email sender for the bounded auto-approval channel.

The builder is pure: it turns the append-only ledger + current-state summary into a
``{json, html, text}`` digest. Every item is labelled with an explicit, simulation-qualified
status — never a bare "approved" — and links use the event_id (never a symbol-only action).

The sender reuses ``memo_email_sender``'s config loader + credential handling. It is a
DISTINCT opt-in (``GOVERNANCE_DIGEST_ENABLED``); disabled → skip cleanly; enabled without
credentials or on send failure → a recorded delivery failure surfaced AMBER (never a silent
degradation, and never blocking/undoing a valid auto-approval). Local-time scheduling
(default 18:00 America/New_York) is DST-safe via zoneinfo.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from portfolio_automation.sim_governance import auto_approval as AA

logger = logging.getLogger("stockbot.sim_governance.governance_digest")

_DELIVERY_LOG = "governance_digest_log.jsonl"

_APPLIED_LABEL = "Auto-applied in simulation · veto available"


# ---------------------------------------------------------------------------
# Builder (pure)
# ---------------------------------------------------------------------------


def _within_window(applied_at: str, now: str, hours: int) -> bool:
    try:
        a = datetime.fromisoformat(str(applied_at).replace("Z", "+00:00"))
        n = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        return (n - a).total_seconds() <= hours * 3600
    except (ValueError, TypeError):
        return True


def build_governance_digest(*, summary: dict, events: list[dict], now: str,
                            veto_window_hours: int = 48,
                            pending_proposals: list[dict] | None = None,
                            gui_base_url: str = "") -> dict:
    """Build the evening digest from the ledger + summary. Pure — no I/O."""
    events = events or []
    summary = summary or {}
    veto_base = (gui_base_url.rstrip("/") + "/dashboard/governance/veto?event_id=") if gui_base_url \
        else "/dashboard/governance/veto?event_id="

    def _of(kind):
        return [e for e in events if e.get("kind") == kind]

    auto_applied = []
    for e in _of(AA.EVENT_APPLIED):
        eid = e.get("event_id")
        auto_applied.append({
            "event_id": eid,
            "target_id": e.get("target_id"),
            "candidate_type": e.get("candidate_type"),
            "confidence": e.get("confidence"),
            "gpt_reasoning": e.get("gpt_reasoning"),
            "gate_summary": [g.get("gate_name") for g in (e.get("gate_trace") or [])
                             if isinstance(g, dict)],
            "applied_at": e.get("application_timestamp") or e.get("ts"),
            "status_label": _APPLIED_LABEL,
            "target_lane": "simulation",
            "feeds_decision_engine": False,
            "veto_link": veto_base + str(eid),
            "within_veto_window": _within_window(
                e.get("application_timestamp") or e.get("ts"), now, veto_window_hours),
        })

    within_window = [i for i in auto_applied if i["within_veto_window"]]
    authority_rejections = [e for e in _of(AA.EVENT_DETERMINISTIC_REJECT)
                            if e.get("reason") == "authority_gate_failed"]
    deterministic_rejections = [e for e in _of(AA.EVENT_DETERMINISTIC_REJECT)
                                if e.get("reason") != "authority_gate_failed"]

    payload = {
        "generated_at": now,
        "schema": "governance_digest.v1",
        "auto_applied": auto_applied,
        "within_veto_window": within_window,
        "gpt_vetoed": _of(AA.EVENT_GPT_VETO),
        "human_vetoes": _of(AA.EVENT_HUMAN_VETO),
        "rollbacks": _of(AA.EVENT_ROLLBACK),
        "rollback_conflicts": _of(AA.EVENT_ROLLBACK_CONFLICT),
        "failed_applications": _of(AA.EVENT_FAILURE),
        "authority_rejections": authority_rejections,
        "deterministic_rejections": deterministic_rejections,
        "pending_human_proposals": pending_proposals or [],
        "circuit_breaker": summary.get("circuit_breaker") or {"engaged": False, "reason": None},
        "counters": summary.get("counters", {}),
    }
    return {"json": payload, "html": _render_html(payload), "text": _render_text(payload),
            "subject_date": (now or "")[:10]}


def _render_text(p: dict) -> str:
    lines = [f"Governance Digest — {p.get('generated_at', '')[:10]}",
             "(Simulation-lane auto-approval. Production remains human-gated.)", ""]
    aa = p["auto_applied"]
    if not aa and not p["human_vetoes"] and not p["rollbacks"] and not p["rollback_conflicts"]:
        lines.append("No auto-approval activity in this period.")
    else:
        lines.append(f"Auto-applied in simulation ({len(aa)}):")
        for i in aa:
            lines.append(f"  • {i['target_id']} [{i['event_id']}] conf={i['confidence']} "
                         f"— {i['status_label']} — {i.get('gpt_reasoning') or ''}")
    for label, key in (("Human vetoes", "human_vetoes"), ("Rolled back", "rollbacks"),
                       ("Rollback conflicts", "rollback_conflicts"),
                       ("Failed applications", "failed_applications"),
                       ("Rejected by authority gate", "authority_rejections")):
        if p[key]:
            lines.append(f"{label}: {len(p[key])}")
    cb = p["circuit_breaker"]
    if cb.get("engaged"):
        lines.append(f"CIRCUIT BREAKER ENGAGED: {cb.get('reason')}")
    return "\n".join(lines)


def _esc(v: Any) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _render_html(p: dict) -> str:
    parts = [f"<h2>Governance Digest — {_esc(p.get('generated_at', '')[:10])}</h2>",
             "<p><em>Simulation-lane auto-approval. Production remains human-gated.</em></p>"]
    aa = p["auto_applied"]
    if not aa and not p["human_vetoes"] and not p["rollbacks"] and not p["rollback_conflicts"]:
        parts.append("<p>No auto-approval activity in this period.</p>")
    if aa:
        parts.append("<h3>Auto-applied in simulation</h3><ul>")
        for i in aa:
            parts.append(
                f"<li><strong>{_esc(i['target_id'])}</strong> "
                f"(<code>{_esc(i['event_id'])}</code>) — {_esc(i['status_label'])}. "
                f"confidence {_esc(i['confidence'])}. GPT: {_esc(i.get('gpt_reasoning') or '')}. "
                f"<a href=\"{_esc(i['veto_link'])}\">Veto</a></li>")
        parts.append("</ul>")
    if p["rollback_conflicts"]:
        parts.append(f"<h3>Rollback conflicts ({len(p['rollback_conflicts'])})</h3>"
                     "<p>Operator resolution needed — current state preserved, not overwritten.</p>")
    if p["authority_rejections"]:
        parts.append(f"<h3>Rejected by authority gate ({len(p['authority_rejections'])})</h3>"
                     "<p>Routed to pending human review; not auto-applied.</p>")
    for label, key in (("Human vetoes", "human_vetoes"), ("Rolled back", "rollbacks"),
                       ("GPT vetoed", "gpt_vetoed"), ("Failed applications", "failed_applications"),
                       ("Pending human proposals", "pending_human_proposals")):
        if p[key]:
            parts.append(f"<p>{_esc(label)}: {len(p[key])}</p>")
    cb = p["circuit_breaker"]
    if cb.get("engaged"):
        parts.append(f"<p style='color:#b00'><strong>Circuit breaker engaged:</strong> "
                     f"{_esc(cb.get('reason'))}</p>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Local-time scheduling (DST-safe)
# ---------------------------------------------------------------------------


def should_send_now(now_utc_iso: str, *, send_hour_local: int,
                    timezone: str = "America/New_York") -> bool:
    """True when the LOCAL hour in *timezone* equals ``send_hour_local``. DST-safe."""
    try:
        from zoneinfo import ZoneInfo
        n = datetime.fromisoformat(str(now_utc_iso).replace("Z", "+00:00"))
        if n.tzinfo is None:
            n = n.replace(tzinfo=_tz_utc())
        local = n.astimezone(ZoneInfo(timezone))
        return local.hour == int(send_hour_local)
    except Exception:
        return False


def _tz_utc():
    return timezone.utc


# ---------------------------------------------------------------------------
# Email sender (opt-in; degrades safely; never leaks credentials)
# ---------------------------------------------------------------------------


def _default_transport(config, message) -> None:
    """Minimal TLS send mirroring memo_email_sender's SMTP core."""
    import smtplib
    import ssl
    rcpt = config.to_addrs + config.cc_addrs + config.bcc_addrs
    if config.use_tls:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(config.smtp_host, config.smtp_port) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ctx)
            smtp.login(config.username, config.password)
            smtp.send_message(message, to_addrs=rcpt)
    else:
        with smtplib.SMTP(config.smtp_host, config.smtp_port) as smtp:
            smtp.login(config.username, config.password)
            smtp.send_message(message, to_addrs=rcpt)


def _build_message(digest: dict, config):
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = f"Governance Digest — {digest.get('subject_date', '')}"
    msg["From"] = config.from_addr
    msg["To"] = ", ".join(config.to_addrs)
    msg.set_content(digest.get("text") or "(no digest content)")
    html = digest.get("html")
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def _record(base_dir: str, entry: dict, write_files: bool) -> dict:
    if write_files:
        try:
            from portfolio_automation.data_governance import OutputNamespace, ensure_output_dir, get_output_path
            ensure_output_dir(OutputNamespace.POLICY, _DELIVERY_LOG, base_dir=base_dir)
            path = get_output_path(OutputNamespace.POLICY, _DELIVERY_LOG, base_dir=base_dir)
            with Path(path).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except Exception as exc:
            logger.debug("governance_digest: delivery-log write failed: %s", exc)
    return entry


def send_governance_digest(digest: dict, *, now: str, base_dir: str = "outputs",
                           env: dict | None = None,
                           transport: Callable[[Any, Any], None] | None = None,
                           write_files: bool = True) -> dict:
    """Send the evening digest. Gated on ``GOVERNANCE_DIGEST_ENABLED``. Never raises;
    records the delivery attempt/result/timestamp and returns a status dict."""
    env = env or {}
    attempt = {"attempted": False, "ts": now, "digest_date": digest.get("subject_date")}

    if not AA._env_truthy(env.get("GOVERNANCE_DIGEST_ENABLED")):
        return _record(base_dir, {**attempt, "status": "skipped", "reason": "disabled"},
                       write_files)

    from portfolio_automation.memo_email_sender import load_memo_email_config
    try:
        from portfolio_automation.memo_email_sender import _sanitize_error
    except Exception:  # pragma: no cover
        def _sanitize_error(exc):  # type: ignore
            return "delivery error"

    config = load_memo_email_config(env=env)
    if not config.has_valid_recipients():
        return _record(base_dir, {**attempt, "status": "delivery_failed",
                                   "reason": "invalid_or_missing_recipients", "health": "AMBER"},
                       write_files)
    if not config.has_smtp_config():
        return _record(base_dir, {**attempt, "status": "delivery_failed",
                                   "reason": "missing_smtp_config", "health": "AMBER"},
                       write_files)

    message = _build_message(digest, config)
    attempt["attempted"] = True
    try:
        (transport or _default_transport)(config, message)
    except Exception as exc:
        return _record(base_dir, {**attempt, "status": "delivery_failed",
                                   "reason": "send_error", "health": "AMBER",
                                   "error": _sanitize_error(exc)}, write_files)
    return _record(base_dir, {**attempt, "status": "sent"}, write_files)


def _load_auto_approval_config(root: str) -> dict:
    try:
        cfg = json.loads((Path(root) / "config.json").read_text(encoding="utf-8"))
        return ((cfg.get("sim_governance") or {}).get("auto_approval") or {})
    except Exception:
        return {}


def run_evening_digest(root: str = ".", now: str | None = None, *, env: dict | None = None,
                       transport: Callable[[Any, Any], None] | None = None,
                       write_files: bool = True) -> dict:
    """Evening-cron entry point: build the digest from the ledger + summary and send it.

    Gated twice (both must hold): config ``auto_approval.evening_digest.enabled`` AND the
    ``GOVERNANCE_DIGEST_ENABLED`` env opt-in (checked inside send). Never raises."""
    import os
    try:
        base_dir = str(Path(root) / "outputs")
        now = now or datetime.now(timezone.utc).isoformat()
        env = env if env is not None else dict(os.environ)
        aa_cfg = _load_auto_approval_config(root)
        dcfg = aa_cfg.get("evening_digest") or {}
        if not dcfg.get("enabled"):
            return {"status": "skipped", "reason": "digest_disabled_in_config", "ts": now}
        events = AA.load_events(base_dir=base_dir)
        summary = AA.build_summary(base_dir=base_dir, now=now)
        digest = build_governance_digest(
            summary=summary, events=events, now=now,
            veto_window_hours=int(aa_cfg.get("veto_window_hours", 48)),
            gui_base_url=env.get("GOVERNANCE_GUI_BASE_URL", ""))
        return send_governance_digest(digest, now=now, base_dir=base_dir, env=env,
                                      transport=transport, write_files=write_files)
    except Exception as exc:
        logger.warning("governance_digest: evening run failed: %s", exc)
        return {"status": "error", "error": str(exc)}
