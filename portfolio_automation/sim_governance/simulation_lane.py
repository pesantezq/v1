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

# Correct provenance for crowd-derived experiments: the unified crowd bus, not
# the absent legacy ``outputs/sandbox/crowd_radar`` path. This is the artifact
# ``_load_unified_crowd_context`` -> ``read_unified_crowd`` actually reads.
_UNIFIED_CROWD_EVIDENCE = "outputs/latest/unified_crowd_intelligence.json"


# ---------------------------------------------------------------------------
# Baseline loading (graceful; production is the "before")
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _input_snapshot_binding(base_dir: str) -> dict:
    """Phase 3: bind this lane run to the Phase 2 immutable input snapshot.

    Every experiment in a single ``run_simulation_lane`` call operates on the
    same baseline, so recording the frozen ``snapshot_hash`` makes it provable
    that production and all shadow strategies evaluated identical inputs (Iron
    rule 4). Degrades to ``None`` when no snapshot exists.
    """
    snap = _read_json(Path(base_dir) / "sandbox" / "daily_input_snapshot.json")
    if isinstance(snap, dict):
        return {"input_snapshot_hash": snap.get("snapshot_hash"),
                "input_snapshot_run_id": snap.get("run_id")}
    return {"input_snapshot_hash": None, "input_snapshot_run_id": None}


def _load_unified_crowd_context(root: Path) -> dict:
    """Per-symbol crowd context for the sim experiments, sourced from the unified
    crowd bus. Maps the unified row onto the {velocity, state, confidence,
    confirmed} shape the experiments expect:

      * velocity   = cross_source_confirmation_score * 2  (so a rank only rises
                     when BOTH lanes confirm attention; >=1.0 bump, >=1.5 ready).
      * state      = unified crowd_state.
      * confidence = unified crowd_confidence.
      * confirmed  = crowd_state == 'confirmed_attention' (strongest cross-source).

    Never raises; returns {} when the unified bus is unavailable (prior behavior).
    """
    try:
        from portfolio_automation.crowd_intelligence.unified_loader import read_unified_crowd
        out = read_unified_crowd(root)
        if not out.get("available") or out.get("source") != "unified":
            return {}
        ctx: dict[str, Any] = {}
        for tk, row in (out.get("by_ticker") or {}).items():
            confirmation = float(row.get("cross_source_confirmation_score") or 0.0)
            state = row.get("crowd_state") or "insufficient_data"
            ctx[str(tk).upper()] = {
                "velocity": round(confirmation * 2.0, 4),
                "state": state,
                "confidence": float(row.get("crowd_confidence") or 0.0),
                "confirmed": state == "confirmed_attention",
                "confirmation": confirmation,
                "divergence": float(row.get("cross_source_divergence_score") or 0.0),
                "retail_attention": row.get("retail_attention_score"),
                "fmp_attention": row.get("fmp_attention_score"),
            }
        return ctx
    except Exception:
        return {}


def load_production_baseline(root: Path) -> dict:
    """Snapshot what production looks like today (the 'before' for comparisons).

    Tolerant: any missing artifact degrades to an empty section so the lane runs
    in any environment. Returns:
      {"watchlist": [tickers], "advisory": [picks], "crowd": {ticker: context}}
    """
    root = Path(root)
    out: dict[str, Any] = {"watchlist": [], "advisory": [], "crowd": {}}

    # Unified crowd context (preferred). Joins ApeWisdom retail attention + FMP
    # market/context attention; the sim lane may actively consume it. Falls back
    # to {} (the prior always-empty behavior) when the unified bus is unavailable.
    out["crowd"] = _load_unified_crowd_context(root)

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

    # Flock Intelligence simulation context (observe-only sim artifacts). The
    # flock producer runs earlier in the pipeline and writes these; the lane
    # consumes them to emit flock SimulationCandidates. Degrades to {} on miss.
    sim = root / "outputs" / "simulation"
    out["flock"] = {
        "report": _read_json(sim / "flock_intelligence.json") or {},
        "watchlist_candidates": _read_json(sim / "flock_watchlist_candidates.json") or {},
        "advisory_context": _read_json(sim / "flock_advisory_context.json") or {},
    }
    return out


_FLOCK_DECISIVE_STATES = frozenset({
    "flock_confirmed", "flock_exhaustion", "flock_dispersing", "flock_broken",
})


def experiment_flock_intelligence(baseline: dict) -> list[S.SimulationCandidate]:
    """Turn Flock Intelligence simulation context into SimulationCandidates.

    Emits (per the spec's proposal types):
      * watchlist add/tag/rank          -> PROPOSAL_FLOCK_WATCHLIST_LOGIC
      * advisory flock-context display  -> PROPOSAL_FLOCK_ADVISORY_CONTEXT
      * exhaustion/dispersion caution   -> PROPOSAL_FLOCK_RISK_OVERLAY
      * confirmed/forming confidence    -> PROPOSAL_FLOCK_SCORING_ADJUSTMENT

    ``ready_for_production_review`` is a HINT only (confident + decisive state);
    the AI/product review decides, and only DECISION_READY yields a *pending*
    proposal. Production is never changed here.
    """
    flock = baseline.get("flock") or {}
    cands: list[S.SimulationCandidate] = []

    # 1. Watchlist candidates derived by the flock producer.
    wl = (flock.get("watchlist_candidates") or {}).get("candidates", []) or []
    for c in wl:
        sym = str(c.get("ticker") or "").upper()
        if not sym:
            continue
        state = c.get("flock_state", "")
        conf = float(c.get("confidence", 0.0) or 0.0)
        cid = S.make_candidate_id(S.PROPOSAL_FLOCK_WATCHLIST_LOGIC, sym,
                                  salt=f"{c.get('action')}:{state}")
        cands.append(S.SimulationCandidate(
            candidate_id=cid, workflow=S.WORKFLOW_WATCHLIST,
            proposal_type=S.PROPOSAL_FLOCK_WATCHLIST_LOGIC, symbol=sym,
            what_changed=f"Flock {c.get('action')} {sym} ({state}) tags={c.get('tags')}",
            why_changed=c.get("rationale", "Flock Intelligence simulation signal"),
            source_evidence=["outputs/simulation/flock_watchlist_candidates.json"],
            production_baseline=None,
            simulated_value={"action": c.get("action"), "tags": c.get("tags"),
                             "sim_rank_delta": c.get("sim_rank_delta")},
            risk_impact="medium" if state in _FLOCK_DECISIVE_STATES else "low",
            confidence=conf, data_quality="ok",
            ready_for_production_review=(conf >= 0.7 and state in _FLOCK_DECISIVE_STATES),
            proposed_production_change={"op": c.get("action"), "symbol": sym,
                                        "tags": c.get("tags"),
                                        "rank_delta": c.get("sim_rank_delta")},
        ))

    # 2. Advisory flock-context overlays for current advisory picks.
    advisory_syms = {str(p.get("symbol", "")).upper()
                     for p in baseline.get("advisory", []) or [] if p.get("symbol")}
    by_symbol = (flock.get("advisory_context") or {}).get("by_symbol", {}) or {}
    dq = (flock.get("report") or {}).get("data_quality_status", "unknown")
    for sym, ctx in by_symbol.items():
        sym = str(sym).upper()
        if sym not in advisory_syms:
            continue
        state = ctx.get("flock_state", "")
        conf = float(ctx.get("confidence", 0.0) or 0.0)
        decisive = state in _FLOCK_DECISIVE_STATES
        # context display candidate (always)
        cands.append(S.SimulationCandidate(
            candidate_id=S.make_candidate_id(S.PROPOSAL_FLOCK_ADVISORY_CONTEXT, sym, salt=state),
            workflow=S.WORKFLOW_ADVISORY, proposal_type=S.PROPOSAL_FLOCK_ADVISORY_CONTEXT,
            symbol=sym, what_changed=f"Attach flock context '{ctx.get('label')}' to {sym}",
            why_changed=ctx.get("meaning", "Flock structure context for the advisory pick"),
            source_evidence=["outputs/simulation/flock_advisory_context.json"],
            production_baseline={"symbol": sym, "flock_context": None},
            simulated_value={"symbol": sym, "flock_context": ctx.get("label"),
                             "flock_state": state},
            risk_impact="medium" if decisive else "low", confidence=conf,
            data_quality=dq if dq in ("ok", "degraded", "stale") else "unknown",
            ready_for_production_review=(conf >= 0.7 and decisive),
            proposed_production_change={"op": "flock_context", "symbol": sym,
                                        "flock_state": state, "label": ctx.get("label")},
        ))
        # risk overlay for crowded/dispersing structure
        if state in ("flock_exhaustion", "flock_dispersing", "flock_broken"):
            cands.append(S.SimulationCandidate(
                candidate_id=S.make_candidate_id(S.PROPOSAL_FLOCK_RISK_OVERLAY, sym, salt=state),
                workflow=S.WORKFLOW_ADVISORY, proposal_type=S.PROPOSAL_FLOCK_RISK_OVERLAY,
                symbol=sym, what_changed=f"Flag {sym} with flock risk '{state}'",
                why_changed=ctx.get("meaning", "Crowd structure indicates elevated risk"),
                source_evidence=["outputs/simulation/flock_intelligence.json"],
                production_baseline=None,
                simulated_value={"symbol": sym, "risk": state,
                                 "dispersion_score": ctx.get("dispersion_score")},
                risk_impact="medium", confidence=conf, data_quality="ok",
                ready_for_production_review=(conf >= 0.7),
                proposed_production_change={"op": "flock_risk", "symbol": sym, "risk": state},
            ))
        # scoring adjustment for cohesive flocks
        elif state in ("flock_confirmed", "flock_forming"):
            cands.append(S.SimulationCandidate(
                candidate_id=S.make_candidate_id(S.PROPOSAL_FLOCK_SCORING_ADJUSTMENT, sym, salt=state),
                workflow=S.WORKFLOW_ADVISORY, proposal_type=S.PROPOSAL_FLOCK_SCORING_ADJUSTMENT,
                symbol=sym, what_changed=f"Simulation confidence nudge for {sym} ({state})",
                why_changed="Cohesive flock supports a small simulation-confidence boost",
                source_evidence=["outputs/simulation/flock_intelligence.json"],
                production_baseline=None,
                simulated_value={"symbol": sym, "scoring_hint": "boost",
                                 "flock_score": ctx.get("flock_score")},
                risk_impact="low", confidence=conf, data_quality="ok",
                ready_for_production_review=False,  # scoring changes need extra scrutiny
                proposed_production_change={"op": "flock_scoring", "symbol": sym,
                                            "hint": "boost"},
            ))
    return cands


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
            source_evidence=[_UNIFIED_CROWD_EVIDENCE],
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
    """Attach a crowd-context annotation to advisory picks (observe-only context).

    crowd_state is a fast-refreshing daily signal (it flips
    confirmed_attention / divergent_attention / insufficient_data day to day), so
    it is treated as a LIVE, self-refreshing observe-only annotation: the candidate
    is materialized straight into the SANDBOX advisory view each run
    (``materialize_simulated_views``), and it is NEVER routed into the human-gated
    production promotion queue. ``ready_for_production_review`` is therefore always
    False, and ``promotion_proposals.generate_proposals`` additionally skips this
    type at the gate. The annotation never feeds decision_engine / decision_plan.
    """
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
            what_changed=f"Annotate {sym} advisory line with live crowd context '{ctx.get('state')}'",
            why_changed=("Crowd state adds public-knowledge context to the advisory pick; "
                         "self-refreshing observe-only annotation (not a gated production change)"),
            source_evidence=[_UNIFIED_CROWD_EVIDENCE],
            production_baseline={"symbol": sym, "crowd_context": None},
            simulated_value={"symbol": sym, "crowd_context": ctx.get("state")},
            risk_impact="low",
            confidence=float(ctx.get("confidence", 0.5)),
            data_quality="ok",
            # Observe-only annotation — never enters the human-gated promotion queue.
            ready_for_production_review=False,
            proposed_production_change={"op": "context", "symbol": sym,
                                        "crowd_context": ctx.get("state")},
        ))
    return cands


DEFAULT_EXPERIMENTS: list[Experiment] = [
    experiment_watchlist_discovery_adds,
    experiment_watchlist_rerank,
    experiment_advisory_crowd_context,
    experiment_flock_intelligence,
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
            elif op == "flock_context":
                rec["flock_context"] = chg.get("label")
                rec["flock_state"] = chg.get("flock_state")
            elif op == "flock_risk":
                rec["flock_risk"] = chg.get("risk")
            elif op == "flock_scoring":
                rec["flock_scoring_hint"] = chg.get("hint")

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
    snapshot_binding = _input_snapshot_binding(base_dir)

    result = {
        "generated_at": now,
        "lane": "simulation",
        "lane_active": True,            # this lane is allowed to change sim outputs
        "observe_only": False,          # active by design (sandbox-scoped only)
        "production_safe": True,        # never writes outside SANDBOX
        # Phase 3: the frozen Phase 2 input snapshot every experiment shared.
        "input_snapshot_hash": snapshot_binding["input_snapshot_hash"],
        "input_snapshot_run_id": snapshot_binding["input_snapshot_run_id"],
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
