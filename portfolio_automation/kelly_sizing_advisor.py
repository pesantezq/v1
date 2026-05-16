"""
Kelly Sizing Advisor — observe-only fractional-Kelly recommendation.

Computes a fractional Kelly sizing recommendation per decision type using
the resolved outcomes in outputs/policy/decision_outcomes.jsonl. This is
an advisory observation; it never replaces conviction.py multipliers or
allocation_engine sizing — those are CLAUDE.md-protected.

Kelly formula (half-Kelly variant by default for safety):

    f_full = (b*p - q) / b
    f_half = 0.5 * max(0, f_full)

  where:
    p = empirical hit rate (correct / judgeable)
    q = 1 - p
    b = win/loss ratio = mean(positive return) / |mean(negative return)|

  The output `kelly_fraction_suggested` is f_half clamped to [0, 0.25] — a
  conservative safety cap that protects against estimation error.

Gate:
  Requires ≥ _MIN_RESOLVED_FOR_KELLY resolved+judgeable rows per decision
  type. Below the gate, that decision reports status="insufficient_data".

Inputs (read-only):
  - outputs/policy/decision_outcomes.jsonl

Outputs (LATEST namespace):
  - outputs/latest/kelly_sizing_advisor.json
  - outputs/latest/kelly_sizing_advisor.md

Hard guarantees:
  - observe_only=True hardcoded.
  - Never modifies conviction_score, allocation, or any decision.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger("stockbot.portfolio_automation.kelly_sizing_advisor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MIN_RESOLVED_FOR_KELLY = 20    # Minimum resolved+judgeable rows per group
_HALF_KELLY = 0.5               # Standard safety multiplier
_KELLY_HARD_CAP = 0.25          # Never recommend > 25% Kelly
_TARGET_DECISIONS = ("BUY", "SCALE", "SELL")  # Decisions we size for

_OUTCOMES_JSONL = ("outputs", "policy", "decision_outcomes.jsonl")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except json.JSONDecodeError:
                pass
    except OSError:
        pass
    return rows


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------


def kelly_fraction(
    *,
    hit_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
    half_kelly: bool = True,
    hard_cap: float = _KELLY_HARD_CAP,
) -> float:
    """
    Compute a fractional Kelly recommendation.

    Inputs:
      hit_rate     — in [0, 1]
      avg_win_pct  — mean positive return (decimal, e.g. 0.04 = 4%)
      avg_loss_pct — mean negative return magnitude (decimal, e.g. 0.03 = 3%)

    Returns f in [0, hard_cap]. Half-Kelly applied by default.
    """
    if avg_loss_pct <= 0 or avg_win_pct <= 0:
        return 0.0
    b = avg_win_pct / avg_loss_pct
    p = max(0.0, min(1.0, hit_rate))
    q = 1.0 - p
    f_full = (b * p - q) / b
    if f_full <= 0:
        return 0.0
    f = (_HALF_KELLY if half_kelly else 1.0) * f_full
    return round(min(f, hard_cap), 4)


def _group_returns(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    For a slice of rows, compute hit_rate, avg_win, avg_loss, n_resolved.
    """
    resolved = [r for r in rows if r.get("resolved")]
    judgeable = [r for r in resolved if r.get("direction_correct") is not None]
    correct = [r for r in judgeable if r.get("direction_correct")]
    returns = [
        _safe_float(r.get("return_pct"))
        for r in resolved
        if _safe_float(r.get("return_pct")) is not None
    ]
    wins = [r for r in returns if r > 0]
    losses = [abs(r) for r in returns if r < 0]
    return {
        "n_total": len(rows),
        "n_resolved": len(resolved),
        "n_judgeable": len(judgeable),
        "n_correct": len(correct),
        "hit_rate": (len(correct) / len(judgeable)) if judgeable else None,
        "avg_win_pct": (sum(wins) / len(wins)) if wins else None,
        "avg_loss_pct": (sum(losses) / len(losses)) if losses else None,
        "n_wins": len(wins),
        "n_losses": len(losses),
    }


def evaluate_decision_group(
    rows: list[dict[str, Any]],
    decision: str,
) -> dict[str, Any]:
    """Return per-decision Kelly recommendation row."""
    stats = _group_returns(rows)
    if (stats["n_judgeable"] or 0) < _MIN_RESOLVED_FOR_KELLY:
        return {
            "decision": decision,
            "status": "insufficient_data",
            "n_judgeable": stats["n_judgeable"],
            "min_required": _MIN_RESOLVED_FOR_KELLY,
            "kelly_fraction_suggested": None,
            **{k: v for k, v in stats.items() if k != "n_total"},
        }
    if stats["avg_win_pct"] is None or stats["avg_loss_pct"] is None:
        return {
            "decision": decision,
            "status": "insufficient_data",
            "reason": "no positive or no negative returns observed",
            "kelly_fraction_suggested": None,
            **stats,
        }
    f = kelly_fraction(
        hit_rate=stats["hit_rate"],
        avg_win_pct=stats["avg_win_pct"],
        avg_loss_pct=stats["avg_loss_pct"],
    )
    return {
        "decision": decision,
        "status": "ok",
        "kelly_fraction_suggested": f,
        "win_loss_ratio_b": round(stats["avg_win_pct"] / stats["avg_loss_pct"], 4),
        **stats,
    }


# ---------------------------------------------------------------------------
# Plan envelope
# ---------------------------------------------------------------------------


def build_plan(
    *,
    rows_by_decision: dict[str, list[dict[str, Any]]],
    notes: list[str],
) -> dict[str, Any]:
    per_decision = []
    actionable = 0
    for d in _TARGET_DECISIONS:
        result = evaluate_decision_group(rows_by_decision.get(d, []) or [], d)
        per_decision.append(result)
        if result["status"] == "ok":
            actionable += 1

    summary_line = (
        f"Kelly sizing: {actionable}/{len(_TARGET_DECISIONS)} decision groups "
        f"have sufficient data (min={_MIN_RESOLVED_FOR_KELLY} resolved each)"
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "schema_version": "1",
        "min_resolved_required": _MIN_RESOLVED_FOR_KELLY,
        "half_kelly": True,
        "hard_cap": _KELLY_HARD_CAP,
        "summary_line": summary_line,
        "by_decision": per_decision,
        "notes": list(notes),
        "advisory_disclaimer": (
            "Advisory only. Live sizing (conviction.py multipliers, "
            "allocation_engine outputs) is NOT modified by this layer."
        ),
    }


def _render_markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# Kelly Sizing Advisor",
        "",
        f"_Generated: {plan.get('generated_at')}_",
        "",
        "Observe-only. Live sizing is NOT modified.",
        "",
        plan.get("summary_line", ""),
        "",
        f"_min_resolved={plan.get('min_resolved_required')}, "
        f"half_kelly={plan.get('half_kelly')}, "
        f"hard_cap={plan.get('hard_cap')}_",
        "",
        "## Per-decision Kelly",
        "",
        "| Decision | Status | n resolved | Hit rate | Avg win | Avg loss | b ratio | Kelly f |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in plan.get("by_decision", []):
        f = r.get("kelly_fraction_suggested")
        hr = r.get("hit_rate")
        aw = r.get("avg_win_pct")
        al = r.get("avg_loss_pct")
        b = r.get("win_loss_ratio_b")
        lines.append("| {d} | {s} | {n} | {hr} | {aw} | {al} | {b} | {f} |".format(
            d=r.get("decision"),
            s=r.get("status"),
            n=r.get("n_judgeable") or 0,
            hr=(f"{hr:.0%}" if hr is not None else "—"),
            aw=(f"{aw:+.2%}" if aw is not None else "—"),
            al=(f"{al:.2%}" if al is not None else "—"),
            b=(f"{b:.2f}" if b is not None else "—"),
            f=(f"{f:.2%}" if f is not None else "—"),
        ))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


def run_kelly_sizing_advisor(
    repo_root: Path | str,
    *,
    base_dir: Path | str = "outputs",
) -> dict[str, Any]:
    repo_root = Path(repo_root)
    base_dir = Path(base_dir)

    # Outcomes path lives in repo_root/outputs/policy/decision_outcomes.jsonl
    outcomes_path = repo_root.joinpath(*_OUTCOMES_JSONL)
    rows = _load_jsonl(outcomes_path)

    notes: list[str] = []
    if not rows:
        notes.append(
            f"no rows in {outcomes_path.relative_to(repo_root) if outcomes_path.is_absolute() else outcomes_path}"
        )

    # Group by decision
    rows_by_decision: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        d = str(r.get("decision") or "").upper()
        rows_by_decision.setdefault(d, []).append(r)

    plan = build_plan(rows_by_decision=rows_by_decision, notes=notes)
    _write_artifacts(plan, base_dir)
    return plan


def _write_artifacts(plan: dict[str, Any], base_dir: Path) -> None:
    try:
        safe_write_json(
            OutputNamespace.LATEST, "kelly_sizing_advisor.json", plan, base_dir=base_dir,
        )
        safe_write_text(
            OutputNamespace.LATEST, "kelly_sizing_advisor.md",
            _render_markdown(plan), base_dir=base_dir,
        )
    except Exception as exc:
        logger.warning(
            "kelly_sizing_advisor: failed to write artifacts (non-fatal): %s", exc
        )
