"""Next-stage lane orchestrator (Phase 14, integration).

Runs all next-stage producers in dependency order, each fully non-fatal. Used for
live runs + as the documented hook for the sandbox/research cron (it writes only
SANDBOX/POLICY/PORTFOLIO + observe-only LATEST review artifacts — never
``decision_plan.json``). Deliberately a standalone orchestrator rather than
threaded into ``main.py`` so the official pipeline is untouched.

Order: system-improvement → universe scan + radar → shadow tracking →
market-opportunity prompts → strategy comparison → approval queues →
broker-aware side-panel.

    python -m portfolio_automation.next_stage.run_next_stage [--root .]
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _step(results: dict[str, Any], name: str, fn: Callable[[], Any]) -> None:
    try:
        results[name] = {"ok": True, "result": fn()}
    except Exception as exc:  # every step is non-fatal
        results[name] = {"ok": False, "error": str(exc)}


def run_all(root: Path, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    results: dict[str, Any] = {"generated_at": now.isoformat(), "observe_only": True}

    from portfolio_automation.system_improvement import write_system_improvement_artifacts
    from portfolio_automation.universe_scanner import write_universe_artifacts
    from portfolio_automation.sandbox.shadow_tracker import write_shadow_artifacts
    from portfolio_automation.market_opportunity_prompts import write_market_opportunity_artifacts
    from portfolio_automation.strategy.strategy_comparator import write_strategy_artifacts
    from portfolio_automation.approval_queue import build_action_queues
    from portfolio_automation.holdings_resolver import write_broker_aware_portfolio

    _step(results, "system_improvement", lambda: write_system_improvement_artifacts(root, now))
    _step(results, "universe_scan", lambda: write_universe_artifacts(root, now))      # writes radar
    _step(results, "shadow_tracking", lambda: write_shadow_artifacts(root, now))      # reads radar
    _step(results, "market_opportunity", lambda: write_market_opportunity_artifacts(root, now))
    _step(results, "strategy", lambda: write_strategy_artifacts(root, now))           # reads radar + holdings
    _step(results, "approval_queues", lambda: build_action_queues(root, now))
    _step(results, "broker_aware", lambda: write_broker_aware_portfolio(root, now))
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the next-stage research/strategy/improvement lane.")
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    args = ap.parse_args(argv)
    res = run_all(Path(args.root).resolve())
    ok = sum(1 for k, v in res.items() if isinstance(v, dict) and v.get("ok"))
    print(f"next-stage run: {ok} step(s) ok")
    for name, v in res.items():
        if isinstance(v, dict) and "ok" in v:
            print(f"  {'OK ' if v['ok'] else 'ERR'} {name}: {v.get('result') or v.get('error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
