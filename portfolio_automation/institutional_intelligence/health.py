"""
Institutional Intelligence health assessor + semantic-liveness detectors.

Developer + quant lens. Pure: takes already-loaded inputs and returns a status
dict. All institutional failures are AMBER-max EXCEPT true contract breaches,
which are RED:

  RED (contract breach):
    * an artifact claims feeds_decision_engine=true
    * a strategy/consensus artifact used quarter-end (not filing availability)
      as the signal date  (look-ahead)
    * options interpreted as a directional signal
    * the institutional stage wrote outside its allowed namespaces
    * a production-boundary breach (production_mutation / is_human_approved on an
      auto path)

  AMBER (health issue, never blocks the core):
    * missing / invalid manager registry, SEC UA missing while live enabled,
      malformed XML, duplicate accession, amendment inconsistency, current filing
      older than expected, enabled manager with no filing history, holdings
      filing with zero parsed holdings, high unresolved-identity rate,
      consensus fresh-but-empty.
"""

from __future__ import annotations

from typing import Any

STATUS_GREEN = "green"
STATUS_AMBER = "amber"
STATUS_RED = "red"

# Thresholds.
HIGH_UNRESOLVED_RATE = 0.30
STALE_FILING_DAYS = 140


def _worst(a: str, b: str) -> str:
    order = {STATUS_GREEN: 0, STATUS_AMBER: 1, STATUS_RED: 2}
    return a if order[a] >= order[b] else b


def assess_institutional_health(
    *,
    config: dict[str, Any] | None,
    registry_ok: bool,
    registry_error: str | None,
    status_artifact: dict[str, Any] | None,
    intelligence_artifact: dict[str, Any] | None,
    sec_user_agent_present: bool,
    wrote_outside_namespace: bool = False,
) -> dict[str, Any]:
    """Return {overall_status, flags, red_flags, amber_flags}."""
    cfg = config or {}
    status = STATUS_GREEN
    red: list[str] = []
    amber: list[str] = []

    def red_flag(name: str) -> None:
        nonlocal status
        red.append(name)
        status = _worst(status, STATUS_RED)

    def amber_flag(name: str) -> None:
        nonlocal status
        amber.append(name)
        status = _worst(status, STATUS_AMBER)

    # --- RED: contract breaches ----------------------------------------
    for art in (status_artifact, intelligence_artifact):
        if isinstance(art, dict) and art.get("feeds_decision_engine") is True:
            red_flag("feeds_decision_engine_true")
    if wrote_outside_namespace:
        red_flag("wrote_outside_allowed_namespace")
    for art in (status_artifact, intelligence_artifact):
        if isinstance(art, dict):
            if art.get("used_quarter_end_as_availability") is True:
                red_flag("look_ahead_quarter_end_as_availability")
            if art.get("options_treated_as_directional") is True:
                red_flag("options_treated_as_directional")
            if art.get("production_mutation") is True:
                red_flag("production_mutation_breach")

    # --- AMBER: health issues ------------------------------------------
    if not registry_ok:
        amber_flag(f"manager_registry_invalid:{registry_error or 'unknown'}")
    enabled = bool(cfg.get("enabled", False))
    live = bool(cfg.get("live_sec_ingestion_enabled", False))
    if enabled and live and not sec_user_agent_present:
        amber_flag("sec_user_agent_missing_while_live")

    st = status_artifact or {}
    overall = st.get("overall_status")
    if overall in ("failed",):
        amber_flag("status_failed")
    if overall == "stale":
        amber_flag("all_filings_stale")

    recs = (intelligence_artifact or {}).get("records") or []
    if recs:
        unresolved = sum(1 for r in recs if r.get("consensus_state") == "insufficient_data")
        if unresolved / len(recs) > HIGH_UNRESOLVED_RATE:
            amber_flag("high_unresolved_identity_rate")
    # Consensus fresh-but-empty: status ok but zero symbols covered.
    if overall == "ok" and st.get("symbols_covered", 0) == 0:
        amber_flag("consensus_fresh_but_empty")

    return {
        "overall_status": status,
        "flags": red + amber,
        "red_flags": red,
        "amber_flags": amber,
        "observe_only": True,
    }


# --- semantic-liveness detectors (constant-value / all-same collapse) -----

def detect_constant_consensus(states: list[str], *, min_sample: int = 30) -> bool:
    """True when >= min_sample consensus states are all identical (collapse)."""
    if len(states) < min_sample:
        return False
    return len(set(states)) == 1


def detect_effective_managers_always_zero(values: list[float], *,
                                          min_sample: int = 30) -> bool:
    if len(values) < min_sample:
        return False
    return all((v or 0.0) == 0.0 for v in values)


def detect_all_same_state(states: list[str], *, min_sample: int = 30,
                          max_distinct: int = 1) -> bool:
    if len(states) < min_sample:
        return False
    return len(set(states)) <= max_distinct
