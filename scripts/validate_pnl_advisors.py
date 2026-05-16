"""
Unified validation runner for all P&L Maximization Roadmap advisors
(Phase 1 + Phase 2 + Phase 3).

Runs every observe-only advisor against the repo's current state, prints
one summary line per advisor, and lists every artifact written.

Usage on VPS:
    source .venv/bin/activate
    python scripts/validate_pnl_advisors.py

Optional flag:
    --strict   exit non-zero if any advisor reports status="insufficient_data"
               (default: exit zero on insufficient_data; only raise on exceptions)

Exit code semantics:
    0  every advisor ran without raising
    1  one or more advisors raised exceptions (real bug or env issue)
    2  with --strict, one or more advisors reported insufficient_data

Replaces the older scripts/validate_phase1_advisors.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fmp_client import FMPClient  # type: ignore  # noqa: E402

from portfolio_automation.alpha_attribution_report import run_alpha_attribution_report  # noqa: E402
from portfolio_automation.cash_deployment_plan import run_cash_deployment_plan  # noqa: E402
from portfolio_automation.correlation_risk_advisor import run_correlation_risk_advisor  # noqa: E402
from portfolio_automation.earnings_gate import run_earnings_gate  # noqa: E402
from portfolio_automation.exit_advisor import run_exit_advisor  # noqa: E402
from portfolio_automation.kelly_sizing_advisor import run_kelly_sizing_advisor  # noqa: E402
from portfolio_automation.tax_harvest_advisor import run_tax_harvest_advisor  # noqa: E402
from portfolio_automation.vol_regime_advisor import run_vol_regime_advisor  # noqa: E402


# Mapping: label -> (callable_factory, expected_artifact_basename)
def _build_advisor_calls(fmp):
    return [
        ("exit_advisor",
            lambda: run_exit_advisor(REPO, fmp_client=fmp, base_dir=REPO / "outputs"),
            "exit_advisor"),
        ("cash_deployment_plan",
            lambda: run_cash_deployment_plan(REPO, base_dir=REPO / "outputs"),
            "cash_deployment_plan"),
        ("correlation_risk_advisor",
            lambda: run_correlation_risk_advisor(REPO, fmp_client=fmp, base_dir=REPO / "outputs"),
            "correlation_risk_advisor"),
        ("earnings_gate",
            lambda: run_earnings_gate(REPO, earnings_lookup=None, base_dir=REPO / "outputs"),
            "earnings_gate"),
        ("vol_regime_advisor",
            lambda: run_vol_regime_advisor(REPO, fmp_client=fmp, base_dir=REPO / "outputs"),
            "vol_regime_advisor"),
        ("tax_harvest_advisor",
            lambda: run_tax_harvest_advisor(REPO, fmp_client=fmp, base_dir=REPO / "outputs"),
            "tax_harvest_advisor"),
        ("kelly_sizing_advisor",
            lambda: run_kelly_sizing_advisor(REPO, base_dir=REPO / "outputs"),
            "kelly_sizing_advisor"),
        ("alpha_attribution_report",
            lambda: run_alpha_attribution_report(REPO, base_dir=REPO / "outputs"),
            "alpha_attribution_report"),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="P&L advisors validation runner")
    parser.add_argument("--strict", action="store_true",
                        help="exit code 2 on any insufficient_data status")
    parser.add_argument("--no-fmp", action="store_true",
                        help="run without FMPClient (degrades to insufficient_data)")
    args = parser.parse_args()

    fmp = None
    if not args.no_fmp:
        try:
            fmp = FMPClient()
        except Exception as exc:
            print(f"WARN: FMPClient unavailable; running degraded — {exc}")

    failed = False
    saw_insufficient = False

    print("# P&L Advisor Validation")
    print()
    print(f"Repo: {REPO}")
    print(f"FMP client: {'enabled' if fmp is not None else 'disabled'}")
    print()

    for label, fn, _ in _build_advisor_calls(fmp):
        try:
            plan = fn()
            summary = plan.get("summary_line", "(no summary)")
            status = plan.get("status", "(no status)")
            print(f"[{label}] {summary}")
            if "insufficient" in str(status).lower():
                saw_insufficient = True
        except Exception as exc:
            print(f"[{label}] FAILED — {type(exc).__name__}: {exc}")
            failed = True

    print()
    print("# Artifacts")
    base = REPO / "outputs" / "latest"
    expected = [
        "exit_advisor.json", "exit_advisor.md",
        "cash_deployment_plan.json", "cash_deployment_plan.md",
        "correlation_risk_advisor.json", "correlation_risk_advisor.md",
        "earnings_gate.json", "earnings_gate.md",
        "vol_regime_advisor.json", "vol_regime_advisor.md",
        "tax_harvest_advisor.json", "tax_harvest_advisor.md",
        "kelly_sizing_advisor.json", "kelly_sizing_advisor.md",
        "alpha_attribution_report.json", "alpha_attribution_report.md",
    ]
    for name in expected:
        path = base / name
        if path.exists():
            print(f"  OK   {path}  ({path.stat().st_size} bytes)")
        else:
            print(f"  MISS {path}")
            failed = True

    if failed:
        return 1
    if args.strict and saw_insufficient:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
