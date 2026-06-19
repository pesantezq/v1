import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from operator_control import worker_runner as wr


def _write_config(root: Path, cost_cap: dict | None):
    oc = {"autonomous_worker": {"enabled": True}}
    if cost_cap is not None:
        oc["cost_cap"] = cost_cap
    (root / "config.json").write_text(json.dumps({"operator_control": oc}), encoding="utf-8")


def _cost_log_path(root: Path) -> Path:
    p = root / "outputs" / "operator_control" / "worker_cost_log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _append_cost(root: Path, cost_usd: float, when: datetime):
    rec = {"timestamp": when.isoformat(), "work_order_id": "wo_x", "cost_usd": cost_usd}
    with _cost_log_path(root).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def test_cost_cap_cfg_reads_block(tmp_path):
    _write_config(tmp_path, {"usd_per_run": 3.0, "usd_per_day": 10.0,
                             "max_turns_per_run": 40, "max_run_seconds": 1200})
    cap = wr._cost_cap_cfg(tmp_path)
    assert cap == {"usd_per_run": 3.0, "usd_per_day": 10.0,
                   "max_turns_per_run": 40, "max_run_seconds": 1200}


def test_cost_cap_cfg_missing_block_all_none(tmp_path):
    _write_config(tmp_path, None)
    cap = wr._cost_cap_cfg(tmp_path)
    assert cap == {"usd_per_run": None, "usd_per_day": None,
                   "max_turns_per_run": None, "max_run_seconds": None}


def test_cost_cap_cfg_zero_or_negative_is_none(tmp_path):
    _write_config(tmp_path, {"usd_per_run": 0, "usd_per_day": -1,
                             "max_turns_per_run": 0, "max_run_seconds": None})
    cap = wr._cost_cap_cfg(tmp_path)
    assert all(v is None for v in cap.values())


def test_today_spend_sums_only_today(tmp_path):
    _write_config(tmp_path, {"usd_per_day": 10.0})
    now = datetime.now(timezone.utc)
    _append_cost(tmp_path, 2.5, now)
    _append_cost(tmp_path, 1.0, now)
    _append_cost(tmp_path, 99.0, now - timedelta(days=1))  # yesterday — excluded
    assert wr._today_spend_usd(tmp_path) == pytest.approx(3.5)


def test_today_spend_empty_log_is_zero(tmp_path):
    _write_config(tmp_path, {"usd_per_day": 10.0})
    assert wr._today_spend_usd(tmp_path) == 0.0
