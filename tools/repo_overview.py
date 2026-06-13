#!/usr/bin/env python3
"""Repo intelligence report generator.

Inspects this repository and produces a high-signal architecture/context report
useful for future Claude Code prompts and developer onboarding.

Outputs:
    repo_overview/REPO_OVERVIEW.md   — human-readable architecture report
    repo_overview/repo_overview.json — machine-readable summary

Usage:
    python -m tools.repo_overview [--root PATH] [--out-dir PATH]
    python tools/repo_overview.py   [--root PATH] [--out-dir PATH]

Stdlib only, no third-party dependencies.
"""

import argparse
import ast
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

IGNORE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
    "htmlcov", "coverage", ".eggs", "repo_overview",
    "outputs", "logs", "data", "gui",
}

IGNORE_FILE_PATTERNS = [
    r"\.pyc$", r"\.pyo$", r"\.DS_Store$", r"\.egg-info",
    r"Thumbs\.db$",
]

# Keywords that raise a file's importance score
IMPORTANCE_KEYWORDS: dict[str, list[str]] = {
    "orchestration": ["main", "runner", "pipeline", "orchestrat", "workflow"],
    "state":         ["state", "store", "db", "sqlite", "database", "history", "cache"],
    "integration":   ["client", "api", "market", "fmp", "alpha_vantage", "email", "smtp"],
    "scanner":       ["scanner", "candidate", "watchlist", "universe", "sleeve"],
    "output":        ["output", "digest", "report", "memo", "file_output"],
    "config":        ["config", "settings", "utils", "constants"],
    "agent":         ["agent", "llm", "prompt", "bundle", "ollama", "claude"],
    "theme":         ["theme", "rss", "topic"],
    "core_logic":    ["scoring", "adjustment", "finance", "recommendation",
                      "portfolio", "contribution", "drawdown", "projection",
                      "guardrail"],
}

# Hard-coded tags for known files (inferred from names)
FILE_TAG_MAP: dict[str, list[str]] = {
    "main.py":                     ["orchestration", "entry_point"],
    "utils.py":                    ["config", "utility"],
    "config.json":                 ["config"],
    "scoring.py":                  ["core_logic"],
    "adjustment.py":               ["core_logic"],
    "finance_analyzer.py":         ["core_logic"],
    "recommendations.py":          ["core_logic"],
    "portfolio.py":                ["core_logic"],
    "contribution_engine.py":      ["core_logic"],
    "drawdown.py":                 ["core_logic"],
    "projections.py":              ["core_logic"],
    "guardrails.py":               ["core_logic"],
    "state_store.py":              ["state"],
    "run_lock.py":                 ["utility"],
    "market_data.py":              ["integration"],
    "fmp_client.py":               ["integration"],
    "email_digest.py":             ["output", "integration"],
    "email_reporter.py":           ["output", "integration"],
    "digest_builder.py":           ["output"],
    "file_output.py":              ["output"],
    "ml_advisor.py":               ["core_logic"],
    "ml_history.py":               ["state"],
    "api_budget.py":               ["utility"],
    "retirement.py":               ["core_logic"],
    "agent/agent_runner.py":       ["agent", "orchestration"],
    "agent/bundle_builder.py":     ["agent", "output"],
    "agent/llm_adapters.py":       ["agent", "integration"],
    "agent/prompts.py":            ["agent"],
    "agent/io_utils.py":           ["agent", "utility"],
    "agent/repo_tree.py":          ["utility"],
    "agent/mcp_agent_tools.py":    ["integration"],
    "scanner/candidate_scanner.py":["scanner", "core_logic"],
    "sleeve/spec_sleeve_allocator.py": ["scanner", "core_logic"],
    "universe/sp500.py":           ["scanner"],
    "theme_engine/theme_detector.py":  ["theme", "integration"],
    "theme_engine/theme_mapper.py":    ["theme", "core_logic"],
    "theme_engine/theme_store.py":     ["theme", "state"],
    "theme_engine/rss_collector.py":   ["theme", "integration"],
    "theme_engine/__main__.py":        ["entry_point"],
    "watchlist_scanner/__main__.py":   ["entry_point"],
    "watchlist_scanner/scanner.py":    ["scanner", "core_logic"],
    "watchlist_scanner/fundamentals_engine.py":  ["core_logic"],
    "watchlist_scanner/cache_manager.py":        ["state"],
    "stockbot_mcp_server.py":      ["integration", "orchestration"],
}

# ---------------------------------------------------------------------------
# File walking
# ---------------------------------------------------------------------------

def _should_ignore(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    parts = set(rel.parts)
    if parts & IGNORE_DIRS:
        return True
    name = path.name
    for pat in IGNORE_FILE_PATTERNS:
        if re.search(pat, name):
            return True
    return False


def walk_repo(root: Path) -> list[Path]:
    """Return all non-ignored files under root."""
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        # Prune ignored directories in-place
        dirnames[:] = [
            d for d in dirnames
            if d not in IGNORE_DIRS and not d.startswith(".")
        ]
        for fname in filenames:
            fpath = dp / fname
            if not _should_ignore(fpath, root):
                results.append(fpath)
    return sorted(results)


def walk_python(root: Path) -> list[Path]:
    return [f for f in walk_repo(root) if f.suffix == ".py"]


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

def _safe_parse(path: Path) -> Optional[ast.Module]:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        return ast.parse(src, filename=str(path))
    except SyntaxError:
        return None


def _get_imports(tree: ast.Module) -> list[str]:
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
    return imports


def _get_classes(tree: ast.Module) -> list[dict]:
    classes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            is_dc = any(
                (isinstance(d, ast.Name) and d.id == "dataclass") or
                (isinstance(d, ast.Attribute) and d.attr == "dataclass")
                for d in node.decorator_list
            )
            bases = [
                (b.id if isinstance(b, ast.Name) else
                 b.attr if isinstance(b, ast.Attribute) else "?")
                for b in node.bases
            ]
            methods = [
                n.name for n in node.body
                if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
            ]
            fields: list[str] = []
            if is_dc:
                for item in node.body:
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        fields.append(item.target.id)
            classes.append({
                "name": node.name,
                "is_dataclass": is_dc,
                "bases": bases,
                "methods": methods[:10],
                "fields": fields,
            })
    return classes


def _get_top_functions(tree: ast.Module) -> list[str]:
    return [
        n.name for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
    ][:20]


def _get_constants(tree: ast.Module) -> list[tuple[str, str]]:
    results = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    val = ast.unparse(node.value)[:80] if hasattr(ast, "unparse") else "..."
                    results.append((t.id, val))
    return results[:20]


def _get_env_vars(tree: ast.Module) -> list[str]:
    env_vars = []
    src = ast.unparse(tree) if hasattr(ast, "unparse") else ""
    # Match os.environ["KEY"] or os.getenv("KEY") or os.environ.get("KEY")
    for m in re.finditer(r'(?:os\.environ\[|os\.getenv\(|os\.environ\.get\()["\']([A-Z][A-Z0-9_]+)["\']', src):
        env_vars.append(m.group(1))
    return list(dict.fromkeys(env_vars))


def _get_sqlite_tables(src: str) -> list[str]:
    tables = []
    # Only match DDL/DML — not sheet names or arbitrary strings
    for m in re.finditer(
        r'(?:CREATE TABLE(?:\s+IF NOT EXISTS)?|INSERT\s+INTO|UPDATE)\s+"?(\w+)"?',
        src, re.IGNORECASE
    ):
        t = m.group(1)
        skip = {"SELECT", "FROM", "WHERE", "SET", "VALUES", "OR", "IF", "NOT", "EXISTS"}
        if t.upper() not in skip and not t[0].isupper():
            tables.append(t)
    # Also capture table names from .execute() string literals (common sqlite3 pattern)
    for m in re.finditer(r'FROM\s+(\w+)\b', src, re.IGNORECASE):
        t = m.group(1)
        skip = {"SELECT", "WHERE", "JOIN", "ON", "AND", "OR", "NOT", "NULL", "TABLE"}
        if t.upper() not in skip and t.islower() and len(t) > 2:
            tables.append(t)
    return list(dict.fromkeys(tables))


def _get_json_files(src: str) -> list[str]:
    return list(dict.fromkeys(
        re.findall(r'["\']([a-zA-Z0-9_/\\.-]+\.json)["\']', src)
    ))[:15]


def _get_todo_fixme(path: Path) -> list[dict]:
    results = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # Only match in actual comments (lines starting with # or containing # before the keyword)
            if "#" not in stripped and not stripped.startswith(("//", "/*", "*")):
                continue
            # Avoid matching regex pattern strings (lines with re. or r' patterns)
            if "re.search" in stripped or "re.findall" in stripped or "re.compile" in stripped:
                continue
            # Require standalone marker — not part of $X,XXX dollar placeholders
            m = re.search(r'#\s*(?:type:\s*ignore.*)?(?<![,$])\b(TODO|FIXME|HACK|XXX|BROKEN|DEPRECATED|TEMPORARY)\b(?!\w)[:\s]*(.*)', line)
            if m:
                results.append({
                    "file": str(path),
                    "line": i,
                    "kind": m.group(1).upper(),
                    "text": m.group(2).strip()[:120],
                })
    except Exception:
        pass
    return results


def _get_api_urls(src: str) -> list[str]:
    urls = []
    for m in re.finditer(r'https?://[^\s\'"<>{}|\\^`\[\]]+', src):
        u = m.group(0).rstrip(".,;)")
        if any(skip in u for skip in ["github.com", "pypi.org", "docs.", "example.com", "localhost"]):
            continue
        urls.append(u)
    return list(dict.fromkeys(urls))[:10]


# ---------------------------------------------------------------------------
# File importance scoring
# ---------------------------------------------------------------------------

def score_file(rel_path: str) -> int:
    """Return an importance score 0-100 for a file."""
    score = 0
    name = rel_path.lower().replace("\\", "/")
    # Entry point bonus
    if re.search(r"(^|/)main\.py$", name):
        score += 40
    if "__main__.py" in name:
        score += 30
    if re.search(r"(^|/)app\.py$", name):
        score += 25
    # Keyword scoring
    for tag, keywords in IMPORTANCE_KEYWORDS.items():
        for kw in keywords:
            if kw in name:
                score += 12
                break
    # Test files — important but separate
    if "test" in name:
        score += 10
    # Config files
    if name.endswith(".json") and "config" in name:
        score += 20
    if name.endswith(".env") or name.endswith(".env.template"):
        score += 15
    if name == "requirements.txt":
        score += 10
    if name.endswith("readme.md"):
        score += 15
    return min(score, 100)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def build_entry_points(root: Path, py_files: list[Path]) -> list[dict]:
    entries = []
    for f in py_files:
        rel = str(f.relative_to(root)).replace("\\", "/")
        tree = _safe_parse(f)
        if tree is None:
            continue
        # Check for argparse / __main__ guard
        has_argparse = any(
            (isinstance(n, ast.Name) and "argparse" in ast.unparse(n)) or
            (isinstance(n, ast.Attribute) and n.attr in ("ArgumentParser", "add_argument"))
            for n in ast.walk(tree)
        )
        has_main_guard = any(
            (isinstance(n, ast.If) and
             ast.unparse(n.test).strip() in ('__name__ == "__main__"', "__name__ == '__main__'"))
            for n in ast.walk(tree)
        )
        is_dunder_main = f.name == "__main__.py"
        if has_argparse or has_main_guard or is_dunder_main:
            # Extract run modes if present
            src = f.read_text(encoding="utf-8", errors="replace")
            run_modes = re.findall(r"'(daily|weekly|monthly|batch|interactive|test)'", src)
            run_modes = list(dict.fromkeys(run_modes))
            entries.append({
                "path": rel,
                "has_argparse": has_argparse,
                "is_package_main": is_dunder_main,
                "run_modes": run_modes,
            })
    return entries


def build_important_files(root: Path, py_files: list[Path]) -> list[dict]:
    scored = []
    for f in py_files:
        rel = str(f.relative_to(root)).replace("\\", "/")
        s = score_file(rel)
        if s < 10:
            continue
        tree = _safe_parse(f)
        classes = _get_classes(tree) if tree else []
        funcs = _get_top_functions(tree) if tree else []
        # Determine tags
        tags = FILE_TAG_MAP.get(rel, [])
        if not tags:
            for tag, keywords in IMPORTANCE_KEYWORDS.items():
                for kw in keywords:
                    if kw in rel.lower():
                        tags.append(tag)
                        break
        # Brief purpose from docstring
        purpose = ""
        if tree:
            ds = ast.get_docstring(tree)
            if ds:
                purpose = ds.splitlines()[0][:120]
        scored.append({
            "path": rel,
            "score": s,
            "tags": list(dict.fromkeys(tags)) or ["utility"],
            "purpose": purpose,
            "key_classes": [c["name"] for c in classes][:6],
            "key_functions": funcs[:8],
        })
    return sorted(scored, key=lambda x: -x["score"])


def build_module_relationships(root: Path, important_paths: list[str]) -> list[dict]:
    """Build import-relationship map between important files."""
    # Map module name -> rel path
    path_to_module: dict[str, str] = {}
    for rel in important_paths:
        mod = rel.replace("/", ".").replace("\\", ".").removesuffix(".py")
        # Also short name (last component without .py)
        short = Path(rel).stem
        path_to_module[short] = rel
        path_to_module[mod] = rel

    relationships = []
    for rel in important_paths:
        if not rel.endswith(".py"):
            continue
        fpath = root / rel
        tree = _safe_parse(fpath)
        if tree is None:
            continue
        imports = _get_imports(tree)
        resolved = []
        for imp in imports:
            short = imp.split(".")[-1]
            if short in path_to_module and path_to_module[short] != rel:
                resolved.append(path_to_module[short])
            elif imp in path_to_module and path_to_module[imp] != rel:
                resolved.append(path_to_module[imp])
        if resolved:
            relationships.append({
                "file": rel,
                "imports": list(dict.fromkeys(resolved))[:12],
            })
    return relationships


def build_data_models(root: Path, py_files: list[Path]) -> list[dict]:
    models = []
    for f in py_files:
        rel = str(f.relative_to(root)).replace("\\", "/")
        tree = _safe_parse(f)
        if tree is None:
            continue
        for cls in _get_classes(tree):
            if cls["is_dataclass"] or "NamedTuple" in cls["bases"] or "TypedDict" in cls["bases"]:
                models.append({
                    "name": cls["name"],
                    "file": rel,
                    "kind": "dataclass" if cls["is_dataclass"] else
                             "NamedTuple" if "NamedTuple" in cls["bases"] else
                             "TypedDict" if "TypedDict" in cls["bases"] else "class",
                    "fields": cls["fields"],
                    "bases": cls["bases"],
                })
            # Also pick up classes that look like data carriers (many fields, no logic)
            elif len(cls["fields"]) >= 3 and len(cls["methods"]) <= 4:
                models.append({
                    "name": cls["name"],
                    "file": rel,
                    "kind": "class",
                    "fields": cls["fields"],
                    "bases": cls["bases"],
                })
    return models


def build_state_storage(root: Path, py_files: list[Path]) -> dict:
    sqlite_tables: list[str] = []
    sqlite_files: list[str] = []
    json_state_files: list[str] = []
    schema_owners: list[str] = []

    for f in py_files:
        rel = str(f.relative_to(root)).replace("\\", "/")
        try:
            src = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        tables = _get_sqlite_tables(src)
        if tables:
            sqlite_tables.extend(tables)
            schema_owners.append(rel)
        # DB file references
        for m in re.finditer(r'["\']([a-zA-Z0-9_/\\.-]+\.db)["\']', src):
            sqlite_files.append(m.group(1))
        # JSON state files (exclude cache/config/output)
        for jf in _get_json_files(src):
            if any(kw in jf for kw in ["state", "history", "lock", "seen", "counter",
                                        "watchlist", "watchlist_cache", "drawdown",
                                        "last_success", "approved"]):
                json_state_files.append(jf)

    return {
        "sqlite_tables": list(dict.fromkeys(sqlite_tables)),
        "sqlite_files": list(dict.fromkeys(sqlite_files)),
        "json_state_files": list(dict.fromkeys(json_state_files)),
        "schema_owners": list(dict.fromkeys(schema_owners)),
    }


def build_config_map(root: Path, py_files: list[Path]) -> dict:
    env_vars: list[str] = []
    constants: list[dict] = []
    config_files: list[str] = []
    feature_flags: list[str] = []

    # Collect env vars from all Python files
    for f in py_files:
        tree = _safe_parse(f)
        if tree:
            evars = _get_env_vars(tree)
            env_vars.extend(evars)

    # Collect constants from key files
    key_files = ["utils.py", "watchlist_scanner/config.py", "fmp_client.py", "api_budget.py"]
    for kf in key_files:
        fpath = root / kf
        if fpath.exists():
            tree = _safe_parse(fpath)
            if tree:
                for name, val in _get_constants(tree):
                    constants.append({"name": name, "value": val, "file": kf})

    # Find config files
    for ext in ["*.json", "*.toml", "*.ini", "*.cfg", "*.yaml", "*.yml"]:
        for cf in root.glob(ext):
            if not _should_ignore(cf, root):
                config_files.append(str(cf.relative_to(root)).replace("\\", "/"))

    # Sniff feature flags from config.json
    cfg_path = root / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            for key, val in cfg.items():
                if isinstance(val, dict) and "enabled" in val:
                    feature_flags.append(f"{key}.enabled = {val['enabled']}")
        except Exception:
            pass

    # .env file
    env_path = root / ".env"
    env_template_path = root / ".env.template"
    env_keys: list[str] = []
    for ep in [env_path, env_template_path]:
        if ep.exists():
            try:
                for line in ep.read_text(encoding="utf-8").splitlines():
                    m = re.match(r"^([A-Z][A-Z0-9_]+)\s*=", line.strip())
                    if m:
                        env_keys.append(m.group(1))
            except Exception:
                pass

    return {
        "config_files": config_files,
        "env_vars_in_code": list(dict.fromkeys(env_vars)),
        "env_file_keys": list(dict.fromkeys(env_keys)),
        "feature_flags": feature_flags,
        "constants_sample": constants[:20],
    }


def build_integrations(root: Path, py_files: list[Path]) -> list[dict]:
    integrations = []

    patterns = [
        {
            "name": "Alpha Vantage API",
            "keywords": ["alphavantage", "alpha_vantage", "ALPHA_VANTAGE_API_KEY", "TIME_SERIES"],
            "files": ["market_data.py"],
            "purpose": "Market data (prices, news sentiment, company overview)",
            "auth": "ALPHA_VANTAGE_API_KEY env var",
            "optional": False,
        },
        {
            "name": "Financial Modeling Prep (FMP) API",
            "keywords": ["financialmodelingprep", "fmp", "FMP_API_KEY"],
            "files": ["fmp_client.py"],
            "purpose": "S&P 500 universe, bulk profiles, metrics",
            "auth": "FMP_API_KEY env var",
            "optional": True,
        },
        {
            "name": "Ollama (local LLM)",
            "keywords": ["ollama", "api/generate", "api/chat"],
            "files": ["agent/llm_adapters.py", "theme_engine/theme_detector.py"],
            "purpose": "Daily/weekly AI narrative generation and theme detection",
            "auth": "None (local)",
            "optional": True,
        },
        {
            "name": "Anthropic Claude API",
            "keywords": ["anthropic", "claude", "ANTHROPIC_API_KEY"],
            "files": ["agent/llm_adapters.py"],
            "purpose": "Monthly AI memos; maintainer patch generation",
            "auth": "ANTHROPIC_API_KEY env var",
            "optional": True,
        },
        {
            "name": "SMTP Email",
            "keywords": ["smtplib", "smtp", "SMTP", "EMAIL_USER", "EMAIL_PASSWORD"],
            "files": ["email_reporter.py", "email_digest.py"],
            "purpose": "Sends portfolio digests and alerts",
            "auth": "EMAIL_USER / EMAIL_PASSWORD / SMTP_HOST env vars",
            "optional": True,
        },
        {
            "name": "MCP Server (Claude Code integration)",
            "keywords": ["mcp", "stockbot_mcp_server", "mcp_agent_tools"],
            "files": ["stockbot_mcp_server.py", "agent/mcp_agent_tools.py"],
            "purpose": "Exposes tools to Claude Code IDE sessions",
            "auth": "None (local)",
            "optional": True,
        },
    ]

    # Verify each integration actually appears in repo
    all_src = ""
    for f in py_files:
        try:
            all_src += f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass

    for p in patterns:
        present_files = [
            fp for fp in p["files"]
            if (root / fp).exists()
        ]
        if present_files or any(kw.lower() in all_src.lower() for kw in p["keywords"]):
            integrations.append({
                "name": p["name"],
                "files": present_files or p["files"],
                "purpose": p["purpose"],
                "auth": p["auth"],
                "optional": p["optional"],
            })

    return integrations


def build_outputs(root: Path, py_files: list[Path]) -> list[dict]:
    output_patterns = []
    for f in py_files:
        rel = str(f.relative_to(root)).replace("\\", "/")
        try:
            src = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # Find write_text / open for write patterns
        output_files = re.findall(
            r'["\']([a-zA-Z0-9_/\\.-]+\.(md|csv|txt|xlsx|json|html))["\']', src
        )
        for _, ext in output_files:
            pass
        if re.search(r"write_text|open\(.*['\"]w['\"]|\.write\(", src):
            # Grab .md/.csv/.txt/.xlsx file mentions
            outs = re.findall(
                r'["\']([a-zA-Z0-9_./-]+\.(md|csv|txt|xlsx))["\']', src
            )
            if outs:
                output_patterns.append({
                    "producer": rel,
                    "outputs": list(dict.fromkeys(o[0] for o in outs))[:8],
                })

    return output_patterns


def build_known_issues(root: Path, py_files: list[Path]) -> list[dict]:
    issues = []
    for f in py_files:
        issues.extend(_get_todo_fixme(f))
    return issues[:60]  # Cap at 60 items


def build_cadence(root: Path, py_files: list[Path]) -> dict:
    cadence_info: dict[str, Any] = {
        "run_modes": [],
        "scheduler_references": [],
        "mode_specific_behaviors": {},
    }
    for f in py_files:
        try:
            src = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rel = str(f.relative_to(root)).replace("\\", "/")
        if re.search(r"\bdaily\b|\bweekly\b|\bmonthly\b", src, re.IGNORECASE):
            modes = list(dict.fromkeys(
                re.findall(r"\b(daily|weekly|monthly|batch)\b", src, re.IGNORECASE)
            ))
            cadence_info["run_modes"] = list(dict.fromkeys(
                cadence_info["run_modes"] + [m.lower() for m in modes]
            ))
        if re.search(r"Task Scheduler|schtasks|crontab|cron\b", src, re.IGNORECASE):
            if "tools/" not in rel:  # don't self-report
                cadence_info["scheduler_references"].append(rel)
    return cadence_info


def build_tests(root: Path) -> dict:
    test_dir = root / "tests"
    test_files = []
    framework = "unittest"
    total_test_count = 0

    if test_dir.exists():
        for f in test_dir.glob("test_*.py"):
            rel = str(f.relative_to(root)).replace("\\", "/")
            try:
                src = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                src = ""
            count = len(re.findall(r"def test_\w+", src))
            total_test_count += count
            if "pytest" in src:
                framework = "pytest"
            test_files.append({"path": rel, "test_count": count})

    return {
        "test_dir": "tests/",
        "framework": framework,
        "test_files": test_files,
        "total_tests": total_test_count,
        "smoke_test_command": "python -m unittest discover tests/ -v",
    }


def build_safe_risky_zones(
    important_files: list[dict],
    relationships: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Infer safe and risky edit zones from coupling and tags."""
    # Count how many files import each file
    import_counts: dict[str, int] = {}
    for rel in relationships:
        for dep in rel.get("imports", []):
            import_counts[dep] = import_counts.get(dep, 0) + 1

    safe = []
    risky = []

    safe_tag_keywords = {"output", "utility", "agent", "theme"}
    risky_tag_keywords = {"orchestration", "state", "core_logic", "integration", "config"}

    for f in important_files:
        path = f["path"]
        tags = set(f.get("tags", []))
        in_degree = import_counts.get(path, 0)

        if "test" in path:
            continue

        if in_degree >= 4 or tags & risky_tag_keywords:
            reason_parts = []
            if in_degree >= 4:
                reason_parts.append(f"imported by {in_degree} modules")
            if tags & risky_tag_keywords:
                reason_parts.append(f"tags: {', '.join(tags & risky_tag_keywords)}")
            risky.append({
                "path": path,
                "reason": "; ".join(reason_parts) or "high coupling",
            })
        elif tags & safe_tag_keywords and in_degree <= 1:
            safe.append({
                "path": path,
                "reason": f"tags: {', '.join(tags & safe_tag_keywords)}; low coupling",
            })

    return safe, risky


def build_prompt_helpers(
    important_files: list[dict],
    integrations: list[dict],
    state_storage: dict,
) -> list[dict]:
    helpers = [
        {
            "scenario": "Changing email digest content or UX",
            "inspect_first": ["email_digest.py", "digest_builder.py", "email_reporter.py"],
            "notes": "Dedup is SHA-256 hash in state_store.py:email_history. "
                     "Anti-spam gating in email_digest.py.",
        },
        {
            "scenario": "Changing state persistence or schema",
            "inspect_first": ["state_store.py", "guardrails.py", "ml_history.py"],
            "notes": "SQLite DDL is in state_store.py. Tables: run_history, snapshots, "
                     "email_history, portfolio_peaks, theme_signals. "
                     "Any schema change needs migration or a fresh db.",
        },
        {
            "scenario": "Changing scoring or rebalancing logic",
            "inspect_first": ["scoring.py", "adjustment.py", "finance_analyzer.py", "recommendations.py"],
            "notes": "Scores are 0-100. Growth mode changes scoring weights — "
                     "check config.json growth_mode.mode. "
                     "Structural violations in guardrails.py gate actions.",
        },
        {
            "scenario": "Changing scanner / API budgeting",
            "inspect_first": ["fmp_client.py", "scanner/candidate_scanner.py",
                               "api_budget.py", "watchlist_scanner/cache_manager.py"],
            "notes": "FMP budget guard: 230 calls/day. AV budget: 20 calls/day. "
                     "Cache TTLs in watchlist_scanner/config.py. "
                     "Daily call counter persisted in data/watchlist_cache/call_counter.json.",
        },
        {
            "scenario": "Changing scheduler or run cadence",
            "inspect_first": ["main.py", "run_lock.py", "state_store.py"],
            "notes": "Run modes: daily|weekly|monthly via --run-mode flag. "
                     "Idempotency anchor: run_history table (run_id = YYYY-MM-DD_mode). "
                     "Lock file: data/run.lock (30-min stale threshold). "
                     "Task Scheduler setup in README.md.",
        },
        {
            "scenario": "Changing AI agent narrative / prompts",
            "inspect_first": ["agent/prompts.py", "agent/agent_runner.py",
                               "agent/llm_adapters.py", "agent/bundle_builder.py"],
            "notes": "LLM routing: daily/weekly → Ollama → Claude fallback. "
                     "monthly → Claude. Offline stub active when STOCKBOT_TESTING=1. "
                     "Bundle JSON is in outputs/latest/agent_bundle.json.",
        },
        {
            "scenario": "Changing theme engine or RSS collection",
            "inspect_first": ["theme_engine/theme_detector.py", "theme_engine/rss_collector.py",
                               "theme_engine/theme_mapper.py", "data/themes_catalog.json"],
            "notes": "Theme detection uses Ollama. testing_mode=True or STOCKBOT_TESTING=1 "
                     "returns MOCK_THEMES. Theme boosts are applied in "
                     "scanner/candidate_scanner.py:apply_theme_boosts().",
        },
        {
            "scenario": "Changing watchlist scanner behavior",
            "inspect_first": ["watchlist_scanner/__main__.py", "watchlist_scanner/scanner.py",
                               "watchlist_scanner/fundamentals_engine.py"],
            "notes": "3-component score: theme_news×0.45 + technical×0.30 + fundamentals×0.25. "
                     "Free-tier AV uses TIME_SERIES_DAILY (no adjusted close). "
                     "ETFs return empty OVERVIEW — handled gracefully.",
        },
        {
            "scenario": "Changing output file formats or paths",
            "inspect_first": ["file_output.py", "agent/io_utils.py", "main.py"],
            "notes": "outputs/latest/ is always overwritten. "
                     "outputs/history/YYYY-MM-DD/ is archived once per day. "
                     "Atomic writes use temp-then-rename pattern in agent/io_utils.py.",
        },
        {
            "scenario": "Adding a new config section or feature flag",
            "inspect_first": ["utils.py", "config.json"],
            "notes": "Config is a dataclass hierarchy in utils.py (Config → sub-configs). "
                     "Feature flags follow pattern: config.section.enabled (bool). "
                     "Always add defaults so old configs remain valid.",
        },
    ]
    return helpers


# ---------------------------------------------------------------------------
# High-level purpose inference
# ---------------------------------------------------------------------------

def infer_purpose(root: Path) -> dict:
    readme = root / "README.md"
    purpose_lines = []
    if readme.exists():
        lines = readme.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[:30]:
            if line.strip() and not line.startswith("#"):
                purpose_lines.append(line.strip())
                if len(purpose_lines) >= 3:
                    break

    main_doc = ""
    main_py = root / "main.py"
    if main_py.exists():
        tree = _safe_parse(main_py)
        if tree:
            main_doc = ast.get_docstring(tree) or ""

    return {
        "summary": (
            "Portfolio automation and decision-support system. "
            "Rules-based rebalancing, scoring (0-100), AI-assisted narrative generation, "
            "and watchlist scanning. Analysis-only — no broker API, no automated trades."
        ),
        "primary_runtime": "CLI (Python 3.12), Windows Task Scheduler, optional Streamlit GUI",
        "action_taking": False,
        "action_taking_note": (
            "Produces recommendations and emails only. "
            "No trades are placed. Emails require explicit SMTP config."
        ),
        "major_workflows": [
            "Daily: fetch prices → score → guardrails → recommendations → ML → email (if ACTION_REQUIRED)",
            "Weekly: same as daily + always send digest",
            "Monthly: same + contribution plan + Claude AI memo + CAGR projections",
            "Watchlist scan: standalone Alpha Vantage scan with fundamental/technical/theme scoring",
            "Theme engine: RSS ingestion → Ollama theme detection → candidate boosts",
        ],
        "readme_excerpt": " ".join(purpose_lines)[:300],
        "main_docstring": main_doc.splitlines()[0][:200] if main_doc else "",
    }


# ---------------------------------------------------------------------------
# Markdown report builder
# ---------------------------------------------------------------------------

def _fmt_tags(tags: list[str]) -> str:
    return " ".join(f"`{t}`" for t in tags)


def build_markdown(data: dict) -> str:
    lines = []
    ts = data["generated_at"]

    lines += [
        f"# Repo Overview: {data['repo_name']}",
        f"",
        f"> Generated {ts} by `tools/repo_overview.py`",
        f"",
        "---",
        "",
    ]

    # 1. Purpose
    p = data["high_level_purpose"]
    lines += [
        "## 1. High-Level Purpose",
        "",
        f"**Summary:** {p['summary']}",
        "",
        f"**Runtime:** {p['primary_runtime']}",
        "",
        f"**Action-taking:** {'Yes' if p['action_taking'] else 'No'} — {p['action_taking_note']}",
        "",
        "**Major workflows:**",
    ]
    for wf in p["major_workflows"]:
        lines.append(f"- {wf}")
    lines.append("")

    # 2. Entry points
    lines += ["## 2. Entry Points & Execution Flow", ""]
    for ep in data["entry_points"]:
        modes = ", ".join(ep["run_modes"]) if ep["run_modes"] else "n/a"
        kind = "package `__main__`" if ep["is_package_main"] else "script"
        lines.append(f"- **[{ep['path']}]({ep['path']})** ({kind}) — run modes: `{modes}`")
    lines.append("")
    lines += [
        "**Main execution flow** (inferred from `main.py`):",
        "",
        "1. Parse args → load `.env` → load `config.json` → acquire run lock",
        "2. Idempotency check (SQLite `run_history`)",
        "3. Fetch market prices (Alpha Vantage)",
        "4. Run guardrail checks",
        "5. Score holdings (0-100)",
        "6. Generate adjustments & recommendations",
        "7. Run ML advisor",
        "8. *Monthly only:* contribution engine + CAGR projections + scanner + theme boosts",
        "9. Write output files (CSV, Excel, markdown memos)",
        "10. Send email digest (if conditions met)",
        "11. Update SQLite state (snapshots, peaks, email history)",
        "12. Release run lock",
        "",
    ]

    # 3. Important files
    lines += ["## 3. Important Files & Modules", ""]
    lines += ["| File | Tags | Purpose |", "|------|------|---------|"]
    for f in data["important_files"][:30]:
        tags = _fmt_tags(f["tags"])
        purpose = f["purpose"][:80] if f["purpose"] else "—"
        lines.append(f"| [{f['path']}]({f['path']}) | {tags} | {purpose} |")
    lines.append("")

    # 4. Module relationships
    lines += ["## 4. Module Relationships", ""]
    # Find central orchestrators (high out-degree)
    sorted_rels = sorted(data["module_relationships"], key=lambda r: -len(r.get("imports", [])))
    for rel in sorted_rels[:15]:
        deps = ", ".join(f"`{d}`" for d in rel["imports"][:8])
        lines.append(f"- **`{rel['file']}`** → {deps}")
    lines.append("")

    # 5. Data models
    lines += ["## 5. Data Models", ""]
    seen_names: set[str] = set()
    for m in data["data_models"]:
        if m["name"] in seen_names:
            continue
        seen_names.add(m["name"])
        fields = ", ".join(f"`{f}`" for f in m["fields"][:8]) if m["fields"] else "—"
        lines.append(f"- **`{m['name']}`** ({m['kind']}) in `{m['file']}` — fields: {fields}")
    lines.append("")

    # 6. State & Storage
    s = data["state_storage"]
    lines += [
        "## 6. State & Storage",
        "",
        f"**SQLite file(s):** {', '.join(s['sqlite_files']) or 'data/portfolio.db (inferred)'}",
        "",
        "**Tables:**",
    ]
    for t in s["sqlite_tables"]:
        lines.append(f"- `{t}`")
    if not s["sqlite_tables"]:
        lines.append("- `run_history`, `snapshots`, `email_history`, `portfolio_peaks`, `theme_signals` (from memory)")
    lines += [
        "",
        "**JSON state files:**",
    ]
    for jf in s["json_state_files"]:
        lines.append(f"- `{jf}`")
    lines.append("")

    # 7. Config map
    cfg = data["config"]
    lines += [
        "## 7. Config Map",
        "",
        f"**Config files:** {', '.join(cfg['config_files'])}",
        "",
        "**Environment variables (from code):**",
    ]
    for ev in cfg["env_vars_in_code"]:
        lines.append(f"- `{ev}`")
    if cfg["env_file_keys"]:
        lines += ["", "**`.env` keys:**"]
        for k in cfg["env_file_keys"]:
            lines.append(f"- `{k}`")
    if cfg["feature_flags"]:
        lines += ["", "**Feature flags (from config.json):**"]
        for ff in cfg["feature_flags"]:
            lines.append(f"- `{ff}`")
    lines.append("")

    # 8. Integrations
    lines += ["## 8. External Integrations", ""]
    for intg in data["integrations"]:
        opt = "optional" if intg["optional"] else "required"
        lines.append(f"### {intg['name']} ({opt})")
        lines.append(f"- **Files:** {', '.join(intg['files'])}")
        lines.append(f"- **Purpose:** {intg['purpose']}")
        lines.append(f"- **Auth:** `{intg['auth']}`")
        lines.append("")

    # 9. Outputs
    lines += ["## 9. Output / Reporting Paths", ""]
    lines += [
        "| Directory | Contents |",
        "|-----------|----------|",
        "| `outputs/latest/` | Always-overwritten: CSV, Excel, markdown memos |",
        "| `outputs/history/YYYY-MM-DD/` | Daily archive (no duplicates) |",
        "| `logs/YYYY-MM-DD.log` | One log file per day (14-day retention) |",
        "| `data/` | Persistent state: SQLite, JSON caches, run lock |",
        "",
    ]
    lines += ["**Key output files:**"]
    key_outputs = [
        "outputs/latest/portfolio_snapshot.csv",
        "outputs/latest/recommendations.csv",
        "outputs/latest/contribution_plan.csv",
        "outputs/latest/compounding_dashboard.txt",
        "outputs/latest/decision_memo.md",
        "outputs/latest/monthly_memo.md",
        "outputs/latest/watchlist_summary.md",
        "outputs/latest/candidates_top20.csv",
        "outputs/latest/agent_bundle.json",
    ]
    for o in key_outputs:
        lines.append(f"- `{o}`")
    lines.append("")

    # 10. Cadence
    cad = data["cadence"]
    lines += [
        "## 10. Run Cadence",
        "",
        f"**Modes detected:** {', '.join(cad['run_modes'])}",
        "",
        "| Mode | Trigger | Key behaviors |",
        "|------|---------|---------------|",
        "| `daily` | Weekday mornings | Silent unless ACTION_REQUIRED; idempotent |",
        "| `weekly` | Sundays | Always sends full digest |",
        "| `monthly` | 1st of month | Contribution plan + Claude memo + scanner run |",
        "",
    ]
    if cad["scheduler_references"]:
        lines.append(f"**Task Scheduler references:** {', '.join(cad['scheduler_references'])}")
    lines.append("")

    # 11. Known issues
    lines += ["## 11. Known Issues / Technical Debt", ""]
    if data["known_issues"]:
        for issue in data["known_issues"][:25]:
            rel_file = issue["file"].replace("\\", "/")
            # make path relative if absolute
            for prefix in [str(Path.cwd()).replace("\\", "/"), "c:/PersonalWork/stock_bot/v1"]:
                rel_file = rel_file.replace(prefix + "/", "")
            lines.append(f"- **{issue['kind']}** [{rel_file}:{issue['line']}]({rel_file}#L{issue['line']}): {issue['text']}")
    else:
        lines.append("- No TODO/FIXME markers found.")
    lines.append("")

    # 12. Safe / Risky zones
    lines += [
        "## 12. Safe vs Risky Edit Zones",
        "",
        "> *Advisory only — inferred from import coupling and module tags.*",
        "",
        "### Safer for additive edits",
    ]
    for sz in data["safe_edit_zones"]:
        lines.append(f"- **[{sz['path']}]({sz['path']})** — {sz['reason']}")
    lines += ["", "### Higher risk — inspect carefully before editing"]
    for rz in data["risky_edit_zones"]:
        lines.append(f"- **[{rz['path']}]({rz['path']})** — {rz['reason']}")
    lines.append("")

    # 13. Tests
    t = data["tests"]
    lines += [
        "## 13. Tests",
        "",
        f"**Framework:** {t['framework']}",
        f"**Test directory:** `{t['test_dir']}`",
        f"**Total tests:** ~{t['total_tests']}",
        "",
        "**Smoke test:**",
        "```bash",
        t["smoke_test_command"],
        "```",
        "",
        "**Test files:**",
    ]
    for tf in t["test_files"]:
        lines.append(f"- `{tf['path']}` ({tf['test_count']} tests)")
    lines.append("")

    # 14. Prompt helpers
    lines += [
        "## 14. Prompt Helper — Where to Look First",
        "",
        "Use these pointers when writing future AI-assisted edit prompts.",
        "",
    ]
    for ph in data["prompt_helpers"]:
        files_str = ", ".join(f"[`{f}`]({f})" for f in ph["inspect_first"])
        lines.append(f"### {ph['scenario']}")
        lines.append(f"**Inspect first:** {files_str}")
        lines.append(f"**Notes:** {ph['notes']}")
        lines.append("")

    lines += ["---", f"*End of report — {ts}*", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run(root: Path, out_dir: Path) -> None:
    print(f"[repo_overview] Root: {root}")
    print(f"[repo_overview] Output: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    py_files = walk_python(root)
    print(f"[repo_overview] Found {len(py_files)} Python files (after ignore rules)")

    print("[repo_overview] Building sections...")

    purpose = infer_purpose(root)
    entry_points = build_entry_points(root, py_files)
    important_files = build_important_files(root, py_files)
    important_paths = [f["path"] for f in important_files]
    relationships = build_module_relationships(root, important_paths)
    data_models = build_data_models(root, py_files)
    state_storage = build_state_storage(root, py_files)
    config_map = build_config_map(root, py_files)
    integrations = build_integrations(root, py_files)
    outputs = build_outputs(root, py_files)
    cadence = build_cadence(root, py_files)
    known_issues = build_known_issues(root, py_files)
    safe_zones, risky_zones = build_safe_risky_zones(important_files, relationships)
    tests = build_tests(root)
    prompt_helpers = build_prompt_helpers(important_files, integrations, state_storage)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    data = {
        "repo_name": root.name,
        "generated_at": ts,
        "high_level_purpose": purpose,
        "entry_points": entry_points,
        "important_files": important_files,
        "module_relationships": relationships,
        "data_models": data_models,
        "state_storage": state_storage,
        "config": config_map,
        "integrations": integrations,
        "outputs": outputs,
        "cadence": cadence,
        "known_issues": known_issues,
        "safe_edit_zones": safe_zones,
        "risky_edit_zones": risky_zones,
        "tests": tests,
        "prompt_helpers": prompt_helpers,
    }

    # Write JSON
    json_path = out_dir / "repo_overview.json"
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"[repo_overview] Wrote {json_path}")

    # Write Markdown
    md_path = out_dir / "REPO_OVERVIEW.md"
    md_path.write_text(build_markdown(data), encoding="utf-8")
    print(f"[repo_overview] Wrote {md_path}")

    # Summary
    print(f"\n[repo_overview] Done.")
    print(f"  Entry points   : {len(entry_points)}")
    print(f"  Important files: {len(important_files)}")
    print(f"  Data models    : {len(data_models)}")
    print(f"  Integrations   : {len(integrations)}")
    print(f"  Known issues   : {len(known_issues)}")
    print(f"  Tests          : {tests['total_tests']} across {len(tests['test_files'])} files")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate repo architecture overview")
    parser.add_argument(
        "--root", type=Path,
        default=Path(__file__).parent.parent,
        help="Repo root (default: parent of tools/)"
    )
    parser.add_argument(
        "--out-dir", type=Path,
        default=None,
        help="Output directory (default: <root>/repo_overview/)"
    )
    args = parser.parse_args()
    root = args.root.resolve()
    out_dir = args.out_dir.resolve() if args.out_dir else root / "repo_overview"
    run(root, out_dir)


if __name__ == "__main__":
    main()
