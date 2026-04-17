from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from config.loader import load_runtime_config_dict
from watchlist_scanner.config_optimizer import (
    analyze_outcomes_and_suggest_config,
    load_state_for_optimization,
    write_config_suggestions,
)
from watchlist_scanner.config_report import build_config_report, write_config_report


logger = logging.getLogger("watchlist_scanner.optimize_config")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="watchlist_scanner.optimize_config",
        description="Analyze resolved watchlist alert outcomes and suggest config tuning changes",
    )
    parser.add_argument("--config", default="config.json", help="Path to config.json or config/ directory")
    parser.add_argument("--profile", default=None, help="Optional structured config profile name")
    parser.add_argument("--db-path", default="data/portfolio.db", help="Path to the shared state database")
    parser.add_argument("--limit", type=int, default=500, help="Max resolved outcomes to analyze")
    parser.add_argument("--write-history", action="store_true", help="Write suggestions JSON into config/history when using structured config")
    parser.add_argument("--report", action="store_true", help="Build a ranked config calibration report and write it to config/history when possible")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    resolved_config = load_runtime_config_dict(args.config, profile=args.profile, record_history=False)
    rows = load_state_for_optimization(args.db_path, limit=args.limit)
    suggestions = analyze_outcomes_and_suggest_config(rows, resolved_config)
    if args.report:
        report = build_config_report(
            suggestions.get("suggestions", []),
            suggestions.get("summary", {}),
            profile=str(suggestions.get("profile") or "base"),
            generated_at=str(suggestions.get("generated_at") or ""),
        )
        suggestions["report"] = report

    written_path = None
    if args.write_history:
        written_path = write_config_suggestions(suggestions, config_path=args.config)
        if written_path:
            suggestions["written_path"] = written_path
    if args.report:
        report_paths = write_config_report(suggestions["report"], config_path=args.config)
        if report_paths:
            suggestions["report_paths"] = report_paths

    print(json.dumps(suggestions, indent=2, default=str))


if __name__ == "__main__":
    main()
