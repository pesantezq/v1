# tests/test_email_suite_health.py
"""Health coverage for the four-email memo suite.

Why this exists (docs/MEMO_SUITE_REDESIGN_PHASE0_AUDIT.md §0): the Phase 0 audit
found the Finance Digest has never been sent — no caller, no cron entry, no
delivery artifact, no test file — while the brief assumed it was live. Nothing in
the repo could have told an operator that. CLAUDE.md's Analysis+Health Coverage
Requirement says a producer without a consumer is debt; this assessor is the
consumer that makes a dormant email surface as a fact instead of an absence.

It also normalizes three genuinely different delivery-log schemas:
  * memo:       memo_date       + sent (bool)
  * watchlist:  watchlist_date  + sent (bool)
  * governance: digest_date     + status ("sent") and NO sent key at all

Observe-only: reads delivery logs, writes one policy artifact, mutates nothing.
"""
from __future__ import annotations

import json

from portfolio_automation import email_suite_health as ESH

NOW = "2026-08-03T22:00:00+00:00"


def _write(tmp_path, name, rows):
    d = tmp_path / "outputs" / "policy"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _memo(date="2026-08-03", sent=True):
    return {"memo_date": date, "sent": sent, "attempted": True, "enabled": True}


def _watchlist(date="2026-08-03", sent=True):
    return {"watchlist_date": date, "sent": sent, "attempted": True, "enabled": True}


def _gov(date="2026-08-03", status="sent"):
    return {"digest_date": date, "status": status, "attempted": True, "ts": NOW}


# --------------------------------------------------------------------------
# Schema normalization
# --------------------------------------------------------------------------

def test_governance_status_string_counts_as_sent():
    """governance has no `sent` bool — only status == "sent"."""
    assert ESH.was_sent({"status": "sent"}) is True
    assert ESH.was_sent({"status": "failed"}) is False


def test_memo_and_watchlist_sent_bool_counts_as_sent():
    assert ESH.was_sent({"sent": True}) is True
    assert ESH.was_sent({"sent": False}) is False


def test_absent_sent_signal_is_not_treated_as_sent():
    """A row with neither key must not read as a successful delivery."""
    assert ESH.was_sent({"attempted": True}) is False
    assert ESH.was_sent({}) is False


def test_date_key_is_resolved_per_schema():
    assert ESH.artifact_date({"memo_date": "2026-08-01"}) == "2026-08-01"
    assert ESH.artifact_date({"watchlist_date": "2026-08-02"}) == "2026-08-02"
    assert ESH.artifact_date({"digest_date": "2026-08-03"}) == "2026-08-03"
    assert ESH.artifact_date({}) is None


# --------------------------------------------------------------------------
# Per-email assessment
# --------------------------------------------------------------------------

def test_sent_today_is_green(tmp_path):
    _write(tmp_path, "memo_delivery_log.jsonl", [_memo()])
    r = ESH.assess_email(tmp_path, ESH.SUITE["daily_memo"], NOW)
    assert r["status"] == "GREEN"
    assert r["last_sent_date"] == "2026-08-03"


def test_daily_email_stale_by_days_is_amber_then_red(tmp_path):
    _write(tmp_path, "memo_delivery_log.jsonl", [_memo(date="2026-08-01")])
    assert ESH.assess_email(tmp_path, ESH.SUITE["daily_memo"], NOW)["status"] == "AMBER"
    _write(tmp_path, "memo_delivery_log.jsonl", [_memo(date="2026-07-20")])
    assert ESH.assess_email(tmp_path, ESH.SUITE["daily_memo"], NOW)["status"] == "RED"


def test_weekly_cadence_tolerates_a_week(tmp_path):
    """The watchlist is weekly — 3 days old is not a problem for it."""
    _write(tmp_path, "watchlist_email_log.jsonl", [_watchlist(date="2026-07-31")])
    assert ESH.assess_email(tmp_path, ESH.SUITE["watchlist_digest"], NOW)["status"] == "GREEN"


def test_never_sent_is_red_not_green(tmp_path):
    """An empty log means we have no evidence of delivery — never GREEN."""
    _write(tmp_path, "memo_delivery_log.jsonl", [])
    r = ESH.assess_email(tmp_path, ESH.SUITE["daily_memo"], NOW)
    assert r["status"] == "RED"
    assert r["last_sent_date"] is None
    assert "never_sent" in r["reasons"]


def test_missing_log_is_red_not_silently_ok(tmp_path):
    r = ESH.assess_email(tmp_path, ESH.SUITE["daily_memo"], NOW)
    assert r["status"] == "RED"
    assert any("log_missing" in x for x in r["reasons"])


def test_attempted_but_all_failed_is_red(tmp_path):
    _write(tmp_path, "governance_digest_log.jsonl", [_gov(status="failed")])
    r = ESH.assess_email(tmp_path, ESH.SUITE["governance_digest"], NOW)
    assert r["status"] == "RED"


# --------------------------------------------------------------------------
# The dormant Finance Digest — the reason this module exists
# --------------------------------------------------------------------------

def test_finance_digest_is_reported_dormant_not_missing(tmp_path):
    r = ESH.assess_email(tmp_path, ESH.SUITE["finance_digest"], NOW)
    assert r["status"] == "DORMANT"
    assert r["is_debt"] is True
    assert "no_delivery_path" in r["reasons"]


def test_dormant_does_not_degrade_the_rollup(tmp_path):
    """Recorded as debt, but it must not create permanent alarm fatigue."""
    _write(tmp_path, "memo_delivery_log.jsonl", [_memo()])
    _write(tmp_path, "watchlist_email_log.jsonl", [_watchlist()])
    _write(tmp_path, "governance_digest_log.jsonl", [_gov()])
    out = ESH.build_email_suite_health(tmp_path, now=NOW)
    assert out["status"] == "GREEN"
    assert out["dormant_count"] == 1
    assert "finance_digest" in out["debt"]


def test_dormant_is_still_visible_in_the_payload(tmp_path):
    out = ESH.build_email_suite_health(tmp_path, now=NOW)
    assert out["emails"]["finance_digest"]["status"] == "DORMANT"
    assert out["emails"]["finance_digest"]["question"]


# --------------------------------------------------------------------------
# Rollup + contract
# --------------------------------------------------------------------------

def test_rollup_takes_the_worst_live_status(tmp_path):
    _write(tmp_path, "memo_delivery_log.jsonl", [_memo(date="2026-07-20")])   # RED
    _write(tmp_path, "watchlist_email_log.jsonl", [_watchlist()])             # GREEN
    _write(tmp_path, "governance_digest_log.jsonl", [_gov()])                 # GREEN
    assert ESH.build_email_suite_health(tmp_path, now=NOW)["status"] == "RED"


def test_every_email_declares_its_responsibility_question(tmp_path):
    """The brief requires each email to own exactly one question."""
    out = ESH.build_email_suite_health(tmp_path, now=NOW)
    questions = {k: v["question"] for k, v in out["emails"].items()}
    assert len(questions) == 4
    assert len(set(questions.values())) == 4, "two emails claim the same job"


def test_artifact_is_observe_only(tmp_path):
    out = ESH.build_email_suite_health(tmp_path, now=NOW)
    assert out["observe_only"] is True
    assert out["feeds_decision_engine"] is False


def test_run_writes_the_policy_artifact(tmp_path):
    _write(tmp_path, "memo_delivery_log.jsonl", [_memo()])
    ESH.run_email_suite_health(root=tmp_path, now=NOW)
    p = tmp_path / "outputs" / "policy" / "email_suite_health.json"
    assert p.exists()
    assert json.loads(p.read_text())["schema_version"]


def test_never_raises_on_corrupt_log(tmp_path):
    d = tmp_path / "outputs" / "policy"
    d.mkdir(parents=True, exist_ok=True)
    (d / "memo_delivery_log.jsonl").write_text("{not json\n")
    r = ESH.assess_email(tmp_path, ESH.SUITE["daily_memo"], NOW)
    assert r["status"] in ("RED", "AMBER")


def test_deterministic_for_fixed_inputs(tmp_path):
    _write(tmp_path, "memo_delivery_log.jsonl", [_memo()])
    a = ESH.build_email_suite_health(tmp_path, now=NOW)
    b = ESH.build_email_suite_health(tmp_path, now=NOW)
    assert a == b
