from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from portfolio_automation.data_budget.governor import FMPBudgetGovernor

_governor: FMPBudgetGovernor | None = None


def _load_config() -> dict:
    try:
        return (json.loads(Path("config.json").read_text(encoding="utf-8"))
                .get("data_budget") or {})
    except Exception:
        return {}


def get_governor() -> FMPBudgetGovernor:
    global _governor
    if _governor is None:
        _governor = FMPBudgetGovernor(
            db_path=Path("data/fmp_budget.db"),
            cache_dir=Path("data/fmp_cache"),
            config=_load_config())
    return _governor


def governed_client(run_mode: str, *, fmp_client: Any = None) -> Any:
    """The single entry point all modules use instead of FMPClient(...)."""
    return get_governor().client(run_mode=run_mode, fmp_client=fmp_client)
