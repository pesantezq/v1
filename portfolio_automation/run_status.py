"""
Pipeline Run Status — Official-Lane Status Artifact
====================================================

Additive, observe-only module that produces a single, machine-readable status
artifact for every official-lane pipeline invocation, mirroring the existing
sandbox-lane ``sandbox_run_status.json`` shape.

Design constraints (do not loosen without explicit approval):

- Read-only with respect to all business logic.  This module never mutates
  scoring, allocations, recommendations, decisions, or any existing output.
- Never raises from :func:`write_pipeline_run_status` — write failures are
  captured in the returned dict so the caller can record them without
  aborting the pipeline.
- Only writes inside ``OutputNamespace.LATEST`` (``outputs/latest/``).
- Hardcoded safety flags ``observe_only=true``, ``no_trade=true``.

Outputs::

    outputs/latest/pipeline_run_status.json
    outputs/latest/pipeline_run_status.md
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger(__name__)

STATUS_JSON_RELATIVE = "pipeline_run_status.json"
STATUS_MD_RELATIVE = "pipeline_run_status.md"

_OBSERVE_ONLY = True
_NO_TRADE = True

_SAFETY_DISCLAIMER = (
    "Official daily pipeline status is advisory observation. "
    "The system does not execute trades, call broker APIs, or place orders. "
    "All outputs are recommendations and analysis artifacts."
)

_STATUS_SUCCEEDED = "succeeded"
_STATUS_FAILED = "failed"
_STATUS_SKIPPED = "skipped"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_run_id(run_mode: str, *, generated_at: str | None = None) -> str:
    """Build a stable run_id: ``YYYY-MM-DD_<mode>_official``."""
    ts = generated_at or _now_iso()
    return f"{ts[:10]}_{run_mode}_official"


@dataclass
class StepStatus:
    """One pipeline step's outcome, normalised across entry points."""
    name: str
    status: str  # "succeeded" | "failed" | "skipped"
    duration_seconds: float = 0.0
    notes: str = ""
    error: str | None = None
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "duration_seconds": round(float(self.duration_seconds or 0.0), 6),
        }
        if self.notes:
            payload["notes"] = self.notes
        if self.error is not None:
            payload["error"] = self.error
        if self.skip_reason is not None:
            payload["skip_reason"] = self.skip_reason
        return payload


@dataclass
class PipelineRunStatus:
    """Aggregate status of one official-lane pipeline invocation."""
    generated_at: str
    run_id: str
    run_mode: str
    source: str  # "main" | "run_daily_pipeline" | future entry points
    success: bool
    exit_code: int
    steps: list[StepStatus] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifacts_written: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def steps_attempted(self) -> int:
        return len(self.steps)

    @property
    def steps_succeeded(self) -> int:
        return sum(1 for s in self.steps if s.status == _STATUS_SUCCEEDED)

    @property
    def steps_failed(self) -> int:
        return sum(1 for s in self.steps if s.status == _STATUS_FAILED)

    @property
    def steps_skipped(self) -> int:
        return sum(1 for s in self.steps if s.status == _STATUS_SKIPPED)


def status_from_main_result(
    result: dict[str, Any],
    *,
    run_mode: str,
    duration_seconds: float | None = None,
    run_id: str | None = None,
) -> PipelineRunStatus:
    """
    Build a :class:`PipelineRunStatus` from ``main.run_portfolio_update``'s
    result dict.  ``main.py`` does not track per-step results, so this adapter
    emits one composite step ``run_portfolio_update`` whose status mirrors
    ``result['success']``.
    """
    generated_at = _now_iso()
    _run_id = run_id or make_run_id(run_mode, generated_at=generated_at)
    success = bool(result.get("success", False))
    errors = [str(e) for e in (result.get("errors") or [])]
    warnings = [str(w) for w in (result.get("warnings") or [])]

    composite_notes = str(result.get("decision_plan_summary") or "")[:200]
    composite_error: str | None = None
    if not success and errors:
        composite_error = "; ".join(errors)[:500]

    step = StepStatus(
        name="run_portfolio_update",
        status=_STATUS_SUCCEEDED if success else _STATUS_FAILED,
        duration_seconds=float(duration_seconds or 0.0),
        notes=composite_notes if success else "",
        error=composite_error,
    )

    scanner = result.get("scanner")
    if isinstance(scanner, dict):
        cands = scanner.get("candidates")
        scanner_candidate_count: int | None = len(cands) if isinstance(cands, list) else None
    else:
        scanner_candidate_count = None
    summary = {
        "drawdown_regime": result.get("drawdown_regime"),
        "degraded_mode": bool(result.get("degraded_mode")),
        "degraded_reason": result.get("degraded_reason"),
        "data_mode": result.get("data_mode"),
        "decision_plan_count": len(result.get("decision_plan") or []),
        "scanner_candidate_count": scanner_candidate_count,
    }

    return PipelineRunStatus(
        generated_at=generated_at,
        run_id=_run_id,
        run_mode=run_mode,
        source="main",
        success=success,
        exit_code=0 if success else 1,
        steps=[step],
        errors=errors,
        warnings=warnings,
        artifacts_written=[],
        summary=summary,
    )


_STATUS_MAP = {
    "ok": _STATUS_SUCCEEDED,
    "succeeded": _STATUS_SUCCEEDED,
    "failed": _STATUS_FAILED,
    "skipped": _STATUS_SKIPPED,
}


def status_from_pipeline_steps(
    step_results: Iterable[Any],
    *,
    run_mode: str = "daily",
    run_id: str | None = None,
) -> PipelineRunStatus:
    """
    Build a :class:`PipelineRunStatus` from
    ``run_daily_pipeline.StepResult`` objects.  Duck-typed: each item must
    expose ``.name``, ``.status``, ``.duration_sec``, ``.notes``.
    """
    generated_at = _now_iso()
    _run_id = run_id or make_run_id(run_mode, generated_at=generated_at)

    steps: list[StepStatus] = []
    for raw in step_results:
        raw_status = str(getattr(raw, "status", "") or "").lower()
        mapped = _STATUS_MAP.get(raw_status, _STATUS_FAILED if raw_status else _STATUS_FAILED)
        notes = str(getattr(raw, "notes", "") or "")
        error: str | None = None
        skip_reason: str | None = None
        kept_notes = ""
        if mapped == _STATUS_FAILED:
            error = notes or None
        elif mapped == _STATUS_SKIPPED:
            skip_reason = notes or None
        else:
            kept_notes = notes
        steps.append(
            StepStatus(
                name=str(getattr(raw, "name", "unknown")),
                status=mapped,
                duration_seconds=float(getattr(raw, "duration_sec", 0.0) or 0.0),
                notes=kept_notes,
                error=error,
                skip_reason=skip_reason,
            )
        )

    failed = sum(1 for st in steps if st.status == _STATUS_FAILED)
    success = failed == 0
    return PipelineRunStatus(
        generated_at=generated_at,
        run_id=_run_id,
        run_mode=run_mode,
        source="run_daily_pipeline",
        success=success,
        exit_code=0 if success else 1,
        steps=steps,
        errors=[],
        warnings=[],
        artifacts_written=[],
        summary={},
    )


def build_status_payload(status: PipelineRunStatus) -> dict[str, Any]:
    """Serialise a :class:`PipelineRunStatus` to the JSON artifact shape."""
    return {
        "generated_at": status.generated_at,
        "run_id": status.run_id,
        "source": status.source,
        "run_mode": status.run_mode,
        "observe_only": _OBSERVE_ONLY,
        "no_trade": _NO_TRADE,
        "disclaimer": _SAFETY_DISCLAIMER,
        "success": status.success,
        "exit_code": status.exit_code,
        "steps_attempted": status.steps_attempted,
        "steps_succeeded": status.steps_succeeded,
        "steps_skipped": status.steps_skipped,
        "steps_failed": status.steps_failed,
        "steps": [s.to_dict() for s in status.steps],
        "errors": list(status.errors),
        "warnings": list(status.warnings),
        "artifacts_written": list(status.artifacts_written),
        "summary": dict(status.summary),
    }


def render_status_markdown(payload: dict[str, Any]) -> str:
    """Render the JSON payload as an operator-readable Markdown document."""
    lines: list[str] = []
    lines.append("# Pipeline Run — Status")
    lines.append("")
    lines.append(f"_Generated: {payload['generated_at']}_")
    lines.append("")
    lines.append("> " + payload["disclaimer"])
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Run ID: `{payload['run_id']}`")
    lines.append(f"- Source: `{payload['source']}`")
    lines.append(f"- Run mode: `{payload['run_mode']}`")
    lines.append(f"- Success: {payload['success']}")
    lines.append(f"- Exit code: {payload['exit_code']}")
    lines.append(
        f"- Steps: attempted={payload['steps_attempted']}, "
        f"succeeded={payload['steps_succeeded']}, "
        f"skipped={payload['steps_skipped']}, "
        f"failed={payload['steps_failed']}"
    )
    lines.append("")
    lines.append("## Safety flags")
    lines.append("")
    lines.append(f"- `observe_only`: {payload['observe_only']}")
    lines.append(f"- `no_trade`: {payload['no_trade']}")
    lines.append("")
    lines.append("## Steps")
    lines.append("")
    for step in payload["steps"]:
        marker = {
            _STATUS_SUCCEEDED: "OK",
            _STATUS_FAILED: "FAIL",
            _STATUS_SKIPPED: "SKIP",
        }.get(step["status"], step["status"].upper())
        lines.append(f"### [{marker}] `{step['name']}`")
        lines.append(f"- Duration: {step['duration_seconds']:.3f}s")
        if step.get("notes"):
            lines.append(f"- Notes: {step['notes']}")
        if step.get("skip_reason"):
            lines.append(f"- Skip reason: {step['skip_reason']}")
        if step.get("error"):
            lines.append(f"- Error: `{step['error']}`")
        lines.append("")
    if payload["errors"]:
        lines.append("## Errors")
        lines.append("")
        for err in payload["errors"]:
            lines.append(f"- {err}")
        lines.append("")
    if payload["warnings"]:
        lines.append("## Warnings")
        lines.append("")
        for warn in payload["warnings"]:
            lines.append(f"- {warn}")
        lines.append("")
    if payload["summary"]:
        lines.append("## Summary fields")
        lines.append("")
        for key, val in payload["summary"].items():
            lines.append(f"- `{key}`: {val}")
        lines.append("")
    if payload["artifacts_written"]:
        lines.append("## Artifact paths written")
        lines.append("")
        for path in payload["artifacts_written"]:
            lines.append(f"- `{path}`")
        lines.append("")
    lines.append("---")
    lines.append(f"*Source: {payload['source']}*")
    return "\n".join(lines)


def write_pipeline_run_status(
    status: PipelineRunStatus,
    *,
    base_dir: Path | str = "outputs",
) -> dict[str, str]:
    """
    Persist *status* as ``outputs/latest/pipeline_run_status.{json,md}``.

    Always returns a dict.  On success::

        {"pipeline_run_status_json": "...", "pipeline_run_status_md": "..."}

    On any failure::

        {"error": "<message>"}

    Never raises.
    """
    try:
        payload = build_status_payload(status)
        json_path = safe_write_json(
            OutputNamespace.LATEST,
            STATUS_JSON_RELATIVE,
            payload,
            base_dir=base_dir,
        )
        md_path = safe_write_text(
            OutputNamespace.LATEST,
            STATUS_MD_RELATIVE,
            render_status_markdown(payload),
            base_dir=base_dir,
        )
        return {
            "pipeline_run_status_json": str(json_path),
            "pipeline_run_status_md": str(md_path),
        }
    except Exception as exc:
        logger.error("Failed to write pipeline_run_status artifacts: %s", exc)
        return {"error": str(exc)}
