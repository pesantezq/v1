"""
Daily Sandbox Run — Orchestrates the sandbox/research lane.
==============================================================

Refreshes the sandbox/research-lane artifacts on a daily cadence without
touching the official daily portfolio pipeline, the email memo delivery
path, or any official portfolio state.

This runner is a thin, additive orchestration layer.  It calls existing
sandbox-safe entry points and aggregates their results into a single
status artifact.  It never reimplements module logic.

Safety invariants (hardcoded, never conditional):
  - observe_only: true
  - no_trade: true
  - not_recommendation: true
  - discovery_only: true
  - no_portfolio_mutation: true
  - no_watchlist_mutation: true
  - no_allocation_policy_change: true
  - no_decision_override: true
  - no_score_mutation: true
  - No broker/API calls
  - No LLM/AI calls inside this orchestrator
  - Only writes to OutputNamespace.SANDBOX
  - Run mode hardcoded to RunMode.DISCOVERY for every step
  - Non-blocking: a failed step does not abort sibling steps and never
    aborts the official daily pipeline (which runs separately)

CLI::

    python -m tools.daily_sandbox_run
    python -m tools.daily_sandbox_run --base-dir /opt/stockbot
    python -m tools.daily_sandbox_run --dry-run

Outputs::

    outputs/sandbox/discovery/sandbox_run_status.json
    outputs/sandbox/discovery/sandbox_run_status.md
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from portfolio_automation.data_governance import (
    OutputNamespace,
    get_output_path,
    safe_write_json,
    safe_write_text,
)
from portfolio_automation.run_mode_governance import RunMode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — safety
# ---------------------------------------------------------------------------

_SOURCE_LABEL = "daily_sandbox_run"
_RUN_MODE_LITERAL = RunMode.DISCOVERY.value  # hardcoded; never configurable

_OBSERVE_ONLY = True
_NO_TRADE = True
_NOT_RECOMMENDATION = True
_DISCOVERY_ONLY = True
_NO_PORTFOLIO_MUTATION = True
_NO_WATCHLIST_MUTATION = True
_NO_ALLOCATION_POLICY_CHANGE = True
_NO_DECISION_OVERRIDE = True
_NO_SCORE_MUTATION = True

_SAFETY_DISCLAIMER = (
    "Daily sandbox run is research-only observation. "
    "It does not execute trades, call broker APIs, mutate official portfolio "
    "or watchlist state, change allocation policy, or emit BUY/SELL/HOLD "
    "recommendations. All outputs are sandbox/discovery research artifacts."
)

# ---------------------------------------------------------------------------
# Constants — artifact paths
# ---------------------------------------------------------------------------

_STATUS_JSON_RELATIVE = "discovery/sandbox_run_status.json"
_STATUS_MD_RELATIVE = "discovery/sandbox_run_status.md"

# Sandbox files we read for status counts (relative to SANDBOX root).
_EMERGING_CANDIDATES_PATH = "discovery/emerging_candidates.json"
_REJECTED_CANDIDATES_PATH = "discovery/rejected_candidates.json"
_NEWS_ENRICHED_PATH = "discovery/news_enriched_candidates.json"
_NEWS_EVIDENCE_PATH = "discovery/news_candidate_evidence.json"
_PROMOTION_CANDIDATES_PATH = "discovery/automatic_promotion_candidates.json"
_PROMOTION_DECISIONS_PATH = "discovery/automatic_promotion_decisions.jsonl"

# Optional replay input — if present, the replay step runs; otherwise skipped.
_REPLAY_INPUT_PATH = "discovery/replay_price_outcomes.json"


# ---------------------------------------------------------------------------
# Status dataclasses
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    name: str
    status: str  # "succeeded" | "failed" | "skipped"
    started_at: str
    finished_at: str
    duration_seconds: float
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 6),
            "summary": self.summary,
        }
        if self.error is not None:
            payload["error"] = self.error
        if self.skip_reason is not None:
            payload["skip_reason"] = self.skip_reason
        return payload


@dataclass
class SandboxRunResult:
    generated_at: str
    run_id: str
    steps: list[StepResult] = field(default_factory=list)
    candidate_counts: dict[str, Any] = field(default_factory=dict)
    news_evidence_counts: dict[str, Any] = field(default_factory=dict)
    automatic_promotion_counts: dict[str, Any] = field(default_factory=dict)
    artifact_paths_written: list[str] = field(default_factory=list)
    status_paths: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False

    @property
    def steps_attempted(self) -> int:
        return len(self.steps)

    @property
    def steps_succeeded(self) -> int:
        return sum(1 for s in self.steps if s.status == "succeeded")

    @property
    def steps_failed(self) -> int:
        return sum(1 for s in self.steps if s.status == "failed")

    @property
    def steps_skipped(self) -> int:
        return sum(1 for s in self.steps if s.status == "skipped")

    @property
    def errors(self) -> list[dict[str, str]]:
        return [
            {"step": s.name, "error": s.error or ""}
            for s in self.steps if s.status == "failed" and s.error
        ]


# ---------------------------------------------------------------------------
# Step wrappers — each must NEVER raise; they always return a StepResult.
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_step(name: str, fn: Callable[[], dict[str, Any]]) -> StepResult:
    """Wrap *fn* so any exception is recorded as a failed step, not raised."""
    started = _now_iso()
    t0 = time.monotonic()
    try:
        summary = fn() or {}
    except Exception as exc:
        finished = _now_iso()
        logger.warning("sandbox step %s failed: %s", name, exc, exc_info=True)
        return StepResult(
            name=name,
            status="failed",
            started_at=started,
            finished_at=finished,
            duration_seconds=time.monotonic() - t0,
            summary={},
            error=str(exc),
        )
    finished = _now_iso()
    # The wrapped module may itself report an internal error — surface it as
    # "failed" rather than "succeeded".
    if isinstance(summary, dict) and summary.get("error"):
        return StepResult(
            name=name,
            status="failed",
            started_at=started,
            finished_at=finished,
            duration_seconds=time.monotonic() - t0,
            summary=summary,
            error=str(summary.get("error")),
        )
    return StepResult(
        name=name,
        status="succeeded",
        started_at=started,
        finished_at=finished,
        duration_seconds=time.monotonic() - t0,
        summary=summary,
    )


def _skipped(name: str, reason: str) -> StepResult:
    ts = _now_iso()
    return StepResult(
        name=name,
        status="skipped",
        started_at=ts,
        finished_at=ts,
        duration_seconds=0.0,
        summary={},
        skip_reason=reason,
    )


# ---------------------------------------------------------------------------
# Read-only helpers — load existing sandbox artifacts for status counts
# ---------------------------------------------------------------------------

def _read_json_safely(path: Path) -> Any | None:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return None


def _count_jsonl_lines(path: Path) -> int | None:
    try:
        if not path.exists():
            return None
        count = 0
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    count += 1
        return count
    except Exception as exc:
        logger.warning("Failed to count %s: %s", path, exc)
        return None


def _sandbox_path(base_dir: Path, relative: str) -> Path:
    return get_output_path(
        OutputNamespace.SANDBOX, relative, base_dir=Path(base_dir) / "outputs"
    )


def _collect_candidate_counts(base_dir: Path) -> dict[str, Any]:
    emerging = _read_json_safely(_sandbox_path(base_dir, _EMERGING_CANDIDATES_PATH))
    rejected = _read_json_safely(_sandbox_path(base_dir, _REJECTED_CANDIDATES_PATH))
    enriched = _read_json_safely(_sandbox_path(base_dir, _NEWS_ENRICHED_PATH))

    def _count_list(payload: Any, key: str) -> int | None:
        if not isinstance(payload, dict):
            return None
        items = payload.get(key)
        if isinstance(items, list):
            return len(items)
        return None

    counts: dict[str, Any] = {
        "emerging": _count_list(emerging, "candidates"),
        "rejected": _count_list(rejected, "candidates"),
        "enriched": _count_list(enriched, "candidates"),
    }
    return counts


def _collect_news_evidence_counts(base_dir: Path) -> dict[str, Any]:
    evidence = _read_json_safely(_sandbox_path(base_dir, _NEWS_EVIDENCE_PATH))
    enriched = _read_json_safely(_sandbox_path(base_dir, _NEWS_ENRICHED_PATH))

    def _evidence_packet_count(payload: Any) -> int | None:
        if not isinstance(payload, dict):
            return None
        items = payload.get("evidence_packets")
        if isinstance(items, list):
            return len(items)
        return None

    with_news = None
    if isinstance(enriched, dict):
        cands = enriched.get("candidates")
        if isinstance(cands, list):
            with_news = sum(
                1 for c in cands
                if isinstance(c, dict) and (c.get("matched_news_count") or 0) > 0
            )

    return {
        "evidence_packets": _evidence_packet_count(evidence),
        "with_news": with_news,
    }


def _collect_automatic_promotion_counts(base_dir: Path) -> dict[str, Any]:
    payload = _read_json_safely(_sandbox_path(base_dir, _PROMOTION_CANDIDATES_PATH))
    if not isinstance(payload, dict):
        return {
            "decision_count": None,
            "monitor": None,
            "needs_review": None,
            "rejected": None,
            "expired": None,
            "decisions_jsonl_lines": _count_jsonl_lines(
                _sandbox_path(base_dir, _PROMOTION_DECISIONS_PATH)
            ),
        }
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        decisions = []
    by_status: dict[str, int] = {}
    for d in decisions:
        if isinstance(d, dict):
            s = str(d.get("proposed_status") or "").upper()
            by_status[s] = by_status.get(s, 0) + 1
    return {
        "decision_count": len(decisions),
        "monitor": by_status.get("MONITOR", 0),
        "needs_review": by_status.get("NEEDS_REVIEW", 0),
        "rejected": by_status.get("REJECTED", 0),
        "expired": by_status.get("EXPIRED", 0),
        "decisions_jsonl_lines": _count_jsonl_lines(
            _sandbox_path(base_dir, _PROMOTION_DECISIONS_PATH)
        ),
    }


def _collect_artifact_paths(step_results: list[StepResult]) -> list[str]:
    paths: list[str] = []
    for s in step_results:
        artifacts = s.summary.get("artifacts") if isinstance(s.summary, dict) else None
        if isinstance(artifacts, dict):
            for v in artifacts.values():
                if isinstance(v, (str, Path)) and v:
                    paths.append(str(v))
        elif isinstance(artifacts, list):
            for v in artifacts:
                if isinstance(v, (str, Path)) and v:
                    paths.append(str(v))
    # Deduplicate, preserve order
    seen: set[str] = set()
    unique: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


# ---------------------------------------------------------------------------
# Step definitions
# ---------------------------------------------------------------------------

def _step_discovery_news_integration(base_dir: Path) -> dict[str, Any]:
    # Imported lazily so a missing module is reported as a step failure
    # rather than a top-level import error.
    from portfolio_automation.discovery import run_discovery_news_integration  # noqa: WPS433
    return run_discovery_news_integration(
        base_dir=str(Path(base_dir) / "outputs"),
        run_mode=_RUN_MODE_LITERAL,
    )


def _step_automatic_promotion_governance(base_dir: Path) -> dict[str, Any]:
    from portfolio_automation.discovery import run_automatic_promotion_governance  # noqa: WPS433
    return run_automatic_promotion_governance(
        base_dir=str(Path(base_dir) / "outputs"),
        run_mode=_RUN_MODE_LITERAL,
        write_files=True,
    )


def _step_discovery_replay_if_inputs_present(base_dir: Path) -> StepResult:
    """
    Run discovery replay only if a price/outcome input file is present in
    the sandbox.  Otherwise return a 'skipped' step.

    The replay module accepts injected outcomes — this runner never fetches
    external price data.  Provisioning of ``replay_price_outcomes.json`` is
    an explicit operator action; absence is the normal state.
    """
    input_path = _sandbox_path(base_dir, _REPLAY_INPUT_PATH)
    if not input_path.exists():
        return _skipped(
            "discovery_replay",
            f"no replay input at {input_path}",
        )
    payload = _read_json_safely(input_path)
    if not isinstance(payload, dict):
        return _skipped(
            "discovery_replay",
            f"replay input at {input_path} is not a JSON object",
        )
    price_outcomes = payload.get("price_outcomes")
    if not isinstance(price_outcomes, dict) or not price_outcomes:
        return _skipped(
            "discovery_replay",
            "replay input present but 'price_outcomes' is missing or empty",
        )

    def _do() -> dict[str, Any]:
        from portfolio_automation.discovery import run_discovery_replay  # noqa: WPS433
        return run_discovery_replay(
            price_outcomes=price_outcomes,
            run_mode=_RUN_MODE_LITERAL,
            base_dir=str(Path(base_dir) / "outputs"),
            write_files=True,
        )
    return _safe_step("discovery_replay", _do)


# ---------------------------------------------------------------------------
# Status payload builders
# ---------------------------------------------------------------------------

def _build_status_payload(result: SandboxRunResult) -> dict[str, Any]:
    return {
        "generated_at": result.generated_at,
        "run_id": result.run_id,
        "source": _SOURCE_LABEL,
        "run_mode": _RUN_MODE_LITERAL,
        # Hardcoded safety flags
        "observe_only": _OBSERVE_ONLY,
        "no_trade": _NO_TRADE,
        "not_recommendation": _NOT_RECOMMENDATION,
        "discovery_only": _DISCOVERY_ONLY,
        "no_portfolio_mutation": _NO_PORTFOLIO_MUTATION,
        "no_watchlist_mutation": _NO_WATCHLIST_MUTATION,
        "no_allocation_policy_change": _NO_ALLOCATION_POLICY_CHANGE,
        "no_decision_override": _NO_DECISION_OVERRIDE,
        "no_score_mutation": _NO_SCORE_MUTATION,
        "disclaimer": _SAFETY_DISCLAIMER,
        # Step tallies
        "steps_attempted": result.steps_attempted,
        "steps_succeeded": result.steps_succeeded,
        "steps_skipped": result.steps_skipped,
        "steps_failed": result.steps_failed,
        "steps": [s.to_dict() for s in result.steps],
        "errors": result.errors,
        # Aggregate counts
        "candidate_counts": result.candidate_counts,
        "news_evidence_counts": result.news_evidence_counts,
        "automatic_promotion_counts": result.automatic_promotion_counts,
        "artifact_paths_written": result.artifact_paths_written,
        "dry_run": result.dry_run,
    }


def _render_status_markdown(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Daily Sandbox Run — Status")
    lines.append("")
    lines.append(f"_Generated: {payload['generated_at']}_")
    lines.append("")
    lines.append("> " + payload["disclaimer"])
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Run ID: `{payload['run_id']}`")
    lines.append(f"- Run mode: `{payload['run_mode']}`")
    lines.append(
        f"- Steps: attempted={payload['steps_attempted']}, "
        f"succeeded={payload['steps_succeeded']}, "
        f"skipped={payload['steps_skipped']}, "
        f"failed={payload['steps_failed']}"
    )
    lines.append(f"- Dry run: {payload['dry_run']}")
    lines.append("")
    lines.append("## Safety flags")
    lines.append("")
    for key in (
        "observe_only", "no_trade", "not_recommendation", "discovery_only",
        "no_portfolio_mutation", "no_watchlist_mutation",
        "no_allocation_policy_change", "no_decision_override",
        "no_score_mutation",
    ):
        lines.append(f"- `{key}`: {payload[key]}")
    lines.append("")
    lines.append("## Steps")
    lines.append("")
    for step in payload["steps"]:
        marker = {
            "succeeded": "OK",
            "failed":    "FAIL",
            "skipped":   "SKIP",
        }.get(step["status"], step["status"].upper())
        lines.append(f"### [{marker}] `{step['name']}`")
        lines.append(f"- Started: {step['started_at']}")
        lines.append(f"- Finished: {step['finished_at']}")
        lines.append(f"- Duration: {step['duration_seconds']:.3f}s")
        if step.get("skip_reason"):
            lines.append(f"- Skip reason: {step['skip_reason']}")
        if step.get("error"):
            lines.append(f"- Error: `{step['error']}`")
        lines.append("")

    lines.append("## Counts")
    lines.append("")
    lines.append("### Discovery candidates")
    for k, v in payload["candidate_counts"].items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("### News evidence")
    for k, v in payload["news_evidence_counts"].items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("### Automatic promotion")
    for k, v in payload["automatic_promotion_counts"].items():
        lines.append(f"- {k}: {v}")
    lines.append("")

    lines.append("## Artifact paths written")
    lines.append("")
    if payload["artifact_paths_written"]:
        for p in payload["artifact_paths_written"]:
            lines.append(f"- `{p}`")
    else:
        lines.append("_(none)_")
    lines.append("")

    lines.append("---")
    lines.append(f"*Source: {_SOURCE_LABEL}*")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_daily_sandbox(
    *,
    base_dir: Path | str = ".",
    run_id: str | None = None,
    dry_run: bool = False,
) -> SandboxRunResult:
    """
    Execute the daily sandbox lane.  Always returns a :class:`SandboxRunResult`;
    never raises (errors are recorded as failed steps).

    Parameters
    ----------
    base_dir:
        Project root directory (the directory that contains ``outputs/``).
        Defaults to the current working directory.
    run_id:
        Optional run identifier; defaults to a timestamp-based one.
    dry_run:
        If True, the runner still calls the existing module entry points
        but does not write the ``sandbox_run_status`` artifacts.  Module
        entry points may still write their own artifacts.  Use
        ``--no-call-modules`` (CLI) to skip the steps entirely.
    """
    base = Path(base_dir).resolve()
    generated_at = _now_iso()
    _run_id = run_id or f"{generated_at[:10]}_daily_sandbox_run"

    step_results: list[StepResult] = []

    # Step 1 — Discovery news integration
    step_results.append(
        _safe_step(
            "discovery_news_integration",
            lambda: _step_discovery_news_integration(base),
        )
    )

    # Step 2 — Automatic promotion governance
    step_results.append(
        _safe_step(
            "automatic_promotion_governance",
            lambda: _step_automatic_promotion_governance(base),
        )
    )

    # Step 3 — Discovery replay (optional; skipped if no inputs)
    step_results.append(_step_discovery_replay_if_inputs_present(base))

    candidate_counts = _collect_candidate_counts(base)
    news_evidence_counts = _collect_news_evidence_counts(base)
    automatic_promotion_counts = _collect_automatic_promotion_counts(base)
    artifact_paths_written = _collect_artifact_paths(step_results)

    result = SandboxRunResult(
        generated_at=generated_at,
        run_id=_run_id,
        steps=step_results,
        candidate_counts=candidate_counts,
        news_evidence_counts=news_evidence_counts,
        automatic_promotion_counts=automatic_promotion_counts,
        artifact_paths_written=artifact_paths_written,
        dry_run=dry_run,
    )

    if not dry_run:
        payload = _build_status_payload(result)
        try:
            json_path = safe_write_json(
                OutputNamespace.SANDBOX,
                _STATUS_JSON_RELATIVE,
                payload,
                base_dir=base / "outputs",
            )
            md_path = safe_write_text(
                OutputNamespace.SANDBOX,
                _STATUS_MD_RELATIVE,
                _render_status_markdown(payload),
                base_dir=base / "outputs",
            )
            result.status_paths = {
                "sandbox_run_status_json": str(json_path),
                "sandbox_run_status_md": str(md_path),
            }
        except Exception as exc:
            logger.error("Failed to write sandbox_run_status artifacts: %s", exc)
            result.status_paths = {"error": str(exc)}

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.daily_sandbox_run",
        description=(
            "Daily sandbox/research lane orchestrator.  Observe-only.  "
            "Does not execute trades, call brokers, or mutate official "
            "portfolio/watchlist state.  Calls existing sandbox modules "
            "only and writes a sandbox_run_status artifact."
        ),
    )
    p.add_argument(
        "--base-dir", default=".",
        help="Project root directory (must contain or create outputs/).",
    )
    p.add_argument(
        "--run-id", default=None,
        help="Optional run identifier (default: timestamp-based).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Run the steps but do not write the sandbox_run_status "
             "artifacts.  Module entry points may still write their own.",
    )
    p.add_argument(
        "--verbose", "-v", action="count", default=0,
        help="Increase logging verbosity (repeat for more).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    level = logging.WARNING
    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose == 1:
        level = logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    result = run_daily_sandbox(
        base_dir=args.base_dir,
        run_id=args.run_id,
        dry_run=args.dry_run,
    )

    print("Daily sandbox run — summary")
    print("---------------------------")
    print(f"Run ID:    {result.run_id}")
    print(f"Run mode:  {_RUN_MODE_LITERAL}")
    print(
        f"Steps:     attempted={result.steps_attempted}, "
        f"succeeded={result.steps_succeeded}, "
        f"skipped={result.steps_skipped}, "
        f"failed={result.steps_failed}"
    )
    for s in result.steps:
        marker = {
            "succeeded": "OK  ",
            "failed":    "FAIL",
            "skipped":   "SKIP",
        }.get(s.status, s.status.upper())
        line = f"  [{marker}] {s.name}"
        if s.error:
            line += f"  error={s.error}"
        elif s.skip_reason:
            line += f"  ({s.skip_reason})"
        print(line)
    if result.status_paths.get("sandbox_run_status_json"):
        print()
        print(f"Status JSON: {result.status_paths['sandbox_run_status_json']}")
        print(f"Status MD:   {result.status_paths['sandbox_run_status_md']}")
    print()
    print(_SAFETY_DISCLAIMER)

    # Non-blocking exit semantics: succeed (0) even if individual steps failed.
    # This keeps the sandbox lane from blocking systemd timer state.  The
    # status artifact is the source of truth for downstream consumers.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
