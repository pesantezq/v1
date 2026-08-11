"""Temporary Direct Production Evidence Bridge V0 (EXPERIMENTAL, TEMPORARY).

Lets the TRUSTED LOCAL controller retrieve narrowly-allowlisted, READ-ONLY
evidence from the StockBot production VPS (account ``stockbot-engineer``, forced
command ``/usr/local/sbin/stockbot-engineer-read``) so the local Engineer Worker
can diagnose the actual current Daily Safe run.

Hard boundaries (enforced here, in trusted code):
* The model only chooses a ``ModelCapability`` enum + a validated selector. It
  NEVER controls host/user/identity/ssh options/remote command — those live in a
  trusted ``CollectorConfig`` the operator wires up, never in model input and
  never inside the rd-worker sandbox.
* The SSH invocation is fixed-argv (never ``shell=True``), with
  ``StrictHostKeyChecking=yes``, a dedicated ``known_hosts``, ``IdentitiesOnly``,
  ``BatchMode``, and all forwarding cleared. The remote side is a forced-command
  wrapper; this client cannot get a shell, arbitrary path, or arbitrary SQL.
* Raw SSH stdout is never handed to the model. It is size-bounded, secret-screened
  (FAIL CLOSED), run-identity checked, bound to run_id + source_commit where
  available, SHA-256 hashed, and persisted as an immutable local snapshot before
  admission.

``experimental_noncanonical`` + ``temporary_direct_read``. Does NOT create or
modify canonical Northstar EvidenceRef/EvidenceSnapshot/WorkerResult/ResearchTask.
See docs/PROD_EVIDENCE_DIRECT_V0.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER

SCHEMA_KIND = EXPERIMENTAL_MARKER
TEMPORARY_MARKER = "temporary_direct_read"
CONTRACT_STATUS = f"{EXPERIMENTAL_MARKER}/{TEMPORARY_MARKER}"
EVIDENCE_SCHEMA_VERSION = "engineering.production_evidence_direct.v0"
_MAX_EVIDENCE_BYTES = 1 << 20            # 1 MiB hard cap on admitted content


class ProdEvidenceError(ValueError):
    """Deterministic, fail-closed validation error."""


class ProductionEvidenceStatus(str, Enum):
    AVAILABLE = "PRODUCTION_EVIDENCE_AVAILABLE"
    UNAVAILABLE = "PRODUCTION_EVIDENCE_UNAVAILABLE"
    REJECTED = "PRODUCTION_EVIDENCE_REJECTED"
    IDENTITY_UNVERIFIED = "PRODUCTION_EVIDENCE_IDENTITY_UNVERIFIED"


class ProductionEvidenceCapability(str, Enum):
    """Server-side forced-command verbs (the only verbs stockbot-engineer-read
    accepts). NOT shell commands. Mirrors the certified VPS allowlist."""
    DAILY_STATUS = "daily-status"
    DAILY_STATUS_JSON = "daily-status-json"
    PIPELINE_STATUS = "pipeline-status"
    RUN_MANIFEST = "run-manifest"
    HEALTH = "health"
    LAST_SUCCESS = "last-success"
    DAILY_LOG = "daily-log"          # optional [YYYY-MM-DD]
    DAILY_CHECK = "daily-check"      # optional [YYYY-MM-DD]
    DB_QUERY = "db-query"            # fixed query id only


class ModelCapability(str, Enum):
    """The ONLY capability names the Engineer Worker / model may request. Mapped
    deterministically to a server verb + fixed selector; the model can never name
    a raw verb, a path, or SQL."""
    PROD_DAILY_STATUS = "PROD_DAILY_STATUS"
    PROD_DAILY_STATUS_JSON = "PROD_DAILY_STATUS_JSON"
    PROD_PIPELINE_STATUS = "PROD_PIPELINE_STATUS"
    PROD_RUN_MANIFEST = "PROD_RUN_MANIFEST"
    PROD_HEALTH = "PROD_HEALTH"
    PROD_LAST_SUCCESS = "PROD_LAST_SUCCESS"
    PROD_DAILY_LOG = "PROD_DAILY_LOG"        # takes a date selector
    PROD_DAILY_CHECK = "PROD_DAILY_CHECK"    # takes a date selector
    PROD_DB_LATEST_DAILY_RUN = "PROD_DB_LATEST_DAILY_RUN"
    PROD_DB_RECENT_HEALTH = "PROD_DB_RECENT_HEALTH"
    PROD_DB_RECENT_ERRORS = "PROD_DB_RECENT_ERRORS"


# model capability -> (server verb, fixed selector | None, accepts_date_selector)
_MODEL_MAP: dict[ModelCapability, tuple[ProductionEvidenceCapability, str | None, bool]] = {
    ModelCapability.PROD_DAILY_STATUS:       (ProductionEvidenceCapability.DAILY_STATUS, None, False),
    ModelCapability.PROD_DAILY_STATUS_JSON:  (ProductionEvidenceCapability.DAILY_STATUS_JSON, None, False),
    ModelCapability.PROD_PIPELINE_STATUS:    (ProductionEvidenceCapability.PIPELINE_STATUS, None, False),
    ModelCapability.PROD_RUN_MANIFEST:       (ProductionEvidenceCapability.RUN_MANIFEST, None, False),
    ModelCapability.PROD_HEALTH:             (ProductionEvidenceCapability.HEALTH, None, False),
    ModelCapability.PROD_LAST_SUCCESS:       (ProductionEvidenceCapability.LAST_SUCCESS, None, False),
    ModelCapability.PROD_DAILY_LOG:          (ProductionEvidenceCapability.DAILY_LOG, None, True),
    ModelCapability.PROD_DAILY_CHECK:        (ProductionEvidenceCapability.DAILY_CHECK, None, True),
    ModelCapability.PROD_DB_LATEST_DAILY_RUN:(ProductionEvidenceCapability.DB_QUERY, "latest-daily-run", False),
    ModelCapability.PROD_DB_RECENT_HEALTH:   (ProductionEvidenceCapability.DB_QUERY, "recent-health", False),
    ModelCapability.PROD_DB_RECENT_ERRORS:   (ProductionEvidenceCapability.DB_QUERY, "recent-errors", False),
}

ALLOWED_DB_QUERY_IDS = frozenset({"latest-daily-run", "recent-health", "recent-errors"})
_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_QUERY_ID_RE = re.compile(r"\A[a-z0-9-]{1,64}\Z")
_NO_SELECTOR = frozenset({
    ProductionEvidenceCapability.DAILY_STATUS, ProductionEvidenceCapability.DAILY_STATUS_JSON,
    ProductionEvidenceCapability.PIPELINE_STATUS, ProductionEvidenceCapability.RUN_MANIFEST,
    ProductionEvidenceCapability.HEALTH, ProductionEvidenceCapability.LAST_SUCCESS,
})


def resolve_model_capability(name: str, selector: str = "") -> tuple[ProductionEvidenceCapability, str]:
    """Map a model-visible PROD_* name (+ optional selector) to a server verb and
    validated selector. Fail closed on anything unknown."""
    try:
        mc = ModelCapability(str(name).strip())
    except ValueError:
        raise ProdEvidenceError(f"unknown model capability: {name!r}")
    verb, fixed_sel, date_ok = _MODEL_MAP[mc]
    sel = fixed_sel if fixed_sel is not None else (selector if date_ok else "")
    return verb, validate_selector(verb, sel)


def validate_selector(capability: ProductionEvidenceCapability, selector: str) -> str:
    """Validate the selector for a server verb. Fail closed on anything not
    explicitly allowed (no paths, no traversal, no SQL, no shell)."""
    sel = (selector or "").strip()
    if any(c in sel for c in "\x00\n\r"):
        raise ProdEvidenceError("selector contains control characters")
    if capability in _NO_SELECTOR:
        return ""
    if capability in (ProductionEvidenceCapability.DAILY_LOG, ProductionEvidenceCapability.DAILY_CHECK):
        if sel in ("today", "latest", ""):
            return sel or "latest"
        if _DATE_RE.match(sel):
            return sel
        raise ProdEvidenceError(f"{capability.value} selector must be today|latest|YYYY-MM-DD: {sel!r}")
    if capability is ProductionEvidenceCapability.DB_QUERY:
        if _QUERY_ID_RE.match(sel) and sel in ALLOWED_DB_QUERY_IDS:
            return sel
        raise ProdEvidenceError(f"db-query id not in allowlist: {sel!r}")
    raise ProdEvidenceError(f"unknown capability: {capability!r}")


@dataclass
class ProductionEvidenceDirectV0:
    retrieval_id: str
    capability: str
    selector: str
    requested_at: str
    retrieved_at: str
    status: ProductionEvidenceStatus
    schema_version: str = EVIDENCE_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND
    contract_status: str = CONTRACT_STATUS
    temporary: str = TEMPORARY_MARKER
    source_environment: str | None = None
    source_identity: str | None = None
    run_id: str | None = None
    source_commit: str | None = None
    generated_at: str | None = None
    content_sha256: str | None = None
    content_byte_count: int = 0
    admitted: bool = False
    content: Any = None
    rejection_reason: str | None = None
    snapshot_path: str | None = None

    def audit_record(self) -> dict[str, Any]:
        """Audit metadata — NO secrets, NO host/key/identity-path."""
        return {
            "retrieval_id": self.retrieval_id, "requested_at": self.requested_at,
            "retrieved_at": self.retrieved_at, "capability": self.capability,
            "selector": self.selector, "source_environment": self.source_environment,
            "source_identity": self.source_identity, "status": self.status.value,
            "run_id": self.run_id, "source_commit": self.source_commit,
            "generated_at": self.generated_at, "content_sha256": self.content_sha256,
            "content_byte_count": self.content_byte_count, "admitted": self.admitted,
            "rejection_reason": self.rejection_reason,
        }

    def to_model_view(self) -> dict[str, Any]:
        """The ONLY thing exposed toward the worker/model: admitted, sanitized
        content + provenance ids. No host, key path, identity, or raw stdout."""
        return {
            "schema_version": self.schema_version, "contract_status": self.contract_status,
            "status": self.status.value, "capability": self.capability, "selector": self.selector,
            "source_environment": self.source_environment, "run_id": self.run_id,
            "source_commit": self.source_commit, "generated_at": self.generated_at,
            "content_sha256": self.content_sha256, "content_byte_count": self.content_byte_count,
            "admitted": self.admitted, "content": self.content,
            "rejection_reason": self.rejection_reason,
        }


@dataclass
class SshResult:
    rc: int | None
    stdout: str
    stderr: str


SshTransport = Callable[[list[str]], SshResult]


@dataclass
class CollectorConfig:
    """Trusted-controller-owned connection facts. NEVER model-controlled, NEVER
    placed in the sandbox / repo / .env / model input."""
    host: str
    identity_file: str                       # ~/.ssh/stockbot_engineer (trusted env only)
    known_hosts_file: str                    # dedicated known_hosts with the PINNED host key
    user: str = "stockbot-engineer"
    port: int = 22
    ssh_bin: str = "ssh"
    connect_timeout: int = 20
    snapshot_dir: str | None = None
    audit_log: str | None = None
    source_environment: str = "production-vps"


def _default_ssh_transport(cfg: CollectorConfig) -> SshTransport:
    def _run(remote_argv: list[str]) -> SshResult:
        argv = [
            cfg.ssh_bin,
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={cfg.known_hosts_file}",
            "-o", f"ConnectTimeout={cfg.connect_timeout}",
            "-o", "ClearAllForwardings=yes",
            "-o", "ForwardAgent=no",
            "-o", "ForwardX11=no",
            "-o", "RequestTTY=no",
            "-p", str(cfg.port),
            "-i", cfg.identity_file,
            f"{cfg.user}@{cfg.host}",
            *remote_argv,   # verb + selector -> forced-command wrapper via SSH_ORIGINAL_COMMAND
        ]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=cfg.connect_timeout + 40,
                               check=False,
                               env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
            return SshResult(p.returncode, p.stdout or "", p.stderr or "")
        except FileNotFoundError:
            return SshResult(127, "", "ssh client not found")
        except subprocess.TimeoutExpired:
            return SshResult(None, "", "ssh timeout")
    return _run


# --- secret screening (FAIL CLOSED — Phase N) --------------------------------
_SECRET_RE = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    r"\bAKIA[0-9A-Z]{16}\b|"                                   # AWS key id
    r"\b(?:password|secret|token|api[_-]?key|authorization|bearer|"
    r"refresh_token|access_token|client_secret|schwab[_-]?token)\b"
    r"\s*[:=]\s*(?!<redacted>|null|\"\"|''|None)\S{4,}",
    re.IGNORECASE)


def _detect_secret(text: str) -> str | None:
    """Deterministic secret detection. Returns a short reason (NOT the value) or
    None. Production status/log evidence must not carry credential material."""
    if _SECRET_RE.search(text):
        return "secret-like material detected in evidence"
    return None


def assert_coherent(evidence: list[ProductionEvidenceDirectV0]) -> None:
    """Cross-evidence run-identity coherence (Phase O). Raises ProdEvidenceError
    if two admitted pieces disagree on run_id or source_commit — never silently
    mix runs."""
    run_ids = {e.run_id for e in evidence if e.admitted and e.run_id}
    commits = {e.source_commit for e in evidence if e.admitted and e.source_commit}
    if len(run_ids) > 1:
        raise ProdEvidenceError(f"mixed run_ids across evidence: {sorted(run_ids)}")
    if len(commits) > 1:
        raise ProdEvidenceError(f"mixed source_commits across evidence: {sorted(commits)}")


class ProductionEvidenceCollector:
    """Trusted-side collector. Runs in the operator's Python process — never in
    the sandbox, never with model-controlled connection facts."""

    def __init__(self, cfg: CollectorConfig, now_fn: Callable[[], str],
                 transport: SshTransport | None = None):
        self.cfg = cfg
        self.now_fn = now_fn
        self.transport = transport or _default_ssh_transport(cfg)

    def retrieve(self, capability: ProductionEvidenceCapability, selector: str = "",
                 *, expected_run_id: str | None = None) -> ProductionEvidenceDirectV0:
        requested_at = self.now_fn()
        retrieval_id = "pe-" + uuid.uuid4().hex[:16]
        try:
            cap = (capability if isinstance(capability, ProductionEvidenceCapability)
                   else ProductionEvidenceCapability(str(capability)))
            sel = validate_selector(cap, selector)
        except (ValueError, ProdEvidenceError) as e:
            return self._finish(retrieval_id, requested_at, str(capability), str(selector),
                                ProductionEvidenceStatus.REJECTED, None, reason=str(e))

        remote_argv = [cap.value] + ([sel] if sel else [])
        res = self.transport(remote_argv)

        # Host-key / transport security failure -> UNAVAILABLE (never auto-accept).
        if res.rc != 0 and ("Host key verification failed" in res.stderr
                            or "REMOTE HOST IDENTIFICATION HAS CHANGED" in res.stderr
                            or "No ED25519 host key is known" in res.stderr
                            or "Permission denied" in res.stderr):
            return self._finish(retrieval_id, requested_at, cap.value, sel,
                                ProductionEvidenceStatus.UNAVAILABLE, None,
                                reason="host-key/auth verification failed")
        if res.rc is None or res.rc != 0 or not res.stdout.strip():
            return self._finish(retrieval_id, requested_at, cap.value, sel,
                                ProductionEvidenceStatus.UNAVAILABLE, None,
                                reason=(res.stderr.strip()[:200] or "empty/failed"))

        raw = res.stdout
        if len(raw.encode("utf-8", "replace")) > _MAX_EVIDENCE_BYTES:
            return self._finish(retrieval_id, requested_at, cap.value, sel,
                                ProductionEvidenceStatus.REJECTED, None, reason="evidence exceeds size bound")
        try:
            raw.encode("utf-8").decode("utf-8")
        except UnicodeError:
            return self._finish(retrieval_id, requested_at, cap.value, sel,
                                ProductionEvidenceStatus.REJECTED, None, reason="non-utf8 content")
        secret = _detect_secret(raw)
        if secret:
            return self._finish(retrieval_id, requested_at, cap.value, sel,
                                ProductionEvidenceStatus.REJECTED, None, reason=secret)

        content: Any = raw
        run_id = source_commit = generated_at = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            content = parsed
            if isinstance(parsed, dict):
                run_id = parsed.get("run_id") or parsed.get("runId")
                source_commit = parsed.get("source_commit") or parsed.get("commit")
                generated_at = parsed.get("generated_at") or parsed.get("timestamp")
        else:
            m = re.search(r"run_id[:=]\s*([A-Za-z0-9._:-]+)", raw)
            run_id = m.group(1) if m else None
            m = re.search(r"(?:source_commit|commit)[:=]\s*([0-9a-f]{7,40})", raw)
            source_commit = m.group(1) if m else None

        # Run-identity gate (Phase O): a MISMATCH against an expected run is a
        # hard identity failure; absence of a run_id (by design, e.g. status)
        # is fine — freshness is carried by generated_at/retrieved_at instead.
        if expected_run_id is not None and run_id is not None and run_id != expected_run_id:
            return self._finish(retrieval_id, requested_at, cap.value, sel,
                                ProductionEvidenceStatus.IDENTITY_UNVERIFIED, None,
                                reason=f"run_id {run_id} != expected {expected_run_id}",
                                run_id=run_id, source_commit=source_commit)

        return self._finish(retrieval_id, requested_at, cap.value, sel,
                            ProductionEvidenceStatus.AVAILABLE, content, run_id=run_id,
                            source_commit=source_commit, generated_at=generated_at, raw_for_hash=raw)

    def _finish(self, retrieval_id, requested_at, capability, selector, status, content, *,
                run_id=None, source_commit=None, generated_at=None, reason=None, raw_for_hash=None
                ) -> ProductionEvidenceDirectV0:
        sha = None
        byte_count = 0
        snap_path = None
        admitted = status is ProductionEvidenceStatus.AVAILABLE
        if admitted:
            blob = (raw_for_hash if raw_for_hash is not None
                    else json.dumps(content, sort_keys=True)).encode("utf-8", "replace")
            sha = "sha256:" + hashlib.sha256(blob).hexdigest()
            byte_count = len(blob)
            try:
                snap_path = self._write_snapshot(retrieval_id, blob)
            except (ProdEvidenceError, OSError) as e:
                # Snapshot persistence is part of admission; if it can't be made
                # immutable/safe, fail closed rather than admit unsnapshotted.
                status = ProductionEvidenceStatus.REJECTED
                admitted = False
                content = None
                sha = None
                byte_count = 0
                reason = f"snapshot failed: {e}"
        ev = ProductionEvidenceDirectV0(
            retrieval_id=retrieval_id, capability=capability, selector=selector,
            requested_at=requested_at, retrieved_at=self.now_fn(), status=status,
            source_environment=self.cfg.source_environment,
            source_identity=f"{self.cfg.user}@{self.cfg.host}:{capability}",
            run_id=run_id, source_commit=source_commit, generated_at=generated_at,
            content_sha256=sha, content_byte_count=byte_count, admitted=admitted,
            content=content if admitted else None, rejection_reason=reason, snapshot_path=snap_path)
        self._audit(ev)
        return ev

    def _write_snapshot(self, retrieval_id: str, blob: bytes) -> str | None:
        if not self.cfg.snapshot_dir:
            return None
        d = Path(self.cfg.snapshot_dir)
        if d.is_symlink():
            raise ProdEvidenceError("snapshot dir is a symlink")
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{retrieval_id}.snapshot"
        if p.exists() or p.is_symlink():
            raise ProdEvidenceError("snapshot already exists (no overwrite)")
        tmp = d / f".{retrieval_id}.tmp"
        tmp.write_bytes(blob)
        os.chmod(tmp, 0o444)
        os.replace(tmp, p)                    # atomic create
        return str(p)

    def _audit(self, ev: ProductionEvidenceDirectV0) -> None:
        if not self.cfg.audit_log:
            return
        try:
            with open(self.cfg.audit_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev.audit_record(), ensure_ascii=True) + "\n")
        except OSError:
            pass
