"""
Active Simulation / Test Lane.

This is the lane the operator wants ACTIVE: experimental advisory, watchlist,
crowd, and discovery logic is applied here and *allowed to change simulation
outputs*. Nothing here touches production — every write lands in the SANDBOX
namespace, and the only path to production is the gated promotion workflow.

Design:
  * Pure, deterministic experiments transform a *baseline* (a snapshot of what
    production looks like today) into a *simulated* view, emitting one
    SimulationCandidate per change with before/after, evidence, risk, confidence,
    and data-quality fields.
  * Experiments are injectable (``experiments=`` / ``baseline=``) so tests can
    prove the lane actively changes outputs without any real artifacts present.
  * Defaults read whatever production/sandbox artifacts exist (graceful) and run
    the built-in experiment set.

The lane writes:
  * outputs/sandbox/sim_governance/simulation_candidates.json
  * outputs/sandbox/sim_governance/simulated_watchlist.json   (actively changed)
  * outputs/sandbox/sim_governance/simulated_advisory.json    (actively changed)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.sim_governance import schemas as S

logger = logging.getLogger("stockbot.sim_governance.simulation_lane")

# An experiment is baseline-dict -> list[SimulationCandidate].
Experiment = Callable[[dict], list[S.SimulationCandidate]]

_SANDBOX_SUBDIR = "sim_governance"


# ---------------------------------------------------------------------------
# Baseline loading (graceful; production is the "before")
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_production_baseline(root: Path) -> dict:
    """Snapshot what production looks like today (the 'before' for comparisons).

    Tolerant: any missing artifact degrades to an empty section so the lane runs
    in any environment. Returns:
      {"watchlist": [tickers], "advisory": [picks], "crowd": {ticker: context}}
    """
    root = Path(root)
    out: dict[str, Any] = {"watchlist": [], "advisory": [], "crowd": {}}

    # Production watchlist (config + extended). Best-effort.
    cfg = _read_json(root / "config.json") or {}
    pf = cfg.get("portfolio", {}) if isinstance(cfg, dict) else {}
    wl = pf.get("watchlist") if isinstance(pf, dict) else None
    if isinstance(wl, list):
        out["watchlist"] = [str(t).upper() for t in wl if t]

    # Production advisory (decision plan picks).
    plan = _read_json(root / "outputs" / "latest" / "decision_plan.json")
    if isinstance(plan, dict):
        decisions = plan.get("decisions") or plan.get("plan") or []
    elif isinstance(plan, list):
        decisions = plan
    else:
        decisions = []
    advisory = []
    for d in decisions if isinstance(decisions, list) else []:
        if isinstance(d, dict) and d.get("symbol"):
            advisory.append({
                "symbol": str(d["symbol"]).upper(),
                "decision": d.get("decision") or d.get("action"),
                "rank": d.get("final_rank_score") or d.get("rank"),
            })
    out["advisory"] = advisory
    return out


# ---------------------------------------------------------------------------
# Built-in experiments — each demonstrably changes the simulated view.
# ---------------------------------------------------------------------------


def experiment_watchlist_discovery_adds(baseline: dict) -> list[S.SimulationCandidate]:
    """Propose adding discovery candidates not already on the production watchlist."""
    prod_wl = {str(t).upper() for t in baseline.get("watchlist", [])}
    cands: list[S.SimulationCandidate] = []
    for item in baseline.get("discovery_candidates", []) or []:
        sym = str(item.get("symbol", "")).upper()
        if not sym or sym in prod_wl:
            continue
        score = float(item.get("score", item.get("corroboration_score", 0.0)) or 0.0)
        cid = S.make_candidate_id(S.PROPOSAL_WATCHLIST_ADD, sym, salt=str(round(score, 4)))
        cands.append(S.SimulationCandidate(
            candidate_id=cid,
            workflow=S.WORKFLOW_WATCHLIST,
            proposal_type=S.PROPOSAL_WATCHLIST_ADD,
            symbol=sym,
            what_changed=f"Add {sym} to the watchlist",
            why_changed=item.get("reason", "Surfaced by discovery lane with corroborating evidence"),
            source_evidence=list(item.get("evidence", [])) or ["outputs/sandbox/discovery"],
            production_baseline=None,
            simulated_value={"symbol": sym, "tags": item.get("tags", []), "score": score},
            risk_impact=item.get("risk_impact", "low"),
            confidence=score,
            data_quality=item.get("data_quality", "ok"),
            ready_for_production_review=score >= 0.80,
            proposed_production_change={"op": "add", "symbol": sym, "tags": item.get("tags", []),
                                        "rank": item.get("rank")},
        ))
    return cands


def experiment_watchlist_rerank(baseline: dict) -> list[S.SimulationCandidate]:
    """Re-rank the simulated watchlist by a simulated signal (crowd velocity)."""
    crowd = baseline.get("crowd", {}) or {}
    cands: list[S.SimulationCandidate] = []
    for entry in baseline.get("watchlist_ranked", []) or []:
        sym = str(entry.get("symbol", "")).upper()
        cur = entry.get("rank")
        vel = float((crowd.get(sym, {}) or {}).get("velocity", 0.0))
        if not sym or cur is None or vel <= 0:
            continue
        new_rank = max(1, int(cur) - 1) if vel >= 1.0 else cur
        if new_rank == cur:
            continue
        cid = S.make_candidate_id(S.PROPOSAL_WATCHLIST_RANK, sym, salt=f"{cur}->{new_rank}")
        cands.append(S.SimulationCandidate(
            candidate_id=cid,
            workflow=S.WORKFLOW_WATCHLIST,
            proposal_type=S.PROPOSAL_WATCHLIST_RANK,
            symbol=sym,
            what_changed=f"Re-rank {sym} {cur} -> {new_rank}",
            why_changed=f"Crowd-velocity z-score {vel:.2f} supports a higher rank",
            source_evidence=["outputs/sandbox/crowd_radar"],
            production_baseline=cur,
            simulated_value=new_rank,
            risk_impact="low",
            confidence=min(1.0, vel / 2.0),
            data_quality="ok",
            ready_for_production_review=vel >= 1.5,
            proposed_production_change={"op": "rank", "symbol": sym, "rank": new_rank},
        ))
    return cands


def experiment_advisory_crowd_context(baseline: dict) -> list[S.SimulationCandidate]:
    """Attach a crowd-context overlay to advisory picks (observe-style context)."""
    crowd = baseline.get("crowd", {}) or {}
    cands: list[S.SimulationCandidate] = []
    for pick in baseline.get("advisory", []) or []:
        sym = str(pick.get("symbol", "")).upper()
        ctx = crowd.get(sym)
        if not sym or not ctx:
            continue
        cid = S.make_candidate_id(S.PROPOSAL_CROWD_CONTEXT, sym, salt=str(ctx.get("state", "")))
        cands.append(S.SimulationCandidate(
            candidate_id=cid,
            workflow=S.WORKFLOW_ADVISORY,
            proposal_type=S.PROPOSAL_CROWD_CONTEXT,
            symbol=sym,
            what_changed=f"Attach crowd context '{ctx.get('state')}' to {sym} advisory line",
            why_changed="Crowd-radar state adds public-knowledge context to the advisory pick",
            source_evidence=["outputs/sandbox/crowd_radar"],
            production_baseline={"symbol": sym, "crowd_context": None},
            simulated_value={"symbol": sym, "crowd_context": ctx.get("state")},
            risk_impact="low",
            confidence=float(ctx.get("confidence", 0.5)),
            data_quality="ok",
            ready_for_production_review=bool(ctx.get("confirmed")),
            proposed_production_change={"op": "context", "symbol": sym,
                                        "crowd_context": ctx.get("state")},
        ))
    return cands


DEFAULT_EXPERIMENTS: list[Experiment] = [
    experiment_watchlist_discovery_adds,
    experiment_watchlist_rerank,
    experiment_advisory_crowd_context,
]


# ---------------------------------------------------------------------------
# Active materialization — the simulated views that experiments mutate.
# ---------------------------------------------------------------------------


def materialize_simulated_views(baseline: dict, candidates: list[S.SimulationCandidate]) -> dict:
    """Apply candidates to the baseline to produce the *actively changed* views.

    This is what makes the lane "active": the returned watchlist/advisory differ
    from production whenever experiments fired.
    """
    sim_watchlist = [str(t).upper() for t in baseline.get("watchlist", [])]
    sim_advisory = {str(p.get("symbol", "")).upper(): dict(p)
                    for p in baseline.get("advisory", []) or [] if p.get("symbol")}

    for c in candidates:
        chg = c.proposed_production_change or {}
        op = chg.get("op")
        sym = (chg.get("symbol") or "").upper()
        if c.workflow == S.WORKFLOW_WATCHLIST:
            if op == "add" and sym and sym not in sim_watchlist:
                sim_watchlist.append(sym)
            elif op == "remove" and sym in sim_watchlist:
                sim_watchlist.remove(sym)
            # rank/tag changes annotate but don't reorder the membership list here
        elif c.workflow == S.WORKFLOW_ADVISORY and sym:
            rec = sim_advisory.setdefault(sym, {"symbol": sym})
            if op == "context":
                rec["crowd_context"] = chg.get("crowd_context")
            elif op == "rank":
                rec["rank"] = chg.get("rank")

    return {
        "simulated_watchlist": sim_watchlist,
        "simulated_advisory": list(sim_advisory.values()),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_simulation_lane(
    root: Path,
    now: str,
    *,
    baseline: dict | None = None,
    experiments: list[Experiment] | None = None,
    write_files: bool = True,
    base_dir: str | None = None,
) -> dict:
    """Run the active simulation lane.

    Args:
        root: repo root.
        now: ISO timestamp string supplied by the caller (deterministic).
        baseline: optional injected baseline (tests). Defaults to a live snapshot.
        experiments: optional injected experiment list (tests). Defaults to the
            built-in set.
        write_files: write SANDBOX artifacts.
        base_dir: outputs base dir (defaults to ``<root>/outputs``).

    Returns a dict with candidates (as dicts), the actively-changed simulated
    views, and counts. Never raises for missing artifacts.
    """
    root = Path(root)
    base_dir = base_dir or str(root / "outputs")
    bl = baseline if baseline is not None else load_production_baseline(root)
    exps = experiments if experiments is not None else DEFAULT_EXPERIMENTS

    candidates: list[S.SimulationCandidate] = []
    for exp in exps:
        try:
            candidates.extend(exp(bl) or [])
        except Exception as exc:  # one bad experiment must not sink the lane
            logger.warning("simulation_lane: experiment %s failed: %s",
                           getattr(exp, "__name__", exp), exc)

    views = materialize_simulated_views(bl, candidates)

    result = {
        "generated_at": now,
        "lane": "simulation",
        "lane_active": True,            # this lane is allowed to change sim outputs
        "observe_only": False,          # active by design (sandbox-scoped only)
        "production_safe": True,        # never writes outside SANDBOX
        "candidate_count": len(candidates),
        "ready_count": sum(1 for c in candidates if c.ready_for_production_review),
        "advisory_candidate_count": sum(1 for c in candidates if c.workflow == S.WORKFLOW_ADVISORY),
        "watchlist_candidate_count": sum(1 for c in candidates if c.workflow == S.WORKFLOW_WATCHLIST),
        "candidates": [c.to_dict() for c in candidates],
        "simulated_watchlist": views["simulated_watchlist"],
        "simulated_advisory": views["simulated_advisory"],
        "production_baseline": bl,
    }

    if write_files:
        try:
            safe_write_json(OutputNamespace.SANDBOX,
                            f"{_SANDBOX_SUBDIR}/simulation_candidates.json",
                            result, base_dir=base_dir)
            safe_write_json(OutputNamespace.SANDBOX,
                            f"{_SANDBOX_SUBDIR}/simulated_watchlist.json",
                            {"generated_at": now, "watchlist": views["simulated_watchlist"]},
                            base_dir=base_dir)
            safe_write_json(OutputNamespace.SANDBOX,
                            f"{_SANDBOX_SUBDIR}/simulated_advisory.json",
                            {"generated_at": now, "advisory": views["simulated_advisory"]},
                            base_dir=base_dir)
        except Exception as exc:
            logger.warning("simulation_lane: write failed: %s", exc)
            result["write_error"] = str(exc)

    return result
