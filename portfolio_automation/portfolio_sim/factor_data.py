"""
Fama-French factor data loader (offline-first).

Reads a cached monthly factor-return CSV from `data/factors/ff_monthly.csv`
(columns: month YYYY-MM, Mkt-RF, SMB, HML, RMW, CMA, MOM, RF — percent or decimal).
Never fetches at runtime; `scripts/fetch_factor_data.sh` populates the cache from
Kenneth French's data library. Absent cache → {} (factor attribution degrades).
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("stockbot.portfolio_sim.factor_data")

_REL = ("data", "factors", "ff_monthly.csv")
_FACTORS = ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "MOM", "RF")


def load_factors(root: str | Path = ".") -> dict[str, dict[str, float]]:
    """Return {month 'YYYY-MM': {factor: return_decimal}}. Empty on miss."""
    path = Path(root).joinpath(*_REL)
    if not path.exists():
        return {}
    out: dict[str, dict[str, float]] = {}
    try:
        with path.open(encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                month = (row.get("month") or row.get("date") or "").strip()[:7]
                if not month:
                    continue
                rec: dict[str, float] = {}
                for f in _FACTORS:
                    v = row.get(f)
                    if v in (None, ""):
                        continue
                    try:
                        x = float(v)
                    except ValueError:
                        continue
                    # F-F publishes percent; normalize to decimal if it looks like percent
                    rec[f] = x / 100.0 if abs(x) > 1.5 else x
                if rec:
                    out[month] = rec
    except Exception as exc:  # pragma: no cover
        logger.debug("factor data load failed (%s)", exc)
        return {}
    return out


def available_factors(factors: dict[str, dict[str, float]]) -> list[str]:
    if not factors:
        return []
    sample = next(iter(factors.values()))
    return [f for f in _FACTORS if f in sample and f != "RF"]
