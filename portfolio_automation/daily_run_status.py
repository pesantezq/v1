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

    if failed_count > 0 or required_missing:
        overall_status = "failed" if failed_count else "partial"
    elif warn_count > 0:
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
