# portfolio_automation/quant_watch_probes.py
"""quant_watch_probes — observe-only ledger of sub-RED quant concerns.

Auto-registers a "watch probe" when a deterministic quant condition fires below
the daily-tool-analysis RED trip-wires, re-checks each open probe every run, and
auto-archives it on resolution / scope-change / escalation. Companion to
applied_fix_verifier (which tracks applied fixes); this tracks open concerns.

Observe-only: mutates ONLY its ledger (data/quant_watch_ledger.json) and its
status artifact (outputs/latest/quant_watch_status.json). Never touches
decision / score / allocation / portfolio state. See
docs/superpowers/specs/2026-06-08-quant-watch-probes-design.md.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json

# ── status levels ───────────────────────────────────────────────────────────
GREEN, AMBER, RED = "green", "amber", "red"

# ── transition statuses ─────────────────────────────────────────────────────
ACTIVE, RESOLVED, ESCALATED = "active", "resolved", "escalated"

# ── detector ids ────────────────────────────────────────────────────────────
DETECTOR_PRIOR_GAUGE = "prior_gauge_underperformance"
DETECTOR_NEG_RETURN = "negative_mean_return_persistence"
DETECTOR_SECTOR_DRAG = "sector_drag"
DETECTOR_MANUAL = "manual"

# ── thresholds (module constants; config-overridable later) ─────────────────
MIN_RESOLVED_1D = 30           # min resolved sample before a probe may fire
PRIOR_GAUGE_FIRE_PP = -10.0    # fire D1 when current-fp <= prior gauge by this pp
PRIOR_GAUGE_RESOLVE_PP = -2.0  # resolve D1 when delta recovers to >= this pp
PRETRACKER_RED_GATE_PP = 10.0  # daily RED gate (|delta vs pre_tracker| >= this)
SECTOR_MIN_N = 30              # min n_samples for a sector:* loser to fire D3
MAX_PROBE_AGE_DAYS = 60        # TTL: stale probe auto-expires
MAX_OBSERVATIONS = 14          # cap per-probe observation trail
MAX_ARCHIVE = 200              # cap archive length (FIFO roll-off)

_LEDGER_REL = "data/quant_watch_ledger.json"
_STATUS_REL = "quant_watch_status.json"  # under outputs/latest/


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_ledger() -> dict:
    return {"schema_version": "1", "active": [], "archive": []}


def load_ledger(path: str | Path) -> dict:
    """Load the ledger; return an empty default if missing or corrupt.
    Backfills missing top-level keys so callers can rely on the shape."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
        if not isinstance(data, dict):
            return _empty_ledger()
        data.setdefault("schema_version", "1")
        data.setdefault("active", [])
        data.setdefault("archive", [])
        if not isinstance(data["active"], list) or not isinstance(data["archive"], list):
            return _empty_ledger()
        return data
    except Exception:
        return _empty_ledger()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _select_prior_gauge(
    by_fp: dict, current_fp: str | None,
    pretracker_label: str = "pre_tracker_unknown",
) -> tuple[str | None, dict | None]:
    """Return (fp, entry) of the gauge era immediately preceding the current
    one: the by_fingerprint entry that is neither current nor pre_tracker, with
    the latest last_signal_time. (None, None) if no such entry."""
    candidates = [
        (k, v) for k, v in (by_fp or {}).items()
        if k not in (current_fp, pretracker_label) and isinstance(v, dict)
    ]
    if not candidates:
        return None, None
    fp, entry = max(candidates, key=lambda kv: kv[1].get("last_signal_time") or "")
    return fp, entry


def _active(probe: dict, detail: str, now_iso: str, observation: dict | None) -> dict:
    # now_iso is accepted for call-site symmetry with _resolved/_escalated; the
    # active-probe timestamp (last_evaluated_at) is stamped in update_ledger, not here.
    return {"id": probe.get("id"), "status": ACTIVE, "detail": detail,
            "observation": observation}


def _resolved(probe: dict, resolution: str, detail: str, now_iso: str) -> dict:
    return {"id": probe.get("id"), "status": RESOLVED, "resolution": resolution,
            "detail": detail, "resolved_at": now_iso, "observation": None}


def _escalated(probe: dict, detail: str, now_iso: str) -> dict:
    return {"id": probe.get("id"), "status": ESCALATED, "resolution": "escalated_to_red",
            "detail": detail, "resolved_at": now_iso, "observation": None}


def _age_days(created_at: str | None, now_iso: str) -> int:
    if not created_at:
        return 0
    try:
        c = datetime.fromisoformat(created_at)
        n = datetime.fromisoformat(now_iso)
        if c.tzinfo is None:
            c = c.replace(tzinfo=timezone.utc)
        if n.tzinfo is None:
            n = n.replace(tzinfo=timezone.utc)
        return (n - c).days
    except Exception:
        return 0


# ── Task 4: D1 — prior_gauge_underperformance ────────────────────────────────

def _pre_tracker_entry(retune: dict) -> dict:
    attr = (retune or {}).get("outcome_attribution") or {}
    by_fp = attr.get("by_fingerprint") or {}
    return by_fp.get(attr.get("pre_tracker_label") or "pre_tracker_unknown") or {}


def detect_prior_gauge_underperformance(
    retune: dict, now_iso: str, created_run: str,
) -> dict | None:
    attr = (retune or {}).get("outcome_attribution") or {}
    by_fp = attr.get("by_fingerprint") or {}
    current_fp = (retune or {}).get("current_fingerprint")
    cur = by_fp.get(current_fp) if current_fp else None
    if not isinstance(cur, dict):
        return None
    resolved = cur.get("resolved_1d") or 0
    if resolved < MIN_RESOLVED_1D:
        return None
    prior_fp, prior = _select_prior_gauge(by_fp, current_fp)
    if not prior:
        return None
    cur_hr, prior_hr = cur.get("hit_rate_1d"), prior.get("hit_rate_1d")
    if cur_hr is None or prior_hr is None:
        return None
    delta_prior = round((cur_hr - prior_hr) * 100, 1)
    if delta_prior > PRIOR_GAUGE_FIRE_PP:
        return None
    pre_hr = _pre_tracker_entry(retune).get("hit_rate_1d")
    delta_pre = round((cur_hr - pre_hr) * 100, 1) if pre_hr is not None else None
    if delta_pre is not None and abs(delta_pre) >= PRETRACKER_RED_GATE_PP:
        return None  # daily RED owns it — not our band
    return {
        "id": f"{DETECTOR_PRIOR_GAUGE}:{current_fp}",
        "detector": DETECTOR_PRIOR_GAUGE,
        "lens": "quant",
        "scope_key": current_fp,
        "created_at": now_iso,
        "created_run": created_run,
        "severity": AMBER,
        "concern": (
            f"current-fp {current_fp[:8]} {delta_prior:+.1f}pp vs prior gauge "
            f"{prior_fp[:8]} at n={resolved}, mean_return_1d "
            f"{cur.get('mean_return_1d', 0):.2f}"
        ),
        "trigger_snapshot": {
            "current_hit_rate_1d": cur_hr, "prior_hit_rate_1d": prior_hr,
            "delta_vs_prior_pp": delta_prior, "delta_vs_pretracker_pp": delta_pre,
            "resolved_1d": resolved, "mean_return_1d": cur.get("mean_return_1d"),
            "prior_fp": prior_fp,
        },
        "resolve_hint": f"delta vs prior gauge recovers to >= {PRIOR_GAUGE_RESOLVE_PP}pp, "
                        f"fingerprint changes, or sample collapses",
        "last_evaluated_at": now_iso,
        "observations": [{"run": now_iso[:10], "delta_vs_prior_pp": delta_prior}],
    }


def _eval_prior_gauge(probe: dict, retune: dict, efficacy: dict | None, current_fp: str | None, now_iso: str) -> dict:
    scope = probe.get("scope_key")
    if current_fp and scope != current_fp:
        return _resolved(probe, "scope_changed", f"current fp now {str(current_fp)[:8]}", now_iso)
    by_fp = ((retune or {}).get("outcome_attribution") or {}).get("by_fingerprint") or {}
    cur = by_fp.get(scope)
    if not isinstance(cur, dict):
        return _resolved(probe, "scope_changed", "fingerprint no longer present", now_iso)
    resolved = cur.get("resolved_1d") or 0
    if resolved == 0:
        return _resolved(probe, "sample_collapsed", "resolved_1d == 0", now_iso)
    cur_hr = cur.get("hit_rate_1d")
    pre_hr = _pre_tracker_entry(retune).get("hit_rate_1d")
    # escalate BEFORE resolve — a worsening probe must not silently resolve
    # gate fires only when current underperforms pre_tracker (negative delta);
    # overperformance vs pre_tracker is not a RED condition.
    if cur_hr is not None and pre_hr is not None:
        delta_pre = round((cur_hr - pre_hr) * 100, 1)
        if delta_pre <= -PRETRACKER_RED_GATE_PP and resolved >= MIN_RESOLVED_1D:
            return _escalated(
                probe, f"crossed daily RED gate: {delta_pre:+.1f}pp vs pre_tracker "
                       f"at n={resolved}", now_iso)
    if _age_days(probe.get("created_at"), now_iso) >= MAX_PROBE_AGE_DAYS:
        return _resolved(probe, "ttl_expired", f"age >= {MAX_PROBE_AGE_DAYS}d", now_iso)
    prior_fp, prior = _select_prior_gauge(by_fp, scope)
    if not prior or prior.get("hit_rate_1d") is None or cur_hr is None:
        return _resolved(probe, "scope_changed", "no prior gauge to compare", now_iso)
    delta_prior = round((cur_hr - prior["hit_rate_1d"]) * 100, 1)
    if delta_prior >= PRIOR_GAUGE_RESOLVE_PP:
        return _resolved(probe, "recovered",
                         f"delta vs prior {delta_prior:+.1f}pp >= {PRIOR_GAUGE_RESOLVE_PP}", now_iso)
    return _active(probe, f"delta vs prior {delta_prior:+.1f}pp", now_iso,
                   {"run": now_iso[:10], "delta_vs_prior_pp": delta_prior})


# ── Task 5: D2 — negative_mean_return_persistence ────────────────────────────

def detect_negative_mean_return_persistence(
    retune: dict, now_iso: str, created_run: str,
) -> dict | None:
    by_fp = ((retune or {}).get("outcome_attribution") or {}).get("by_fingerprint") or {}
    current_fp = (retune or {}).get("current_fingerprint")
    cur = by_fp.get(current_fp) if current_fp else None
    if not isinstance(cur, dict):
        return None
    resolved = cur.get("resolved_1d") or 0
    mean_ret = cur.get("mean_return_1d")
    if resolved < MIN_RESOLVED_1D or mean_ret is None or mean_ret >= 0:
        return None
    return {
        "id": f"{DETECTOR_NEG_RETURN}:{current_fp}",
        "detector": DETECTOR_NEG_RETURN,
        "lens": "quant",
        "scope_key": current_fp,
        "created_at": now_iso,
        "created_run": created_run,
        "severity": AMBER,
        "concern": (f"current-fp {current_fp[:8]} mean_return_1d {mean_ret:.2f} "
                    f"(< 0) at n={resolved}"),
        "trigger_snapshot": {"mean_return_1d": mean_ret, "resolved_1d": resolved},
        "resolve_hint": "mean_return_1d recovers to >= 0, or fingerprint changes",
        "last_evaluated_at": now_iso,
        "observations": [{"run": now_iso[:10], "mean_return_1d": mean_ret}],
    }


def _eval_neg_return(probe: dict, retune: dict, efficacy: dict | None, current_fp: str | None, now_iso: str) -> dict:
    scope = probe.get("scope_key")
    if current_fp and scope != current_fp:
        return _resolved(probe, "scope_changed", f"current fp now {str(current_fp)[:8]}", now_iso)
    by_fp = ((retune or {}).get("outcome_attribution") or {}).get("by_fingerprint") or {}
    cur = by_fp.get(scope)
    if not isinstance(cur, dict):
        return _resolved(probe, "scope_changed", "fingerprint no longer present", now_iso)
    resolved = cur.get("resolved_1d") or 0
    if resolved == 0:
        return _resolved(probe, "sample_collapsed", "resolved_1d == 0", now_iso)
    if _age_days(probe.get("created_at"), now_iso) >= MAX_PROBE_AGE_DAYS:
        return _resolved(probe, "ttl_expired", f"age >= {MAX_PROBE_AGE_DAYS}d", now_iso)
    mean_ret = cur.get("mean_return_1d")
    if mean_ret is None:
        return _active(probe, "mean_return_1d absent (degraded artifact)", now_iso,
                       {"run": now_iso[:10], "mean_return_1d": None})
    if mean_ret >= 0:
        return _resolved(probe, "recovered", f"mean_return_1d {mean_ret:.2f} >= 0", now_iso)
    return _active(probe, f"mean_return_1d {mean_ret:.2f}", now_iso,
                   {"run": now_iso[:10], "mean_return_1d": mean_ret})


# ── Task 6: D3 — sector_drag ─────────────────────────────────────────────────

def detect_sector_drag(efficacy: dict, now_iso: str, created_run: str) -> list[dict]:
    by_tag = (efficacy or {}).get("by_tag") or {}
    probes: list[dict] = []
    for tag, row in by_tag.items():
        if not (isinstance(tag, str) and tag.startswith("sector:") and isinstance(row, dict)):
            continue
        if row.get("significance") != "loser" or (row.get("n_samples") or 0) < SECTOR_MIN_N:
            continue
        sector = tag.split("sector:", 1)[1]
        probes.append({
            "id": f"{DETECTOR_SECTOR_DRAG}:{sector}",
            "detector": DETECTOR_SECTOR_DRAG,
            "lens": "quant",
            "scope_key": sector,
            "created_at": now_iso,
            "created_run": created_run,
            "severity": AMBER,
            "concern": (f"sector {sector} is a loser ({row.get('vs_baseline_pp')}pp vs "
                        f"baseline) at n={row.get('n_samples')}"),
            "trigger_snapshot": {"vs_baseline_pp": row.get("vs_baseline_pp"),
                                 "n_samples": row.get("n_samples"),
                                 "hit_rate_1d": row.get("hit_rate_1d")},
            "resolve_hint": "sector no longer flagged 'loser' or the tag disappears",
            "last_evaluated_at": now_iso,
            "observations": [{"run": now_iso[:10], "vs_baseline_pp": row.get("vs_baseline_pp")}],
        })
    return probes


def _eval_sector_drag(probe: dict, retune: dict, efficacy: dict | None, current_fp: str | None, now_iso: str) -> dict:
    sector = probe.get("scope_key")
    by_tag = (efficacy or {}).get("by_tag") or {}
    row = by_tag.get(f"sector:{sector}")
    if not isinstance(row, dict):
        return _resolved(probe, "scope_changed", "sector tag absent", now_iso)
    # n_samples is intentionally NOT rechecked post-creation; 'significance' (Wilson-CI classification) is the sole resolution signal.
    if row.get("significance") != "loser":
        return _resolved(probe, "recovered",
                         f"sector no longer loser (now {row.get('significance')})", now_iso)
    if _age_days(probe.get("created_at"), now_iso) >= MAX_PROBE_AGE_DAYS:
        return _resolved(probe, "ttl_expired", f"age >= {MAX_PROBE_AGE_DAYS}d", now_iso)
    return _active(probe, f"still loser ({row.get('vs_baseline_pp')}pp)", now_iso,
                   {"run": now_iso[:10], "vs_baseline_pp": row.get("vs_baseline_pp")})


# ── Task 7: Aggregators — detect(), evaluate(), evaluator dispatch ────────────

_EVALUATORS = {
    DETECTOR_PRIOR_GAUGE: _eval_prior_gauge,
    DETECTOR_NEG_RETURN: _eval_neg_return,
    DETECTOR_SECTOR_DRAG: _eval_sector_drag,
}


def detect(retune, efficacy, ledger, now_iso, created_run) -> list[dict]:
    """Run every detector; return NEW probes whose id is not already active."""
    active_ids = {p.get("id") for p in (ledger.get("active") or [])}
    found: list[dict] = []
    p1 = detect_prior_gauge_underperformance(retune, now_iso, created_run)
    if p1:
        found.append(p1)
    p2 = detect_negative_mean_return_persistence(retune, now_iso, created_run)
    if p2:
        found.append(p2)
    found.extend(detect_sector_drag(efficacy, now_iso, created_run))
    return [p for p in found if p["id"] not in active_ids]


def evaluate(retune, efficacy, current_fp, ledger, now_iso) -> list[dict]:
    """Re-check each active probe; return one transition per probe. Probes whose
    detector has no evaluator (e.g. manual) stay active until cleared by hand."""
    out: list[dict] = []
    for probe in (ledger.get("active") or []):
        ev = _EVALUATORS.get(probe.get("detector"))
        if ev is None:
            out.append(_active(probe, "manual — operator clears", now_iso, None))
            continue
        try:
            out.append(ev(probe, retune, efficacy, current_fp, now_iso))
        except Exception as exc:  # never let one bad probe abort the run
            out.append(_active(probe, f"eval error: {exc}", now_iso, None))
    return out


# ── Task 8: update_ledger() ──────────────────────────────────────────────────

def update_ledger(ledger, new_probes, transitions, now_iso) -> dict:
    """Return a NEW ledger (input not mutated):
      - resolved/escalated probes move to archive with resolved_at + resolution
        + lifetime_days;
      - still-active probes get last_evaluated_at bumped and their observation
        appended (capped at MAX_OBSERVATIONS);
      - new_probes are appended to active;
      - archive is FIFO-capped at MAX_ARCHIVE."""
    active_in = {p.get("id"): copy.deepcopy(p) for p in (ledger.get("active") or [])}
    archive = [copy.deepcopy(a) for a in (ledger.get("archive") or [])]
    by_id = {t.get("id"): t for t in transitions}

    new_active: list[dict] = []
    for pid, probe in active_in.items():
        t = by_id.get(pid)
        if t is None:
            new_active.append(probe)  # no transition (shouldn't happen) → keep
            continue
        if t.get("status", "") in (RESOLVED, ESCALATED):
            probe["resolved_at"] = t.get("resolved_at", now_iso)
            probe["resolved_run"] = now_iso[:10]
            probe["resolution"] = t.get("resolution")
            probe["resolution_detail"] = t.get("detail")
            probe["lifetime_days"] = _age_days(probe.get("created_at"), now_iso)
            archive.append(probe)
        else:  # active
            probe["last_evaluated_at"] = now_iso
            obs = t.get("observation")
            if obs:
                trail = list(probe.get("observations") or [])
                trail.append(copy.deepcopy(obs))
                probe["observations"] = trail[-MAX_OBSERVATIONS:]
            new_active.append(probe)

    for p in (new_probes or []):
        new_active.append(copy.deepcopy(p))

    archive = archive[-MAX_ARCHIVE:]
    return {"schema_version": "1", "active": new_active, "archive": archive}


# ── Task 9: overall_status() + render_status() + ledger_liveness ─────────────

def overall_status(ledger, transitions) -> str:
    if any(t.get("status") == ESCALATED for t in (transitions or [])):
        return RED
    if ledger.get("active"):
        return AMBER
    return GREEN


# ── Task 10: write_ledger() + run_quant_watch() orchestrator ─────────────────

def write_ledger(path: str | Path, ledger: dict) -> None:
    """Write the ledger JSON to path; creates parent directory if needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ledger, indent=2, default=str), encoding="utf-8")


def run_quant_watch(*, root: str | Path = ".", now_iso: str | None = None,
                    created_run: str = "quant-watch-analysis",
                    write_files: bool = True) -> dict:
    """Load ledger + source artifacts → evaluate → detect → update → render.
    Writes the ledger and the status artifact when write_files=True. Returns the
    status dict. Never raises — degrades to an empty-but-valid status on error."""
    root_path = Path(root).resolve()
    now = now_iso or _now_iso()
    try:
        ledger = load_ledger(root_path / _LEDGER_REL)
        retune = _load_json(root_path / "outputs/latest/retune_impact.json") or {}
        efficacy = _load_json(root_path / "outputs/latest/pattern_efficacy_monthly.json") or {}
        current_fp = retune.get("current_fingerprint")

        transitions = evaluate(retune, efficacy, current_fp, ledger, now)
        new_probes = detect(retune, efficacy, ledger, now, created_run)
        # CRITICAL: update_ledger first, then render_status on the post-update ledger
        # so active_count reflects newly-registered probes and excludes archived ones.
        new_ledger = update_ledger(ledger, new_probes, transitions, now)
        status = render_status(new_ledger, new_probes, transitions, now)

        if write_files:
            # ledger is the durable state — write it first, then the consumer-facing status artifact
            write_ledger(root_path / _LEDGER_REL, new_ledger)
            safe_write_json(OutputNamespace.LATEST, _STATUS_REL, status,
                            base_dir=root_path / "outputs")
        return status
    except Exception as exc:
        return {"generated_at": now, "observe_only": True, "schema_version": "1",
                "source": "quant_watch_probes",
                "overall_status": GREEN, "active_count": 0, "active": [],
                "registered_today": [], "resolved_today": [], "escalated_today": [],
                "ledger_liveness": {"status": "warn", "error": str(exc)},
                "disclaimer": "Observe-only quant watch ledger (degraded)."}


def render_status(ledger, new_probes, transitions, now_iso) -> dict:
    active = ledger.get("active") or []
    new_ids = [p.get("id") for p in (new_probes or [])]
    resolved_today = [{"id": t.get("id"), "resolution": t.get("resolution")}
                      for t in (transitions or []) if t.get("status") == RESOLVED]
    escalated_today = [{"id": t.get("id"), "resolution": t.get("resolution")}
                       for t in (transitions or []) if t.get("status") == ESCALATED]
    # liveness: an active probe is "stale" if it has no observation this run;
    # new probes (registered this run) are never stale.
    new_id_set = set(new_ids)
    stale = sum(1 for p in active
                if p.get("id") not in new_id_set
                and (p.get("last_evaluated_at") or p.get("created_at")) != now_iso)
    return {
        "generated_at": now_iso,
        "observe_only": True,
        "schema_version": "1",
        "source": "quant_watch_probes",
        "overall_status": overall_status(ledger, transitions),
        "active_count": len(active),
        "active": [{
            "id": p.get("id"), "detector": p.get("detector"),
            "concern": p.get("concern"), "severity": p.get("severity"),
            "age_days": _age_days(p.get("created_at"), now_iso),
            "last_observation": (p.get("observations") or [None])[-1],
        } for p in active],
        "registered_today": new_ids,
        "resolved_today": resolved_today,
        "escalated_today": escalated_today,
        "ledger_liveness": {"status": "ok" if stale == 0 else "warn",
                            "active_count": len(active), "stale_active": stale},
        "disclaimer": (
            "Observe-only quant watch ledger. Tracks sub-RED quant concerns; "
            "re-checks and auto-retires them. Does not modify portfolio, "
            "allocation, scoring, or decision state."),
    }
