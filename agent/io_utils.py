"""
agent/io_utils.py — Safe I/O helpers for the AI agent layer.

Provides:
  read_json_safe(path)              → dict | None
  write_markdown_atomic(path, text) → None  (write-temp-then-replace)
  redact(text)                      → str   (strips secrets from any string)
  tail_latest_log(logs_dir, n)      → str   (last n lines of newest log file)
"""

import os
import re
import tempfile
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Secret redaction
# Patterns: apikey=VALUE, api_key=VALUE, ANTHROPIC_API_KEY=VALUE, password=VALUE
# Matches until whitespace, &, ", ', <, >, or newline.
# ---------------------------------------------------------------------------
_REDACT_RE = re.compile(
    r'((?:apikey|api_key|password|passwd|token|secret|ANTHROPIC_API_KEY|FMP_API_KEY'
    r'|ALPHA_VANTAGE_API_KEY|EMAIL_PASSWORD)=[^\s&"\'<>\n]+)',
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Replace secret=VALUE with secret=<REDACTED> in any string."""
    return _REDACT_RE.sub(
        lambda m: m.group(0).split("=")[0] + "=<REDACTED>",
        text,
    )


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def read_json_safe(path: Path) -> Optional[dict]:
    """
    Read and parse a JSON file.

    Returns the parsed dict on success, or None if the file does not exist
    or is not valid JSON.  Never raises.
    """
    try:
        return __import__("json").loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Atomic markdown write
# ---------------------------------------------------------------------------

def write_markdown_atomic(path: Path, content: str) -> None:
    """
    Write *content* to *path* atomically: write to a temp file in the same
    directory, then os.replace() to atomically swap it in.

    Creates parent directories if they do not exist.
    Redacts any secrets before writing.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = redact(content)
    # Write to a sibling temp file so os.replace is on the same filesystem
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=".~" + path.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(clean)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up orphan temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Log tailing
# ---------------------------------------------------------------------------

def tail_latest_log(logs_dir: Path, n: int = 80) -> str:
    """
    Return the last *n* lines of the most recently modified .log file in
    *logs_dir*.

    Looks for files matching ``*.log``.  Returns an informational message if
    no log files exist.
    """
    logs_dir = Path(logs_dir)
    if not logs_dir.exists():
        return "(logs directory not found)"

    log_files = sorted(
        logs_dir.glob("*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not log_files:
        return "(no log files found in logs/)"

    latest = log_files[0]
    try:
        raw = latest.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"(could not read {latest.name}: {exc})"

    lines = raw.splitlines()
    tail_lines = lines[-n:] if len(lines) > n else lines
    header = f"--- tail of {latest.name} (last {len(tail_lines)} lines) ---\n"
    return header + "\n".join(tail_lines)
