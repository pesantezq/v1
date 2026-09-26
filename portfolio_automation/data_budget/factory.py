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


# Fallback when config.json lacks a readable api_limits.fmp_daily_calls_budget.
# Mirrors config/loader.py, which resolves the same key with a 230 default.
_FMP_DAILY_BUDGET_FALLBACK = 230


def _load_fmp_daily_budget(config_path: Any = "config.json", *,
                           default: int = _FMP_DAILY_BUDGET_FALLBACK) -> int:
    """The operator-configured LOCAL FMP daily budget, from
    ``config.json api_limits.fmp_daily_calls_budget``.

    Established semantics (match config/loader.py and FMPClient.would_exceed,
    which treats ``budget <= 0`` as no local daily cap):

    * explicit ``0``            -> ``0`` (no LOCAL daily cap; the VS-002 mission
                                   is still hard-bounded to 22 attempts by the
                                   StrictLiveAcquirer, which is a separate control)
    * positive integer          -> that local cap, verbatim
    * key genuinely absent      -> ``default`` (230), the loader's fallback
    * malformed (null / string / object / bool / non-int) -> ``default`` (230)

    Malformed configuration is NEVER silently interpreted as uncapped — a
    parse/shape failure falls back to the safe positive cap, not to 0.
    """
    try:
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except Exception:
        return default
    if not isinstance(cfg, dict):
        return default
    limits = cfg.get("api_limits")
    if not isinstance(limits, dict) or "fmp_daily_calls_budget" not in limits:
        return default
    raw = limits["fmp_daily_calls_budget"]
    # bool is a subclass of int; a True/False here is a config mistake, not a cap.
    # bool is a subclass of int; a True/False here is a config mistake, not a
    # cap. A negative value would make FMPClient.would_exceed treat it as <= 0
    # (uncapped), contradicting "only explicit 0 is uncapped" — so it, too, is
    # rejected to the safe positive fallback rather than silently uncapping.
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return default
    return raw


def vs002_strict_evidence_client(*, daily_budget: int | None = None,
                                 cache_dir: Any = None,
                                 config_path: Any = "config.json") -> Any:
    """FMP client for the bounded VS-002 strict-live evidence acquisition.

    Deliberately NOT a GovernedFMPClient: that proxy SKIPS or returns an empty
    result under run-mode / bandwidth / rate pressure ("never raises into
    callers"), which is exactly the silent fallback the VS-002 evidence
    contract forbids — evidence acquisition must fail closed. This client still
    honours the daily call counter for budget accounting and makes exactly one
    HTTP attempt per symbol via ``get_dividend_adjusted_bars_strict_live``,
    reading no cache. FMP client construction stays in this sanctioned factory.

    ``daily_budget`` is the LOCAL daily-call cap. When ``None`` (the default) it
    is inherited from the operator policy in ``config.json``
    (``api_limits.fmp_daily_calls_budget``) via :func:`_load_fmp_daily_budget`,
    so a configured ``0`` means no local daily cap — matching the rest of the
    application — while the VS-002 mission stays hard-bounded to 22 attempts by
    the StrictLiveAcquirer. An explicit integer argument overrides config
    verbatim (a deterministic seam for tests and controlled callers).
    """
    from fmp_client import FMPClient
    if daily_budget is None:
        daily_budget = _load_fmp_daily_budget(config_path)
    return FMPClient(retry_max=1, daily_budget=daily_budget,
                     cache_dir=Path(cache_dir) if cache_dir is not None else None)
