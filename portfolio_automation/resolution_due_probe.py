"""
Decisions-Due-For-Resolution Probe — observe-only data-maturation accelerator.

The decision outcome system stamps signal_outcomes.csv rows with
outcome_return_Nd for N in (1, 3, 7) once N trading days pass and a
follow-up price snapshot lands. Sometimes a signal's window has elapsed
but no outcome was ever attributed — typically because the follow-up
price wasn't fetched, the ticker dropped from the watchlist, or a job
silently skipped that row.

When this happens, ml_advisor and Kelly clocks stall: both gate their
emissions on resolved-decision counts. Each stuck signal is a free
"unit of progress" the system is leaving on the table.

This probe surfaces those stuck rows so an operator (or a future
follow-up resolver) can investigate. v1 responsibilities:

  1. Read signal_outcomes.csv.
  2. For each row, compute calendar age since signal_time.
  3. Flag any row where calendar age exceeds 2x the resolution window
     (1d -> 2 cal days, 3d -> 6 cal days, 7d -> 14 cal days) AND the
     corresponding outcome_return_Nd is null.
  4. Group by (ticker, decision-window) and write a summary so the
     operator can scan for patterns (same ticker repeatedly stuck).

The 2x multiplier on calendar days converts the trading-day windows
into a forgiving wall-clock threshold so weekends/holidays don't
generate false positives.

Hard guarantees:
  - observe_only=True hardcoded.
  - Read-only over signal_outcomes.csv. No DB writes, no decision/score
    mutation.
  - Degrades to status="insufficient_data" when the CSV is missing.

Public API:
  scan_unresolved(rows, now) -> list[dict]
  build_resolution_due(root, now) -> dict
  run_resolution_due_probe(root, write_files) -> dict
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger("stockbot.portfolio_automation.resolution_due_probe")

_SCHEMA_VERSION = "1"
_SOURCE_LABEL = "resolution_due_probe"
_OBSERVE_ONLY = True

# How forgiving to be on calendar age before flagging an N-day window as stuck.
# 2.0 means a 1-trading-day window only fires after 2 calendar days have passed.
_CAL_DAY_MULTIPLIER = 2.0

# Windows tracked (must match outcome_return_Nd columns in signal_outcomes.csv).
_WINDOWS = (1, 3, 7)

_SIGNAL_OUTCOMES_REL = ("outputs", "performance", "signal_outcomes.csv")
_OUTPUT_JSON_REL = "decisions_due_for_resolution.json"
_OUTPUT_MD_REL = "decisions_due_for_resolution.md"

_DISCLAIMER = (
    "Observe-only data-maturation probe. Surfaces signal_outcomes rows whose "
    "resolution window has elapsed but whose outcome_return_Nd is null. Does "
    "not call APIs, does not mutate any decision, score, or outcome state."
)


def _parse_signal_time(s: str) -> datetime | None:
    """Parse the CSV's signal_time (timezone-naive ISO) into a naive UTC datetime."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _is_null_outcome(v: Any) -> bool:
    """True when the outcome_return_Nd cell is empty/null in the CSV."""
    if v is None:
        return True
    s = str(v).strip()
    return s in ("", "—", "none", "None", "null")


def scan_unresolved(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """
    Return flagged rows. Each entry: {ticker, signal_time, window_days,
    age_calendar_days, expected_calendar_days, gap_calendar_days, source}.
    """
    if not rows:
        return []
    ref_now = (now or datetime.now(timezone.utc))
    if ref_now.tzinfo is not None:
        ref_now = ref_now.astimezone(timezone.utc).replace(tzinfo=None)

    flagged: list[dict[str, Any]] = []
    for r in rows:
        sig_time = _parse_signal_time(r.get("signal_time") or "")
        if sig_time is None:
            continue
        age = (ref_now - sig_time).total_seconds() / 86400.0  # calendar days
        for w in _WINDOWS:
            expected_cal_days = w * _CAL_DAY_MULTIPLIER
            if age < expected_cal_days:
                continue  # window not elapsed yet, give it more time
            cell = r.get(f"outcome_return_{w}d")
            if not _is_null_outcome(cell):
                continue  # already resolved, no probe action needed
            flagged.append({
                "ticker": str(r.get("ticker") or "—"),
                "signal_time": r.get("signal_time"),
                "window_days": w,
                "age_calendar_days": round(age, 2),
                "expected_calendar_days": round(expected_cal_days, 2),
                "gap_calendar_days": round(age - expected_cal_days, 2),
                "watchlist_source": r.get("watchlist_source") or "",
                "signal_score": r.get("signal_score") or "",
                "conviction_band": r.get("conviction_band") or "",
            })
    return flagged


def _group_by_ticker(flagged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate flagged rows by ticker for operator scan."""
    by_ticker: dict[str, dict[str, Any]] = {}
    for row in flagged:
        t = row["ticker"]
        b = by_ticker.setdefault(t, {
            "ticker": t,
            "stuck_signals": 0,
            "windows_stuck": set(),
            "max_gap_days": 0.0,
        })
        b["stuck_signals"] += 1
        b["windows_stuck"].add(row["window_days"])
        b["max_gap_days"] = max(b["max_gap_days"], row["gap_calendar_days"])
    out: list[dict[str, Any]] = []
    for t, b in by_ticker.items():
        out.append({
            "ticker": t,
            "stuck_signals": b["stuck_signals"],
            "windows_stuck": sorted(b["windows_stuck"]),
            "max_gap_days": round(b["max_gap_days"], 2),
        })
    out.sort(key=lambda r: (-r["stuck_signals"], -r["max_gap_days"]))
    return out


def build_resolution_due(
    *,
    root: str | Path = ".",
    now: datetime | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Compose the artifact payload (no file writes)."""
    ts = generated_at or datetime.now(timezone.utc).isoformat()
    root_path = Path(root).resolve()
    csv_path = root_path.joinpath(*_SIGNAL_OUTCOMES_REL)

    base: dict[str, Any] = {
        "generated_at": ts,
        "observe_only": _OBSERVE_ONLY,
        "schema_version": _SCHEMA_VERSION,
        "source": _SOURCE_LABEL,
        "windows_tracked": list(_WINDOWS),
        "cal_day_multiplier": _CAL_DAY_MULTIPLIER,
        "disclaimer": _DISCLAIMER,
    }

    if not csv_path.exists():
        return {**base, "status": "insufficient_data",
                "reason": "no_signal_outcomes_csv",
                "stuck_count": 0, "stuck_rows": [], "by_ticker": []}

    try:
        with csv_path.open("r", encoding="utf-8-sig", errors="replace") as f:
            rows = list(csv.DictReader(f))
    except Exception as exc:
        return {**base, "status": "error",
                "reason": f"csv_parse_failed: {exc}",
                "stuck_count": 0, "stuck_rows": [], "by_ticker": []}

    flagged = scan_unresolved(rows, now=now)
    by_ticker = _group_by_ticker(flagged)
    total_resolved_1d = sum(
        1 for r in rows if not _is_null_outcome(r.get("outcome_return_1d"))
    )

    # Per-window stuck counts for the memo-friendly summary line.
    by_window: dict[int, int] = {w: 0 for w in _WINDOWS}
    for row in flagged:
        by_window[row["window_days"]] = by_window.get(row["window_days"], 0) + 1

    return {
        **base,
        "status": "ok" if flagged or rows else "insufficient_data",
        "total_signals": len(rows),
        "total_resolved_1d": total_resolved_1d,
        "stuck_count": len(flagged),
        "stuck_by_window": by_window,
        "stuck_rows": flagged[:50],  # cap list size for artifact readability
        "by_ticker": by_ticker[:30],
    }


def render_resolution_due_md(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append
    a(f"# Decisions Due For Resolution — {payload.get('generated_at', '')[:10]}")
    a("")
    a(f"**Generated:** {payload.get('generated_at', '')}  ")
    a(f"**Status:** `{payload.get('status', 'unknown')}`  ")
    a(
        f"**Stuck signals:** {payload.get('stuck_count', 0)} "
        f"of {payload.get('total_signals', 0)} total "
        f"(resolved 1d: {payload.get('total_resolved_1d', 0)})"
    )
    a("")
    a(f"> {payload.get('disclaimer', _DISCLAIMER)}")
    a("")

    by_window = payload.get("stuck_by_window") or {}
    if by_window:
        a("## Stuck By Window")
        a("")
        for w in (1, 3, 7):
            n = by_window.get(w, 0) if isinstance(by_window, dict) else 0
            a(f"- **{w}-day window:** {n} stuck signal(s)")
        a("")

    by_ticker = payload.get("by_ticker") or []
    if by_ticker:
        a("## Top Stuck Tickers")
        a("")
        a("| Ticker | Stuck signals | Windows | Max gap (days) |")
        a("|---|---|---|---|")
        for t in by_ticker:
            windows = ", ".join(f"{w}d" for w in t.get("windows_stuck") or [])
            a(
                f"| `{t.get('ticker')}` | {t.get('stuck_signals')} | "
                f"{windows} | {t.get('max_gap_days')} |"
            )
        a("")

    stuck_rows = payload.get("stuck_rows") or []
    if stuck_rows:
        a("## Detail (first 50)")
        a("")
        a("| Ticker | Signal time | Window | Age (cal days) | Gap |")
        a("|---|---|---|---|---|")
        for r in stuck_rows[:50]:
            a(
                f"| `{r.get('ticker')}` | `{r.get('signal_time', '')[:19]}` "
                f"| {r.get('window_days')}d | "
                f"{r.get('age_calendar_days')} | "
                f"+{r.get('gap_calendar_days')} |"
            )
        a("")

    if not stuck_rows and payload.get("total_signals", 0) > 0:
        a("_All signals within their resolution windows are either resolved or still pending._")

    a("---")
    a("_Observe-only — operator probe._")
    return "\n".join(lines)


def run_resolution_due_probe(
    *,
    root: str | Path = ".",
    write_files: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    try:
        payload = build_resolution_due(root=root_path, now=now)
        artifacts: dict[str, str] = {}
        if write_files:
            md = render_resolution_due_md(payload)
            json_path = safe_write_json(
                OutputNamespace.LATEST,
                _OUTPUT_JSON_REL,
                payload,
                base_dir=root_path / "outputs",
            )
            md_path = safe_write_text(
                OutputNamespace.LATEST,
                _OUTPUT_MD_REL,
                md,
                base_dir=root_path / "outputs",
            )
            artifacts = {
                "resolution_due_json": str(json_path),
                "resolution_due_md": str(md_path),
            }
        return {
            "status": payload.get("status"),
            "stuck_count": payload.get("stuck_count"),
            "total_signals": payload.get("total_signals"),
            "artifacts": artifacts,
        }
    except Exception as exc:
        logger.error("resolution_due_probe failed: %s", exc, exc_info=True)
        return {"status": "error", "error": str(exc)}


if __name__ == "__main__":
    import sys
    r = run_resolution_due_probe(root=Path(__file__).resolve().parents[1])
    print(
        f"resolution_due: status={r.get('status')}"
        f" stuck={r.get('stuck_count')} / total={r.get('total_signals')}"
    )
    sys.exit(0)
