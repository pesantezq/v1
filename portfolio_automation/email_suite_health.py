"""Delivery health for the four-email memo suite — observe-only.

Why this module exists
---------------------
The 2026-08-03 Phase 0 audit (``docs/MEMO_SUITE_REDESIGN_PHASE0_AUDIT.md`` §0)
found that the Finance Digest has **never been sent**: ``FinanceEmailDigest.send_digest``
has no caller, no cron entry, no delivery artifact, and no test file. The mission
brief that commissioned the redesign assumed it was live. Nothing in the repo
could have contradicted that, because *absence of a delivery log looked exactly
like absence of a check*.

CLAUDE.md's Analysis+Health Coverage Requirement states that every artifact needs
at least one consumer and that producers without consumers are debt. This
assessor is that consumer for the delivery layer: it turns "this email never
sends" from an invisible gap into a recorded, named state.

It also normalizes three genuinely different delivery-log schemas, which is why a
generic "read the log" check would have been wrong:

===============  ==================  =========================
email            date key            success signal
===============  ==================  =========================
daily memo       ``memo_date``       ``sent`` (bool)
watchlist        ``watchlist_date``  ``sent`` (bool)
governance       ``digest_date``     ``status == "sent"`` only
===============  ==================  =========================

Observe-only: reads delivery logs, writes one POLICY artifact, and mutates no
decision, allocation, score, approval, or portfolio state. Deterministic — the
caller supplies ``now``, so fixed inputs give a byte-identical payload.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("portfolio_automation.email_suite_health")

SCHEMA_VERSION = "1"

_STATUS_RANK = {"GREEN": 0, "DORMANT": 0, "AMBER": 1, "RED": 2}

# Declarative suite registry. Each email owns exactly ONE question — the brief's
# anti-duplication contract — and a test asserts no two questions collide.
SUITE: dict[str, dict[str, Any]] = {
    "daily_memo": {
        "label": "Daily Investment Memo",
        "question": "What can/should happen in the portfolio today?",
        "sender": "portfolio_automation/memo_email_sender.py",
        "log": "memo_delivery_log.jsonl",
        "cadence": "daily",
        "amber_after_days": 1,
        "red_after_days": 3,
        "live": True,
    },
    "finance_digest": {
        "label": "Finance Digest",
        "question": ("What should happen with cash safety, contributions, "
                     "allocation policy, and the long-term plan?"),
        "sender": "email_digest.py (FinanceEmailDigest)",
        # No log: there is no delivery path at all. See dormant_reason.
        "log": None,
        "cadence": "n/a",
        "live": False,
        "dormant_reason": "no_delivery_path",
        "dormant_detail": (
            "Built but never wired: send_digest() has no caller, no cron entry, "
            "no delivery artifact, and no test file (audit 2026-08-03). Activating "
            "it is a scoped decision, not a bug fix — it would create a new "
            "outbound email path."
        ),
    },
    "watchlist_digest": {
        "label": "Watchlist Digest",
        "question": "What deserves research attention, and how trustworthy is today's ranking?",
        "sender": "portfolio_automation/watchlist_email_sender.py",
        "log": "watchlist_email_log.jsonl",
        "cadence": "weekly",
        "amber_after_days": 8,
        "red_after_days": 14,
        "live": True,
    },
    "governance_digest": {
        "label": "Governance Digest",
        "question": "What needs approval/veto and are authority controls healthy?",
        "sender": "portfolio_automation/sim_governance/governance_digest.py",
        "log": "governance_digest_log.jsonl",
        "cadence": "daily",
        "amber_after_days": 1,
        "red_after_days": 3,
        "live": True,
    },
}


# ---------------------------------------------------------------------------
# Schema normalization
# ---------------------------------------------------------------------------

def was_sent(row: Any) -> bool:
    """True only on positive evidence of delivery.

    Fails closed: a row carrying neither ``sent`` nor ``status`` is NOT a
    delivery. Treating an ambiguous row as sent would let a broken sender read
    healthy, which is the failure mode this module was written to catch.
    """
    if not isinstance(row, dict):
        return False
    if "sent" in row:
        return bool(row.get("sent"))
    status = row.get("status")
    if status is not None:
        return str(status).strip().lower() == "sent"
    return False


def artifact_date(row: Any) -> str | None:
    """The date the entry is ABOUT, across the three log dialects."""
    if not isinstance(row, dict):
        return None
    for key in ("memo_date", "watchlist_date", "digest_date"):
        value = row.get(key)
        if isinstance(value, str) and len(value) >= 10:
            return value[:10]
    return None


def _read_log(path: Path) -> tuple[list[dict], list[str]]:
    """Return (rows, reasons). A corrupt line is skipped and reported, never fatal."""
    if not path.exists():
        return [], [f"log_missing:{path.name}"]
    rows: list[dict] = []
    bad = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                bad += 1
    except OSError as exc:
        return [], [f"log_unreadable:{exc}"]
    return rows, ([f"corrupt_lines:{bad}"] if bad else [])


def _days_between(later: str, earlier: str) -> int | None:
    try:
        a = datetime.fromisoformat(str(later).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(earlier).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return (a.date() - b.date()).days


# ---------------------------------------------------------------------------
# Per-email assessment
# ---------------------------------------------------------------------------

def assess_email(root: Path | str, spec: dict[str, Any], now: str) -> dict[str, Any]:
    """Assess one email's delivery health. Never raises."""
    base = {
        "label": spec["label"],
        "question": spec["question"],
        "sender": spec["sender"],
        "cadence": spec["cadence"],
        "last_sent_date": None,
        "days_since_sent": None,
        "reasons": [],
        "is_debt": False,
    }

    if not spec.get("live"):
        # Dormant is a first-class state, distinct from "broken" and from "fine".
        # It ranks as non-degrading so it cannot cause permanent alarm fatigue,
        # but it is always listed so it cannot be forgotten either.
        return {**base, "status": "DORMANT", "is_debt": True,
                "reasons": [spec.get("dormant_reason", "not_wired")],
                "detail": spec.get("dormant_detail", "")}

    path = Path(root) / "outputs" / "policy" / str(spec["log"])
    rows, reasons = _read_log(path)
    sent_dates = sorted({d for d in (artifact_date(r) for r in rows if was_sent(r)) if d})

    if not sent_dates:
        return {**base, "status": "RED",
                "reasons": reasons + (["never_sent"] if not reasons or rows else ["never_sent"])}

    last = sent_dates[-1]
    age = _days_between(now, last)
    status = "GREEN"
    if age is None:
        status, reasons = "AMBER", reasons + ["undeterminable_age"]
    elif age > int(spec["red_after_days"]):
        status, reasons = "RED", reasons + [f"stale:{age}d>{spec['red_after_days']}d"]
    elif age > int(spec["amber_after_days"]):
        status, reasons = "AMBER", reasons + [f"stale:{age}d>{spec['amber_after_days']}d"]

    return {**base, "status": status, "last_sent_date": last,
            "days_since_sent": age, "reasons": reasons}


# ---------------------------------------------------------------------------
# Suite rollup
# ---------------------------------------------------------------------------

def build_email_suite_health(root: Path | str = ".", *, now: str) -> dict[str, Any]:
    """Assess all four emails and roll up to the worst LIVE status."""
    emails = {key: assess_email(root, spec, now) for key, spec in SUITE.items()}

    worst = "GREEN"
    for result in emails.values():
        if _STATUS_RANK.get(result["status"], 0) > _STATUS_RANK[worst]:
            worst = result["status"]

    debt = sorted(k for k, v in emails.items() if v.get("is_debt"))
    return {
        "schema_version": SCHEMA_VERSION,
        "observe_only": True,
        "feeds_decision_engine": False,
        "assessed_at": now,
        "status": worst,
        "emails": emails,
        "live_count": sum(1 for v in emails.values() if v["status"] != "DORMANT"),
        "dormant_count": sum(1 for v in emails.values() if v["status"] == "DORMANT"),
        "debt": debt,
    }


def run_email_suite_health(root: Path | str = ".", now: str | None = None,
                           *, write: bool = True) -> dict[str, Any]:
    """Pipeline entry point. Non-blocking: returns a degraded payload on failure."""
    stamp = now or datetime.now().astimezone().isoformat()
    try:
        payload = build_email_suite_health(root, now=stamp)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("email_suite_health: assessment failed: %s", exc)
        return {"schema_version": SCHEMA_VERSION, "observe_only": True,
                "feeds_decision_engine": False, "assessed_at": stamp,
                "status": "AMBER", "error": str(exc), "emails": {}}

    if write:
        try:
            from portfolio_automation.data_governance import (
                OutputNamespace, ensure_output_dir, get_output_path,
            )
            base_dir = Path(root) / "outputs"
            ensure_output_dir(OutputNamespace.POLICY, base_dir=base_dir)
            out = get_output_path(OutputNamespace.POLICY, "email_suite_health.json",
                                  base_dir=base_dir)
            out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        except Exception as exc:
            logger.warning("email_suite_health: write failed: %s", exc)
    return payload
