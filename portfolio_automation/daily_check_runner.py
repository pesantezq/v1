"""
Daily-check deterministic runner (cron-safe).

Pure-Python implementation of the triage portion of the
`/daily-portfolio-check` slash command. Reads today's observe-only
artifacts under `outputs/latest/`, applies the GREEN/AMBER/RED rules
documented in `.claude/commands/daily-portfolio-check.md`, and writes a
markdown report to `daily_checks/YYYY-MM-DD.md`.

Why this exists:
  The slash command itself can only run inside Claude Code (which has
  the LLM context for agent dispatch). Cron on the VPS cannot reliably
  invoke `claude --print` because of auth-context issues. This module
  reproduces the deterministic triage so the cron still emits a useful
  daily report without any LLM dependency. When the verdict is RED, the
  wrapper script may *optionally* invoke `claude --print` afterwards to
  attach analyst dispatch + config proposals as an appendix; that part
  is best-effort and out of scope for this module.

Hard guarantees:
  - Read-only over `outputs/latest/*`. Never mutates pipeline artifacts.
  - Writes only `daily_checks/YYYY-MM-DD.md` and
    `data/daily_check_state.json` (the state file the slash command
    itself owns; this module follows the same schema).
  - Degrades gracefully when artifacts are missing or malformed —
    a missing artifact downgrades the verdict, never crashes the run.
  - No protected-semantics surface is touched.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("stockbot.portfolio_automation.daily_check_runner")

_OUTPUTS_LATEST = "outputs/latest"
_STATE_FILE_REL = "data/daily_check_state.json"
_REPORT_DIR_REL = "daily_checks"
_GAUGE_VERSIONS_REL = "data/gauge_versions.jsonl"

_SAMPLE_THRESHOLDS = (10, 30, 50, 100)


@dataclass
class DailyCheckResult:
    verdict: str  # "GREEN" | "AMBER" | "RED"
    headline: str
    body_lines: list[str]
    red_action: str | None = None
    agent_dispatch: list[str] = field(default_factory=list)
    newly_crossed_thresholds: list[str] = field(default_factory=list)
    current_fingerprint: str | None = None
    current_fp_resolved_1d: int = 0
    pre_tracker_hit_rate_1d: float | None = None
    fingerprint_changed: bool = False
    failures: list[str] = field(default_factory=list)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("failed to read %s: %s", path, exc)
        return None


def _load_state(root: Path) -> dict[str, Any]:
    path = root / _STATE_FILE_REL
    state = _read_json(path)
    if not isinstance(state, dict):
        return {
            "last_run_at": "",
            "last_fingerprint": "",
            "last_current_fp_resolved_1d": 0,
            "last_pre_tracker_hit_rate_1d": None,
            "thresholds_crossed": [],
        }
    return state


def _write_state(root: Path, state: dict[str, Any]) -> None:
    path = root / _STATE_FILE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _load_gauge_versions_tail(root: Path) -> dict | None:
    path = root / _GAUGE_VERSIONS_REL
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _days_between(start: datetime | None, end: datetime) -> int | None:
    if start is None:
        return None
    return max(0, (end.date() - start.date()).days)


def _newly_crossed(resolved_1d: int, already: list[str]) -> list[str]:
    out = []
    for threshold in _SAMPLE_THRESHOLDS:
        marker = f"n_{threshold}"
        if resolved_1d >= threshold and marker not in already:
            out.append(marker)
    return out


def _triage(
    *,
    status: dict | None,
    risk: dict | None,
    budget: dict | None,
    due: dict | None,
    delta_hit_rate_pp: float | None,
    current_fp_resolved_1d: int,
    fingerprint_age_days: int | None,
    fingerprint_changed_unexpectedly: bool,
) -> tuple[str, str, str | None]:
    """Return (verdict, primary_fault_label, red_action_template or None)."""
    overall = (status or {}).get("overall_status", "missing")
    required_missing = int((status or {}).get("required_missing_count", 1))
    stuck = int((due or {}).get("stuck_count", 0))
    budget_status = ((budget or {}).get("budget", {}) or {}).get("status", "missing")
    risk_status = (risk or {}).get("overall_status", "missing")

    # ---- RED conditions (priority order from the skill) ----
    if stuck > 0:
        top_stuck = ""
        rows = (due or {}).get("by_ticker") or []
        if rows and isinstance(rows[0], dict):
            top_stuck = rows[0].get("symbol") or rows[0].get("ticker") or ""
        action = (
            f"Resolver lag on {top_stuck or 'unknown ticker'} — run "
            "`python -m portfolio_automation.resolution_due_probe` manually; "
            "investigate FMP cache TTL."
        )
        return "RED", f"{stuck} signal(s) stuck unresolved", action

    if budget_status == "exhausted":
        count = ((budget or {}).get("budget", {}) or {}).get("count_today", "?")
        cap = ((budget or {}).get("budget", {}) or {}).get("budget", "?")
        action = (
            f"FMP daily budget exhausted at {count}/{cap} — news intel may be "
            "degraded; consider raising `fmp_daily_calls_budget` or staggering "
            "producer calls."
        )
        return "RED", f"FMP budget exhausted ({count}/{cap})", action

    if risk_status == "breach":
        conc = (risk or {}).get("concentration") or {}
        top = conc.get("top_position") or {}
        sym = top.get("symbol", "?")
        weight_pct = round(float(top.get("weight", 0)) * 100, 1)
        cap_pct = round(float(top.get("cap", 0)) * 100, 1)
        action = (
            f"Concentration breach on {sym} ({weight_pct}% > cap {cap_pct}%); "
            "structural-cap trim signal active in decision_plan."
        )
        return "RED", f"Concentration breach on {sym}", action

    if (
        delta_hit_rate_pp is not None
        and delta_hit_rate_pp <= -10
        and current_fp_resolved_1d >= 30
    ):
        action = (
            f"Current-fp underperforming pre-tracker by {delta_hit_rate_pp:.1f}pp "
            f"on n={current_fp_resolved_1d}; consider reverting most-aggressive "
            "knob first and re-check in 14 days."
        )
        return "RED", f"Retune underperforming ({delta_hit_rate_pp:.1f}pp)", action

    if (
        delta_hit_rate_pp is not None
        and delta_hit_rate_pp >= 10
        and current_fp_resolved_1d >= 30
    ):
        action = (
            f"Current-fp outperforming pre-tracker by {delta_hit_rate_pp:.1f}pp "
            f"on n={current_fp_resolved_1d}; retune validated. Consider whether "
            "to advance to next gauge candidate."
        )
        return "RED", f"Retune validated (+{delta_hit_rate_pp:.1f}pp)", action

    if overall == "failed":
        action = "Pipeline failed — check logs/daily_safe_<date>.log for the stage that errored."
        return "RED", "Pipeline failed", action

    if (
        current_fp_resolved_1d == 0
        and fingerprint_age_days is not None
        and fingerprint_age_days >= 2
    ):
        action = (
            "Resolver not picking up current-fp data — check FMP cache TTL and "
            "verify cron at 09:01 produced today's signal_outcomes.csv."
        )
        return "RED", "Attribution lag (fingerprint stale)", action

    if overall in ("missing", "partial"):
        return "RED", f"overall_status={overall}", "Pipeline incomplete — investigate manually."

    # ---- AMBER conditions ----
    amber_reasons = []
    if budget_status == "near_cap":
        amber_reasons.append("FMP near cap")
    if risk_status == "near_cap":
        amber_reasons.append(f"{((risk or {}).get('concentration') or {}).get('top_position', {}).get('symbol', '?')} near concentration cap")
    if (
        fingerprint_age_days is not None
        and fingerprint_age_days >= 2
        and current_fp_resolved_1d == 0
    ):
        amber_reasons.append("attribution lag")
    if required_missing > 0:
        amber_reasons.append(f"{required_missing} required artifact(s) missing")
    if fingerprint_changed_unexpectedly:
        amber_reasons.append("unexpected fingerprint change")

    if amber_reasons:
        return "AMBER", "; ".join(amber_reasons), None

    # ---- GREEN ----
    return "GREEN", "all checks nominal", None


def _agent_dispatch_signals(
    *,
    status: dict | None,
    due: dict | None,
    newly_crossed: list[str],
    fingerprint_changed: bool,
    delta_hit_rate_pp: float | None,
    current_fp_resolved_1d: int,
    current_fp_age_days: int | None,
) -> list[str]:
    """Match the dispatch rules in /daily-portfolio-check Step 3."""
    out = []

    overall = (status or {}).get("overall_status")
    required_missing = int((status or {}).get("required_missing_count", 0))
    stuck = int((due or {}).get("stuck_count", 0))
    if (
        overall != "ok"
        or required_missing > 0
        or stuck > 0
        or (current_fp_resolved_1d == 0 and (current_fp_age_days or 0) >= 2)
    ):
        out.append("portfolio-resolver-investigator")

    if (
        newly_crossed
        or fingerprint_changed
        or (
            delta_hit_rate_pp is not None
            and abs(delta_hit_rate_pp) >= 10
            and current_fp_resolved_1d >= 30
        )
    ):
        out.append("portfolio-attribution-analyst")

    # render-reviewer needs `git log` inspection — we leave that to the wrapper
    # to decide. Skipping here keeps the deterministic module side-effect-free.
    return out


def _format_attribution_line(by_fp: dict, current_fp: str | None, pre_tracker_label: str) -> str:
    cur = by_fp.get(current_fp or "", {}) if isinstance(by_fp, dict) else {}
    pre = by_fp.get(pre_tracker_label, {}) if isinstance(by_fp, dict) else {}
    cur_n = int(cur.get("resolved_1d", 0))
    cur_h = float(cur.get("hit_rate_1d") or 0.0) * 100
    pre_n = int(pre.get("resolved_1d", 0))
    pre_h = float(pre.get("hit_rate_1d") or 0.0) * 100
    delta = cur_h - pre_h
    sign = "+" if delta >= 0 else ""
    return (
        f"Attribution: current-fp n={cur_n} at {cur_h:.1f}% / "
        f"pre-tracker n={pre_n} at {pre_h:.1f}% · Δ {sign}{delta:.1f}pp"
    )


def _format_risk_line(risk: dict | None) -> str:
    if not risk:
        return "Risk: (panel unavailable)"
    conc = risk.get("concentration") or {}
    top = conc.get("top_position") or {}
    sym = top.get("symbol", "?")
    w = round(float(top.get("weight", 0)) * 100, 1)
    cap = round(float(top.get("cap", 0)) * 100, 1)
    headroom = round(float(top.get("headroom", 0)) * 100, 1)
    lev = risk.get("leverage") or {}
    lev_pct = round(float(lev.get("total_exposure", 0)) * 100, 1)
    return f"Risk: {sym} {w}% (cap {cap}%, +{headroom}pp); leverage {lev_pct}%"


def run_daily_check(root: Path | str, now: datetime | None = None) -> DailyCheckResult:
    """Execute the deterministic daily-check triage.

    Returns a DailyCheckResult. Side effects: updates
    `data/daily_check_state.json`. The caller is responsible for writing
    the formatted report to disk.
    """
    root = Path(root)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    state = _load_state(root)
    latest = root / _OUTPUTS_LATEST

    status = _read_json(latest / "daily_run_status.json")
    risk = _read_json(latest / "risk_delta.json")
    retune = _read_json(latest / "retune_impact.json")
    budget = _read_json(latest / "fmp_budget_status.json")
    due = _read_json(latest / "decisions_due_for_resolution.json")
    gauge_tail = _load_gauge_versions_tail(root)

    failures = []
    for label, artifact in [
        ("daily_run_status", status),
        ("risk_delta", risk),
        ("retune_impact", retune),
        ("fmp_budget_status", budget),
        ("decisions_due_for_resolution", due),
    ]:
        if artifact is None:
            failures.append(f"{label}.json missing or unreadable")

    # If all required artifacts are missing → cron didn't run.
    if status is None and risk is None and retune is None and budget is None:
        return DailyCheckResult(
            verdict="RED",
            headline="cron did not run today",
            body_lines=[
                "All required artifacts under outputs/latest/ are missing or unreadable.",
                "Likely the production cron at 09:00 UTC did not complete.",
                "Investigate: ls -la outputs/latest/ and tail logs/daily_safe_*.log",
            ],
            red_action="Pipeline did not run — investigate production cron",
            failures=failures,
        )

    by_fp = ((retune or {}).get("outcome_attribution") or {}).get("by_fingerprint") or {}
    current_fp = (retune or {}).get("current_fingerprint")
    pre_tracker_label = ((retune or {}).get("outcome_attribution") or {}).get(
        "pre_tracker_label", "pre_tracker_unknown"
    )

    current_fp_data = by_fp.get(current_fp or "", {})
    current_fp_resolved_1d = int(current_fp_data.get("resolved_1d", 0))
    current_fp_hit_rate = current_fp_data.get("hit_rate_1d")
    pre_tracker_data = by_fp.get(pre_tracker_label, {})
    pre_tracker_hit_rate = pre_tracker_data.get("hit_rate_1d")

    delta_hit_rate_pp = None
    if (
        isinstance(current_fp_hit_rate, (int, float))
        and isinstance(pre_tracker_hit_rate, (int, float))
    ):
        delta_hit_rate_pp = (float(current_fp_hit_rate) - float(pre_tracker_hit_rate)) * 100

    fingerprint_age_days = None
    if gauge_tail and gauge_tail.get("fingerprint") == current_fp:
        first_seen = _parse_iso(gauge_tail.get("first_seen_at"))
        fingerprint_age_days = _days_between(first_seen, now)

    fingerprint_changed = bool(state.get("last_fingerprint")) and (
        state.get("last_fingerprint") != current_fp
    )
    # An unexpected change is one where the user wasn't told to expect a new
    # fingerprint — we can't tell that here, so we treat any change as expected
    # for triage purposes. (The slash command interprets this similarly.)
    fingerprint_changed_unexpectedly = False

    existing_thresholds = list(state.get("thresholds_crossed") or [])
    if fingerprint_changed:
        existing_thresholds = []
    newly_crossed = _newly_crossed(current_fp_resolved_1d, existing_thresholds)

    verdict, fault_label, red_action = _triage(
        status=status,
        risk=risk,
        budget=budget,
        due=due,
        delta_hit_rate_pp=delta_hit_rate_pp,
        current_fp_resolved_1d=current_fp_resolved_1d,
        fingerprint_age_days=fingerprint_age_days,
        fingerprint_changed_unexpectedly=fingerprint_changed_unexpectedly,
    )

    dispatch = _agent_dispatch_signals(
        status=status,
        due=due,
        newly_crossed=newly_crossed,
        fingerprint_changed=fingerprint_changed,
        delta_hit_rate_pp=delta_hit_rate_pp,
        current_fp_resolved_1d=current_fp_resolved_1d,
        current_fp_age_days=fingerprint_age_days,
    )

    # ---- Headline grammar ----
    if verdict == "GREEN":
        stage_summary = (status or {}).get("stage_summary") or {}
        ok_stages = stage_summary.get("ok", "?")
        budget_count = ((budget or {}).get("budget", {}) or {}).get("count_today", "?")
        budget_cap = ((budget or {}).get("budget", {}) or {}).get("budget", "?")
        d_str = f"{delta_hit_rate_pp:+.1f}" if delta_hit_rate_pp is not None else "?"
        h_str = (
            f"{float(current_fp_hit_rate) * 100:.1f}%"
            if isinstance(current_fp_hit_rate, (int, float))
            else "?"
        )
        headline = (
            f"{ok_stages} stages OK · retune n={current_fp_resolved_1d} at {h_str} "
            f"(Δ {d_str}pp vs baseline) · FMP {budget_count}/{budget_cap}"
        )
    elif verdict == "AMBER":
        headline = f"WARN — {fault_label}; others nominal"
    else:
        headline = f"ALERT — {fault_label}; action: {red_action or 'see body'}"

    # ---- Body lines ----
    body = []
    body.append(_format_attribution_line(by_fp, current_fp, pre_tracker_label))
    body.append(_format_risk_line(risk))
    if dispatch:
        body.append(
            "Agent dispatch signals (would fire in LLM-driven run): "
            + ", ".join(dispatch)
        )
    if verdict == "RED" and red_action:
        body.append(f"RED action: {red_action}")
    if verdict == "GREEN":
        body.append("No action required.")
    if failures:
        body.append(f"Degraded reads: {'; '.join(failures)}")
    if fingerprint_changed:
        body.append(
            f"Fingerprint changed: {state.get('last_fingerprint') or '∅'} → {current_fp}"
        )

    # ---- State write-back ----
    if fingerprint_changed:
        new_thresholds = list(newly_crossed)
    else:
        new_thresholds = sorted(set(existing_thresholds) | set(newly_crossed))

    state.update(
        {
            "last_run_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_fingerprint": current_fp or "",
            "last_current_fp_resolved_1d": current_fp_resolved_1d,
            "last_pre_tracker_hit_rate_1d": (
                float(pre_tracker_hit_rate)
                if isinstance(pre_tracker_hit_rate, (int, float))
                else None
            ),
            "thresholds_crossed": new_thresholds,
        }
    )
    _write_state(root, state)

    return DailyCheckResult(
        verdict=verdict,
        headline=headline,
        body_lines=body,
        red_action=red_action,
        agent_dispatch=dispatch,
        newly_crossed_thresholds=newly_crossed,
        current_fingerprint=current_fp,
        current_fp_resolved_1d=current_fp_resolved_1d,
        pre_tracker_hit_rate_1d=(
            float(pre_tracker_hit_rate)
            if isinstance(pre_tracker_hit_rate, (int, float))
            else None
        ),
        fingerprint_changed=fingerprint_changed,
        failures=failures,
    )


def format_report_markdown(result: DailyCheckResult, date_utc: str) -> str:
    """Render the heartbeat + body as the daily_checks/YYYY-MM-DD.md content."""
    lines = [
        f"[{result.verdict}] daily check {date_utc}: {result.headline}",
        "",
    ]
    for line in result.body_lines:
        lines.append(line)
    lines.append("")
    lines.append(
        "_Generated by `portfolio_automation.daily_check_runner` "
        "(deterministic, no LLM)._"
    )
    return "\n".join(lines) + "\n"


def write_report(root: Path, result: DailyCheckResult, date_utc: str) -> Path:
    report_dir = root / _REPORT_DIR_REL
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{date_utc}.md"
    report_path.write_text(format_report_markdown(result, date_utc), encoding="utf-8")
    return report_path


def main(root: Path | str | None = None) -> int:
    repo_root = Path(root or Path.cwd())
    now = datetime.now(timezone.utc)
    date_utc = now.strftime("%Y-%m-%d")
    result = run_daily_check(repo_root, now=now)
    path = write_report(repo_root, result, date_utc)
    print(f"[{result.verdict}] {date_utc} — {result.headline}")
    print(f"report: {path}")
    return 0 if result.verdict != "RED" else 2


if __name__ == "__main__":
    import sys

    sys.exit(main())
