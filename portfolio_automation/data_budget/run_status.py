from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

from portfolio_automation.data_budget.usage_ledger import UsageLedger
from portfolio_automation.data_budget.status_producer import write_status_artifacts


def run_data_budget_status(*, root: Path | str = ".") -> None:
    """Non-blocking: build the 3 budget artifacts from the ledger. Never raises."""
    try:
        root = Path(root)
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
        db = cfg.get("data_budget") or {}
        holdings = (cfg.get("portfolio") or {}).get("holdings") or []
        symbols = [str(h.get("symbol")) for h in holdings if h.get("symbol")]
        ledger = UsageLedger(root / "data" / "fmp_budget.db")
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        write_status_artifacts(
            ledger=ledger, cache_dir=root / "data" / "fmp_cache",
            portfolio_symbols=symbols, month=month,
            monthly_bandwidth_gb=db.get("monthly_bandwidth_gb", 20),
            run_modes=db.get("run_modes", {}), base_dir=root / "outputs")
    except Exception:
        pass


if __name__ == "__main__":
    run_data_budget_status()
