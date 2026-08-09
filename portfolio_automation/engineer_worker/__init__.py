"""Engineer Worker MVP 0A — local diagnostics + disposable repair candidates.

EXPERIMENTAL / NON-CANONICAL. This package is a parallel prototype of the first
local engineering worker for the StockBot Agent Lab. It deliberately does NOT
define project-wide canonical contracts (EvidenceRef, ResearchTask, WorkerResult,
ResearchClaim, ExperimentSpec, ExperimentResult — those belong to the Northstar
canonical-contracts work). All schemas here are ``Engineering*V0`` and are marked
``experimental_noncanonical`` so they can be cleanly mapped to future Northstar
contracts without competing with them.

Authority model (hard rule): the local model is NEVER an executor of arbitrary
shell. Trusted deterministic code (the controller + allowlisted adapters) decides
what actually runs. The worker can analyze evidence, request *approved*
diagnostic capabilities, and edit files inside a *disposable* workspace only. It
cannot touch the authoritative environment, canonical branch, production, or any
protected path. See docs/ENGINEER_WORKER_MVP.md.
"""
from __future__ import annotations

EXPERIMENTAL_MARKER = "experimental_noncanonical"
MVP_VERSION = "0A"

__all__ = ["EXPERIMENTAL_MARKER", "MVP_VERSION"]
