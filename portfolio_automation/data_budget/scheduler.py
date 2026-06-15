from __future__ import annotations
from typing import Any

# Defaults — overridable from config.json data_budget.run_modes. call_budget=0 = uncapped.
DEFAULT_RUN_MODES: dict[str, dict[str, Any]] = {
    "gui_refresh":       {"call_budget": 30,   "priority": "high"},
    "daily":             {"call_budget": 0,    "priority": "high"},   # 0 = uncapped (honors config uncap)
    "weekly_review":     {"call_budget": 800,  "priority": "medium"},
    "monthly":           {"call_budget": 1500, "priority": "medium"},
    # 650 covers a cold crowd-intelligence run over the capped universe (60 symbols
    # x ~9 per-symbol endpoints + ~11 shared ≈ 551) with headroom. Calls are within
    # the existing paid FMP Starter allowance (300/min, 20GB/mo) — no extra cost.
    "discovery":         {"call_budget": 650,  "priority": "low"},
    "historical_replay": {"call_budget": 0,    "priority": "low", "cache_only": True},
}


class RunModeScheduler:
    """Pure policy: per-run-mode call budget, priority tier, skip decisions."""

    def __init__(self, run_modes: dict[str, dict[str, Any]]) -> None:
        self._modes = run_modes

    def _mode(self, run_mode: str) -> dict[str, Any]:
        return self._modes.get(run_mode, {"call_budget": 0, "priority": "medium"})

    def priority(self, run_mode: str) -> str:
        return self._mode(run_mode).get("priority", "medium")

    def call_budget(self, run_mode: str) -> int:
        m = self._mode(run_mode)
        if m.get("cache_only"):
            return 0
        return int(m.get("call_budget", 0))

    def is_cache_only(self, run_mode: str) -> bool:
        return bool(self._mode(run_mode).get("cache_only"))

    def over_run_budget(self, run_mode: str, *, calls_so_far: int) -> bool:
        # cache_only modes (e.g. historical_replay) are governed by cache-first
        # pass-through, NOT a positive call budget — never report them as
        # over-budget, or every call would be skipped and the cache never served.
        if self.is_cache_only(run_mode):
            return False
        budget = self.call_budget(run_mode)
        if budget <= 0:
            return False  # uncapped
        return calls_so_far >= budget

    def should_skip(self, run_mode: str, *, bandwidth_exhausted: bool) -> bool:
        return bool(bandwidth_exhausted and self.priority(run_mode) == "low")
