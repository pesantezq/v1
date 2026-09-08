"""release — the production release-immutability contract.

Establishes, in code, the invariant that a deployed release is immutable and
that everything production writes at runtime lands outside the tracked tree:

    production_code_sha == approved_release_sha
    AND no unauthorized tracked code drift

Three concerns, three modules:

``contracts``  which paths are RUNTIME_MUTABLE vs RELEASE_IMMUTABLE, and which
               artifacts are runtime-generated rather than source.
``scheduler``  every systemd Exec*, WorkingDirectory and cron command must
               resolve to the approved release, not a legacy checkout.
``pointer``    prove /opt/stockbot/current resolves to a release whose git SHA
               IS the approved SHA — path alignment alone cannot establish it.
``preflight``  prove the selected environment imports project modules from the
               approved release and not from a legacy checkout.

Everything here is pure: text and path analysis over a caller-supplied tree.
No network, no root, no systemd, no cron, no secrets, and deliberately no
import of the Northstar authority layer — this is deployment mechanics, not
roadmap authority.
"""
from __future__ import annotations

from .contracts import (  # noqa: F401
    IMMUTABLE_EVIDENCE_ARTIFACTS,
    IMMUTABLE_EXPERIMENT_EVIDENCE,
    RELEASE_IMMUTABLE,
    RUNTIME_GENERATED_ARTIFACTS,
    RUNTIME_MUTABLE,
    RUNTIME_ROOTS,
    classify_path,
    is_runtime_mutable,
)

__all__ = [
    "IMMUTABLE_EVIDENCE_ARTIFACTS",
    "IMMUTABLE_EXPERIMENT_EVIDENCE",
    "RELEASE_IMMUTABLE",
    "RUNTIME_GENERATED_ARTIFACTS",
    "RUNTIME_MUTABLE",
    "RUNTIME_ROOTS",
    "classify_path",
    "is_runtime_mutable",
]
