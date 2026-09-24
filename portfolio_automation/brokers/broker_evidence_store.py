# portfolio_automation/brokers/broker_evidence_store.py
"""Immutable-success + latest-attempt persistence for Schwab broker evidence.

Two records, deliberately separate, so a failed synchronization can NEVER
erase the last valid broker observation:

    outputs/latest/broker_evidence_latest_attempt.json
        what the most recent sync attempt did (every attempt writes it)
    outputs/latest/broker_evidence_latest_admitted.json
        the most recent ADMITTED canonical EvidenceSnapshot (only an
        admitted success writes it)
    outputs/archive/broker_evidence/<YYYY-MM-DD>/<snapshot_id>.json
        write-once archive of every admitted snapshot (audit / recovery)

Readers get one of four truths, never a fabricated empty portfolio:

    CURRENT                 latest attempt admitted == latest admitted
    STALE_LAST_KNOWN_GOOD   an admitted snapshot exists; the latest attempt
                            did not replace it (failed / refused / re-auth)
    NO_TRUTH                nothing has ever been admitted
    (+ reauth_required)     orthogonal flag from the latest attempt

``positions()`` returns ``None`` — unknown — when there is no admitted
evidence. It never returns ``[]`` for a failure. The admitted record is
re-verified through ``EvidenceSnapshot.from_dict`` on every read, so a
tampered file is reported as corrupt rather than trusted.

Writes go through the governed writer (``data_governance.safe_write_json``).
The archive follows the existing ``outputs/archive/<lane>/<day>/`` convention
with the same atomic temp-file + replace mechanics; it is write-once.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from portfolio_automation.brokers.broker_models import redact
from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.evidence_gateway.admission import AdmissionDecision
from portfolio_automation.northstar.evidence import EvidenceSnapshot

SCHEMA_VERSION = "1.0.0"
ATTEMPT_CONTRACT_TYPE = "broker_evidence_attempt"
ADMITTED_CONTRACT_TYPE = "broker_evidence_admitted"

LATEST_ATTEMPT_FILE = "broker_evidence_latest_attempt.json"
LATEST_ADMITTED_FILE = "broker_evidence_latest_admitted.json"
ARCHIVE_LANE = "broker_evidence"


class SyncOutcome(str, Enum):
    """Closed set of attempt outcomes. A non-ADMITTED outcome never implies an
    empty portfolio; it implies UNKNOWN current truth."""
    ADMITTED = "ADMITTED"
    UNCONFIGURED = "UNCONFIGURED"
    REAUTH_REQUIRED = "REAUTH_REQUIRED"
    AUTH_UNAVAILABLE = "AUTH_UNAVAILABLE"       # transient auth: rate limit / endpoint / lock
    ACQUISITION_FAILED = "ACQUISITION_FAILED"   # Schwab account/positions GET failed
    SCHEMA_DRIFT = "SCHEMA_DRIFT"               # response shape not recognizable
    NORMALIZATION_FAILED = "NORMALIZATION_FAILED"
    EVIDENCE_REFUSED = "EVIDENCE_REFUSED"       # gateway refused the snapshot


class TruthStatus(str, Enum):
    CURRENT = "CURRENT"
    STALE_LAST_KNOWN_GOOD = "STALE_LAST_KNOWN_GOOD"
    NO_TRUTH = "NO_TRUTH"


def _now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def _write_latest(root: Path, name: str, payload: dict) -> Path:
    return safe_write_json(OutputNamespace.LATEST, name, payload, base_dir=Path(root) / "outputs")


def _read_latest(root: Path, name: str) -> Optional[dict]:
    try:
        data = json.loads((Path(root) / "outputs" / "latest" / name).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def record_attempt(root: Path, *, sync_id: str, outcome: SyncOutcome,
                   auth_state: Optional[str] = None,
                   admission: Optional[AdmissionDecision] = None,
                   snapshot_id: Optional[str] = None,
                   error: Optional[str] = None,
                   now: Optional[datetime] = None) -> dict[str, Any]:
    """Persist the latest-attempt record. Never touches the admitted record."""
    rec = {
        "contract_type": ATTEMPT_CONTRACT_TYPE, "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(now), "observe_only": True, "source": "schwab",
        "sync_id": sync_id,
        "outcome": SyncOutcome(outcome).value,
        "auth_state": auth_state,
        "admission": admission.to_dict() if isinstance(admission, AdmissionDecision) else None,
        "snapshot_id": snapshot_id if outcome is SyncOutcome.ADMITTED else None,
        "error": redact(error) if error else None,
        # Stated explicitly so no reader can misinterpret a failure record.
        "implies_empty_portfolio": False,
        "truth_replaced": outcome is SyncOutcome.ADMITTED,
    }
    try:
        _write_latest(root, LATEST_ATTEMPT_FILE, rec)
    except Exception:  # noqa: BLE001 — observe-only: a status write never breaks the sync
        pass
    return rec


def _archive_write_once(root: Path, day: str, snapshot: EvidenceSnapshot, record: dict) -> Optional[Path]:
    """Write-once archive: an existing file is never rewritten (immutability)."""
    adir = Path(root) / "outputs" / "archive" / ARCHIVE_LANE / day
    target = adir / f"{snapshot.snapshot_id}.json"
    if target.exists():
        return target
    adir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(adir), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record, indent=2, default=str))
        if target.exists():          # lost a race to another writer: keep theirs
            os.unlink(tmp)
            return target
        os.replace(tmp, target)
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def record_admitted(root: Path, *, snapshot: EvidenceSnapshot, decision: AdmissionDecision,
                    sync_id: str, now: Optional[datetime] = None) -> dict[str, Any]:
    """Persist an ADMITTED snapshot as the latest admitted truth + archive it.
    Refuses (raises) for anything the gateway did not admit."""
    if not isinstance(decision, AdmissionDecision) or not decision.admitted:
        raise ValueError("only an ADMITTED decision may be recorded as broker truth")
    if decision.snapshot_id != snapshot.snapshot_id:
        raise ValueError("admission decision does not name this snapshot")
    ts = _now_iso(now)
    rec = {
        "contract_type": ADMITTED_CONTRACT_TYPE, "schema_version": SCHEMA_VERSION,
        "admitted_at": ts, "generated_at": ts, "observe_only": True, "source": "schwab",
        "sync_id": sync_id,
        "snapshot_id": snapshot.snapshot_id,
        "payload_hash": snapshot.payload_hash,
        "admission": decision.to_dict(),
        "evidence": snapshot.to_canonical_dict(),
    }
    _write_latest(root, LATEST_ADMITTED_FILE, rec)
    try:
        _archive_write_once(root, ts[:10], snapshot, rec)
    except Exception:  # noqa: BLE001 — archive is best-effort; latest_admitted is authoritative
        pass
    return rec


@dataclass(frozen=True)
class BrokerEvidenceState:
    truth_status: TruthStatus
    reauth_required: bool
    latest_attempt: Optional[dict[str, Any]]
    latest_admitted: Optional[dict[str, Any]]
    admitted_snapshot: Optional[EvidenceSnapshot] = field(default=None, repr=False)
    admitted_age_s: Optional[float] = None
    integrity_error: Optional[str] = None

    @property
    def latest_attempt_outcome(self) -> Optional[str]:
        return (self.latest_attempt or {}).get("outcome")

    def positions(self) -> Optional[list[dict[str, Any]]]:
        """Admitted positions, or None when current truth is unknown. NEVER []
        as a stand-in for 'the sync failed'."""
        if self.admitted_snapshot is None:
            return None
        return list(self.admitted_snapshot.payload_copy().get("positions", []))

    def to_dict(self) -> dict[str, Any]:
        return {
            "truth_status": self.truth_status.value,
            "reauth_required": self.reauth_required,
            "latest_attempt_outcome": self.latest_attempt_outcome,
            "latest_attempt_at": (self.latest_attempt or {}).get("generated_at"),
            "admitted_snapshot_id": (self.latest_admitted or {}).get("snapshot_id")
            if self.admitted_snapshot is not None else None,
            "admitted_at": (self.latest_admitted or {}).get("admitted_at")
            if self.admitted_snapshot is not None else None,
            "admitted_age_s": self.admitted_age_s,
            "integrity_error": self.integrity_error,
        }


def read_state(root: Path, *, now: Optional[datetime] = None) -> BrokerEvidenceState:
    """Read both records and classify current truth. Re-verifies the admitted
    evidence's identity; a tampered/corrupt admitted record yields NO_TRUTH
    with ``integrity_error`` set, never a trusted payload."""
    attempt = _read_latest(root, LATEST_ATTEMPT_FILE)
    admitted = _read_latest(root, LATEST_ADMITTED_FILE)
    reauth = bool(attempt and attempt.get("outcome") == SyncOutcome.REAUTH_REQUIRED.value)

    snapshot: Optional[EvidenceSnapshot] = None
    integrity_error: Optional[str] = None
    age: Optional[float] = None
    if admitted is not None:
        try:
            if admitted.get("contract_type") != ADMITTED_CONTRACT_TYPE:
                raise ValueError("not a broker_evidence_admitted record")
            snapshot = EvidenceSnapshot.from_dict(admitted["evidence"])
            if admitted.get("snapshot_id") != snapshot.snapshot_id:
                raise ValueError("recorded snapshot_id does not reproduce from the evidence")
            if admitted.get("payload_hash") != snapshot.payload_hash:
                raise ValueError("recorded payload_hash does not reproduce from the evidence")
            adm = admitted.get("admission") or {}
            if not adm.get("admitted") or adm.get("snapshot_id") != snapshot.snapshot_id:
                raise ValueError("admitted record carries a non-admitting or mismatched decision")
        except Exception as exc:  # noqa: BLE001 — corrupt truth is reported, never trusted
            snapshot = None
            integrity_error = f"{type(exc).__name__}: {redact(str(exc))}"
        else:
            try:
                admitted_at = datetime.fromisoformat(str(admitted.get("admitted_at")))
                ref = now or datetime.now(timezone.utc)
                age = max(0.0, (ref - admitted_at).total_seconds())
            except (TypeError, ValueError):
                age = None

    if snapshot is None:
        status = TruthStatus.NO_TRUTH
    elif (attempt and attempt.get("outcome") == SyncOutcome.ADMITTED.value
          and attempt.get("snapshot_id") == snapshot.snapshot_id):
        status = TruthStatus.CURRENT
    else:
        status = TruthStatus.STALE_LAST_KNOWN_GOOD
    return BrokerEvidenceState(truth_status=status, reauth_required=reauth,
                               latest_attempt=attempt, latest_admitted=admitted,
                               admitted_snapshot=snapshot, admitted_age_s=age,
                               integrity_error=integrity_error)
