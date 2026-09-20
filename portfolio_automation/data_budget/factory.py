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


def vs002_strict_evidence_client(*, daily_budget: int = 230,
                                 cache_dir: Any = None) -> Any:
    """FMP client for the bounded VS-002 strict-live evidence acquisition.

    Deliberately NOT a GovernedFMPClient: that proxy SKIPS or returns an empty
    result under run-mode / bandwidth / rate pressure ("never raises into
    callers"), which is exactly the silent fallback the VS-002 evidence
    contract forbids — evidence acquisition must fail closed. This client still
    honours the daily call counter for budget accounting and makes exactly one
    HTTP attempt per symbol via ``get_dividend_adjusted_bars_strict_live``,
    reading no cache. FMP client construction stays in this sanctioned factory.
    """
    from fmp_client import FMPClient
    return FMPClient(retry_max=1, daily_budget=daily_budget,
                     cache_dir=Path(cache_dir) if cache_dir is not None else None)
