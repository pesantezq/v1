"""
CLI entry point for Historical Replay v1.

Usage:
    python -m portfolio_automation.historical_replay.replay_runner --days 90

Options:
    --days N            Number of trading days to replay (default: 90)
    --symbols A,B,C     Extra symbols beyond config holdings
    --output-dir PATH   Output directory (default: outputs/backtest)
    --window-days 1,3,7 Outcome resolution windows (default: 1,3,7)
    --dry-run           Simulate without writing any files
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("stockbot.portfolio_automation.historical_replay.runner")

_DEFAULT_OUTPUT_DIR = Path("outputs") / "backtest"
_DEFAULT_DAYS = 90
_DEFAULT_WINDOWS = (1, 3, 7)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, default=str) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def _load_fmp_client() -> Any:
    try:
        from fmp_client import FMPClient  # type: ignore[import]
        return FMPClient()
    except Exception as exc:
        logger.warning("runner: could not load FMPClient — %s", exc)
        return None


def run_replay(
    *,
    days: int = _DEFAULT_DAYS,
    extra_symbols: list[str] | None = None,
    output_dir: Path | None = None,
    window_days: tuple[int, ...] = _DEFAULT_WINDOWS,
    dry_run: bool = False,
    fmp_client: Any = None,
    config_path: Path | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """
    Run the full historical replay pipeline.

    Steps:
      1. Load symbol universe (config holdings + optional extras)
      2. Fetch historical EOD prices via FMPClient
      3. Simulate proxy decisions (momentum rule)
      4. Resolve outcomes using forward prices
      5. Write JSONL + calibration + attribution reports

    Returns a summary dict.  Non-fatal: top-level exceptions are caught and
    recorded in summary["errors"].
    """
    from portfolio_automation.historical_replay.replay_data_loader import (
        load_holdings_symbols,
        load_universe,
        load_historical_prices,
    )
    from portfolio_automation.historical_replay.replay_decision_simulator import (
        simulate_all_decisions,
    )
    from portfolio_automation.historical_replay.replay_outcome_resolver import (
        resolve_outcomes,
    )
    from portfolio_automation.historical_replay.replay_reports import (
        build_historical_calibration,
        build_historical_attribution,
        write_calibration,
        write_attribution,
    )

    repo_root = root or Path(".")
    out_dir = output_dir or _DEFAULT_OUTPUT_DIR
    if not out_dir.is_absolute():
        out_dir = repo_root / out_dir

    cfg_path = config_path or (repo_root / "config.json")

    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "days": days,
        "window_days": list(window_days),
        "errors": [],
    }

    # ── Step 1: Universe ──────────────────────────────────────────────────────
    print("  [1/5] Loading universe...")
    symbols = load_universe(cfg_path, extra_symbols)
    if not symbols:
        summary["errors"].append("No symbols found in universe; using AAPL,MSFT,NVDA,QQQ")
        symbols = ["AAPL", "MSFT", "NVDA", "QQQ"]
    summary["universe_size"] = len(symbols)
    print(f"        {len(symbols)} symbols")

    # ── Step 2: Historical prices ─────────────────────────────────────────────
    print("  [2/5] Loading historical prices...")
    if dry_run and fmp_client is None:
        print("  DRY RUN: skipping price fetch")
        summary.update({"prices_loaded": 0, "decisions_generated": 0,
                         "decisions_resolved": 0, "output_files": []})
        return summary

    client = fmp_client or _load_fmp_client()
    if client is None:
        summary["errors"].append("FMPClient unavailable — cannot load historical prices")
        return summary

    price_data = load_historical_prices(symbols, client, days=days)
    summary["prices_loaded"] = len(price_data)
    print(f"        {len(price_data)} / {len(symbols)} symbols loaded")

    if not price_data:
        summary["errors"].append("No price data loaded for any symbol")
        return summary

    # ── Step 3: Simulate decisions ────────────────────────────────────────────
    print("  [3/5] Simulating decisions...")
    holding_syms = frozenset(h.upper() for h in load_holdings_symbols(cfg_path))
    decision_rows = simulate_all_decisions(
        price_data, holding_symbols=holding_syms, days=days,
    )
    summary["decisions_generated"] = len(decision_rows)
    print(f"        {len(decision_rows)} decisions generated")

    if not decision_rows:
        summary["errors"].append("No decisions generated (insufficient price history?)")
        summary["decisions_resolved"] = 0
        summary["output_files"] = []
        return summary

    # ── Step 4: Resolve outcomes ──────────────────────────────────────────────
    print("  [4/5] Resolving outcomes...")
    resolved_rows = resolve_outcomes(decision_rows, price_data, window_days=window_days)
    resolved_count = sum(1 for r in resolved_rows if r.get("resolved"))
    summary["decisions_resolved"] = resolved_count
    print(f"        {resolved_count} / {len(resolved_rows)} resolved")

    # ── Step 5: Write outputs ─────────────────────────────────────────────────
    print("  [5/5] Writing outputs...")
    cal_payload = build_historical_calibration(resolved_rows)
    attr_payload = build_historical_attribution(resolved_rows)
    output_files: list[str] = []

    if not dry_run:
        jsonl_path = out_dir / "decision_outcomes_historical.jsonl"
        _write_jsonl(jsonl_path, resolved_rows)
        output_files.append(str(jsonl_path))

        j, m = write_calibration(cal_payload, out_dir)
        output_files += [str(j), str(m)]

        j, m = write_attribution(attr_payload, out_dir)
        output_files += [str(j), str(m)]

        for f in output_files:
            print(f"        Written: {f}")
    else:
        print("  DRY RUN: file writes skipped")

    summary["output_files"] = output_files

    hr = cal_payload.get("overall_hit_rate")
    avg_ret = cal_payload.get("overall_avg_return")
    print()
    print("  ── Summary ──────────────────────────────────────────────────")
    print(f"  Universe:      {len(symbols)} symbols")
    print(f"  Prices:        {len(price_data)} loaded")
    print(f"  Decisions:     {len(decision_rows)}")
    print(f"  Resolved:      {resolved_count}")
    print(f"  Hit rate:      {f'{hr:.0%}' if hr is not None else '—'}")
    print(f"  Avg return:    {f'{avg_ret:+.2%}' if avg_ret is not None else '—'}")
    if summary["errors"]:
        print(f"  Warnings:      {'; '.join(summary['errors'])}")
    print("  ─────────────────────────────────────────────────────────────")

    return summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="replay_runner",
        description="Historical Replay v1 — offline portfolio advisory backtesting",
    )
    p.add_argument("--days", type=int, default=_DEFAULT_DAYS,
                   help="Trading days to replay (default: 90)")
    p.add_argument("--symbols", type=str, default=None,
                   help="Comma-separated extra symbols beyond config holdings")
    p.add_argument("--output-dir", type=str, default=str(_DEFAULT_OUTPUT_DIR),
                   help="Output directory (default: outputs/backtest)")
    p.add_argument("--window-days", type=str, default="1,3,7",
                   help="Resolution windows comma-separated (default: 1,3,7)")
    p.add_argument("--dry-run", action="store_true",
                   help="Simulate without writing files")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    try:
        windows = tuple(int(w.strip()) for w in args.window_days.split(",") if w.strip())
    except ValueError:
        print(f"ERROR: --window-days must be comma-separated integers, got: {args.window_days!r}")
        return 1

    from portfolio_automation.historical_replay.replay_data_loader import load_extra_symbols
    extra = load_extra_symbols(args.symbols) or None

    print()
    print("Historical Replay v1")
    print(f"  Days:       {args.days}")
    print(f"  Windows:    {windows}")
    print(f"  Output dir: {args.output_dir}")
    print(f"  Dry run:    {args.dry_run}")
    if extra:
        print(f"  Extra syms: {extra}")
    print()

    summary = run_replay(
        days=args.days,
        extra_symbols=extra,
        output_dir=Path(args.output_dir),
        window_days=windows,
        dry_run=args.dry_run,
    )

    # Non-zero exit only when pipeline produced nothing useful
    if summary.get("errors") and not summary.get("decisions_generated"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
