"""
Validation runner for Phase 1 P&L advisors.

Runs the three observe-only advisors against the repo's current state and
prints a one-line summary per advisor plus the artifact paths written.

Usage on VPS:
    source .venv/bin/activate
    python scripts/validate_phase1_advisors.py

Exit code is 0 when every advisor runs without raising; non-zero when any
advisor raises an exception. The advisors themselves never raise — they
degrade to status="insufficient_data" — so a non-zero exit indicates a
genuine import or environment problem.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from portfolio_automation.exit_advisor import run_exit_advisor
from portfolio_automation.cash_deployment_plan import run_cash_deployment_plan
from portfolio_automation.correlation_risk_advisor import run_correlation_risk_advisor


def main() -> int:
    base = REPO / "outputs"
    failed = False

    for label, fn in [
        ("exit_advisor",            lambda: run_exit_advisor(REPO, fmp_client=None, base_dir=base)),
        ("cash_deployment_plan",    lambda: run_cash_deployment_plan(REPO, base_dir=base)),
        ("correlation_risk_advisor", lambda: run_correlation_risk_advisor(REPO, fmp_client=None, base_dir=base)),
    ]:
        try:
            plan = fn()
            print(f"{label}: {plan.get('summary_line', '(no summary)')}")
        except Exception as exc:
            print(f"{label}: FAILED — {exc}")
            failed = True

    print()
    print("Artifacts:")
    for name in (
        "exit_advisor.json", "exit_advisor.md",
        "cash_deployment_plan.json", "cash_deployment_plan.md",
        "correlation_risk_advisor.json", "correlation_risk_advisor.md",
    ):
        path = base / "latest" / name
        size = path.stat().st_size if path.exists() else "—"
        print(f"  {path}  ({size} bytes)" if path.exists() else f"  {path}  MISSING")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
