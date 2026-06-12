"""Observe-only documentation auditor.

Scans docs against machine-readable sources of truth + git history. Reports
drift / dead-refs / cross-doc inconsistency / coverage gaps. Never recomputes
decisions; never mutates portfolio, allocation, scoring, or decision state.
"""
from __future__ import annotations

import glob as _glob
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
        return f"{float(value) * 100:.0f}"
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


# Calibrated 2026-06-01 against the real doc prose. Each anchor is authoritative
# ONLY in its doc_globs; its `pattern` has exactly one capture group = the
# documented value, and that value must render identically to resolve_source's
# formatted output for an in-sync doc.
#
# The structural caps below are documented in ALLOCATION_POLICY.md as decimals in
# canonical Markdown bullet lines, e.g. "- `concentration_cap = 0.60` — ...". The
# `^- \`name = ` bullet anchor is deliberate: it EXCLUDES the nearby
# "Pre-retune baseline: ... `concentration_cap = 0.40`" lines, so the auditor can
# never rewrite the historical baseline record (a false-positive that an
# unanchored pattern would cause).
#
# Intentionally NOT auto-fix anchors (would replace a correct statement with a
# wrong one — kept out of the registry until/unless reconciled by an operator):
#   * pipeline stage count — docs say "13-stage pipeline" (PIPELINE_RUNBOOK) and
#     "17-stage safe wrapper" (ARCHITECTURE) while daily_run_status.total=24
#     counts ALL wrapper steps; these are three different measures, not drift.
#   * FMP daily budget — DATA_AND_FMP_ENDPOINTS.md documents the client DEFAULT
#     (230) while fmp_budget_status.budget=500 is the configured LIVE value;
#     default != live, so auto-fixing would be wrong.
#   * AI monthly cap — AI_BUDGET.md shows `monthly_cost_limit_usd: null` as an
#     illustrative default, not a live figure.
ANCHOR_REGISTRY: list[Anchor] = [
    Anchor("concentration_cap", "outputs/latest/retune_impact.json",
           "current_snapshot.structural_caps.concentration_cap",
           ("docs/ALLOCATION_POLICY.md",),
           r"^- `concentration_cap = (\d+\.\d+)`", "float2"),
    Anchor("leverage_cap", "outputs/latest/retune_impact.json",
           "current_snapshot.structural_caps.leverage_cap",
           ("docs/ALLOCATION_POLICY.md",),
           r"^- `leverage_cap = (\d+\.\d+)`", "float2"),
    Anchor("sector_cap", "outputs/latest/retune_impact.json",
           "current_snapshot.allocation_engine.sector_cap",
           ("docs/ALLOCATION_POLICY.md",),
           r"^- `sector_cap = (\d+\.\d+)`", "float2"),
]


def _iter_doc_lines(root: str, glob_rel: str):
    p = Path(root) / glob_rel
    if not p.exists():
        return
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        yield i, line


_SOURCE_DIRS = ("portfolio_automation/", "watchlist_scanner/", "scanner/")


_DOC_PY_REF_RX = re.compile(r"`([\w/]+\.py)`")


def collect_documented_modules(root: str) -> set[str]:
    """Scan the docs corpus for backticked `*.py` references and return the set
    of cited module identifiers — each captured token plus its basename. Used to
    recognize modules documented in grouped/subsystem docs (e.g. a phase table in
    docs/NEXT_STAGE_IMPLEMENTATION.md) that do not have a per-module docs/<stem>.md.
    Pure-ish over the filesystem; the matching predicate lives in find_coverage_gaps."""
    cited: set[str] = set()
    for doc_path in _glob.glob(str(Path(root) / "docs" / "**" / "*.md"), recursive=True):
        try:
            text = Path(doc_path).read_text(encoding="utf-8")
        except OSError:
            continue
        for token in _DOC_PY_REF_RX.findall(text):
            cited.add(token)
            cited.add(Path(token).name)
    return cited


def find_coverage_gaps(changed_files: list[str], existing_doc_paths: set[str],
                       documented_modules: set[str] | None = None) -> list[Finding]:
    """Flag new source modules in changed_files that are undocumented. A module is
    covered if either a per-module docs/<stem>.md exists OR the module is cited in a
    grouped/subsystem doc (by full path or basename, via documented_modules).
    Pure function over the changed-file list + the set of existing doc paths + the
    set of doc-cited modules, so it is trivially testable; the git diff and the
    corpus scan are injected by the caller."""
    documented = documented_modules or set()
    findings: list[Finding] = []
    for f in changed_files:
        if not any(f.startswith(d) for d in _SOURCE_DIRS):
            continue
        if not f.endswith(".py") or f.startswith("tests/") or "/test_" in f:
            continue
        module = Path(f).stem
        if module.startswith("_"):
            continue
        expected_doc = f"docs/{module}.md"
        if expected_doc in existing_doc_paths:
            continue
        if f in documented or Path(f).name in documented:
            continue
        findings.append(Finding(
            dimension="coverage", severity="med", doc=expected_doc,
            detail=f"module {f} has no documentation at {expected_doc}",
            auto_fixable=False,
        ))
    return findings


_PY_REF_RX = re.compile(r"`((?:portfolio_automation|watchlist_scanner|scanner)/[\w/]+\.py)`")


def find_dead_refs(root: str) -> list[Finding]:
    """Flag `path/to/file.py` references in docs that no longer exist on disk."""
    findings: list[Finding] = []
    for doc_path in sorted(_glob.glob(str(Path(root) / "docs" / "**" / "*.md"), recursive=True)):
        rel_doc = str(Path(doc_path).relative_to(root))
        for lineno, line in _iter_doc_lines(root, rel_doc):
            for m in _PY_REF_RX.finditer(line):
                ref = m.group(1)
                if not (Path(root) / ref).exists():
                    findings.append(Finding(
                        dimension="dead_ref", severity="med", doc=rel_doc,
                        detail=f"references missing file {ref}", line=lineno,
                    ))
    return findings


def find_cross_doc_inconsistency(root: str, registry: list[Anchor] | None = None) -> list[Finding]:
    """For each anchor, collect the documented value seen across ALL its docs;
    flag when two docs disagree (independent of whether the source resolves).

    `registry` defaults to ANCHOR_REGISTRY; tests inject a custom list."""
    findings: list[Finding] = []
    for anchor in (registry if registry is not None else ANCHOR_REGISTRY):
        rx = re.compile(anchor.pattern, re.IGNORECASE)
        seen: dict[str, str] = {}
        for doc_rel in anchor.doc_globs:
            for _lineno, line in _iter_doc_lines(root, doc_rel):
                m = rx.search(line)
                if m:
                    seen[doc_rel] = m.group(1)
                    break
        if len(set(seen.values())) > 1:
            findings.append(Finding(
                dimension="consistency", severity="high", doc=", ".join(seen),
                detail=f"{anchor.name} disagrees across docs: {seen}",
                anchor=anchor.name,
            ))
    return findings


def find_drift(root: str, registry: list[Anchor] | None = None) -> list[Finding]:
    """Compare each anchor's documented value (in its authoritative docs) to its
    source of truth. Only emits a finding when the source resolves AND differs.

    `registry` defaults to ANCHOR_REGISTRY; tests inject a custom list."""
    findings: list[Finding] = []
    for anchor in (registry if registry is not None else ANCHOR_REGISTRY):
        expected = resolve_source(anchor, root)
        if expected is None:
            continue  # cannot prove drift -> never guess
        rx = re.compile(anchor.pattern, re.IGNORECASE)
        for doc_rel in anchor.doc_globs:
            for lineno, line in _iter_doc_lines(root, doc_rel):
                m = rx.search(line)
                if not m:
                    continue
                current = m.group(1)
                if current != expected:
                    findings.append(Finding(
                        dimension="drift", severity="med", doc=doc_rel,
                        detail=f"{anchor.name}: doc says {current}, source says {expected}",
                        auto_fixable=True, anchor=anchor.name,
                        current=current, expected=expected, line=lineno,
                    ))
    return findings


# ---------------------------------------------------------------------------
# Status assembler + artifact writer
# ---------------------------------------------------------------------------

_DISCLAIMER = (
    "Observe-only documentation audit. Reads docs + code + git; never "
    "recomputes decisions or mutates portfolio, allocation, scoring, or "
    "decision state."
)


def _finding_dict(f: Finding) -> dict:
    return {
        "dimension": f.dimension, "severity": f.severity, "doc": f.doc,
        "detail": f.detail, "auto_fixable": f.auto_fixable, "anchor": f.anchor,
        "current": f.current, "expected": f.expected, "line": f.line,
    }


def run_doc_audit(root: str, last_audited_sha: str | None,
                  changed_files: list[str], existing_doc_paths: set[str]) -> dict:
    """Assemble the full observe-only status dict. Pure over its inputs (the git
    range is resolved by the caller and injected as changed_files)."""
    findings: list[Finding] = []
    try:
        findings += find_drift(root)
        findings += find_dead_refs(root)
        findings += find_cross_doc_inconsistency(root)
        findings += find_coverage_gaps(
            changed_files, existing_doc_paths, collect_documented_modules(root))
    except Exception as exc:  # never abort the pipeline
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "observe_only": True, "schema_version": "1", "source": "doc_audit",
            "overall_status": "error", "error": str(exc), "findings": [],
            "auto_fix_candidates": [], "coverage_gaps": [],
            "disclaimer": _DISCLAIMER,
        }

    auto = [f for f in findings if f.auto_fixable]
    gaps = [f for f in findings if f.dimension == "coverage"]
    if gaps:
        status = "coverage_gap"
    elif auto or any(f.dimension == "drift" for f in findings):
        status = "drift"
    elif findings:
        status = "ok_with_warnings"
    else:
        status = "ok"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True, "schema_version": "1", "source": "doc_audit",
        "last_audited_sha": last_audited_sha,
        "overall_status": status,
        "findings": [_finding_dict(f) for f in findings],
        "auto_fix_candidates": [_finding_dict(f) for f in auto],
        "coverage_gaps": [_finding_dict(f) for f in gaps],
        "auto_fixes_applied": [],
        "disclaimer": _DISCLAIMER,
    }


def _anchor_by_name(name: str) -> Anchor | None:
    for a in ANCHOR_REGISTRY:
        if a.name == name:
            return a
    return None


def apply_auto_fix(finding: Finding, root: str) -> bool:
    """Replace ONLY the captured value on the finding's line, in-place. Returns
    True if a substitution was made. Refuses anything not auto_fixable."""
    if not finding.auto_fixable or finding.anchor is None or finding.line is None:
        return False
    anchor = _anchor_by_name(finding.anchor)
    if anchor is None or finding.expected is None:
        return False
    path = Path(root) / finding.doc
    if not path.resolve().is_relative_to(Path(root).resolve()):
        return False  # refuse to write outside the repo tree
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    idx = finding.line - 1
    if idx < 0 or idx >= len(lines):
        return False
    rx = re.compile(anchor.pattern, re.IGNORECASE)
    m = rx.search(lines[idx])
    if not m or m.group(1) != finding.current:
        return False  # line moved/changed since audit -> refuse
    start, end = m.start(1), m.end(1)
    lines[idx] = lines[idx][:start] + finding.expected + lines[idx][end:]
    path.write_text("".join(lines), encoding="utf-8")
    return True


def write_doc_audit_status(result: dict, root: str) -> str:
    """Write JSON + compact MD via OutputNamespace.LATEST. Returns the JSON path.

    Uses safe_write_json with base_dir=Path(root)/"outputs" so the file lands
    at <root>/outputs/latest/doc_audit_status.json — matching the LATEST
    namespace convention (LATEST.value == "latest", base_dir == "outputs").
    """
    from portfolio_automation.data_governance import OutputNamespace, safe_write_json

    base_dir = Path(root) / "outputs"
    json_path = safe_write_json(
        OutputNamespace.LATEST, "doc_audit_status.json", result, base_dir=base_dir
    )

    md_lines = [
        f"# Doc Audit — {result['generated_at'][:10]}",
        f"\n**Status:** {result['overall_status']}  ",
        f"**Findings:** {len(result['findings'])} "
        f"({len(result['auto_fix_candidates'])} auto-fixable)\n",
    ]
    for f in result["findings"]:
        md_lines.append(
            f"- [{f['severity']}] {f['dimension']} · {f['doc']} — {f['detail']}"
        )
    md_path = Path(json_path).parent / "doc_audit_status.md"
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    return str(json_path)
