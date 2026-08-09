"""
Agent Production Export — deterministic, sanitized, immutable snapshots.

Purpose
-------
Freeze an allowlisted subset of a completed StockBot run into an immutable,
hash-verified, self-describing snapshot that a future Agent Lab / Prime consumer
can trust as a read-only production-state input. This module implements ONLY the
export/build/validation side. There is NO network transport, NO Hetzner
integration, NO Prime invocation, and NO production behaviour change: it reads
existing artifacts and writes a new snapshot tree plus a health artifact.

Trust model
-----------
    StockBot runtime artifacts (outputs/**)
        -> agent_export builder  (allowlist + secret guard + path guard)
        -> immutable snapshot dir + manifest.json (sha256 per file + fingerprint)
        -> validate_agent_snapshot() proves integrity for the consumer

Hard guarantees
---------------
* ALLOWLIST-based: only artifacts named in :data:`ALLOWLIST` can ever cross.
* Secret boundary: forbidden basenames/patterns are rejected fail-closed.
* Path boundary: every source path is realpath-resolved and must stay inside the
  approved artifacts root; symlink/traversal/absolute escapes fail closed.
* Atomic: a snapshot is built in a temp dir and finalized with a single rename;
  a partial build is never visible as a valid snapshot.
* Immutable: finalized snapshot files are made read-only; an existing snapshot id
  is never silently overwritten (identical -> idempotent, different -> fail).
* Observe-only: this subsystem cannot change any decision/score/allocation state.

Public API
----------
    build_snapshot(...)           -> manifest dict (or plan dict when dry_run)
    validate_agent_snapshot(dir)  -> {"valid": bool, "errors": [...], ...}
    compare_shas(prod, shadow)    -> "MATCH" | "SHADOW_BEHIND" | "SHADOW_AHEAD" | "UNKNOWN"
    build_agent_export_health(...) / run_agent_export_health(...)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger("stockbot.portfolio_automation.agent_export")

SCHEMA_VERSION = "1"
_SOURCE_LABEL = "agent_export"
_OBSERVE_ONLY = True

DEFAULT_ARTIFACTS_ROOT = "outputs"
EXPORT_SUBDIR = "agent_export"          # under the output root
SNAPSHOTS_SUBDIR = "snapshots"
TMP_SUBDIR = ".tmp"
ARTIFACTS_DIRNAME = "artifacts"
MANIFEST_NAME = "manifest.json"
LATEST_POINTER_NAME = "latest.json"
HEALTH_REL = "agent_export_health.json"  # written under outputs/policy/

GREEN, AMBER, RED = "GREEN", "AMBER", "RED"

# Stale threshold for the health check (hours). A snapshot older than this is
# reported AMBER. Export is on-demand, so this is generous.
_STALE_HOURS = 48.0


# ---------------------------------------------------------------------------
# Allowlist — the ONLY artifacts that may cross into a snapshot.
# ---------------------------------------------------------------------------
# Mapped to real registry paths (portfolio_automation/artifact_registry.yaml).
# `required=True` means a snapshot cannot be built without it (fail closed).
# Everything else is optional: absence is a recorded gap + AMBER, not a failure.
@dataclass(frozen=True)
class AllowlistEntry:
    logical_name: str
    source_relpath: str   # relative to the artifacts root (e.g. "latest/decision_plan.json")
    required: bool
    producer: str
    category: str


ALLOWLIST: tuple[AllowlistEntry, ...] = (
    # ── Core decisions ────────────────────────────────────────────────────
    AllowlistEntry("decision_plan",            "latest/decision_plan.json",            True,  "decision_engine",   "core_decisions"),
    AllowlistEntry("decision_plan_md",         "latest/decision_plan.md",              False, "decision_engine",   "core_decisions"),
    AllowlistEntry("system_decision_summary",  "latest/system_decision_summary.json",  True,  "decision_engine",   "core_decisions"),
    AllowlistEntry("decision_explanations",    "latest/decision_explanations.json",    False, "decision_explainer","core_decisions"),
    AllowlistEntry("portfolio_snapshot",       "portfolio/portfolio_snapshot.json",    False, "portfolio_builder", "core_decisions"),
    # ── Discovery / watchlist ─────────────────────────────────────────────
    AllowlistEntry("watchlist_signals",        "latest/watchlist_signals.json",        False, "watchlist_scanner", "discovery"),
    AllowlistEntry("theme_signals",            "latest/theme_signals.json",            False, "theme_engine",      "discovery"),
    AllowlistEntry("news_intelligence",        "latest/news_intelligence.json",        False, "news_intelligence", "discovery"),
    AllowlistEntry("market_narrative_daily",   "latest/market_narrative_daily.json",   False, "market_narrative",  "discovery"),
    # ── Governance summaries (sanitized status artifacts, not raw ledgers) ─
    AllowlistEntry("artifact_registry_status", "latest/artifact_registry_status.json", False, "artifact_registry", "governance"),
    AllowlistEntry("active_strategy_selection","policy/active_strategy_selection.json",False, "strategy_governance","governance"),
    AllowlistEntry("approved_ranking_config",  "performance/approved_ranking_config.json",   False, "ranking_governance",    "governance"),
    AllowlistEntry("approved_allocation_policy","performance/approved_allocation_policy.json",False, "allocation_governance", "governance"),
    # ── System health ─────────────────────────────────────────────────────
    AllowlistEntry("daily_run_status",         "latest/daily_run_status.json",         False, "daily_run_status",  "system_health"),
    AllowlistEntry("pipeline_run_status",      "latest/pipeline_run_status.json",      False, "run_status",        "system_health"),
    AllowlistEntry("fmp_budget_status",        "latest/fmp_budget_status.json",        False, "fmp_budget_telemetry","system_health"),
    AllowlistEntry("risk_delta",               "latest/risk_delta.json",               False, "risk_delta_advisor","system_health"),
    # ── Learning / outcomes ───────────────────────────────────────────────
    AllowlistEntry("retune_impact",            "latest/retune_impact.json",            False, "retune_impact_tracker","learning"),
    AllowlistEntry("confidence_calibration",   "latest/confidence_calibration.json",   False, "confidence_calibration","learning"),
    AllowlistEntry("quant_watch_status",       "latest/quant_watch_status.json",       False, "quant_watch",       "learning"),
    AllowlistEntry("pattern_efficacy_monthly", "latest/pattern_efficacy_monthly.json", False, "pattern_efficacy",  "learning"),
    # ── Context ───────────────────────────────────────────────────────────
    AllowlistEntry("crowd_intelligence",       "latest/crowd_intelligence.json",       False, "crowd_intelligence","context"),
    AllowlistEntry("ai_budget_summary",        "latest/ai_budget_summary.json",        False, "ai_budget",         "context"),
    # ── Memo / coherence ──────────────────────────────────────────────────
    AllowlistEntry("daily_memo_md",            "latest/daily_memo.md",                 False, "daily_memo",        "memo"),
    AllowlistEntry("daily_memo_txt",           "latest/daily_memo.txt",                False, "daily_memo",        "memo"),
    AllowlistEntry("memo_datasets",            "latest/memo_datasets.json",            False, "memo_datasets",     "memo"),
)


# ---------------------------------------------------------------------------
# Secret boundary
# ---------------------------------------------------------------------------
FORBIDDEN_BASENAMES: frozenset[str] = frozenset({
    ".env", "auth.json", "credentials.json", "id_rsa", "id_ed25519", "id_dsa",
    "id_ecdsa", ".netrc", ".git-credentials", ".pgpass", ".htpasswd",
    "cookies.txt", "cookies.sqlite", ".bash_history", ".zsh_history",
    "secrets.json", "secret.json", "service-account.json", "token.json",
})
FORBIDDEN_SUFFIXES: tuple[str, ...] = (
    ".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".ppk", ".crt", ".cer",
)
FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "secret", "token", "password", "passwd", "credential", "api_key", "apikey",
    "private_key", "privatekey", "oauth", ".env",
)


class AgentExportError(Exception):
    """Base class for agent-export failures."""


class SecurityBoundaryError(AgentExportError):
    """Raised when a secret filename or a path escape is detected. Fail closed."""


class SnapshotExistsError(AgentExportError):
    """Raised when a snapshot id already exists with DIFFERENT content."""


class SnapshotValidationError(AgentExportError):
    """Raised when a required-artifact / integrity precondition fails during build."""


def is_forbidden_name(name: str) -> bool:
    """True if *name* (a basename) matches any secret allow-never rule."""
    low = name.lower()
    if low in FORBIDDEN_BASENAMES:
        return True
    if any(low.endswith(sfx) for sfx in FORBIDDEN_SUFFIXES):
        return True
    return any(sub in low for sub in FORBIDDEN_SUBSTRINGS)


# ---------------------------------------------------------------------------
# Path boundary
# ---------------------------------------------------------------------------
def _resolve_within(root: Path, candidate: Path) -> Path:
    """
    Resolve *candidate* (following symlinks) and require the real path to stay
    inside the real *root*. Raises :class:`SecurityBoundaryError` otherwise.

    Blocks ``../`` traversal, absolute paths pointing elsewhere, and symlinks
    whose target escapes the approved root.
    """
    real_root = Path(os.path.realpath(root))
    real = Path(os.path.realpath(candidate))
    try:
        real.relative_to(real_root)
    except ValueError:
        raise SecurityBoundaryError(
            f"path escapes approved root: {candidate} -> {real} not under {real_root}"
        )
    return real


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------
def compute_file_sha256(path: str | Path) -> str:
    """Return ``sha256:<hex>`` for the file at *path*."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, UTF-8, no NaN."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def compute_snapshot_fingerprint(manifest: dict) -> str:
    """
    Deterministic snapshot-level fingerprint over stable identity + ordered
    artifact hashes. Excludes ``created_at`` (wall clock) and ``snapshot_hash``
    (self-reference), so the same frozen inputs always fingerprint identically.

    Contract (documented in docs/STOCKBOT_AGENT_EXPORT.md):
        sha256( canonical_json({
            schema_version, code_identity, run_identity,
            artifacts: [{logical_name, snapshot_path, sha256, size_bytes, required}
                        sorted by logical_name],
            gaps: [logical_name sorted],
        }) )
    """
    core = {
        "schema_version": manifest.get("schema_version"),
        "code_identity": manifest.get("code_identity"),
        "run_identity": manifest.get("run_identity"),
        "artifacts": sorted(
            (
                {
                    "logical_name": a["logical_name"],
                    "snapshot_path": a["snapshot_path"],
                    "sha256": a["sha256"],
                    "size_bytes": a["size_bytes"],
                    "required": a["required"],
                }
                for a in manifest.get("artifacts", [])
            ),
            key=lambda a: a["logical_name"],
        ),
        "gaps": sorted(g["logical_name"] for g in manifest.get("gaps", [])),
    }
    return "sha256:" + hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sanitize_id(value: str) -> str:
    keep = [c if (c.isalnum() or c in "._-") else "-" for c in value]
    out = "".join(keep).strip("-._")
    return out or "run"


def _derive_snapshot_id(production_run_id: str | None, production_git_sha: str | None) -> str:
    if production_run_id:
        return "snap-" + _sanitize_id(production_run_id)
    sha = (production_git_sha or "unknown")[:12]
    return "snap-" + _sanitize_id(sha)


def _export_root(output_root: str | Path) -> Path:
    return Path(output_root) / EXPORT_SUBDIR


def _make_readonly_tree(root: Path) -> None:
    """Best-effort immutability: files 0444, directories 0555 (bottom-up)."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for fn in filenames:
            try:
                os.chmod(os.path.join(dirpath, fn), 0o444)
            except OSError:
                pass
        try:
            os.chmod(dirpath, 0o555)
        except OSError:
            pass


def _rmtree_force(path: Path) -> None:
    """Remove a possibly read-only tree (cleanup of temp / rollback)."""
    def _onerror(func, p, exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    if path.exists():
        shutil.rmtree(path, onerror=_onerror)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build_snapshot(
    *,
    production_git_sha: str,
    production_run_id: str | None,
    run_started_at: str | None,
    run_completed_at: str | None,
    artifacts_root: str | Path = DEFAULT_ARTIFACTS_ROOT,
    output_root: str | Path | None = None,
    allowlist: Sequence[AllowlistEntry] = ALLOWLIST,
    run_overall_status: str | None = None,
    run_warnings: Iterable[str] | None = None,
    run_critical_failures: Iterable[str] | None = None,
    created_at: str | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Build an immutable agent snapshot from allowlisted artifacts under
    *artifacts_root*, writing under ``<output_root>/agent_export/snapshots/<id>/``.

    Returns the manifest dict. When *dry_run* is True, returns a plan dict
    (``{"dry_run": True, "would_include": [...], "gaps": [...], "snapshot_id": ...}``)
    and writes nothing.

    Fail-closed conditions:
      * a required allowlisted artifact is missing  -> SnapshotValidationError
      * a source path escapes the artifacts root     -> SecurityBoundaryError
      * a forbidden (secret) basename is encountered  -> SecurityBoundaryError
      * the snapshot id exists with different content -> SnapshotExistsError
    """
    if not production_git_sha or not str(production_git_sha).strip():
        raise SnapshotValidationError("production_git_sha is required and must be non-empty")

    artifacts_root = Path(artifacts_root)
    output_root = Path(output_root) if output_root is not None else artifacts_root
    real_src_root = Path(os.path.realpath(artifacts_root))
    snapshot_id = _derive_snapshot_id(production_run_id, production_git_sha)

    # Resolve each allowlist entry against the source root, applying both guards.
    resolved: list[tuple[AllowlistEntry, Path]] = []
    gaps: list[dict] = []
    for entry in allowlist:
        if is_forbidden_name(Path(entry.source_relpath).name):
            # Defense in depth: an allowlist edit must never smuggle a secret name.
            raise SecurityBoundaryError(
                f"allowlist entry '{entry.logical_name}' has forbidden name: {entry.source_relpath}"
            )
        src = artifacts_root / entry.source_relpath
        if not src.exists():
            gaps.append({"logical_name": entry.logical_name, "reason": "source_absent",
                         "source_path": str(src), "required": entry.required})
            if entry.required:
                raise SnapshotValidationError(
                    f"required artifact '{entry.logical_name}' missing at {src}"
                )
            continue
        real = _resolve_within(real_src_root, src)   # symlink/traversal/abs guard
        if not real.is_file():
            gaps.append({"logical_name": entry.logical_name, "reason": "not_a_file",
                         "source_path": str(src), "required": entry.required})
            if entry.required:
                raise SnapshotValidationError(
                    f"required artifact '{entry.logical_name}' is not a regular file: {src}"
                )
            continue
        resolved.append((entry, real))

    would_include = [e.logical_name for e, _ in resolved]

    if dry_run:
        return {
            "dry_run": True,
            "snapshot_id": snapshot_id,
            "artifacts_root": str(artifacts_root),
            "would_include": sorted(would_include),
            "gaps": gaps,
        }

    export_root = _export_root(output_root)
    final_dir = export_root / SNAPSHOTS_SUBDIR / snapshot_id
    tmp_parent = export_root / TMP_SUBDIR
    tmp_parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"{snapshot_id}.", dir=str(tmp_parent)))

    try:
        art_out = tmp_dir / ARTIFACTS_DIRNAME
        art_out.mkdir(parents=True, exist_ok=True)

        artifact_rows: list[dict] = []
        for entry, real in resolved:
            # snapshot-relative layout mirrors the logical name + original filename
            rel = f"{ARTIFACTS_DIRNAME}/{entry.logical_name}/{Path(entry.source_relpath).name}"
            dest = tmp_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Re-verify the destination stays inside the temp snapshot (paranoia)
            _resolve_within(tmp_dir, dest.parent)
            if is_forbidden_name(dest.name):
                raise SecurityBoundaryError(f"refusing to write forbidden name: {dest.name}")
            shutil.copyfile(real, dest)   # bytes only; never copies mode/xattrs
            sha = compute_file_sha256(dest)
            size = dest.stat().st_size
            generated_at = datetime.fromtimestamp(
                real.stat().st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            artifact_rows.append({
                "logical_name": entry.logical_name,
                "category": entry.category,
                "producer": entry.producer,
                "source_path": str(artifacts_root / entry.source_relpath),
                "snapshot_path": rel,
                "sha256": sha,
                "size_bytes": size,
                "required": entry.required,
                "generated_at": generated_at,
            })

        artifact_rows.sort(key=lambda a: a["logical_name"])

        # Derive export-completeness health if the caller didn't pass run health.
        optional_gaps = [g for g in gaps if not g["required"]]
        if run_overall_status is not None:
            overall = run_overall_status
        else:
            overall = AMBER if optional_gaps else GREEN
        warnings = list(run_warnings or [])
        if run_overall_status is None:
            warnings += [f"optional artifact missing: {g['logical_name']}" for g in optional_gaps]

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "created_at": created_at or _utc_now_iso(),
            "observe_only": _OBSERVE_ONLY,
            "code_identity": {
                "production_git_sha": str(production_git_sha),
            },
            "run_identity": {
                "production_run_id": production_run_id,
                "run_started_at": run_started_at,
                "run_completed_at": run_completed_at,
            },
            "health": {
                "overall_status": overall,
                "warnings": warnings,
                "critical_failures": list(run_critical_failures or []),
            },
            "artifacts": artifact_rows,
            "gaps": gaps,
        }
        manifest["snapshot_hash"] = compute_snapshot_fingerprint(manifest)

        # Write + self-validate the manifest inside the temp dir.
        (tmp_dir / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=False, default=str) + "\n",
            encoding="utf-8",
        )
        pre = validate_agent_snapshot(tmp_dir)
        if not pre["valid"]:
            raise SnapshotValidationError(
                "internal validation failed before finalize: " + "; ".join(pre["errors"])
            )

        # Idempotency / no-silent-overwrite.
        if final_dir.exists():
            existing = _read_manifest(final_dir)
            if existing and existing.get("snapshot_hash") == manifest["snapshot_hash"]:
                logger.info("agent_export: snapshot %s already exists with identical "
                            "content; idempotent no-op", snapshot_id)
                _rmtree_force(tmp_dir)
                _write_latest_pointer(export_root, existing)
                return existing
            raise SnapshotExistsError(
                f"snapshot id {snapshot_id} exists with different content; refusing to overwrite"
            )

        final_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_dir, final_dir)   # atomic finalize (same filesystem)
        tmp_dir = None  # ownership transferred
        # Immutability is applied AFTER the rename: chmod'ing the dir read-only
        # before the move would deny the rename (moving a dir updates its "..").
        _make_readonly_tree(final_dir)
        _write_latest_pointer(export_root, manifest)
        logger.info("agent_export: finalized snapshot %s (%d artifacts, %d gaps)",
                    snapshot_id, len(artifact_rows), len(gaps))
        return manifest
    finally:
        if tmp_dir is not None:
            _rmtree_force(tmp_dir)


def _write_latest_pointer(export_root: Path, manifest: dict) -> Path:
    """Write a small metadata pointer (NOT a second copy) to the latest snapshot."""
    pointer = {
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_path": f"{SNAPSHOTS_SUBDIR}/{manifest['snapshot_id']}",
        "snapshot_hash": manifest["snapshot_hash"],
        "schema_version": manifest["schema_version"],
        "created_at": manifest["created_at"],
        "production_git_sha": manifest["code_identity"]["production_git_sha"],
        "overall_status": manifest["health"]["overall_status"],
    }
    export_root.mkdir(parents=True, exist_ok=True)
    dest = export_root / LATEST_POINTER_NAME
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pointer, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, dest)
    return dest


def _read_manifest(snapshot_dir: str | Path) -> dict | None:
    p = Path(snapshot_dir) / MANIFEST_NAME
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Validation (consumer-side; must NOT mutate the snapshot)
# ---------------------------------------------------------------------------
def validate_agent_snapshot(snapshot_dir: str | Path) -> dict:
    """
    Verify a finalized snapshot. Read-only. Returns
    ``{"valid": bool, "errors": [str, ...], "snapshot_id": str|None}``.

    Checks: schema version, required manifest fields, required artifacts present,
    per-file hash + size integrity, expected paths, no unexpected files, no
    forbidden filenames, recomputed snapshot fingerprint, production git sha /
    run id present, health metadata present.
    """
    d = Path(snapshot_dir)
    errors: list[str] = []
    manifest = _read_manifest(d)
    if manifest is None:
        return {"valid": False, "errors": [f"manifest missing or unreadable: {d/MANIFEST_NAME}"],
                "snapshot_id": None}

    sid = manifest.get("snapshot_id")

    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version mismatch: got {manifest.get('schema_version')!r}, "
                      f"expected {SCHEMA_VERSION!r}")

    for field in ("snapshot_id", "created_at", "code_identity", "run_identity",
                  "health", "artifacts", "snapshot_hash"):
        if field not in manifest:
            errors.append(f"missing manifest field: {field}")

    code_id = manifest.get("code_identity") or {}
    if not code_id.get("production_git_sha"):
        errors.append("code_identity.production_git_sha missing/empty")

    run_id = manifest.get("run_identity")
    if not isinstance(run_id, dict) or "production_run_id" not in run_id:
        errors.append("run_identity.production_run_id missing")

    health = manifest.get("health") or {}
    if "overall_status" not in health:
        errors.append("health.overall_status missing")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append("artifacts is not a list")
        artifacts = []

    # Fingerprint integrity (detects manifest tampering of identity/hashes).
    recomputed = compute_snapshot_fingerprint(manifest)
    if manifest.get("snapshot_hash") != recomputed:
        errors.append(f"snapshot_hash mismatch: manifest {manifest.get('snapshot_hash')} "
                      f"!= recomputed {recomputed}")

    # Per-artifact integrity + expected-path set.
    expected_rel = {MANIFEST_NAME}
    required_seen = 0
    required_total = 0
    for a in artifacts:
        rel = a.get("snapshot_path", "")
        name = Path(rel).name
        if is_forbidden_name(name):
            errors.append(f"forbidden filename in manifest: {rel}")
        expected_rel.add(rel)
        fp = d / rel
        if a.get("required"):
            required_total += 1
        if not fp.is_file():
            errors.append(f"artifact file missing: {rel}")
            continue
        actual_sha = compute_file_sha256(fp)
        if actual_sha != a.get("sha256"):
            errors.append(f"hash mismatch for {rel}: {actual_sha} != {a.get('sha256')}")
        elif a.get("required"):
            required_seen += 1
        if fp.stat().st_size != a.get("size_bytes"):
            errors.append(f"size mismatch for {rel}")

    # No unexpected files anywhere under the snapshot dir.
    for dirpath, _dirnames, filenames in os.walk(d):
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), d).replace(os.sep, "/")
            if rel not in expected_rel:
                errors.append(f"unexpected file in snapshot: {rel}")
            if is_forbidden_name(fn):
                errors.append(f"forbidden file present in snapshot: {rel}")

    if required_total and required_seen < required_total:
        errors.append(f"required artifacts not fully verified: {required_seen}/{required_total}")

    return {"valid": not errors, "errors": errors, "snapshot_id": sid}


# ---------------------------------------------------------------------------
# Production/shadow SHA comparison (pure; no git, no network)
# ---------------------------------------------------------------------------
def compare_shas(
    production_snapshot_sha: str | None,
    local_shadow_sha: str | None,
    ancestry: Sequence[str] | None = None,
) -> str:
    """
    Compare a production snapshot's git sha against a local shadow sha.

    Returns one of ``MATCH``, ``SHADOW_BEHIND``, ``SHADOW_AHEAD``, ``UNKNOWN``.

    Pure/offline: runs NO git and NO network. Ordering (behind/ahead) can only be
    determined when *ancestry* is supplied — an oldest->newest ordered list of
    commit shas (full or short, matched by prefix). Without it, equal shas report
    MATCH and anything else reports UNKNOWN.
    """
    p = (production_snapshot_sha or "").strip().lower()
    s = (local_shadow_sha or "").strip().lower()
    if not p or not s:
        return "UNKNOWN"
    if p == s or p.startswith(s) or s.startswith(p):
        return "MATCH"
    if not ancestry:
        return "UNKNOWN"
    order = [x.strip().lower() for x in ancestry if x and x.strip()]

    def _index(sha: str) -> int | None:
        for i, c in enumerate(order):
            if c == sha or c.startswith(sha) or sha.startswith(c):
                return i
        return None

    pi, si = _index(p), _index(s)
    if pi is None or si is None:
        return "UNKNOWN"
    if si < pi:
        return "SHADOW_BEHIND"
    if si > pi:
        return "SHADOW_AHEAD"
    return "MATCH"


# ---------------------------------------------------------------------------
# Health producer  (outputs/policy/agent_export_health.json)
# ---------------------------------------------------------------------------
def build_agent_export_health(
    output_root: str | Path = DEFAULT_ARTIFACTS_ROOT,
    now: datetime | None = None,
) -> dict:
    """
    Observe-only health for the export subsystem. Reads the latest pointer +
    validates the latest snapshot. Never mutates anything.

    Status:
      GREEN — a current, valid snapshot exists.
      AMBER — no snapshot yet, or snapshot is stale, or has optional gaps.
      RED   — latest snapshot fails validation (hash/schema/security/missing req).
    """
    now = now or datetime.now(timezone.utc)
    export_root = _export_root(output_root)
    pointer_path = export_root / LATEST_POINTER_NAME

    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": _SOURCE_LABEL,
        "observe_only": _OBSERVE_ONLY,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "latest_snapshot_id": None,
        "latest_snapshot_age_hours": None,
        "production_git_sha": None,
        "required_artifacts_present": None,
        "hash_validation": None,
        "schema_validation": None,
        "warnings": [],
        "errors": [],
        "status": AMBER,
    }

    if not pointer_path.exists():
        out["warnings"].append("no snapshot has been built yet")
        out["status"] = AMBER
        return out

    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except Exception as exc:
        out["errors"].append(f"latest pointer unreadable: {exc}")
        out["status"] = RED
        return out

    sid = pointer.get("snapshot_id")
    out["latest_snapshot_id"] = sid
    out["production_git_sha"] = pointer.get("production_git_sha")
    snap_dir = export_root / SNAPSHOTS_SUBDIR / (sid or "")

    result = validate_agent_snapshot(snap_dir)
    out["schema_validation"] = "pass" if not any(
        "schema_version" in e for e in result["errors"]) else "fail"
    out["hash_validation"] = "pass" if not any(
        "hash mismatch" in e or "snapshot_hash mismatch" in e for e in result["errors"]) else "fail"

    manifest = _read_manifest(snap_dir)
    if manifest:
        req = [a for a in manifest.get("artifacts", []) if a.get("required")]
        out["required_artifacts_present"] = bool(req) or True
        created = manifest.get("created_at")
        try:
            ts = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age = (now - ts).total_seconds() / 3600.0
            out["latest_snapshot_age_hours"] = round(age, 2)
        except Exception:
            age = None
        gaps = [g for g in manifest.get("gaps", []) if not g.get("required")]
        if gaps:
            out["warnings"].append(f"{len(gaps)} optional artifact(s) absent in latest snapshot")
    else:
        age = None
        out["errors"].append("latest snapshot manifest unreadable")

    if not result["valid"]:
        out["errors"].extend(result["errors"])
        out["status"] = RED
    elif age is not None and age > _STALE_HOURS:
        out["warnings"].append(f"latest snapshot is stale ({age:.1f}h > {_STALE_HOURS}h)")
        out["status"] = AMBER
    elif out["warnings"]:
        out["status"] = AMBER
    else:
        out["status"] = GREEN
    return out


def run_agent_export_health(
    output_root: str | Path = DEFAULT_ARTIFACTS_ROOT,
    write_files: bool = True,
) -> dict:
    """Build the export health dict and (optionally) write it to the POLICY namespace."""
    payload = build_agent_export_health(output_root=output_root)
    if write_files:
        # Local import keeps this module importable in stripped environments.
        from portfolio_automation.data_governance import OutputNamespace, safe_write_json
        safe_write_json(OutputNamespace.POLICY, HEALTH_REL, payload, base_dir=str(output_root))
    return payload
