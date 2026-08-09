"""The secret boundary: what may cross into an Agent Lab snapshot, and nothing else.

This module is the single chokepoint every candidate file must pass through.
It is deliberately ALLOW-explicit:

    ALLOW  an artifact named in ARTIFACT_ALLOWLIST, resolving inside a
           permitted output root, whose every path component clears the
           forbidden-name rules
    DENY   everything else

There is no "copy the tree and filter out secrets" path anywhere in this
package. That inversion matters: a deny-list silently leaks every sensitive
file nobody thought to name, and this export is designed to eventually cross a
trust boundary onto a different machine.

Nothing here reads file *contents* to make a decision — the boundary is
structural (name + resolved location), so the exporter never needs to inspect,
log, or hold a secret value in order to refuse it.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath


class SecretBoundaryViolation(Exception):
    """A candidate path is refused entry to the snapshot.

    Raised (never swallowed into a warning) so a boundary breach can only ever
    abort a build — it can never degrade into a snapshot that ships the file.
    """


class ExclusionReason(str, Enum):
    """Why a class of production data is deliberately kept out of the export."""

    SECRET = "SECRET"
    CREDENTIAL = "CREDENTIAL"
    PII = "PII"
    MUTABLE_INTERNAL_STATE = "MUTABLE_INTERNAL_STATE"
    NOT_AGENT_RELEVANT = "NOT_AGENT_RELEVANT"


# ---------------------------------------------------------------------------
# Structural boundary rules
# ---------------------------------------------------------------------------

# A source path must live under one of these repo-relative roots. These are the
# artifact namespaces from data_governance.OutputNamespace — all of them hold
# generated, operator-facing artifacts. Note what is NOT here: the repo root
# (.env, config.json), data/ (sqlite state), logs/, .git/, and the home dir.
PERMITTED_SOURCE_ROOTS: tuple[str, ...] = (
    "outputs/latest",
    "outputs/policy",
    "outputs/portfolio",
    "outputs/performance",
    "outputs/sandbox",
    "outputs/simulation",
    "outputs/weekly_etf_bundles",
)

# Exact basenames that may never appear, case-insensitively.
FORBIDDEN_EXACT_NAMES: frozenset[str] = frozenset({
    ".env", ".envrc", ".netrc", "_netrc", ".git-credentials", ".htpasswd",
    "auth.json", "authorization.json", "credentials.json", "credential.json",
    "secrets.json", "secret.json", "token.json", "tokens.json",
    "service-account.json", "serviceaccount.json", "client_secret.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "known_hosts",
    "cookies.txt", "cookies.json", "config.json", "settings.local.json",
    ".pypirc", ".npmrc", ".dockercfg", "kubeconfig",
})

# Glob patterns matched against each path component, case-insensitively.
FORBIDDEN_NAME_PATTERNS: tuple[str, ...] = (
    ".env*",            # .env, .env.bak-*, .env.template, .env.example
    "*.pem", "*.key", "*.pfx", "*.p12", "*.jks", "*.keystore", "*.asc", "*.gpg",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "*secret*", "*credential*", "*password*", "*passwd*",
    "*apikey*", "*api_key*", "*_token*", "*token_*", "*oauth*",
    "*.sqlite", "*.sqlite3", "*.db", "*.db-wal", "*.db-shm",
    "*.pyc", "*.so", "*.pid",
)

# Directory components that may never appear anywhere in a source path.
FORBIDDEN_PATH_COMPONENTS: frozenset[str] = frozenset({
    ".git", ".ssh", ".gnupg", ".aws", ".config", ".venv", "venv",
    "node_modules", "__pycache__", "logs", "data", "backups", "site-packages",
})


def forbidden_reason(component: str) -> str | None:
    """Return the rule a single path *component* violates, else ``None``.

    Checked case-insensitively. Any component beginning with ``.`` is refused
    outright — that single rule covers ``.env``, ``.git``, ``.ssh``, and every
    dotfile nobody has thought of yet, which is precisely the class of mistake
    a deny-list makes.
    """
    if not component or component in {".", ".."}:
        return "path_traversal_or_empty_component"
    low = component.lower()
    if low.startswith("."):
        return "dotfile_or_dotdir"
    if low in FORBIDDEN_PATH_COMPONENTS:
        return f"forbidden_component:{low}"
    if low in FORBIDDEN_EXACT_NAMES:
        return f"forbidden_name:{low}"
    for pattern in FORBIDDEN_NAME_PATTERNS:
        if fnmatch.fnmatch(low, pattern):
            return f"forbidden_pattern:{pattern}"
    return None


def assert_name_allowed(relative_path: str) -> None:
    """Raise :exc:`SecretBoundaryViolation` if any component of *relative_path* is forbidden."""
    for component in PurePosixPath(relative_path).parts:
        reason = forbidden_reason(component)
        if reason is not None:
            raise SecretBoundaryViolation(
                f"{relative_path!r} refused: component {component!r} violates {reason}"
            )


def _is_within(child: Path, parent: Path) -> bool:
    try:
        return child == parent or child.is_relative_to(parent)
    except (ValueError, OSError):
        return False


def resolve_source_path(root: Path | str, relative_path: str) -> Path:
    """Resolve *relative_path* under *root* into a verified, real source file.

    This is the only function permitted to turn an allowlist entry into a path
    the builder will read. It enforces, in order:

    1. the path is relative, POSIX-style, with no traversal or dotfile parts;
    2. no component matches a forbidden name/pattern (pre-resolution);
    3. the path is declared under one of :data:`PERMITTED_SOURCE_ROOTS`;
    4. after **full symlink resolution** the real file still sits inside the
       resolved permitted root — a symlink pointing at ``/opt/stockbot/.env``
       or ``~/.ssh/id_ed25519`` escapes containment and is refused here;
    5. no component of the *resolved* path is forbidden either;
    6. the target is an existing regular file (not a dir, device, or socket).

    Raises :exc:`SecretBoundaryViolation` on any failure. Raises
    :exc:`FileNotFoundError` only for the benign "allowlisted artifact simply
    was not produced" case, which callers treat as missing-not-hostile.
    """
    rel = str(relative_path).replace("\\", "/")

    if not rel or rel.startswith("/") or PurePosixPath(rel).is_absolute():
        raise SecretBoundaryViolation(f"{relative_path!r} refused: absolute paths are never allowed")
    if ":" in rel.split("/")[0]:  # C:\ style drive prefix
        raise SecretBoundaryViolation(f"{relative_path!r} refused: drive-qualified path")

    assert_name_allowed(rel)

    if not any(rel == r or rel.startswith(r + "/") for r in PERMITTED_SOURCE_ROOTS):
        raise SecretBoundaryViolation(
            f"{relative_path!r} refused: not under a permitted source root "
            f"({', '.join(PERMITTED_SOURCE_ROOTS)})"
        )

    root_resolved = Path(root).resolve()
    declared = root_resolved / rel

    # Containment must be judged on the REAL path, after following every
    # symlink. strict=False so a missing file resolves cleanly and is reported
    # as absent below rather than as a boundary breach.
    real = declared.resolve(strict=False)

    permitted_roots_resolved = [
        (root_resolved / r).resolve(strict=False) for r in PERMITTED_SOURCE_ROOTS
    ]
    if not any(_is_within(real, p) for p in permitted_roots_resolved):
        raise SecretBoundaryViolation(
            f"{relative_path!r} refused: resolves to {real} which escapes every "
            f"permitted source root (symlink escape or traversal)"
        )

    # Re-check names on the resolved path: a symlink may keep an innocent name
    # while pointing at a differently-named file inside a permitted root.
    try:
        tail = real.relative_to(root_resolved).as_posix()
    except ValueError:
        raise SecretBoundaryViolation(
            f"{relative_path!r} refused: resolved path {real} is outside the repo root"
        ) from None
    assert_name_allowed(tail)

    if not real.exists():
        raise FileNotFoundError(f"allowlisted artifact not produced: {rel}")
    if real.is_symlink() or not real.is_file():
        raise SecretBoundaryViolation(
            f"{relative_path!r} refused: not a regular file (dir, device, or dangling link)"
        )
    return real


# ---------------------------------------------------------------------------
# The allowlist itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AllowlistEntry:
    """One artifact that may cross the boundary.

    ``logical_name`` is the stable key the Agent Lab addresses the artifact by;
    it stays constant even if the production path moves. ``producer`` is stated
    only for artifacts absent from ``artifact_registry.yaml`` — for registry-
    governed ones provenance is joined from the registry so there is exactly one
    source of truth for who produces what (verified by test).
    """

    logical_name: str
    source_path: str
    category: str
    required: bool = False
    producer: str | None = None
    note: str = ""


@dataclass(frozen=True)
class ExclusionEntry:
    """A class of production data deliberately withheld, recorded for the manifest.

    Recording an exclusion never requires reading the excluded data — these are
    declarations about paths and classes, so the manifest can say "credentials
    exist and were withheld" without the credential ever being touched.
    """

    logical_name: str
    source_pattern: str
    reason: ExclusionReason
    detail: str


#: Category → human-readable purpose, used in the docs and the manifest.
CATEGORIES: dict[str, str] = {
    "core_decision": "The authoritative decision output and its narrative form",
    "portfolio_risk": "Portfolio state and the risk/capital advisories around it",
    "watchlist_discovery": "Ranked watchlist, discovery, and scanner diagnostics",
    "governance": "Artifact/simulation governance, approval and audit summaries",
    "health": "Pipeline, artifact, and semantic health probes",
    "outcome_learning": "Matured outcomes, calibration, and performance scorecards",
    "context": "Crowd, news, regime, and AI-cost context for the run",
}


ARTIFACT_ALLOWLIST: tuple[AllowlistEntry, ...] = (
    # ── Core decisions ────────────────────────────────────────────────────
    AllowlistEntry("decision_plan", "outputs/latest/decision_plan.json",
                   "core_decision", required=True,
                   note="Decision source of truth (CLAUDE.md hard boundary)."),
    AllowlistEntry("decision_plan_md", "outputs/latest/decision_plan.md",
                   "core_decision", required=True,
                   note="Narrative rendering of the same plan."),
    AllowlistEntry("system_decision_summary", "outputs/latest/system_decision_summary.json",
                   "core_decision", required=True),
    AllowlistEntry("daily_memo_md", "outputs/latest/daily_memo.md",
                   "core_decision", required=True,
                   note="Compact operator brief; the memo-vs-plan coherence subject."),
    AllowlistEntry("decision_explanations", "outputs/latest/decision_explanations.json",
                   "core_decision"),
    AllowlistEntry("decision_triage", "outputs/latest/decision_triage.json",
                   "core_decision"),
    AllowlistEntry("memo_datasets", "outputs/latest/memo_datasets.json",
                   "core_decision"),
    AllowlistEntry("decision_authority", "outputs/latest/decision_authority.json",
                   "core_decision", producer="decision_authority",
                   note="Instructed-vs-funded conflict view; not registry-governed."),

    # ── Portfolio state, capital actions, risk ────────────────────────────
    AllowlistEntry("portfolio_snapshot", "outputs/portfolio/portfolio_snapshot.json",
                   "portfolio_risk", required=True,
                   note="Holdings/valuation state the plan was computed against."),
    AllowlistEntry("risk_delta", "outputs/latest/risk_delta.json",
                   "portfolio_risk", required=True),
    AllowlistEntry("cash_deployment_plan", "outputs/latest/cash_deployment_plan.json",
                   "portfolio_risk"),
    AllowlistEntry("daily_capital_plan", "outputs/latest/daily_capital_plan.json",
                   "portfolio_risk", producer="capital_plan_view",
                   note="Funded/deferred capital actions; not registry-governed."),
    AllowlistEntry("scenario_risk", "outputs/latest/scenario_risk.json", "portfolio_risk"),
    AllowlistEntry("correlation_risk_advisor", "outputs/latest/correlation_risk_advisor.json",
                   "portfolio_risk"),
    AllowlistEntry("exit_advisor", "outputs/latest/exit_advisor.json", "portfolio_risk"),
    AllowlistEntry("earnings_gate", "outputs/latest/earnings_gate.json", "portfolio_risk"),
    AllowlistEntry("kelly_sizing_advisor", "outputs/latest/kelly_sizing_advisor.json",
                   "portfolio_risk"),
    AllowlistEntry("vol_regime_advisor", "outputs/latest/vol_regime_advisor.json",
                   "portfolio_risk"),

    # ── Watchlist / discovery ─────────────────────────────────────────────
    AllowlistEntry("watchlist_signals", "outputs/latest/watchlist_signals.json",
                   "watchlist_discovery"),
    AllowlistEntry("watch_candidates", "outputs/latest/watch_candidates.json",
                   "watchlist_discovery"),
    AllowlistEntry("market_opportunities", "outputs/latest/market_opportunities.json",
                   "watchlist_discovery"),
    AllowlistEntry("top100_daily", "outputs/latest/top100_daily.json", "watchlist_discovery"),
    AllowlistEntry("theme_signals", "outputs/latest/theme_signals.json", "watchlist_discovery"),
    AllowlistEntry("discovery_pulse_status", "outputs/latest/discovery_pulse_status.json",
                   "watchlist_discovery", required=True),
    AllowlistEntry("scraped_intel_run_summary", "outputs/latest/scraped_intel_run_summary.json",
                   "watchlist_discovery", note="Scanner run diagnostics."),
    AllowlistEntry("scanner_recovery_canary", "outputs/policy/scanner_recovery_canary.json",
                   "watchlist_discovery", note="Scanner-quality acceptance canary."),

    # ── Governance ────────────────────────────────────────────────────────
    AllowlistEntry("run_manifest", "outputs/policy/run_manifest.json",
                   "governance", required=True,
                   note="Run identity + provenance; the snapshot's anchor."),
    AllowlistEntry("artifact_registry_status", "outputs/latest/artifact_registry_status.json",
                   "governance", required=True,
                   note="Artifact-governance verdict for the whole corpus."),
    AllowlistEntry("operator_action_queue", "outputs/latest/operator_action_queue.json",
                   "governance"),
    AllowlistEntry("strategy_review_queue", "outputs/latest/strategy_review_queue.json",
                   "governance"),
    AllowlistEntry("daily_input_snapshot", "outputs/sandbox/daily_input_snapshot.json",
                   "governance", note="Input freshness/validity gate for the run."),
    AllowlistEntry("auto_approval_audit", "outputs/policy/auto_approval_audit.json",
                   "governance", producer="sim_governance.auto_approval",
                   note="DERIVED summary of the append-only auto-approval ledger. "
                        "The .jsonl ledger itself is excluded as mutable state."),
    AllowlistEntry("auto_apply_audit", "outputs/policy/auto_apply_audit.json",
                   "governance", producer="backtesting.auto_apply",
                   note="DERIVED summary of gated registry weight applies."),
    AllowlistEntry("active_strategy_selection", "outputs/policy/active_strategy_selection.json",
                   "governance"),

    # ── Health ────────────────────────────────────────────────────────────
    AllowlistEntry("daily_run_status", "outputs/latest/daily_run_status.json",
                   "health", required=True,
                   note="Per-stage run outcome; drives snapshot health."),
    AllowlistEntry("pipeline_wiring_status", "outputs/latest/pipeline_wiring_status.json",
                   "health"),
    AllowlistEntry("semantic_liveness_status", "outputs/latest/semantic_liveness_status.json",
                   "health"),
    AllowlistEntry("quant_watch_status", "outputs/latest/quant_watch_status.json", "health"),
    AllowlistEntry("data_quality_report", "outputs/latest/data_quality_report.json", "health"),
    AllowlistEntry("regime_coverage_status", "outputs/latest/regime_coverage_status.json",
                   "health"),
    AllowlistEntry("memo_coherence", "outputs/latest/memo_coherence.json",
                   "health", producer="memo_coherence",
                   note="Memo-vs-plan coherence; not registry-governed."),
    AllowlistEntry("pipeline_run_status", "outputs/latest/pipeline_run_status.json", "health"),

    # ── Outcome learning ──────────────────────────────────────────────────
    AllowlistEntry("decision_outcome_summary", "outputs/policy/decision_outcome_summary.json",
                   "outcome_learning", producer="decision_outcome_tracker",
                   note="DERIVED summary of matured forward outcomes. The "
                        "decision_outcomes.jsonl ledger is excluded."),
    AllowlistEntry("recommendation_evaluation", "outputs/policy/recommendation_evaluation.json",
                   "outcome_learning", producer="policy_evaluator"),
    AllowlistEntry("confidence_calibration", "outputs/latest/confidence_calibration.json",
                   "outcome_learning"),
    AllowlistEntry("alpha_attribution_report", "outputs/latest/alpha_attribution_report.json",
                   "outcome_learning"),
    AllowlistEntry("quant_feedback", "outputs/latest/quant_feedback.json", "outcome_learning"),
    AllowlistEntry("retune_impact", "outputs/latest/retune_impact.json", "outcome_learning"),
    AllowlistEntry("pattern_efficacy_monthly", "outputs/latest/pattern_efficacy_monthly.json",
                   "outcome_learning"),
    AllowlistEntry("strategy_catalog", "outputs/sandbox/strategy_catalog.json",
                   "outcome_learning", note="Strategy scorecards + rationale coverage."),
    AllowlistEntry("system_improvement_scorecard", "outputs/latest/system_improvement_scorecard.json",
                   "outcome_learning"),

    # ── Context ───────────────────────────────────────────────────────────
    AllowlistEntry("crowd_intelligence", "outputs/latest/crowd_intelligence.json", "context"),
    AllowlistEntry("unified_crowd_intelligence", "outputs/latest/unified_crowd_intelligence.json",
                   "context"),
    AllowlistEntry("news_intelligence", "outputs/latest/news_intelligence.json", "context"),
    AllowlistEntry("market_narrative_daily", "outputs/latest/market_narrative_daily.json",
                   "context"),
    AllowlistEntry("institutional_intelligence", "outputs/latest/institutional_intelligence.json",
                   "context"),
    AllowlistEntry("ai_budget_summary", "outputs/latest/ai_budget_summary.json",
                   "context", note="AI cost/usage envelope for the run."),
    AllowlistEntry("fmp_budget_status", "outputs/latest/fmp_budget_status.json", "context"),
)


DECLARED_EXCLUSIONS: tuple[ExclusionEntry, ...] = (
    ExclusionEntry("env_files", ".env, .env.*, .env.bak-*", ExclusionReason.SECRET,
                   "Runtime secrets: FMP/OpenAI/Anthropic keys, SMTP and broker credentials."),
    ExclusionEntry("production_config", "config.json", ExclusionReason.SECRET,
                   "Raw production config may embed endpoint credentials. The run's "
                   "config_hash is carried in run_manifest instead, which proves which "
                   "config produced the run without disclosing it."),
    ExclusionEntry("ssh_and_signing_keys", "id_*, *.pem, *.key, stockbot.txt",
                   ExclusionReason.CREDENTIAL,
                   "Private keys and deploy identities are never artifact data."),
    ExclusionEntry("broker_account_artifacts",
                   "outputs/latest/schwab_positions.json, schwab_portfolio_snapshot.json, "
                   "schwab_tax_lots.json",
                   ExclusionReason.PII,
                   "Raw broker account state may carry account identifiers and tax lots. "
                   "The sanitized portfolio_snapshot is exported instead."),
    ExclusionEntry("email_delivery_artifacts",
                   "outputs/latest/email_view.csv, email_prompt.txt, "
                   "outputs/policy/*_email_log.jsonl, memo_delivery_log.jsonl",
                   ExclusionReason.PII,
                   "Recipient addresses and delivered message bodies."),
    ExclusionEntry("append_only_ledgers", "outputs/policy/*.jsonl",
                   ExclusionReason.MUTABLE_INTERNAL_STATE,
                   "Writable event ledgers grow between reads and are the substrate the "
                   "governance circuit breakers derive from. Their DERIVED summaries "
                   "(*_audit.json, decision_outcome_summary.json) are exported instead."),
    ExclusionEntry("sqlite_state", "data/*.db (portfolio.db, sim_governance_watchlist.db)",
                   ExclusionReason.MUTABLE_INTERNAL_STATE,
                   "Live databases are mutable and unsafe to copy mid-write."),
    ExclusionEntry("run_logs", "logs/*",
                   ExclusionReason.NOT_AGENT_RELEVANT,
                   "Tracebacks can incidentally embed secrets and URLs; run outcome is "
                   "already structured in daily_run_status."),
    ExclusionEntry("git_internals", ".git/*",
                   ExclusionReason.NOT_AGENT_RELEVANT,
                   "The Agent Lab refreshes its own shadow checkout to production_git_sha; "
                   "it never needs production's object store or remote credentials."),
    ExclusionEntry("prior_run_history", "outputs/history/*",
                   ExclusionReason.NOT_AGENT_RELEVANT,
                   "A snapshot describes exactly one run; prior runs have their own snapshots."),
)


def allowlist_by_name() -> dict[str, AllowlistEntry]:
    """Allowlist keyed by ``logical_name`` (stable, deterministic ordering)."""
    return {e.logical_name: e for e in ARTIFACT_ALLOWLIST}


def required_entries() -> tuple[AllowlistEntry, ...]:
    """The subset whose absence must fail the build closed."""
    return tuple(e for e in ARTIFACT_ALLOWLIST if e.required)


def sorted_allowlist() -> tuple[AllowlistEntry, ...]:
    """Allowlist in deterministic order — the order artifacts appear in a manifest."""
    return tuple(sorted(ARTIFACT_ALLOWLIST, key=lambda e: e.logical_name))
