"""
CLI entry point for the policy evaluator.

Usage:
    py -m policy_evaluator                       # evaluate + write reports
    py -m policy_evaluator --dry-run             # evaluate only, no writes
    py -m policy_evaluator --history PATH        # use custom history file
    py -m policy_evaluator --summary             # print memo summary to stdout
    py -m policy_evaluator --outcomes            # run outcome attribution too
    py -m policy_evaluator --outcomes --db PATH  # use custom portfolio.db

Runs independently of the main portfolio system — reads only
outputs/policy/recommendation_history.jsonl (or the path you supply).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate recommendation quality from history JSONL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--history",
        type=str,
        default=None,
        help="Path to recommendation_history.jsonl (default: outputs/policy/recommendation_history.jsonl)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Directory to write reports (default: outputs/policy/)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate but do not write output files",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print a short memo-style summary to stdout",
    )
    parser.add_argument(
        "--json",
        dest="print_json",
        action="store_true",
        help="Print full evaluation JSON to stdout",
    )
    parser.add_argument(
        "--outcomes",
        action="store_true",
        help="Also run outcome attribution (links recs to forward portfolio returns)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to portfolio.db for outcome attribution (default: data/portfolio.db)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    from policy_evaluator.evaluator import evaluate_history
    from policy_evaluator.report_writer import write_evaluation_reports, build_memo_summary

    history_path = Path(args.history) if args.history else None
    output_dir = Path(args.output_dir) if args.output_dir else None

    result = evaluate_history(history_path=history_path)

    if args.summary:
        print(build_memo_summary(result))

    if args.print_json:
        print(json.dumps(result.to_dict(), indent=2))

    if not args.dry_run:
        ok = write_evaluation_reports(result, policy_dir=output_dir)
        if not ok:
            return 1

    print(
        f"Policy evaluation complete: {result.total_records} records, "
        f"{result.total_runs} runs ({result.date_range.get('first')} → "
        f"{result.date_range.get('last')})"
    )

    # --- Optional: outcome attribution ---
    if args.outcomes:
        from policy_evaluator.outcome_attributor import run_outcome_attribution
        from policy_evaluator.outcome_writer import write_outcome_reports, build_outcome_memo

        db_path = Path(args.db) if args.db else None
        outcome_result = run_outcome_attribution(
            history_path=history_path,
            db_path=db_path,
        )

        if args.summary:
            print(build_outcome_memo(outcome_result))

        if args.print_json:
            print(json.dumps(outcome_result.to_dict(), indent=2))

        if not args.dry_run:
            ok = write_outcome_reports(outcome_result, policy_dir=output_dir)
            if not ok:
                return 1

        print(
            f"Outcome attribution complete: {outcome_result.attributable_records}/"
            f"{outcome_result.total_records} records attributed "
            f"(coverage {(outcome_result.coverage_rate or 0)*100:.0f}%)"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
