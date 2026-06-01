# Documentation Audit System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only documentation auditor that scans docs against code + git history, reports drift/coverage/clarity/consistency findings, and auto-fixes high-confidence factual drift under guardrails — runnable on-demand anywhere and on a VPS cron.

**Architecture:** Follows the repo's observe-only-producer → read-only-agent → orchestrator-skill → cron pattern. A pure-function producer (`doc_audit.py`) does deterministic checks and writes `outputs/latest/doc_audit_status.json`. A `/doc-audit` skill orchestrates the weekly pass and applies guardrailed auto-fixes. A new `portfolio-doc-auditor` agent handles judgment dimensions in the monthly tier. Cross-workstation state is git itself: the last-audited commit SHA lives in a committed `.agent/doc_audit_state.yaml`, and "what changed" is `git diff <sha>..HEAD`.

**Tech Stack:** Python 3.12, pytest, `portfolio_automation.data_governance.OutputNamespace`, PyYAML (for `.agent/*.yaml`), git CLI via `subprocess`.

---

## File Structure

| File | Responsibility | New/Modify |
|---|---|---|
| `portfolio_automation/doc_audit.py` | Observe-only producer: anchor registry, deterministic checks, status assembler, artifact writer, auto-fix primitives | Create |
| `portfolio_automation/doc_audit_state.py` | Read/write the committed `.agent/doc_audit_state.yaml`; resolve last-audited SHA + git diff range | Create |
| `tests/test_doc_audit.py` | Unit tests for all producer checks + auto-fix eligibility + degraded states | Create |
| `tests/test_doc_audit_state.py` | Unit tests for state read/write + git-range derivation | Create |
| `.agent/doc_audit_state.yaml` | Committed state: `last_audited_sha`, `last_run_at`, `apply_enabled`, fix counters | Create |
| `.claude/skills/doc-audit/SKILL.md` | Weekly orchestrator: run producer → auto-fix → dispatch writer → write state | Create |
| `.claude/skills/doc-audit-monthly/SKILL.md` | Monthly judgment retrospective orchestrator (report-only) | Create |
| `.claude/agents/portfolio-doc-auditor.md` | Read-only documentation lens (clarity/conciseness/redundancy) | Create |
| `.claude/commands/daily-tool-analysis.md` | Add `doc_audit_status.json` consumer line | Modify |
| `.claude/commands/monthly-tool-analysis.md` | Add doc-audit-monthly dispatch hook | Modify |
| `docs/DOC_AUDIT.md` | Module doc for the audit system | Create |
| `docs/CHANGELOG_DECISIONS.md` | Architecture entry | Modify |
| `.agent/project_state.yaml` | Record feature completion | Modify |

**Module API (defined once, used across tasks):**

```python
# portfolio_automation/doc_audit.py
from dataclasses import dataclass, field

@dataclass(frozen=True)
class Anchor:
    name: str                 # e.g. "pipeline_stage_count"
    source_artifact: str      # path rel to root, e.g. "outputs/latest/daily_run_status.json"
    source_json_path: str     # dotted, e.g. "stage_summary.total"
    doc_globs: tuple[str, ...]# docs where this anchor is authoritative, e.g. ("docs/PIPELINE_RUNBOOK.md",)
    pattern: str              # regex with exactly ONE capture group = the documented value
    fmt: str = "int"          # how to render source value: "int" | "float2" | "pct1" | "usd0"

@dataclass
class Finding:
    dimension: str            # "drift" | "dead_ref" | "consistency" | "coverage"
    severity: str             # "low" | "med" | "high"
    doc: str
    detail: str
    auto_fixable: bool = False
    anchor: str | None = None
    current: str | None = None      # value currently in the doc
    expected: str | None = None     # value from source of truth
    line: int | None = None
```

---

## Task 1: Anchor registry + source resolution

**Files:**
- Create: `portfolio_automation/doc_audit.py`
- Test: `tests/test_doc_audit.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_doc_audit.py
import json
from pathlib import Path
import pytest
from portfolio_automation import doc_audit


def _write(root: Path, rel: str, content: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_resolve_source_reads_nested_json_value(tmp_path):
    _write(tmp_path, "outputs/latest/daily_run_status.json",
           json.dumps({"stage_summary": {"total": 24}}))
    anchor = doc_audit.Anchor(
        name="pipeline_stage_count",
        source_artifact="outputs/latest/daily_run_status.json",
        source_json_path="stage_summary.total",
        doc_globs=("docs/PIPELINE_RUNBOOK.md",),
        pattern=r"(\d+)\s+pipeline stages",
        fmt="int",
    )
    assert doc_audit.resolve_source(anchor, str(tmp_path)) == "24"


def test_resolve_source_returns_none_when_artifact_missing(tmp_path):
    anchor = doc_audit.Anchor(
        name="x", source_artifact="outputs/latest/missing.json",
        source_json_path="a.b", doc_globs=("docs/X.md",), pattern=r"(\d+)",
    )
    assert doc_audit.resolve_source(anchor, str(tmp_path)) is None


def test_registry_is_non_empty_and_well_formed():
    assert len(doc_audit.ANCHOR_REGISTRY) >= 6
    for a in doc_audit.ANCHOR_REGISTRY:
        assert a.pattern.count("(") >= 1  # at least one capture group
        assert a.fmt in {"int", "float2", "pct1", "usd0"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_doc_audit.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_automation.doc_audit'`

- [ ] **Step 3: Write minimal implementation**

```python
# portfolio_automation/doc_audit.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_doc_audit.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/doc_audit.py tests/test_doc_audit.py
git commit -m "feat(doc-audit): anchor registry + source resolution"
```

---

## Task 2: Drift detection

**Files:**
- Modify: `portfolio_automation/doc_audit.py`
- Test: `tests/test_doc_audit.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_doc_audit.py
def test_find_drift_flags_mismatch_and_marks_auto_fixable(tmp_path):
    _write(tmp_path, "outputs/latest/daily_run_status.json",
           json.dumps({"stage_summary": {"total": 24}}))
    _write(tmp_path, "docs/PIPELINE_RUNBOOK.md",
           "The wrapper runs 17 pipeline stages end to end.\n")
    findings = doc_audit.find_drift(str(tmp_path))
    drift = [f for f in findings if f.anchor == "pipeline_stage_count"]
    assert len(drift) == 1
    assert drift[0].current == "17" and drift[0].expected == "24"
    assert drift[0].auto_fixable is True
    assert drift[0].dimension == "drift"


def test_find_drift_silent_when_doc_matches_source(tmp_path):
    _write(tmp_path, "outputs/latest/daily_run_status.json",
           json.dumps({"stage_summary": {"total": 24}}))
    _write(tmp_path, "docs/PIPELINE_RUNBOOK.md", "Runs 24 pipeline stages.\n")
    assert doc_audit.find_drift(str(tmp_path)) == []


def test_find_drift_reports_only_when_source_unresolvable(tmp_path):
    # No source artifact -> cannot prove drift -> no finding (never guesses)
    _write(tmp_path, "docs/PIPELINE_RUNBOOK.md", "Runs 17 pipeline stages.\n")
    assert doc_audit.find_drift(str(tmp_path)) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_doc_audit.py -k drift -q`
Expected: FAIL — `AttributeError: module 'portfolio_automation.doc_audit' has no attribute 'find_drift'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to portfolio_automation/doc_audit.py
def _iter_doc_lines(root: str, glob_rel: str):
    p = Path(root) / glob_rel
    if not p.exists():
        return
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        yield i, line


def find_drift(root: str) -> list[Finding]:
    """Compare each anchor's documented value (in its authoritative docs) to its
    source of truth. Only emits a finding when the source resolves AND differs."""
    findings: list[Finding] = []
    for anchor in ANCHOR_REGISTRY:
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_doc_audit.py -k drift -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/doc_audit.py tests/test_doc_audit.py
git commit -m "feat(doc-audit): factual-drift detection bound to anchor sources"
```

---

## Task 3: Coverage-gap detection from a git range

**Files:**
- Modify: `portfolio_automation/doc_audit.py`
- Test: `tests/test_doc_audit.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_doc_audit.py
def test_find_coverage_gaps_flags_new_module_without_doc():
    changed = ["portfolio_automation/new_widget.py", "tests/test_new_widget.py"]
    existing_docs = {"docs/ARCHITECTURE.md"}  # no docs/new_widget.md
    gaps = doc_audit.find_coverage_gaps(changed, existing_docs)
    names = [g.detail for g in gaps]
    assert any("new_widget" in n for n in names)
    assert all(g.dimension == "coverage" and g.auto_fixable is False for g in gaps)


def test_find_coverage_gaps_silent_when_doc_exists():
    changed = ["portfolio_automation/new_widget.py"]
    existing_docs = {"docs/new_widget.md"}
    assert doc_audit.find_coverage_gaps(changed, existing_docs) == []


def test_find_coverage_gaps_ignores_non_source_changes():
    changed = ["README.md", "outputs/latest/foo.json"]
    assert doc_audit.find_coverage_gaps(changed, set()) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_doc_audit.py -k coverage -q`
Expected: FAIL — `AttributeError: ... has no attribute 'find_coverage_gaps'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to portfolio_automation/doc_audit.py
_SOURCE_DIRS = ("portfolio_automation/", "watchlist_scanner/", "scanner/")


def find_coverage_gaps(changed_files: list[str], existing_doc_paths: set[str]) -> list[Finding]:
    """Flag new source modules in changed_files that have no docs/<module>.md.
    Pure function over the changed-file list + the set of existing doc paths,
    so it is trivially testable; the git diff is injected by the caller."""
    findings: list[Finding] = []
    for f in changed_files:
        if not any(f.startswith(d) for d in _SOURCE_DIRS):
            continue
        if not f.endswith(".py") or f.startswith("tests/") or "/test_" in f:
            continue
        module = Path(f).stem
        if module.startswith("_") or module == "__init__":
            continue
        expected_doc = f"docs/{module}.md"
        if expected_doc not in existing_doc_paths:
            findings.append(Finding(
                dimension="coverage", severity="med", doc=expected_doc,
                detail=f"new module {f} shipped without {expected_doc}",
                auto_fixable=False,
            ))
    return findings
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_doc_audit.py -k coverage -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/doc_audit.py tests/test_doc_audit.py
git commit -m "feat(doc-audit): coverage-gap detection over a changed-file list"
```

---

## Task 4: Dead-reference + cross-doc consistency

**Files:**
- Modify: `portfolio_automation/doc_audit.py`
- Test: `tests/test_doc_audit.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_doc_audit.py
def test_find_dead_refs_flags_missing_python_file(tmp_path):
    _write(tmp_path, "docs/ARCHITECTURE.md",
           "See `portfolio_automation/ghost_module.py` for details.\n")
    findings = doc_audit.find_dead_refs(str(tmp_path))
    assert any("ghost_module.py" in f.detail for f in findings)
    assert all(f.dimension == "dead_ref" and f.auto_fixable is False for f in findings)


def test_find_dead_refs_silent_for_existing_file(tmp_path):
    _write(tmp_path, "portfolio_automation/real_module.py", "x = 1\n")
    _write(tmp_path, "docs/ARCHITECTURE.md",
           "See `portfolio_automation/real_module.py`.\n")
    assert doc_audit.find_dead_refs(str(tmp_path)) == []


def test_find_cross_doc_inconsistency_flags_disagreement(tmp_path):
    _write(tmp_path, "outputs/latest/daily_run_status.json",
           json.dumps({"stage_summary": {"total": 24}}))
    _write(tmp_path, "docs/PIPELINE_RUNBOOK.md", "Runs 17 pipeline stages.\n")
    _write(tmp_path, "docs/ARCHITECTURE.md", "Runs 24 pipeline stages.\n")
    findings = doc_audit.find_cross_doc_inconsistency(str(tmp_path))
    assert any(f.anchor == "pipeline_stage_count" for f in findings)
    assert all(f.dimension == "consistency" for f in findings)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_doc_audit.py -k "dead_refs or cross_doc" -q`
Expected: FAIL — `AttributeError: ... has no attribute 'find_dead_refs'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to portfolio_automation/doc_audit.py
import glob as _glob

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


def find_cross_doc_inconsistency(root: str) -> list[Finding]:
    """For each anchor, collect the documented value seen across ALL its docs;
    flag when two docs disagree (independent of whether the source resolves)."""
    findings: list[Finding] = []
    for anchor in ANCHOR_REGISTRY:
        rx = re.compile(anchor.pattern, re.IGNORECASE)
        seen: dict[str, str] = {}  # doc -> value
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_doc_audit.py -k "dead_refs or cross_doc" -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/doc_audit.py tests/test_doc_audit.py
git commit -m "feat(doc-audit): dead-reference + cross-doc consistency checks"
```

---

## Task 5: Status assembler + artifact writer

**Files:**
- Modify: `portfolio_automation/doc_audit.py`
- Test: `tests/test_doc_audit.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_doc_audit.py
def test_run_doc_audit_assembles_status_dict(tmp_path):
    _write(tmp_path, "outputs/latest/daily_run_status.json",
           json.dumps({"stage_summary": {"total": 24}}))
    _write(tmp_path, "docs/PIPELINE_RUNBOOK.md", "Runs 17 pipeline stages.\n")
    result = doc_audit.run_doc_audit(
        str(tmp_path), last_audited_sha="abc123",
        changed_files=["portfolio_automation/new_widget.py"],
        existing_doc_paths={"docs/PIPELINE_RUNBOOK.md"},
    )
    assert result["observe_only"] is True
    assert result["source"] == "doc_audit"
    assert result["last_audited_sha"] == "abc123"
    assert result["overall_status"] in {"drift", "coverage_gap", "ok_with_warnings"}
    assert any(f["anchor"] == "pipeline_stage_count" for f in result["findings"])
    assert any(f["auto_fixable"] for f in result["auto_fix_candidates"])


def test_run_doc_audit_degrades_gracefully_on_empty_repo(tmp_path):
    result = doc_audit.run_doc_audit(str(tmp_path), last_audited_sha=None,
                                     changed_files=[], existing_doc_paths=set())
    assert result["observe_only"] is True
    assert result["overall_status"] == "ok"
    assert result["findings"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_doc_audit.py -k run_doc_audit -q`
Expected: FAIL — `AttributeError: ... has no attribute 'run_doc_audit'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to portfolio_automation/doc_audit.py
from datetime import datetime, timezone


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
        findings += find_coverage_gaps(changed_files, existing_doc_paths)
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
    if any(f.dimension == "coverage" and f.severity == "high" for f in findings):
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


_DISCLAIMER = ("Observe-only documentation audit. Reads docs + code + git; never "
               "recomputes decisions or mutates portfolio, allocation, scoring, or "
               "decision state.")


def write_doc_audit_status(result: dict, root: str) -> str:
    """Write JSON + compact MD via OutputNamespace.LATEST. Returns the JSON path."""
    from portfolio_automation.data_governance import OutputNamespace
    out_dir = Path(root) / OutputNamespace.LATEST.value
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "doc_audit_status.json"
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    md = [f"# Doc Audit — {result['generated_at'][:10]}",
          f"\n**Status:** {result['overall_status']}  ",
          f"**Findings:** {len(result['findings'])} "
          f"({len(result['auto_fix_candidates'])} auto-fixable)\n"]
    for f in result["findings"]:
        md.append(f"- [{f['severity']}] {f['dimension']} · {f['doc']} — {f['detail']}")
    (out_dir / "doc_audit_status.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return str(json_path)
```

> NOTE on `OutputNamespace`: confirm the enum member + how a value maps to
> `outputs/latest/` by reading `portfolio_automation/data_governance.py` before
> implementing. If `OutputNamespace.LATEST` is not directly a path string, use the
> module's documented write helper instead of `.value`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_doc_audit.py -k run_doc_audit -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/doc_audit.py tests/test_doc_audit.py
git commit -m "feat(doc-audit): status assembler + JSON/MD artifact writer"
```

---

## Task 6: Auto-fix eligibility + pure value substitution

**Files:**
- Modify: `portfolio_automation/doc_audit.py`
- Test: `tests/test_doc_audit.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_doc_audit.py
def test_apply_auto_fix_substitutes_only_the_captured_value(tmp_path):
    _write(tmp_path, "outputs/latest/daily_run_status.json",
           json.dumps({"stage_summary": {"total": 24}}))
    doc = _write(tmp_path, "docs/PIPELINE_RUNBOOK.md",
                 "Runs 17 pipeline stages. The 17 here is unrelated prose.\n")
    f = doc_audit.find_drift(str(tmp_path))[0]
    changed = doc_audit.apply_auto_fix(f, str(tmp_path))
    assert changed is True
    text = doc.read_text()
    assert "Runs 24 pipeline stages." in text
    # only the anchored "NN pipeline stages" value changed; loose "17" prose left intact
    assert "The 17 here is unrelated prose." in text


def test_apply_auto_fix_refuses_non_auto_fixable():
    f = doc_audit.Finding(dimension="dead_ref", severity="med",
                          doc="docs/X.md", detail="x", auto_fixable=False)
    assert doc_audit.apply_auto_fix(f, ".") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_doc_audit.py -k auto_fix -q`
Expected: FAIL — `AttributeError: ... has no attribute 'apply_auto_fix'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to portfolio_automation/doc_audit.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_doc_audit.py -k auto_fix -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/doc_audit.py tests/test_doc_audit.py
git commit -m "feat(doc-audit): guardrailed pure-substitution auto-fix"
```

---

## Task 7: Committed state module (cross-workstation portability)

**Files:**
- Create: `portfolio_automation/doc_audit_state.py`
- Create: `.agent/doc_audit_state.yaml`
- Test: `tests/test_doc_audit_state.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_doc_audit_state.py
from pathlib import Path
import pytest
from portfolio_automation import doc_audit_state as st


def _write(root, rel, content):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_load_state_defaults_when_missing(tmp_path):
    s = st.load_state(str(tmp_path))
    assert s["apply_enabled"] is True
    assert s["last_audited_sha"] is None


def test_round_trip_save_then_load(tmp_path):
    st.save_state(str(tmp_path), {"last_audited_sha": "deadbee",
                                  "last_run_at": "2026-06-01T00:00:00Z",
                                  "apply_enabled": False, "fixes_last_run": 3})
    s = st.load_state(str(tmp_path))
    assert s["last_audited_sha"] == "deadbee"
    assert s["apply_enabled"] is False
    assert s["fixes_last_run"] == 3


def test_state_path_is_under_agent_dir(tmp_path):
    assert st.state_path(str(tmp_path)).endswith(".agent/doc_audit_state.yaml")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_doc_audit_state.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_automation.doc_audit_state'`

- [ ] **Step 3: Write minimal implementation**

```python
# portfolio_automation/doc_audit_state.py
"""Committed, cross-workstation state for the doc auditor. Lives in .agent/ (a
tracked dir) so it travels via git; the last-audited SHA lets any workstation
derive 'what changed since last audit' from git diff."""
from __future__ import annotations

from pathlib import Path
import yaml

_DEFAULTS = {"last_audited_sha": None, "last_run_at": None,
             "apply_enabled": True, "fixes_last_run": 0}


def state_path(root: str) -> str:
    return str(Path(root) / ".agent" / "doc_audit_state.yaml")


def load_state(root: str) -> dict:
    p = Path(state_path(root))
    if not p.exists():
        return dict(_DEFAULTS)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return dict(_DEFAULTS)
    return {**_DEFAULTS, **data}


def save_state(root: str, state: dict) -> None:
    p = Path(state_path(root))
    p.parent.mkdir(parents=True, exist_ok=True)
    merged = {**_DEFAULTS, **state}
    p.write_text(yaml.safe_dump(merged, sort_keys=True), encoding="utf-8")
```

- [ ] **Step 4: Run tests + create the committed seed file**

Run: `python3 -m pytest tests/test_doc_audit_state.py -q`
Expected: PASS (3 passed)

Then create the seed state (HEAD SHA injected at first run; start null):

```bash
cat > .agent/doc_audit_state.yaml <<'YAML'
apply_enabled: true
fixes_last_run: 0
last_audited_sha: null
last_run_at: null
YAML
```

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/doc_audit_state.py tests/test_doc_audit_state.py .agent/doc_audit_state.yaml
git commit -m "feat(doc-audit): committed cross-workstation state module"
```

---

## Task 8: `/doc-audit` weekly orchestrator skill

**Files:**
- Create: `.claude/skills/doc-audit/SKILL.md`

- [ ] **Step 1: Write the skill file**

```markdown
---
name: doc-audit
description: Weekly documentation audit. Runs the doc_audit producer over the corpus, auto-fixes high-confidence factual drift under guardrails, dispatches portfolio-doc-writer for the rest, and advances committed state. Runs on demand from any workstation and via VPS cron. Observe-by-default; only the guardrailed drift-fix mutates docs.
---

# Skill: doc-audit (weekly tier)

Working dir: `/opt/stockbot`.

## Step 1 — Resolve the git range

```bash
LAST_SHA=$(python3 -c "from portfolio_automation.doc_audit_state import load_state; print(load_state('.')['last_audited_sha'] or '')")
HEAD_SHA=$(git rev-parse HEAD)
if [ -n "$LAST_SHA" ]; then RANGE="$LAST_SHA..HEAD"; else RANGE="HEAD~20..HEAD"; fi
git diff --name-only $RANGE > /tmp/doc_audit_changed.txt
```

## Step 2 — Run the producer

```bash
python3 - <<'PY'
import glob, json
from portfolio_automation import doc_audit, doc_audit_state
import subprocess
last = doc_audit_state.load_state('.')['last_audited_sha']
changed = [l.strip() for l in open('/tmp/doc_audit_changed.txt') if l.strip()]
existing = set(p for p in glob.glob('docs/**/*.md', recursive=True))
result = doc_audit.run_doc_audit('.', last, changed, existing)
doc_audit.write_doc_audit_status(result, '.')
print(json.dumps({"status": result["overall_status"],
                  "findings": len(result["findings"]),
                  "auto": len(result["auto_fix_candidates"])}))
PY
```

## Step 3 — Triage

Read `outputs/latest/doc_audit_status.json`.
- **GREEN** — `overall_status == "ok"`; no findings. Emit heartbeat, done.
- **AMBER** — drift or `ok_with_warnings`; auto-fixes available, no high-severity coverage gap.
- **RED** — `overall_status == "coverage_gap"` (a shipped change has no doc) OR any `consistency` finding with severity high.

## Step 4 — Apply guardrailed auto-fixes (only if apply_enabled)

Guardrails: only `auto_fix_candidates`; cap at 10 per run; skip if `apply_enabled` is false.

```bash
python3 - <<'PY'
import json
from portfolio_automation import doc_audit, doc_audit_state
st = doc_audit_state.load_state('.')
result = json.load(open('outputs/latest/doc_audit_status.json'))
applied = []
if st.get('apply_enabled', True):
    from portfolio_automation.doc_audit import Finding
    for fd in result['auto_fix_candidates'][:10]:
        f = Finding(**{k: fd.get(k) for k in
            ('dimension','severity','doc','detail','auto_fixable','anchor','current','expected','line')})
        if doc_audit.apply_auto_fix(f, '.'):
            applied.append({"doc": f.doc, "anchor": f.anchor,
                            "from": f.current, "to": f.expected})
print(json.dumps(applied))
PY
```

If any fixes were applied, commit them in a dedicated commit:

```bash
git add docs/
git commit -m "docs(auto): doc-audit drift fixes $(date -u +%F)" || echo "no doc changes"
```

(Rollback if wrong: `git revert <that commit>`.)

## Step 5 — Dispatch portfolio-doc-writer for the rest

For every finding that is NOT auto-fixable (dead_ref, coverage, judgment), dispatch
the `portfolio-doc-writer` agent with the finding list so it can draft the doc
updates for operator approval. Do NOT auto-commit the writer's edits.

## Step 6 — Advance committed state

```bash
python3 - <<'PY'
from datetime import datetime, timezone
import subprocess, json
from portfolio_automation import doc_audit_state
applied = json.loads(open('/tmp/doc_audit_applied.json').read()) if __import__('os').path.exists('/tmp/doc_audit_applied.json') else []
head = subprocess.check_output(['git','rev-parse','HEAD']).decode().strip()
st = doc_audit_state.load_state('.')
st['last_audited_sha'] = head
st['last_run_at'] = datetime.now(timezone.utc).isoformat()
st['fixes_last_run'] = len(applied)
doc_audit_state.save_state('.', st)
PY
git add .agent/doc_audit_state.yaml
git commit -m "chore(doc-audit): advance audit state $(date -u +%F)" || echo "state unchanged"
```

## Step 7 — Heartbeat output

`[GREEN|AMBER|RED] doc-audit YYYY-MM-DD: N findings, M auto-fixed, K coverage gaps`
Then list each coverage gap + dead-ref so the operator sees what needs a doc.

## Push note

This skill commits locally. It does NOT push. Push when you next sync, or add
`git push` to the VPS cron wrapper if you want hands-off remote sync.
```

- [ ] **Step 2: Smoke-test the skill end-to-end**

Run the Step 1 + Step 2 bash blocks manually in `/opt/stockbot`.
Expected: `outputs/latest/doc_audit_status.json` is written; JSON parses; `overall_status` present.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/doc-audit/SKILL.md
git commit -m "feat(doc-audit): /doc-audit weekly orchestrator skill"
```

---

## Task 9: `portfolio-doc-auditor` agent (judgment lens)

**Files:**
- Create: `.claude/agents/portfolio-doc-auditor.md`

- [ ] **Step 1: Write the agent file**

```markdown
---
name: portfolio-doc-auditor
description: Read-only documentation lens for the Portfolio Automation System. Audits the docs corpus for clarity, conciseness, redundancy across docs, and "this doc grew too large, decompose it" — the judgment dimensions the deterministic doc_audit producer cannot compute. Returns ranked findings; never edits. Use in the monthly doc-audit tier or when asked to review documentation quality.
tools: Read, Grep, Glob, Bash
---

# Portfolio Doc Auditor Agent

You are a read-only documentation auditor. You judge quality; you never edit.
The deterministic producer (`portfolio_automation/doc_audit.py`) already handles
factual drift, dead refs, cross-doc number consistency, and coverage gaps — do
NOT re-do those. Your job is the judgment layer.

## What you assess

1. **Clarity** — sections that are confusing, ambiguous, or bury the point.
2. **Conciseness** — redundant prose, repeated explanations, padding.
3. **Cross-doc redundancy** — the same concept explained in 3 places that should
   be one canonical doc + links.
4. **Decomposition** — docs that have grown too large to hold one responsibility
   (e.g. `OUTPUT_ARTIFACT_CONTRACTS.md` at ~1.5k lines). Recommend a split.

## Inputs

- `outputs/latest/doc_audit_status.json` (the deterministic findings — context only)
- The docs corpus (`docs/**/*.md`)

## Output (return as your final message)

A ranked list of findings: `{doc, dimension, severity, what, why, suggestion}`.
End with the single highest-leverage cleanup. You do not edit; the operator hands
accepted findings to `portfolio-doc-writer`.

## You do NOT

- Edit any file.
- Re-report deterministic drift/dead-ref/coverage already in the producer JSON.
- Recommend changes to runtime code, tests, or output schemas.
```

- [ ] **Step 2: Commit**

```bash
git add .claude/agents/portfolio-doc-auditor.md
git commit -m "feat(doc-audit): portfolio-doc-auditor read-only judgment agent"
```

> NOTE: per CLAUDE.md, a NEW agent is snapshotted at session start and will not
> dispatch until the next session. After committing, tell the operator the agent
> needs a session restart before the monthly skill can dispatch it; until then the
> monthly skill degrades to producer-only findings.

---

## Task 10: `/doc-audit-monthly` skill + monthly-tool-analysis hook

**Files:**
- Create: `.claude/skills/doc-audit-monthly/SKILL.md`
- Modify: `.claude/commands/monthly-tool-analysis.md`

- [ ] **Step 1: Write the monthly skill file**

```markdown
---
name: doc-audit-monthly
description: Monthly documentation retrospective. Runs the deterministic doc_audit producer for context, then dispatches the read-only portfolio-doc-auditor agent for the judgment dimensions (clarity, conciseness, redundancy, large-doc decomposition). Report-only — accepted findings are handed to portfolio-doc-writer. Runs on demand anywhere and via VPS cron on the 1st.
---

# Skill: doc-audit-monthly (judgment tier)

Working dir: `/opt/stockbot`.

## Step 1 — Run the deterministic producer for context

Run the `/doc-audit` Step 2 producer block (do NOT auto-fix here; this tier is
report-only). This refreshes `outputs/latest/doc_audit_status.json`.

## Step 2 — Dispatch the judgment lens

Dispatch the `portfolio-doc-auditor` agent. Pass it the producer JSON path and ask
for a ranked clarity/conciseness/redundancy/decomposition review of the corpus.
(If the agent is not yet live — newly committed, pre-restart — note that and emit
producer-only findings.)

## Step 3 — Monthly heartbeat

`[GREEN|AMBER|RED] doc-audit-monthly YYYY-MM: <headline>`
Body: top 5 judgment findings + any standing coverage gaps from the producer.
Report-only: list what to hand to `portfolio-doc-writer`; do not edit.
```

- [ ] **Step 2: Add the hook to monthly-tool-analysis**

Open `.claude/commands/monthly-tool-analysis.md`, find its dispatch/section list, and add (match the file's existing grammar):

```markdown
- **Documentation lens** — invoke the `/doc-audit-monthly` skill (or note its latest
  `outputs/latest/doc_audit_status.json`) and fold its verdict into the monthly
  heartbeat: report standing coverage gaps + the doc-auditor's top decomposition
  recommendation.
```

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/doc-audit-monthly/SKILL.md .claude/commands/monthly-tool-analysis.md
git commit -m "feat(doc-audit): monthly judgment tier + monthly-tool-analysis hook"
```

---

## Task 11: daily-tool-analysis consumer line (CLAUDE.md coverage pairing)

**Files:**
- Modify: `.claude/commands/daily-tool-analysis.md`

- [ ] **Step 1: Add the artifact-read + AMBER trigger**

In Step 1 "Read artifacts", add:

```markdown
15. `outputs/latest/doc_audit_status.json` → overall_status, len(coverage_gaps),
    count of unfixed drift findings (added 2026-06-01; weekly-cadence producer)
```

In Step 2 triage, add to AMBER:

```markdown
- `doc_audit_status.overall_status == "coverage_gap"` OR any unfixed `drift`/`consistency`
  finding present (docs lag code — advisory; resolved by the next /doc-audit run)
```

In Step 4 body grammar, add a line (only when the artifact exists):

```markdown
6b. Docs: `"Docs: {overall_status} · {N} findings, {K} coverage gaps (last audit {last_audited_sha[:8]})"`
```

- [ ] **Step 2: Commit**

```bash
git add .claude/commands/daily-tool-analysis.md
git commit -m "feat(doc-audit): wire doc_audit_status consumer into daily-tool-analysis"
```

---

## Task 12: VPS cron entries

**Files:**
- None in-repo (crontab is host state). Returned as a copyable block for the operator.

- [ ] **Step 1: Provide the crontab block (operator runs on VPS)**

```cron
# Weekly doc audit — Mondays 09:45 UTC (after daily cron settles)
45 9 * * 1 cd /opt/stockbot && flock -n /var/lock/stockbot-doc-audit.lock bash scripts/run_doc_audit.sh >> logs/doc_audit_$(date +\%F).log 2>&1
# Monthly doc audit — 1st of month 09:15 UTC (BEFORE monthly-tool-analysis at 09:30 so its verdict is fresh)
15 9 1 * * cd /opt/stockbot && flock -n /var/lock/stockbot-doc-audit.lock bash scripts/run_doc_audit_monthly.sh >> logs/doc_audit_monthly_$(date +\%F).log 2>&1
```

- [ ] **Step 2: Create the wrapper scripts**

`scripts/run_doc_audit.sh` and `scripts/run_doc_audit_monthly.sh` invoke Claude Code
headless with the corresponding skill (match the pattern in the existing
`scripts/run_daily_safe.sh` / discovery-pulse wrappers — read one first). Each must
acquire the lock, run the skill, and exit non-zero on failure for the log.

- [ ] **Step 3: Commit the wrappers**

```bash
git add scripts/run_doc_audit.sh scripts/run_doc_audit_monthly.sh
git commit -m "feat(doc-audit): VPS cron wrapper scripts"
```

> NOTE: monthly cron is 09:15 (not 09:45) so the doc verdict is fresh when
> monthly-tool-analysis reads it at 09:30. This corrects the spec's 09:45 monthly time.

---

## Task 13: Self-documenting docs + CHANGELOG + state sync

**Files:**
- Create: `docs/DOC_AUDIT.md`
- Modify: `docs/CHANGELOG_DECISIONS.md`, `.agent/project_state.yaml`

- [ ] **Step 1: Write `docs/DOC_AUDIT.md`**

Cover: purpose, the four check families, the anchor registry (how to add an anchor),
auto-fix guardrails, the git-as-state model, both cadence tiers, and how to pause
auto-fix (`apply_enabled: false` in `.agent/doc_audit_state.yaml`). Keep it concise —
this doc is itself subject to the auditor.

- [ ] **Step 2: Append a CHANGELOG_DECISIONS.md entry**

Area `architecture`. Record: new observe-only producer + read-only agent + two skills
+ cron; auto-fix bounded to the anchor registry; git-committed state for portability.

- [ ] **Step 3: Sync `.agent/project_state.yaml`**

Add a completion/observation note for the doc-audit system. Do NOT change
`next_official_step` (this was operator-requested, not a roadmap step).

- [ ] **Step 4: Run the full targeted suite + commit**

```bash
python3 -m pytest tests/test_doc_audit.py tests/test_doc_audit_state.py -q
```
Expected: all pass.

```bash
git add docs/DOC_AUDIT.md docs/CHANGELOG_DECISIONS.md .agent/project_state.yaml
git commit -m "docs(doc-audit): module doc + changelog + project_state sync"
```

---

## Self-Review

**Spec coverage:**
- Producer / 4 check families → Tasks 1–5. ✓
- Anchor registry (full sweep seed) → Task 1 (≥6 seeded; expand during Task 1 by sweeping documented constants). ✓
- Auto-fix + guardrails → Task 6 (eligibility + pure substitution) + Task 8 Step 4 (cap 10, apply_enabled). ✓
- Git-committed state / portability → Task 7. ✓
- `/doc-audit` weekly skill → Task 8. ✓
- `portfolio-doc-auditor` agent → Task 9. ✓
- `/doc-audit-monthly` + monthly-tool-analysis hook → Task 10. ✓
- daily-tool-analysis consumer pairing → Task 11. ✓
- Cron (weekly + monthly) → Task 12 (monthly time corrected to 09:15). ✓
- Self-documenting docs + CHANGELOG + project_state → Task 13. ✓
- Tests healthy + degraded → Tasks 1–7 (degraded: Task 1 missing-artifact, Task 2 unresolvable-source, Task 5 empty-repo). ✓

**Placeholder scan:** Task 12 wrapper scripts reference "match the existing pattern" — intentional (the engineer must read the real wrapper; exact contents are host-specific). Task 13 docs are prose deliverables with explicit content lists. No code step lacks code.

**Type consistency:** `Anchor` and `Finding` fields are defined in Task 1 and reused verbatim in Tasks 2–8. `resolve_source`, `find_drift`, `find_coverage_gaps`, `find_dead_refs`, `find_cross_doc_inconsistency`, `run_doc_audit`, `write_doc_audit_status`, `apply_auto_fix` signatures are consistent across tasks and the skill. State API (`load_state`/`save_state`/`state_path`) consistent between Task 7 and Task 8.

**Known follow-up for the implementer:** verify `OutputNamespace` usage against `data_governance.py` (flagged inline in Task 5) and confirm PyYAML is already a dependency (it is used elsewhere via `.agent/*.yaml`; if not importable in the test env, the state module test will surface it).
