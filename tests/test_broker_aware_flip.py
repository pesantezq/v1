import json
from pathlib import Path


def test_config_has_broker_aware_enabled():
    cfg = json.loads(Path("config.json").read_text())
    assert cfg["portfolio"]["broker_aware"]["enabled"] is True
