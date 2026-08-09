"""Agent Lab export — frozen, hash-verified, read-only production snapshots.

Converts a COMPLETED StockBot production run into an immutable, self-describing
input package that the Agent Lab can analyse offline, without being handed
``/opt/stockbot``, production credentials, or any mutable production file.

Design contract (all four properties are enforced in code + tests):

* **Allowlist-only.** The exporter copies exactly the artifacts named in
  :mod:`.allowlist`. It is ALLOW-explicit / DENY-everything-else, never
  "copy the tree minus known secrets".
* **Read-only + observe-only.** Nothing here mutates a decision, allocation,
  score, portfolio, or approval. The export lane is a pure downstream sink:
  no pipeline stage reads from ``outputs/agent_export/``.
* **Fail closed.** A missing required artifact, a hash mismatch, a schema
  violation, or a secret-boundary breach produces NO snapshot rather than a
  partial one. Validation refuses to bless what it cannot verify.
* **Frozen.** A finalised snapshot directory is never modified. Re-exporting
  the same run+SHA either verifies byte-identical or fails; it never overwrites.

Transport to the Agent Lab is deliberately NOT implemented here — see
``docs/STOCKBOT_AGENT_EXPORT.md``. This package only ever writes to the local
filesystem.
"""
from __future__ import annotations

SCHEMA_VERSION = "1.0"
ARTIFACT_TYPE = "agent_production_snapshot"

# Directory layout under the AGENT_EXPORT namespace root.
SNAPSHOTS_DIRNAME = "snapshots"
ARTIFACTS_DIRNAME = "artifacts"
MANIFEST_FILENAME = "manifest.json"
LATEST_POINTER_FILENAME = "latest.json"

# Prefix for in-progress build directories. A directory with this prefix is by
# definition NOT a finalised snapshot — the builder renames it into place only
# after the manifest verifies. Consumers must ignore these.
BUILD_PREFIX = ".build-"

__all__ = [
    "SCHEMA_VERSION",
    "ARTIFACT_TYPE",
    "SNAPSHOTS_DIRNAME",
    "ARTIFACTS_DIRNAME",
    "MANIFEST_FILENAME",
    "LATEST_POINTER_FILENAME",
    "BUILD_PREFIX",
]
