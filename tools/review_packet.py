#!/usr/bin/env python3
"""
Review packet generator — captures post-change state for human review.

Run this AFTER Claude (or any change) has modified the codebase.
It collects diffs, test results, and output changes to help you decide
whether to accept, revise, or reject the changes.

Usage:
    python -m tools.review_packet [--root PATH] [--out-dir PATH] [--task-id ID]
    python tools/review_packet.py

Output:
    daily_update/review_packet.md
    daily_update/review_packet.json
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).parent.parent

SENSITIVE_PATTERNS = [".env", "secret", "password", "api_key", "token", "credential"]

# Paths to skip in changed-file analysis (noisy / non-source)
SKIP_PATH_PREFIXES = (
    ".venv", ".venv/", "venv/",
    "__pycache__", "__pycache__/",
    "data/watchlist_cache/", "data/fmp_cache/",
    "outputs/", "logs/", "runs/",
    "daily_update/", "prompts/", "repo_overview/",
)
SKIP_EXTENSIONS = {".pyc", ".pyo", ".db-journal", ".db-wal"}


def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str]:
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


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def git_changed_files(root: Path) -> list[dict]:
    """Return list of changed files with status."""
    rc, out = _run(["git", "status", "--short"], root)
    if rc != 0:
        return []
    results = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        status = line[:2].strip()
        path = line[3:].strip()
        # Skip noise paths
        path_norm = path.replace("\\", "/")
        if any(path_norm.startswith(p) or ("/" + p.rstrip("/") + "/") in path_norm
               for p in SKIP_PATH_PREFIXES):
            continue
        ext = Path(path).suffix.lower()
        if ext in SKIP_EXTENSIONS:
            continue
        sensitive = any(pat in path.lower() for pat in SENSITIVE_PATTERNS)
        results.append({
            "status": status,
            "path": path,
            "sensitive": sensitive,
        })
    return results


def git_diff_summary(root: Path) -> dict:
    """Return diff stat and a truncated unified diff (no secrets)."""
    rc_stat, stat = _run(["git", "diff", "--stat", "HEAD"], root)
    rc_diff, diff = _run(["git", "diff", "HEAD", "--", "*.py", "*.json",
                          "*.txt", "*.md", "*.yaml"], root, timeout=30)

    # Truncate large diffs
    diff_lines = diff.splitlines()
    truncated = False
    if len(diff_lines) > 300:
        diff_lines = diff_lines[:300]
        truncated = True

    # Redact any lines that look like secrets
    safe_lines = []
    for line in diff_lines:
        low = line.lower()
        if any(pat in low for pat in SENSITIVE_PATTERNS):
            safe_lines.append(f"[REDACTED — line contained sensitive keyword]")
        else:
            safe_lines.append(line)

    return {
        "stat": stat[:600] if stat else "(no changes)",
        "diff": "\n".join(safe_lines),
        "truncated": truncated,
        "diff_lines": len(diff.splitlines()),
    }


def git_log_since_yesterday(root: Path) -> list[str]:
    rc, out = _run(["git", "log", "--oneline", "--since=1.day"], root)
    return out.splitlines() if rc == 0 else []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def run_tests(root: Path) -> dict:
    rc, out = _run(
        ["python", "-m", "unittest", "discover", "tests/", "-v"],
        root, timeout=120
    )
    lines = out.splitlines()
    ran = failed = errors = skipped = 0
    status = "unknown"

    import re
    for line in reversed(lines):
        if line.startswith("Ran "):
            m = re.search(r"Ran (\d+)", line)
            if m:
                ran = int(m.group(1))
        if line.strip() == "OK" or (line.startswith("OK") and "skipped" in line):
            status = "pass"
        elif "FAILED" in line:
            status = "fail"
            m = re.search(r"failures=(\d+)", line)
            if m:
                failed = int(m.group(1))
            m = re.search(r"errors=(\d+)", line)
            if m:
                errors = int(m.group(1))
        if "skipped" in line:
            m = re.search(r"skipped=(\d+)", line)
            if m:
                skipped = int(m.group(1))

    tail = "\n".join(lines[-20:]) if lines else out[:500]
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
# Output comparison
# ---------------------------------------------------------------------------

def scan_outputs(root: Path) -> list[dict]:
    """List outputs/latest/ files with modification times."""
    out_dir = root / "outputs" / "latest"
    if not out_dir.exists():
        return []
    now = datetime.datetime.now()
    recent_threshold = now - datetime.timedelta(hours=2)
    results = []
    for f in sorted(out_dir.iterdir()):
        if not f.is_file():
            continue
        mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime)
        results.append({
            "file": f.name,
            "modified": mtime.strftime("%Y-%m-%d %H:%M"),
            "recently_changed": mtime >= recent_threshold,
            "size_kb": round(f.stat().st_size / 1024, 1),
        })
    return results


# ---------------------------------------------------------------------------
# Risk assessment
# ---------------------------------------------------------------------------

def assess_risks(changed_files: list[dict], tests: dict) -> list[str]:
    risks = []
    sensitive = [f for f in changed_files if f["sensitive"]]
    if sensitive:
        risks.append(f"SENSITIVE files changed: {[f['path'] for f in sensitive]}")

    high_risk_patterns = [
        ("state_store.py", "SQLite schema/data changes — check migration safety"),
        ("email_digest.py", "Email logic changed — verify no unintended sends"),
        ("guardrails.py", "Guardrail changes — verify violation detection still works"),
        ("main.py", "Entry point changed — verify all run modes still work"),
        ("utils.py", "Config/utils changed — may affect all modules"),
        ("adjustment.py", "Portfolio adjustment logic changed — review recommendations"),
        ("scoring.py", "Scoring logic changed — review score outputs"),
        ("run_lock.py", "Run lock changed — verify no concurrent run issues"),
    ]
    changed_paths = {f["path"] for f in changed_files}
    for pattern, note in high_risk_patterns:
        if any(pattern in p for p in changed_paths):
            risks.append(note)

    if tests["status"] == "fail":
        risks.append(f"TESTS FAILING — {tests['failed']} failures, {tests['errors']} errors")
    elif tests["status"] == "error":
        risks.append("TEST RUNNER FAILED — investigate before accepting changes")

    if not risks:
        risks.append("No obvious risks detected — review diff manually to confirm.")
    return risks


# ---------------------------------------------------------------------------
# Markdown builder
# ---------------------------------------------------------------------------

def build_markdown(packet: dict, template_path: Path) -> str:
    now = packet["generated_at"]
    task_id = packet.get("task_id", "unknown")
    changed = packet.get("changed_files", [])
    diff = packet.get("diff_summary", {})
    tests = packet.get("tests", {})
    outputs = packet.get("outputs", [])
    risks = packet.get("risks", [])
    commits = packet.get("recent_commits", [])

    # Load template if available
    if template_path.exists():
        tmpl = template_path.read_text(encoding="utf-8")
        # Format changed files
        if changed:
            files_block = "\n".join(
                f"  [{f['status']}] {f['path']}" + (" ⚠ SENSITIVE" if f["sensitive"] else "")
                for f in changed
            )
        else:
            files_block = "  (no changes detected)"

        # Format diff
        diff_block = diff.get("stat", "(no stat)") or "(no changes)"
        if diff.get("truncated"):
            diff_block += f"\n\n(diff truncated at 300 lines; full diff has {diff['diff_lines']} lines)"

        # Format tests
        t = tests
        if t.get("status") == "pass":
            tests_block = f"PASS — {t['ran']} ran, {t['skipped']} skipped"
        elif t.get("status") == "fail":
            tests_block = (
                f"FAIL — {t['ran']} ran, {t['failed']} failures, {t['errors']} errors\n\n"
                f"```\n{t.get('tail', '')[-400:]}\n```"
            )
        else:
            tests_block = f"UNKNOWN — status={t.get('status')}, returncode={t.get('returncode')}"

        # Format output changes
        recently = [o for o in outputs if o["recently_changed"]]
        if recently:
            out_block = "\n".join(
                f"  * {o['file']} — {o['modified']} ({o['size_kb']} KB)"
                for o in recently
            )
        else:
            out_block = "  (no output files modified recently)"

        risks_block = "\n".join(f"  * {r}" for r in risks)

        return tmpl.format(
            generated_date=now,
            task_id=task_id,
            changed_files=files_block,
            diff_summary=diff_block,
            test_results=tests_block,
            output_changes=out_block,
            risks=risks_block,
            test_count=t.get("ran", "?"),
        )

    # Fallback: build from scratch
    lines = [f"# Change Review Packet — {now}", "", f"**Task:** {task_id}", ""]

    lines += ["## Changed Files", ""]
    if changed:
        for f in changed:
            sens = " ⚠ SENSITIVE" if f["sensitive"] else ""
            lines.append(f"- `[{f['status']}]` `{f['path']}`{sens}")
    else:
        lines.append("_(no changes detected)_")
    lines += ["", "## Diff Stat", "", f"```", diff.get("stat", ""), "```", ""]

    lines += ["## Test Results", ""]
    t = tests
    if t.get("status") == "pass":
        lines.append(f"**PASS** — {t['ran']} ran, {t['skipped']} skipped")
    else:
        lines.append(f"**{t.get('status', 'unknown').upper()}** — "
                     f"{t['ran']} ran, {t['failed']} failed, {t['errors']} errors")
    lines += ["", "## Risks", ""]
    for r in risks:
        lines.append(f"- {r}")
    lines += ["", "---", "", "Decision: [ ] ACCEPT  [ ] REVISE  [ ] REJECT"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(root: Path, out_dir: Path, task_id: str | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()
    generated_at = now.strftime("%Y-%m-%d %H:%M")

    # Try to get task_id from proposed_task.json if not provided
    if task_id is None:
        task_json = root / "daily_update" / "proposed_task.json"
        if task_json.exists():
            try:
                data = json.loads(task_json.read_text(encoding="utf-8"))
                top = data.get("top_task") or {}
                task_id = top.get("id", "unknown")
            except (json.JSONDecodeError, KeyError):
                task_id = "unknown"

    print(f"[review_packet] Gathering review signals for task: {task_id} ...")

    changed = git_changed_files(root)
    diff = git_diff_summary(root)
    print(f"[review_packet] Running tests (up to 120s) ...")
    tests = run_tests(root)
    outputs = scan_outputs(root)
    risks = assess_risks(changed, tests)
    commits = git_log_since_yesterday(root)

    packet = {
        "generated_at": generated_at,
        "task_id": task_id,
        "changed_files": changed,
        "diff_summary": {"stat": diff["stat"], "truncated": diff["truncated"],
                         "diff_lines": diff["diff_lines"]},
        "diff_full": diff["diff"],
        "tests": tests,
        "outputs": outputs,
        "risks": risks,
        "recent_commits": commits,
    }

    template_path = root / "templates" / "review_template.txt"
    md = build_markdown(packet, template_path)

    md_path = out_dir / "review_packet.md"
    md_path.write_text(md, encoding="utf-8")

    # JSON without the full diff (too large)
    json_data = {k: v for k, v in packet.items() if k != "diff_full"}
    json_path = out_dir / "review_packet.json"
    json_path.write_text(json.dumps(json_data, indent=2, default=str), encoding="utf-8")

    t = tests
    test_status = f"{t['status'].upper()} ({t['ran']} ran, {t['failed']} failed)"
    print(f"[review_packet] Tests: {test_status}")
    print(f"[review_packet] Changed files: {len(changed)}")
    print(f"[review_packet] Risks: {len(risks)}")
    print(f"[review_packet] Written: {md_path}")
    print(f"[review_packet] Written: {json_path}")
    return packet


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="Output directory (default: <root>/daily_update)")
    parser.add_argument("--task-id", type=str, default=None,
                        help="Task ID to label the review packet")
    args = parser.parse_args()

    out_dir = args.out_dir or (args.root / "daily_update")
    run(args.root, out_dir, task_id=args.task_id)


if __name__ == "__main__":
    main()
