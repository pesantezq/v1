"""Content-addressed immutable snapshots for the Intraday Lab. HISTORICAL only.

Every snapshot is keyed by a hash of its own content, so identity and content
cannot drift apart. Three rules, all enforced rather than documented:

* **Verify-and-reuse.** Writing a snapshot whose directory already exists with
  identical content is a no-op that returns the same identity.
* **Hard failure on collision.** Same identity, different bytes means the
  fingerprint no longer describes the data. That is never resolved by
  overwriting — a silently replaced dataset would invalidate every experiment
  that ever bound to it, with no trace.
* **Nothing is mutated in place.** A correction is a new identity.

`retrieved_at` is deliberately outside the raw content hash: refetching the same
market observations must reuse the same raw identity, or the store would fill
with duplicates that differ only by when they were downloaded.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from portfolio_automation.intraday_lab import identity as ID
from portfolio_automation.intraday_lab.identity import (        # noqa: F401
    canonical_json as _identity_canonical_json, content_hash,
    INTEGRITY_FAILURE, VERIFIED_CURRENT, VERIFIED_LEGACY_ARCHIVAL,
    VERIFIED_LEGACY_MIGRATABLE, VERIFIED_STATES, CURRENT_RESEARCH_STATES,
)

SCHEMA_VERSION = "1"

# CONTENT objects are keyed by what the data IS. EVENT objects record when and
# why a retrieval/build happened. Mixing them is what produced the false
# collision: an acquisition_manifest carrying retrieved_at lived inside a
# content-addressed raw directory, so refetching identical observations an hour
# later raised SnapshotCollisionError. Content dedupes; events accumulate.
RAW = "raw/content"
RAW_EVENTS = "raw/events"
DATASETS = "datasets/content"
DATASET_MANIFESTS = "datasets/manifests"
DATASET_EVENTS = "datasets/events"
FEATURES = "features/content"
FEATURE_EVENTS = "features/events"
# Identity-era migration lineage. LINEAGE is content-addressed (the same
# migration always has the same identity); EVENTS carry when it was performed.
MIGRATIONS = "migrations/lineage"
MIGRATION_EVENTS = "migrations/events"
# Bounded historical pilot evidence. Content-addressed, so a graduation verdict
# can be re-derived from disk after any restart instead of living in a caller's
# in-memory dictionary.
PILOTS = "pilot/content"
PILOT_EVENTS = "pilot/events"
# The certified exchange schedule a dataset was built under, archived so the
# calendar's MEANING survives a dependency upgrade rather than only its digest.
CALENDARS = "calendar/content"
# The single mutable pointer in the store: which pilot is the graduation
# evidence. It names an immutable object and never copies one.
GRADUATION_POINTER = "graduation/pointer.json"

CONTENT_KINDS = frozenset({RAW, DATASETS, FEATURES})


class SnapshotCollisionError(RuntimeError):
    """Same identity, different content. Never resolved by overwriting."""


def intraday_root(root: str = ".") -> Path:
    """`outputs/backtest/intraday` — HISTORICAL namespace, never LATEST."""
    return Path(root) / "outputs" / "backtest" / "intraday"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _canonical_json(payload: Any) -> str:
    # Single source of truth lives in identity.py so an era's hash function and
    # the store can never drift apart on separators, key order or coercion.
    return _identity_canonical_json(payload)


def raw_payload_hash(rows: Any, *, symbol: str, timeframe: str,
                     provider: str = "fmp", endpoint: str = "") -> str:
    """Identity of a raw provider observation object, under the CURRENT era.

    Source semantics ARE part of what a raw object means: the content_manifest
    stored beneath the hash already recorded provider/endpoint, so two different
    endpoints returning identical rows would have collided on one identity while
    carrying different manifests — identity narrower than the content stored
    under it. Retrieval metadata stays out, so an identical refetch still dedupes.

    Historical eras are NOT reachable from here by design: new objects are only
    ever minted under the current era. Verification of older objects goes
    through `identity.attribute`.
    """
    return ID.CURRENT_RAW_ERA.compute(
        rows, {"symbol": symbol, "timeframe": timeframe,
               "provider": provider, "endpoint": endpoint})


def raw_content_manifest(rows: Any, *, symbol: str, timeframe: str,
                         provider: str, endpoint: str, identity: str) -> dict:
    """The envelope stored beside a raw payload, declaring its identity era."""
    return {
        "schema_version": SCHEMA_VERSION,
        "storage_schema": ID.STORAGE_SCHEMA,
        "content_schema": ID.CONTENT_SCHEMA_RAW,
        "identity_schema": ID.CURRENT_RAW_ERA.schema_id,
        "provider": provider,
        "endpoint": endpoint,
        "symbol": symbol,
        "timeframe": timeframe,
        "row_count": len(rows) if isinstance(rows, list) else 0,
        "raw_content_fingerprint": identity,
    }


def canonical_content_manifest(bars: Sequence[dict], *, identity: str,
                               timeframe: str, adjustment_state: str) -> dict:
    """The envelope stored beside canonical bars, declaring its identity era."""
    return {
        "schema_version": SCHEMA_VERSION,
        "storage_schema": ID.STORAGE_SCHEMA,
        "content_schema": ID.CONTENT_SCHEMA_CANONICAL,
        "identity_schema": ID.CURRENT_CANONICAL_ERA.schema_id,
        "dataset_fingerprint": identity,
        "timeframe": timeframe,
        "adjustment_state": adjustment_state,
        "bar_count": len(bars),
    }


def write_snapshot(kind: str, identity: str, files: dict[str, Any], *,
                   root: str = ".") -> Path:
    """Write an immutable snapshot directory, or verify-and-reuse an existing one.

    DIRECTORY-ATOMIC: all files are written into a temporary directory and then
    renamed into place, so a crash mid-write cannot leave a half-valid snapshot
    that later verifies as real.

    Raises SnapshotCollisionError when the identity exists with different
    content. Only files belonging to this identity are compared — event objects
    live in their own namespace precisely so their timestamps cannot look like
    corruption of a content object.
    """
    if kind in CONTENT_KINDS or kind == DATASET_MANIFESTS:
        files = {k: strip_volatile(v) for k, v in files.items()}
    base = intraday_root(root) / kind
    target = base / identity
    if target.exists():
        for name, payload in files.items():
            existing = target / name
            if not existing.exists():
                raise SnapshotCollisionError(
                    f"{kind}/{identity} exists but is missing {name} — the "
                    f"snapshot is incomplete and cannot be trusted")
            if not _same_persisted_content(existing, payload):
                raise SnapshotCollisionError(
                    f"{kind}/{identity} already exists with DIFFERENT content in "
                    f"{name}. The fingerprint no longer describes the data; "
                    f"refusing to overwrite an immutable snapshot")
        return target        # identical -> reuse

    base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=str(base), prefix=".staging-"))
    try:
        for name, payload in files.items():
            _atomic_write(staging / name, _canonical_json(payload))
        try:
            os.rename(str(staging), str(target))
        except OSError:
            # Another process won the race; fall back to verify-and-reuse.
            import shutil
            shutil.rmtree(staging, ignore_errors=True)
            return write_snapshot(kind, identity, files, root=root)
    except BaseException:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target


# Keys that may be ADDED to an already-written immutable object without that
# being a content divergence. Two kinds qualify, and only these two:
#
#   * schema LABELS (`storage_schema`, `content_schema`, `identity_schema`) —
#     they describe how the object is addressed, not what it contains;
#   * `calendar_identity` — a DISCLOSURE of an input the manifest fingerprint
#     already commits to. Two manifests sharing an identity necessarily shared
#     a calendar identity, because it is hashed into that identity. So a
#     difference here cannot represent divergent content; it can only mean one
#     object was written before we started disclosing it.
#
# Objects written before these existed legitimately lack them. Tolerating their
# ABSENCE lets a pre-declaration snapshot be verify-and-reused instead of
# raising a false collision. Tolerating a DISAGREEMENT would be a real hole, so
# it is not tolerated: every key the stored object actually has must still match
# exactly. Same reasoning as `strip_volatile`, one level up.
ADDITIVE_DISCLOSURE_KEYS = frozenset({
    "storage_schema", "content_schema", "identity_schema", "calendar_identity",
})

# Keys whose VALUE may legitimately differ between two writes of the same
# identity, because they are prose REGENERATED from current code state rather
# than facts about the stored data. `limitations` is the only one: it restates
# the calendar coverage and admission rules in force at build time, so improving
# that wording changed the bytes of manifests whose data was identical, and the
# collision guard correctly refused the rerun.
#
# Deliberately narrow. The guard exists to catch "same identity, different
# DATA", and prose about the build environment is not data — it is the same
# reasoning as `strip_volatile`, one level up. The cleaner long-term home for
# derived disclosure is outside the content object entirely; that is a storage
# change, not a gate-integrity fix, so it is left as a follow-up.
DERIVED_DISCLOSURE_KEYS = frozenset({"limitations"})


def _same_persisted_content(existing: Path, payload: Any) -> bool:
    """Byte-identical, or differing ONLY by newly-added disclosure keys."""
    text = existing.read_text(encoding="utf-8")
    if text == _canonical_json(payload):
        return True
    try:
        stored = json.loads(text)
        # Round-trip so both sides have identical JSON typing (the writer
        # coerces via default=str; comparing raw Python objects would report
        # false differences for dates, tuples and the like).
        incoming = json.loads(_canonical_json(payload))
    except Exception:
        return False
    if not isinstance(stored, dict) or not isinstance(incoming, dict):
        return False
    for key, value in stored.items():
        if key in DERIVED_DISCLOSURE_KEYS:
            continue            # regenerated prose, not a fact about the data
        if key not in incoming or incoming[key] != value:
            return False        # a key the object already had disagrees
    return set(incoming) - set(stored) <= (ADDITIVE_DISCLOSURE_KEYS
                                           | DERIVED_DISCLOSURE_KEYS)


def _attribution_to_result(kind: str, identity: str, att: dict,
                           **extra: Any) -> dict:
    """Shared shape for era-aware verification results.

    `verified` answers INTEGRITY only. `current_era` answers RESEARCH
    ELIGIBILITY. Callers that need eligibility must read `current_era` — a
    legacy object is honestly verified and deliberately not current.
    """
    state = att["state"]
    return {
        "verified": state in VERIFIED_STATES,
        "state": state,
        "current_era": state in CURRENT_RESEARCH_STATES,
        "identity_schema": att.get("identity_schema"),
        "current_identity_schema": att.get("current_identity_schema"),
        "current_identity": att.get("current_identity"),
        "migration_required": att.get("migration_required", False),
        "attribution": att.get("attribution"),
        "identity": identity,
        "reason": att.get("reason"),
        **extra,
    }


def verify_raw_content(identity: str, *, root: str = ".") -> dict:
    """Verify a raw object against the identity era that actually minted it."""
    payload = read_snapshot(RAW, identity, "payload.json", root=root)
    man = read_snapshot(RAW, identity, "content_manifest.json", root=root)
    if payload is None or man is None:
        return {"verified": False, "state": INTEGRITY_FAILURE, "current_era": False,
                "identity": identity, "migration_required": False,
                "reason": "missing payload or content_manifest"}
    if man.get("raw_content_fingerprint") != identity:
        return {"verified": False, "state": INTEGRITY_FAILURE, "current_era": False,
                "identity": identity, "migration_required": False,
                "reason": "content manifest declares a different fingerprint"}
    att = ID.attribute("raw", identity, payload, man)
    return _attribution_to_result("raw", identity, att,
                                  recomputed=att.get("recomputed"))


def verify_feature_snapshot(identity: str, *, root: str = ".") -> dict:
    """Recompute the feature fingerprint from persisted feature bytes."""
    from portfolio_automation.intraday_lab import features as _F

    rows = read_snapshot(FEATURES, identity, "features.json", root=root)
    man = read_snapshot(FEATURES, identity, "feature_content_manifest.json", root=root)
    if rows is None or man is None:
        return {"verified": False, "reason": "missing features or content manifest"}
    recomputed = _F.feature_fingerprint_from_rows(rows)
    ok = recomputed == identity == man.get("feature_fingerprint")
    return {
        "verified": ok, "recomputed": recomputed, "identity": identity,
        "source_dataset_fingerprint": man.get("source_dataset_fingerprint"),
        "source_dataset_manifest_fingerprint": man.get("source_dataset_manifest_fingerprint"),
        "observation_count": len(rows),
        "reason": None if ok else "persisted features do not hash to their identity",
    }


def read_snapshot(kind: str, identity: str, name: str, *, root: str = ".") -> Any:
    path = intraday_root(root) / kind / identity / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def snapshot_exists(kind: str, identity: str, *, root: str = ".") -> bool:
    return (intraday_root(root) / kind / identity).is_dir()


# Fields that DEFINE canonical content. `retrieved_at` is deliberately absent:
# it lives in the acquisition event. Serializing it into the content object was
# the same defect one level down -- identical observations refetched later
# produced byte-different canonical_bars.json under an identical fingerprint,
# so the second legitimate run raised a false SnapshotCollisionError.
CANONICAL_BAR_FIELDS = ("symbol", "timeframe", "bar_start_at", "bar_end_at",
                        "known_at", "open", "high", "low", "close", "volume",
                        "source", "source_endpoint", "adjustment_state")


# Volatile keys are stripped from every persisted immutable object. They are
# real audit facts, but they belong to EVENTS. Left inside a content-addressed
# object they make each rerun look like corruption of the previous one.
VOLATILE_KEYS = frozenset({"generated_at", "retrieved_at", "created_at"})


def strip_volatile(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {k: strip_volatile(v) for k, v in payload.items()
                if k not in VOLATILE_KEYS}
    if isinstance(payload, list):
        return [strip_volatile(v) for v in payload]
    return payload


def bars_to_rows(bars: Sequence[Any]) -> list[dict]:
    """Content-only serialization of canonical bars."""
    return [{k: v for k, v in b.to_dict().items() if k in CANONICAL_BAR_FIELDS}
            for b in bars]


def bars_from_rows(rows: Sequence[dict]) -> list[Any]:
    """Rehydrate IntradayBar objects from PERSISTED canonical rows.

    The exact inverse of `bars_to_rows`, used by identity migration so a legacy
    dataset can be re-expressed under the current identity WITHOUT refetching
    the provider — provider history can change, so a refetch would silently
    substitute different data for the archived evidence.

    `publication_delay` is recovered as `known_at - bar_end_at` rather than
    defaulted: known_at is now part of canonical identity, so assuming the
    default would produce a bar whose knowability differs from the persisted
    record and therefore a different (wrong) migrated identity.
    """
    from datetime import timedelta as _td
    from portfolio_automation.intraday_lab.models import IntradayBar

    out = []
    for r in rows:
        start = datetime.fromisoformat(r["bar_start_at"])
        end = datetime.fromisoformat(r["bar_end_at"])
        known = datetime.fromisoformat(r["known_at"])
        out.append(IntradayBar(
            symbol=r["symbol"], timeframe=r["timeframe"], bar_start_at=start,
            open=r["open"], high=r["high"], low=r["low"], close=r["close"],
            volume=r["volume"], source=r.get("source", "unknown"),
            source_endpoint=r.get("source_endpoint", ""),
            adjustment_state=r.get("adjustment_state", "unknown"),
            publication_delay=known - end))
    return out


def verify_canonical_snapshot(identity: str, *, root: str = ".") -> dict:
    """Verify canonical bars against the identity era that actually minted them.

    Integrity is recomputed from PERSISTED bytes, never inferred from the
    directory name. A snapshot minted under an OLDER era is reported as verified
    legacy evidence — not as tampering — but is not current-era, so it cannot
    satisfy the research gate until migrated.
    """
    bars = read_snapshot(DATASETS, identity, "canonical_bars.json", root=root)
    manifest = read_snapshot(DATASETS, identity, "content_manifest.json", root=root)
    if bars is None or manifest is None:
        return {"verified": False, "state": INTEGRITY_FAILURE, "current_era": False,
                "identity": identity, "migration_required": False,
                "reason": "missing canonical_bars or content_manifest"}
    if manifest.get("dataset_fingerprint") != identity:
        return {"verified": False, "state": INTEGRITY_FAILURE, "current_era": False,
                "identity": identity, "migration_required": False,
                "reason": "content manifest declares a different fingerprint"}
    att = ID.attribute("canonical", identity, bars, manifest)
    return _attribution_to_result("canonical", identity, att,
                                  declared=manifest.get("dataset_fingerprint"),
                                  recomputed=att.get("recomputed"),
                                  bar_count=len(bars))


# ── Event identity projections ─────────────────────────────────────────────
# ONE definition per event kind, used at mint time AND at verify time. They
# were previously inlined at the write site only, so `verify_acquisition_event`
# claimed in its docstring that "its id recomputes" while never recomputing
# anything — a verifier documented as stronger than it was. Sharing the
# projection makes the two impossible to drift apart.
#
# `retrieved_at` IS part of acquisition-event identity: an event records one
# retrieval, so two fetches of identical observations are two events (and one
# content object). That is the opposite of the CONTENT rule, deliberately.

def acquisition_event_identity(record: dict) -> str:
    """Identity of one provider call, from the fields that define the call."""
    return content_hash({
        "request_fingerprint": record.get("request_fingerprint"),
        "symbol": record.get("symbol"),
        "requested_start": record.get("requested_start"),
        "requested_end": record.get("requested_end"),
        "retrieved_at": record.get("retrieved_at"),
        "provider_status": record.get("provider_status"),
        "raw_content_fingerprint": record.get("raw_payload_hash"),
    })


def build_event_identity(manifest_fingerprint: str,
                         acquisition_event_ids: Sequence[str]) -> str:
    """Identity of one dataset build: which manifest, from which retrievals."""
    return content_hash({
        "manifest_fingerprint": manifest_fingerprint,
        "acquisition_event_ids": list(acquisition_event_ids),
    })


def verify_acquisition_event(event_id: str, *, root: str = ".") -> dict:
    """Recompute the event identity, then check its causal state and evidence."""
    ev = read_snapshot(RAW_EVENTS, event_id, "acquisition_event.json", root=root)
    if ev is None:
        return {"verified": False, "identity": event_id,
                "reason": "missing acquisition_event"}
    recomputed = acquisition_event_identity(ev)
    if recomputed != event_id:
        return {"verified": False, "identity": event_id, "recomputed": recomputed,
                "reason": "acquisition event does not recompute to its identity — "
                          "an identity-defining field was modified"}
    if ev.get("acquisition_event_id") not in (None, event_id):
        return {"verified": False, "identity": event_id,
                "reason": "acquisition event declares a different id"}
    raw_fp = ev.get("raw_payload_hash")
    if raw_fp and not verify_raw_content(raw_fp, root=root).get("verified"):
        return {"verified": False, "identity": event_id,
                "reason": f"referenced raw {raw_fp} fails verification"}
    # A provider failure legitimately has no payload, but must say why.
    if not raw_fp and not ev.get("error_code") and ev.get("provider_status") == "OK":
        return {"verified": False, "identity": event_id,
                "reason": "OK status with no raw content and no error"}
    return {"verified": True, "identity": event_id, "recomputed": recomputed,
            "provider_status": ev.get("provider_status"),
            "error_code": ev.get("error_code"),
            "raw_content_fingerprint": raw_fp, "reason": None}


def verify_build_event(event_id: str, *, root: str = ".") -> dict:
    """Recompute a dataset build event's identity from its own fields."""
    ev = read_snapshot(DATASET_EVENTS, event_id, "build_event.json", root=root)
    if ev is None:
        return {"verified": False, "identity": event_id,
                "reason": "missing build_event"}
    recomputed = build_event_identity(ev.get("manifest_fingerprint"),
                                      ev.get("acquisition_event_ids") or [])
    if recomputed != event_id:
        return {"verified": False, "identity": event_id, "recomputed": recomputed,
                "reason": "build event does not recompute to its identity — an "
                          "identity-defining field was modified"}
    return {"verified": True, "identity": event_id, "recomputed": recomputed,
            "manifest_fingerprint": ev.get("manifest_fingerprint"),
            "acquisition_event_ids": ev.get("acquisition_event_ids") or [],
            "reason": None}


# Reconciliation states that legitimately account for a requested item.
_ACCOUNTED_STATES = frozenset({
    "ADMITTED", "NOT_A_TRADING_SESSION", "REJECTED_CALENDAR_UNCERTIFIED",
    "REJECTED_PROVIDER_ERROR", "REJECTED_NORMALIZATION_ERROR",
    "REJECTED_MISSING_BARS", "REJECTED_SURPLUS_BARS", "REJECTED_OFF_GRID",
    "REJECTED_CONFLICTING_DUPLICATE", "REJECTED_EXACT_DUPLICATE",
    "REJECTED_IDENTITY_MISMATCH", "REJECTED_MIXED_ADJUSTMENT",
    "REJECTED_UNEXPECTED_PROVIDER_RESULT",
})


def verify_manifest_acquisitions(manifest_fingerprint: str, *, root: str = ".",
                                 require_evidence: bool = False) -> dict:
    """Verify the build and acquisition events behind a manifest.

    Acquisition event ids deliberately stay OUT of dataset identity — they
    legitimately differ between two runs that fetched identical observations,
    and folding them in once made an idempotent rerun look like corruption.
    They are still evidence, so they are verified through the per-run BUILD
    EVENT that references them rather than through the manifest's identity.

    `require_evidence` encodes the one place context legitimately changes the
    standard. A CURRENT graduation manifest claims governed real-data
    acquisition, so "no build event found" must fail — otherwise a manifest with
    no acquisition lineage at all would pass by vacuous truth. LEGACY archival
    objects predate build events entirely and must not be failed for missing
    infrastructure that did not exist when they were written.
    """
    base = intraday_root(root) / DATASET_EVENTS
    events, checked, bad = [], 0, []
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            ev = read_snapshot(DATASET_EVENTS, d.name, "build_event.json", root=root)
            if not ev or ev.get("manifest_fingerprint") != manifest_fingerprint:
                continue
            bv = verify_build_event(d.name, root=root)
            events.append(d.name)
            if not bv.get("verified"):
                bad.append({"build_event_id": d.name, "reason": bv.get("reason")})
                continue
            for aid in bv["acquisition_event_ids"]:
                if not aid:
                    continue
                checked += 1
                v = verify_acquisition_event(aid, root=root)
                if not v.get("verified"):
                    bad.append({"acquisition_event_id": aid, "reason": v.get("reason")})

    reason = None
    if bad:
        reason = f"{len(bad)} event(s) failed verification"
    elif require_evidence and not events:
        reason = ("no dataset build event references this manifest, so its "
                  "acquisition lineage cannot be verified — a current "
                  "graduation manifest must carry real acquisition evidence")
    elif require_evidence and checked == 0:
        reason = ("build event(s) present but reference no acquisition events, "
                  "so no provider call is evidenced for this manifest")
    return {"verified": reason is None, "build_events": events,
            "acquisitions": checked, "failures": bad, "reason": reason}


def _requested_matrix(req: dict) -> set[tuple[str, str]] | None:
    """The exact (symbol, market_date) matrix a request asked for.

    Deliberately CALENDAR-INDEPENDENT: it is the cartesian product of the
    requested symbols and every calendar date in [start, end]. Weekends,
    holidays and uncertified dates are all recorded as accounting states rather
    than dropped, so the matrix does not depend on any exchange calendar — which
    is what lets an archived manifest be checked without consulting today's.
    """
    try:
        symbols = list(req["symbols"])
        start = date.fromisoformat(req["start"])
        end = date.fromisoformat(req["end"])
    except Exception:
        return None
    if not symbols or end < start:
        return None
    out: set[tuple[str, str]] = set()
    d = start
    while d <= end:
        for s in symbols:
            out.add((s, d.isoformat()))
        d += timedelta(days=1)
    return out


def _reconciliation_coherent(recs: list, req: dict) -> str | None:
    """Field-level agreement between reconciliation records and the request.

    Returns a failure reason, or None. Deliberately does NOT re-resolve sessions
    through the live calendar: an archived manifest must be checkable under the
    semantics it was built with, not today's.
    """
    symbols = set(req.get("symbols") or [])
    timeframe = req.get("timeframe")
    try:
        start = date.fromisoformat(req["start"])
        end = date.fromisoformat(req["end"])
    except Exception:
        return "request manifest has unusable start/end dates"
    for r in recs:
        if r.get("symbol") not in symbols:
            return f"reconciliation names symbol {r.get('symbol')!r}, not in the request"
        try:
            d = date.fromisoformat(r.get("market_date"))
        except Exception:
            return f"reconciliation has an unusable market_date {r.get('market_date')!r}"
        if not (start <= d <= end):
            return f"reconciliation date {d} lies outside the requested range"
        if timeframe and r.get("timeframe") not in (None, timeframe):
            return (f"reconciliation timeframe {r.get('timeframe')!r} disagrees "
                    f"with the requested {timeframe!r}")
    return None


def verify_dataset_provenance(manifest_fingerprint: str, *, root: str = ".") -> dict:
    """Walk the whole persisted graph. Evidence only — no caller claims.

        dataset manifest -> request -> calendar -> reconciliation
                         -> canonical content -> raw evidence

    Verified against the calendar identity PERSISTED WITH THIS MANIFEST, not the
    current calendar implementation: an old immutable manifest must not silently
    reinterpret itself under a future calendar change.
    """
    from portfolio_automation.intraday_lab.dataset import DatasetRequest

    def fail(reason: str, **extra) -> dict:
        return {"verified": False, "reason": reason,
                "manifest_fingerprint": manifest_fingerprint, **extra}

    man = read_snapshot(DATASET_MANIFESTS, manifest_fingerprint,
                        "dataset_manifest.json", root=root)
    req = read_snapshot(DATASET_MANIFESTS, manifest_fingerprint,
                        "request_manifest.json", root=root)
    recon = read_snapshot(DATASET_MANIFESTS, manifest_fingerprint,
                          "reconciliation.json", root=root)
    if man is None or req is None or recon is None:
        return fail("missing dataset_manifest, request_manifest or reconciliation")

    if man.get("manifest_fingerprint") != manifest_fingerprint:
        return fail("dataset manifest declares a different manifest fingerprint")

    # Request identity recomputes from persisted fields.
    try:
        from datetime import date as _date
        rebuilt = DatasetRequest(
            symbols=tuple(sorted(req["symbols"])),
            start=_date.fromisoformat(req["start"]),
            end=_date.fromisoformat(req["end"]),
            timeframe=req["timeframe"]).fingerprint()
    except Exception as exc:
        return fail(f"request manifest unusable: {type(exc).__name__}")
    if rebuilt != req.get("request_fingerprint"):
        return fail("persisted request fields do not recompute to its fingerprint")

    # Calendar identity belonging to THIS manifest.
    if not req.get("calendar_fingerprint"):
        return fail("no calendar identity persisted with the manifest")
    cal_identity = req.get("calendar_identity")
    if isinstance(cal_identity, dict) and cal_identity:
        from portfolio_automation.intraday_lab.dataset import calendar_fingerprint_of
        if calendar_fingerprint_of(cal_identity) != req["calendar_fingerprint"]:
            return fail("persisted calendar_identity does not hash to the "
                        "calendar_fingerprint stored beside it")

    # Reconciliation must equal the requested matrix EXACTLY — not merely cover
    # it. The matrix is the cartesian product of requested symbols and every
    # date in [start, end], which is CALENDAR-INDEPENDENT: weekends, holidays
    # and uncertified dates are all recorded as accounting states, so a count
    # comparison could be satisfied by the wrong items.
    unknown = {r.get("admission_status") for r in recon} - _ACCOUNTED_STATES
    if unknown:
        return fail(f"unrecognised reconciliation state(s): {sorted(unknown)}")

    requested = _requested_matrix(req)
    if requested is None:
        return fail("request manifest lacks the fields needed to reconstruct "
                    "the requested symbol-date matrix")
    # The persisted count is no longer what verification RELIES on, but it is
    # still a claim the manifest makes. Leaving it unchecked would make it a
    # field no consumer validates — the debt pattern that lets a stored number
    # drift away from the data it describes.
    declared_count = req.get("requested_symbol_date_count")
    if declared_count is not None and declared_count != len(requested):
        return fail(f"request manifest declares {declared_count} requested "
                    f"symbol-dates but its own symbols/date-range imply "
                    f"{len(requested)}")

    # Records for results OUTSIDE the authorized matrix are accounted separately
    # and must never be counted as requested coverage — otherwise a provider
    # returning an unrequested symbol could paper over a genuinely missing one.
    requested_recs = [r for r in recon
                      if r.get("admission_status") != "REJECTED_UNEXPECTED_PROVIDER_RESULT"]
    unexpected_recs = [r for r in recon
                       if r.get("admission_status") == "REJECTED_UNEXPECTED_PROVIDER_RESULT"]

    seen = [(r.get("symbol"), r.get("market_date")) for r in requested_recs]
    if len(seen) != len(set(seen)):
        dupes = sorted({s for s in seen if seen.count(s) > 1})
        return fail(f"duplicate reconciliation records for requested item(s): {dupes}")
    seen_set = set(seen)
    missing = sorted(requested - seen_set)
    extra = sorted(seen_set - requested)
    if missing:
        return fail(f"{len(missing)} requested symbol-date(s) have no "
                    f"reconciliation record — a requested session disappeared: "
                    f"{missing[:5]}")
    if extra:
        return fail(f"{len(extra)} reconciliation record(s) are not in the "
                    f"requested matrix: {extra[:5]}")
    intruder = sorted({(r.get("symbol"), r.get("market_date"))
                       for r in unexpected_recs} & requested)
    if intruder:
        return fail(f"unexpected-provider record(s) claim requested items: {intruder[:5]}")

    # Field-level coherence against the request itself.
    coherence = _reconciliation_coherent(requested_recs, req)
    if coherence:
        return fail(coherence)

    # Manifest identity must RECOMPUTE from persisted semantics, not merely
    # match its own directory name. Requires the calendar identity that this
    # manifest was built under — never the live calendar, which would let a
    # calendar change silently reinterpret archived research.
    manifest_recomputed = None
    if isinstance(cal_identity, dict) and cal_identity:
        from portfolio_automation.intraday_lab.dataset import (
            manifest_fingerprint_from_parts,
        )
        try:
            manifest_recomputed = manifest_fingerprint_from_parts(
                content_fingerprint=req.get("canonical_content_fingerprint"),
                request=man.get("request"), calendar=cal_identity,
                timeframe=man.get("timeframe"),
                adjustment_state=man.get("adjustment_state"),
                sessions=[[r.get("symbol"), r.get("market_date"),
                           r.get("admission_status")] for r in recon])
        except Exception as exc:
            return fail(f"manifest identity could not be recomputed: "
                        f"{type(exc).__name__}")
        if manifest_recomputed != manifest_fingerprint:
            return fail("persisted manifest semantics do not recompute to the "
                        "manifest identity — the manifest has been modified",
                        manifest_recomputed=manifest_recomputed)

    # Canonical content.
    content_fp = req.get("canonical_content_fingerprint")
    if not content_fp or content_fp != man.get("dataset_fingerprint"):
        return fail("manifest and request disagree on the canonical content id")
    canon = verify_canonical_snapshot(content_fp, root=root)
    if not canon.get("verified"):
        return fail(f"canonical content failed verification: {canon.get('reason')}",
                    canonical=canon, state=canon.get("state"))

    # Raw evidence referenced by the manifest.
    raw_results = {}
    for raw_fp in req.get("raw_content_fingerprints") or []:
        raw_results[raw_fp] = verify_raw_content(raw_fp, root=root)
        if not raw_results[raw_fp].get("verified"):
            return fail(f"referenced raw content {raw_fp} failed verification",
                        raw=raw_results)

    # INTEGRITY is now established. RESEARCH ELIGIBILITY is a separate question:
    # a graph is current-era only if EVERY object in it is. A dataset minted
    # under today's canonical identity but resting on raw evidence identified
    # under an older contract is not coherently current — migration remints the
    # manifest with the migrated raw ids, so this stays satisfiable.
    legacy_raw = sorted(fp for fp, r in raw_results.items()
                        if not r.get("current_era"))
    # A manifest whose identity cannot be RECOMPUTED from its own persisted
    # semantics is intact evidence but cannot prove what it means without
    # consulting the live calendar. That is exactly the reinterpretation the
    # persisted calendar identity exists to prevent, so it is honest legacy
    # evidence — never silently current.
    not_current = None
    if not canon.get("current_era"):
        not_current = "canonical content is not current-era"
    elif legacy_raw:
        not_current = f"raw evidence not current-era: {legacy_raw}"
    elif manifest_recomputed is None:
        not_current = ("no calendar_identity persisted with this manifest, so its "
                       "identity cannot be recomputed from its own evidence — "
                       "archival only")
    current_era = not_current is None
    return {"verified": True, "reason": None,
            "manifest_fingerprint": manifest_fingerprint,
            "manifest_recomputed": manifest_recomputed,
            "manifest_identity_recomputed": manifest_recomputed is not None,
            "canonical_content_fingerprint": content_fp,
            "request_fingerprint": rebuilt,
            "calendar_fingerprint": req.get("calendar_fingerprint"),
            "calendar_identity_verified": bool(
                isinstance(cal_identity, dict) and cal_identity),
            "requested_items": len(requested),
            "reconciled_items": len(seen),
            "unexpected_provider_records": len(unexpected_recs),
            "raw_verified": sorted(raw_results),
            "current_era": current_era,
            "canonical_state": canon.get("state"),
            "canonical_identity_schema": canon.get("identity_schema"),
            "legacy_raw_content": legacy_raw,
            "migration_required": bool(canon.get("migration_required") or legacy_raw),
            "not_current_reason": not_current}
