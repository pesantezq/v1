"""
Signal-tagging proposer  (additive | advisory-only | observe-only | proposes-only)

Pattern-Improvement Loop — sub-project D2. The first real_signals_live run showed ~70% of
history signals carry NO alert_basis (→ classified UNKNOWN), and the SIGNAL_SCORE family
maps to no registry signal_id (so those signals can never receive a weight). This module
quantifies the gap and proposes, as bounded owner-gated REVIEW items:

  1. registry_entry — a registry signal_id for each mapped family that lacks one
     (suggested neutral default_weight 0.0), and
  2. backfill_inference — a deterministic rule to infer alert_basis for untagged rows
     from fields present on the row (signal_score / volume_ratio), with the count it
     would newly tag.

It mutates NOTHING — not the registry, not the signal producer, not scoring. It only
writes one review artifact to OutputNamespace.POLICY. Any failure degrades to a status
dict; never raises.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from backtesting.signal_sources import _map_basis, _representative_pattern

_OBSERVE_ONLY = True
_GENERATED_BY = "backtesting.tagging_proposer"
_UNKNOWN = "UNKNOWN"
_UNTAGGED_PROPOSE_THRESHOLD = 0.10  # propose a backfill rule once >=10% are untagged


def _load_registry_ids(registry_path: str) -> set[str]:
    """Read the set of registry signal_ids. Read-only; {} on missing/malformed."""
    import yaml
    p = Path(registry_path)
    if not p.exists():
        return set()
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return set()
    ids: set[str] = set()
    for entry in (doc.get("signals") or []):
        if isinstance(entry, dict) and entry.get("signal_id") is not None:
            ids.add(str(entry["signal_id"]))
    return ids


def _family_covered(family: str, registry_ids: set[str]) -> bool:
    """A family is covered if a registry signal_id equals it or is a directional
    refinement of it (e.g. STRONG_MOVE → STRONG_MOVE_UP / STRONG_MOVE_DOWN)."""
    return any(sid == family or sid.startswith(family + "_") for sid in registry_ids)


def _family_of(sig: dict) -> str:
    """Representative family for a signal, robust to BOTH representations:
    a normalized harness signal carries `patterns`/`pattern` (alert_basis already
    consumed by signal_sources._normalize_row), while a raw watchlist row carries
    `alert_basis`. Falls through to UNKNOWN."""
    patterns = sig.get("patterns")
    if isinstance(patterns, (list, tuple)) and patterns:
        return _representative_pattern([str(p).upper() for p in patterns])
    pattern = sig.get("pattern")
    if pattern:
        return str(pattern).upper()
    return _representative_pattern(_map_basis(sig.get("alert_basis")))


def _is_untagged(sig: dict) -> bool:
    """Untagged = the signal resolves to no informative family (UNKNOWN), whether it
    arrived raw (empty alert_basis) or normalized (pattern UNKNOWN)."""
    return _family_of(sig) == _UNKNOWN


def propose_tagging_fixes(signals: list[dict], *,
                          registry_path: str = "config/signal_registry.yaml") -> dict[str, Any]:
    """Quantify the alert_basis coverage gap and propose owner-gated tagging fixes.

    Returns ``{observe_only, proposed_only, advisory_only, generated_by, status, total,
    untagged_count, untagged_pct, family_distribution, families_missing_registry_id,
    proposals, rationale}``. Never raises (degrades to status dict).
    """
    try:
        total = len(signals)
        if total == 0:
            return {
                "observe_only": _OBSERVE_ONLY, "proposed_only": True, "advisory_only": True,
                "generated_by": _GENERATED_BY, "status": "ok", "total": 0,
                "untagged_count": 0, "untagged_pct": 0.0, "family_distribution": {},
                "families_missing_registry_id": [], "proposals": [],
                "rationale": "no signals; nothing to assess",
            }

        registry_ids = _load_registry_ids(registry_path)
        untagged = sum(1 for s in signals if _is_untagged(s))
        backfillable = sum(1 for s in signals if _is_untagged(s) and s.get("signal_score") is not None)

        fams = Counter(_family_of(s) for s in signals)
        family_distribution = dict(fams)

        mapped_families = [f for f in fams if f != _UNKNOWN]
        missing = [f for f in mapped_families if not _family_covered(f, registry_ids)]

        proposals: list[dict[str, Any]] = []
        for fam in sorted(missing):
            proposals.append({
                "kind": "registry_entry",
                "signal_id": fam,
                "suggested_default_weight": 0.0,
                "rationale": (f"'{fam}' is emitted ({fams[fam]} signals) but has no registry "
                              "signal_id, so it can never receive a weight. Propose a neutral "
                              "(0.0) registry entry so Step 4 can evaluate it once OOS matures."),
            })

        untagged_pct = round(untagged / total, 4)
        if untagged_pct >= _UNTAGGED_PROPOSE_THRESHOLD:
            proposals.append({
                "kind": "backfill_inference",
                "rule": ("when alert_basis is empty: infer ['signal_score'] if signal_score is "
                         "present; add 'volume_spike' if volume_ratio >= 2.0"),
                "would_tag": backfillable,
                "rationale": (f"{untagged}/{total} ({untagged_pct:.0%}) signals carry no "
                              "alert_basis and fall through to UNKNOWN, starving per-pattern "
                              "attribution. Backfilling from fields already on the row would "
                              f"tag {backfillable} of them. Rule is a review spec only — wiring "
                              "it into the scanner is a separate, explicitly-approved change."),
            })

        return {
            "observe_only": _OBSERVE_ONLY, "proposed_only": True, "advisory_only": True,
            "generated_by": _GENERATED_BY, "status": "ok", "total": total,
            "untagged_count": untagged, "untagged_pct": untagged_pct,
            "family_distribution": family_distribution,
            "families_missing_registry_id": sorted(missing),
            "proposals": proposals,
            "rationale": ("Tagging coverage assessment. Proposes registry entries for "
                          "unmapped families + a backfill rule for untagged rows. "
                          "Proposes only — applies nothing."),
        }
    except Exception as exc:  # observe-only: degrade, never raise
        return {
            "observe_only": _OBSERVE_ONLY, "proposed_only": True, "advisory_only": True,
            "generated_by": _GENERATED_BY, "status": "degraded", "error": str(exc),
            "total": 0, "untagged_count": 0, "untagged_pct": 0.0,
            "family_distribution": {}, "families_missing_registry_id": [], "proposals": [],
        }


def _markdown(payload: dict[str, Any]) -> str:
    L = ["# Signal-Tagging Proposal",
         "",
         "> Observe-only, proposes-only. Review artifact for owner approval; mutates "
         "neither the registry nor the signal producer.",
         "",
         f"- Status: {payload.get('status')}  |  untagged: {payload.get('untagged_count')}"
         f"/{payload.get('total')} ({payload.get('untagged_pct')})  |  "
         f"families missing registry id: {payload.get('families_missing_registry_id')}",
         "",
         payload.get("rationale", ""),
         ""]
    props = payload.get("proposals") or []
    if props:
        L += ["## Proposals", ""]
        for p in props:
            if p["kind"] == "registry_entry":
                L.append(f"- **registry_entry** `{p['signal_id']}` "
                         f"(default_weight {p['suggested_default_weight']}): {p['rationale']}")
            else:
                L.append(f"- **backfill_inference** (would tag {p['would_tag']}): "
                         f"{p['rule']} — {p['rationale']}")
    return "\n".join(L) + "\n"


def write_tagging_proposal(payload: dict[str, Any], base_dir: str = "outputs") -> Path:
    """Write the proposal JSON+MD to OutputNamespace.POLICY; return the JSON path."""
    from portfolio_automation.data_governance import (
        OutputNamespace, safe_write_json, safe_write_text,
    )
    safe_write_text(OutputNamespace.POLICY, "signal_tagging_proposal.md",
                    _markdown(payload), base_dir=base_dir)
    return safe_write_json(OutputNamespace.POLICY, "signal_tagging_proposal.json",
                           payload, base_dir=base_dir)
