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

from portfolio_automation.sim_governance import approval_packet
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


def _age_days(created: Any, now: str) -> int | None:
    """Whole days between ``created`` and ``now``, or None when undeterminable.

    Returns None — never 0 — for a missing/malformed timestamp. A missing
    created_at reading as "brand new" would hide the oldest item in the operator
    decision queue, which is the one the reader most needs to see.
    """
    if not created:
        return None
    try:
        c = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        n = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return (n - c).days


def _assess_status(payload: dict, packet_health: dict | None) -> tuple[str, list[str]]:
    """GREEN / AMBER / RED over BOTH authority tiers.

    Mapping follows CLAUDE.md's sim-governance oversight contract: RED on a failed
    application or an authority-gate breach (the two things that must never read
    GREEN), AMBER on an active awaiting-veto item, a successful rollback, a
    rollback conflict, or a production review still waiting on a human.

    ``packet_health`` is folded in rather than recomputed. Note its
    ``packet_missing_or_unreadable`` AMBER is preserved deliberately: if the
    production queue cannot be read, we must not assert it is empty — that would
    be a verdict derived from absent data.
    """
    red: list[str] = []
    amber: list[str] = []

    if (payload.get("circuit_breaker") or {}).get("engaged"):
        red.append("circuit_breaker_engaged")
    if payload.get("failed_applications"):
        red.append(f"failed_applications:{len(payload['failed_applications'])}")
    if payload.get("authority_rejections"):
        red.append(f"authority_rejections:{len(payload['authority_rejections'])}")

    if payload.get("within_veto_window"):
        amber.append(f"awaiting_veto:{len(payload['within_veto_window'])}")
    if payload.get("rollbacks"):
        amber.append(f"rollbacks:{len(payload['rollbacks'])}")
    if payload.get("rollback_conflicts"):
        amber.append(f"rollback_conflicts:{len(payload['rollback_conflicts'])}")
    if payload.get("pending_human_proposals"):
        amber.append(f"production_pending:{len(payload['pending_human_proposals'])}")

    if isinstance(packet_health, dict):
        ph_status = str(packet_health.get("status") or "").upper()
        ph_reasons = [str(r) for r in (packet_health.get("reasons") or [])]
        if ph_status == "RED":
            red.extend(ph_reasons or ["packet_health_red"])
        elif ph_status == "AMBER":
            amber.extend(ph_reasons or ["packet_health_amber"])

    if red:
        return "RED", red + amber
    if amber:
        return "AMBER", amber
    return "GREEN", []


def _headline(payload: dict) -> str:
    """One-line rollup. Always states both tiers so neither can be invisible."""
    counts = payload["counts_two_tier"]
    prod = counts["production_pending"]
    sim = counts["sim_changes"]
    exceptions = (payload.get("authority_rejections") or []) + \
                 (payload.get("failed_applications") or [])
    exc = f"{len(exceptions)} authority exception{'s' if len(exceptions) != 1 else ''}" \
        if exceptions else "no authority exceptions"
    return (f"GOVERNANCE — {payload['governance_status']} · "
            f"{prod} production approval{'s' if prod != 1 else ''} pending · "
            f"{sim} simulation change{'s' if sim != 1 else ''} · {exc}")


def build_governance_digest(*, summary: dict, events: list[dict], now: str,
                            veto_window_hours: int = 48,
                            pending_proposals: list[dict] | None = None,
                            packet_health: dict | None = None,
                            gui_base_url: str = "",
                            approval_page_url: str | None = None) -> dict:
    """Build the evening digest from the ledger + summary. Pure — no I/O.

    Summarizes BOTH authority tiers. ``pending_proposals`` carries tier-b
    production promotions still awaiting a human (source:
    ``approval_packet.build_operator_packet``); ``packet_health`` carries
    ``approval_packet.assess_packet_health``. Reporting only — this builder can
    neither approve nor mutate governance state.
    """
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
        "approval_page_url": approval_page_url or "",
        "circuit_breaker": summary.get("circuit_breaker") or {"engaged": False, "reason": None},
        "counters": summary.get("counters", {}),
    }

    pending = payload["pending_human_proposals"]
    ages = [a for a in (_age_days(p.get("created_at"), now) for p in pending
                        if isinstance(p, dict)) if a is not None]
    stale = len([r for r in ((packet_health or {}).get("reasons") or [])
                 if str(r).startswith("stale_pending")])
    payload["counts_two_tier"] = {
        "production_pending": len(pending),
        "sim_within_veto": len(within_window),
        # "changes" the operator could still act on or needs to know happened.
        "sim_changes": len(auto_applied) + len(payload["human_vetoes"]) +
                       len(payload["rollbacks"]) + len(payload["rollback_conflicts"]),
        "oldest_pending_age_days": max(ages) if ages else None,
        "stale_pending": stale,
    }
    status, reasons = _assess_status(payload, packet_health)
    payload["governance_status"] = status
    payload["status_reasons"] = reasons
    payload["packet_health"] = packet_health or {}
    payload["headline"] = _headline(payload)

    return {"json": payload, "html": _render_html(payload), "text": _render_text(payload),
            "subject_date": (now or "")[:10], "approval_page_url": payload["approval_page_url"],
            "governance_status": status}


def _production_lines(p: dict) -> list[str]:
    """Tier-b block. Rendered identically (in substance) by text and HTML.

    Always emitted when something is pending, and never omitted just because the
    simulation lane was quiet — that omission was the audit's defect 3.
    """
    counts = p["counts_two_tier"]
    n = counts["production_pending"]
    if not n:
        return []
    out = [f"Pending human approvals (production): {n}"]
    age = counts["oldest_pending_age_days"]
    out.append(f"  Oldest pending: {age} day{'s' if age != 1 else ''}"
               if age is not None else "  Oldest pending: unknown (no created_at)")
    if counts["stale_pending"]:
        out.append(f"  Stale beyond threshold: {counts['stale_pending']}")
    for item in p["pending_human_proposals"][:10]:
        if not isinstance(item, dict):
            continue
        out.append(f"  • {item.get('proposal_id') or '?'} "
                   f"{item.get('proposal_type') or item.get('workflow') or ''} "
                   f"{item.get('symbol') or ''}".rstrip())
    return out


def _render_text(p: dict) -> str:
    lines = [p["headline"],
             f"Governance Digest — {p.get('generated_at', '')[:10]}",
             "(Simulation-lane auto-approval. Production remains human-gated: this "
             "digest reports state and cannot approve.)", ""]
    if p["status_reasons"]:
        lines.append(f"Why: {', '.join(p['status_reasons'])}")
        lines.append("")
    prod = _production_lines(p)
    if prod:
        lines.extend(["PRODUCTION", *prod, ""])
    if p.get("approval_page_url"):
        lines.append(f"Review & approve today's packet → {p['approval_page_url']}")
        lines.append("")
    aa = p["auto_applied"]
    if (not aa and not p["human_vetoes"] and not p["rollbacks"]
            and not p["rollback_conflicts"] and not prod):
        lines.append("No auto-approval activity in this period.")
    elif aa:
        # Guarded on `aa` specifically: a production-only digest must not emit an
        # empty "Auto-applied in simulation (0):" header.
        lines.append(f"Auto-applied in simulation ({len(aa)}):")
        for i in aa:
            lines.append(f"  • {i['target_id']} [{i['event_id']}] conf={i['confidence']} "
                         f"— {i['status_label']} — {i.get('gpt_reasoning') or ''}")
    for label, key in (("Human vetoes", "human_vetoes"), ("Rolled back", "rollbacks"),
                       ("Rollback conflicts", "rollback_conflicts"),
                       ("GPT vetoed", "gpt_vetoed"),
                       ("Failed applications", "failed_applications"),
                       ("Rejected by authority gate", "authority_rejections")):
        if p[key]:
            lines.append(f"{label}: {len(p[key])}")
    cb = p["circuit_breaker"]
    if cb.get("engaged"):
        lines.append(f"Circuit breaker ENGAGED: {cb.get('reason')}")
    return "\n".join(lines)


def _esc(v: Any) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _render_html(p: dict) -> str:
    # Status is text, never colour alone — the rollup must survive a plain-text
    # client, a screen reader, and monochrome printing.
    parts = [f"<p style=\"font-weight:600;\">{_esc(p['headline'])}</p>",
             f"<h2>Governance Digest — {_esc(p.get('generated_at', '')[:10])}</h2>",
             "<p><em>Simulation-lane auto-approval. Production remains human-gated: "
             "this digest reports state and cannot approve.</em></p>"]
    if p["status_reasons"]:
        parts.append(f"<p>Why: {_esc(', '.join(p['status_reasons']))}</p>")
    prod = _production_lines(p)
    if prod:
        parts.append("<h3>Production</h3><ul>")
        parts.extend(f"<li>{_esc(line.strip().lstrip('• '))}</li>" for line in prod)
        parts.append("</ul>")
    if p.get("approval_page_url"):
        url = _esc(p["approval_page_url"])
        parts.append(f'<p><a href="{url}">Review &amp; approve today\'s packet →</a></p>')
    aa = p["auto_applied"]
    if (not aa and not p["human_vetoes"] and not p["rollbacks"]
            and not p["rollback_conflicts"] and not prod):
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
    # NB: pending_human_proposals is deliberately NOT in this loop — it now has a
    # full Production block above, rendered in BOTH formats. It previously
    # appeared here only, which made HTML and text disagree on a critical fact.
    for label, key in (("Human vetoes", "human_vetoes"), ("Rolled back", "rollbacks"),
                       ("GPT vetoed", "gpt_vetoed"),
                       ("Failed applications", "failed_applications")):
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


def build_subject(digest: dict, *, prefix: str = "") -> str:
    """Subject conveys the human action state, not just a date.

    ``Governance — 6 Production Reviews · Health AMBER · as of 2026-08-03``.
    The previous form (``Governance Digest — <date>``) gave the reader no way to
    tell an empty steady state from ten waiting approvals without opening it.
    """
    payload = digest.get("json") or {}
    counts = payload.get("counts_two_tier") or {}
    n = counts.get("production_pending", 0)
    status = digest.get("governance_status") or payload.get("governance_status") or "UNKNOWN"
    bits = [f"{n} Production Review{'s' if n != 1 else ''}", f"Health {status}"]
    date_str = digest.get("subject_date") or ""
    if date_str:
        bits.append(f"as of {date_str}")
    subject = f"Governance — {' · '.join(bits)}"
    return f"{prefix.strip()} {subject}".strip() if prefix.strip() else subject


def _build_message(digest: dict, config):
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = build_subject(digest)
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


def _load_sim_governance_config(root: str) -> dict:
    try:
        cfg = json.loads((Path(root) / "config.json").read_text(encoding="utf-8"))
        return (cfg.get("sim_governance") or {})
    except Exception:
        return {}


def _load_auto_approval_config(root: str) -> dict:
    return (_load_sim_governance_config(root) or {}).get("auto_approval") or {}


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
        ap_cfg = _load_sim_governance_config(root).get("approval_packet") or {}
        approval_url = ""
        base = ap_cfg.get("deep_link_base", "")
        if base:
            approval_url = f"{base.rstrip('/')}/dashboard/governance"
        # Tier-b: production promotions still awaiting a human. The operator
        # approval packet already consolidates both tiers and assesses its own
        # health; until 2026-08-03 this digest never consulted it, so
        # pending_human_proposals was ALWAYS empty in production and the email
        # could report "no auto-approval activity" while N approvals waited.
        # Read-only, and degraded rather than fatal — a packet we cannot read
        # surfaces as AMBER, never as an implied empty queue.
        pending_proposals: list[dict] = []
        packet_health: dict | None = None
        try:
            packet = approval_packet.build_operator_packet(
                base_dir, now, deep_link_base=base,
                veto_window_hours=int(aa_cfg.get("veto_window_hours", 48)))
            pending_proposals = list(packet.get("tier_production") or [])
            packet_health = approval_packet.assess_packet_health(base_dir, now)
        except Exception as exc:
            logger.warning("governance_digest: approval packet unavailable: %s", exc)
            packet_health = {"status": "AMBER",
                             "reasons": [f"approval_packet_unavailable:{exc}"], "counts": {}}
        digest = build_governance_digest(
            summary=summary, events=events, now=now,
            veto_window_hours=int(aa_cfg.get("veto_window_hours", 48)),
            pending_proposals=pending_proposals,
            packet_health=packet_health,
            gui_base_url=env.get("GOVERNANCE_GUI_BASE_URL", ""),
            approval_page_url=approval_url)
        return send_governance_digest(digest, now=now, base_dir=base_dir, env=env,
                                      transport=transport, write_files=write_files)
    except Exception as exc:
        logger.warning("governance_digest: evening run failed: %s", exc)
        return {"status": "error", "error": str(exc)}
