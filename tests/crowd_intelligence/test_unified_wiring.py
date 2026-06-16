"""Wiring/contract guard for the Unified Crowd Intelligence Bus (2026-06-16).

Pins the additive governance + wiring for the unified crowd writer:
  - run_daily_safe.sh runs the unified writer as a daily stage (after both lanes);
  - artifact_registry.yaml registers crowd_intelligence.json + unified_crowd_intelligence.json
    with non-empty consumers (closing producer-without-consumer debt).

The writer's own behavior is covered by test_unified_bus.py / test_unified_integration.py;
this module only guards the pipeline wiring + registry coverage so the producers cannot
silently fall out of their cron driver or lose their registered consumers again.
"""

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_DAILY = _ROOT / "scripts" / "run_daily_safe.sh"
_REGISTRY = _ROOT / "portfolio_automation" / "artifact_registry.yaml"


def test_run_daily_safe_runs_unified_writer() -> None:
    text = _DAILY.read_text(encoding="utf-8")
    assert "crowd_intelligence.unified_writer" in text
    assert "Unified Crowd Intelligence Bus" in text


def test_registry_registers_crowd_artifacts_with_consumers() -> None:
    registry = yaml.safe_load(_REGISTRY.read_text(encoding="utf-8"))
    artifacts = registry.get("artifacts", registry)

    for name in ("crowd_intelligence.json", "unified_crowd_intelligence.json"):
        assert name in artifacts, f"{name} missing from artifact registry"
        entry = artifacts[name]
        consumers = entry.get("consumers")
        assert consumers, f"{name} must declare non-empty consumers"
        assert entry.get("producer"), f"{name} must declare a producer"
