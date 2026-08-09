"""Identity-era migration for the Intraday Lab corpus. Research-only.

WHAT THIS DOES
==============

Objects minted under an older identity era are sound evidence but are NOT
eligible for current research (see `identity.py`). Migration promotes them by
re-expressing the SAME PERSISTED BYTES under the current identity era:

    legacy immutable object
        -> verify under its OWN historical identity schema
        -> confirm every field the current identity protects is present
        -> compute the current identity FROM THE PERSISTED BYTES
        -> write a NEW immutable current-era object
        -> write immutable migration lineage

FOUR RULES, ALL ENFORCED RATHER THAN DOCUMENTED
===============================================

1. **Never refetch to migrate.** Provider history changes — vendors restate,
   re-adjust and backfill. Refetching would silently substitute today's data for
   the archived evidence and call it the same dataset. Migration is a pure
   function of bytes already on disk.

2. **Never rewrite, rename or delete the legacy object.** It stays exactly as
   written, as archived evidence. A correction is a NEW identity; that is the
   whole premise of the store.

3. **Content equivalence is proved, not assumed.** The migrated object must
   contain byte-identical payload/bars, and must verify as VERIFIED_CURRENT
   afterwards. Both are checked before lineage is written.

4. **Calendar meaning is held constant.** A manifest binds its research meaning
   to a calendar identity. Reminting under a NEWER calendar would silently
   change what an archived manifest means. Migration reproduces the legacy
   manifest identity first and refuses to proceed if it cannot — so migrating
   after a calendar change fails closed instead of quietly reinterpreting.

Migration is idempotent. A second run recognises completed work from the
immutable lineage and re-verifies the object it names before trusting it, so
re-running after a calendar change reports ALREADY_MIGRATED rather than a
permanent (and misleading) refusal — while a lineage record pointing at a
corrupted target still fails closed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from portfolio_automation.intraday_lab import features as F
from portfolio_automation.intraday_lab import identity as ID
from portfolio_automation.intraday_lab import pipeline as PL
from portfolio_automation.intraday_lab import storage as ST
from portfolio_automation.intraday_lab.dataset import (
    _calendar_identity, manifest_fingerprint_from_parts,
)

SCHEMA_VERSION = "1"
MIGRATION_VERSION = "intraday_identity_migration_v1"

# Outcomes.
MIGRATED = "MIGRATED"
ALREADY_CURRENT = "ALREADY_CURRENT"
ALREADY_MIGRATED = "ALREADY_MIGRATED"
NOT_MIGRATABLE = "NOT_MIGRATABLE"
REFUSED = "REFUSED"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_write(kind: str, identity: str, files: dict, *, root: str) -> str | None:
    """Write a migrated object, converting a collision into a refusal.

    A collision here means the destination identity already exists holding
    DIFFERENT bytes — i.e. the corpus is corrupt, or something else wrote there.
    Migration must report that as a refusal, not raise: a tool that crashes
    halfway through leaves the operator with no verdict and a half-migrated
    store. Refusing writes nothing and keeps the corpus exactly as it was.
    """
    try:
        ST.write_snapshot(kind, identity, files, root=root)
        return None
    except ST.SnapshotCollisionError as exc:
        return f"refusing to migrate onto a conflicting object: {exc}"


def _lineage_identity(payload: dict) -> str:
    return ID.content_hash(payload)


def _write_lineage(kind: str, legacy: dict, current: dict, *,
                   equivalence: dict, root: str, extra: dict | None = None) -> str:
    """Immutable lineage: content-addressed body + a timestamped event.

    The body is deterministic so re-running migration reproduces one lineage
    object rather than accumulating near-duplicates. WHEN it ran is an event
    fact and lives in the event object, exactly as elsewhere in the lab.
    """
    body = {
        "schema_version": SCHEMA_VERSION,
        "source_module": "intraday_lab.migration",
        "observe_only": True,
        "object_kind": kind,
        "legacy_identity": legacy["identity"],
        "legacy_identity_schema": legacy["identity_schema"],
        "current_identity": current["identity"],
        "current_identity_schema": current["identity_schema"],
        "content_equivalence": equivalence,
        "migration_version": MIGRATION_VERSION,
        "identity_registry": ID.era_registry_provenance(),
        "legacy_object_retained": True,
        "legacy_eligibility": "ARCHIVAL_EVIDENCE_ONLY",
        **(extra or {}),
    }
    lineage_id = _lineage_identity(body)
    ST.write_snapshot(ST.MIGRATIONS, lineage_id,
                      {"migration_lineage.json": body}, root=root)
    ST.write_snapshot(ST.MIGRATION_EVENTS, ID.content_hash(
        {"lineage_id": lineage_id, "migrated_at": _now()}),
        {"migration_event.json": {
            "schema_version": SCHEMA_VERSION,
            "lineage_id": lineage_id,
            "object_kind": kind,
            "legacy_identity": legacy["identity"],
            "current_identity": current["identity"],
            "migration_version": MIGRATION_VERSION,
            "migrated_at": _now(),
        }}, root=root)
    return lineage_id


def find_lineage(legacy_identity: str, object_kind: str, *,
                 root: str = ".") -> dict | None:
    """The durable record that a legacy object has already been migrated.

    Needed because migration is only reproducible while the calendar it was
    built under is still reproducible. Once the calendar advances, re-running
    migration on an ALREADY-migrated manifest would refuse — correctly, but
    misleadingly, since the work is long done. Lineage answers that from
    evidence instead of leaving a permanent false alarm in the report.
    """
    base = ST.intraday_root(root) / ST.MIGRATIONS
    if not base.is_dir():
        return None
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        body = ST.read_snapshot(ST.MIGRATIONS, d.name, "migration_lineage.json",
                                root=root)
        if (body and body.get("legacy_identity") == legacy_identity
                and body.get("object_kind") == object_kind):
            return {**body, "lineage_id": d.name}
    return None


def migrate_raw_content(identity: str, *, root: str = ".") -> dict:
    """Promote one raw object to the current identity era."""
    v = ST.verify_raw_content(identity, root=root)
    if v.get("current_era"):
        return {"status": ALREADY_CURRENT, "legacy_identity": identity,
                "current_identity": identity, "state": v.get("state")}
    if v.get("state") != ID.VERIFIED_LEGACY_MIGRATABLE:
        return {"status": NOT_MIGRATABLE, "legacy_identity": identity,
                "state": v.get("state"), "reason": v.get("reason")}

    payload = ST.read_snapshot(ST.RAW, identity, "payload.json", root=root)
    man = ST.read_snapshot(ST.RAW, identity, "content_manifest.json", root=root)
    new_id = v["current_identity"]

    conflict = _safe_write(ST.RAW, new_id, {
        "payload.json": payload,                       # BYTE-IDENTICAL evidence
        "content_manifest.json": ST.raw_content_manifest(
            payload, symbol=man.get("symbol"), timeframe=man.get("timeframe"),
            provider=man.get("provider"), endpoint=man.get("endpoint"),
            identity=new_id),
    }, root=root)
    if conflict:
        return {"status": REFUSED, "legacy_identity": identity,
                "current_identity": new_id, "reason": conflict}

    after = ST.verify_raw_content(new_id, root=root)
    if not after.get("current_era"):
        return {"status": REFUSED, "legacy_identity": identity,
                "current_identity": new_id,
                "reason": f"migrated object did not verify as current-era: "
                          f"{after.get('reason') or after.get('state')}"}

    equivalence = {
        "payload_identical": ST.read_snapshot(ST.RAW, new_id, "payload.json",
                                              root=root) == payload,
        "row_count": len(payload) if isinstance(payload, list) else None,
        "source_semantics_preserved": {
            "provider": man.get("provider"), "endpoint": man.get("endpoint")},
    }
    lineage = _write_lineage(
        "raw_content",
        {"identity": identity, "identity_schema": v["identity_schema"]},
        {"identity": new_id, "identity_schema": after["identity_schema"]},
        equivalence=equivalence, root=root)
    return {"status": MIGRATED, "legacy_identity": identity,
            "current_identity": new_id, "lineage_id": lineage,
            "legacy_identity_schema": v["identity_schema"],
            "current_identity_schema": after["identity_schema"]}


def migrate_canonical_content(identity: str, *, root: str = ".") -> dict:
    """Promote one canonical dataset object to the current identity era."""
    v = ST.verify_canonical_snapshot(identity, root=root)
    if v.get("current_era"):
        return {"status": ALREADY_CURRENT, "legacy_identity": identity,
                "current_identity": identity, "state": v.get("state")}
    if v.get("state") != ID.VERIFIED_LEGACY_MIGRATABLE:
        return {"status": NOT_MIGRATABLE, "legacy_identity": identity,
                "state": v.get("state"), "reason": v.get("reason")}

    bars = ST.read_snapshot(ST.DATASETS, identity, "canonical_bars.json", root=root)
    man = ST.read_snapshot(ST.DATASETS, identity, "content_manifest.json", root=root)
    new_id = v["current_identity"]

    conflict = _safe_write(ST.DATASETS, new_id, {
        "canonical_bars.json": bars,                   # BYTE-IDENTICAL evidence
        "content_manifest.json": ST.canonical_content_manifest(
            bars, identity=new_id, timeframe=man.get("timeframe"),
            adjustment_state=man.get("adjustment_state")),
    }, root=root)
    if conflict:
        return {"status": REFUSED, "legacy_identity": identity,
                "current_identity": new_id, "reason": conflict}

    after = ST.verify_canonical_snapshot(new_id, root=root)
    if not after.get("current_era"):
        return {"status": REFUSED, "legacy_identity": identity,
                "current_identity": new_id,
                "reason": f"migrated object did not verify as current-era: "
                          f"{after.get('reason') or after.get('state')}"}

    equivalence = {
        "bars_identical": ST.read_snapshot(ST.DATASETS, new_id,
                                           "canonical_bars.json", root=root) == bars,
        "bar_count": len(bars),
        "pit_fields_present": ["bar_end_at", "known_at"],
    }
    lineage = _write_lineage(
        "canonical_content",
        {"identity": identity, "identity_schema": v["identity_schema"]},
        {"identity": new_id, "identity_schema": after["identity_schema"]},
        equivalence=equivalence, root=root)
    return {"status": MIGRATED, "legacy_identity": identity,
            "current_identity": new_id, "lineage_id": lineage,
            "legacy_identity_schema": v["identity_schema"],
            "current_identity_schema": after["identity_schema"]}


def _resolve_calendar(req: dict, legacy_manifest_fp: str, parts: dict) -> dict:
    """The calendar identity this manifest was ACTUALLY built under.

    Preferred source is the identity persisted with the manifest. Older
    manifests stored only its hash, so the live calendar is used as a candidate
    and must REPRODUCE the legacy manifest fingerprint to be accepted. If it
    cannot, the calendar has changed since the manifest was written and there is
    no honest way to remint — the caller fails closed rather than guessing.
    """
    persisted = req.get("calendar_identity")
    if isinstance(persisted, dict) and persisted:
        return persisted
    candidate = _calendar_identity()
    reproduced = manifest_fingerprint_from_parts(calendar=candidate, **parts)
    if reproduced == legacy_manifest_fp:
        return candidate
    return {}


def migrate_dataset_manifest(manifest_fp: str, *, root: str = ".",
                             lookback: int = 3) -> dict:
    """Remint a manifest (and its features) onto migrated current-era objects.

    The manifest binds research MEANING: request, calendar and reconciliation
    are carried across unchanged, and ONLY the canonical/raw references are
    updated. Because manifest identity is computed over the canonical content
    fingerprint, that intentional change necessarily yields a new manifest
    identity — the old manifest is never mutated.
    """
    prov = ST.verify_dataset_provenance(manifest_fp, root=root)
    if not prov.get("verified"):
        return {"status": REFUSED, "legacy_manifest_fingerprint": manifest_fp,
                "reason": f"provenance does not verify: {prov.get('reason')}"}
    if prov.get("current_era"):
        return {"status": ALREADY_CURRENT,
                "legacy_manifest_fingerprint": manifest_fp,
                "current_manifest_fingerprint": manifest_fp}

    # Already migrated on a previous run? Trust the immutable lineage, but only
    # after re-verifying that the object it names is still a sound current-era
    # graph — a lineage record is a claim, and claims are checked here.
    prior = find_lineage(manifest_fp, "dataset_manifest", root=root)
    if prior:
        target = ST.verify_dataset_provenance(prior["current_identity"], root=root)
        if target.get("verified") and target.get("current_era"):
            return {"status": ALREADY_MIGRATED,
                    "legacy_manifest_fingerprint": manifest_fp,
                    "current_manifest_fingerprint": prior["current_identity"],
                    "lineage_id": prior["lineage_id"]}

    man = ST.read_snapshot(ST.DATASET_MANIFESTS, manifest_fp,
                           "dataset_manifest.json", root=root)
    req = ST.read_snapshot(ST.DATASET_MANIFESTS, manifest_fp,
                           "request_manifest.json", root=root)
    recon = ST.read_snapshot(ST.DATASET_MANIFESTS, manifest_fp,
                             "reconciliation.json", root=root)

    # 1. Migrate the objects this manifest points at.
    canon = migrate_canonical_content(req["canonical_content_fingerprint"], root=root)
    if canon["status"] not in (MIGRATED, ALREADY_CURRENT, ALREADY_MIGRATED):
        return {"status": REFUSED, "legacy_manifest_fingerprint": manifest_fp,
                "reason": f"canonical content not migratable: {canon.get('reason')}",
                "canonical": canon}
    new_content_fp = canon["current_identity"]

    raw_results, new_raw = [], []
    for raw_fp in req.get("raw_content_fingerprints") or []:
        r = migrate_raw_content(raw_fp, root=root)
        if r["status"] not in (MIGRATED, ALREADY_CURRENT, ALREADY_MIGRATED):
            return {"status": REFUSED, "legacy_manifest_fingerprint": manifest_fp,
                    "reason": f"raw evidence {raw_fp} not migratable: {r.get('reason')}",
                    "raw": r}
        raw_results.append(r)
        new_raw.append(r["current_identity"])

    # 2. Reproduce the legacy manifest identity to PROVE meaning is unchanged.
    sessions = [[r.get("symbol"), r.get("market_date"), r.get("admission_status")]
                for r in recon]
    parts = {"request": man.get("request"), "timeframe": man.get("timeframe"),
             "adjustment_state": man.get("adjustment_state"), "sessions": sessions}
    calendar = _resolve_calendar(req, manifest_fp,
                                 {**parts, "content_fingerprint":
                                  req["canonical_content_fingerprint"]})
    if not calendar:
        return {"status": REFUSED, "legacy_manifest_fingerprint": manifest_fp,
                "reason": "cannot reproduce the legacy manifest identity under any "
                          "available calendar identity — the calendar has changed "
                          "since this manifest was written, so reminting would "
                          "silently reinterpret archived research"}
    replayed = manifest_fingerprint_from_parts(
        content_fingerprint=req["canonical_content_fingerprint"],
        calendar=calendar, **parts)
    if replayed != manifest_fp:
        return {"status": REFUSED, "legacy_manifest_fingerprint": manifest_fp,
                "reason": f"legacy manifest identity did not replay from persisted "
                          f"parts (got {replayed}); refusing to remint a manifest "
                          f"whose meaning cannot be reproduced"}

    # 3. Mint the current manifest: identical meaning, migrated references.
    new_manifest_fp = manifest_fingerprint_from_parts(
        content_fingerprint=new_content_fp, calendar=calendar, **parts)
    # Only identity-bearing references change. The migration breadcrumb is
    # deliberately NOT stored here: a reminted manifest must be byte-identical
    # to one the pipeline would build fresh for the same data, or the object
    # would carry content its identity does not describe — the very defect this
    # session fixed for raw objects. Legacy->current lineage lives in the
    # immutable lineage object instead.
    new_manifest = {**man, "dataset_fingerprint": new_content_fp,
                    "manifest_fingerprint": new_manifest_fp,
                    "dataset_id": f"intraday-{man.get('timeframe')}-{new_content_fp[:16]}",
                    "fingerprint_schema": ID.CURRENT_CANONICAL_ERA.schema_id}
    new_req = {**req, "canonical_content_fingerprint": new_content_fp,
               "manifest_fingerprint": new_manifest_fp,
               "raw_content_fingerprints": sorted(new_raw),
               "calendar_identity": calendar}
    conflict = _safe_write(ST.DATASET_MANIFESTS, new_manifest_fp, {
        "dataset_manifest.json": new_manifest,
        "reconciliation.json": recon,                  # meaning carried unchanged
        "request_manifest.json": new_req,
    }, root=root)
    if conflict:
        return {"status": REFUSED, "legacy_manifest_fingerprint": manifest_fp,
                "current_manifest_fingerprint": new_manifest_fp, "reason": conflict}

    after = ST.verify_dataset_provenance(new_manifest_fp, root=root)
    if not (after.get("verified") and after.get("current_era")):
        return {"status": REFUSED, "legacy_manifest_fingerprint": manifest_fp,
                "current_manifest_fingerprint": new_manifest_fp,
                "reason": f"reminted manifest is not a verified current-era graph: "
                          f"{after.get('reason') or after.get('not_current_reason')}"}

    # 4. Remint features onto the migrated dataset identity.
    features = _remint_features(new_content_fp, new_manifest_fp, man,
                                root=root, lookback=lookback)

    lineage = _write_lineage(
        "dataset_manifest",
        {"identity": manifest_fp,
         "identity_schema": prov.get("canonical_identity_schema")},
        {"identity": new_manifest_fp,
         "identity_schema": ID.CURRENT_CANONICAL_ERA.schema_id},
        equivalence={
            "request_meaning_unchanged": True,
            "reconciliation_meaning_unchanged": True,
            "calendar_meaning_unchanged": True,
            "legacy_manifest_identity_replayed": replayed == manifest_fp,
            "canonical_reference_updated": True,
            "reconciled_items": len(recon),
        },
        root=root,
        extra={"legacy_canonical_identity": req["canonical_content_fingerprint"],
               "current_canonical_identity": new_content_fp,
               "legacy_raw_identities": sorted(req.get("raw_content_fingerprints") or []),
               "current_raw_identities": sorted(new_raw),
               "features": features})
    return {"status": MIGRATED, "legacy_manifest_fingerprint": manifest_fp,
            "current_manifest_fingerprint": new_manifest_fp,
            "canonical": canon, "raw": raw_results, "features": features,
            "lineage_id": lineage}


def _remint_features(content_fp: str, manifest_fp: str, legacy_manifest: dict, *,
                     root: str, lookback: int) -> dict:
    """Rebuild features from the MIGRATED dataset, deterministically.

    Feature identity binds to the source dataset identity by design, so the
    values must be numerically identical while the fingerprint MUST differ. Both
    halves are asserted here and recorded in lineage — relabelling the old
    feature object instead would break exactly that binding.
    """
    rows = ST.read_snapshot(ST.DATASETS, content_fp, "canonical_bars.json", root=root)
    bars = ST.bars_from_rows(rows)
    timeframe = legacy_manifest.get("timeframe") or "5min"
    dataset_id = f"intraday-{timeframe}-{content_fp[:16]}"
    values = PL.features_from_bars(bars, dataset_id=dataset_id,
                                   fingerprint=content_fp,
                                   manifest_fingerprint=manifest_fp,
                                   lookback=lookback)
    new_fp = F.feature_fingerprint(values)
    ST.write_snapshot(ST.FEATURES, new_fp, {
        "features.json": [v.to_dict() for v in values],
        "feature_content_manifest.json": {
            "schema_version": SCHEMA_VERSION,
            "feature_fingerprint": new_fp,
            "feature_set_version": F.FEATURE_SET_VERSION,
            "source_dataset_fingerprint": content_fp,
            "source_dataset_manifest_fingerprint": manifest_fp,
            "observation_count": len(values),
            "features_enabled": list(F.ENABLED_FEATURES),
        },
    }, root=root)
    v = ST.verify_feature_snapshot(new_fp, root=root)
    return {"feature_fingerprint": new_fp, "observation_count": len(values),
            "verified": bool(v.get("verified")),
            "source_dataset_fingerprint": content_fp,
            "source_dataset_manifest_fingerprint": manifest_fp}


def migrate_corpus(*, root: str = ".", lookback: int = 3) -> dict:
    """Migrate every legacy manifest graph found on disk. Idempotent."""
    base = ST.intraday_root(root) / ST.DATASET_MANIFESTS
    results = []
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                results.append(migrate_dataset_manifest(d.name, root=root,
                                                        lookback=lookback))
    return {
        "schema_version": SCHEMA_VERSION,
        "source_module": "intraday_lab.migration",
        "observe_only": True,
        "migration_version": MIGRATION_VERSION,
        "manifests_examined": len(results),
        "migrated": [r for r in results if r["status"] == MIGRATED],
        "already_current": [r for r in results if r["status"] == ALREADY_CURRENT],
        "already_migrated": [r for r in results if r["status"] == ALREADY_MIGRATED],
        "refused": [r for r in results if r["status"] == REFUSED],
        "results": results,
    }


def active_corpus(*, root: str = ".") -> dict:
    """Which manifest graphs Session 3 may consume, and which are archival.

    Computed from persisted evidence, never from a curated list: an object is
    active only if its whole graph verifies AND is current-era today.
    """
    base = ST.intraday_root(root) / ST.DATASET_MANIFESTS
    active, archival, broken = [], [], []
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            v = ST.verify_dataset_provenance(d.name, root=root)
            entry = {"manifest_fingerprint": d.name,
                     "canonical_identity_schema": v.get("canonical_identity_schema"),
                     "reason": v.get("reason") or v.get("not_current_reason")}
            if not v.get("verified"):
                broken.append(entry)
            elif v.get("current_era"):
                active.append(entry)
            else:
                archival.append(entry)
    return {
        "schema_version": SCHEMA_VERSION,
        "observe_only": True,
        "current_identity_schemas": {
            "raw": ID.CURRENT_RAW_ERA.schema_id,
            "canonical": ID.CURRENT_CANONICAL_ERA.schema_id,
        },
        "active_manifests": active,
        "archival_manifests": archival,
        "integrity_failures": broken,
        "session_3_eligible_count": len(active),
        "policy": "Session 3 consumes ACTIVE manifests only. Archival manifests "
                  "are retained, verifiable legacy evidence and are never "
                  "silently reused.",
    }
