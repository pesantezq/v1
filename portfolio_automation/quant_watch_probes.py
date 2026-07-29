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

WS16 fix (.superpowers/audit/ws-13-15-16-universe-experiments.md, 2026-07-28)
------------------------------------------------------------------------------
The audit found all three detectors sharing a pure age-based 60-day
auto-resolve (`ttl_expired`), independent of whether the underlying condition
had actually cleared — an unfixed concern silently vanished after 60 days.
This module now enforces: **a concern closes only when its detector no
longer fires AND a closure record (`closure_evidence`) exists.** Age alone
(`MAX_PROBE_AGE_DAYS`) is no longer a resolution path anywhere in this
module — it is now purely an operator-visibility marker
(`stale_unresolved` in `render_status`'s active listing), not a closure
trigger. Every RESOLVED/ESCALATED transition now populates `closure_evidence`
(operator-recorded via `record_closure`, or detector-derived automatically in
`update_ledger` when not pre-supplied) so a closed concern always carries the
evidence that justified closing it.

Escalation now keys on **persistence + impact + trust-boundary severity**,
never age: D1 (unchanged) gates on `resolved_1d >= MIN_RESOLVED_1D` +
`delta_vs_pretracker_pp`; D2/D3 (new) gate on a stricter sample floor +
`consecutive_observations` + a severity threshold. `TRUST_BOUNDARY_CONCERN_CLASSES`
concerns (timestamp leakage, revocation resurrection, decision/presentation
divergence) may register directly at RED severity — a single confirmed
occurrence of a trust-boundary breach is not statistical noise and does not
need to wait for persistence.

The concern schema is expanded (see `CONCERN_CLASSES` / `_migrate_probe_shape`)
with `consecutive_observations`, `evidence_artifact`, `affected_component`,
`escalation_threshold`, a structured `owner`, `remediation_status`,
`closure_evidence`, and `regression_test_reference`. `load_ledger` migrates
every probe (active + archived) into this shape tolerantly — unknown/extra
fields (e.g. the historical ad hoc free-text `owner` on
`manual:regime_classifier_neutral_collapse`) are preserved, never dropped, and
missing new fields are backfilled with safe, non-fabricated defaults. This is
a read-time migration only; it does not rewrite ledger history on disk.
"""
from __future__ import annotations

import copy
import json
import re
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
SECTOR_XCHECK_MIN_N = 20       # min current-fp resolved_1d for the sector cross-check to veto

# WS16: this is now an OPERATOR-VISIBILITY marker only ("this concern has been
# open a long time and never closed"), surfaced as `stale_unresolved` on each
# active probe in render_status(). It is NEVER used to auto-resolve a probe —
# age alone must never close a concern (that was the WS16 defect).
MAX_PROBE_AGE_DAYS = 60

# D2/D3 RED-escalation gates (WS16 — persistence + impact, mirroring D1's
# existing pattern; never age-based). Deliberately stricter than the AMBER
# fire-gates above: a probe must persist across multiple evaluation cycles
# (`consecutive_observations`) AND cross a more severe magnitude before it
# escalates, so a one-day blip cannot RED-escalate.
NEG_RETURN_RED_PCT = -1.0          # mean_return_1d at/under this is severe
NEG_RETURN_RED_MIN_N = 60          # persistence: 2x the D2 fire-gate sample size
NEG_RETURN_RED_MIN_CONSECUTIVE = 3 # persistence: must still fire after >=3 checks
SECTOR_RED_GATE_PP = -15.0         # vs_baseline_pp at/under this is severe drag
SECTOR_RED_MIN_N = 60              # persistence: 2x the D3 fire-gate sample size
SECTOR_RED_MIN_CONSECUTIVE = 3     # persistence: must still be a loser after >=3 checks

MAX_OBSERVATIONS = 14           # cap per-probe observation trail
MAX_ARCHIVE = 200               # cap archive length (FIFO roll-off)

_LEDGER_REL = "data/quant_watch_ledger.json"
_STATUS_REL = "quant_watch_status.json"  # under outputs/latest/

# ── WS16: concern-class taxonomy ─────────────────────────────────────────────
# Declarative taxonomy the reliability program needs. Most of these classes are
# not yet backed by a live detector (that would be separate, future work per
# class) — they exist so `register_manual_concern`/future detectors can
# classify a finding consistently instead of inventing ad hoc strings, exactly
# the gap WS16 found (`owner` was free text because nothing enforced a shape).
CONCERN_CLASS_STATISTICAL_INSUFFICIENCY = "statistical_insufficiency"
CONCERN_CLASS_EFFECTIVE_SAMPLE_SIZE_COLLAPSE = "effective_sample_size_collapse"
CONCERN_CLASS_OOS_EVIDENCE_MISSING = "oos_evidence_missing"
CONCERN_CLASS_MULTIPLE_COMPARISON_RISK = "multiple_comparison_risk"
CONCERN_CLASS_REGIME_CONCENTRATION = "regime_concentration"
CONCERN_CLASS_SINGLE_SYMBOL_DEPENDENCY = "single_symbol_dependency"
CONCERN_CLASS_SINGLE_WEEK_DEPENDENCY = "single_week_dependency"
CONCERN_CLASS_SCORE_INSTABILITY = "score_instability"
CONCERN_CLASS_FRESHNESS_CONTRACT_VIOLATION = "freshness_contract_violation"
CONCERN_CLASS_SILENT_ZERO_OUTPUT = "silent_zero_output"
CONCERN_CLASS_ZERO_VARIANCE_RANKING = "zero_variance_ranking"
CONCERN_CLASS_PRODUCER_CONSUMER_CADENCE_MISMATCH = "producer_consumer_cadence_mismatch"
CONCERN_CLASS_ARTIFACT_SCHEMA_DRIFT = "artifact_schema_drift"
CONCERN_CLASS_PRESENTATION_OMISSION = "presentation_omission"
CONCERN_CLASS_STALE_TEST_FIXTURE = "stale_test_fixture"
CONCERN_CLASS_APPROVAL_DURABILITY_MISMATCH = "approval_durability_mismatch"
CONCERN_CLASS_REVOCATION_INTEGRITY = "revocation_integrity"
CONCERN_CLASS_CONCURRENCY_RISK = "concurrency_risk"
CONCERN_CLASS_UNBOUNDED_AUDIT_GROWTH = "unbounded_audit_growth"
CONCERN_CLASS_QUALITY_SCREEN_BYPASS = "quality_screen_bypass"
CONCERN_CLASS_INERT_EXPERIMENT = "inert_experiment"
CONCERN_CLASS_MEMO_DECISION_MISMATCH = "memo_versus_decision_mismatch"

CONCERN_CLASSES = frozenset({
    DETECTOR_PRIOR_GAUGE, DETECTOR_NEG_RETURN, DETECTOR_SECTOR_DRAG, DETECTOR_MANUAL,
    CONCERN_CLASS_STATISTICAL_INSUFFICIENCY, CONCERN_CLASS_EFFECTIVE_SAMPLE_SIZE_COLLAPSE,
    CONCERN_CLASS_OOS_EVIDENCE_MISSING, CONCERN_CLASS_MULTIPLE_COMPARISON_RISK,
    CONCERN_CLASS_REGIME_CONCENTRATION, CONCERN_CLASS_SINGLE_SYMBOL_DEPENDENCY,
    CONCERN_CLASS_SINGLE_WEEK_DEPENDENCY, CONCERN_CLASS_SCORE_INSTABILITY,
    CONCERN_CLASS_FRESHNESS_CONTRACT_VIOLATION, CONCERN_CLASS_SILENT_ZERO_OUTPUT,
    CONCERN_CLASS_ZERO_VARIANCE_RANKING, CONCERN_CLASS_PRODUCER_CONSUMER_CADENCE_MISMATCH,
    CONCERN_CLASS_ARTIFACT_SCHEMA_DRIFT, CONCERN_CLASS_PRESENTATION_OMISSION,
    CONCERN_CLASS_STALE_TEST_FIXTURE, CONCERN_CLASS_APPROVAL_DURABILITY_MISMATCH,
    CONCERN_CLASS_REVOCATION_INTEGRITY, CONCERN_CLASS_CONCURRENCY_RISK,
    CONCERN_CLASS_UNBOUNDED_AUDIT_GROWTH, CONCERN_CLASS_QUALITY_SCREEN_BYPASS,
    CONCERN_CLASS_INERT_EXPERIMENT, CONCERN_CLASS_MEMO_DECISION_MISMATCH,
})

# Trust-boundary concern classes (WS16 item 3): a single CONFIRMED occurrence
# is eligible for immediate RED severity at registration — these are
# correctness/safety breaches, not statistical noise that needs persistence.
TRUST_BOUNDARY_CONCERN_CLASSES = frozenset({
    "timestamp_leakage", "revocation_resurrection", "decision_presentation_divergence",
})

_REMEDIATION_STATUSES = frozenset({"open", "in_progress", "queued", "wontfix", "closed", "escalated"})
_DEFAULT_REMEDIATION_STATUS = "open"

# Schema fields added by WS16 that must exist (possibly None) on every probe
# after migration, in addition to the historical
# id/detector/lens/scope_key/created_at/created_run/severity/concern/
# trigger_snapshot/resolve_hint/last_evaluated_at/observations shape.
_NEW_SCHEMA_FIELDS = (
    "concern_class", "consecutive_observations", "evidence_artifact",
    "affected_component", "escalation_threshold", "owner", "remediation_status",
    "closure_evidence", "regression_test_reference",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_ledger() -> dict:
    return {"schema_version": "1", "active": [], "archive": []}


def _normalize_owner(raw: Any) -> dict | None:
    """Tolerantly normalize an ``owner`` value into a structured shape.

    WS16 finding: the one hand-authored manual probe in the live ledger
    (``manual:regime_classifier_neutral_collapse``) carries an ad hoc
    free-text ``owner`` string ("regime-classifier owner (market_regime.py)"),
    proving the dict had no enforced shape. This gives ``owner`` a real shape
    (``{role, identifier, note}``) WITHOUT discarding the historical string —
    it becomes both ``identifier`` and ``note`` on a bare string, and any
    already-structured dict passes through with unknown keys preserved
    (additive schema, not a closed one). Absence stays ``None``.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        out = {"role": raw.get("role"), "identifier": raw.get("identifier"), "note": raw.get("note")}
        for k, v in raw.items():
            out.setdefault(k, v)
        return out
    return {"role": None, "identifier": str(raw), "note": str(raw)}


def _migrate_probe_shape(probe: dict, *, archived: bool) -> dict:
    """Tolerantly backfill the WS16-expanded schema onto one probe dict.

    Read-time only (never rewrites the file on disk by itself) — called from
    ``load_ledger`` so every consumer sees the full shape regardless of when
    the probe was written. Never drops unknown/extra fields (e.g. the ad hoc
    ``owner`` string above): this is tolerant migration, not a rewrite of
    history. The two real probes in ``data/quant_watch_ledger.json`` (one
    active `sector_drag`/manual-class concern, one archived
    `manual:regime_classifier_neutral_collapse`) must keep loading unchanged
    in substance — only the new fields are ever added.
    """
    p = dict(probe)
    p["owner"] = _normalize_owner(p.get("owner"))
    if not p.get("concern_class"):
        # the 3 built-in detector ids already double as a concern class; a
        # hand-registered manual probe may carry its own concern_class.
        p["concern_class"] = p.get("detector")
    if p.get("consecutive_observations") is None:
        p["consecutive_observations"] = len(p.get("observations") or []) or 1
    if p.get("remediation_status") not in _REMEDIATION_STATUSES:
        p["remediation_status"] = "closed" if archived else _DEFAULT_REMEDIATION_STATUS
    for k in ("evidence_artifact", "affected_component", "escalation_threshold",
              "regression_test_reference"):
        p.setdefault(k, None)
    if archived and not p.get("closure_evidence") and p.get("resolution"):
        # backfilled=True marks this as a read-time reconstruction from the
        # pre-WS16 resolution/resolution_detail fields, not a live-computed
        # closure — honest about its provenance, never fabricated.
        p["closure_evidence"] = {
            "artifact": p.get("evidence_artifact"),
            "snapshot": (p.get("observations") or [None])[-1],
            "note": p.get("resolution_detail"),
            "closed_by": "detector" if p.get("resolution") != "escalated_to_red" else "detector_escalation",
            "closed_at": p.get("resolved_at"),
            "backfilled": True,
        }
    else:
        p.setdefault("closure_evidence", None)
    return p


def load_ledger(path: str | Path) -> dict:
    """Load the ledger; return an empty default if missing or corrupt.
    Backfills missing top-level keys so callers can rely on the shape, and
    migrates every probe (active + archived) into the WS16-expanded schema
    tolerantly (see ``_migrate_probe_shape``)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
        if not isinstance(data, dict):
            return _empty_ledger()
        data.setdefault("schema_version", "1")
        data.setdefault("active", [])
        data.setdefault("archive", [])
        if not isinstance(data["active"], list) or not isinstance(data["archive"], list):
            return _empty_ledger()
        data["active"] = [_migrate_probe_shape(p, archived=False)
                          for p in data["active"] if isinstance(p, dict)]
        data["archive"] = [_migrate_probe_shape(p, archived=True)
                           for p in data["archive"] if isinstance(p, dict)]
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


def _active(probe: dict, detail: str, now_iso: str, observation: dict | None,
            consecutive_observations: int | None = None) -> dict:
    # now_iso is accepted for call-site symmetry with _resolved/_escalated; the
    # active-probe timestamp (last_evaluated_at) is stamped in update_ledger, not here.
    # consecutive_observations is optional (omitted from the dict when not
    # given) purely to keep the transition-builder shape byte-for-byte
    # backward compatible for callers that don't need it; update_ledger()
    # increments+persists this itself when a transition doesn't supply it.
    out = {"id": probe.get("id"), "status": ACTIVE, "detail": detail,
           "observation": observation}
    if consecutive_observations is not None:
        out["consecutive_observations"] = consecutive_observations
    return out


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
        "concern_class": DETECTOR_PRIOR_GAUGE,
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
        "consecutive_observations": 1,
        "evidence_artifact": "outputs/latest/retune_impact.json",
        "affected_component": "portfolio_automation/retune_impact_tracker.py (gauge fingerprint outcome attribution)",
        "escalation_threshold": (
            f"delta_vs_pretracker_pp <= -{PRETRACKER_RED_GATE_PP} at resolved_1d >= {MIN_RESOLVED_1D}"),
        "owner": None,
        "remediation_status": "open",
        "closure_evidence": None,
        "regression_test_reference": None,
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
        "concern_class": DETECTOR_NEG_RETURN,
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
        "consecutive_observations": 1,
        "evidence_artifact": "outputs/latest/retune_impact.json",
        "affected_component": "portfolio_automation/retune_impact_tracker.py (gauge fingerprint outcome attribution)",
        "escalation_threshold": (
            f"mean_return_1d <= {NEG_RETURN_RED_PCT} at resolved_1d >= {NEG_RETURN_RED_MIN_N} "
            f"and consecutive_observations >= {NEG_RETURN_RED_MIN_CONSECUTIVE}"),
        "owner": None,
        "remediation_status": "open",
        "closure_evidence": None,
        "regression_test_reference": None,
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
    mean_ret = cur.get("mean_return_1d")
    # escalate BEFORE resolve (WS16 — persistence + impact, never age): a
    # probe that has persisted across >= NEG_RETURN_RED_MIN_CONSECUTIVE
    # evaluations at a severe (not merely negative) mean return escalates.
    consecutive_next = int(probe.get("consecutive_observations") or 0) + 1
    if (mean_ret is not None and mean_ret <= NEG_RETURN_RED_PCT
            and resolved >= NEG_RETURN_RED_MIN_N
            and consecutive_next >= NEG_RETURN_RED_MIN_CONSECUTIVE):
        return _escalated(
            probe, f"persistent severe negative return: mean_return_1d {mean_ret:.2f} <= "
                   f"{NEG_RETURN_RED_PCT} at n={resolved}, {consecutive_next} consecutive "
                   f"observations", now_iso)
    if mean_ret is None:
        return _active(probe, "mean_return_1d absent (degraded artifact)", now_iso,
                       {"run": now_iso[:10], "mean_return_1d": None})
    if mean_ret >= 0:
        return _resolved(probe, "recovered", f"mean_return_1d {mean_ret:.2f} >= 0", now_iso)
    return _active(probe, f"mean_return_1d {mean_ret:.2f}", now_iso,
                   {"run": now_iso[:10], "mean_return_1d": mean_ret})


# ── Task 6: D3 — sector_drag ─────────────────────────────────────────────────

def _norm_sector(name: str) -> str:
    """Canonicalize a sector label for cross-artifact matching. pattern_efficacy's
    by_tag uses underscores ('Communication_Services', 'ETF_Index') while
    retune_impact's sector_composition uses spaces/slashes ('Communication Services',
    'ETF/Index'). Strip to lowercase alphanumerics so the two line up."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _current_fp_sector_verdict(retune: dict | None, sector: str,
                               min_n: int = SECTOR_XCHECK_MIN_N) -> str:
    """Cross-check a pooled `sector:* loser` against the CURRENT fingerprint's own
    sector_composition. by_tag pools outcomes across gauge eras, so a sector a
    retired gauge dragged can read 'loser' forever while the live gauge treats it
    as a winner (the cross-gauge pooling artifact behind the stale
    Communication_Services probe, 2026-07-10). Returns:
      'contradicts' — live gauge shows the sector mean_return_1d >= 0 at adequate n
                      (the pooled loser does not hold on the current gauge);
      'confirms'    — live gauge also shows the sector negative-mean;
      'unknown'     — no adequate current-fp evidence (absent / thin sample)."""
    by_fp = ((retune or {}).get("outcome_attribution") or {}).get("by_fingerprint") or {}
    cur_fp = (retune or {}).get("current_fingerprint")
    comp = ((by_fp.get(cur_fp) or {}).get("sector_composition")) or {}
    target = _norm_sector(sector)
    row = next((r for name, r in comp.items()
                if _norm_sector(name) == target and isinstance(r, dict)), None)
    if not isinstance(row, dict):
        return "unknown"
    mean_ret = row.get("mean_return_1d")
    if (row.get("resolved_1d") or 0) < min_n or mean_ret is None:
        return "unknown"
    return "contradicts" if mean_ret >= 0 else "confirms"


def detect_sector_drag(efficacy: dict, now_iso: str, created_run: str,
                       retune: dict | None = None) -> list[dict]:
    by_tag = (efficacy or {}).get("by_tag") or {}
    probes: list[dict] = []
    for tag, row in by_tag.items():
        if not (isinstance(tag, str) and tag.startswith("sector:") and isinstance(row, dict)):
            continue
        if row.get("significance") != "loser" or (row.get("n_samples") or 0) < SECTOR_MIN_N:
            continue
        sector = tag.split("sector:", 1)[1]
        # Cross-gauge pooling guard: skip when the CURRENT fingerprint's own
        # sector slice contradicts the pooled loser verdict (live gauge is fine).
        if _current_fp_sector_verdict(retune, sector) == "contradicts":
            continue
        probes.append({
            "id": f"{DETECTOR_SECTOR_DRAG}:{sector}",
            "detector": DETECTOR_SECTOR_DRAG,
            "concern_class": DETECTOR_SECTOR_DRAG,
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
            "consecutive_observations": 1,
            "evidence_artifact": "outputs/latest/pattern_efficacy_monthly.json",
            "affected_component": "portfolio_automation/pattern_learning.py (by_tag sector attribution)",
            "escalation_threshold": (
                f"vs_baseline_pp <= {SECTOR_RED_GATE_PP} at n_samples >= {SECTOR_RED_MIN_N} "
                f"and consecutive_observations >= {SECTOR_RED_MIN_CONSECUTIVE}"),
            "owner": None,
            "remediation_status": "open",
            "closure_evidence": None,
            "regression_test_reference": None,
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
    # Cross-gauge pooling guard: the pooled by_tag still says loser, but if the
    # CURRENT fingerprint's own sector slice contradicts it (live gauge positive-
    # mean at adequate n), the pooled verdict is a stale cross-era artifact — retire.
    if _current_fp_sector_verdict(retune, sector) == "contradicts":
        return _resolved(probe, "current_fp_contradicts",
                         f"pooled by_tag still 'loser' but current-fp {sector} "
                         f"mean_return_1d >= 0 at n>={SECTOR_XCHECK_MIN_N} "
                         f"(cross-gauge pooling artifact)", now_iso)
    # escalate BEFORE resolve (WS16 — persistence + impact, never age): a
    # sector that has persisted as a loser across >= SECTOR_RED_MIN_CONSECUTIVE
    # evaluations at a severe (not merely "loser") vs_baseline_pp escalates.
    vs_baseline_pp = row.get("vs_baseline_pp")
    n_samples = row.get("n_samples") or 0
    consecutive_next = int(probe.get("consecutive_observations") or 0) + 1
    if (vs_baseline_pp is not None and vs_baseline_pp <= SECTOR_RED_GATE_PP
            and n_samples >= SECTOR_RED_MIN_N
            and consecutive_next >= SECTOR_RED_MIN_CONSECUTIVE):
        return _escalated(
            probe, f"persistent severe sector drag: {vs_baseline_pp}pp vs baseline <= "
                   f"{SECTOR_RED_GATE_PP}pp at n={n_samples}, {consecutive_next} consecutive "
                   f"observations", now_iso)
    return _active(probe, f"still loser ({vs_baseline_pp}pp)", now_iso,
                   {"run": now_iso[:10], "vs_baseline_pp": vs_baseline_pp})


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
    found.extend(detect_sector_drag(efficacy, now_iso, created_run, retune=retune))
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
        + lifetime_days + closure_evidence (WS16 — populated from a
        pre-attached `record_closure` call if present, else derived from the
        resolving transition's own evidence; NEVER left unset, and NEVER
        derived from age alone);
      - still-active probes get last_evaluated_at bumped, consecutive_observations
        incremented (WS16 — the persistence counter escalation logic reads),
        and their observation appended (capped at MAX_OBSERVATIONS);
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
            is_escalation = t.get("status") == ESCALATED
            probe["remediation_status"] = "escalated" if is_escalation else "closed"
            # WS16 core fix: a concern only ever leaves `active` here because
            # its detector re-evaluated it as no-longer-firing (the branch
            # above) — age is never consulted. This is condition (1) of
            # "detector no longer fires AND a closure record exists".
            # Condition (2): populate closure_evidence if a human hasn't
            # already pre-attached one via record_closure().
            if not probe.get("closure_evidence"):
                probe["closure_evidence"] = {
                    "artifact": probe.get("evidence_artifact"),
                    "snapshot": t.get("observation") or (probe.get("observations") or [None])[-1],
                    "note": t.get("detail"),
                    "closed_by": "detector_escalation" if is_escalation else "detector",
                    "closed_at": probe["resolved_at"],
                    "backfilled": False,
                }
            archive.append(probe)
        else:  # active
            probe["last_evaluated_at"] = now_iso
            probe["consecutive_observations"] = t.get(
                "consecutive_observations", int(probe.get("consecutive_observations") or 0) + 1)
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
    # WS16: a manually-registered trust-boundary concern can carry RED
    # severity from the moment it is registered (register_manual_concern) —
    # it has no evaluator to produce an ESCALATED transition, so overall
    # status must also honor a probe's own recorded severity.
    if any(p.get("severity") == RED for p in (ledger.get("active") or [])):
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
            "concern_class": p.get("concern_class") or p.get("detector"),
            "concern": p.get("concern"), "severity": p.get("severity"),
            "age_days": _age_days(p.get("created_at"), now_iso),
            "last_observation": (p.get("observations") or [None])[-1],
            "consecutive_observations": p.get("consecutive_observations"),
            "remediation_status": p.get("remediation_status"),
            "owner": p.get("owner"),
            "affected_component": p.get("affected_component"),
            "escalation_threshold": p.get("escalation_threshold"),
            # WS16: age is a VISIBILITY marker only — it never resolves a
            # probe. A long-lived, still-active, never-closed concern is
            # exactly the failure mode this flag exists to keep visible
            # instead of letting it silently disappear at 60 days.
            "stale_unresolved": _age_days(p.get("created_at"), now_iso) >= MAX_PROBE_AGE_DAYS,
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


# ── WS16: manual-concern registration + closure-evidence API ─────────────────
# Replaces "operator hand-edits ledger JSON" (the documented, but ad hoc, path
# for manual probes) with a schema-correct API. `semantic_liveness.py` already
# best-effort-calls `register_manual_concern` via getattr — this is that
# function.

def register_manual_concern(
    *, root: str | Path = ".", concern: str, detector: str = DETECTOR_MANUAL,
    concern_class: str | None = None, scope_key: str | None = None,
    severity: str = AMBER, evidence_artifact: str | None = None,
    affected_component: str | None = None, owner: Any = None,
    escalation_threshold: str | None = None, trust_boundary: bool = False,
    resolve_hint: str | None = None, now_iso: str | None = None,
    created_run: str = "manual", write_files: bool = True,
) -> dict:
    """Append a manual concern to the ledger with the full WS16 schema.

    Idempotent by id (``detector:scope_key``) — calling this twice for the
    same concern does not duplicate an already-active entry. Manual concerns
    are NEVER auto-resolved by ``evaluate()`` (no evaluator is registered for
    ``manual``-class detectors, matching the documented behavior); they close
    only via ``record_closure`` with explicit closure evidence.

    A ``concern_class`` (or an explicit ``trust_boundary=True``) in
    ``TRUST_BOUNDARY_CONCERN_CLASSES`` may register directly at RED severity —
    a single confirmed trust-boundary occurrence does not need to wait for
    persistence (WS16 item 3). Everything else defaults to AMBER regardless of
    the requested severity, so a caller cannot casually mint a RED concern
    outside the trust-boundary class list.
    """
    root_path = Path(root).resolve()
    now = now_iso or _now_iso()
    cls = concern_class or detector
    is_trust_boundary = bool(trust_boundary) or cls in TRUST_BOUNDARY_CONCERN_CLASSES
    resolved_severity = severity if (is_trust_boundary and severity == RED) else (
        AMBER if severity == RED else severity)
    scope = scope_key or re.sub(r"[^a-z0-9]+", "_", concern.lower()).strip("_")[:40] or "concern"
    concern_id = f"{detector}:{scope}"

    ledger = load_ledger(root_path / _LEDGER_REL)
    active_ids = {p.get("id") for p in (ledger.get("active") or [])}
    if concern_id in active_ids:
        return {"status": "already_active", "id": concern_id}

    probe = {
        "id": concern_id, "detector": detector, "concern_class": cls, "lens": "quant",
        "scope_key": scope, "created_at": now, "created_run": created_run,
        "severity": resolved_severity,
        "concern": concern,
        "trigger_snapshot": {},
        "resolve_hint": resolve_hint or "record_closure(...) with explicit closure evidence",
        "last_evaluated_at": now,
        "observations": [{"run": now[:10], "note": "registered"}],
        "consecutive_observations": 1,
        "evidence_artifact": evidence_artifact,
        "affected_component": affected_component,
        "escalation_threshold": escalation_threshold,
        "owner": _normalize_owner(owner),
        "remediation_status": "open",
        "closure_evidence": None,
        "regression_test_reference": None,
        "trust_boundary": is_trust_boundary,
    }
    new_ledger = {
        "schema_version": ledger.get("schema_version", "1"),
        "active": list(ledger.get("active") or []) + [probe],
        "archive": list(ledger.get("archive") or []),
    }
    if write_files:
        write_ledger(root_path / _LEDGER_REL, new_ledger)
    return {"status": "registered", "id": concern_id, "probe": probe}


def record_closure(
    root: str | Path, concern_id: str, *,
    evidence_artifact: str | None = None, snapshot: dict | None = None,
    note: str = "", closed_by: str = "operator",
    regression_test_reference: str | None = None,
    now_iso: str | None = None, write_files: bool = True,
) -> dict:
    """Attach structured closure evidence to an active concern by id.

    For a ``manual``-detector concern (no evaluator exists — it can never
    auto-resolve), this call retires it immediately: closure evidence is the
    only signal a manual concern can ever receive, so recording it IS the
    resolution. This replaces "operator hand-edits the ledger JSON" with a
    schema-correct call.

    For a detector-tracked concern (D1/D2/D3), this does NOT remove it from
    ``active`` by itself — the evidence is pre-attached and preserved, but
    the concern only archives once its own detector ALSO confirms (on the
    next ``evaluate()`` cycle) that the condition no longer fires. Allowing
    evidence alone to close a still-firing detector concern would reintroduce
    the same silent-close failure mode this fix removes, just via a different
    trigger than age.
    """
    root_path = Path(root).resolve()
    now = now_iso or _now_iso()
    ledger = load_ledger(root_path / _LEDGER_REL)
    active = list(ledger.get("active") or [])
    idx = next((i for i, p in enumerate(active) if p.get("id") == concern_id), None)
    if idx is None:
        return {"status": "not_found", "id": concern_id}

    probe = copy.deepcopy(active[idx])
    probe["closure_evidence"] = {
        "artifact": evidence_artifact, "snapshot": snapshot, "note": note,
        "closed_by": closed_by, "closed_at": now, "backfilled": False,
    }
    if regression_test_reference:
        probe["regression_test_reference"] = regression_test_reference

    # Any concern whose detector has NO registered evaluator (the literal
    # "manual" detector, or any other ad hoc id such as semantic_liveness's
    # findings) can never receive an automatic re-evaluation confirming it
    # cleared — for those, recorded closure evidence IS the resolution.
    if probe.get("detector") not in _EVALUATORS:
        probe["remediation_status"] = "closed"
        probe["resolved_at"] = now
        probe["resolved_run"] = now[:10]
        probe["resolution"] = "closed_with_evidence"
        probe["resolution_detail"] = note or "operator-recorded closure evidence"
        probe["lifetime_days"] = _age_days(probe.get("created_at"), now)
        archive = list(ledger.get("archive") or []) + [probe]
        new_ledger = {"schema_version": ledger.get("schema_version", "1"),
                      "active": [p for i, p in enumerate(active) if i != idx],
                      "archive": archive[-MAX_ARCHIVE:]}
        status = "closed"
    else:
        probe["remediation_status"] = "in_progress"
        active[idx] = probe
        new_ledger = {"schema_version": ledger.get("schema_version", "1"),
                      "active": active, "archive": list(ledger.get("archive") or [])}
        status = "evidence_recorded_awaiting_detector_confirmation"

    if write_files:
        write_ledger(root_path / _LEDGER_REL, new_ledger)
    return {"status": status, "id": concern_id, "probe": probe}
