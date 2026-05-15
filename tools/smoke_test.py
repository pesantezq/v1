"""
Post-Deploy Smoke Test — Phase F
=================================

Registry-driven read-only smoke check for production artifacts.

Closes audit finding G-T1 (no smoke test that proves each registered
artifact can be produced and matches its declared shape). Pairs with
``tools.status``:

  - ``tools.status``       — "does it exist? when was it written? is it healthy?"
  - ``tools.smoke_test``   — "if it exists, is its shape what the registry claims?"

Per artifact this checks:

  - File exists at the registry path (missing → FAIL if required, INFO if optional).
  - File is readable.
  - For ``format=json``: parseable JSON; ``observe_only=True`` present when
    ``observe_only_required=True``.
  - For ``format=jsonl``: every non-blank line parseable as JSON.
  - For ``format=md`` / ``txt``: file non-empty.

Safety:

  - Read-only; never writes, never mutates.
  - Importing the module performs no I/O.
  - Same dotenv autoload affordance as ``portfolio_automation.env`` is NOT
    needed — smoke validates files on disk, not env.

Usage::

    python -m tools.smoke_test                     # text
    python -m tools.smoke_test --format json
    python -m tools.smoke_test --strict            # exit 1 on any FAIL
    python -m tools.smoke_test --verbose           # show OK rows too
    python -m tools.smoke_test --include-optional  # don't downgrade missing optional to INFO

Exit codes:
    0 — diagnostic mode default (always)
    1 — ``--strict`` and at least one FAIL
    2 — repo root marker missing
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


SEV_OK = "OK"
SEV_INFO = "INFO"
SEV_WARN = "WARN"
SEV_FAIL = "FAIL"
_SEV_ORDER = {SEV_OK: 0, SEV_INFO: 1, SEV_WARN: 2, SEV_FAIL: 3}

_REPO_ROOT_MARKER = "main.py"


@dataclass
class SmokeResult:
    """One artifact's smoke result."""
    name: str
    namespace: str
    relative_path: str
    severity: str
    message: str
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "namespace": self.namespace,
            "relative_path": self.relative_path,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
            "details": dict(self.details),
        }


@dataclass
class SmokeReport:
    """Aggregate smoke result for one invocation."""
    generated_at: str
    repo_root: str
    include_optional: bool
    results: list[SmokeResult] = field(default_factory=list)

    @property
    def overall_severity(self) -> str:
        worst = SEV_OK
        for r in self.results:
            if _SEV_ORDER.get(r.severity, 0) > _SEV_ORDER.get(worst, 0):
                worst = r.severity
        return worst

    @property
    def severity_counts(self) -> dict[str, int]:
        counts = {SEV_OK: 0, SEV_INFO: 0, SEV_WARN: 0, SEV_FAIL: 0}
        for r in self.results:
            counts[r.severity] = counts.get(r.severity, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "repo_root": self.repo_root,
            "include_optional": self.include_optional,
            "overall_severity": self.overall_severity,
            "severity_counts": self.severity_counts,
            "results": [r.to_dict() for r in self.results],
            "advisory_only": True,
            "no_trade": True,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def detect_repo_root(explicit: Path | str | None = None) -> Path:
    if explicit is not None:
        candidate = Path(explicit).resolve()
    else:
        candidate = Path(__file__).resolve().parents[1]
    if not (candidate / _REPO_ROOT_MARKER).exists():
        raise FileNotFoundError(
            f"Repo root marker {_REPO_ROOT_MARKER!r} not found in {candidate}. "
            "Pass --repo-root explicitly."
        )
    return candidate


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Per-format validators
# ---------------------------------------------------------------------------

def _validate_json(
    path: Path, art: Any, *, observe_only_required: bool,
) -> tuple[str, str, dict[str, Any]]:
    """
    Return (severity, message, details) for a JSON artifact.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return SEV_FAIL, f"unreadable: {exc}", {}

    if not raw.strip():
        return SEV_FAIL, "file is empty", {"size_bytes": 0}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return SEV_FAIL, f"invalid JSON: line {exc.lineno}, col {exc.colno}", {}

    details: dict[str, Any] = {"size_bytes": len(raw)}

    if observe_only_required:
        if not isinstance(payload, dict):
            return SEV_FAIL, "observe_only required but JSON root is not an object", details
        if payload.get("observe_only") is not True:
            return (
                SEV_FAIL,
                "observe_only=True required by registry but not present in artifact",
                details,
            )
        details["observe_only"] = True

    # Soft signal: most artifacts have generated_at; flag if missing.
    if isinstance(payload, dict) and "generated_at" not in payload:
        details["missing_recommended_fields"] = ["generated_at"]
        return SEV_WARN, "valid JSON but missing recommended 'generated_at'", details

    return SEV_OK, "valid JSON; required flags present", details


def _validate_jsonl(path: Path) -> tuple[str, str, dict[str, Any]]:
    """Return (severity, message, details) for a JSONL artifact."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return SEV_FAIL, f"unreadable: {exc}", {}

    lines = [line for line in raw.splitlines() if line.strip()]
    if not lines:
        return SEV_INFO, "JSONL file empty (no events yet)", {"row_count": 0}

    bad: list[int] = []
    for idx, line in enumerate(lines, start=1):
        try:
            json.loads(line)
        except json.JSONDecodeError:
            bad.append(idx)
            if len(bad) >= 5:
                break

    details = {"row_count": len(lines), "bad_rows": bad}
    if bad:
        return SEV_FAIL, f"{len(bad)} unparseable row(s) (first at line {bad[0]})", details
    return SEV_OK, f"valid JSONL with {len(lines)} row(s)", details


def _validate_text(path: Path, kind: str) -> tuple[str, str, dict[str, Any]]:
    """Return (severity, message, details) for a non-JSON text artifact."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return SEV_FAIL, f"unreadable: {exc}", {}
    if not raw.strip():
        return SEV_WARN, f"{kind} file is empty", {"size_bytes": 0}
    return SEV_OK, f"valid {kind}; {len(raw)} bytes", {"size_bytes": len(raw)}


# ---------------------------------------------------------------------------
# Main validation walk
# ---------------------------------------------------------------------------

def validate_registry(
    repo_root: Path, *, include_optional: bool = False,
) -> SmokeReport:
    """
    Walk the artifacts registry, validate every entry's on-disk file.

    Parameters
    ----------
    repo_root:
        Resolved repo root.
    include_optional:
        When True, a missing optional artifact is reported as FAIL.  When
        False (default), missing optional artifacts are reported as INFO and
        cannot dominate the overall severity.
    """
    try:
        from portfolio_automation.artifacts_registry import REGISTRY, artifact_path
        from portfolio_automation.artifacts_registry import (
            FORMAT_JSON, FORMAT_JSONL, FORMAT_MARKDOWN, FORMAT_TEXT, FORMAT_CSV,
        )
    except Exception as exc:
        # Registry can't be loaded — single FAIL result and bail.
        return SmokeReport(
            generated_at=_now_iso(),
            repo_root=str(repo_root),
            include_optional=include_optional,
            results=[SmokeResult(
                name="<registry>",
                namespace="",
                relative_path="",
                severity=SEV_FAIL,
                message=f"artifacts_registry not importable: {exc}",
            )],
        )

    base_outputs = repo_root / "outputs"
    report = SmokeReport(
        generated_at=_now_iso(),
        repo_root=str(repo_root),
        include_optional=include_optional,
    )

    for art in REGISTRY:
        path = artifact_path(art.name, base_dir=base_outputs)
        result = SmokeResult(
            name=art.name,
            namespace=art.namespace.value,
            relative_path=art.relative_path,
            severity=SEV_OK,
            message="",
            path=str(path),
        )

        if not path.exists():
            if art.append_only:
                # JSONL audit logs grow over time; absence on a fresh deploy
                # means "no events yet", not a producer failure.
                result.severity = SEV_INFO
                result.message = "append-only artifact absent (no events yet)"
            elif art.optional and not include_optional:
                result.severity = SEV_INFO
                result.message = "optional artifact absent"
            else:
                result.severity = SEV_FAIL
                result.message = "artifact missing"
            report.results.append(result)
            continue

        if art.format == FORMAT_JSON:
            sev, msg, det = _validate_json(
                path, art, observe_only_required=art.observe_only_required,
            )
        elif art.format == FORMAT_JSONL:
            sev, msg, det = _validate_jsonl(path)
        elif art.format == FORMAT_MARKDOWN:
            sev, msg, det = _validate_text(path, "markdown")
        elif art.format == FORMAT_TEXT:
            sev, msg, det = _validate_text(path, "text")
        elif art.format == FORMAT_CSV:
            sev, msg, det = _validate_text(path, "csv")
        else:
            sev, msg, det = SEV_WARN, f"unknown format {art.format!r}", {}

        result.severity = sev
        result.message = msg
        result.details = det
        report.results.append(result)

    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_SEV_LABEL = {
    SEV_OK:   "[ OK ]",
    SEV_INFO: "[INFO]",
    SEV_WARN: "[WARN]",
    SEV_FAIL: "[FAIL]",
}


def render_text(report: SmokeReport, *, verbose: bool = False) -> str:
    lines: list[str] = []
    lines.append("Portfolio Automation — Smoke Test")
    lines.append("=" * 40)
    lines.append(f"Generated: {report.generated_at}")
    lines.append(f"Repo:      {report.repo_root}")
    lines.append(f"Mode:      include_optional={report.include_optional}")
    counts = report.severity_counts
    lines.append(
        f"Overall:   {report.overall_severity}  "
        f"(ok={counts[SEV_OK]}, info={counts[SEV_INFO]}, "
        f"warn={counts[SEV_WARN]}, fail={counts[SEV_FAIL]})"
    )
    lines.append("")
    for r in report.results:
        if not verbose and r.severity == SEV_OK and report.overall_severity != SEV_OK:
            continue
        if not verbose and r.severity == SEV_INFO and report.overall_severity in (SEV_WARN, SEV_FAIL):
            continue
        label = _SEV_LABEL.get(r.severity, r.severity)
        lines.append(f"{label} {r.name}: {r.message}")
    lines.append("")
    lines.append("Advisory only — no trades executed.")
    return "\n".join(lines) + "\n"


def render_json(report: SmokeReport) -> str:
    return json.dumps(report.to_dict(), indent=2, default=str)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.smoke_test",
        description=(
            "Registry-driven shape smoke test. Validates every registered "
            "artifact on disk against its declared format and required flags. "
            "Read-only; never writes."
        ),
    )
    p.add_argument(
        "--repo-root", default=None,
        help="Repo root override. Default: directory above this file.",
    )
    p.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="Output format. Default: text.",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Include OK and INFO rows even when overall severity is OK.",
    )
    p.add_argument(
        "--include-optional", action="store_true",
        help="Treat missing optional artifacts as FAIL instead of INFO.",
    )
    p.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero on any FAIL.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        root = detect_repo_root(args.repo_root)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    report = validate_registry(root, include_optional=args.include_optional)
    if args.format == "json":
        print(render_json(report))
    else:
        print(render_text(report, verbose=args.verbose), end="")

    if args.strict and report.severity_counts[SEV_FAIL] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
