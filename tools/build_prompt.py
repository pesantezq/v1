#!/usr/bin/env python3
"""
Prompt builder — generates ready-to-use Claude Code prompts.

Combines the top-ranked task from proposed_task.json with repo context
and fills in the task template to produce a high-signal, copy-paste prompt.

Usage:
    python -m tools.build_prompt [--root PATH] [--out-dir PATH]
    python tools/build_prompt.py

Output:
    prompts/claude_today.txt   — main task prompt (copy into Claude Code)
    prompts/review_today.txt   — short review-request prompt
"""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path


ROOT = Path(__file__).parent.parent

# Tag → relevant files mapping for this repo
TAG_TO_FILES: dict[str, list[str]] = {
    "email_digest":         ["email_digest.py", "email_reporter.py", "digest_builder.py"],
    "digest_ux":            ["digest_builder.py", "email_digest.py"],
    "alert_fatigue":        ["email_digest.py", "state_store.py"],
    "state_store":          ["state_store.py"],
    "state":                ["state_store.py", "drawdown.py"],
    "guardrails":           ["guardrails.py"],
    "projections":          ["projections.py", "contribution_engine.py"],
    "contribution_engine":  ["contribution_engine.py", "projections.py"],
    "watchlist_scanner":    ["watchlist_scanner/scanner.py",
                             "watchlist_scanner/fundamentals_engine.py",
                             "watchlist_scanner/__main__.py"],
    "fundamentals_engine":  ["watchlist_scanner/fundamentals_engine.py",
                             "watchlist_scanner/scanner.py"],
    "fmp_client":           ["fmp_client.py", "scanner/candidate_scanner.py"],
    "scanner":              ["scanner/candidate_scanner.py", "fmp_client.py"],
    "gui":                  ["gui/app.py"],
    "theme_engine":         ["theme_engine/theme_detector.py",
                             "theme_engine/theme_mapper.py",
                             "theme_engine/__main__.py"],
    "api_budget":           ["api_budget.py", "watchlist_scanner/cache_manager.py"],
    "cache":                ["watchlist_scanner/cache_manager.py", "fmp_client.py"],
    "reliability":          ["guardrails.py", "run_lock.py", "main.py"],
    "run_lock":             ["run_lock.py", "main.py"],
    "testing":              ["tests/"],
    "scoring":              ["scoring.py", "finance_analyzer.py"],
    "config":               ["utils.py", "config.json"],
    "ux":                   ["email_digest.py", "digest_builder.py"],
    "cash":                 ["adjustment.py", "state_store.py"],
    "monthly":              ["main.py", "email_digest.py", "projections.py"],
    "orchestration":        ["main.py"],
    "agent":                ["agent/agent_runner.py", "agent/bundle_builder.py"],
}

# Files always worth mentioning as cross-cutting context
ALWAYS_CONTEXT = ["main.py", "utils.py", "state_store.py"]


def get_relevant_files(tags: list[str], root: Path) -> list[str]:
    """Return deduplicated file list for the given tags, filtering to existing files."""
    seen: set[str] = set()
    files: list[str] = []
    for tag in tags:
        for f in TAG_TO_FILES.get(tag, []):
            if f not in seen:
                seen.add(f)
                # Check existence (skip directories)
                fpath = root / f
                if fpath.exists() and fpath.is_file():
                    files.append(f)
                elif fpath.is_dir():
                    files.append(f"{f}  (directory)")
                else:
                    files.append(f"{f}  (not found — may have been renamed)")
    return files


def get_overview_snippet(root: Path, max_lines: int = 50) -> str:
    """Return a concise snippet from REPO_OVERVIEW.md (purpose + entry points)."""
    overview = root / "repo_overview" / "REPO_OVERVIEW.md"
    if not overview.exists():
        return "(repo overview not found — run: python -m tools.repo_overview)"
    text = overview.read_text(errors="ignore")
    lines = text.splitlines()
    snippet: list[str] = []
    for line in lines:
        # Stop before section 3 (Important Files) to keep it brief
        if snippet and line.startswith("## 3."):
            break
        snippet.append(line)
        if len(snippet) >= max_lines:
            break
    return "\n".join(snippet)


def load_task(task_json_path: Path) -> dict | None:
    if not task_json_path.exists():
        return None
    try:
        data = json.loads(task_json_path.read_text(encoding="utf-8"))
        return data.get("top_task")
    except (json.JSONDecodeError, KeyError):
        return None


def load_template(template_path: Path) -> str:
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")
    # Fallback minimal template
    return """\
================================================================================
CLAUDE CODE TASK PROMPT — {generated_date}
================================================================================

## TASK: {task_title}

{task_description}

Area: {task_area} | Tags: {task_tags}

## RELEVANT FILES
{relevant_files}

## REPO CONTEXT
{repo_overview_snippet}

## CONSTRAINTS
- Python 3.12, stdlib preferred
- Run tests: python -m unittest discover tests/ -v  (expect ~185 passing)
- Keep changes minimal and incremental
- Do not break existing behavior

================================================================================
"""


def build_prompt(task: dict, root: Path, template: str) -> str:
    tags = task.get("tags", [])
    relevant_files = get_relevant_files(tags, root)
    overview = get_overview_snippet(root)
    date = datetime.date.today().isoformat()

    # Format relevant files list
    if relevant_files:
        files_block = "\n".join(f"  - {f}" for f in relevant_files)
        # Add always-context files not already included
        extra = [f for f in ALWAYS_CONTEXT
                 if f not in relevant_files and (root / f).exists()]
        if extra:
            files_block += "\n\n  Also worth checking (cross-cutting):\n"
            files_block += "\n".join(f"  - {f}" for f in extra)
    else:
        files_block = "  (could not determine relevant files — inspect manually)"

    return template.format(
        generated_date=date,
        task_id=task.get("id", "unknown"),
        task_title=task.get("title", "(no title)"),
        task_description=task.get("description", "(no description)").strip(),
        task_area=task.get("area", "unknown"),
        task_tags=", ".join(tags) if tags else "(none)",
        relevant_files=files_block,
        repo_overview_snippet=overview,
    )


def build_review_prompt(task: dict, date: str) -> str:
    return f"""\
================================================================================
REVIEW PROMPT — {date}
Task: {task.get('id', '?')} — {task.get('title', '?')}
================================================================================

Please review the changes you just made for task `{task.get('id', '?')}`.

Provide:
1. Summary of what was changed (files + reason)
2. Any assumptions made
3. Potential risks or side effects
4. How the change was validated

Then run: `python -m unittest discover tests/ -v`
And confirm all tests pass.

After validation, run: `python -m tools.review_packet`
to generate a human review packet at daily_update/review_packet.md

================================================================================
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(root: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.date.today().isoformat()

    task_json = root / "daily_update" / "proposed_task.json"
    task = load_task(task_json)

    if task is None:
        print("[build_prompt] WARNING: no proposed task found.")
        print("[build_prompt] Run python -m tools.task_ranker first.")
        # Write a placeholder prompt
        placeholder = (
            f"# Claude Code Prompt — {date}\n\n"
            "No proposed task found.\n"
            "Run: python -m tools.task_ranker\n"
            "Then re-run: python -m tools.build_prompt\n"
        )
        (out_dir / "claude_today.txt").write_text(placeholder, encoding="utf-8")
        return

    template_path = root / "templates" / "claude_task_template.txt"
    template = load_template(template_path)

    prompt = build_prompt(task, root, template)
    review = build_review_prompt(task, date)

    main_path = out_dir / "claude_today.txt"
    review_path = out_dir / "review_today.txt"

    main_path.write_text(prompt, encoding="utf-8")
    review_path.write_text(review, encoding="utf-8")

    task_id = task.get("id", "?")
    task_title = task.get("title", "?")[:60]
    print(f"[build_prompt] Task: {task_id} — {task_title}")
    print(f"[build_prompt] Written: {main_path}")
    print(f"[build_prompt] Written: {review_path}")
    print(f"[build_prompt] Prompt size: {len(prompt)} chars")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="Prompt output directory (default: <root>/prompts)")
    args = parser.parse_args()

    out_dir = args.out_dir or (args.root / "prompts")
    run(args.root, out_dir)


if __name__ == "__main__":
    main()
