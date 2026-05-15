"""
Production Status — Read-Only Health CLI
==========================================

Single command for operators to ask "is production healthy right now?"
without grepping logs or interrogating SQLite. Consumes existing JSON
status artifacts plus the artifact registry; never writes anything.

Closes audit findings G-L1 (no "last run" view), G-O2 (no single status
command), and partially G-I3 (registry probe surfaces missing artifacts).

Usage::

    python -m tools.status                       # plain text
    python -m tools.status --format json         # JSON envelope
    python -m tools.status --format md           # Markdown
    python -m tools.status --verbose             # include INFO checks
    python -m tools.status --strict              # exit non-zero on warn/fail

Inputs (all read-only, all optional — missing artifacts degrade to checks):

  - outputs/latest/pipeline_run_status.json       (official lane)
  - outputs/sandbox/discovery/sandbox_run_status.json (research lane)
  - outputs/latest/ai_budget_summary.json
  - outputs/latest/memo_delivery_status.json
  - outputs/policy/decision_outcome_summary.json
  - the artifacts_registry — checks each non-optional artifact exists

Output:

  Default ("text") prints a banner with overall severity and a one-line
  check per source, listing only non-OK checks unless --verbose. JSON and
  Markdown formats render the same data for tooling or memo embedding.

Safety properties:

  - Pure reader; no writes.
  - Importing this module performs no I/O.
  - Every probe is wrapped: missing or malformed artifacts surface as
    INFO/WARN, never as exceptions to the caller.
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


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

SEV_OK = "OK"
SEV_INFO = "INFO"
SEV_WARN = "WARN"
SEV_FAIL = "FAIL"

_SEV_ORDER: dict[str, int] = {SEV_OK: 0, SEV_INFO: 1, SEV_WARN: 2, SEV_FAIL: 3}

_REPO_ROOT_MARKER = "main.py"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class HealthCheck:
    """One operator-facing health observation."""
    name: str
    severity: str   # SEV_OK | SEV_INFO | SEV_WARN | SEV_FAIL
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "severity": self.severity,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass
class StatusReport:
    """Aggregate status across all probes."""
    generated_at: str
    repo_root: str
    checks: list[HealthCheck] = field(default_factory=list)

    @property
    def overall_severity(self) -> str:
        worst = SEV_OK
        for c in self.checks:
            if _SEV_ORDER.get(c.severity, 0) > _SEV_ORDER.get(worst, 0):
                worst = c.severity
        return worst

    @property
    def severity_counts(self) -> dict[str, int]:
        counts = {SEV_OK: 0, SEV_INFO: 0, SEV_WARN: 0, SEV_FAIL: 0}
        for c in self.checks:
            counts[c.severity] = counts.get(c.severity, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "repo_root": self.repo_root,
            "overall_severity": self.overall_severity,
            "severity_counts": self.severity_counts,
            "checks": [c.to_dict() for c in self.checks],
            "advisory_only": True,
            "no_trade": True,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_repo_root(explicit: Path | str | None = None) -> Path:
    """
    Resolve the repo root the same way ``tools/cleanup_orphan_outputs.py``
    does: explicit override, else ``parents[1]`` of this file.  Raises if
    the marker is absent — never guesses.
    """
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


def _read_json(path: Path) -> Any | None:
    """Read and parse a JSON file; return None on any failure."""
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("status: failed to read %s — %s", path, exc)
        return None


def _age_hours(iso: str | None) -> float | None:
    """Hours elapsed since *iso* timestamp, or None if unparseable."""
    if not iso:
        return None
    try:
        # ISO with timezone offset
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return delta.total_seconds() / 3600.0
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

# Threshold (hours) beyond which a status artifact is considered stale.
_STALE_HOURS = 26.0


def probe_pipeline_run_status(repo_root: Path) -> HealthCheck:
    """Read outputs/latest/pipeline_run_status.json and classify."""
    path = repo_root / "outputs" / "latest" / "pipeline_run_status.json"
    payload = _read_json(path)
    if payload is None:
        return HealthCheck(
            name="pipeline_run_status",
            severity=SEV_FAIL,
            message=f"missing or unreadable: {path}",
            details={"path": str(path)},
        )

    success = bool(payload.get("success"))
    exit_code = int(payload.get("exit_code", 0) or 0)
    steps_failed = int(payload.get("steps_failed", 0) or 0)
    steps_skipped = int(payload.get("steps_skipped", 0) or 0)
    generated_at = payload.get("generated_at")
    age = _age_hours(generated_at)
    skip_reason = None
    if payload.get("steps"):
        first = payload["steps"][0] if isinstance(payload["steps"], list) else None
        if isinstance(first, dict):
            skip_reason = first.get("skip_reason")

    details: dict[str, Any] = {
        "run_id": payload.get("run_id"),
        "run_mode": payload.get("run_mode"),
        "source": payload.get("source"),
        "success": success,
        "exit_code": exit_code,
        "generated_at": generated_at,
        "age_hours": round(age, 2) if age is not None else None,
        "steps_failed": steps_failed,
        "steps_skipped": steps_skipped,
        "skip_reason": skip_reason,
        "summary": payload.get("summary", {}),
    }

    if not success or exit_code != 0 or steps_failed > 0:
        return HealthCheck(
            name="pipeline_run_status",
            severity=SEV_FAIL,
            message=(
                f"last pipeline run reports failure "
                f"(success={success}, exit_code={exit_code}, "
                f"steps_failed={steps_failed})"
            ),
            details=details,
        )

    if age is not None and age > _STALE_HOURS:
        return HealthCheck(
            name="pipeline_run_status",
            severity=SEV_WARN,
            message=f"last pipeline run is {age:.1f}h old (threshold {_STALE_HOURS}h)",
            details=details,
        )

    if skip_reason:
        return HealthCheck(
            name="pipeline_run_status",
            severity=SEV_INFO,
            message=f"latest invocation skipped (reason: {skip_reason})",
            details=details,
        )

    return HealthCheck(
        name="pipeline_run_status",
        severity=SEV_OK,
        message=f"last run ok (run_id={payload.get('run_id')})",
        details=details,
    )


def probe_sandbox_run_status(repo_root: Path) -> HealthCheck:
    """Read outputs/sandbox/discovery/sandbox_run_status.json and classify."""
    path = repo_root / "outputs" / "sandbox" / "discovery" / "sandbox_run_status.json"
    payload = _read_json(path)
    if payload is None:
        return HealthCheck(
            name="sandbox_run_status",
            severity=SEV_INFO,
            message=f"no sandbox status artifact ({path}) — sandbox lane may be disabled",
            details={"path": str(path)},
        )

    steps_failed = int(payload.get("steps_failed", 0) or 0)
    generated_at = payload.get("generated_at")
    age = _age_hours(generated_at)

    details: dict[str, Any] = {
        "run_id": payload.get("run_id"),
        "generated_at": generated_at,
        "age_hours": round(age, 2) if age is not None else None,
        "steps_attempted": payload.get("steps_attempted"),
        "steps_succeeded": payload.get("steps_succeeded"),
        "steps_skipped": payload.get("steps_skipped"),
        "steps_failed": steps_failed,
        "errors": payload.get("errors", []),
    }

    if steps_failed > 0:
        return HealthCheck(
            name="sandbox_run_status",
            severity=SEV_WARN,
            message=f"sandbox lane reports {steps_failed} failed step(s)",
            details=details,
        )

    if age is not None and age > _STALE_HOURS:
        return HealthCheck(
            name="sandbox_run_status",
            severity=SEV_WARN,
            message=f"sandbox status is {age:.1f}h old (threshold {_STALE_HOURS}h)",
            details=details,
        )

    return HealthCheck(
        name="sandbox_run_status",
        severity=SEV_OK,
        message=f"sandbox lane ok (run_id={payload.get('run_id')})",
        details=details,
    )


def probe_ai_budget(repo_root: Path) -> HealthCheck:
    """Read outputs/latest/ai_budget_summary.json and classify."""
    path = repo_root / "outputs" / "latest" / "ai_budget_summary.json"
    payload = _read_json(path)
    if payload is None:
        return HealthCheck(
            name="ai_budget",
            severity=SEV_INFO,
            message=f"no AI budget summary at {path}",
            details={"path": str(path)},
        )

    enabled = bool(payload.get("enabled"))
    blocked = bool(payload.get("blocked"))
    warning = bool(payload.get("warning"))
    daily_cost = float(payload.get("daily_cost_total_usd", 0.0) or 0.0)
    daily_limit = payload.get("daily_cost_limit_usd")
    monthly_cost = float(payload.get("monthly_cost_total_usd", 0.0) or 0.0)
    monthly_limit = payload.get("monthly_cost_limit_usd")

    details: dict[str, Any] = {
        "enabled": enabled,
        "blocked": blocked,
        "warning": warning,
        "daily_cost_total_usd": daily_cost,
        "daily_cost_limit_usd": daily_limit,
        "monthly_cost_total_usd": monthly_cost,
        "monthly_cost_limit_usd": monthly_limit,
        "warnings": payload.get("warnings", []),
        "event_count": payload.get("event_count"),
    }

    if blocked:
        return HealthCheck(
            name="ai_budget",
            severity=SEV_FAIL,
            message=f"AI budget BLOCKED (daily ${daily_cost:.2f} / monthly ${monthly_cost:.2f})",
            details=details,
        )
    if warning or (daily_limit and daily_cost / daily_limit >= 0.8):
        return HealthCheck(
            name="ai_budget",
            severity=SEV_WARN,
            message=(
                f"AI budget warning "
                f"(daily ${daily_cost:.2f}"
                + (f" of ${daily_limit:.2f}" if daily_limit else "")
                + ")"
            ),
            details=details,
        )

    return HealthCheck(
        name="ai_budget",
        severity=SEV_OK,
        message=f"AI budget ok (daily ${daily_cost:.2f} monthly ${monthly_cost:.2f})",
        details=details,
    )


def probe_memo_delivery(repo_root: Path) -> HealthCheck:
    """Read outputs/latest/memo_delivery_status.json and classify."""
    path = repo_root / "outputs" / "latest" / "memo_delivery_status.json"
    payload = _read_json(path)
    if payload is None:
        return HealthCheck(
            name="memo_delivery",
            severity=SEV_INFO,
            message=f"no memo delivery status at {path}",
            details={"path": str(path)},
        )

    enabled = bool(payload.get("enabled"))
    sent = bool(payload.get("sent"))
    skipped = bool(payload.get("skipped"))
    reason = payload.get("reason")
    generated_at = payload.get("generated_at")

    details: dict[str, Any] = {
        "enabled": enabled,
        "sent": sent,
        "skipped": skipped,
        "reason": reason,
        "generated_at": generated_at,
        "recipients_count": payload.get("recipients_count"),
    }

    if not enabled:
        return HealthCheck(
            name="memo_delivery",
            severity=SEV_INFO,
            message="memo email delivery disabled (MEMO_EMAIL_ENABLED=0)",
            details=details,
        )
    if sent:
        return HealthCheck(
            name="memo_delivery",
            severity=SEV_OK,
            message=f"memo email sent (reason={reason or 'sent'})",
            details=details,
        )
    if skipped:
        return HealthCheck(
            name="memo_delivery",
            severity=SEV_INFO,
            message=f"memo email skipped (reason={reason})",
            details=details,
        )
    return HealthCheck(
        name="memo_delivery",
        severity=SEV_WARN,
        message=f"memo email enabled but not sent (reason={reason})",
        details=details,
    )


def probe_decision_outcomes(repo_root: Path) -> HealthCheck:
    """Read outputs/policy/decision_outcome_summary.json and classify."""
    path = repo_root / "outputs" / "policy" / "decision_outcome_summary.json"
    payload = _read_json(path)
    if payload is None:
        return HealthCheck(
            name="decision_outcomes",
            severity=SEV_INFO,
            message=f"no decision outcome summary at {path}",
            details={"path": str(path)},
        )

    total = payload.get("total_decisions")
    resolved = payload.get("resolved")
    hit_rate = payload.get("hit_rate")
    avg_return = payload.get("avg_return_pct")
    details = {
        "total_decisions": total,
        "resolved": resolved,
        "unresolved": payload.get("unresolved"),
        "hit_rate": hit_rate,
        "avg_return_pct": avg_return,
    }
    msg_parts = []
    if isinstance(total, int):
        msg_parts.append(f"{total} decisions tracked")
    if isinstance(resolved, int):
        msg_parts.append(f"{resolved} resolved")
    if isinstance(hit_rate, (int, float)):
        msg_parts.append(f"hit_rate={hit_rate:.2%}")
    if isinstance(avg_return, (int, float)):
        msg_parts.append(f"avg_return={avg_return:.2%}")
    return HealthCheck(
        name="decision_outcomes",
        severity=SEV_INFO,
        message=", ".join(msg_parts) if msg_parts else "decision outcome summary present",
        details=details,
    )


def probe_registry_artifacts(repo_root: Path) -> HealthCheck:
    """
    Walk the artifact registry and report missing non-optional, non-append-only
    entries.  Optional artifacts that are absent are listed in details but
    don't elevate severity.
    """
    try:
        from portfolio_automation.artifacts_registry import REGISTRY, artifact_path
    except Exception as exc:
        return HealthCheck(
            name="registry_probe",
            severity=SEV_INFO,
            message=f"artifacts_registry not importable: {exc}",
        )

    base_outputs = repo_root / "outputs"
    missing_required: list[str] = []
    missing_optional: list[str] = []
    present_required = 0

    for art in REGISTRY:
        if art.append_only:
            continue  # JSONL audit logs grow over time; existence isn't a health signal
        path = artifact_path(art.name, base_dir=base_outputs)
        if path.exists():
            if not art.optional:
                present_required += 1
            continue
        if art.optional:
            missing_optional.append(art.name)
        else:
            missing_required.append(art.name)

    details = {
        "present_required": present_required,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
    }

    if missing_required:
        return HealthCheck(
            name="registry_probe",
            severity=SEV_WARN,
            message=(
                f"{len(missing_required)} non-optional registered artifact(s) missing: "
                + ", ".join(missing_required)
            ),
            details=details,
        )
    if missing_optional:
        return HealthCheck(
            name="registry_probe",
            severity=SEV_INFO,
            message=(
                f"all non-optional artifacts present; "
                f"{len(missing_optional)} optional missing: "
                + ", ".join(missing_optional)
            ),
            details=details,
        )
    return HealthCheck(
        name="registry_probe",
        severity=SEV_OK,
        message=f"all {present_required} non-optional registered artifacts present",
        details=details,
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def collect_status(repo_root: Path) -> StatusReport:
    """Run every probe and assemble a :class:`StatusReport`. Never raises."""
    checks: list[HealthCheck] = []
    for probe in (
        probe_pipeline_run_status,
        probe_sandbox_run_status,
        probe_ai_budget,
        probe_memo_delivery,
        probe_decision_outcomes,
        probe_registry_artifacts,
    ):
        try:
            checks.append(probe(repo_root))
        except Exception as exc:
            checks.append(HealthCheck(
                name=probe.__name__.replace("probe_", ""),
                severity=SEV_WARN,
                message=f"probe raised: {exc}",
            ))
    return StatusReport(
        generated_at=_now_iso(),
        repo_root=str(repo_root),
        checks=checks,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_SEV_LABEL = {
    SEV_OK:   "[ OK ]",
    SEV_INFO: "[INFO]",
    SEV_WARN: "[WARN]",
    SEV_FAIL: "[FAIL]",
}


def render_text(report: StatusReport, *, verbose: bool = False) -> str:
    """Render the report as plain text suitable for a terminal or cron log."""
    lines: list[str] = []
    lines.append("Portfolio Automation — Status")
    lines.append("=" * 40)
    lines.append(f"Generated: {report.generated_at}")
    lines.append(f"Repo:      {report.repo_root}")
    counts = report.severity_counts
    lines.append(
        f"Overall:   {report.overall_severity}  "
        f"(ok={counts[SEV_OK]}, info={counts[SEV_INFO]}, "
        f"warn={counts[SEV_WARN]}, fail={counts[SEV_FAIL]})"
    )
    lines.append("")
    for c in report.checks:
        if not verbose and c.severity == SEV_OK and report.overall_severity != SEV_OK:
            # Hide OK checks when surfacing problems unless verbose
            continue
        if not verbose and c.severity == SEV_INFO and report.overall_severity in (SEV_WARN, SEV_FAIL):
            continue
        label = _SEV_LABEL.get(c.severity, c.severity)
        lines.append(f"{label} {c.name}: {c.message}")
    if not lines[-1]:
        lines.pop()
    lines.append("")
    lines.append("Advisory only — no trades executed.")
    return "\n".join(lines) + "\n"


def render_json(report: StatusReport) -> str:
    return json.dumps(report.to_dict(), indent=2, default=str)


def render_markdown(report: StatusReport) -> str:
    """Render as Markdown for embedding in a memo or doc."""
    lines: list[str] = []
    lines.append("# Portfolio Automation — Status")
    lines.append("")
    lines.append(f"_Generated: {report.generated_at}_")
    lines.append("")
    lines.append(f"- Repo: `{report.repo_root}`")
    counts = report.severity_counts
    lines.append(
        f"- Overall: **{report.overall_severity}** "
        f"(ok={counts[SEV_OK]}, info={counts[SEV_INFO]}, "
        f"warn={counts[SEV_WARN]}, fail={counts[SEV_FAIL]})"
    )
    lines.append("")
    lines.append("## Checks")
    lines.append("")
    for c in report.checks:
        label = _SEV_LABEL.get(c.severity, c.severity)
        lines.append(f"- {label} `{c.name}` — {c.message}")
    lines.append("")
    lines.append("---")
    lines.append("*Advisory only — no trades executed.*")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.status",
        description=(
            "Read-only health check for the portfolio automation system. "
            "Reads JSON status artifacts and the artifact registry; never writes."
        ),
    )
    p.add_argument(
        "--repo-root", default=None,
        help="Repo root override. Default: directory above this file.",
    )
    p.add_argument(
        "--format", choices=("text", "json", "md"), default="text",
        help="Output format. Default: text.",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Include OK and INFO checks even when the overall severity is OK.",
    )
    p.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero when overall severity is WARN or FAIL.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        root = detect_repo_root(args.repo_root)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    report = collect_status(root)

    if args.format == "json":
        print(render_json(report))
    elif args.format == "md":
        print(render_markdown(report), end="")
    else:
        print(render_text(report, verbose=args.verbose), end="")

    if args.strict and report.overall_severity in (SEV_WARN, SEV_FAIL):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
