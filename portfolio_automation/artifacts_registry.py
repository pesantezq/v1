"""
Artifact Registry — Machine-Readable Contract
==============================================

Single source of truth for every output artifact the live pipeline writes.

Today the contract lives in prose in :doc:`docs/OUTPUT_ARTIFACT_CONTRACTS.md`.
Operators and GUI loaders have no programmable way to ask "which artifacts
should exist?", "which namespace does artifact X live in?", or "is artifact
X required or optional?". This module closes that gap with a frozen
dataclass + a populated tuple of :class:`Artifact` entries.

Design constraints (do not loosen without explicit approval):

- **Pure data, no I/O.** Importing this module must not read, write, or stat
  any file.
- **Additive.** Adding a new entry is the supported workflow. Renaming or
  removing entries requires an explicit migration note in the prose contract.
- **No writer behaviour change.** This module does not call writers, validate
  artifact contents at write time, or affect any existing pipeline path.

Usage::

    from portfolio_automation.artifacts_registry import (
        get_artifact, artifact_path, artifacts_for_namespace,
    )

    art = get_artifact("pipeline_run_status")
    path = artifact_path("pipeline_run_status")            # outputs/latest/pipeline_run_status.json
    latest = artifacts_for_namespace(OutputNamespace.LATEST)

Schema fields are documented on the :class:`Artifact` dataclass. The
registry is not exhaustive — it begins with the most-consumed core artifacts
and grows additively as new artifacts gain stable contracts.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from portfolio_automation.data_governance import (
    OutputNamespace,
    get_output_path,
)

# ---------------------------------------------------------------------------
# Formats
# ---------------------------------------------------------------------------

# Allowed values for Artifact.format. Documented here so contract tests can
# reject typos without depending on Enum boilerplate.
FORMAT_JSON = "json"
FORMAT_MARKDOWN = "md"
FORMAT_JSONL = "jsonl"
FORMAT_CSV = "csv"
FORMAT_TEXT = "txt"

ALLOWED_FORMATS: frozenset[str] = frozenset({
    FORMAT_JSON,
    FORMAT_MARKDOWN,
    FORMAT_JSONL,
    FORMAT_CSV,
    FORMAT_TEXT,
})

# File-extension expectation for each format. Used by contract tests to catch
# path/format mismatches (e.g. format=json but path ends in .md).
_EXPECTED_SUFFIX: dict[str, str] = {
    FORMAT_JSON: ".json",
    FORMAT_MARKDOWN: ".md",
    FORMAT_JSONL: ".jsonl",
    FORMAT_CSV: ".csv",
    FORMAT_TEXT: ".txt",
}


# ---------------------------------------------------------------------------
# Artifact schema
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Artifact:
    """
    One registered output artifact.

    Attributes:
        name:
            Short snake_case identifier. Unique across the registry.
        namespace:
            :class:`OutputNamespace` the artifact lives in. Maps deterministically
            to a directory under ``outputs/``.
        relative_path:
            Path within the namespace directory (e.g. ``"pipeline_run_status.json"``
            or ``"discovery/sandbox_run_status.json"``). Never absolute; never
            starts with ``"/"``.
        format:
            One of :data:`ALLOWED_FORMATS`.
        writer_module:
            Dotted Python module that owns the write. Used by tools that need
            to import the writer (e.g. a future contract test that imports the
            module and asserts it is importable).
        writer_function:
            Specific function name within ``writer_module`` that performs the
            write, or ``None`` when the writer is invoked via ``runpy`` or has
            no single canonical entry point.
        consumers:
            Tuple of dotted module paths that READ this artifact.  Informational —
            useful for "if I change artifact X, what breaks" queries.
        schema_version:
            Integer schema version. Bump when fields change in a non-additive way.
        append_only:
            ``True`` when the writer appends rows (``.jsonl`` audit logs).
            ``False`` for overwrite-style artifacts.
        optional:
            ``True`` when the artifact may legitimately be absent (e.g. when a
            feature is disabled).
        observe_only_required:
            ``True`` when consumers should reject the artifact if
            ``observe_only`` is not present and ``True`` in the top-level JSON.
            Set ``False`` for non-JSON artifacts or artifacts that pre-date the
            observe-only convention.
        documented_in:
            Anchor in the prose contract doc (e.g. ``"docs/OUTPUT_ARTIFACT_CONTRACTS.md"``)
            or ``None`` when documentation is pending.
        description:
            One-line operator-facing description.
    """
    name: str
    namespace: OutputNamespace
    relative_path: str
    format: str
    writer_module: str
    writer_function: str | None
    consumers: tuple[str, ...]
    schema_version: int
    append_only: bool
    optional: bool
    observe_only_required: bool
    documented_in: str | None
    description: str


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
#
# This list begins with the most consumed core artifacts. Adding a new entry
# is the supported way to grow it; removals require a documented migration.
#
# Entries are intentionally grouped by namespace then by topic.

REGISTRY: tuple[Artifact, ...] = (

    # ------------------------------------------------------------------
    # LATEST — live per-run operator artifacts
    # ------------------------------------------------------------------

    Artifact(
        name="pipeline_run_status",
        namespace=OutputNamespace.LATEST,
        relative_path="pipeline_run_status.json",
        format=FORMAT_JSON,
        writer_module="portfolio_automation.run_status",
        writer_function="write_pipeline_run_status",
        consumers=(),  # future: portfolio_automation.status CLI
        schema_version=1,
        append_only=False,
        optional=False,
        observe_only_required=True,
        documented_in="docs/PRODUCTION_HARDENING_AUDIT.md",
        description="Official-lane pipeline status: per-step results, success, "
                    "exit_code, observe_only/no_trade safety flags.",
    ),
    Artifact(
        name="pipeline_run_status_md",
        namespace=OutputNamespace.LATEST,
        relative_path="pipeline_run_status.md",
        format=FORMAT_MARKDOWN,
        writer_module="portfolio_automation.run_status",
        writer_function="write_pipeline_run_status",
        consumers=(),
        schema_version=1,
        append_only=False,
        optional=False,
        observe_only_required=False,
        documented_in="docs/PRODUCTION_HARDENING_AUDIT.md",
        description="Operator-readable companion to pipeline_run_status.json.",
    ),
    Artifact(
        name="data_quality_report",
        namespace=OutputNamespace.LATEST,
        relative_path="data_quality_report.json",
        format=FORMAT_JSON,
        writer_module="portfolio_automation.data_quality_monitor",
        writer_function="write_data_quality_report",
        consumers=("agent.bundle_builder", "gui_operator_data"),
        schema_version=1,
        append_only=False,
        optional=False,
        observe_only_required=True,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Observe-only data quality summary: 13 issue types, per-symbol health.",
    ),
    Artifact(
        name="ai_budget_summary",
        namespace=OutputNamespace.LATEST,
        relative_path="ai_budget_summary.json",
        format=FORMAT_JSON,
        writer_module="portfolio_automation.ai_budget",
        writer_function="write_ai_budget_summary",
        consumers=("gui_operator_data",),
        schema_version=1,
        append_only=False,
        optional=False,
        observe_only_required=True,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="AI usage and cost summary; advisory observability only.",
    ),
    Artifact(
        name="watchlist_signals",
        namespace=OutputNamespace.LATEST,
        relative_path="watchlist_signals.json",
        format=FORMAT_JSON,
        writer_module="watchlist_scanner.output_writers",
        writer_function=None,
        consumers=("watchlist_scanner.system_summary", "watchlist_scanner.daily_memo"),
        schema_version=1,
        append_only=False,
        optional=False,
        observe_only_required=False,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Per-symbol watchlist signal scoring, confidence, and ranking outputs.",
    ),
    Artifact(
        name="system_decision_summary",
        namespace=OutputNamespace.LATEST,
        relative_path="system_decision_summary.json",
        format=FORMAT_JSON,
        writer_module="watchlist_scanner.system_summary",
        writer_function="generate_system_decision_summary",
        consumers=("watchlist_scanner.daily_memo", "gui_operator_data"),
        schema_version=1,
        append_only=False,
        # Optional because it is produced by run_daily_pipeline.py, not by
        # main.py.  The production cron (scripts/run_daily.sh) currently
        # invokes main.py only, so this artifact is legitimately absent in
        # production today.
        optional=True,
        observe_only_required=False,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Top-level system state, capital preview, policy insight, "
                    "data health digest. Produced by run_daily_pipeline.py "
                    "(not by main.py).",
    ),
    Artifact(
        name="decision_plan",
        namespace=OutputNamespace.LATEST,
        relative_path="decision_plan.json",
        format=FORMAT_JSON,
        writer_module="portfolio_automation.decision_engine",
        writer_function=None,
        consumers=(
            "portfolio_automation.ai_decision_validator",
            "portfolio_automation.decision_explainer",
            "portfolio_automation.decision_outcome_tracker",
            "watchlist_scanner.daily_memo",
            "gui_operator_data",
        ),
        schema_version=1,
        append_only=False,
        optional=False,
        observe_only_required=True,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Source of truth for advisory decisions. Downstream readers "
                    "never mutate or recompute it.",
    ),
    Artifact(
        name="ai_decision_validation",
        namespace=OutputNamespace.LATEST,
        relative_path="ai_decision_validation.json",
        format=FORMAT_JSON,
        writer_module="portfolio_automation.ai_decision_validator",
        writer_function=None,
        consumers=("portfolio_automation.decision_outcome_tracker", "gui_operator_data"),
        schema_version=1,
        append_only=False,
        optional=False,
        observe_only_required=True,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Deterministic + optional-LLM validation of decision_plan rows.",
    ),
    Artifact(
        name="decision_explanations",
        namespace=OutputNamespace.LATEST,
        relative_path="decision_explanations.json",
        format=FORMAT_JSON,
        writer_module="portfolio_automation.decision_explainer",
        writer_function=None,
        consumers=("watchlist_scanner.daily_memo", "gui_operator_data"),
        schema_version=1,
        append_only=False,
        optional=False,
        observe_only_required=True,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Compact, deterministic explanations of top decisions.",
    ),
    Artifact(
        name="memo_delivery_status",
        namespace=OutputNamespace.LATEST,
        relative_path="memo_delivery_status.json",
        format=FORMAT_JSON,
        writer_module="portfolio_automation.memo_email_sender",
        writer_function=None,
        consumers=("gui_operator_data",),
        schema_version=1,
        append_only=False,
        optional=True,
        observe_only_required=True,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Latest memo email delivery attempt status. Disabled by default.",
    ),
    Artifact(
        name="daily_memo_txt",
        namespace=OutputNamespace.LATEST,
        relative_path="daily_memo.txt",
        format=FORMAT_TEXT,
        writer_module="watchlist_scanner.daily_memo",
        writer_function="generate_daily_memo",
        consumers=("portfolio_automation.memo_email_sender",),
        schema_version=1,
        append_only=False,
        optional=False,
        observe_only_required=False,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Plain-text daily memo for operator email and review surfaces.",
    ),
    Artifact(
        name="daily_memo_md",
        namespace=OutputNamespace.LATEST,
        relative_path="daily_memo.md",
        format=FORMAT_MARKDOWN,
        writer_module="watchlist_scanner.daily_memo",
        writer_function="generate_daily_memo",
        consumers=("portfolio_automation.memo_email_sender",),
        schema_version=1,
        append_only=False,
        optional=False,
        observe_only_required=False,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Markdown daily memo companion to daily_memo.txt.",
    ),
    Artifact(
        name="agent_bundle",
        namespace=OutputNamespace.LATEST,
        relative_path="agent_bundle.json",
        format=FORMAT_JSON,
        writer_module="agent.bundle_builder",
        writer_function="write_bundle_json",
        consumers=("gui_operator_data",),
        schema_version=1,
        append_only=False,
        # Optional in production: bundle is produced by run_daily_pipeline.py
        # and downstream agent flows.  The production cron (run_daily.sh)
        # invokes main.py only, so this artifact is legitimately absent.
        optional=True,
        observe_only_required=False,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Consolidated AI-oriented bundle of latest artifacts. "
                    "Produced by run_daily_pipeline.py / agent flows "
                    "(not by main.py).",
    ),
    Artifact(
        name="news_evidence_layer",
        namespace=OutputNamespace.LATEST,
        relative_path="news_evidence_layer.json",
        format=FORMAT_JSON,
        writer_module="portfolio_automation.news_evidence_layer",
        writer_function=None,
        consumers=("watchlist_scanner.daily_memo", "gui_operator_data"),
        schema_version=1,
        append_only=False,
        optional=True,
        observe_only_required=True,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Capped, context-only news evidence adjacent to the decision engine.",
    ),

    # ------------------------------------------------------------------
    # PORTFOLIO — portfolio snapshot
    # ------------------------------------------------------------------

    Artifact(
        name="portfolio_snapshot",
        namespace=OutputNamespace.PORTFOLIO,
        relative_path="portfolio_snapshot.json",
        format=FORMAT_JSON,
        writer_module="watchlist_scanner.output_writers",
        writer_function=None,
        consumers=("watchlist_scanner.system_summary", "watchlist_scanner.daily_memo", "gui_operator_data"),
        schema_version=1,
        append_only=False,
        optional=False,
        observe_only_required=False,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Portfolio holdings and allocation snapshot for the run.",
    ),

    # ------------------------------------------------------------------
    # POLICY — append-only audit logs and aggregated summaries
    # ------------------------------------------------------------------

    Artifact(
        name="decision_outcomes",
        namespace=OutputNamespace.POLICY,
        relative_path="decision_outcomes.jsonl",
        format=FORMAT_JSONL,
        writer_module="portfolio_automation.decision_outcome_tracker",
        writer_function="snapshot_decisions",
        consumers=("portfolio_automation.decision_outcome_tracker",),
        schema_version=1,
        append_only=True,
        optional=False,
        observe_only_required=False,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Append-only per-decision snapshot. Idempotent by run_id.",
    ),
    Artifact(
        name="decision_outcome_summary",
        namespace=OutputNamespace.POLICY,
        relative_path="decision_outcome_summary.json",
        format=FORMAT_JSON,
        writer_module="portfolio_automation.decision_outcome_tracker",
        writer_function="make_summary_json",
        consumers=("gui_operator_data",),
        schema_version=1,
        append_only=False,
        optional=False,
        observe_only_required=False,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Aggregated outcome summary: hit rate, returns, top/bottom decisions.",
    ),
    Artifact(
        name="ai_usage_events",
        namespace=OutputNamespace.POLICY,
        relative_path="ai_usage_events.jsonl",
        format=FORMAT_JSONL,
        writer_module="portfolio_automation.ai_budget",
        writer_function="record_ai_usage_event",
        consumers=("portfolio_automation.ai_budget",),
        schema_version=1,
        append_only=True,
        optional=False,
        observe_only_required=False,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Append-only LLM usage event log; aggregated into ai_budget_summary.",
    ),
    Artifact(
        name="memo_delivery_log",
        namespace=OutputNamespace.POLICY,
        relative_path="memo_delivery_log.jsonl",
        format=FORMAT_JSONL,
        writer_module="portfolio_automation.memo_email_sender",
        writer_function=None,
        consumers=(),
        schema_version=1,
        append_only=True,
        optional=True,
        observe_only_required=False,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Append-only audit log of every memo email delivery attempt.",
    ),
    Artifact(
        name="policy_recommendation",
        namespace=OutputNamespace.POLICY,
        relative_path="policy_recommendation.json",
        format=FORMAT_JSON,
        writer_module="policy_evaluator.evaluator",
        writer_function=None,
        consumers=("watchlist_scanner.system_summary", "agent.bundle_builder", "gui_operator_data"),
        schema_version=1,
        append_only=False,
        # Optional in production: policy evaluation runs via
        # run_daily_pipeline.py (stage policy_eval), not via main.py.  The
        # production cron currently invokes main.py only.
        optional=True,
        observe_only_required=False,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Policy profile selection score and recommendation. "
                    "Produced by policy_evaluator via run_daily_pipeline.py "
                    "(not by main.py).",
    ),
    Artifact(
        name="profit_attribution",
        namespace=OutputNamespace.POLICY,
        relative_path="profit_attribution.json",
        format=FORMAT_JSON,
        writer_module="profit_attribution.report_writer",
        writer_function=None,
        consumers=("gui_operator_data", "watchlist_scanner.daily_memo"),
        schema_version=1,
        append_only=False,
        optional=True,
        observe_only_required=False,
        documented_in="docs/OUTPUT_ARTIFACT_CONTRACTS.md",
        description="Profit attribution report aggregating realized outcomes.",
    ),

    # ------------------------------------------------------------------
    # SANDBOX — research lane status (the reference shape for pipeline_run_status)
    # ------------------------------------------------------------------

    Artifact(
        name="sandbox_run_status",
        namespace=OutputNamespace.SANDBOX,
        relative_path="discovery/sandbox_run_status.json",
        format=FORMAT_JSON,
        writer_module="tools.daily_sandbox_run",
        writer_function="run_daily_sandbox",
        consumers=("gui_operator_data",),
        schema_version=1,
        append_only=False,
        optional=False,
        observe_only_required=True,
        documented_in="docs/DAILY_SANDBOX_RUN.md",
        description="Research-lane orchestrator status: per-step results plus "
                    "discovery candidate counts.",
    ),
    Artifact(
        name="sandbox_run_status_md",
        namespace=OutputNamespace.SANDBOX,
        relative_path="discovery/sandbox_run_status.md",
        format=FORMAT_MARKDOWN,
        writer_module="tools.daily_sandbox_run",
        writer_function="run_daily_sandbox",
        consumers=(),
        schema_version=1,
        append_only=False,
        optional=False,
        observe_only_required=False,
        documented_in="docs/DAILY_SANDBOX_RUN.md",
        description="Operator-readable companion to sandbox_run_status.json.",
    ),
)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

class ArtifactNotRegistered(KeyError):
    """Raised when :func:`get_artifact` is called with an unknown name."""


def all_artifacts() -> tuple[Artifact, ...]:
    """Return the full registry as a tuple (defensive copy semantics via tuple)."""
    return REGISTRY


def find_artifact(name: str) -> Artifact | None:
    """Return the :class:`Artifact` with the given *name*, or ``None``."""
    for art in REGISTRY:
        if art.name == name:
            return art
    return None


def get_artifact(name: str) -> Artifact:
    """
    Return the :class:`Artifact` with the given *name*.

    Raises :class:`ArtifactNotRegistered` if the name is not in the registry.
    """
    art = find_artifact(name)
    if art is None:
        raise ArtifactNotRegistered(f"No artifact registered with name {name!r}")
    return art


def artifacts_for_namespace(namespace: OutputNamespace) -> tuple[Artifact, ...]:
    """Return all entries that live in *namespace*."""
    return tuple(a for a in REGISTRY if a.namespace == namespace)


def artifacts_by_writer(writer_module: str) -> tuple[Artifact, ...]:
    """Return all entries written by *writer_module*."""
    return tuple(a for a in REGISTRY if a.writer_module == writer_module)


def artifact_path(
    name: str,
    *,
    base_dir: Path | str = "outputs",
    user_id: str = "owner",
) -> Path:
    """
    Resolve the canonical filesystem path for the registered artifact *name*.

    Thin wrapper over :func:`portfolio_automation.data_governance.get_output_path`
    that uses the artifact's namespace and relative_path. Does not create
    directories or check existence.
    """
    art = get_artifact(name)
    return get_output_path(
        art.namespace,
        art.relative_path,
        user_id=user_id,
        base_dir=base_dir,
    )


# ---------------------------------------------------------------------------
# Self-consistency
# ---------------------------------------------------------------------------

def check_registry_consistency(entries: Iterable[Artifact] | None = None) -> list[str]:
    """
    Validate the registry's internal consistency.  Returns a list of human-readable
    error strings; empty list means everything checks out.

    Checks:

    - names are non-empty, snake_case, and unique
    - relative_path is non-empty and does not start with ``"/"``
    - (namespace, relative_path) is unique
    - format is in :data:`ALLOWED_FORMATS`
    - extension of relative_path matches format
    - format == "jsonl" implies append_only == True
    - writer_module is non-empty
    """
    items = tuple(entries) if entries is not None else REGISTRY
    errors: list[str] = []

    seen_names: set[str] = set()
    seen_paths: set[tuple[OutputNamespace, str]] = set()

    for art in items:
        prefix = f"artifact {art.name!r}"
        if not art.name:
            errors.append("artifact has empty name")
            continue
        if not art.name.replace("_", "").isalnum():
            errors.append(f"{prefix}: name must be snake_case alphanumeric, got {art.name!r}")
        if art.name in seen_names:
            errors.append(f"{prefix}: duplicate name")
        seen_names.add(art.name)

        if not art.relative_path:
            errors.append(f"{prefix}: empty relative_path")
        elif art.relative_path.startswith("/"):
            errors.append(f"{prefix}: relative_path must not start with '/' (got {art.relative_path!r})")

        key = (art.namespace, art.relative_path)
        if key in seen_paths:
            errors.append(
                f"{prefix}: duplicate (namespace, relative_path) "
                f"({art.namespace.value}, {art.relative_path})"
            )
        seen_paths.add(key)

        if art.format not in ALLOWED_FORMATS:
            errors.append(
                f"{prefix}: format must be one of {sorted(ALLOWED_FORMATS)}, got {art.format!r}"
            )
        else:
            expected = _EXPECTED_SUFFIX[art.format]
            if not art.relative_path.endswith(expected):
                errors.append(
                    f"{prefix}: relative_path {art.relative_path!r} must end "
                    f"with {expected!r} for format={art.format!r}"
                )

        if art.format == FORMAT_JSONL and not art.append_only:
            errors.append(f"{prefix}: format=jsonl requires append_only=True")

        if not art.writer_module:
            errors.append(f"{prefix}: writer_module must be non-empty")

    return errors
