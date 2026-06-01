"""Observe-only documentation auditor.

Scans docs against machine-readable sources of truth + git history. Reports
drift / dead-refs / cross-doc inconsistency / coverage gaps. Never recomputes
decisions; never mutates portfolio, allocation, scoring, or decision state.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Anchor:
    name: str
    source_artifact: str
    source_json_path: str
    doc_globs: tuple[str, ...]
    pattern: str
    fmt: str = "int"


@dataclass
class Finding:
    dimension: str
    severity: str
    doc: str
    detail: str
    auto_fixable: bool = False
    anchor: str | None = None
    current: str | None = None
    expected: str | None = None
    line: int | None = None


def _fmt_value(value, fmt: str) -> str:
    if fmt == "int":
        return str(int(value))
    if fmt == "float2":
        return f"{float(value):.2f}"
    if fmt == "pct1":
        return f"{float(value) * 100:.1f}"
    if fmt == "usd0":
        return f"{float(value):.0f}"
    return str(value)


def resolve_source(anchor: Anchor, root: str) -> str | None:
    """Return the source-of-truth value for an anchor, formatted, or None if
    the artifact is missing/unreadable or the json path does not resolve."""
    path = Path(root) / anchor.source_artifact
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    cur = data
    for key in anchor.source_json_path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    try:
        return _fmt_value(cur, anchor.fmt)
    except (TypeError, ValueError):
        return None


# Seeded by an upfront sweep of documented constants; growable. Each anchor is
# authoritative ONLY in its doc_globs, and its pattern has exactly one capture
# group = the documented value.
ANCHOR_REGISTRY: list[Anchor] = [
    Anchor("pipeline_stage_count", "outputs/latest/daily_run_status.json",
           "stage_summary.total", ("docs/PIPELINE_RUNBOOK.md", "docs/ARCHITECTURE.md"),
           r"(\d+)\s+pipeline stages", "int"),
    Anchor("concentration_cap", "outputs/latest/retune_impact.json",
           "current_snapshot.structural_caps.concentration_cap",
           ("docs/ALLOCATION_POLICY.md",), r"concentration cap[^\d]*(\d+)\s*%", "pct1"),
    Anchor("leverage_cap", "outputs/latest/retune_impact.json",
           "current_snapshot.structural_caps.leverage_cap",
           ("docs/ALLOCATION_POLICY.md",), r"leverage cap[^\d]*(\d+)\s*%", "pct1"),
    Anchor("sector_cap", "outputs/latest/retune_impact.json",
           "current_snapshot.allocation_engine.sector_cap",
           ("docs/ALLOCATION_POLICY.md",), r"sector cap[^\d]*(\d+)\s*%", "pct1"),
    Anchor("fmp_daily_budget", "outputs/latest/fmp_budget_status.json",
           "budget.budget", ("docs/AI_BUDGET.md",),
           r"fmp_daily_calls_budget[^\d]*(\d+)", "int"),
    Anchor("ai_monthly_cap", "outputs/latest/ai_budget_summary.json",
           "monthly_cost_limit_usd", ("docs/AI_BUDGET.md",),
           r"monthly[^\$]*\$(\d+)", "usd0"),
]
