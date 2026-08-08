"""
Watchlist Email Sender
======================
Delivers the daily watchlist as its own email, separate from the daily memo.

Why a separate module rather than a flag on an existing one
-----------------------------------------------------------
Operator decision (2026-07-29): "make a new one so it's isolated and can be
handled independently to not cause conflicts with the memo, but use existing
infrastructure."

So this module is deliberately **isolated** from
``portfolio_automation.memo_email_sender``: it imports nothing from it and shares
no state, config key, artifact, or dedup ledger. A failure, a config change, or a
future refactor on either side cannot affect the other — the memo email is the one
the operator depends on daily, and it is not put at risk by this.

It **reuses** the surrounding infrastructure rather than reinventing it:

* ``utils.get_env_first`` for the dedicated-name-wins env fallback chain,
* ``portfolio_automation.data_governance`` (``OutputNamespace`` /
  ``safe_write_json`` / ``get_output_path``) for all writes,
* the established delivery-status + append-only-log + date-based-dedup shape,
* ``run_aux_stage`` in ``scripts/run_daily_safe.sh`` for non-blocking wiring.

Note the existing ``email_digest.FinanceEmailDigest`` was NOT reused: its sections
(Top 3 Actions, Portfolio Summary, What Changed, Theme Highlights) largely repeat
the memo, and it is gated to send only when an item is ACTION_REQUIRED. This
module carries watchlist content the memo does not: the ranked universe, the
source breakdown, new candidates, and the operator alert queue.

CLI::

    python -m portfolio_automation.watchlist_email_sender --dry-run
    python -m portfolio_automation.watchlist_email_sender --send
    python -m portfolio_automation.watchlist_email_sender --force-resend

Environment variables:

  WATCHLIST_EMAIL_ENABLED        0|1  (default 0 — disabled)
  WATCHLIST_EMAIL_DRY_RUN        0|1  (default 1 — build but do not send)
  WATCHLIST_EMAIL_TO             recipients; falls back to EMAIL_TO
  WATCHLIST_EMAIL_SUBJECT_PREFIX optional subject prefix
  WATCHLIST_EMAIL_FORCE_RESEND   0|1  (default 0 — skip already-sent dates)
  WATCHLIST_EMAIL_MAX_ROWS       int  (default 15 — ranked rows in the body)

Transport falls back to the generic mail config already on the box:
  SMTP_SERVER / SMTP_PORT / EMAIL_USER / EMAIL_PASS

``WATCHLIST_EMAIL_ENABLED`` has NO fallback, by the same reasoning as
``MEMO_EMAIL_ENABLED``: the presence of a working SMTP config must never be enough
to start emailing on its own. Turning this on stays a deliberate opt-in.

Artifacts:

  outputs/latest/watchlist_email_status.json   — per-run delivery status
  outputs/policy/watchlist_email_log.jsonl     — append-only audit log

Governance:

  observe_only: true, no_trade: true hard-coded in every artifact.
  Read-only: no market-data APIs, no AI/LLM calls, no portfolio-state mutation,
  never writes or reads decision_plan.json.
"""
from __future__ import annotations

import csv
import html as _html
import io
import json
import logging
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    get_output_path,
    safe_write_json,
)
from utils import get_env_first

logger = logging.getLogger("stockbot.portfolio_automation.watchlist_email_sender")

_SCHEMA_VERSION = "1"
_SOURCE_LABEL = "watchlist_email_sender"
_OBSERVE_ONLY = True

_STATUS_FILENAME = "watchlist_email_status.json"
_LOG_FILENAME = "watchlist_email_log.jsonl"

_UNIVERSE_REL = ("outputs", "latest", "top100_daily.json")
_CANDIDATES_REL = ("outputs", "latest", "watch_candidates.json")
_ALERTS_REL = ("outputs", "latest", "watchlist_alerts.csv")

_DEFAULT_MAX_ROWS = 15
_MAX_ALERT_ROWS = 10
_MAX_CANDIDATE_ROWS = 10

_DISCLAIMER = (
    "Observe-only watchlist digest. Ranked universe and alerts are advisory "
    "research context; nothing here is an instruction to trade and no order is "
    "ever placed by this system."
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool, env: dict[str, str] | None = None) -> bool:
    raw = (env or os.environ).get(name, "")
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


def _env_int(name: str, default: int, env: dict[str, str] | None = None) -> int:
    raw = (env or os.environ).get(name, "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _first(names: list[str], default: str = "",
           env: dict[str, str] | None = None) -> str:
    """Env-dict-aware wrapper over utils.get_env_first (tests inject a dict)."""
    if env is None:
        return get_env_first(names, default) or ""
    for name in names:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return default


def _parse_addrs(raw: str) -> list[str]:
    if not raw:
        return []
    parts = [a.strip() for a in raw.replace(";", ",").split(",")]
    return [p for p in parts if p]


def _valid_addr(addr: str) -> bool:
    parts = addr.split("@")
    return len(parts) == 2 and bool(parts[0]) and bool(parts[1])


@dataclass
class WatchlistEmailConfig:
    enabled: bool = False
    dry_run: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    username: str = ""
    password: str = field(default="", repr=False)  # never logged or persisted
    from_addr: str = ""
    to_addrs: list[str] = field(default_factory=list)
    use_tls: bool = True
    subject_prefix: str = ""
    force_resend: bool = False
    max_rows: int = _DEFAULT_MAX_ROWS

    def has_valid_recipients(self) -> bool:
        return bool(self.to_addrs) and all(_valid_addr(a) for a in self.to_addrs)

    def has_smtp_config(self) -> bool:
        return bool(self.smtp_host and self.username and self.password and self.from_addr)


def load_watchlist_email_config(
    env: dict[str, str] | None = None,
) -> WatchlistEmailConfig:
    """Build config from the environment (or an explicit dict for testing).

    ``enabled`` is gated on ``WATCHLIST_EMAIL_ENABLED`` ALONE. Everything else may
    fall back to the generic mail config, so an operator who already has SMTP set
    up only has to flip one flag — but never has it flipped for them.
    """
    cfg = WatchlistEmailConfig(
        enabled=_env_bool("WATCHLIST_EMAIL_ENABLED", False, env),
        dry_run=_env_bool("WATCHLIST_EMAIL_DRY_RUN", True, env),
        smtp_host=_first(["WATCHLIST_EMAIL_SMTP_HOST", "SMTP_SERVER"], "", env),
        username=_first(["WATCHLIST_EMAIL_USERNAME", "EMAIL_USER"], "", env),
        from_addr=_first(["WATCHLIST_EMAIL_FROM", "EMAIL_USER"], "", env),
        to_addrs=_parse_addrs(_first(["WATCHLIST_EMAIL_TO", "EMAIL_TO"], "", env)),
        use_tls=_env_bool("WATCHLIST_EMAIL_USE_TLS", True, env),
        subject_prefix=_first(["WATCHLIST_EMAIL_SUBJECT_PREFIX"], "", env),
        force_resend=_env_bool("WATCHLIST_EMAIL_FORCE_RESEND", False, env),
        max_rows=_env_int("WATCHLIST_EMAIL_MAX_ROWS", _DEFAULT_MAX_ROWS, env),
    )
    port_raw = _first(["WATCHLIST_EMAIL_SMTP_PORT", "SMTP_PORT"], "587", env)
    try:
        cfg.smtp_port = int(port_raw or "587")
    except (TypeError, ValueError):
        cfg.smtp_port = 587
    cfg.password = _first(["WATCHLIST_EMAIL_PASSWORD", "EMAIL_PASS"], "", env)
    return cfg


# ---------------------------------------------------------------------------
# Inputs (read-only, degrade individually)
# ---------------------------------------------------------------------------

def _load_json_safe(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_alerts(path: Path, limit: int = _MAX_ALERT_ROWS) -> list[dict[str, str]]:
    """Parse watchlist_alerts.csv. Tolerates the UTF-8 BOM the writer emits."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = [r for r in reader if (r.get("ticker") or "").strip()]
    except Exception as exc:
        logger.debug("watchlist_email: alerts parse failed — %s", exc)
        return []
    return rows[:limit]


def collect_watchlist_inputs(root: str | Path = ".") -> dict[str, Any]:
    """Gather everything the email renders. Never raises.

    Each source degrades on its own so a single missing artifact narrows the email
    rather than blocking it, and ``sources_present`` records exactly which ones
    were readable — an empty section must be distinguishable from a section whose
    producer did not run.
    """
    root_path = Path(root).resolve()
    universe = _load_json_safe(root_path.joinpath(*_UNIVERSE_REL))
    candidates_doc = _load_json_safe(root_path.joinpath(*_CANDIDATES_REL))
    alerts = _load_alerts(root_path.joinpath(*_ALERTS_REL))

    return {
        "universe": universe,
        "candidates": list(candidates_doc.get("watch_candidates") or []),
        "candidates_meta": {
            "run_date": candidates_doc.get("run_date"),
            "degraded_mode": candidates_doc.get("degraded_mode"),
            "degraded_reason": candidates_doc.get("degraded_reason"),
            "data_sources_used": candidates_doc.get("data_sources_used") or [],
        },
        "alerts": alerts,
        "sources_present": {
            "universe": bool(universe),
            "candidates": bool(candidates_doc),
            "alerts": bool(alerts),
        },
    }


def resolve_watchlist_date(inputs: dict[str, Any]) -> str:
    """The date this email is *about* — the dedup key.

    Prefers the universe artifact's generated_at, then watch_candidates.run_date,
    then today. Keyed on artifact content rather than wall-clock so a re-run after
    midnight UTC does not re-send the same watchlist under a new key.
    """
    gen = ((inputs.get("universe") or {}).get("generated_at") or "")
    if isinstance(gen, str) and len(gen) >= 10:
        return gen[:10]
    run_date = (inputs.get("candidates_meta") or {}).get("run_date")
    if isinstance(run_date, str) and len(run_date) >= 10:
        return run_date[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Body rendering
# ---------------------------------------------------------------------------

def _fmt_score(value: Any) -> str:
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_pct_or_dash(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


def build_watchlist_text(inputs: dict[str, Any], *, max_rows: int = _DEFAULT_MAX_ROWS,
                         watchlist_date: str | None = None) -> str:
    """Plain-text body. Deterministic; no I/O."""
    uni = inputs.get("universe") or {}
    diag = uni.get("ranking_diagnostics") or {}
    candidates = inputs.get("candidates") or []
    alerts = inputs.get("alerts") or []
    present = inputs.get("sources_present") or {}
    date_str = watchlist_date or resolve_watchlist_date(inputs)

    out: list[str] = []
    a = out.append
    a("=" * 60)
    a(f"  WATCHLIST — {date_str}")
    a("=" * 60)
    a("")

    if not present.get("universe"):
        a("RANKED UNIVERSE")
        a("-" * 40)
        a("  Unavailable — top100_daily.json was not readable this run.")
        a("")
    else:
        total = uni.get("total_distinct_tickers")
        a(f"RANKED UNIVERSE — {total if total is not None else '?'} distinct tickers "
          f"(lookback {uni.get('lookback_days', '?')}d)")
        a("-" * 40)
        # Ranking-quality caveat first: the rank order is the thing the reader is
        # about to trust, so a known degeneracy must precede the table, not follow it.
        if diag.get("degenerate_ranking"):
            a("  ! RANKING QUALITY WARNING")
            for chunk in _wrap(str(diag.get("warning") or ""), 66):
                a(f"    {chunk}")
            a("")
        rows = uni.get("candidates") or []
        if not rows:
            a("  No candidates ranked this run.")
        else:
            a(f"  {'#':>3} {'SYMBOL':<7} {'SCORE':>6} {'SECTOR':<22} {'THEME':>6} SOURCES")
            for row in rows[:max_rows]:
                srcs = ",".join(row.get("sources") or [])[:34]
                a(f"  {str(row.get('rank', '?')):>3} {str(row.get('symbol', '?')):<7} "
                  f"{_fmt_score(row.get('score')):>6} "
                  f"{str(row.get('sector') or '—')[:22]:<22} "
                  f"{_fmt_pct_or_dash(row.get('theme_confidence_max')):>6} {srcs}")
            if len(rows) > max_rows:
                a(f"  … {len(rows) - max_rows} more (full list in "
                  f"outputs/latest/top100_daily.md)")
        a("")
        breakdown = uni.get("source_breakdown") or {}
        if breakdown:
            a("SOURCE BREAKDOWN")
            a("-" * 40)
            for src, n in sorted(breakdown.items(), key=lambda kv: (-kv[1], kv[0])):
                a(f"  {src:<22} {n}")
            a("")

    a("NEW WATCH CANDIDATES")
    a("-" * 40)
    meta = inputs.get("candidates_meta") or {}
    if not present.get("candidates"):
        a("  Unavailable — watch_candidates.json was not readable this run.")
    elif not candidates:
        a("  None surfaced this run.")
    else:
        for c in candidates[:_MAX_CANDIDATE_ROWS]:
            themes = ", ".join(c.get("themes") or []) or "—"
            a(f"  {str(c.get('ticker', '?')):<7} conf {_fmt_pct_or_dash(c.get('confidence')):>5}  "
              f"{themes}")
        if len(candidates) > _MAX_CANDIDATE_ROWS:
            a(f"  … {len(candidates) - _MAX_CANDIDATE_ROWS} more")
    if meta.get("degraded_mode"):
        a(f"  ! Discovery ran degraded: {meta.get('degraded_reason') or 'reason unrecorded'}")
    if meta.get("data_sources_used"):
        a(f"  Sources used: {', '.join(str(s) for s in meta['data_sources_used'])}")
    a("")

    a("OPERATOR ALERT QUEUE")
    a("-" * 40)
    if not present.get("alerts"):
        a("  No alerts file this run.")
    elif not alerts:
        a("  No alerts raised.")
    else:
        a(f"  {'#':>3} {'SYMBOL':<7} {'EFF':>6} {'TIER':<8} {'CONVICTION':<12} SECTOR")
        for r in alerts:
            a(f"  {str(r.get('operator_rank') or '?'):>3} {str(r.get('ticker') or '?'):<7} "
              f"{_fmt_score(r.get('effective_score')):>6} "
              f"{str(r.get('alert_tier') or '—')[:8]:<8} "
              f"{str(r.get('conviction_band') or '—')[:12]:<12} "
              f"{str(r.get('sector') or '—')[:20]}")
    a("")
    a("-" * 60)
    for chunk in _wrap(_DISCLAIMER, 58):
        a(chunk)
    return "\n".join(out)


def _wrap(text: str, width: int) -> list[str]:
    """Minimal greedy wrap (no textwrap dependency on formatting nuances)."""
    words = str(text).split()
    if not words:
        return []
    lines: list[str] = []
    cur = words[0]
    for w in words[1:]:
        if len(cur) + 1 + len(w) <= width:
            cur = f"{cur} {w}"
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def build_watchlist_html(inputs: dict[str, Any], *, max_rows: int = _DEFAULT_MAX_ROWS,
                         watchlist_date: str | None = None) -> str:
    """HTML alternative. Every interpolated value is escaped."""
    uni = inputs.get("universe") or {}
    diag = uni.get("ranking_diagnostics") or {}
    present = inputs.get("sources_present") or {}
    date_str = watchlist_date or resolve_watchlist_date(inputs)
    e = _html.escape

    css_body = ("font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
                "Helvetica,Arial,sans-serif;font-size:14px;color:#18181b;")
    parts: list[str] = [
        f'<div style="{css_body}">',
        f'<h2 style="margin:0 0 4px 0;">Watchlist — {e(date_str)}</h2>',
    ]

    if diag.get("degenerate_ranking"):
        parts.append(
            '<div style="background:#fef3c7;border-left:3px solid #f59e0b;'
            'padding:8px 10px;margin:10px 0;font-size:13px;">'
            f'<strong>Ranking quality warning.</strong> {e(str(diag.get("warning") or ""))}'
            "</div>"
        )

    if not present.get("universe"):
        parts.append("<p><em>Ranked universe unavailable this run.</em></p>")
    else:
        total = uni.get("total_distinct_tickers")
        parts.append(
            f'<p style="color:#52525b;margin:4px 0 10px 0;">'
            f"{e(str(total if total is not None else '?'))} distinct tickers · "
            f"lookback {e(str(uni.get('lookback_days', '?')))}d</p>"
        )
        rows = uni.get("candidates") or []
        if rows:
            parts.append(
                '<table cellpadding="5" cellspacing="0" '
                'style="border-collapse:collapse;font-size:13px;width:100%;">'
                '<tr style="background:#f4f4f5;text-align:left;">'
                "<th>#</th><th>Symbol</th><th>Score</th><th>Sector</th>"
                "<th>Theme</th><th>Sources</th></tr>"
            )
            for i, row in enumerate(rows[:max_rows]):
                bg = "#ffffff" if i % 2 == 0 else "#fafafa"
                parts.append(
                    f'<tr style="background:{bg};">'
                    f"<td>{e(str(row.get('rank', '?')))}</td>"
                    f"<td><strong>{e(str(row.get('symbol', '?')))}</strong></td>"
                    f"<td>{e(_fmt_score(row.get('score')))}</td>"
                    f"<td>{e(str(row.get('sector') or '—'))}</td>"
                    f"<td>{e(_fmt_pct_or_dash(row.get('theme_confidence_max')))}</td>"
                    f"<td style=\"color:#71717a;\">{e(', '.join(row.get('sources') or []))}</td>"
                    "</tr>"
                )
            parts.append("</table>")
            if len(rows) > max_rows:
                parts.append(
                    f'<p style="color:#71717a;font-size:12px;">… '
                    f"{len(rows) - max_rows} more in top100_daily.md</p>"
                )

    candidates = inputs.get("candidates") or []
    parts.append('<h3 style="margin:18px 0 6px 0;">New watch candidates</h3>')
    if not candidates:
        parts.append("<p><em>None surfaced this run.</em></p>")
    else:
        parts.append('<ul style="margin:4px 0 0 18px;padding:0;">')
        for c in candidates[:_MAX_CANDIDATE_ROWS]:
            themes = ", ".join(c.get("themes") or []) or "—"
            parts.append(
                f"<li><strong>{e(str(c.get('ticker', '?')))}</strong> — conf "
                f"{e(_fmt_pct_or_dash(c.get('confidence')))} · {e(themes)}</li>"
            )
        parts.append("</ul>")

    alerts = inputs.get("alerts") or []
    parts.append('<h3 style="margin:18px 0 6px 0;">Operator alert queue</h3>')
    if not alerts:
        parts.append("<p><em>No alerts raised.</em></p>")
    else:
        parts.append(
            '<table cellpadding="5" cellspacing="0" '
            'style="border-collapse:collapse;font-size:13px;width:100%;">'
            '<tr style="background:#f4f4f5;text-align:left;">'
            "<th>#</th><th>Symbol</th><th>Eff</th><th>Tier</th>"
            "<th>Conviction</th><th>Sector</th></tr>"
        )
        for i, r in enumerate(alerts):
            bg = "#ffffff" if i % 2 == 0 else "#fafafa"
            parts.append(
                f'<tr style="background:{bg};">'
                f"<td>{e(str(r.get('operator_rank') or '?'))}</td>"
                f"<td><strong>{e(str(r.get('ticker') or '?'))}</strong></td>"
                f"<td>{e(_fmt_score(r.get('effective_score')))}</td>"
                f"<td>{e(str(r.get('alert_tier') or '—'))}</td>"
                f"<td>{e(str(r.get('conviction_band') or '—'))}</td>"
                f"<td>{e(str(r.get('sector') or '—'))}</td>"
                "</tr>"
            )
        parts.append("</table>")

    parts.append(
        f'<p style="color:#71717a;font-size:12px;margin-top:18px;'
        f'border-top:1px solid #e4e4e7;padding-top:8px;">{e(_DISCLAIMER)}</p>'
    )
    parts.append("</div>")
    return "".join(parts)


def build_subject(inputs: dict[str, Any], *, prefix: str = "",
                  watchlist_date: str | None = None) -> str:
    uni = inputs.get("universe") or {}
    rows = uni.get("candidates") or []
    alerts = inputs.get("alerts") or []
    date_str = watchlist_date or resolve_watchlist_date(inputs)
    bits = [f"{len(rows)} ranked"]
    if alerts:
        bits.append(f"{len(alerts)} alert{'s' if len(alerts) != 1 else ''}")
    if (uni.get("ranking_diagnostics") or {}).get("degenerate_ranking"):
        bits.append("ranking degenerate")
    subject = f"Watchlist {date_str} — {' · '.join(bits)}"
    return f"{prefix.strip()} {subject}".strip() if prefix.strip() else subject


# ---------------------------------------------------------------------------
# Artifacts + idempotency
# ---------------------------------------------------------------------------

def write_watchlist_email_status(status: dict[str, Any],
                                 base_dir: str | Path = "outputs") -> Path:
    return safe_write_json(OutputNamespace.LATEST, _STATUS_FILENAME, status,
                           base_dir=base_dir)


def append_watchlist_email_log(entry: dict[str, Any],
                               base_dir: str | Path = "outputs") -> Path:
    path = get_output_path(OutputNamespace.POLICY, _LOG_FILENAME, base_dir=base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")
    return path


def _load_log(base_dir: str | Path) -> list[dict[str, Any]]:
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
            except Exception:
                continue          # a corrupt line must not hide earlier sends
            if isinstance(obj, dict):
                entries.append(obj)
    except Exception:
        return entries
    return entries


def already_sent(watchlist_date: str, base_dir: str | Path = "outputs") -> bool:
    """True when a prior entry recorded sent=True for this watchlist date."""
    for entry in _load_log(base_dir):
        if entry.get("sent") and entry.get("watchlist_date") == watchlist_date:
            return True
    return False


def _sanitize_error(exc: Exception) -> str:
    """Error text with credentials stripped — the password must never reach an artifact."""
    text = f"{exc}"
    for secret in (os.environ.get("EMAIL_PASS"), os.environ.get("WATCHLIST_EMAIL_PASSWORD"),
                   os.environ.get("EMAIL_PASSWORD")):
        if secret:
            text = text.replace(secret, "***")
    return text[:400]


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def _build_message(cfg: WatchlistEmailConfig, subject: str,
                   text_body: str, html_body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.from_addr
    msg["To"] = ", ".join(cfg.to_addrs)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


def _deliver(cfg: WatchlistEmailConfig, msg: EmailMessage) -> dict[str, Any]:
    result: dict[str, Any] = {"sent": False, "error_class": None,
                              "error_message_sanitized": None}
    try:
        if cfg.use_tls:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ctx)
                smtp.ehlo()
                smtp.login(cfg.username, cfg.password)
                smtp.send_message(msg, to_addrs=cfg.to_addrs)
        else:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as smtp:
                smtp.login(cfg.username, cfg.password)
                smtp.send_message(msg, to_addrs=cfg.to_addrs)
        result["sent"] = True
        logger.info("WATCHLIST EMAIL: sent to %d recipient(s)", len(cfg.to_addrs))
    except Exception as exc:
        result["error_class"] = type(exc).__name__
        result["error_message_sanitized"] = _sanitize_error(exc)
        logger.warning("WATCHLIST EMAIL: send failed — %s: %s",
                       result["error_class"], result["error_message_sanitized"])
    return result


def run_watchlist_email_delivery(
    *,
    root: str | Path = ".",
    base_dir: str | Path = "outputs",
    write_files: bool = True,
    env: dict[str, str] | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Build and (when enabled and not dry-run) send the watchlist email.

    Never raises: every failure is recorded on the returned status dict, because
    this runs as a non-blocking pipeline stage and must not abort the chain.
    """
    ts = now_iso or datetime.now(timezone.utc).isoformat()
    cfg = load_watchlist_email_config(env)
    inputs = collect_watchlist_inputs(root)
    watchlist_date = resolve_watchlist_date(inputs)

    status: dict[str, Any] = {
        "generated_at": ts,
        "observe_only": _OBSERVE_ONLY,
        "no_trade": True,
        "schema_version": _SCHEMA_VERSION,
        "source": _SOURCE_LABEL,
        "available": True,
        "enabled": cfg.enabled,
        "dry_run": cfg.dry_run,
        "attempted": False,
        "sent": False,
        "skipped": False,
        "reason": "",
        "watchlist_date": watchlist_date,
        "ranked_count": len(((inputs.get("universe") or {}).get("candidates") or [])),
        "candidate_count": len(inputs.get("candidates") or []),
        "alert_count": len(inputs.get("alerts") or []),
        "degenerate_ranking": bool(
            ((inputs.get("universe") or {}).get("ranking_diagnostics") or {})
            .get("degenerate_ranking")
        ),
        "sources_present": inputs.get("sources_present") or {},
        "recipients_count": len(cfg.to_addrs),
        "smtp_host_present": bool(cfg.smtp_host),
        "username_present": bool(cfg.username),
        "error_class": None,
        "error_message_sanitized": None,
        "disclaimer": _DISCLAIMER,
    }

    subject = build_subject(inputs, prefix=cfg.subject_prefix,
                            watchlist_date=watchlist_date)
    status["subject"] = subject

    def _finish(reason: str) -> dict[str, Any]:
        status["reason"] = reason
        if write_files:
            try:
                write_watchlist_email_status(status, base_dir=base_dir)
                append_watchlist_email_log({
                    k: status[k] for k in (
                        "generated_at", "watchlist_date", "enabled", "dry_run",
                        "attempted", "sent", "skipped", "reason", "ranked_count",
                        "candidate_count", "alert_count", "degenerate_ranking",
                        "recipients_count", "error_class", "observe_only", "no_trade",
                    )
                }, base_dir=base_dir)
            except Exception as exc:   # artifact write must not break the caller
                logger.debug("watchlist_email: artifact write failed — %s", exc)
        return status

    if not cfg.enabled:
        status["skipped"] = True
        logger.info("WATCHLIST EMAIL: disabled (WATCHLIST_EMAIL_ENABLED not set)")
        return _finish("disabled")

    if not inputs.get("sources_present", {}).get("universe") and not inputs.get("alerts"):
        # Nothing worth mailing. Distinguished from "disabled" so a broken producer
        # is visible in the log rather than reading as an operator choice.
        status["skipped"] = True
        return _finish("no_watchlist_content")

    if not cfg.has_valid_recipients():
        status["skipped"] = True
        return _finish("no_valid_recipients")

    if not cfg.dry_run and not cfg.has_smtp_config():
        status["skipped"] = True
        return _finish("smtp_not_configured")

    if not cfg.dry_run and not cfg.force_resend and already_sent(watchlist_date, base_dir):
        status["skipped"] = True
        logger.info("WATCHLIST EMAIL: already sent for %s — skipping", watchlist_date)
        return _finish("already_sent")

    text_body = build_watchlist_text(inputs, max_rows=cfg.max_rows,
                                    watchlist_date=watchlist_date)
    html_body = build_watchlist_html(inputs, max_rows=cfg.max_rows,
                                     watchlist_date=watchlist_date)

    if cfg.dry_run:
        status["attempted"] = False
        status["body_text_chars"] = len(text_body)
        return _finish("dry_run")

    status["attempted"] = True
    msg = _build_message(cfg, subject, text_body, html_body)
    outcome = _deliver(cfg, msg)
    status["sent"] = outcome["sent"]
    status["error_class"] = outcome["error_class"]
    status["error_message_sanitized"] = outcome["error_message_sanitized"]
    return _finish("sent" if outcome["sent"] else "send_failed")


# ---------------------------------------------------------------------------
# Health (paired check — CLAUDE.md Analysis + Health Coverage Requirement)
# ---------------------------------------------------------------------------
# Slack for benign clock jitter between the writer and the health reader before
# calling a status "future-dated". Well under the 30h staleness window, so the
# two guards cannot both fire on the same artifact.
_FUTURE_STAMP_TOLERANCE_H = 1.0

def assess_watchlist_email_health(
    base_dir: str | Path = "outputs",
    now_iso: str | None = None,
    *,
    max_age_hours: float = 30.0,
) -> dict[str, Any]:
    """GREEN/AMBER verdict over watchlist_email_status.json. Never raises.

    AMBER on: `send_failed`; `no_watchlist_content` while enabled (a producer
    upstream broke); `no_valid_recipients` / `smtp_not_configured` while enabled
    (misconfigured opt-in); or a stale status artifact while enabled. Never RED —
    this is an observe-only delivery layer and must not gate the pipeline.

    `disabled` and `dry_run` are the expected inert pre-activation states and are
    reported, never alerted on. An absent artifact means the stage has not run
    yet, which is also inert.
    """
    status_path = get_output_path(OutputNamespace.LATEST, _STATUS_FILENAME,
                                 base_dir=base_dir)
    if not status_path.exists():
        return {"status": "GREEN", "reasons": [], "state": "not_run",
                "observe_only": True}
    data = _load_json_safe(status_path)
    if not data:
        return {"status": "AMBER", "reasons": ["status_artifact_unreadable"],
                "state": "unreadable", "observe_only": True}

    reason = str(data.get("reason") or "")
    enabled = bool(data.get("enabled"))
    reasons: list[str] = []

    if reason == "send_failed":
        reasons.append(f"send_failed:{data.get('error_class') or 'unknown'}")
    if enabled and reason == "no_watchlist_content":
        reasons.append("no_watchlist_content")
    if enabled and reason in {"no_valid_recipients", "smtp_not_configured"}:
        reasons.append(reason)

    if enabled:
        gen = data.get("generated_at")
        try:
            gen_dt = datetime.fromisoformat(str(gen).replace("Z", "+00:00"))
            if gen_dt.tzinfo is None:
                gen_dt = gen_dt.replace(tzinfo=timezone.utc)
            now = (datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
                   if now_iso else datetime.now(timezone.utc))
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            age_h = (now - gen_dt).total_seconds() / 3600.0
            if age_h > max_age_hours:
                reasons.append(f"status_stale:{age_h / 24.0:.1f}d")
            elif age_h < -_FUTURE_STAMP_TOLERANCE_H:
                # A status stamped in the future yields a NEGATIVE age, which
                # silently satisfies `age_h > max_age_hours` and reads as fresh
                # — the staleness guard is disabled by exactly the clock-skew
                # (or bad-fixture) condition it should surface. Same defect
                # class as a verdict derived from absent data. (2026-08-08)
                reasons.append(f"status_future_dated:{-age_h / 24.0:.1f}d")
        except Exception:
            reasons.append("generated_at_unparseable")

    return {
        "status": "AMBER" if reasons else "GREEN",
        "reasons": reasons,
        "state": reason or "unknown",
        "enabled": enabled,
        "sent": bool(data.get("sent")),
        "watchlist_date": data.get("watchlist_date"),
        "ranked_count": data.get("ranked_count"),
        "alert_count": data.get("alert_count"),
        "degenerate_ranking": bool(data.get("degenerate_ranking")),
        "observe_only": True,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m portfolio_automation.watchlist_email_sender",
        description="Deliver the daily watchlist by email (separate from the memo).",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true",
                       help="Build the message but do not send")
    group.add_argument("--send", action="store_true", help="Send (requires SMTP env)")
    group.add_argument("--force-resend", action="store_true",
                       help="Send even if already sent for this watchlist date")
    parser.add_argument("--print-body", action="store_true",
                        help="Print the text body (dry-run inspection)")
    args = parser.parse_args(argv)

    # The CLI always enables delivery for this invocation; the env flag governs
    # only the automated pipeline stage.
    overrides: dict[str, str] = {"WATCHLIST_EMAIL_ENABLED": "1"}
    if args.dry_run:
        overrides["WATCHLIST_EMAIL_DRY_RUN"] = "1"
    elif args.force_resend:
        overrides["WATCHLIST_EMAIL_DRY_RUN"] = "0"
        overrides["WATCHLIST_EMAIL_FORCE_RESEND"] = "1"
    else:
        overrides["WATCHLIST_EMAIL_DRY_RUN"] = "0"

    original: dict[str, str | None] = {}
    for k, v in overrides.items():
        original[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        result = run_watchlist_email_delivery(write_files=True)
    finally:
        for k, orig in original.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig

    for key in ("enabled", "dry_run", "attempted", "sent", "skipped", "reason",
                "watchlist_date", "ranked_count", "alert_count"):
        print(f"{key + ':':<17} {result.get(key)}")
    if args.print_body:
        print()
        print(build_watchlist_text(collect_watchlist_inputs(".")))
    return 0 if (result.get("sent") or result.get("reason") in
                 {"dry_run", "already_sent", "disabled"}) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
