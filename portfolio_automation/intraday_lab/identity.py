"""Identity eras for Intraday Lab immutable objects. Research-only.

WHY THIS MODULE EXISTS
======================

Every immutable object in the lab is content-addressed: its directory name IS a
hash of its own content. That makes tampering detectable — but it also means
that **changing the hash function retroactively re-labels every existing object
as corrupt**. The bytes did not change; the question being asked of them did.

That happened on 2026-08-09. Raw identity gained `provider`/`endpoint` and
canonical identity gained `bar_end_at`/`known_at`. Both were correct changes,
but the verifier recomputed every object under the CURRENT function and reported
older objects with the tampering reason ("persisted payload does not hash to its
identity"). Five of ten objects were byte-perfect yet reported as corrupt.

The dangerous consequence is not the false alarm. It is DESENSITISATION: once
half the corpus permanently reports tampering, that message stops meaning
anything and a genuine tampering event hides in the noise.

So identity changes are treated as ERAS, and verification asks two separate
questions that must never be collapsed into one:

    INTEGRITY            Does this object verify under the identity schema
                         that actually minted it?

    RESEARCH ELIGIBILITY Does this object satisfy the identity contract in
                         force TODAY?

A legacy object that verifies under its own era is sound EVIDENCE. It is not
thereby eligible for current research — that requires migration to the current
era (see `migration.py`). Neither answer is allowed to imply the other.

THREE SCHEMAS, DELIBERATELY DISTINCT
====================================

    storage_schema   how the snapshot directory is laid out
    content_schema   what the stored records mean
    identity_schema  which function minted the directory name

Only the third decides how to verify. The pre-existing `schema_version: "1"` is
a STORAGE envelope version — it did not change when the identity function did,
which is precisely why it could not be used to pick a verifier.

BOUNDED, NOT OPEN-ENDED
=======================

The registry below is a CLOSED list of the eras this corpus actually contains.
It is not a "try every hash until something matches" mechanism:

* an object that DECLARES its era is verified under that era ONLY (no downgrade
  probing — otherwise an attacker could pick whichever era suits a forgery);
* an object with NO declared era (written before declarations existed) is
  attributed by probing the closed registry, and the attribution is frozen into
  an immutable attestation so the probe happens once, not forever;
* if no era reproduces the identity  -> INTEGRITY_FAILURE (fail closed);
* if more than one era reproduces it -> AMBIGUOUS (fail closed);
* if the declared era is unknown     -> UNSUPPORTED (fail closed).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Sequence

SCHEMA_VERSION = "1"

# The storage envelope. Bumped only when the DIRECTORY LAYOUT changes.
STORAGE_SCHEMA = "intraday_content_envelope_v1"

# What the stored records mean, independent of how they are hashed.
CONTENT_SCHEMA_RAW = "intraday_provider_rows_v1"
CONTENT_SCHEMA_CANONICAL = "intraday_bar_v1"


# ── Verification states ────────────────────────────────────────────────────
# VERIFIED_CURRENT            integrity OK under the era in force today.
#                             The only state eligible for current research.
# VERIFIED_LEGACY_MIGRATABLE  integrity OK under an older era, and the persisted
#                             bytes carry everything the current era needs, so a
#                             current identity is computable WITHOUT refetching.
# VERIFIED_LEGACY_ARCHIVAL    integrity OK under an older era, but the persisted
#                             bytes cannot express the current era (a field the
#                             current identity protects was never stored).
#                             Sound evidence; permanently archival.
# UNSUPPORTED_IDENTITY_SCHEMA the object names an era this code does not
#                             implement. Fail closed — never guess.
# AMBIGUOUS_IDENTITY_SCHEMA   more than one era reproduces the identity, so the
#                             object's meaning is not determined. Fail closed.
# INTEGRITY_FAILURE           no supported era reproduces the identity. This is
#                             the ONLY state that means tampering/corruption.
VERIFIED_CURRENT = "VERIFIED_CURRENT"
VERIFIED_LEGACY_MIGRATABLE = "VERIFIED_LEGACY_MIGRATABLE"
VERIFIED_LEGACY_ARCHIVAL = "VERIFIED_LEGACY_ARCHIVAL"
UNSUPPORTED_IDENTITY_SCHEMA = "UNSUPPORTED_IDENTITY_SCHEMA"
AMBIGUOUS_IDENTITY_SCHEMA = "AMBIGUOUS_IDENTITY_SCHEMA"
INTEGRITY_FAILURE = "INTEGRITY_FAILURE"

# States in which the persisted bytes are known-good.
VERIFIED_STATES = frozenset({
    VERIFIED_CURRENT, VERIFIED_LEGACY_MIGRATABLE, VERIFIED_LEGACY_ARCHIVAL,
})
# States that permit use as a CURRENT research object. Deliberately a single
# member: "verified" must never widen into "eligible".
CURRENT_RESEARCH_STATES = frozenset({VERIFIED_CURRENT})


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)


def content_hash(payload: Any) -> str:
    """The single hashing primitive. Every era builds its payload and calls this.

    Defined here rather than in storage so that an era's function and the store
    can never drift apart on separators, key order or default coercion — a
    divergence there would silently re-label the entire corpus.
    """
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:32]


class IdentityEra:
    """One historical identity function, addressed by its schema id.

    `compute` returns None when the persisted bytes lack a field this era
    hashes. That is not an error: it is how an object is recognised as unable to
    express a given era, which is what separates ARCHIVAL from MIGRATABLE.
    """

    __slots__ = ("schema_id", "compute", "protects", "note")

    def __init__(self, schema_id: str, compute: Callable[..., str | None],
                 protects: tuple[str, ...], note: str) -> None:
        self.schema_id = schema_id
        self.compute = compute
        self.protects = protects
        self.note = note

    def __repr__(self) -> str:            # pragma: no cover - debugging aid
        return f"IdentityEra({self.schema_id!r})"


# ── RAW identity eras ──────────────────────────────────────────────────────
# A raw object is one provider response: the observations AND where they came
# from. v1 hashed only the observations, so two different endpoints returning
# identical rows collided on one identity while storing DIFFERENT content
# manifests — an identity narrower than the content stored beneath it.

RAW_V1 = "intraday_raw_v1"
RAW_V2 = "intraday_raw_v2"


def _raw_v1(rows: Any, manifest: dict) -> str | None:
    symbol, timeframe = manifest.get("symbol"), manifest.get("timeframe")
    if symbol is None or timeframe is None:
        return None
    return content_hash({"schema": RAW_V1, "symbol": symbol,
                         "timeframe": timeframe, "rows": rows})


def _raw_v2(rows: Any, manifest: dict) -> str | None:
    symbol, timeframe = manifest.get("symbol"), manifest.get("timeframe")
    provider, endpoint = manifest.get("provider"), manifest.get("endpoint")
    if symbol is None or timeframe is None or provider is None or endpoint is None:
        return None
    return content_hash({"schema": RAW_V2, "provider": provider,
                         "endpoint": endpoint, "symbol": symbol,
                         "timeframe": timeframe, "rows": rows})


# ── CANONICAL identity eras ────────────────────────────────────────────────
# v2 hashed OHLCV + bar_start_at only. Two datasets whose bars became KNOWABLE
# at different instants therefore shared one identity, even though one confers a
# look-ahead advantage over the other. Temporal knowability is the core PIT
# contract; it belongs in identity, not in metadata beside it.

CANONICAL_V2 = "intraday_canonical_v2"
CANONICAL_V3 = "intraday_canonical_v3"

# Fields the CURRENT canonical identity protects. Anything here that is absent
# from a legacy object makes that object permanently archival.
CANONICAL_V3_PROTECTS = ("symbol", "timeframe", "bar_start_at", "bar_end_at",
                         "known_at", "open", "high", "low", "close", "volume",
                         "adjustment_state")


def _canonical_rows(bars: Sequence[dict], fields: Sequence[str]) -> list[list] | None:
    out = []
    for r in bars:
        if any(f not in r for f in fields):
            return None
        out.append([r[f] for f in fields])
    return sorted(out, key=lambda x: (x[0], x[1], x[2]))


_V2_FIELDS = ("symbol", "timeframe", "bar_start_at",
              "open", "high", "low", "close", "volume")
_V3_FIELDS = ("symbol", "timeframe", "bar_start_at", "bar_end_at", "known_at",
              "open", "high", "low", "close", "volume")


def _canonical_v2(bars: Sequence[dict], manifest: dict) -> str | None:
    rows = _canonical_rows(bars, _V2_FIELDS)
    if rows is None:
        return None
    return content_hash({"schema": CANONICAL_V2,
                         "timeframe": manifest.get("timeframe"),
                         "adjustment_state": manifest.get("adjustment_state"),
                         "rows": rows})


def _canonical_v3(bars: Sequence[dict], manifest: dict) -> str | None:
    rows = _canonical_rows(bars, _V3_FIELDS)
    if rows is None:
        return None
    return content_hash({"schema": CANONICAL_V3,
                         "timeframe": manifest.get("timeframe"),
                         "adjustment_state": manifest.get("adjustment_state"),
                         "rows": rows})


# Newest first. Order affects only which era is reported first in diagnostics —
# attribution requires EXACTLY ONE match, so it can never depend on order.
RAW_ERAS: tuple[IdentityEra, ...] = (
    IdentityEra(RAW_V2, _raw_v2, ("provider", "endpoint", "symbol", "timeframe", "rows"),
                "adds provider + endpoint: source semantics are part of what a "
                "raw observation object MEANS"),
    IdentityEra(RAW_V1, _raw_v1, ("symbol", "timeframe", "rows"),
                "observations only; source semantics unprotected"),
)

CANONICAL_ERAS: tuple[IdentityEra, ...] = (
    IdentityEra(CANONICAL_V3, _canonical_v3, CANONICAL_V3_PROTECTS,
                "adds bar_end_at + known_at: when a bar became knowable changes "
                "point-in-time research meaning and must change identity"),
    IdentityEra(CANONICAL_V2, _canonical_v2,
                ("symbol", "timeframe", "bar_start_at", "open", "high", "low",
                 "close", "volume", "adjustment_state"),
                "OHLCV + bar_start_at only; PIT knowability unprotected"),
)

CURRENT_RAW_ERA = RAW_ERAS[0]
CURRENT_CANONICAL_ERA = CANONICAL_ERAS[0]

_REGISTRY: dict[str, tuple[IdentityEra, ...]] = {
    "raw": RAW_ERAS,
    "canonical": CANONICAL_ERAS,
}


def eras_for(kind: str) -> tuple[IdentityEra, ...]:
    if kind not in _REGISTRY:
        raise KeyError(f"unknown identity kind {kind!r}; known: {sorted(_REGISTRY)}")
    return _REGISTRY[kind]


def current_era(kind: str) -> IdentityEra:
    return eras_for(kind)[0]


def attribute(kind: str, identity: str, payload: Any, manifest: dict) -> dict:
    """Resolve which identity era minted `identity`, and what that implies.

    Returns a dict carrying the state, the resolved era, whether the object is
    current-era, and — when an older era minted it — the identity the CURRENT
    era assigns to the same persisted bytes, so migration never has to re-derive
    it from a second source of truth.

    A DECLARED era is authoritative and is checked alone. Probing is reserved
    for objects written before declarations existed, and demands a unique match.
    """
    eras = eras_for(kind)
    by_id = {e.schema_id: e for e in eras}
    cur = eras[0]
    manifest = manifest or {}
    declared = manifest.get("identity_schema")

    def result(state: str, era: IdentityEra | None, **extra) -> dict:
        schema_id = era.schema_id if era else declared
        current_identity = None
        if era is not None and era.schema_id != cur.schema_id:
            current_identity = cur.compute(payload, manifest)
        elif era is not None:
            current_identity = identity
        return {
            "state": state,
            "identity_schema": schema_id,
            "is_current_era": bool(era is not None and era.schema_id == cur.schema_id),
            "current_identity_schema": cur.schema_id,
            "current_identity": current_identity,
            "migration_required": bool(
                era is not None and era.schema_id != cur.schema_id
                and current_identity is not None),
            "attribution": "declared" if declared else "probed",
            "identity": identity,
            **extra,
        }

    if declared is not None:
        era = by_id.get(declared)
        if era is None:
            # Never guess past a declaration. An object naming an era this build
            # does not implement is unverifiable, which is not the same as
            # corrupt — and must not be silently downgraded to an era we do have.
            return result(UNSUPPORTED_IDENTITY_SCHEMA, None,
                          reason=f"declared identity schema {declared!r} is not "
                                 f"implemented by this build; supported: "
                                 f"{sorted(by_id)}")
        recomputed = era.compute(payload, manifest)
        if recomputed != identity:
            return result(INTEGRITY_FAILURE, None, recomputed=recomputed,
                          reason=f"persisted bytes do not hash to their identity "
                                 f"under their own declared schema {declared!r} — "
                                 f"the object has been modified")
        state = (VERIFIED_CURRENT if era.schema_id == cur.schema_id
                 else _legacy_state(era, cur, payload, manifest))
        return result(state, era, recomputed=recomputed, reason=None)

    # Undeclared: probe the CLOSED registry. Exactly one match required.
    matches = [e for e in eras if e.compute(payload, manifest) == identity]
    if not matches:
        return result(INTEGRITY_FAILURE, None,
                      reason="no supported identity schema reproduces this "
                             "identity from the persisted bytes — the object has "
                             "been modified or was written by an unknown build",
                      probed=[e.schema_id for e in eras])
    if len(matches) > 1:
        return result(AMBIGUOUS_IDENTITY_SCHEMA, None,
                      reason="more than one identity schema reproduces this "
                             "identity, so the object's meaning is undetermined",
                      probed=[e.schema_id for e in matches])
    era = matches[0]
    state = (VERIFIED_CURRENT if era.schema_id == cur.schema_id
             else _legacy_state(era, cur, payload, manifest))
    return result(state, era, reason=None)


def _legacy_state(era: IdentityEra, cur: IdentityEra, payload: Any,
                  manifest: dict) -> str:
    """MIGRATABLE when the current era can be computed from the SAME bytes."""
    return (VERIFIED_LEGACY_MIGRATABLE
            if cur.compute(payload, manifest) is not None
            else VERIFIED_LEGACY_ARCHIVAL)


def era_registry_provenance() -> dict:
    """What this build can verify. Persisted with migration lineage."""
    return {
        "schema_version": SCHEMA_VERSION,
        "storage_schema": STORAGE_SCHEMA,
        "kinds": {
            kind: {
                "current_identity_schema": eras[0].schema_id,
                "supported_identity_schemas": [
                    {"identity_schema": e.schema_id, "protects": list(e.protects),
                     "note": e.note} for e in eras],
            } for kind, eras in _REGISTRY.items()
        },
    }
