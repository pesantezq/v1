"""Operator-control plane for the StockBot advisory system.

This package is a **control plane**, not a pipeline producer. It lets the
dashboard (and CLI) turn dashboard *probes* into allowlisted *work orders* that
a future Claude Code *worker* can pick up — generate a focused worker prompt,
run in a sandbox/worktree, run tests, and report back for human review.

SAFETY MODEL (Phase 1):
  * Observe-only. Nothing here executes trades, places broker orders, restarts
    services, installs dependencies, or runs arbitrary shell commands.
  * The web app can only *create* work orders (append a record). It never
    executes a worker. Worker execution is out of scope for Phase 1.
  * ``requested_action`` text is composed ONLY from the allowlisted probe +
    skill registries. There is no field through which a caller can inject an
    executable command string.
  * Work-order and audit storage is **append-only** JSONL.

NAMESPACE NOTE:
  Operator-control artifacts live under ``outputs/operator_control/`` and are
  written directly by this package — NOT via ``OutputNamespace``. This mirrors
  the existing control-state precedent (``data/*_check_state.json``):
  ``OutputNamespace`` governs *pipeline* artifacts (user-scoped, validated,
  consumed by the daily run); the operator-control plane is human-triggered
  governance state that sits *over* the pipeline, so it owns its own directory.
  See ``docs/operator_control.md`` for the rationale.
"""
from __future__ import annotations

from pathlib import Path

OPERATOR_CONTROL_DIRNAME = "operator_control"


def operator_control_dir(root: Path | str) -> Path:
    """Return ``{root}/outputs/operator_control`` (created on demand by writers)."""
    return Path(root) / "outputs" / OPERATOR_CONTROL_DIRNAME


def work_orders_path(root: Path | str) -> Path:
    return operator_control_dir(root) / "work_orders.jsonl"


def audit_log_path(root: Path | str) -> Path:
    return operator_control_dir(root) / "audit_log.jsonl"


def prompts_dir(root: Path | str) -> Path:
    return operator_control_dir(root) / "prompts"


def reports_dir(root: Path | str) -> Path:
    return operator_control_dir(root) / "reports"


def prompt_path(root: Path | str, work_order_id: str) -> Path:
    return prompts_dir(root) / f"{work_order_id}.md"


def report_path(root: Path | str, work_order_id: str) -> Path:
    return reports_dir(root) / f"{work_order_id}.md"


__all__ = [
    "OPERATOR_CONTROL_DIRNAME",
    "operator_control_dir",
    "work_orders_path",
    "audit_log_path",
    "prompts_dir",
    "reports_dir",
    "prompt_path",
    "report_path",
]
