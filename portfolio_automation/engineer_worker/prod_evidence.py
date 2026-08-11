"""Temporary Direct Production Evidence Bridge V0 (EXPERIMENTAL, TEMPORARY).

Lets the TRUSTED LOCAL controller retrieve narrowly-allowlisted, READ-ONLY
evidence from the StockBot production VPS through a single restricted capability,
so the local Engineer Worker can diagnose the actual current Daily Safe run.

Hard boundaries (enforced here, in trusted code):
* The model only chooses a capability enum + a validated selector. It NEVER
  controls host/user/identity/ssh options/remote executable — those live in a
  trusted ``CollectorConfig`` the operator wires up, never in model input and
  never inside the rd-worker sandbox.
* The SSH invocation is fixed-argv (never ``shell=True``), with
  ``StrictHostKeyChecking=yes``, a dedicated ``known_hosts``, ``IdentitiesOnly``,
  ``BatchMode``, and all forwarding cleared. The remote side is a forced-command
  wrapper (installed out-of-band by the operator); this client cannot obtain a
  shell, arbitrary path, or arbitrary SQL.
* Raw SSH stdout is never handed to the model. It is size-bounded, secret-scanned
  /sanitized, bound to run_id + source_commit where available, SHA-256 hashed,
  and persisted as an immutable local snapshot before admission.

This is ``experimental_noncanonical`` + ``temporary_direct_read`` and does NOT
create or modify canonical Northstar EvidenceRef/EvidenceSnapshot contracts. It
is superseded by the governed Agent Export / authenticated evidence-admission
path. See docs/PROD_EVIDENCE_DIRECT_V0.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER

SCHEMA_KIND = EXPERIMENTAL_MARKER
TEMPORARY_MARKER = "temporary_direct_read"
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
    """Server-side forced-command capability IDs (the only verbs the wrapper
    accepts). These are NOT shell commands."""
    DAILY_STATUS = "daily-status"
    DAILY_LOG = "daily-log"
    RUN_MANIFEST = "run-manifest"
    ARTIFACT = "artifact"
    DB_QUERY = "db-query"


# Predefined DB query IDs -> fixed SQL lives SERVER-SIDE in the wrapper. The
# client/model may only name an ID; it may never submit SQL text.
ALLOWED_DB_QUERY_IDS = frozenset({"latest-daily-run", "recent-health", "recent-errors"})

# Allowlisted artifact names (NOT paths). The wrapper maps each to a fixed file.
ALLOWED_ARTIFACT_NAMES = frozenset({
    "daily_status", "run_manifest", "health", "artifact_registry_health",
    "decision_summary", "risk_summary",
})

_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_RUN_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_ARTIFACT_RE = re.compile(r"\A[a-z0-9_]{1,64}\Z")
_QUERY_ID_RE = re.compile(r"\A[a-z0-9-]{1,64}\Z")


def validate_selector(capability: ProductionEvidenceCapability, selector: str) -> str:
    """Validate the model-supplied selector for a capability. Fail closed on
    anything not explicitly allowed (no paths, no traversal, no SQL, no shell)."""
    sel = (selector or "").strip()
    if "\x00" in sel or "\n" in sel or "\r" in sel:
        raise ProdEvidenceError("selector contains control characters")
    if capability is ProductionEvidenceCapability.DAILY_STATUS:
        return ""  # no selector needed
    if capability is ProductionEvidenceCapability.DAILY_LOG:
        if sel in ("today", "latest") or _DATE_RE.match(sel):
            return sel or "latest"
        raise ProdEvidenceError(f"daily-log selector must be today|latest|YYYY-MM-DD: {sel!r}")
    if capability is ProductionEvidenceCapability.RUN_MANIFEST:
        if sel in ("latest", "") or _RUN_ID_RE.match(sel):
            return sel or "latest"
        raise ProdEvidenceError(f"run-manifest selector must be a run_id or latest: {sel!r}")
    if capability is ProductionEvidenceCapability.ARTIFACT:
        if _ARTIFACT_RE.match(sel) and sel in ALLOWED_ARTIFACT_NAMES:
            return sel
        raise ProdEvidenceError(f"artifact selector not in allowlist: {sel!r}")
    if capability is ProductionEvidenceCapability.DB_QUERY:
        if _QUERY_ID_RE.match(sel) and sel in ALLOWED_DB_QUERY_IDS:
            return sel
        raise ProdEvidenceError(f"db-query id not in allowlist: {sel!r}")
    raise ProdEvidenceError(f"unknown capability: {capability!r}")


@dataclass
class ProductionEvidenceDirectV0:
    retrieval_id: str
    retrieved_at: str
    capability: str
    selector: str
    status: ProductionEvidenceStatus
    schema_version: str = EVIDENCE_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND
    temporary: str = TEMPORARY_MARKER
    run_id: str | None = None
    source_commit: str | None = None
    content_sha256: str | None = None
    byte_count: int = 0
    admitted: bool = False
    content: Any = None            # sanitized text or parsed JSON (never raw secrets)
    rejection_reason: str | None = None
    snapshot_path: str | None = None

    def audit_record(self) -> dict[str, Any]:
        """Audit metadata — contains NO secrets, NO host/key/identity."""
        return {
            "retrieval_id": self.retrieval_id, "retrieved_at": self.retrieved_at,
            "capability": self.capability, "selector": self.selector,
            "status": self.status.value, "run_id": self.run_id,
            "source_commit": self.source_commit, "content_sha256": self.content_sha256,
            "byte_count": self.byte_count, "admitted": self.admitted,
            "rejection_reason": self.rejection_reason,
        }

    def to_model_view(self) -> dict[str, Any]:
        """The ONLY thing exposed toward the worker/model: admitted, sanitized
        content + provenance ids. No host, key path, identity, or raw stdout."""
        return {
            "schema_version": self.schema_version, "schema_kind": self.schema_kind,
            "temporary": self.temporary, "status": self.status.value,
            "capability": self.capability, "selector": self.selector,
            "run_id": self.run_id, "source_commit": self.source_commit,
            "content_sha256": self.content_sha256, "byte_count": self.byte_count,
            "admitted": self.admitted, "content": self.content,
            "rejection_reason": self.rejection_reason,
        }


# --- transport (dependency-injected; default = fixed-argv ssh) ---------------
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
    identity_file: str                       # e.g. ~/.ssh/stockbot_observer (trusted env only)
    known_hosts_file: str                    # dedicated known_hosts with the PINNED VPS host key
    user: str = "stockbot-observer"
    ssh_bin: str = "ssh"
    connect_timeout: int = 20
    snapshot_dir: str | None = None          # where immutable local snapshots are written
    audit_log: str | None = None             # optional JSONL audit sink


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
            "-i", cfg.identity_file,
            f"{cfg.user}@{cfg.host}",
            *remote_argv,   # capability + selector -> forced-command wrapper via SSH_ORIGINAL_COMMAND
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


# --- secret scanning / sanitization (defense in depth) -----------------------
_HARD_SECRET_RE = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    r"\b(?:refresh_token|access_token|client_secret|schwab[_-]?token)\b\s*[:=]\s*\S{8,}",
    re.IGNORECASE)
_SOFT_SECRET_RE = re.compile(
    r"(?i)\b(password|secret|token|api[_-]?key|authorization|bearer)\b\s*[:=]\s*\S+")


def _scan_and_sanitize(text: str) -> tuple[str, str | None]:
    """Return (sanitized_text, hard_reject_reason|None). Hard credential material
    -> reject (fail closed). Soft secret-shaped assignments -> redacted."""
    if _HARD_SECRET_RE.search(text):
        return text, "hard credential material present in evidence"
    sanitized = _SOFT_SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    return sanitized, None


class ProductionEvidenceCollector:
    """Trusted-side collector. Runs in the operator's Python process — never in
    the sandbox, never with model-controlled connection facts."""

    def __init__(self, cfg: CollectorConfig, now_fn: Callable[[], str],
                 transport: SshTransport | None = None):
        self.cfg = cfg
        self.now_fn = now_fn
        self.transport = transport or _default_ssh_transport(cfg)

    def retrieve(self, capability: ProductionEvidenceCapability, selector: str
                 ) -> ProductionEvidenceDirectV0:
        retrieval_id = "pe-" + uuid.uuid4().hex[:16]
        # Validate BEFORE any transport call (selector never reaches a shell).
        try:
            cap = capability if isinstance(capability, ProductionEvidenceCapability) \
                else ProductionEvidenceCapability(str(capability))
            sel = validate_selector(cap, selector)
        except (ValueError, ProdEvidenceError) as e:
            return self._finish(retrieval_id, str(capability), str(selector),
                                ProductionEvidenceStatus.REJECTED, None, reason=str(e))

        remote_argv = [cap.value] + ([sel] if sel else [])
        res = self.transport(remote_argv)

        # Host-key / identity verification failure -> distinct fail-closed state.
        if res.rc != 0 and ("Host key verification failed" in res.stderr
                            or "REMOTE HOST IDENTIFICATION HAS CHANGED" in res.stderr
                            or "No ED25519 host key is known" in res.stderr):
            return self._finish(retrieval_id, cap.value, sel,
                                ProductionEvidenceStatus.IDENTITY_UNVERIFIED, None,
                                reason="host key not verified")
        if res.rc is None or res.rc != 0 or not res.stdout.strip():
            return self._finish(retrieval_id, cap.value, sel,
                                ProductionEvidenceStatus.UNAVAILABLE, None,
                                reason=(res.stderr.strip()[:200] or "empty/failed"))

        raw = res.stdout
        if len(raw.encode("utf-8", "replace")) > _MAX_EVIDENCE_BYTES:
            return self._finish(retrieval_id, cap.value, sel,
                                ProductionEvidenceStatus.REJECTED, None,
                                reason="evidence exceeds size bound")
        sanitized, hard = _scan_and_sanitize(raw)
        if hard:
            return self._finish(retrieval_id, cap.value, sel,
                                ProductionEvidenceStatus.REJECTED, None, reason=hard)

        # Parse (JSON where applicable) + bind run_id / source_commit.
        content: Any = sanitized
        run_id = source_commit = None
        parsed = None
        try:
            parsed = json.loads(sanitized)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            content = parsed
            run_id = parsed.get("run_id") or parsed.get("runId")
            source_commit = parsed.get("source_commit") or parsed.get("commit")
        else:
            m = re.search(r"run_id[:=]\s*([A-Za-z0-9._:-]+)", sanitized)
            run_id = m.group(1) if m else None
            m = re.search(r"(?:source_commit|commit)[:=]\s*([0-9a-f]{7,40})", sanitized)
            source_commit = m.group(1) if m else None

        return self._finish(retrieval_id, cap.value, sel,
                            ProductionEvidenceStatus.AVAILABLE, content,
                            run_id=run_id, source_commit=source_commit,
                            raw_for_hash=sanitized)

    def _finish(self, retrieval_id, capability, selector, status, content, *,
                run_id=None, source_commit=None, reason=None, raw_for_hash=None
                ) -> ProductionEvidenceDirectV0:
        sha = byte_count = None
        snap_path = None
        admitted = status is ProductionEvidenceStatus.AVAILABLE
        if admitted:
            blob = (raw_for_hash if raw_for_hash is not None
                    else json.dumps(content, sort_keys=True)).encode("utf-8", "replace")
            sha = "sha256:" + hashlib.sha256(blob).hexdigest()
            byte_count = len(blob)
            snap_path = self._write_snapshot(retrieval_id, blob)
        ev = ProductionEvidenceDirectV0(
            retrieval_id=retrieval_id, retrieved_at=self.now_fn(), capability=capability,
            selector=selector, status=status, run_id=run_id, source_commit=source_commit,
            content_sha256=sha, byte_count=byte_count or 0, admitted=admitted,
            content=content if admitted else None, rejection_reason=reason,
            snapshot_path=snap_path)
        self._audit(ev)
        return ev

    def _write_snapshot(self, retrieval_id: str, blob: bytes) -> str | None:
        if not self.cfg.snapshot_dir:
            return None
        d = Path(self.cfg.snapshot_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{retrieval_id}.snapshot"
        p.write_bytes(blob)
        try:
            os.chmod(p, 0o444)   # immutable-ish: read-only local snapshot
        except OSError:
            pass
        return str(p)

    def _audit(self, ev: ProductionEvidenceDirectV0) -> None:
        if not self.cfg.audit_log:
            return
        try:
            with open(self.cfg.audit_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev.audit_record(), ensure_ascii=True) + "\n")
        except OSError:
            pass
