"""What must be backed up to rebuild Northstar after losing the VPS — and why.

THE DESIGN RULE.

Include only known recovery state. The old producer used `ls data/*.json
data/*.jsonl`, which silently changes what gets backed up whenever a new file
appears: it can omit nested state added later, and it can sweep in a new
secret-bearing file without anyone reviewing the change. A wildcard is a
backup policy that nobody approved.

So every surface is named, classified, and given a reason. Adding a database or
a state file is a reviewable diff with a test that fails until the allowlist is
updated deliberately.

WHY SOME STATE IS DELIBERATELY NOT BACKED UP.

``.agent/`` and ``config/`` carry the protected roadmap, authority and runtime
configuration — the most important state in the system — and are NOT in the
backup set. They are tracked in Git and recoverable from the authoritative
remote, so duplicating them into a nightly tarball would create a second,
staler copy of record.

That reasoning has one failure mode, and it is not hypothetical: this very
reconciliation began because the VPS carried 12 commits that had never been
pushed. Git durability is a property of *pushed* commits, not of tracked files.
So the snapshot records Git provenance (HEAD, whether HEAD is contained in the
remote, and whether tracked recovery state is dirty) and the verifier surfaces
it. SOURCE_CONTROL_DURABLE is then a checked claim rather than an assumption.

``experimental_noncanonical``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SCHEMA_VERSION = "engineering.backup_recovery_set.v2"

Classification = Literal[
    "BACKUP_REQUIRED",
    "REGENERABLE",
    "SOURCE_CONTROL_DURABLE",
    "SECRET_EXCLUDED",
    "EPHEMERAL_EXCLUDED",
]


@dataclass(frozen=True)
class Surface:
    """One state surface, its classification, and the reason for it."""

    path: str
    classification: Classification
    reason: str
    #: Only BACKUP_REQUIRED surfaces are read by the producer. A required
    #: surface that is absent fails the snapshot; an optional one is recorded
    #: as a gap.
    required: bool = False


# ── databases ─────────────────────────────────────────────────────────────
# Derived by searching the code for every `data/*.db` path, NOT by listing the
# filesystem: the laptop has two of these and production has more, so a
# filesystem-driven list would silently under-cover production.
DATABASES: tuple[Surface, ...] = (
    Surface("data/portfolio.db", "BACKUP_REQUIRED",
            "Primary runtime store: positions, decisions, and "
            "watchlist_signal_feedback — the recorded signal history VS-002 "
            "depends on. 94 code references. Exists nowhere else.", True),
    Surface("data/crowd_intelligence.db", "BACKUP_REQUIRED",
            "crowd_raw_events / crowd_signal_daily / fmp_endpoint_capabilities. "
            "A point-in-time event series; re-fetching later returns different "
            "data, so it is not regenerable."),
    Surface("data/fmp_budget.db", "BACKUP_REQUIRED",
            "API usage ledger. The quota itself resets, but the ledger is the "
            "audit record of vendor spend and is not reconstructable."),
    Surface("data/sim_governance_watchlist.db", "BACKUP_REQUIRED",
            "Simulation-governance approval state. Governance decisions are "
            "not derivable from anything else."),
    Surface("data/rd_control.db", "BACKUP_REQUIRED",
            "R&D Control Plane. docs/RD_CONTROL_PLANE.md: 'SQLite "
            "(data/rd_control.db) is the single authoritative store.' ADDED "
            "AFTER the August backup set was written, and omitted from it — "
            "this is the concrete gap that motivated re-inventorying."),
    Surface("data/institutional_intelligence.db", "BACKUP_REQUIRED",
            "13F holdings and SEC filing vintages. Technically re-fetchable, "
            "but each row is a point-in-time observation and refetching loses "
            "the vintage. Classified conservatively."),
    Surface("data/stockbot.db", "EPHEMERAL_EXCLUDED",
            "PHANTOM. Referenced only by docs/PROD_EVIDENCE_DIRECT_V0.md and "
            "the stale ops/prod_evidence/stockbot-observe script (whose own "
            "comment says '# adjust'). No production code creates it. Recorded "
            "here so a future reader does not add it on the strength of those "
            "two stale references."),
)

# ── non-database runtime state ────────────────────────────────────────────
STATE_FILES: tuple[Surface, ...] = (
    Surface("data/finance_history.json", "BACKUP_REQUIRED",
            "Daily portfolio value / cash / per-symbol drift history. Runtime "
            "state, untracked, and not reconstructable from the DBs.", True),
)

# ── everything deliberately outside the backup set ────────────────────────
EXCLUDED: tuple[Surface, ...] = (
    Surface(".agent/", "SOURCE_CONTROL_DURABLE",
            "Protected roadmap and controller state (phase_status.yaml, "
            "project_state.yaml, doc_audit_state.yaml). Fully tracked in Git; "
            "the remote is the record of authority. Duplicating it would "
            "create a staler second copy. Git provenance is recorded in the "
            "manifest so this claim is verified, not assumed."),
    Surface("config/", "SOURCE_CONTROL_DURABLE",
            "Runtime policy and authority configuration (agent_policy.yaml, "
            "ew0a_*.json). Tracked in Git."),
    Surface("docs/DEFECT_INTERVENTION_JOURNAL.jsonl", "SOURCE_CONTROL_DURABLE",
            "Append-only defect journal. Tracked in Git."),
    Surface("evals/", "SOURCE_CONTROL_DURABLE",
            "G1 measurement and Vertical Slice evidence, including the frozen "
            "VS-001 preregistration and result. Tracked in Git."),
    Surface("data/fmp_cache/", "REGENERABLE",
            "Vendor response cache. Refetchable and carries no state of record."),
    Surface("data/watchlist_cache/", "REGENERABLE",
            "Watchlist scanner cache. Rebuilt on the next scan and holds "
            "no state of record."),
    Surface("outputs/latest/", "REGENERABLE",
            "Overwritten every pipeline run. Gitignored by design."),
    Surface("outputs/backtest/historical/", "REGENERABLE",
            "5-year daily price archive. Large, and re-fetchable from the "
            "vendor by the existing weekend backfill. Excluded to keep the "
            "nightly artifact small; note that refetching costs quota and "
            "returns the vendor's then-current adjustment vintage."),
    Surface("outputs/vs002_evidence/", "REGENERABLE",
            "Deterministically rebuilt from portfolio.db plus the price "
            "archive by the merged VS-002 builder."),
    Surface("outputs/agent_export/", "REGENERABLE",
            "Immutable export snapshots, rebuildable from their sources."),
    Surface("logs/", "EPHEMERAL_EXCLUDED",
            "Rotating operational logs. Useful for diagnosis, never a "
            "source of recoverable state."),
    Surface("daily_checks/", "REGENERABLE",
            "Operator logbook derived from DB state. Contains live position "
            "data, so it is excluded from the artifact rather than shipped "
            "off-box."),
    Surface(".env", "SECRET_EXCLUDED", "Vendor and service credentials."),
    Surface("data/nonexistent.db", "EPHEMERAL_EXCLUDED",
            "Test fixture path, never a real database."),
)

#: Names that must never appear inside a snapshot. Checked by the producer and
#: again by the verifier: a backup that quietly grew a credential is worse than
#: no backup, because it gets copied off-box.
FORBIDDEN_BASENAMES: frozenset[str] = frozenset({
    ".env", ".env.local", "auth.json", "credentials.json", "secrets.json",
    "secret.json", "service-account.json", "token.json", ".netrc",
    ".git-credentials", ".pgpass", "id_rsa", "id_ed25519", "id_ecdsa",
    "id_dsa", ".stockbot_backup_key",
})
FORBIDDEN_SUFFIXES: tuple[str, ...] = (
    ".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".ppk",
)

ALL_SURFACES: tuple[Surface, ...] = DATABASES + STATE_FILES + EXCLUDED


def backup_databases() -> tuple[Surface, ...]:
    return tuple(s for s in DATABASES if s.classification == "BACKUP_REQUIRED")


def backup_state_files() -> tuple[Surface, ...]:
    return tuple(s for s in STATE_FILES if s.classification == "BACKUP_REQUIRED")


def is_forbidden(name: str) -> bool:
    """True if this basename must never enter a snapshot."""
    lowered = name.lower()
    return (lowered in FORBIDDEN_BASENAMES
            or lowered.endswith(FORBIDDEN_SUFFIXES))


def classification_of(path: str) -> Classification | None:
    for s in ALL_SURFACES:
        if s.path == path:
            return s.classification
    return None
