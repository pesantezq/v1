"""Tests for portfolio_automation/watchlist_email_sender.py.

Operator decision 2026-07-29: the watchlist ships as its OWN email, isolated from
the memo so neither can break the other, but reusing existing infrastructure
(utils.get_env_first, data_governance namespaces, the delivery-status +
append-only-log + date-dedup shape, run_aux_stage wiring).

Coverage focus:
  - disabled by default; only WATCHLIST_EMAIL_ENABLED turns it on (no fallback)
  - transport/recipients DO fall back to the generic mail config
  - isolation from memo_email_sender (no import, no shared artifact or ledger)
  - date-based dedup, and force_resend bypassing it
  - each input degrades independently with a DISTINCT reason
  - the ranking-degeneracy caveat reaches the body
  - HTML escaping; the SMTP password never reaches an artifact
  - never raises; observe_only / no_trade hardcoded
  - the paired health assessor
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation import watchlist_email_sender as wes

_ENV_NAMES = [
    "WATCHLIST_EMAIL_ENABLED", "WATCHLIST_EMAIL_DRY_RUN", "WATCHLIST_EMAIL_TO",
    "WATCHLIST_EMAIL_FROM", "WATCHLIST_EMAIL_USERNAME", "WATCHLIST_EMAIL_PASSWORD",
    "WATCHLIST_EMAIL_SMTP_HOST", "WATCHLIST_EMAIL_SMTP_PORT",
    "WATCHLIST_EMAIL_FORCE_RESEND", "WATCHLIST_EMAIL_MAX_ROWS",
    "WATCHLIST_EMAIL_SUBJECT_PREFIX", "WATCHLIST_EMAIL_USE_TLS",
    "SMTP_SERVER", "SMTP_PORT", "EMAIL_USER", "EMAIL_PASS", "EMAIL_TO",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _universe(n=5, degenerate=False):
    return {
        "generated_at": "2026-07-29T15:00:20.797059+00:00",
        "lookback_days": 1,
        "total_distinct_tickers": n,
        "candidates": [
            {"rank": i + 1, "symbol": f"SYM{i}", "score": 0.5 - i * 0.01,
             "sector": "Technology", "theme_confidence_max": 0.85,
             "sources": ["static", "recent_signal"]}
            for i in range(n)
        ],
        "source_breakdown": {"static": n, "recent_signal": 2},
        "ranking_diagnostics": {
            "degenerate_ranking": degenerate,
            "warning": "Ranking is degenerate this run — majority tie at 0.16."
            if degenerate else "",
        },
    }


def _write_inputs(root: Path, *, universe=None, candidates=None, alerts_csv=None):
    latest = root / "outputs" / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    if universe is not None:
        (latest / "top100_daily.json").write_text(json.dumps(universe), encoding="utf-8")
    if candidates is not None:
        (latest / "watch_candidates.json").write_text(json.dumps(candidates), encoding="utf-8")
    if alerts_csv is not None:
        (latest / "watchlist_alerts.csv").write_text(alerts_csv, encoding="utf-8-sig")


_ALERTS_CSV = (
    "operator_rank,ticker,effective_score,alert_tier,conviction_band,sector\n"
    "1,AMD,0.682,high,normal,Technology\n"
    "2,PLTR,0.647,high,normal,Technology\n"
)


# ── Config ───────────────────────────────────────────────────────────────────

def test_disabled_by_default():
    cfg = wes.load_watchlist_email_config({})
    assert cfg.enabled is False
    assert cfg.dry_run is True


def test_generic_mail_config_alone_does_not_enable():
    """A working SMTP config must never be sufficient to start emailing."""
    cfg = wes.load_watchlist_email_config({
        "SMTP_SERVER": "smtp.example.com", "EMAIL_USER": "a@b.com",
        "EMAIL_PASS": "pw", "EMAIL_TO": "a@b.com",
    })
    assert cfg.enabled is False
    assert cfg.has_smtp_config() is True, "transport resolves; only the flag is missing"


def test_transport_falls_back_to_generic_names():
    cfg = wes.load_watchlist_email_config({
        "WATCHLIST_EMAIL_ENABLED": "1",
        "SMTP_SERVER": "smtp.example.com", "SMTP_PORT": "2525",
        "EMAIL_USER": "ops@example.com", "EMAIL_PASS": "pw",
        "EMAIL_TO": "ops@example.com",
    })
    assert cfg.enabled is True
    assert (cfg.smtp_host, cfg.smtp_port) == ("smtp.example.com", 2525)
    assert cfg.username == "ops@example.com"
    assert cfg.from_addr == "ops@example.com"
    assert cfg.to_addrs == ["ops@example.com"]
    assert cfg.has_valid_recipients() and cfg.has_smtp_config()


def test_dedicated_names_win_over_generic():
    cfg = wes.load_watchlist_email_config({
        "WATCHLIST_EMAIL_ENABLED": "1",
        "SMTP_SERVER": "generic.example.com", "EMAIL_USER": "generic@example.com",
        "EMAIL_PASS": "pw", "EMAIL_TO": "generic@example.com",
        "WATCHLIST_EMAIL_SMTP_HOST": "dedicated.example.com",
        "WATCHLIST_EMAIL_TO": "wl@example.com",
    })
    assert cfg.smtp_host == "dedicated.example.com"
    assert cfg.to_addrs == ["wl@example.com"]


def test_malformed_max_rows_and_port_fall_back_to_defaults():
    cfg = wes.load_watchlist_email_config({
        "WATCHLIST_EMAIL_MAX_ROWS": "not-a-number",
        "WATCHLIST_EMAIL_SMTP_PORT": "",
    })
    assert cfg.max_rows == wes._DEFAULT_MAX_ROWS
    assert cfg.smtp_port == 587


# ── Isolation from the memo path ─────────────────────────────────────────────

def test_does_not_import_memo_email_sender():
    """Isolation is the operator's stated requirement: a memo refactor must not
    reach this module, and vice versa."""
    src = Path("portfolio_automation/watchlist_email_sender.py").read_text(encoding="utf-8")
    code = "\n".join(
        ln for ln in src.splitlines()
        if ln.strip().startswith(("import ", "from "))
    )
    assert "memo_email_sender" not in code


def test_artifacts_and_env_do_not_collide_with_memo():
    from portfolio_automation import memo_email_sender as mes
    assert wes._STATUS_FILENAME != mes._STATUS_FILENAME
    assert wes._LOG_FILENAME != mes._LOG_FILENAME
    assert "MEMO_EMAIL" not in "".join(_ENV_NAMES[:12])


# ── Content ──────────────────────────────────────────────────────────────────

def test_body_includes_ranked_rows_sources_candidates_and_alerts(tmp_path):
    _write_inputs(tmp_path, universe=_universe(3),
                  candidates={"run_date": "2026-07-29", "watch_candidates": [
                      {"ticker": "GOOGL", "confidence": 0.85, "themes": ["AI Infrastructure"]}]},
                  alerts_csv=_ALERTS_CSV)
    inputs = wes.collect_watchlist_inputs(tmp_path)
    text = wes.build_watchlist_text(inputs)
    assert "RANKED UNIVERSE" in text and "SYM0" in text
    assert "SOURCE BREAKDOWN" in text and "static" in text
    assert "GOOGL" in text and "AI Infrastructure" in text
    assert "OPERATOR ALERT QUEUE" in text and "AMD" in text and "PLTR" in text
    assert "no order is ever placed" in text


def test_degenerate_ranking_warning_reaches_body_before_the_table(tmp_path):
    """The caveat must precede the rank order it qualifies."""
    _write_inputs(tmp_path, universe=_universe(3, degenerate=True))
    inputs = wes.collect_watchlist_inputs(tmp_path)
    text = wes.build_watchlist_text(inputs)
    assert "RANKING QUALITY WARNING" in text
    assert text.index("RANKING QUALITY WARNING") < text.index("SYM0")
    html = wes.build_watchlist_html(inputs)
    assert "Ranking quality warning" in html


def test_clean_ranking_emits_no_warning(tmp_path):
    _write_inputs(tmp_path, universe=_universe(3, degenerate=False))
    inputs = wes.collect_watchlist_inputs(tmp_path)
    assert "RANKING QUALITY WARNING" not in wes.build_watchlist_text(inputs)


def test_row_cap_is_applied_and_the_remainder_is_disclosed(tmp_path):
    """A truncated list must say so — a silent cap reads as a complete universe."""
    _write_inputs(tmp_path, universe=_universe(30))
    inputs = wes.collect_watchlist_inputs(tmp_path)
    text = wes.build_watchlist_text(inputs, max_rows=5)
    assert "SYM4" in text and "SYM5" not in text
    assert "25 more" in text


def test_alerts_csv_bom_is_tolerated(tmp_path):
    _write_inputs(tmp_path, universe=_universe(2), alerts_csv=_ALERTS_CSV)
    inputs = wes.collect_watchlist_inputs(tmp_path)
    assert [r["ticker"] for r in inputs["alerts"]] == ["AMD", "PLTR"]


def test_html_escapes_injected_markup(tmp_path):
    uni = _universe(1)
    uni["candidates"][0]["symbol"] = "<script>alert(1)</script>"
    _write_inputs(tmp_path, universe=uni)
    html = wes.build_watchlist_html(wes.collect_watchlist_inputs(tmp_path))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_subject_reports_counts_and_degeneracy(tmp_path):
    _write_inputs(tmp_path, universe=_universe(4, degenerate=True), alerts_csv=_ALERTS_CSV)
    inputs = wes.collect_watchlist_inputs(tmp_path)
    subject = wes.build_subject(inputs, prefix="[stockbot]")
    assert subject.startswith("[stockbot] Watchlist 2026-07-29")
    assert "4 ranked" in subject and "2 alerts" in subject
    assert "ranking degenerate" in subject


# ── Degradation ──────────────────────────────────────────────────────────────

def test_each_source_degrades_independently(tmp_path):
    _write_inputs(tmp_path, universe=_universe(2))     # no candidates, no alerts
    inputs = wes.collect_watchlist_inputs(tmp_path)
    assert inputs["sources_present"] == {"universe": True, "candidates": False,
                                         "alerts": False}
    text = wes.build_watchlist_text(inputs)
    assert "SYM0" in text
    assert "not readable this run" in text        # candidates section
    assert "No alerts file this run" in text


def test_missing_universe_is_named_not_silently_empty(tmp_path):
    _write_inputs(tmp_path, alerts_csv=_ALERTS_CSV)
    text = wes.build_watchlist_text(wes.collect_watchlist_inputs(tmp_path))
    assert "Unavailable — top100_daily.json was not readable" in text


def test_collect_never_raises_on_corrupt_inputs(tmp_path):
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (latest / "top100_daily.json").write_text("{not json", encoding="utf-8")
    (latest / "watch_candidates.json").write_text("[]", encoding="utf-8")
    inputs = wes.collect_watchlist_inputs(tmp_path)
    assert inputs["universe"] == {}
    assert inputs["candidates"] == []
    wes.build_watchlist_text(inputs)      # must not raise
    wes.build_watchlist_html(inputs)


def test_watchlist_date_precedence(tmp_path):
    _write_inputs(tmp_path, universe=_universe(1))
    assert wes.resolve_watchlist_date(wes.collect_watchlist_inputs(tmp_path)) == "2026-07-29"
    # No universe → fall back to watch_candidates.run_date
    other = tmp_path / "other"
    _write_inputs(other, candidates={"run_date": "2026-07-28", "watch_candidates": []})
    assert wes.resolve_watchlist_date(wes.collect_watchlist_inputs(other)) == "2026-07-28"


# ── Delivery flow ────────────────────────────────────────────────────────────

def _run(tmp_path, env, **kw):
    return wes.run_watchlist_email_delivery(
        root=tmp_path, base_dir=tmp_path / "outputs", env=env, **kw)


def test_disabled_skips_and_writes_artifacts(tmp_path):
    _write_inputs(tmp_path, universe=_universe(2))
    res = _run(tmp_path, {})
    assert res["enabled"] is False and res["skipped"] is True
    assert res["reason"] == "disabled"
    assert res["observe_only"] is True and res["no_trade"] is True
    assert (tmp_path / "outputs" / "latest" / "watchlist_email_status.json").exists()
    assert (tmp_path / "outputs" / "policy" / "watchlist_email_log.jsonl").exists()


def test_dry_run_builds_without_sending(tmp_path):
    _write_inputs(tmp_path, universe=_universe(2))
    res = _run(tmp_path, {"WATCHLIST_EMAIL_ENABLED": "1", "EMAIL_TO": "a@b.com"})
    assert res["reason"] == "dry_run"
    assert res["attempted"] is False and res["sent"] is False
    assert res["body_text_chars"] > 0


def test_no_content_is_distinct_from_disabled(tmp_path):
    """A broken upstream producer must not look like an operator opt-out."""
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    res = _run(tmp_path, {"WATCHLIST_EMAIL_ENABLED": "1", "EMAIL_TO": "a@b.com"})
    assert res["reason"] == "no_watchlist_content"
    assert res["skipped"] is True


def test_missing_recipients_and_smtp_have_distinct_reasons(tmp_path):
    _write_inputs(tmp_path, universe=_universe(2))
    res = _run(tmp_path, {"WATCHLIST_EMAIL_ENABLED": "1"})
    assert res["reason"] == "no_valid_recipients"
    res = _run(tmp_path, {"WATCHLIST_EMAIL_ENABLED": "1", "WATCHLIST_EMAIL_DRY_RUN": "0",
                          "EMAIL_TO": "a@b.com"})
    assert res["reason"] == "smtp_not_configured"


def test_already_sent_dedup_and_force_resend(tmp_path, monkeypatch):
    _write_inputs(tmp_path, universe=_universe(2))
    sent_env = {
        "WATCHLIST_EMAIL_ENABLED": "1", "WATCHLIST_EMAIL_DRY_RUN": "0",
        "SMTP_SERVER": "smtp.example.com", "EMAIL_USER": "a@b.com",
        "EMAIL_PASS": "pw", "EMAIL_TO": "a@b.com",
    }
    calls: list[str] = []
    monkeypatch.setattr(wes, "_deliver", lambda cfg, msg: (
        calls.append(msg["Subject"]) or
        {"sent": True, "error_class": None, "error_message_sanitized": None}))

    first = _run(tmp_path, sent_env)
    assert first["sent"] is True and first["reason"] == "sent"
    second = _run(tmp_path, sent_env)
    assert second["reason"] == "already_sent" and second["sent"] is False
    assert len(calls) == 1, "the same watchlist date must not be mailed twice"

    forced = _run(tmp_path, {**sent_env, "WATCHLIST_EMAIL_FORCE_RESEND": "1"})
    assert forced["sent"] is True
    assert len(calls) == 2


def test_send_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    _write_inputs(tmp_path, universe=_universe(2))
    monkeypatch.setattr(wes, "_deliver", lambda cfg, msg: {
        "sent": False, "error_class": "SMTPAuthenticationError",
        "error_message_sanitized": "auth failed"})
    res = _run(tmp_path, {
        "WATCHLIST_EMAIL_ENABLED": "1", "WATCHLIST_EMAIL_DRY_RUN": "0",
        "SMTP_SERVER": "smtp.example.com", "EMAIL_USER": "a@b.com",
        "EMAIL_PASS": "pw", "EMAIL_TO": "a@b.com"})
    assert res["reason"] == "send_failed"
    assert res["error_class"] == "SMTPAuthenticationError"


def test_password_never_appears_in_artifacts(tmp_path, monkeypatch):
    _write_inputs(tmp_path, universe=_universe(2))
    secret = "sup3r-secret-app-pw"
    monkeypatch.setenv("EMAIL_PASS", secret)
    monkeypatch.setattr(wes, "_deliver", lambda cfg, msg: {
        "sent": False, "error_class": "SMTPException",
        "error_message_sanitized": wes._sanitize_error(Exception(f"bad pw {secret}"))})
    _run(tmp_path, {
        "WATCHLIST_EMAIL_ENABLED": "1", "WATCHLIST_EMAIL_DRY_RUN": "0",
        "SMTP_SERVER": "s", "EMAIL_USER": "a@b.com", "EMAIL_PASS": secret,
        "EMAIL_TO": "a@b.com"})
    for rel in (("latest", "watchlist_email_status.json"),
                ("policy", "watchlist_email_log.jsonl")):
        blob = (tmp_path / "outputs" / rel[0] / rel[1]).read_text(encoding="utf-8")
        assert secret not in blob


def test_message_has_text_and_html_alternatives(tmp_path):
    _write_inputs(tmp_path, universe=_universe(2))
    cfg = wes.load_watchlist_email_config({
        "WATCHLIST_EMAIL_ENABLED": "1", "SMTP_SERVER": "s",
        "EMAIL_USER": "a@b.com", "EMAIL_PASS": "pw", "EMAIL_TO": "a@b.com"})
    inputs = wes.collect_watchlist_inputs(tmp_path)
    msg = wes._build_message(cfg, "subj", wes.build_watchlist_text(inputs),
                            wes.build_watchlist_html(inputs))
    assert msg.is_multipart()
    assert {p.get_content_type() for p in msg.iter_parts()} == {
        "text/plain", "text/html"}


# ── Health ───────────────────────────────────────────────────────────────────

def test_health_green_when_never_run(tmp_path):
    h = wes.assess_watchlist_email_health(base_dir=tmp_path / "outputs")
    assert h["status"] == "GREEN" and h["state"] == "not_run"


def test_health_green_while_disabled(tmp_path):
    _write_inputs(tmp_path, universe=_universe(2))
    _run(tmp_path, {})
    h = wes.assess_watchlist_email_health(base_dir=tmp_path / "outputs",
                                         now_iso="2026-07-29T16:00:00+00:00")
    assert h["status"] == "GREEN", "inert is not a fault"
    assert h["state"] == "disabled"


def test_health_amber_on_send_failure(tmp_path, monkeypatch):
    _write_inputs(tmp_path, universe=_universe(2))
    monkeypatch.setattr(wes, "_deliver", lambda cfg, msg: {
        "sent": False, "error_class": "SMTPAuthenticationError",
        "error_message_sanitized": "x"})
    _run(tmp_path, {"WATCHLIST_EMAIL_ENABLED": "1", "WATCHLIST_EMAIL_DRY_RUN": "0",
                    "SMTP_SERVER": "s", "EMAIL_USER": "a@b.com", "EMAIL_PASS": "pw",
                    "EMAIL_TO": "a@b.com"})
    h = wes.assess_watchlist_email_health(base_dir=tmp_path / "outputs",
                                         now_iso="2026-07-29T16:00:00+00:00")
    assert h["status"] == "AMBER"
    assert any(r.startswith("send_failed") for r in h["reasons"])


def test_health_amber_when_enabled_but_no_content(tmp_path):
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    _run(tmp_path, {"WATCHLIST_EMAIL_ENABLED": "1", "EMAIL_TO": "a@b.com"})
    h = wes.assess_watchlist_email_health(base_dir=tmp_path / "outputs",
                                         now_iso="2026-07-29T16:00:00+00:00")
    assert h["status"] == "AMBER" and "no_watchlist_content" in h["reasons"]


def test_health_amber_on_stale_status_while_enabled(tmp_path):
    _write_inputs(tmp_path, universe=_universe(2))
    _run(tmp_path, {"WATCHLIST_EMAIL_ENABLED": "1", "EMAIL_TO": "a@b.com"})
    h = wes.assess_watchlist_email_health(base_dir=tmp_path / "outputs",
                                         now_iso="2026-08-05T16:00:00+00:00")
    assert h["status"] == "AMBER"
    assert any(r.startswith("status_stale") for r in h["reasons"])


def test_health_never_returns_red(tmp_path, monkeypatch):
    """Observe-only delivery must never gate the pipeline."""
    _write_inputs(tmp_path, universe=_universe(2))
    monkeypatch.setattr(wes, "_deliver", lambda cfg, msg: {
        "sent": False, "error_class": "Boom", "error_message_sanitized": "x"})
    _run(tmp_path, {"WATCHLIST_EMAIL_ENABLED": "1", "WATCHLIST_EMAIL_DRY_RUN": "0",
                    "SMTP_SERVER": "s", "EMAIL_USER": "a@b.com", "EMAIL_PASS": "pw",
                    "EMAIL_TO": "a@b.com"})
    for now in ("2026-07-29T16:00:00+00:00", "2027-01-01T00:00:00+00:00"):
        assert wes.assess_watchlist_email_health(
            base_dir=tmp_path / "outputs", now_iso=now)["status"] in {"GREEN", "AMBER"}


def test_health_amber_on_unreadable_status(tmp_path):
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (latest / "watchlist_email_status.json").write_text("{broken", encoding="utf-8")
    h = wes.assess_watchlist_email_health(base_dir=tmp_path / "outputs")
    assert h["status"] == "AMBER" and "status_artifact_unreadable" in h["reasons"]
