"""
Pattern-Improvement Loop — end-to-end driver  (additive | advisory-only | observe-only)

One command that chains Steps 1→4 of the loop so it is runnable end-to-end
instead of only as composable pieces:

  Step 1   load the system's REAL emitted signals (signal_sources) — a single
           watchlist_signals.json artifact, or the aggregated outputs/history
           snapshots (longer history = more out-of-sample folds).
  Steps 1b/3  run the POC simulation (run_poc) → outputs/backtest/
           poc_simulation_results.json (per-pattern, directional, per-regime).
  Step 2   compute per-signal OUT-OF-SAMPLE efficacy via walk-forward, grouped
           by the registry signal_id each signal scores against.
  Step 4   convert the OOS efficacy into SMALL, guardrailed weight *proposals*
           (tuning_proposals) → outputs/policy/signal_weight_proposals.json.

Step 5 (governed apply) is intentionally NOT invoked here. Applying approved
proposals is the protected, owner-gated path in backtesting/registry_apply.py
and stays inert — this driver only ever proposes.

Observe-only: reads signals + config/signal_registry.yaml read-only, runs the
existing FMPBacktester, and writes only the two review artifacts through the
governed safe writers. Touches no protected scoring/decision/allocation logic
and leaves the registry byte-identical. Offline (deterministic synthetic prices)
is the default and needs no FMP key; --live uses real FMP prices for real
forward outcomes.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any

from backtesting.direction_resolution import signal_direction
from backtesting.fmp_backtester import FMPBacktester
from backtesting.poc_simulation_harness import SyntheticPriceProvider, run_poc
from backtesting.signal_sources import (
    load_historical_signal_snapshots,
    load_signals_from_artifact,
)
from backtesting.tuning_proposals import propose_weight_changes, write_proposals
from backtesting.walk_forward import oos_window_status, walk_forward

_OBSERVE_ONLY = True  # hardcoded per repo observe-only policy (CLAUDE.md)
_DEFAULT_SIGNALS = "outputs/latest/watchlist_signals.json"
_DEFAULT_HISTORY = "outputs/history"
_DEFAULT_REGISTRY = "config/signal_registry.yaml"


def registry_signal_id(signal: dict) -> str:
    """Map a normalized signal to the registry ``signal_id`` it scores against.

    The STRONG_MOVE family is direction-resolved (Step 1b) to STRONG_MOVE_UP /
    STRONG_MOVE_DOWN — the registry keys are directional, the loaded family is
    not. All other pattern families pass through unchanged. Non-registry families
    (SIGNAL_SCORE, UNKNOWN) also pass through so propose_weight_changes flags them
    'unknown_signal' rather than silently dropping them.
    """
    pattern = str(signal.get("pattern") or "UNKNOWN").upper()
    if pattern == "STRONG_MOVE":
        return "STRONG_MOVE_DOWN" if signal_direction(signal) == "down" else "STRONG_MOVE_UP"
    return pattern


def per_signal_oos(
    signals: list[dict],
    bt: Any,
    *,
    forward_days: int = 10,
    train_days: int = 252,
    test_days: int = 63,
    step_days: int = 63,
    min_signals_per_fold: int = 30,
) -> list[dict]:
    """Group signals by registry signal_id and run walk-forward per group,
    returning one OUT-OF-SAMPLE efficacy entry per signal_id, shaped for
    ``propose_weight_changes`` ({signal_id, n, hit_rate, hit_rate_ci95,
    avg_return}) plus an ``oos_status``/``folds_ok`` annotation.

    A group whose OOS aggregate is 'insufficient' is still returned (n=0,
    hit_rate=None) so Step 4 flags it 'insufficient_evidence' rather than
    dropping it. Never raises (a group that errors is reported insufficient).
    """
    groups: dict[str, list[dict]] = {}
    for sig in signals:
        groups.setdefault(registry_signal_id(sig), []).append(sig)

    out: list[dict] = []
    for sid, group in sorted(groups.items()):
        try:
            wf = walk_forward(
                group, bt, train_days=train_days, test_days=test_days,
                step_days=step_days, forward_days=forward_days,
                min_signals_per_fold=min_signals_per_fold,
            )
            agg = wf.get("aggregate") or {}
        except Exception as exc:  # never let one group abort the loop
            agg = {"status": f"error:{exc}", "n": 0, "folds_ok": 0,
                   "hit_rate": None, "hit_rate_ci95": None, "avg_return": None}
        out.append({
            "signal_id": sid,
            "n": agg.get("n") or 0,
            "hit_rate": agg.get("hit_rate"),
            "hit_rate_ci95": agg.get("hit_rate_ci95"),
            "avg_return": agg.get("avg_return"),
            "oos_status": agg.get("status"),
            "folds_ok": agg.get("folds_ok", 0),
        })
    return out


def _load_signals(signals_source: str | None, history_dir: str | None) -> tuple[list[dict], str]:
    """Return (signals, source_label). history_dir wins when set."""
    if history_dir:
        return load_historical_signal_snapshots(history_dir), f"history:{history_dir}"
    path = signals_source or _DEFAULT_SIGNALS
    return load_signals_from_artifact(path), f"artifact:{path}"


def run_loop(
    *,
    signals_source: str | None = _DEFAULT_SIGNALS,
    history_dir: str | None = None,
    live: bool = False,
    seed: int = 42,
    forward_days: int = 10,
    train_days: int = 252,
    test_days: int = 63,
    step_days: int = 63,
    min_signals_per_fold: int = 30,
    registry_path: str = _DEFAULT_REGISTRY,
    min_n: int = 50,
    max_abs_delta: float = 0.05,
    write: bool = True,
    base_dir: str = "outputs",
) -> dict[str, Any]:
    """Run Steps 1→4 over one signal set and return a combined summary dict.

    Degrades to a status dict (never raises) on no signals or any unhandled error.
    """
    try:
        signals, source_label = _load_signals(signals_source, history_dir)
        if not signals:
            return {
                "observe_only": _OBSERVE_ONLY,
                "status": "no_signals",
                "source": source_label,
                "reason": "no signals loaded; nothing to simulate or propose",
            }

        # Steps 1/1b/3 — POC simulation metrics artifact (reuses run_poc).
        window = oos_window_status(signals, train_days=train_days, test_days=test_days,
                                   today=date.today())
        poc = run_poc(signals=signals, live=live, seed=seed, forward_days=forward_days,
                      write=write, base_dir=base_dir, oos_window=window)

        # Step 2 — per-signal OOS via walk-forward on the same kind of provider.
        if live:
            from fmp_client import FMPClient  # lazy: offline default needs no deps/keys
            provider: Any = FMPClient()
        else:
            provider = SyntheticPriceProvider(seed=seed)
        bt = FMPBacktester(provider, years_default=3)
        oos = per_signal_oos(
            signals, bt, forward_days=forward_days, train_days=train_days,
            test_days=test_days, step_days=step_days, min_signals_per_fold=min_signals_per_fold,
        )

        # Step 4 — bounded, guardrailed weight proposals (writes POLICY artifact).
        proposals = propose_weight_changes(oos, registry_path=registry_path,
                                           min_n=min_n, max_abs_delta=max_abs_delta)
        proposals["source"] = source_label
        proposals["mode"] = poc.get("mode")
        proposals["step_5_status"] = "inert_owner_gated"  # apply path not invoked here
        if write:
            write_proposals(proposals, base_dir=base_dir)

        perf = poc.get("performance") or {}
        return {
            "observe_only": _OBSERVE_ONLY,
            "status": "ok",
            "mode": poc.get("mode"),
            "source": source_label,
            "n_signals": len(signals),
            "poc": {
                "evaluated": perf.get("evaluated"),
                "hit_rate": perf.get("hit_rate"),
                "calibration_slope": (poc.get("calibration") or {}).get("calibration_slope"),
            },
            "oos_window": window,
            "oos_groups": oos,
            "proposals_summary": proposals.get("summary"),
        }
    except Exception as exc:  # degrade, never break the operator's run
        return {"observe_only": _OBSERVE_ONLY, "status": "error", "error": str(exc)}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m backtesting.run_loop",
        description="Run the Pattern-Improvement Loop end-to-end (Steps 1→4, observe-only). "
                    "Step 5 (governed apply) stays inert.",
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--signals-source", default=_DEFAULT_SIGNALS,
                     help=f"watchlist_signals.json artifact to replay (default: {_DEFAULT_SIGNALS})")
    src.add_argument("--history", dest="history_dir", nargs="?", const=_DEFAULT_HISTORY, default=None,
                     help=f"aggregate dated snapshots under this dir instead (default dir: {_DEFAULT_HISTORY})")
    p.add_argument("--live", action="store_true",
                   help="use real FMP prices for real forward outcomes (needs FMP_API_KEY)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--forward-days", type=int, default=10)
    p.add_argument("--train-days", type=int, default=252)
    p.add_argument("--test-days", type=int, default=63)
    p.add_argument("--step-days", type=int, default=63)
    p.add_argument("--min-signals-per-fold", type=int, default=30)
    p.add_argument("--registry", dest="registry_path", default=_DEFAULT_REGISTRY)
    p.add_argument("--min-n", type=int, default=50)
    p.add_argument("--max-abs-delta", type=float, default=0.05)
    p.add_argument("--no-write", action="store_true", help="compute only; write no artifacts")
    return p


def main(argv: list[str] | None = None) -> int:
    a = _build_parser().parse_args(argv)
    out = run_loop(
        signals_source=a.signals_source, history_dir=a.history_dir, live=a.live,
        seed=a.seed, forward_days=a.forward_days, train_days=a.train_days,
        test_days=a.test_days, step_days=a.step_days,
        min_signals_per_fold=a.min_signals_per_fold, registry_path=a.registry_path,
        min_n=a.min_n, max_abs_delta=a.max_abs_delta, write=not a.no_write,
    )
    if out["status"] == "ok":
        s = out["proposals_summary"] or {}
        print("[run_loop] mode={m} source={src} n_signals={n} | poc evaluated={e} "
              "hit_rate={h}% calib_slope={c}".format(
                  m=out["mode"], src=out["source"], n=out["n_signals"],
                  e=out["poc"]["evaluated"], h=out["poc"]["hit_rate"],
                  c=out["poc"]["calibration_slope"]))
        print("[run_loop] proposals: evaluated={ev} proposed={pr} insufficient={ins} "
              "no_edge={ne} unknown={uk}".format(
                  ev=s.get("evaluated"), pr=s.get("proposed_count"),
                  ins=s.get("insufficient_evidence"), ne=s.get("no_significant_edge"),
                  uk=s.get("unknown_signal")))
        if not a.no_write:
            print("[run_loop] wrote outputs/backtest/poc_simulation_results.{json,md} (HISTORICAL) "
                  "+ outputs/policy/signal_weight_proposals.json (POLICY)")
        print("[run_loop] Step 5 (apply) NOT invoked — proposals are a review artifact only.")
        return 0
    print("[run_loop] status={st}: {detail}".format(
        st=out["status"], detail=out.get("reason") or out.get("error") or ""))
    # no_signals is an expected degraded state, not a driver failure.
    return 0 if out["status"] == "no_signals" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
