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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

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
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)


def content_hash(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:32]


def raw_payload_hash(rows: Any, *, symbol: str, timeframe: str) -> str:
    """Identity of the provider's OBSERVATIONS, excluding retrieval metadata."""
    return content_hash({"schema": "intraday_raw_v1", "symbol": symbol,
                         "timeframe": timeframe, "rows": rows})


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
            if existing.read_text(encoding="utf-8") != _canonical_json(payload):
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


def verify_raw_content(identity: str, *, root: str = ".") -> dict:
    """Recompute raw identity from persisted observations."""
    payload = read_snapshot(RAW, identity, "payload.json", root=root)
    man = read_snapshot(RAW, identity, "content_manifest.json", root=root)
    if payload is None or man is None:
        return {"verified": False, "reason": "missing payload or content_manifest"}
    recomputed = raw_payload_hash(payload, symbol=man.get("symbol"),
                                  timeframe=man.get("timeframe"))
    ok = recomputed == identity == man.get("raw_content_fingerprint")
    return {"verified": ok, "recomputed": recomputed, "identity": identity,
            "reason": None if ok else "persisted payload does not hash to its identity"}


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


def verify_canonical_snapshot(identity: str, *, root: str = ".") -> dict:
    """Recompute identity from PERSISTED bytes rather than trusting the path.

    A snapshot whose stored bars no longer hash to their own directory name has
    been tampered with or written by a different schema; readiness must not be
    inferred from the filename.
    """
    bars = read_snapshot(DATASETS, identity, "canonical_bars.json", root=root)
    manifest = read_snapshot(DATASETS, identity, "content_manifest.json", root=root)
    if bars is None or manifest is None:
        return {"verified": False, "reason": "missing canonical_bars or content_manifest"}

    recomputed = content_hash({
        "schema": "intraday_canonical_v2",
        "timeframe": manifest.get("timeframe"),
        "adjustment_state": manifest.get("adjustment_state"),
        "rows": sorted([[r["symbol"], r["timeframe"], r["bar_start_at"],
                         r["open"], r["high"], r["low"], r["close"], r["volume"]]
                        for r in bars], key=lambda x: (x[0], x[1], x[2])),
    })
    declared = manifest.get("dataset_fingerprint")
    return {
        "verified": recomputed == declared == identity,
        "recomputed": recomputed, "declared": declared, "identity": identity,
        "bar_count": len(bars),
        "reason": None if recomputed == declared == identity
        else "persisted bytes do not hash to the declared identity",
    }
