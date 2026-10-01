"""Research Store — durable persistence of canonical evidence.

WHAT THIS IS.

An immutable, local, deterministic store for ``EvidenceSnapshot`` objects. It
persists evidence and returns candidate corpora. That is all.

WHAT THIS DELIBERATELY IS NOT.

  * It is NOT the EvidenceGateway. Historical admissibility -- whether a
    snapshot was knowable as of some instant -- is decided by
    ``evidence_gateway.admit`` and ``resolve_visibility``, which produce
    reason-coded refusals. A ``WHERE known_at <= ?`` would return the same rows
    and produce no audit trail, and the 0C exit gate is lookahead-AUDITED reads.
  * It does NOT choose a revision winner. Winner policy is
    UNRESOLVED_NOT_INVENTED and stays that way.
  * It does NOT interpret ``effective_period``. That question is open.

So there is no ``as_of`` parameter anywhere in this package, and no method named
latest, current, newest, best or active. Those names are how winner selection
re-enters a system that explicitly declined to define it.

WHY EVIDENCE IS KEYED BY snapshot_id AND NOT BY ITS BYTES.

Identity excludes acquisition metadata: ``retrieved_at`` is dropped from the
identity view and ``provenance.recorded_at`` is excluded. So one snapshot_id
legitimately corresponds to more than one canonical byte string. Addressing by
content would put those at two different addresses, and the identity conflict
this store must refuse would simply become invisible.
"""
from portfolio_automation.research_store.store import (  # noqa: F401
    GetResult,
    PutResult,
    ResearchStore,
    ResearchStoreError,
    StoreRefusal,
)

__all__ = [
    "GetResult",
    "PutResult",
    "ResearchStore",
    "ResearchStoreError",
    "StoreRefusal",
]
