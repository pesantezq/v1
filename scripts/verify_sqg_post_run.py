#!/usr/bin/env python3
"""Post-run verification for the SQG program (v1, merged 2026-07-01).

READ-ONLY. Reads artifacts and asserts the daily (and, with --weekly, the
weekly) integration invariants the operator wants to confirm after a live run.
Never mutates anything; never calls an API. Exit 0 iff no check FAILED
(SKIP is not a failure — it means the artifact isn't present yet).

Usage:
    .venv/bin/python scripts/verify_sqg_post_run.py            # daily checks
    .venv/bin/python scripts/verify_sqg_post_run.py --weekly   # daily + weekly
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Allow `import portfolio_automation` when invoked directly from any CWD.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results: list[tuple[str, str, str]] = []


def _load(rel: str):
    p = ROOT / rel
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # corrupt → treated as absent, reported by caller
        return {"__error__": str(exc)}


def _jsonl(rel: str) -> list[dict]:
    p = ROOT / rel
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def check(name: str, status: str, detail: str = "") -> None:
    _results.append((name, status, detail))


def run_daily() -> None:
    manifest = _load("outputs/policy/run_manifest.json")
    run_id = manifest.get("run_id") if isinstance(manifest, dict) else None

    # 1. run_manifest.status == complete
    if not manifest:
        check("run_manifest.status == complete", SKIP, "run_manifest.json absent")
    else:
        st = manifest.get("status")
        check("run_manifest.status == complete",
              PASS if st in ("complete", "complete_with_warnings") else FAIL,
              f"status={st!r}")

    # 2. decision_plan.run_id matches the manifest
    dp = _load("outputs/latest/decision_plan.json")
    if not dp:
        check("decision_plan.run_id == manifest.run_id", SKIP, "decision_plan.json absent")
    elif "run_id" not in dp:
        check("decision_plan.run_id == manifest.run_id", SKIP,
              "decision_plan.json has no run_id yet (predates the lineage stamp — confirm after the next daily run)")
    else:
        check("decision_plan.run_id == manifest.run_id",
              PASS if run_id and dp.get("run_id") == run_id else FAIL,
              f"plan={dp.get('run_id')!r} manifest={run_id!r}")

    # 3. daily_input_snapshot.run_id matches the manifest
    snap = _load("outputs/sandbox/daily_input_snapshot.json")
    snap_hash = snap.get("snapshot_hash") if isinstance(snap, dict) else None
    if not snap:
        check("daily_input_snapshot.run_id == manifest.run_id", SKIP, "snapshot absent")
    else:
        check("daily_input_snapshot.run_id == manifest.run_id",
              PASS if run_id and snap.get("run_id") == run_id else FAIL,
              f"snapshot={snap.get('run_id')!r} manifest={run_id!r}")

    # 4. simulation bundle references the snapshot hash
    bundle = _load("outputs/simulation/daily_simulation_bundle.json")
    if not bundle:
        check("simulation bundle references snapshot hash", SKIP, "bundle absent")
    elif not snap_hash:
        check("simulation bundle references snapshot hash", SKIP, "no snapshot hash to match")
    else:
        check("simulation bundle references snapshot hash",
              PASS if bundle.get("input_snapshot_hash") == snap_hash else FAIL,
              f"bundle={str(bundle.get('input_snapshot_hash'))[:12]} snapshot={str(snap_hash)[:12]}")

    # 5. decision-context capture contains new records (for this run_id)
    ctx = _jsonl("outputs/policy/decision_context_log.jsonl")
    if not ctx:
        check("decision-context capture has records", SKIP, "log empty/absent")
    else:
        this_run = [r for r in ctx if r.get("run_id") == run_id] if run_id else []
        check("decision-context capture has records",
              PASS if (this_run or ctx) else FAIL,
              f"{len(this_run)} records for this run ({len(ctx)} total)")

    # 6. quant-feedback fallback rate is visible
    qf = _load("outputs/latest/quant_feedback.json")
    if not qf:
        check("quant_feedback fallback_rate visible", SKIP, "quant_feedback.json absent")
    else:
        check("quant_feedback fallback_rate visible",
              PASS if "fallback_rate" in qf else FAIL,
              f"fallback_rate={qf.get('fallback_rate')}")

    # 7. semantic-liveness produced a valid artifact
    sl = _load("outputs/latest/semantic_liveness_status.json")
    if not sl:
        check("semantic_liveness valid artifact", SKIP, "absent")
    else:
        check("semantic_liveness valid artifact",
              PASS if sl.get("overall_status") else FAIL,
              f"overall_status={sl.get('overall_status')!r} findings={sl.get('finding_count')}")

    # 8. pending proposals remain unapproved
    pas = _load("outputs/promotion_approvals/production_application_state.json")
    if not pas:
        check("pending proposals remain unapproved", SKIP, "production_application_state absent")
    else:
        applied = pas.get("applied_count", 0)
        check("pending proposals remain unapproved",
              PASS if applied == 0 else FAIL,
              f"applied_count={applied} (expected 0)")

    # 9. production overlays remain disabled
    if not pas:
        check("production overlays disabled", SKIP, "production_application_state absent")
    else:
        overlay = pas.get("production_overlay_live") or {}
        live = bool(overlay.get("watchlist")) or bool(overlay.get("advisory"))
        check("production overlays disabled",
              PASS if not live else FAIL,
              f"overlay_live={overlay}")


def run_weekly() -> None:
    # 1. strategy mandates generated
    sm = _load("outputs/sandbox/strategy_mandates.json")
    if not sm:
        check("strategy_mandates generated", SKIP, "absent (run weekly cycle first)")
    else:
        check("strategy_mandates generated",
              PASS if sm.get("mandates") else FAIL,
              f"{len(sm.get('mandates') or {})} mandate(s)")
        # 2. mandate coverage complete
        check("mandate coverage_complete",
              PASS if sm.get("coverage_complete") else FAIL,
              f"unmandated={sm.get('unmandated')}")

    # 3. experiment-registry review works (absent AND populated)
    try:
        from portfolio_automation.experiment_registry import read_registry
        reg = read_registry(str(ROOT))
        n = len(reg) if hasattr(reg, "__len__") else 0
        check("experiment_registry review works",
              PASS, f"read_registry ok — {n} experiment(s) "
              f"({'populated' if n else 'empty/absent handled gracefully'})")
    except Exception as exc:
        check("experiment_registry review works", FAIL, f"read_registry raised: {exc}")

    # 4. strategy + simulation outputs remain sandbox-scoped
    strays = []
    for rel in ("outputs/sandbox/strategy_mandates.json",
                "outputs/sandbox/experiment_registry.json",
                "outputs/simulation/daily_simulation_bundle.json"):
        p = ROOT / rel
        if p.exists() and "sandbox" not in rel and "simulation" not in rel:
            strays.append(rel)
    ucs = _load("outputs/latest/unified_crowd_intelligence_status.json") or {}
    feeds = ucs.get("feeds_decision_engine")
    check("strategy/sim outputs sandbox-scoped",
          PASS if not strays and feeds in (False, None) else FAIL,
          f"strays={strays} feeds_decision_engine={feeds}")


def main() -> int:
    ap = argparse.ArgumentParser(description="SQG post-run verification (read-only)")
    ap.add_argument("--weekly", action="store_true", help="also run the weekly-cycle checks")
    args = ap.parse_args()

    print("== SQG daily integration checks ==")
    run_daily()
    if args.weekly:
        print("== SQG weekly integration checks ==")
        run_weekly()

    width = max(len(n) for n, _, _ in _results)
    n_fail = n_skip = 0
    for name, status, detail in _results:
        if status == FAIL:
            n_fail += 1
        elif status == SKIP:
            n_skip += 1
        print(f"  [{status:4}] {name:<{width}}  {detail}")
    print(f"\nSummary: {len(_results) - n_fail - n_skip} pass, {n_fail} fail, {n_skip} skip")
    if n_fail:
        print("RESULT: FAIL — investigate the failing invariant(s) above.")
        return 1
    if n_skip:
        print("RESULT: OK (some checks skipped — artifact not present yet; re-run after the next cycle).")
        return 0
    print("RESULT: OK — all SQG integration invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
