"""FMP dependency inventory (FMP-1, research-only, offline).

Statically scans an EXPLICITLY supplied repo root for FMP data dependencies and
emits deterministic, sorted JSON. Purpose: evidence for the 2026-11-27 FMP
Starter -> Basic/Free downgrade cutover. Status is always NOT_READY: a static
scan cannot prove entitlement, rights, or data parity.

Safety contract:
  * never imports or executes scanned files (``ast.parse`` + text regex only);
  * no network, no FMP API, no environment/secret reads; ``.env*`` files are
    never opened; only ``*.py``, ``*.sh`` and a root ``config.json`` are read;
  * writes nothing unless ``--output`` is given explicitly.

Heuristic limits (reported, never hidden): method-name matches do not resolve
the receiver type, so a ``get_historical_prices`` call on a non-FMP provider is
still listed (``match="method_name"``). Non-constant endpoint paths, dynamic
``getattr`` and unparseable files are emitted as ``risk="unknown"``.

Usage:
    python tools/fmp_dependency_inventory.py --repo-root . [--include-tests]
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional

SCHEMA_VERSION = 1
STATUS = "NOT_READY"
STATUS_REASON = (
    "Static inventory only. Basic/Free entitlement, redistribution rights and "
    "data parity are UNVERIFIED; no live probe was performed."
)

# Method -> (risk category, legacy marker). Legacy markers cite fmp_client.py
# endpoint constants (_EP_V3_* / _EP_V4_*).
FMP_METHODS: Dict[str, tuple] = {
    "get_batch_quotes": ("quote", None),
    "get_quote_short": ("quote", None),
    "get_sp500_constituents": ("universe", "v3/sp500_constituent"),
    "get_bulk_profiles": ("universe", "v4/profile/all"),
    "get_batch_profiles_v3": ("universe", "v3/profile (stable/profile fallback)"),
    "get_batch_profiles": ("universe", None),
    "get_profile": ("universe", None),
    "get_full_market_universe": ("universe", None),
    "get_hybrid_universe": ("universe", None),
    "get_historical_prices": ("eod_history", None),
    "get_full_bars_strict_live": ("eod_history", None),
    "get_historical_prices_dividend_adjusted": ("adjusted_history_vs002", None),
    "get_dividend_adjusted_bars_strict_live": ("adjusted_history_vs002", None),
    "get_stock_news": ("news", None),
    "get_ratios": ("fundamentals", None),
    "get_key_metrics": ("fundamentals", None),
    "get_financial_growth": ("fundamentals", None),
    "get_income_statement": ("fundamentals", None),
    "get_fundamentals_v3": ("fundamentals", "v3/key-metrics fallback"),
    "get_bulk_key_metrics": ("fundamentals", "v4/key-metrics-bulk"),
    "get_earnings_calendar": ("fundamentals", None),
}

# Constructors / factories that create FMP-backed clients or providers.
CONSTRUCTORS = {
    "FMPClient": "client_construction",
    "GovernedFMPClient": "client_construction",
    "governed_client": "client_construction",
    "FMPMarketClient": "market_data_construction",
    "create_market_client": "market_data_construction",
    "FMPUniverse": "market_data_construction",
    "FMPDividendAdjustedProvider": "market_data_construction",
}

QUOTA_KWARGS = {"daily_budget", "call_budget", "retry_max"}
QUOTA_NAMES = {"CallBudgetExceeded", "CallCounter"}
# Generic path-taking methods -> index of the endpoint-path positional arg.
GENERIC_PATH_METHODS = {"get_json": 0, "_raw_get": 0, "_get_cached": 1}

_FMP_HOST = "financialmodeling" + "prep"  # split so this file never self-matches
_LEGACY_PATH_RE = re.compile(r"(^|/)(api/)?v[34]/")
_SH_RE = re.compile(
    r"FMP_API_KEY|fmp_\w+|FMPClient|" + _FMP_HOST + r"|data_budget", re.I)
_CONFIG_RE = re.compile(
    r'"(fmp_daily_calls_budget|call_budget|rate_per_min|burst|'
    r'monthly_bandwidth_gb|cache_only)"\s*:\s*([^,}\n]+)')

EXCLUDED_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__",
                 ".pytest_cache", ".mypy_cache", ".agent"}
# Data/artifact trees: excluded at the repo root only (gui_v2/data/ is code).
EXCLUDED_ROOT_DIRS = {"data", "outputs"}
SELF_REL = "tools/fmp_dependency_inventory.py"

# Key source files expected in Northstar; absence is reported, not fatal.
EXPECTED_SOURCES = (
    "fmp_client.py",
    "fmp_endpoint_registry.py",
    "market_data.py",
    "portfolio_automation/data_budget/factory.py",
    "portfolio_automation/vs002_evidence/production_runner.py",
    "scripts/preflight.sh",
    "scripts/run_monthly_universe_refresh.sh",
    "universe/fmp_universe.py",
)


def iter_source_files(root: Path, include_tests: bool = False) -> List[Path]:
    """Sorted *.py / *.sh files under root (plus root config.json); never .env*."""
    out: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        at_root = Path(dirpath) == root
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in EXCLUDED_DIRS and not d.startswith(".")
            and not (at_root and d in EXCLUDED_ROOT_DIRS)
            and (include_tests or d != "tests"))
        for name in filenames:
            if name.startswith(".env"):
                continue
            if name.endswith((".py", ".sh")):
                out.append(Path(dirpath) / name)
    cfg = root / "config.json"
    if cfg.is_file():
        out.append(cfg)
    return sorted(out, key=lambda p: p.relative_to(root).as_posix())


def _safe_detail(kind: str, detail: str) -> str:
    # Only inventory source identity, never unbounded source expressions:
    # URLs, shell expressions, receiver/kwarg literals may contain credentials.
    if kind in {"url_literal", "shell_dependency", "dynamic_dispatch",
                "direct_call", "quota_kwarg"}:
        return "<source_expression_elided>"
    if kind == "config_quota":
        return (detail[:160] if re.fullmatch(
            r"-?[0-9]+(?:\.[0-9]+)?|true|false|null", detail.strip(), re.I
        ) else "<config_value_elided>")
    if kind == "generic_endpoint_call":
        # Keep endpoint identity, not query values such as apikey or tokens.
        return detail.split("?", 1)[0][:130] + (
            "?<query_elided>" if "?" in detail else ""
        )
    return detail[:160]


def _finding(path: str, line: int, kind: str, risk: str, symbol: str,
             detail: str = "", match: str = "ast", legacy: Optional[str] = None
             ) -> dict:
    return {"path": path, "line": line, "kind": kind, "risk": risk,
            "symbol": symbol, "detail": _safe_detail(kind, detail), "match": match,
            "legacy_v3_v4": ("<legacy_endpoint_present>" if legacy and kind == "generic_endpoint_call" else legacy)}


def _callee_name(func: ast.AST) -> Optional[str]:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _module_str_constants(tree: ast.Module) -> Dict[str, str]:
    consts: Dict[str, str] = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            consts[node.targets[0].id] = node.value.value
    return consts


def _resolve_path_arg(arg: ast.AST, consts: Dict[str, str]) -> Optional[str]:
    """Best-effort static endpoint path; None means dynamic/UNKNOWN."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name) and arg.id in consts:
        return consts[arg.id]
    if isinstance(arg, ast.JoinedStr) and arg.values:
        head = arg.values[0]
        if isinstance(head, ast.FormattedValue) and isinstance(head.value, ast.Name):
            base = consts.get(head.value.id)
            return f"{base}/{{...}}" if base else None
        if isinstance(head, ast.Constant):
            return f"{head.value}{{...}}"
    return None


def _path_risk(path: str) -> str:
    p = path.lower()
    if "dividend-adjusted" in p:
        return "adjusted_history_vs002"
    if "historical-price" in p or "historical-chart" in p:
        return "eod_history"
    if "quote" in p:
        return "quote"
    if "news" in p:
        return "news"
    if any(k in p for k in ("sp500", "profile", "screener", "stock-list")):
        return "universe"
    if any(k in p for k in ("metric", "ratio", "growth", "statement", "earning")):
        return "fundamentals"
    return "other_endpoint"


def scan_python(rel: str, text: str) -> List[dict]:
    try:
        tree = ast.parse(text, filename=rel)
    except (SyntaxError, ValueError) as exc:
        return [_finding(rel, getattr(exc, "lineno", 0) or 0, "parse_error",
                         "unknown", "<unparseable>", type(exc).__name__,
                         match="none")]
    consts = _module_str_constants(tree)
    found: List[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and _FMP_HOST in node.value:
            legacy = "api/v3|v4 literal" if _LEGACY_PATH_RE.search(
                node.value.split(".com", 1)[-1].lstrip("/")) else None
            found.append(_finding(rel, node.lineno, "url_literal",
                                  _path_risk(node.value), "<url>",
                                  node.value, legacy=legacy))
        if isinstance(node, ast.Name) and node.id in QUOTA_NAMES:
            found.append(_finding(rel, node.lineno, "quota_reference",
                                  "quota_budget", node.id))
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node.func)
        if name is None:
            continue
        if name in FMP_METHODS and isinstance(node.func, ast.Attribute):
            risk, legacy = FMP_METHODS[name]
            found.append(_finding(rel, node.lineno, "direct_call", risk, name,
                                  ast.unparse(node.func.value)[:60],
                                  match="method_name", legacy=legacy))
        elif name in CONSTRUCTORS:
            found.append(_finding(rel, node.lineno, CONSTRUCTORS[name],
                                  "construction", name))
        elif (name in GENERIC_PATH_METHODS and isinstance(node.func, ast.Attribute)
              and _mentions_fmp(text)):
            idx = GENERIC_PATH_METHODS[name]
            path = (_resolve_path_arg(node.args[idx], consts)
                    if len(node.args) > idx else None)
            if path is None:
                found.append(_finding(rel, node.lineno, "dynamic_endpoint",
                                      "unknown", name,
                                      "endpoint path not statically resolvable",
                                      match="none"))
            else:
                legacy = path if _LEGACY_PATH_RE.search(path.lstrip("/")) else None
                found.append(_finding(rel, node.lineno, "generic_endpoint_call",
                                      _path_risk(path), name, path,
                                      legacy=legacy))
        elif name == "getattr" and len(node.args) >= 2 and not (
                isinstance(node.args[1], ast.Constant)) and any(
                k in ast.unparse(node.args[0]).lower() for k in ("fmp", "client")):
            found.append(_finding(rel, node.lineno, "dynamic_dispatch",
                                  "unknown", "getattr",
                                  ast.unparse(node)[:80], match="none"))
        for kw in node.keywords:
            if kw.arg in QUOTA_KWARGS:
                found.append(_finding(rel, node.lineno, "quota_kwarg",
                                      "quota_budget", f"{name}({kw.arg}=)",
                                      ast.unparse(kw.value)[:40]))
    return found


def _mentions_fmp(text: str) -> bool:
    return "fmp" in text.lower()


def scan_shell(rel: str, text: str) -> List[dict]:
    """Non-comment shell lines touching FMP. Only names are recorded — a line
    containing an assignment has its value elided so no secret can leak."""
    found: List[dict] = []
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _SH_RE.search(line)
        if not m:
            continue
        token = m.group(0)
        risk = "quota_budget" if "budget" in token.lower() else "shell_dependency"
        if token.upper() == "FMP_API_KEY":
            risk = "credential_presence_gate"
        found.append(_finding(rel, i, "shell_dependency", risk, token,
                              re.sub(r"=\S+", "=<elided>", line), match="regex"))
    return found


def scan_config(rel: str, text: str) -> List[dict]:
    found: List[dict] = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in _CONFIG_RE.finditer(line):
            found.append(_finding(rel, i, "config_quota", "quota_budget",
                                  m.group(1), m.group(2).strip(), match="regex"))
    return found


def _sort_key(f: dict) -> tuple:
    return (f["path"], f["line"], f["kind"], f["symbol"], f["detail"])


def build_inventory(repo_root: Path, include_tests: bool = False) -> dict:
    root = Path(repo_root)
    if not root.is_dir():
        return {"schema_version": SCHEMA_VERSION, "status": "ERROR",
                "error": "repo_root_not_found", "findings": []}
    root = root.resolve()
    findings: List[dict] = []
    files = iter_source_files(root, include_tests)
    for p in files:
        rel = p.relative_to(root).as_posix()
        if rel == SELF_REL:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if rel.endswith(".py"):
            findings.extend(scan_python(rel, text))
        elif rel.endswith(".sh"):
            findings.extend(scan_shell(rel, text))
        else:
            findings.extend(scan_config(rel, text))
    findings.sort(key=_sort_key)
    by_risk: Dict[str, int] = {}
    by_kind: Dict[str, int] = {}
    for f in findings:
        by_risk[f["risk"]] = by_risk.get(f["risk"], 0) + 1
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS,
        "status_reason": STATUS_REASON,
        "include_tests": include_tests,
        "files_scanned": len(files),
        "absent_expected_sources": sorted(
            s for s in EXPECTED_SOURCES if not (root / s).is_file()),
        "summary": {
            "by_risk": dict(sorted(by_risk.items())),
            "by_kind": dict(sorted(by_kind.items())),
            "legacy_v3_v4": sum(1 for f in findings if f["legacy_v3_v4"]),
            "unknown": by_risk.get("unknown", 0),
        },
        "findings": findings,
    }


def to_json(inv: dict) -> str:
    return json.dumps(inv, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", required=True,
                    help="repo root to scan (must be supplied explicitly)")
    ap.add_argument("--include-tests", action="store_true")
    ap.add_argument("--output", help="optional JSON output path")
    args = ap.parse_args(list(argv) if argv is not None else None)
    inv = build_inventory(Path(args.repo_root), args.include_tests)
    payload = to_json(inv)
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 2 if inv["status"] == "ERROR" else 0


if __name__ == "__main__":
    raise SystemExit(main())
