#!/usr/bin/env python3
"""
Daily engineering packet generator.

Gathers repo health signals into a concise daily snapshot:
  - Git status and recent commits
  - Test results
  - Recent log warnings
  - Known issues from repo overview
  - Top backlog items
  - Latest output file timestamps

Usage:
    python -m tools.daily_packet [--root PATH] [--out-dir PATH] [--skip-tests]
    python tools/daily_packet.py [--root PATH] [--out-dir PATH] [--skip-tests]

Output:
    daily_update/daily_packet.md
    daily_update/daily_packet.json
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parent.parent
IGNORE_OUTPUT_DIRS = {"__pycache__", ".git", ".venv", "venv"}


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str]:
    """Run a subprocess, return (returncode, combined output). Never raises."""
    try:
        result = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace"
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except FileNotFoundError:
        return 1, f"[command not found: {cmd[0]}]"
    except subprocess.TimeoutExpired:
        return 1, f"[timeout after {timeout}s]"
    except Exception as exc:
        return 1, f"[error: {exc}]"


def git_status(root: Path) -> dict:
    rc, out = _run(["git", "status", "--short"], root)
    if rc != 0:
        return {"available": False, "error": out}
    lines = [l for l in out.splitlines() if l.strip()]
    modified = sum(1 for l in lines if not l.startswith("??"))
    untracked = sum(1 for l in lines if l.startswith("??"))
    # Filter out .venv noise from untracked count
    real_untracked = sum(
        1 for l in lines
        if l.startswith("??") and ".venv" not in l and "__pycache__" not in l
    )
    return {
        "available": True,
        "modified": modified,
        "untracked": untracked,
        "real_untracked": real_untracked,
        "lines": [l for l in lines if ".venv" not in l][:30],
    }


def git_log(root: Path, n: int = 8) -> list[str]:
    rc, out = _run(["git", "log", "--oneline", f"-{n}"], root)
    if rc != 0:
        return []
    return out.splitlines()


def git_diff_stat(root: Path) -> str:
    rc, out = _run(["git", "diff", "--stat", "HEAD"], root)
    if rc != 0 or not out:
        return "(no uncommitted diff)"
    return out[:800]


def git_branch(root: Path) -> str:
    rc, out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
    return out if rc == 0 else "unknown"


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def run_tests(root: Path) -> dict:
    rc, out = _run(
        ["python", "-m", "unittest", "discover", "tests/", "-v"],
        root, timeout=120
    )
    lines = out.splitlines()
    ran = 0
    failed = 0
    errors = 0
    skipped = 0
    status = "unknown"

    for line in reversed(lines):
        if line.startswith("Ran "):
            parts = line.split()
            ran = int(parts[1]) if len(parts) > 1 else 0
        if "OK" in line and ("skipped" in line or line.strip() == "OK"):
            status = "pass"
        elif line.strip() == "OK":
            status = "pass"
        if "FAILED" in line:
            status = "fail"
            # FAILED (failures=X, errors=Y)
            import re
            m = re.search(r"failures=(\d+)", line)
            if m:
                failed = int(m.group(1))
            m = re.search(r"errors=(\d+)", line)
            if m:
                errors = int(m.group(1))
        if "skipped" in line:
            import re
            m = re.search(r"skipped=(\d+)", line)
            if m:
                skipped = int(m.group(1))

    # Grab tail of output for context
    tail = "\n".join(lines[-15:]) if lines else out[:500]

    return {
        "ran": ran,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "status": status if ran > 0 else ("error" if rc != 0 else "unknown"),
        "returncode": rc,
        "tail": tail,
    }


# ---------------------------------------------------------------------------
# Log analysis
# ---------------------------------------------------------------------------

def read_recent_log(root: Path) -> dict:
    logs_dir = root / "logs"
    if not logs_dir.exists():
        return {"available": False, "file": None, "issues": []}

    today = datetime.date.today().isoformat()
    candidates = sorted(logs_dir.glob("????-??-??.log"), reverse=True)
    if not candidates:
        return {"available": False, "file": None, "issues": []}

    log_file = candidates[0]
    try:
        text = log_file.read_text(errors="ignore")
    except OSError:
        return {"available": True, "file": str(log_file.name), "issues": []}

    lines = text.splitlines()[-150:]
    issue_keywords = {"ERROR", "WARNING", "WARN", "FAILED", "EXCEPTION",
                      "CRITICAL", "TRACEBACK", "STALE LOCK"}
    issues = [
        l for l in lines
        if any(kw in l.upper() for kw in issue_keywords)
    ]
    return {
        "available": True,
        "file": log_file.name,
        "is_today": log_file.stem == today,
        "total_lines": len(text.splitlines()),
        "issues": issues[:25],
    }


# ---------------------------------------------------------------------------
# Repo overview
# ---------------------------------------------------------------------------

def get_overview_snippet(root: Path) -> str:
    overview = root / "repo_overview" / "REPO_OVERVIEW.md"
    if not overview.exists():
        return "(not found — run: python -m tools.repo_overview)"
    text = overview.read_text(errors="ignore")
    lines = text.splitlines()
    # Return up to the start of section 3 (Important Files) to keep it brief
    snippet_lines = []
    for i, line in enumerate(lines):
        if i > 0 and line.startswith("## 3."):
            break
        snippet_lines.append(line)
        if len(snippet_lines) >= 60:
            break
    return "\n".join(snippet_lines)


def get_known_issues(root: Path) -> list[dict]:
    overview_json = root / "repo_overview" / "repo_overview.json"
    if not overview_json.exists():
        return []
    try:
        data = json.loads(overview_json.read_text())
        val = data.get("known_issues", [])
        # Handle both list and legacy dict-with-items formats
        items = val if isinstance(val, list) else val.get("items", [])
        return items[:15]
    except (json.JSONDecodeError, KeyError):
        return []


def get_overview_stats(root: Path) -> dict:
    overview_json = root / "repo_overview" / "repo_overview.json"
    if not overview_json.exists():
        return {}
    try:
        data = json.loads(overview_json.read_text())

        def _len(key: str) -> int:
            val = data.get(key, [])
            if isinstance(val, list):
                return len(val)
            if isinstance(val, dict):
                return len(val.get("items", val))
            return 0

        tests_data = data.get("tests", {})
        test_count = (
            tests_data.get("total_tests", 0)
            if isinstance(tests_data, dict)
            else 0
        )
        return {
            "entry_points": _len("entry_points"),
            "important_files": _len("important_files"),
            "data_models": _len("data_models"),
            "integrations": _len("integrations"),
            "known_issues": _len("known_issues"),
            "test_count": test_count,
        }
    except (json.JSONDecodeError, KeyError):
        return {}


# ---------------------------------------------------------------------------
# Backlog
# ---------------------------------------------------------------------------

def get_backlog_top(root: Path, n: int = 5) -> list[dict]:
    backlog = root / "backlog" / "improvements.json"
    if not backlog.exists():
        return []
    try:
        data = json.loads(backlog.read_text())
        items = data.get("improvements", [])
    except (json.JSONDecodeError, KeyError):
        return []

    def priority_score(item: dict) -> float:
        s = 0.0
        s += item.get("user_visible_impact", 0) * 3.0
        s += item.get("reliability_impact", 0) * 2.5
        s += (5.0 - min(item.get("effort", 3), 5)) * 1.5
        if item.get("broken", False):
            s += 20.0
        if item.get("blocked_by"):
            s -= 15.0
        return s

    return sorted(items, key=priority_score, reverse=True)[:n]


# ---------------------------------------------------------------------------
# Latest outputs
# ---------------------------------------------------------------------------

def get_latest_outputs(root: Path) -> list[dict]:
    out_dir = root / "outputs" / "latest"
    if not out_dir.exists():
        return []
    today = datetime.date.today()
    result = []
    for f in sorted(out_dir.iterdir()):
        if not f.is_file():
            continue
        mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime)
        result.append({
            "file": f.name,
            "modified": mtime.strftime("%Y-%m-%d %H:%M"),
            "today": mtime.date() == today,
            "size_kb": round(f.stat().st_size / 1024, 1),
        })
    return result


# ---------------------------------------------------------------------------
# Markdown builder
# ---------------------------------------------------------------------------

def build_markdown(packet: dict) -> str:
    date = packet["date"]
    git = packet.get("git_status", {})
    log = packet.get("log", {})
    tests = packet.get("tests", {})
    issues = packet.get("known_issues", [])
    backlog = packet.get("backlog_top", [])
    outputs = packet.get("latest_outputs", [])
    stats = packet.get("overview_stats", {})

    lines = [
        f"# Daily Engineering Packet — {date}",
        "",
        f"> Branch: `{packet.get('branch', 'unknown')}` | "
        f"Generated: {packet['generated_at']}",
        "",
    ]

    # --- Git ---
    lines += ["## Git Status", ""]
    if not git.get("available"):
        lines += ["_(git not available)_", ""]
    else:
        mod = git.get("modified", 0)
        unt = git.get("real_untracked", 0)
        lines.append(f"**Modified:** {mod} files | **New/Untracked (non-.venv):** {unt} files")
        lines.append("")
        changed = [l for l in git.get("lines", []) if ".venv" not in l and "__pycache__" not in l]
        if changed:
            lines += ["```"]
            lines += changed[:20]
            lines += ["```", ""]

    # --- Commits ---
    commits = packet.get("recent_commits", [])
    lines += ["## Recent Commits", ""]
    if commits:
        for c in commits:
            lines.append(f"- `{c}`")
    else:
        lines.append("_(no commits found)_")
    lines.append("")

    # --- Tests ---
    lines += ["## Test Status", ""]
    if tests.get("status") == "pass":
        icon = "PASS"
        summary = f"{tests['ran']} ran, {tests['skipped']} skipped"
    elif tests.get("status") == "fail":
        icon = "FAIL"
        summary = f"{tests['ran']} ran | {tests['failed']} failures, {tests['errors']} errors"
    elif tests.get("status") == "error":
        icon = "ERROR"
        summary = "Test runner failed to start"
    else:
        icon = "UNKNOWN"
        summary = "Tests not run (use --skip-tests=false to enable)"
    lines.append(f"**{icon}** — {summary}")
    if tests.get("status") in ("fail", "error") and tests.get("tail"):
        lines += ["", "```", tests["tail"][-600:], "```"]
    lines.append("")

    # --- Log ---
    lines += ["## Recent Log Issues", ""]
    if not log.get("available"):
        lines.append("_(no log files found in logs/)_")
    else:
        freshness = "today" if log.get("is_today") else log.get("file", "?")
        lines.append(f"Log file: `{log.get('file')}` ({freshness})")
        issues_log = log.get("issues", [])
        if issues_log:
            lines += ["", "```"]
            lines += issues_log[:15]
            lines += ["```"]
        else:
            lines.append("No ERROR/WARNING lines in last 150 log lines.")
    lines.append("")

    # --- Known Issues ---
    lines += ["## Known Issues (repo scan)", ""]
    if issues:
        for iss in issues:
            f = iss.get("file", "?")
            lno = iss.get("line", "?")
            kind = iss.get("kind", "?")
            text = iss.get("text", "").strip()[:100]
            lines.append(f"- `{f}:{lno}` [{kind}] {text}")
    else:
        lines.append("None detected by repo scanner.")
    lines.append("")

    # --- Backlog ---
    lines += ["## Top Backlog Items", ""]
    if backlog:
        for i, item in enumerate(backlog, 1):
            impact = item.get("user_visible_impact", 0)
            effort = item.get("effort", 3)
            tag = item.get("area", "")
            lines.append(
                f"{i}. **[{tag}]** {item['title']} "
                f"_(impact={impact}, effort={effort})_"
            )
    else:
        lines.append("_(backlog/improvements.json not found)_")
    lines.append("")

    # --- Latest Outputs ---
    lines += ["## Latest Outputs (outputs/latest/)", ""]
    if outputs:
        today_files = [o for o in outputs if o["today"]]
        other_files = [o for o in outputs if not o["today"]]
        if today_files:
            lines.append("**Modified today:**")
            for o in today_files:
                lines.append(f"- `{o['file']}` — {o['modified']} ({o['size_kb']} KB)")
        if other_files:
            lines.append("")
            lines.append("**Older:**")
            for o in other_files[:8]:
                lines.append(f"- `{o['file']}` — {o['modified']}")
    else:
        lines.append("_(outputs/latest/ is empty or not found)_")
    lines.append("")

    # --- Repo Health ---
    lines += ["## Repo Health Summary", ""]
    if stats:
        lines += [
            f"- Entry points: {stats.get('entry_points', '?')}",
            f"- Important files tracked: {stats.get('important_files', '?')}",
            f"- Data models: {stats.get('data_models', '?')}",
            f"- Integrations: {stats.get('integrations', '?')}",
            f"- Known issues (scan): {stats.get('known_issues', '?')}",
            f"- Test count (last scan): {stats.get('test_count', '?')}",
        ]
    else:
        lines.append("_(run python -m tools.repo_overview first)_")
    lines.append("")

    lines += [
        "---",
        "",
        "**Next step:** Run `python -m tools.task_ranker` → `daily_update/proposed_task.md`",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(root: Path, out_dir: Path, skip_tests: bool = False) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()

    print(f"[daily_packet] Gathering signals for {now.date().isoformat()} ...")

    packet: dict = {
        "date": now.date().isoformat(),
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "branch": git_branch(root),
        "git_status": git_status(root),
        "recent_commits": git_log(root),
        "diff_stat": git_diff_stat(root),
        "log": read_recent_log(root),
        "known_issues": get_known_issues(root),
        "backlog_top": get_backlog_top(root),
        "latest_outputs": get_latest_outputs(root),
        "overview_stats": get_overview_stats(root),
    }

    if skip_tests:
        packet["tests"] = {"status": "skipped", "ran": 0, "failed": 0,
                           "errors": 0, "skipped": 0}
        print("[daily_packet] Tests skipped.")
    else:
        print("[daily_packet] Running tests (up to 120s) ...")
        packet["tests"] = run_tests(root)
        t = packet["tests"]
        print(f"[daily_packet] Tests: {t['status'].upper()} "
              f"({t['ran']} ran, {t['failed']} failed, {t['errors']} errors)")

    md = build_markdown(packet)
    md_path = out_dir / "daily_packet.md"
    md_path.write_text(md, encoding="utf-8")

    json_path = out_dir / "daily_packet.json"
    json_path.write_text(json.dumps(packet, indent=2, default=str), encoding="utf-8")

    print(f"[daily_packet] Written: {md_path}")
    print(f"[daily_packet] Written: {json_path}")
    return packet


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="Repo root (default: parent of tools/)")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="Output directory (default: <root>/daily_update)")
    parser.add_argument("--skip-tests", action="store_true",
                        help="Skip test execution (faster)")
    args = parser.parse_args()

    out_dir = args.out_dir or (args.root / "daily_update")
    run(args.root, out_dir, skip_tests=args.skip_tests)


if __name__ == "__main__":
    main()
