"""Bounded validator for the daily-memo decision-coherence layer.

Work order: quant.daily_memo_coherence. Runs the read-only reconciliation
(``portfolio_automation.memo_coherence.run_memo_coherence``) against the current
``outputs/latest/`` artifact set and prints a human-readable validation report:

  * memo coherence status (ok / warning / degraded)
  * funded vs unfunded capital
  * contradictions detected (resolved + unresolved)
  * ranking ties / default-fallback priorities
  * stale-artifact usage
  * crowd-state consistency (attention overlap vs classified state)
  * investor/operator section integrity (does the rendered memo put the
    investor core before the operator appendix)

Governance / honesty
--------------------
* This tool is READ-ONLY: it executes no trades and mutates no production state.
* ``--write`` only re-emits ``outputs/latest/memo_coherence.{json,md}`` via the
  same governed writer the pipeline uses; pass nothing to dry-run.
* All values are copied from already-produced artifacts; nothing is fabricated.

Usage:
    python -m tools.validate_daily_memo_coherence [ROOT] [--write]
Exit code is non-zero only on a hard error, never on a degraded/warning status
(degraded memo coherence is a finding to surface, not a tool failure).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.memo_coherence import run_memo_coherence  # noqa: E402


def _fmt_money(v) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "n/a"


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    write = "--write" in argv
    root = args[0] if args else "."

    try:
        result = run_memo_coherence(root, write_files=write)
    except Exception as exc:  # pragma: no cover - hard error path
        print(f"ERROR: validator failed to run: {exc}")
        return 1

    rec = result.get("reconciliation", {})
    fund = result.get("funding", {})
    rank = result.get("ranking", {})
    hr = result.get("hit_rate", {})
    crowd = result.get("crowd", {})
    overlap = result.get("overlap", {})
    fresh = result.get("freshness", {})

    print("=" * 64)
    print("DAILY MEMO COHERENCE VALIDATION")
    print("=" * 64)
    print(f"Status            : {result.get('coherence_status', 'unknown').upper()}")
    print(f"Snapshot          : {result.get('snapshot_timestamp')}")
    print(f"Max source skew   : {fresh.get('max_skew_minutes')} min")
    print(f"Stale sources     : {', '.join(fresh.get('stale_sources') or []) or 'none'}")
    print("-" * 64)

    print("FUNDING")
    if fund.get("available"):
        print(f"  Available cash  : {_fmt_money(fund.get('available_cash'))}"
              + ("  (below 5% reserve)" if fund.get("below_safety_floor") else ""))
        print(f"  Max deployable  : {_fmt_money(fund.get('max_deployable'))} "
              f"(cash {_fmt_money(fund.get('deployable_from_cash'))} + "
              f"incoming {_fmt_money(fund.get('deployable_from_incoming'))})")
        print(f"  Funded          : {fund.get('funded_count')} actions / "
              f"{_fmt_money(fund.get('funded_capital'))}")
        print(f"  Blocked/deferred: {fund.get('blocked_count')} actions / "
              f"unfunded {_fmt_money(fund.get('unfunded_capital'))}")
    else:
        print(f"  unavailable ({fund.get('reason')}) — degraded funding")
    print("-" * 64)

    print("RANKING")
    print(f"  Total actions   : {rank.get('total_actions')}")
    print(f"  Distinct priors : {rank.get('distinct_priorities')}")
    print(f"  Default-fallback: {rank.get('default_fallback_count')} "
          f"(priority {rank.get('default_fallback_priority')})")
    print(f"  Tie-break rule  : {rank.get('tie_break_rule')}")
    print("-" * 64)

    print("HIT-RATE (neutral band)")
    if hr.get("available"):
        print(f"  Directional acc : {hr.get('directional_accuracy_pct')}% "
              f"(correct {hr.get('correct')} / incorrect {hr.get('incorrect')} / "
              f"neutral {hr.get('neutral')})")
        print(f"  Neutral band    : +/-{hr.get('neutral_band_pct')}%  "
              f"(raw calibration {hr.get('raw_calibration_hit_rate')})")
    else:
        print("  no resolved outcomes")
    print("-" * 64)

    print("OVERLAP")
    print(f"  Eff. ind. bets  : {overlap.get('effective_independent_bets')}")
    print(f"  ETF lookthrough : {'yes' if overlap.get('etf_lookthrough_available') else 'degraded (' + str(overlap.get('etf_lookthrough_reason')) + ')'}")
    for c in (overlap.get("clusters") or [])[:5]:
        flag = " *same-thesis*" if c.get("multiple_proposed_same_thesis") else ""
        print(f"    cluster: {', '.join(c.get('members') or [])} [{c.get('basis')}]{flag}")
    print("-" * 64)

    print("CROWD")
    if crowd.get("available"):
        print(f"  confirmed(attn) : {len(crowd.get('cross_source_confirmed') or [])}  "
              f"divergent {len(crowd.get('divergent') or [])}  "
              f"insufficient {crowd.get('insufficient_data_count')}")
        print(f"  classified buy  : {crowd.get('any_classified_buy_state')}  "
              f"social_sentiment {crowd.get('social_sentiment_status')}  "
              f"production_eligible {crowd.get('production_eligible')}")
    else:
        print("  unavailable (non-blocking)")
    print("-" * 64)

    print("CONTRADICTIONS")
    issues = rec.get("issues", [])
    if not issues:
        print("  none")
    for i in issues:
        tag = "RESOLVED" if i.get("resolved") else i.get("severity", "?").upper()
        print(f"  [{tag:8s}] {i.get('id')}: {i.get('message')}")
    print("-" * 64)

    # Investor/operator section integrity check against the rendered memo.
    try:
        from watchlist_scanner.daily_memo import generate_daily_memo
        txt, _ = generate_daily_memo(root=root, write_files=False)
        i_pos = txt.find("TODAY'S POSTURE")
        a_pos = txt.find("OPERATOR / SYSTEM APPENDIX")
        ok = i_pos >= 0 and a_pos >= 0 and i_pos < a_pos
        print(f"SECTION INTEGRITY : investor core before operator appendix = {ok}")
    except Exception as exc:
        print(f"SECTION INTEGRITY : could not render memo ({exc})")
    print("=" * 64)
    print("Advisory only — no trades executed. Production remains human-gated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
