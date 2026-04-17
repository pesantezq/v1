#!/usr/bin/env python3
"""
Task ranker — deterministic prioritization from backlog/improvements.json.

No LLM required. Scores each task using configurable weights and produces
a ranked list with a single "proposed next task" recommendation.

Usage:
    python -m tools.task_ranker [--backlog PATH] [--root PATH] [--out-dir PATH]
    python tools/task_ranker.py

Output:
    daily_update/proposed_task.md
    daily_update/proposed_task.json
"""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Scoring weights — edit these to shift priorities
# ---------------------------------------------------------------------------

WEIGHTS = {
    "user_visible_impact": 3.0,   # 1-5: how much the user sees/feels the fix
    "reliability_impact": 2.5,    # 1-5: prevents crashes / data errors / silent failures
    "ease_bonus": 1.5,            # bonus for low effort: (5 - effort) * ease_bonus
    "broken_bonus": 20.0,         # extra points if item.broken == true
    "blocker_penalty": 15.0,      # subtracted if blocked_by is non-empty
}

# Area priority multipliers (areas you care most about right now)
AREA_PRIORITY: dict[str, float] = {
    "reliability": 1.20,
    "state": 1.15,
    "email_digest": 1.10,
    "digest_ux": 1.10,
    "testing": 1.05,
    "scanner": 1.00,
    "projections": 1.00,
    "gui": 0.90,
    "theme_engine": 0.85,
}

DEFAULT_AREA_MULTIPLIER = 1.0


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_item(item: dict[str, Any]) -> float:
    """Return a priority score (higher = more urgent)."""
    s = 0.0
    s += item.get("user_visible_impact", 0) * WEIGHTS["user_visible_impact"]
    s += item.get("reliability_impact", 0) * WEIGHTS["reliability_impact"]
    effort = min(max(item.get("effort", 3), 1), 5)
    s += (5 - effort) * WEIGHTS["ease_bonus"]
    if item.get("broken", False):
        s += WEIGHTS["broken_bonus"]
    if item.get("blocked_by"):
        s -= WEIGHTS["blocker_penalty"]
    area = item.get("area", "")
    s *= AREA_PRIORITY.get(area, DEFAULT_AREA_MULTIPLIER)
    return round(s, 2)


def rank_backlog(items: list[dict]) -> list[dict]:
    """Return items sorted by score descending, with scores attached."""
    scored = []
    for item in items:
        item = dict(item)  # don't mutate original
        item["_score"] = score_item(item)
        item["_blocked"] = bool(item.get("blocked_by"))
        scored.append(item)
    return sorted(scored, key=lambda x: x["_score"], reverse=True)


def label(score: float) -> str:
    if score >= 30:
        return "HIGH"
    if score >= 18:
        return "MEDIUM"
    return "LOW"


# ---------------------------------------------------------------------------
# Markdown builder
# ---------------------------------------------------------------------------

def build_markdown(ranked: list[dict], generated_at: str) -> str:
    top = ranked[0] if ranked else None
    alternates = ranked[1:6]

    lines = [
        f"# Proposed Next Task — {generated_at}",
        "",
    ]

    if not ranked:
        lines += [
            "> No tasks found. Add items to `backlog/improvements.json`.",
        ]
        return "\n".join(lines)

    # --- TOP TASK ---
    assert top is not None
    score = top["_score"]
    lines += [
        "## Recommended Task",
        "",
        f"**ID:** `{top['id']}`",
        f"**Priority:** {label(score)} (score: {score})",
        f"**Area:** {top.get('area', '?')}",
        f"**Effort:** {top.get('effort', '?')}/5 "
        f"(impact={top.get('user_visible_impact', '?')}, "
        f"reliability={top.get('reliability_impact', '?')})",
        f"**Tags:** {', '.join(top.get('tags', []))}",
        "",
        f"### {top['title']}",
        "",
        top.get("description", "").strip(),
        "",
    ]

    if top.get("_blocked"):
        blocked = top.get("blocked_by", [])
        lines += [f"> ⚠ BLOCKED by: {', '.join(str(b) for b in blocked)}", ""]

    lines += [
        "### Rationale",
        "",
        _rationale(top),
        "",
        "---",
        "",
    ]

    # --- ALTERNATES ---
    lines += ["## Ranked Alternatives", ""]
    for i, item in enumerate(alternates, 2):
        s = item["_score"]
        blocked_note = " *(blocked)*" if item["_blocked"] else ""
        lines.append(
            f"{i}. **[{label(s)}]** `{item['id']}` — {item['title']}"
            f"{blocked_note} _(score: {s}, area: {item.get('area', '?')})_"
        )
    lines.append("")

    # --- SKIPPED ---
    skipped = [x for x in ranked[6:] if not x["_blocked"]]
    blocked_count = sum(1 for x in ranked if x["_blocked"])
    lines += [
        "## Stats",
        "",
        f"- Total backlog items: {len(ranked)}",
        f"- Blocked: {blocked_count}",
        f"- Shown in alternates: {len(alternates)}",
        f"- Not shown: {len(skipped)}",
        "",
        "---",
        "",
        "**Next step:** Run `python -m tools.build_prompt` → `prompts/claude_today.txt`",
    ]

    return "\n".join(lines)


def _rationale(item: dict) -> str:
    parts = []
    uvi = item.get("user_visible_impact", 0)
    ri = item.get("reliability_impact", 0)
    effort = item.get("effort", 3)
    area = item.get("area", "")

    if item.get("broken"):
        parts.append("This item is marked as **broken** — highest urgency.")
    if uvi >= 4:
        parts.append(f"High user-visible impact ({uvi}/5) — users will notice the improvement.")
    if ri >= 4:
        parts.append(f"High reliability impact ({ri}/5) — reduces risk of silent failure.")
    if effort <= 2:
        parts.append(f"Low effort ({effort}/5) — good return on time invested.")
    mul = AREA_PRIORITY.get(area, DEFAULT_AREA_MULTIPLIER)
    if mul > 1.0:
        parts.append(f"Area `{area}` has elevated priority (×{mul}).")
    if not parts:
        parts.append("Ranked above alternatives on combined impact × ease score.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(backlog_path: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()
    generated_at = now.strftime("%Y-%m-%d %H:%M")

    # Load backlog
    items: list[dict] = []
    if backlog_path.exists():
        try:
            data = json.loads(backlog_path.read_text(encoding="utf-8"))
            items = data.get("improvements", [])
            print(f"[task_ranker] Loaded {len(items)} items from {backlog_path.name}")
        except (json.JSONDecodeError, KeyError) as exc:
            print(f"[task_ranker] WARNING: could not parse backlog: {exc}")
    else:
        print(f"[task_ranker] WARNING: backlog not found at {backlog_path}")

    ranked = rank_backlog(items)

    result = {
        "generated_at": generated_at,
        "top_task": ranked[0] if ranked else None,
        "ranked": ranked,
        "weights": WEIGHTS,
        "area_priority": AREA_PRIORITY,
    }

    md = build_markdown(ranked, generated_at)
    md_path = out_dir / "proposed_task.md"
    md_path.write_text(md, encoding="utf-8")

    json_path = out_dir / "proposed_task.json"
    json_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    if ranked:
        top = ranked[0]
        print(f"[task_ranker] Top task: [{label(top['_score'])}] {top['id']} — {top['title'][:60]}")
    print(f"[task_ranker] Written: {md_path}")
    print(f"[task_ranker] Written: {json_path}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--backlog", type=Path, default=None,
                        help="Path to improvements.json (default: <root>/backlog/improvements.json)")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="Output directory (default: <root>/daily_update)")
    args = parser.parse_args()

    backlog = args.backlog or (args.root / "backlog" / "improvements.json")
    out_dir = args.out_dir or (args.root / "daily_update")
    run(backlog, out_dir)


if __name__ == "__main__":
    main()
