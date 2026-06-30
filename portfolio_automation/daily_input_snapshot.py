"""Phase 2 — immutable daily input snapshot.

Freezes a single point-in-time view of every input the daily run (production
AND simulations) depends on, so they all evaluate the SAME data and no daily
simulation can read later information (Iron rules 4, 5).

Design: **references + content hashes, not copies** (Iron-rule-friendly; the
mission explicitly allows this for large/immutable-for-the-run datasets). For
each declared source the snapshot records its path, observation timestamp,
available-as-of, freshness, quality, source label, and a sha256 of the file —
plus a single ``snapshot_hash`` over the valid inputs that is stable for
identical inputs (idempotent retries) and changes when a meaningful input
changes. Future-dated inputs are rejected (excluded from the coherent hash) so
look-ahead data cannot leak into the run's input identity.

Observe-only: building a snapshot reads artifacts and writes one sandbox
artifact. It mutates no decision, allocation, score, or portfolio state.
Pure except the injected ``now`` (callers supply it; deterministic for tests).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.next_stage.contracts import observe_only_envelope, lineage

SNAPSHOT_SCHEMA_VERSION = "1"
_SNAPSHOT_FILENAME = "daily_input_snapshot.json"
_PRODUCER = "daily_input_snapshot"

__all__ = [
    "InputSource", "INPUT_SOURCES", "build_input_snapshot", "write_input_snapshot",
    "read_input_snapshot", "load_input", "run_daily_input_snapshot",
    "SNAPSHOT_SCHEMA_VERSION",
]


@dataclass(frozen=True)
class InputSource:
    """One declared decision-time input (a reference, never a copy)."""
    key: str                 # stable accessor key, e.g. "holdings"
    path: str                # repo-relative artifact path
    kind: str                # holdings|cash|prices|regime|factors|news|crowd|overlays|config|strategy|source_health|decision
    source: str              # provenance label, e.g. "broker_overlay", "fmp_cache"
    stale_after_hours: float = 26.0   # freshness policy (daily inputs: ~1 day + slack)


# The declared decision-time input set. Best-effort references to the real
# artifacts; an absent source degrades to quality="missing" (honest), never a
# crash. Tuned for daily cadence; extend as later phases add inputs.
INPUT_SOURCES: list[InputSource] = [
    InputSource("holdings", "outputs/portfolio/broker_aware_portfolio.json", "holdings", "broker_overlay"),
    InputSource("portfolio_snapshot", "outputs/portfolio/portfolio_snapshot.json", "holdings", "allocation_advisory"),
    InputSource("decision_baseline", "outputs/latest/decision_plan.json", "decision", "decision_engine"),
    InputSource("decision_holdings_source", "outputs/latest/decision_holdings_source.json", "holdings", "holdings_resolver"),
    InputSource("news", "outputs/latest/news_intelligence.json", "news", "news_intelligence"),
    InputSource("crowd_unified", "outputs/latest/unified_crowd_intelligence.json", "crowd", "unified_crowd_bus"),
    InputSource("regime", "outputs/regime/regime_performance.json", "regime", "regime_performance", stale_after_hours=720.0),
    InputSource("factors", "data/factors/ff_monthly.csv", "factors", "fama_french", stale_after_hours=2160.0),
    InputSource("source_health", "outputs/latest/data_budget_status.json", "source_health", "data_budget"),
    InputSource("active_overlays", "outputs/promotion_approvals/production_application_state.json", "overlays", "sim_governance", stale_after_hours=8760.0),
    InputSource("prod_config", "config.json", "config", "config_file", stale_after_hours=8760.0),
    InputSource("strategy_config", "outputs/sandbox/strategy_profiles.json", "strategy", "strategy_profiles", stale_after_hours=720.0),
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _observation_ts(path: Path, raw: bytes) -> str | None:
    """Prefer the payload's ``generated_at``; fall back to file mtime (UTC)."""
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            for k in ("generated_at", "data_as_of", "signal_date", "generated"):
                v = obj.get(k)
                if isinstance(v, str) and _parse_iso(v):
                    return v
    except Exception:
        pass
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return mtime.isoformat()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def _evaluate_source(root: Path, src: InputSource, now_dt: datetime,
                     available_as_of: str) -> dict[str, Any]:
    path = root / src.path
    base = {
        "key": src.key, "kind": src.kind, "source": src.source, "path": src.path,
        "available_as_of": available_as_of,
    }
    if not path.exists():
        return {**base, "present": False, "observation_timestamp": None,
                "freshness": "missing", "age_hours": None, "quality": "missing",
                "content_hash": None}
    try:
        raw = path.read_bytes()
    except Exception:
        return {**base, "present": False, "observation_timestamp": None,
                "freshness": "missing", "age_hours": None, "quality": "missing",
                "content_hash": None}

    obs = _observation_ts(path, raw)
    obs_dt = _parse_iso(obs)
    content_hash = _hash_bytes(raw)
    age_hours = None
    if obs_dt is not None:
        age_hours = round((now_dt - obs_dt).total_seconds() / 3600.0, 4)

    if obs_dt is not None and obs_dt > now_dt:
        # Look-ahead guard: an input observed AFTER the run's as-of is rejected.
        quality = freshness = "invalid_future"
    elif age_hours is not None and age_hours > src.stale_after_hours:
        quality = freshness = "stale"
    else:
        quality, freshness = "ok", "fresh"

    return {**base, "present": True, "observation_timestamp": obs,
            "freshness": freshness, "age_hours": age_hours, "quality": quality,
            "content_hash": content_hash}


def build_input_snapshot(
    root: Path | str,
    *,
    run_id: str,
    data_as_of: str,
    now: str,
    source_commit: str = "unknown",
    config_hash: str = "unknown",
    sources: list[InputSource] | None = None,
) -> dict[str, Any]:
    """Build the immutable input snapshot (no file writes).

    ``now`` is the run's as-of for freshness/future checks; ``data_as_of`` is the
    point-in-time the inputs represent (usually == now). Pure for fixed inputs.
    """
    root = Path(root)
    now_dt = _parse_iso(now) or datetime.now(timezone.utc)
    srcs = sources if sources is not None else INPUT_SOURCES

    records = [_evaluate_source(root, s, now_dt, data_as_of) for s in srcs]

    # snapshot_hash over the VALID inputs only (ok+stale): future/missing inputs
    # cannot leak into the run's coherent input identity. Sorted -> deterministic.
    valid = [r for r in records if r["quality"] in ("ok", "stale")]
    hash_material = "|".join(f"{r['key']}={r['content_hash']}"
                             for r in sorted(valid, key=lambda r: r["key"]))
    snapshot_hash = _hash_bytes(hash_material.encode("utf-8"))

    env = observe_only_envelope(now, **lineage(
        run_id=run_id, data_as_of=data_as_of, producer=_PRODUCER,
        source_commit=source_commit, config_hash=config_hash,
        upstream_refs=[r["path"] for r in records if r["present"]]))

    return {
        **env,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "artifact_type": "daily_input_snapshot",
        "data_as_of": data_as_of,
        "inputs": records,
        "snapshot_hash": snapshot_hash,
        "input_count": len(records),
        "valid_count": len(valid),
        "stale_count": sum(1 for r in records if r["quality"] == "stale"),
        "missing_count": sum(1 for r in records if r["quality"] == "missing"),
        "future_rejected_count": sum(1 for r in records if r["quality"] == "invalid_future"),
    }


# ---------------------------------------------------------------------------
# persistence + accessors (the single frozen source for consumers)
# ---------------------------------------------------------------------------


def write_input_snapshot(root: Path | str, snapshot: dict[str, Any]) -> Path:
    base = str(Path(root) / "outputs")
    return safe_write_json(OutputNamespace.SANDBOX, _SNAPSHOT_FILENAME,
                           snapshot, base_dir=base)


def read_input_snapshot(root: Path | str) -> dict[str, Any] | None:
    path = Path(root) / "outputs" / "sandbox" / _SNAPSHOT_FILENAME
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_input(snapshot: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
    """Accessor for consumers (production + every shadow sim use this)."""
    if not snapshot:
        return None
    for rec in snapshot.get("inputs") or []:
        if rec.get("key") == key:
            return rec
    return None


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def run_daily_input_snapshot(root: Path | str = ".", now: str | None = None) -> dict[str, Any]:
    """Build + persist today's snapshot, inheriting run_id/data_as_of/provenance
    from the Phase 1 run manifest when present (degrades to ``now`` otherwise).
    Never raises."""
    from portfolio_automation.run_manifest import read_manifest  # local: acyclic
    root = Path(root)
    now = now or datetime.now(timezone.utc).isoformat()
    manifest = read_manifest(root) or {}
    run_id = manifest.get("run_id") or f"{now[:10]}_daily_official"
    data_as_of = manifest.get("data_as_of") or now
    snap = build_input_snapshot(
        root, run_id=run_id, data_as_of=data_as_of, now=now,
        source_commit=manifest.get("source_commit", "unknown"),
        config_hash=manifest.get("config_hash", "unknown"))
    try:
        write_input_snapshot(root, snap)
    except Exception:
        pass
    return snap
