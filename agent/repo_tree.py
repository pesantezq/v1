"""
agent/repo_tree.py — Compact repo tree for maintainer prompts.

Produces a depth-limited, exclusion-filtered directory listing as a plain
text string suitable for inclusion in an LLM prompt.

Usage:
    from agent.repo_tree import get_repo_tree
    tree_str = get_repo_tree(root, max_depth=3)
"""

from pathlib import Path

# Directories to skip entirely (never recurse into)
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
    ".idea",
    ".vscode",
}

# Sub-paths to skip (relative to repo root, matched as path prefixes)
_SKIP_SUBPATHS = {
    "outputs/history",
    "data/fmp_cache",
}

# File extensions to omit from listing (typically large/binary/generated)
_SKIP_EXTENSIONS = {
    ".xlsx",
    ".xls",
    ".db",
    ".lock",
    ".pyc",
    ".pyo",
    ".log",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".woff",
    ".woff2",
}

# Exact filenames to omit
_SKIP_FILES = {
    ".env",
    ".env.example",
    "*.pem",
    "*.key",
    "*.crt",
}


def get_repo_tree(root: Path, max_depth: int = 3) -> str:
    """
    Return a compact, readable directory tree for the repo at *root*.

    Args:
        root:      Absolute path to the repository root.
        max_depth: Maximum depth to recurse (root itself is depth 0).

    Returns:
        Multi-line string, one entry per line, formatted as::

            repo_root/
              agent/
                agent_runner.py
                bundle_builder.py
              data/
                drawdown_state.json
                price_cache.json
              main.py
              config.json
    """
    root = Path(root).resolve()
    lines: list[str] = [root.name + "/"]
    _collect(root, root, depth=0, max_depth=max_depth, lines=lines, indent="  ")
    return "\n".join(lines)


def _collect(
    base: Path,
    current: Path,
    depth: int,
    max_depth: int,
    lines: list[str],
    indent: str,
) -> None:
    if depth >= max_depth:
        return

    try:
        entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return

    for entry in entries:
        # Skip hidden entries
        if entry.name.startswith(".") and entry.name not in {".mcp.json"}:
            continue

        # Build relative path for sub-path checks
        try:
            rel = entry.relative_to(base)
            rel_str = str(rel).replace("\\", "/")
        except ValueError:
            rel_str = entry.name

        if entry.is_dir():
            if entry.name in _SKIP_DIRS:
                continue
            if any(rel_str == sp or rel_str.startswith(sp + "/") for sp in _SKIP_SUBPATHS):
                continue
            lines.append(indent + entry.name + "/")
            _collect(base, entry, depth + 1, max_depth, lines, indent + "  ")
        else:
            if entry.suffix.lower() in _SKIP_EXTENSIONS:
                continue
            if entry.name in _SKIP_FILES:
                continue
            lines.append(indent + entry.name)
