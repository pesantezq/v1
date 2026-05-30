"""
Daily Run Status — observe-only ops-visibility for the official lane.

Mirrors the sandbox lane's `sandbox_run_status.json` pattern so operators
can answer "did today's run succeed?" without grepping logs. The producer
scans today's daily_safe log file and the LATEST namespace to determine:

  - Which stages ran, in order, and what each reported.
  - Which artifacts were written (paths + freshness).
  - Which artifacts are expected but absent (e.g. when a stage failed
    or the policy_activation gate hasn't passed yet).
  - The pipeline's overall status (ok / partial / failed).

Hard guarantees:
  - observe_only=True hardcoded.
  - Reads files only — no network, no FMP, no decision/score mutation.
  - Degrades safely when the log file or expected artifacts are missing.

Public API:
  scan_log_stages(log_path) -> list[dict]
  scan_expected_artifacts(root) -> list[dict]
  build_daily_run_status(root) -> dict
  run_daily_run_status(root, write_files) -> dict
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger("stockbot.portfolio_automation.daily_run_status")

_SCHEMA_VERSION = "1"
_SOURCE_LABEL = "daily_run_status"
_OBSERVE_ONLY = True

_DISCLAIMER = (
    "Observe-only daily-run telemetry. Reads logs + artifact timestamps; "
    "does not call APIs and does not mutate any decision, allocation, or "
    "score state."
)

# Content-liveness checks: artifacts whose mere presence isn't enough — they
# also need non-empty payload to indicate the upstream pipeline produced
# meaningful output. Each entry is (relative_path, name, payload_predicate, rationale).
# The predicate receives the parsed JSON dict and returns (status, observed)
# where status is one of "ok" | "warn" | "unknown".
def _check_theme_signals(payload: dict[str, Any]) -> tuple[str, int]:
    themes = payload.get("themes") or []
    if not isinstance(themes, list):
        return ("unknown", 0)
    n = len(themes)
    return (("ok" if n > 0 else "warn"), n)


def _check_news_articles(payload: dict[str, Any]) -> tuple[str, int]:
    """News intelligence: article_count_raw == 0 means RSS/news fetch returned
    nothing. Could be a market-closed weekend or a broken news producer.
    Returns "unknown" when the expected field is missing (malformed payload)."""
    if "article_count_raw" not in payload:
        return ("unknown", 0)
    n = int(payload.get("article_count_raw") or 0)
    return (("ok" if n > 0 else "warn"), n)


def _check_scraped_intel_degraded(payload: dict[str, Any]) -> tuple[str, int]:
    """Scraped intel: degraded_mode=True signals fallback path active. The
    `observed` value reports total evidence count so operators see whether
    the degraded run still produced anything useful. Returns "unknown" when
    the degraded_mode field is missing (malformed payload)."""
    if not payload.get("enabled", True):
        return ("ok", 0)
    if "degraded_mode" not in payload:
        return ("unknown", 0)
    si = payload.get("scraped_intel") or {}
    evidence = int(si.get("total_evidence") or 0)
    is_degraded = bool(payload.get("degraded_mode"))
    return (("warn" if is_degraded else "ok"), evidence)


def _check_ai_budget_events(payload: dict[str, Any]) -> tuple[str, int]:
    """AI budget: event_count == 0 means no AI calls were logged. Once theme
    engine or ai_decision_validator is wired to a remote LLM, we expect ≥1
    event per day. Zero events under normal operation indicates the budget
    tracker isn't being called from the LLM-using producers. Returns
    "unknown" when event_count field is missing (malformed payload)."""
    if not payload.get("enabled", True):
        return ("ok", 0)
    if "event_count" not in payload:
        return ("unknown", 0)
    n = int(payload.get("event_count") or 0)
    return (("ok" if n > 0 else "warn"), n)


def _check_pulse_last_run_age(payload: dict[str, Any]) -> tuple[str, int]:
    """Discovery pulse: warn if last successful run is older than 14 hours.
    The cron's longest *by-design* gap is the overnight window (weekday
    23:00->11:00 = 12h; weekend 20:00->12:00 = 16h, observed as ~13.25h at
    the 09:15 daily check), so a 14h SLA tolerates the legitimate overnight
    gap and only fires on a genuinely missed cycle. Reports age in minutes."""
    from datetime import datetime as _dt, timezone as _tz
    usage = payload.get("usage") or {}
    if usage.get("total_runs_month") in (None, 0):
        # No runs ever — could be brand new install. Return unknown rather
        # than warn so a fresh deploy doesn't false-alarm.
        return ("unknown", 0)
    # Need last_run_at from payload; status artifact carries it via state file
    # but payload itself includes generated_at as a proxy.
    last_run = payload.get("last_run_at") or payload.get("generated_at")
    try:
        last_dt = _dt.fromisoformat(last_run)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=_tz.utc)
        age_min = int((_dt.now(_tz.utc) - last_dt).total_seconds() // 60)
    except Exception:
        return ("unknown", 0)
    # 14h = 840min; tolerates the legitimate overnight gap (worst case the
    # weekend ~13.25h gap seen at the 09:15 check) and warns only on a real
    # missed cycle.
    if age_min > 840:
        return ("warn", age_min)
    return ("ok", age_min)


def _check_top100_daily(payload: dict[str, Any]) -> tuple[str, int]:
    """Universe sanitation: warn if top100_daily has zero candidates OR is
    missing the candidates list. Reports the candidate count."""
    cands = payload.get("candidates")
    if not isinstance(cands, list):
        return ("unknown", 0)
    n = len(cands)
    return (("ok" if n > 0 else "warn"), n)


def _check_historical_backfill(payload: dict[str, Any]) -> tuple[str, int]:
    """Historical backfill: weekend-only producer. Treats "no recent run"
    as `unknown` (acceptable on weekdays). Warns only when a recorded
    run actually errored on most of its universe — i.e., errored ≥
    fetched + skipped_fresh. Reports fetched count."""
    if "universe_size" not in payload:
        return ("unknown", 0)
    fetched = int(payload.get("fetched") or 0)
    errored = int(payload.get("errored") or 0)
    skipped_fresh = int(payload.get("skipped_fresh") or 0)
    progress = fetched + skipped_fresh
    if errored > 0 and errored >= max(progress, 1):
        return ("warn", fetched)
    return ("ok", fetched)


def _check_pulse_cap_status(payload: dict[str, Any]) -> tuple[str, int]:
    """Discovery pulse: warn if any monthly cap is at or above 90% utilization.
    Reports the highest cap utilization seen, in integer percent."""
    usage = payload.get("usage") or {}
    caps = payload.get("caps") or {}
    cost = float(usage.get("openai_cost_usd_month") or 0.0)
    fmp = int(usage.get("fmp_calls_month") or 0)
    cost_cap = float(caps.get("openai_cost_usd_max") or 0.0)
    fmp_cap = int(caps.get("fmp_calls_max") or 0)

    pcts: list[float] = []
    if cost_cap > 0:
        pcts.append(cost / cost_cap)
    if fmp_cap > 0:
        pcts.append(fmp / fmp_cap)

    max_pct_int = int(max(pcts) * 100) if pcts else 0
    if not pcts:
        return ("unknown", 0)
    if max_pct_int >= 90:
        return ("warn", max_pct_int)
    return ("ok", max_pct_int)


_CONTENT_LIVENESS_CHECKS: list[tuple[str, str, Any, str]] = [
    (
        "outputs/latest/theme_signals.json",
        "theme_signals.themes",
        _check_theme_signals,
        "Theme engine emitted zero themes — likely upstream LLM unreachable or "
        "no RSS headlines collected. Causes extended_watchlist to stay dormant.",
    ),
    (
        "outputs/latest/news_intelligence.json",
        "news_intelligence.article_count_raw",
        _check_news_articles,
        "News intelligence returned zero articles — RSS aggregator or FMP news "
        "feed may be failing. Degrades news_packets and ml_advisor evidence.",
    ),
    (
        "outputs/latest/scraped_intel_run_summary.json",
        "scraped_intel.degraded_mode",
        _check_scraped_intel_degraded,
        "Scraped-intel pipeline is running in degraded/fallback mode. Usually "
        "means top100_watchlist.json is stale; rebuild via weekly cron mode.",
    ),
    (
        "outputs/latest/ai_budget_summary.json",
        "ai_budget.event_count",
        _check_ai_budget_events,
        "AI budget tracker logged zero events today. Under normal operation, at "
        "least theme_engine should log one event. Zero events implies the "
        "LLM-using producers aren't calling check_ai_budget/record_ai_usage_event.",
    ),
    (
        "outputs/latest/discovery_pulse_status.json",
        "discovery_pulse.last_run_age",
        _check_pulse_last_run_age,
        "Discovery pulse hasn't run in > 6 hours. Expected cadence is 4h "
        "weekday / 8h weekend. Check crontab and /var/lock/stockbot-discovery-pulse.lock.",
    ),
    (
        "outputs/latest/discovery_pulse_status.json",
        "discovery_pulse.monthly_cap_status",
        _check_pulse_cap_status,
        "Discovery pulse monthly cap > 90% utilized. Approaching the trip-wire; "
        "remaining runs this month will skip when cap reaches 100%.",
    ),
    (
        "outputs/latest/top100_daily.json",
        "universe_sanitation.top100_daily",
        _check_top100_daily,
        "Universe sanitation produced an empty top100_daily — either no "
        "dynamic sources contributed tickers or the producer hit an error.",
    ),
    (
        "outputs/latest/historical_backfill_status.json",
        "historical_backfill.last_run",
        _check_historical_backfill,
        "Historical backfill's last run errored on most/all of its universe. "
        "Likely FMP budget exhausted or auth issue. Check logs/historical_backfill_*.log "
        "and verify FMP_API_KEY + raised fmp_daily_calls_budget.",
    ),
]

# Expected artifacts the official-lane run should produce. Used to flag
# missing-but-expected outputs in the status report. Tuple of
# (relative_path, label, must_be_today).
_EXPECTED_ARTIFACTS = [
    ("outputs/latest/decision_plan.json",                  "decision plan",                True),
    ("outputs/latest/decision_plan.md",                    "decision plan (md)",           True),
    ("outputs/latest/system_decision_summary.json",        "system decision summary",      True),
    ("outputs/latest/daily_memo.md",                       "daily memo (md)",              True),
    ("outputs/latest/daily_memo.txt",                      "daily memo (txt)",             True),
    ("outputs/latest/news_intelligence.json",              "news intelligence",            True),
    ("outputs/latest/risk_delta.json",                     "risk delta panel",             True),
    ("outputs/portfolio/portfolio_snapshot.json",          "portfolio snapshot",           True),
    # The following are advisory and may be absent until their gate passes —
    # tracked so operators see drift, but a miss does not move overall_status.
    ("outputs/performance/approved_ranking_config.json",   "approved ranking config",      False),
    ("outputs/performance/approved_allocation_policy.json","approved allocation policy",   False),
    ("outputs/latest/theme_opportunities.json",            "theme opportunities",          False),
]

# Stage section marker in the daily_safe log.
_STAGE_HEADER_RE = re.compile(r"^==\s+(?P<name>.+?)\s+==$")
_OK_LINE_RE = re.compile(r"^(?P<name>.+?):\s+OK$")
_WARN_LINE_RE = re.compile(r"^(?P<name>.+?):\s+WARN\s+\(non-blocking;\s+exit\s+(?P<rc>\d+)\)$")


def _load_json_safe(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Stage scanning
# ---------------------------------------------------------------------------


def scan_log_stages(log_path: Path) -> list[dict[str, Any]]:
    """
    Parse a daily_safe_YYYY-MM-DD.log file into a stage timeline.

    Each stage entry has:
      {name, status, output_lines_count, exit_code (if WARN/FAIL)}

    "Preflight" subsections (Repo Root, Virtual Environment, etc.) are
    grouped under a single "Preflight" entry to avoid noise. Recognized
    statuses: "ok", "warn", "unknown" (no trailing OK/WARN line found).
    """
    if not log_path.exists():
        return []

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    lines = text.splitlines()
    stages: list[dict[str, Any]] = []
    in_preflight = False

    for i, raw in enumerate(lines):
        m = _STAGE_HEADER_RE.match(raw.strip())
        if not m:
            continue
        name = m.group("name").strip()

        # Group the preflight subsections under one entry to reduce noise.
        preflight_kids = {
            "Repo Root", "Virtual Environment", "Required Files", "Environment",
            "Env Var Registry", "Artifact Shape Smoke Test", "FMP Compliance",
            "FMP Tests", "Compile Check", "Summary",
        }
        if name == "Preflight":
            in_preflight = True
            stages.append({"name": "Preflight", "status": "unknown", "output_lines_count": 0})
            continue
        if name in preflight_kids and in_preflight:
            # Stay grouped — but increment line count.
            if stages and stages[-1]["name"] == "Preflight":
                stages[-1]["output_lines_count"] += 1
            continue
        # Leave preflight grouping once we hit a non-preflight section.
        if name not in preflight_kids:
            in_preflight = False

        stages.append({"name": name, "status": "unknown", "output_lines_count": 0})

    # Re-walk the log to assign status to non-preflight stages by looking
    # for a "<name>: OK" or "<name>: WARN" line within ~30 lines of each header.
    name_to_status: dict[str, dict[str, Any]] = {}
    for raw in lines:
        stripped = raw.strip()
        ok = _OK_LINE_RE.match(stripped)
        if ok:
            n = ok.group("name").strip()
            name_to_status[n] = {"status": "ok"}
            continue
        warn = _WARN_LINE_RE.match(stripped)
        if warn:
            n = warn.group("name").strip()
            name_to_status[n] = {"status": "warn", "exit_code": int(warn.group("rc"))}

    # Mark "Preflight" ok when we observed a "PASS: Preflight completed" line.
    if "PASS: Preflight completed successfully" in text:
        name_to_status["Preflight"] = {"status": "ok"}
    elif "Preflight" in (s["name"] for s in stages):
        # Otherwise infer from "DAILY RUN PASSED" presence.
        if "DAILY RUN PASSED" in text:
            name_to_status.setdefault("Preflight", {"status": "ok"})

    # The "Daily Pipeline" stage itself doesn't print "Daily Pipeline: OK" —
    # it's the fail-fast first stage. Infer ok from "DAILY RUN PASSED".
    if "DAILY RUN PASSED" in text:
        name_to_status.setdefault("Daily Pipeline", {"status": "ok"})
    elif "DAILY RUN FAILED" in text:
        name_to_status.setdefault("Daily Pipeline", {"status": "failed"})

    for s in stages:
        ss = name_to_status.get(s["name"])
        if ss:
            s.update(ss)
    return stages


# ---------------------------------------------------------------------------
# Artifact scanning
# ---------------------------------------------------------------------------


def scan_expected_artifacts(root: Path) -> list[dict[str, Any]]:
    """
    For each expected artifact path, return {path, label, exists, mtime_iso,
    fresh_today, required}.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    results: list[dict[str, Any]] = []
    for rel_path, label, required in _EXPECTED_ARTIFACTS:
        p = root / rel_path
        row: dict[str, Any] = {
            "path": rel_path,
            "label": label,
            "required": required,
            "exists": p.exists(),
        }
        if p.exists():
            try:
                mtime = p.stat().st_mtime
                mtime_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
                row["mtime_iso"] = mtime_iso
                row["fresh_today"] = mtime_iso[:10] == today
            except Exception:
                row["mtime_iso"] = None
                row["fresh_today"] = False
        else:
            row["mtime_iso"] = None
            row["fresh_today"] = False
        results.append(row)
    return results


# ---------------------------------------------------------------------------
# Content liveness
# ---------------------------------------------------------------------------


def scan_content_liveness(root: Path) -> list[dict[str, Any]]:
    """Per-artifact content checks beyond presence/freshness.

    Each result row: {name, path, status, observed, rationale}. status is
    "ok" (content present), "warn" (file exists but content empty/degraded),
    or "unknown" (file missing or parse failed). A "warn" here escalates
    overall_status from "ok" to "ok_with_warnings".
    """
    results: list[dict[str, Any]] = []
    for rel_path, name, predicate, rationale in _CONTENT_LIVENESS_CHECKS:
        p = root / rel_path
        row: dict[str, Any] = {
            "name": name,
            "path": rel_path,
            "status": "unknown",
            "observed": 0,
            "rationale": rationale,
        }
        if not p.exists():
            row["reason"] = "artifact_missing"
            results.append(row)
            continue
        payload = _load_json_safe(p)
        if payload is None:
            row["reason"] = "parse_failed"
            results.append(row)
            continue
        try:
            status, observed = predicate(payload)
        except Exception as exc:
            row["reason"] = f"predicate_error:{type(exc).__name__}"
            results.append(row)
            continue
        row["status"] = status
        row["observed"] = observed
        results.append(row)
    return results


# ---------------------------------------------------------------------------
# Build artifact
# ---------------------------------------------------------------------------


def build_daily_run_status(
    *,
    root: str | Path = ".",
    log_path: str | Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Compose the artifact payload (no file writes)."""
    ts = generated_at or datetime.now(timezone.utc).isoformat()
    root_path = Path(root).resolve()

    if log_path is None:
        today = datetime.now(timezone.utc).date().isoformat()
        log_path = root_path / "logs" / f"daily_safe_{today}.log"
    else:
        log_path = Path(log_path)

    stages = scan_log_stages(log_path)
    artifacts = scan_expected_artifacts(root_path)
    content_liveness = scan_content_liveness(root_path)

    stage_count = len(stages)
    ok_count = sum(1 for s in stages if s["status"] == "ok")
    warn_count = sum(1 for s in stages if s["status"] == "warn")
    failed_count = sum(1 for s in stages if s["status"] == "failed")

    required_missing = [
        a for a in artifacts
        if a["required"] and (not a["exists"] or not a["fresh_today"])
    ]
    optional_missing = [
        a for a in artifacts
        if not a["required"] and (not a["exists"] or not a["fresh_today"])
    ]

    content_warn_count = sum(1 for c in content_liveness if c["status"] == "warn")

    if failed_count > 0 or required_missing:
        overall_status = "failed" if failed_count else "partial"
    elif warn_count > 0 or content_warn_count > 0:
        overall_status = "ok_with_warnings"
    elif stage_count == 0:
        overall_status = "no_log"
    else:
        overall_status = "ok"

    return {
        "generated_at": ts,
        "observe_only": _OBSERVE_ONLY,
        "schema_version": _SCHEMA_VERSION,
        "source": _SOURCE_LABEL,
        "overall_status": overall_status,
        "log_path": str(log_path),
        "stage_summary": {
            "total": stage_count,
            "ok": ok_count,
            "warn": warn_count,
            "failed": failed_count,
        },
        "stages": stages,
        "artifacts": artifacts,
        "content_liveness": content_liveness,
        "content_warn_count": content_warn_count,
        "required_missing_count": len(required_missing),
        "optional_missing_count": len(optional_missing),
        "disclaimer": _DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _status_glyph(s: str) -> str:
    return {
        "ok":         "✓",
        "warn":       "!",
        "failed":     "✗",
        "unknown":    "?",
    }.get(s, "?")


def render_daily_run_status_md(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append

    a(f"# Daily Run Status — {payload.get('generated_at', '')[:10]}")
    a("")
    a(f"**Generated:** {payload.get('generated_at', '')}  ")
    a(f"**Overall status:** `{payload.get('overall_status', 'unknown')}`  ")
    a(f"**Log:** `{payload.get('log_path', '')}`")
    a("")
    a(f"> {payload.get('disclaimer', _DISCLAIMER)}")
    a("")

    summary = payload.get("stage_summary") or {}
    a(
        f"**Stages:** {summary.get('total', 0)} total — "
        f"{summary.get('ok', 0)} ok · {summary.get('warn', 0)} warn · "
        f"{summary.get('failed', 0)} failed"
    )
    a("")

    stages = payload.get("stages") or []
    if stages:
        a("## Stage Timeline")
        a("")
        for s in stages:
            glyph = _status_glyph(s.get("status", "unknown"))
            extra = ""
            if s.get("exit_code") is not None:
                extra = f" (exit {s['exit_code']})"
            a(f"- {glyph} `{s.get('name')}` — {s.get('status')}{extra}")
        a("")

    artifacts = payload.get("artifacts") or []
    if artifacts:
        a(f"## Expected Artifacts ({payload.get('required_missing_count', 0)} required missing, "
          f"{payload.get('optional_missing_count', 0)} optional missing)")
        a("")
        for art in artifacts:
            status_tag = "fresh" if art.get("fresh_today") else (
                "stale" if art.get("exists") else "MISSING"
            )
            req_tag = "[required]" if art.get("required") else "[optional]"
            mtime = art.get("mtime_iso") or "—"
            a(
                f"- `{art.get('path')}` — {status_tag} {req_tag} "
                f"(mtime: `{mtime}`)"
            )
        a("")

    liveness = payload.get("content_liveness") or []
    if liveness:
        warn_n = payload.get("content_warn_count", 0)
        a(f"## Content Liveness ({warn_n} warn)")
        a("")
        for c in liveness:
            glyph = _status_glyph(c.get("status", "unknown"))
            observed = c.get("observed", 0)
            a(f"- {glyph} `{c.get('name')}` — {c.get('status')} (observed={observed})")
            if c.get("status") == "warn":
                a(f"    > {c.get('rationale', '')}")
        a("")

    a("---")
    a("_Observe-only ops telemetry._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def run_daily_run_status(
    *,
    root: str | Path = ".",
    log_path: str | Path | None = None,
    write_files: bool = True,
) -> dict[str, Any]:
    """Compose payload, write artifacts."""
    root_path = Path(root).resolve()
    try:
        payload = build_daily_run_status(root=root_path, log_path=log_path)
        artifacts: dict[str, str] = {}
        if write_files:
            md = render_daily_run_status_md(payload)
            json_path = safe_write_json(
                OutputNamespace.LATEST,
                "daily_run_status.json",
                payload,
                base_dir=root_path / "outputs",
            )
            md_path = safe_write_text(
                OutputNamespace.LATEST,
                "daily_run_status.md",
                md,
                base_dir=root_path / "outputs",
            )
            artifacts = {
                "daily_run_status_json": str(json_path),
                "daily_run_status_md": str(md_path),
            }
        return {
            "status": "ok",
            "overall_status": payload.get("overall_status"),
            "required_missing_count": payload.get("required_missing_count"),
            "artifacts": artifacts,
        }
    except Exception as exc:
        logger.error("daily_run_status failed: %s", exc, exc_info=True)
        return {"status": "error", "error": str(exc)}


if __name__ == "__main__":
    import sys
    r = run_daily_run_status(root=Path(__file__).resolve().parents[1])
    print(
        f"daily_run_status: status={r.get('status')}"
        f" overall={r.get('overall_status')}"
        f" missing_required={r.get('required_missing_count')}"
    )
    sys.exit(0)
