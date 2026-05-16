"""
Alpha Attribution Report — observe-only Sharpe + information ratio per source.

Extends `decision_performance_attribution.py` with risk-adjusted metrics so
the operator can see which alpha source (structural / portfolio / finance /
watchlist / market) actually earns risk-adjusted returns over time.

Inputs (read-only):
  - outputs/policy/decision_outcomes.jsonl

Outputs (LATEST namespace):
  - outputs/latest/alpha_attribution_report.json
  - outputs/latest/alpha_attribution_report.md

Metrics per source:
  - n_resolved
  - hit_rate
  - mean_return_pct
  - return_stdev_pct
  - sharpe_proxy = mean / stdev   (no rf; daily; advisory)
  - information_ratio_proxy = mean / stdev (vs zero benchmark)
  - sortino_proxy = mean / downside_stdev
  - kurtosis flag if extreme returns dominate

Gate:
  - Source reports status="insufficient_data" when n_resolved < _MIN_N.

Hard guarantees:
  - observe_only=True hardcoded.
  - Never modifies decision_plan, score fields, or attribution outputs of
    the existing performance_attribution layer.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger("stockbot.portfolio_automation.alpha_attribution_report")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MIN_N = 20   # minimum resolved returns per source to compute metrics

_TARGET_SOURCES = ("structural", "portfolio", "finance", "watchlist", "market")

_OUTCOMES_JSONL = ("outputs", "policy", "decision_outcomes.jsonl")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
        return x if math.isfinite(x) else None
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
# Statistics
# ---------------------------------------------------------------------------


def _stdev(values: list[float], mean: float) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / (n - 1)  # sample stdev
    return math.sqrt(var) if var > 0 else 0.0


def _downside_stdev(values: list[float], target: float = 0.0) -> float:
    """Stdev of returns below *target* (zero by default)."""
    downside = [v - target for v in values if v < target]
    if len(downside) < 2:
        return 0.0
    mean = sum(downside) / len(downside)
    var = sum((d - mean) ** 2 for d in downside) / (len(downside) - 1)
    return math.sqrt(var) if var > 0 else 0.0


def compute_source_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute risk-adjusted metrics for one source slice."""
    resolved = [r for r in rows if r.get("resolved")]
    judgeable = [r for r in resolved if r.get("direction_correct") is not None]
    correct = [r for r in judgeable if r.get("direction_correct")]
    returns = [
        _safe_float(r.get("return_pct"))
        for r in resolved
        if _safe_float(r.get("return_pct")) is not None
    ]
    n = len(returns)
    if n < _MIN_N:
        return {
            "status": "insufficient_data",
            "n_total": len(rows),
            "n_resolved": len(resolved),
            "n_judgeable": len(judgeable),
            "min_required": _MIN_N,
            "hit_rate": None,
            "mean_return_pct": None,
            "return_stdev_pct": None,
            "sharpe_proxy": None,
            "sortino_proxy": None,
        }
    mean = sum(returns) / n
    sd = _stdev(returns, mean)
    dsd = _downside_stdev(returns)
    sharpe = round(mean / sd, 4) if sd > 0 else None
    sortino = round(mean / dsd, 4) if dsd > 0 else None
    hit_rate = (len(correct) / len(judgeable)) if judgeable else None
    return {
        "status": "ok",
        "n_total": len(rows),
        "n_resolved": len(resolved),
        "n_judgeable": len(judgeable),
        "n_returns": n,
        "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
        "mean_return_pct": round(mean, 4),
        "return_stdev_pct": round(sd, 4),
        "downside_stdev_pct": round(dsd, 4),
        "sharpe_proxy": sharpe,
        "sortino_proxy": sortino,
        "information_ratio_proxy": sharpe,  # vs zero benchmark for now
    }


# ---------------------------------------------------------------------------
# Plan envelope
# ---------------------------------------------------------------------------


def build_plan(
    *,
    rows_by_source: dict[str, list[dict[str, Any]]],
    notes: list[str],
) -> dict[str, Any]:
    by_source: dict[str, dict[str, Any]] = {}
    actionable = 0
    for src in _TARGET_SOURCES:
        m = compute_source_metrics(rows_by_source.get(src, []) or [])
        by_source[src] = m
        if m["status"] == "ok":
            actionable += 1

    # Identify best/worst source by Sharpe when available
    ranked = sorted(
        (
            (src, m) for src, m in by_source.items()
            if m["status"] == "ok" and m.get("sharpe_proxy") is not None
        ),
        key=lambda x: x[1]["sharpe_proxy"],
        reverse=True,
    )
    best_source = ranked[0][0] if ranked else None
    worst_source = ranked[-1][0] if ranked else None

    summary_line = (
        f"Alpha attribution: {actionable}/{len(_TARGET_SOURCES)} sources have "
        f"sufficient data (min {_MIN_N} returns each)"
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "schema_version": "1",
        "summary_line": summary_line,
        "min_n_required": _MIN_N,
        "by_source": by_source,
        "best_sharpe_source": best_source,
        "worst_sharpe_source": worst_source,
        "notes": list(notes),
        "advisory_disclaimer": (
            "Risk-adjusted metrics are observational. They do not alter "
            "decision_plan or any scoring outputs."
        ),
    }


def _render_markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# Alpha Attribution Report",
        "",
        f"_Generated: {plan.get('generated_at')}_",
        "",
        "Observe-only. Does not alter decision_plan or scoring.",
        "",
        plan.get("summary_line", ""),
        "",
        f"_min_n_required={plan.get('min_n_required')}_",
        "",
    ]
    if plan.get("best_sharpe_source"):
        lines.append(f"- Best Sharpe source: **{plan['best_sharpe_source']}**")
    if plan.get("worst_sharpe_source") and plan.get("worst_sharpe_source") != plan.get("best_sharpe_source"):
        lines.append(f"- Worst Sharpe source: **{plan['worst_sharpe_source']}**")
    lines += [
        "",
        "## Per-source risk-adjusted metrics",
        "",
        "| Source | Status | n returns | Hit rate | Mean ret | Stdev | Sharpe | Sortino |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for src, m in plan.get("by_source", {}).items():
        hr = m.get("hit_rate")
        mu = m.get("mean_return_pct")
        sd = m.get("return_stdev_pct")
        sh = m.get("sharpe_proxy")
        st = m.get("sortino_proxy")
        lines.append("| {s} | {status} | {n} | {hr} | {mu} | {sd} | {sh} | {st} |".format(
            s=src,
            status=m.get("status"),
            n=m.get("n_returns") or 0,
            hr=(f"{hr:.0%}" if hr is not None else "—"),
            mu=(f"{mu:+.2%}" if mu is not None else "—"),
            sd=(f"{sd:.2%}" if sd is not None else "—"),
            sh=(f"{sh:+.2f}" if sh is not None else "—"),
            st=(f"{st:+.2f}" if st is not None else "—"),
        ))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


def run_alpha_attribution_report(
    repo_root: Path | str,
    *,
    base_dir: Path | str = "outputs",
) -> dict[str, Any]:
    repo_root = Path(repo_root)
    base_dir = Path(base_dir)
    outcomes_path = repo_root.joinpath(*_OUTCOMES_JSONL)
    rows = _load_jsonl(outcomes_path)

    notes: list[str] = []
    if not rows:
        notes.append("no rows in decision_outcomes.jsonl")

    rows_by_source: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        src = str(r.get("source") or "").lower()
        rows_by_source.setdefault(src, []).append(r)

    plan = build_plan(rows_by_source=rows_by_source, notes=notes)

    try:
        safe_write_json(
            OutputNamespace.LATEST, "alpha_attribution_report.json", plan,
            base_dir=base_dir,
        )
        safe_write_text(
            OutputNamespace.LATEST, "alpha_attribution_report.md",
            _render_markdown(plan), base_dir=base_dir,
        )
    except Exception as exc:
        logger.warning(
            "alpha_attribution_report: failed to write artifacts (non-fatal): %s",
            exc,
        )

    return plan
