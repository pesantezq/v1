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

RAW = "raw"
DATASETS = "datasets"
FEATURES = "features"


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

    Raises SnapshotCollisionError when the identity exists with different
    content.
    """
    target = intraday_root(root) / kind / identity
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
    for name, payload in files.items():
        _atomic_write(target / name, _canonical_json(payload))
    return target


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


def bars_to_rows(bars: Sequence[Any]) -> list[dict]:
    return [b.to_dict() for b in bars]


def verify_canonical_snapshot(identity: str, *, root: str = ".") -> dict:
    """Recompute identity from PERSISTED bytes rather than trusting the path.

    A snapshot whose stored bars no longer hash to their own directory name has
    been tampered with or written by a different schema; readiness must not be
    inferred from the filename.
    """
    bars = read_snapshot(DATASETS, identity, "canonical_bars.json", root=root)
    manifest = read_snapshot(DATASETS, identity, "dataset_manifest.json", root=root)
    if bars is None or manifest is None:
        return {"verified": False, "reason": "missing canonical_bars or manifest"}

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
