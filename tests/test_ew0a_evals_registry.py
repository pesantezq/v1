"""The eval registry must reference REAL, collectible pytest node IDs.

A registry that names tests which no longer exist is worse than no registry: it
reports coverage that is not being run. This test makes registry rot a build
failure rather than a silent gap.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
REGISTRY = REPO / "evals" / "registry.json"


@pytest.fixture(scope="module")
def registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def collected_node_ids() -> set[str]:
    """Every node id pytest can actually collect from the referenced test files."""
    files = sorted({nid.split("::", 1)[0]
                    for cat in json.loads(REGISTRY.read_text(encoding="utf-8"))["categories"].values()
                    for nid in cat["node_ids"]})
    if not files:
        return set()
    out = subprocess.run(
        [sys.executable, "-m", "pytest", *files, "--collect-only", "-q", "-p", "no:randomly"],
        cwd=REPO, capture_output=True, text=True, timeout=300)
    return {ln.strip() for ln in out.stdout.splitlines() if "::" in ln}


def test_every_registered_node_id_exists(registry, collected_node_ids):
    missing = [nid for cat in registry["categories"].values()
               for nid in cat["node_ids"] if nid not in collected_node_ids]
    assert not missing, f"eval registry references non-existent node ids: {missing}"


def test_hidden_certification_set_is_empty(registry):
    """A held-out set authored by the session that built the system is not held out.
    It must stay empty until a separate certification mission authors it."""
    assert registry["categories"]["certification/hidden"]["node_ids"] == []


def test_all_required_categories_are_present(registry):
    required = {
        "regression/authority", "regression/false_certification", "regression/stale_state",
        "regression/reconciliation", "regression/lesson_poisoning", "regression/retry",
        "regression/escalation", "regression/crash_recovery",
        "capability/task_selection", "capability/risk_routing", "capability/executor_routing",
        "capability/acceptance_criteria", "capability/verification_planning",
        "capability/lesson_transfer", "capability/safe_reconciliation",
        "certification/hidden"}
    assert required <= set(registry["categories"])
