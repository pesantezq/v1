"""
Institutional (13F) tilt primitive + Strategy Lab tactic + variants.

Sandbox-only, observe-only, feeds_decision_engine=false. The tilt is PURE,
DETERMINISTIC, POINT-IN-TIME-safe, LONG-ONLY, BOUNDED, and NORMALIZED. It never
adds a symbol merely because one famous manager initiated it — a qualifying
signal must clear minimum consensus confidence AND minimum effective-independent
managers. The 10% sleeve / 2% per-position caps are TESTED empirically by the
Strategy Lab, not assumed.
"""

from __future__ import annotations

from dataclasses import dataclass

from .tactics import TimeVaryingTactic, _normalize

# Default caps (mirror config/base.json:institutional_intelligence.strategy).
DEFAULT_MAX_TOTAL_SLEEVE = 0.10
DEFAULT_MAX_NEW_POSITION = 0.02
DEFAULT_MAX_EXISTING_TILT = 0.02
DEFAULT_MAX_DISTRIBUTION_TRIM = 0.02
DEFAULT_MIN_CONFIDENCE = 0.55
DEFAULT_MIN_EFFECTIVE_MANAGERS = 1.5

_ACCUM = "accumulation"
_DIST = "distribution"


@dataclass(frozen=True)
class InstitutionalCaps:
    max_total_sleeve: float = DEFAULT_MAX_TOTAL_SLEEVE
    max_new_position: float = DEFAULT_MAX_NEW_POSITION
    max_existing_tilt: float = DEFAULT_MAX_EXISTING_TILT
    max_distribution_trim: float = DEFAULT_MAX_DISTRIBUTION_TRIM
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    min_effective_managers: float = DEFAULT_MIN_EFFECTIVE_MANAGERS


def _direction(consensus_state: str | None, score: float) -> str | None:
    if consensus_state in ("strong_accumulation", "moderate_accumulation",
                           "crowded_accumulation") or (score > 0):
        return _ACCUM
    if consensus_state in ("strong_distribution", "moderate_distribution",
                           "crowded_distribution") or (score < 0):
        return _DIST
    return None


def apply_institutional_tilt(
    core_weights: dict[str, float],
    signals: dict[str, dict],
    caps: InstitutionalCaps | None = None,
    *,
    use_strategy_fit: bool = False,
    crowding_aware: bool = False,
    contrarian: bool = False,
) -> dict[str, float]:
    """Apply a bounded institutional tilt to ``core_weights``.

    ``signals``: {symbol: {consensus_score, consensus_confidence,
    effective_independent_managers, consensus_state, crowding_score,
    strategy_fit}}. A signal that fails the confidence / effective-manager gate
    (or is stale, i.e. absent) produces NO tilt. Returns long-only, normalized
    weights that sum to 1.
    """
    caps = caps or InstitutionalCaps()
    weights = dict(core_weights)
    sleeve_used = 0.0

    # Deterministic order: strongest absolute signal first, then symbol.
    def _key(item):
        sym, sig = item
        return (-abs(float(sig.get("consensus_score") or 0.0)), sym)

    for symbol, sig in sorted(signals.items(), key=_key):
        conf = float(sig.get("consensus_confidence") or 0.0)
        eff = float(sig.get("effective_independent_managers") or 0.0)
        if conf < caps.min_confidence or eff < caps.min_effective_managers:
            continue  # not funded on one/weak/stale manager — no tilt
        score = float(sig.get("consensus_score") or 0.0)
        state = sig.get("consensus_state")
        crowding = float(sig.get("crowding_score") or 0.0)
        fit = float(sig.get("strategy_fit") or 1.0) if use_strategy_fit else 1.0
        crowded = state in ("crowded_accumulation", "crowded_distribution")

        direction = _direction(state, score)
        if direction is None:
            continue

        is_existing = symbol in core_weights and core_weights[symbol] > 0

        if direction == _ACCUM:
            if contrarian and crowded:
                # Contrarian: treat a crowded accumulation as a caution -> trim.
                tilt = -min(caps.max_distribution_trim, abs(score) * caps.max_distribution_trim)
            else:
                cap = caps.max_existing_tilt if is_existing else caps.max_new_position
                magnitude = abs(score) * cap * conf * fit
                if crowding_aware and crowded:
                    magnitude *= (1.0 - crowding)   # dampen crowded adds
                tilt = min(cap, magnitude)
        else:  # distribution -> trim (long-only: never go short)
            cap = caps.max_distribution_trim
            tilt = -min(cap, abs(score) * cap * conf)

        # Respect the total sleeve budget.
        if sleeve_used + abs(tilt) > caps.max_total_sleeve:
            remaining = caps.max_total_sleeve - sleeve_used
            if remaining <= 0:
                break
            tilt = remaining if tilt > 0 else -remaining
        sleeve_used += abs(tilt)

        new_w = weights.get(symbol, 0.0) + tilt
        weights[symbol] = max(0.0, new_w)   # long-only floor

    return _normalize(weights)


class InstitutionalTactic(TimeVaryingTactic):
    """PIT-safe institutional tilt tactic. ``signals_by_date`` maps a date to a
    per-symbol signals dict; ``target_weights_asof`` uses the nearest snapshot
    with date <= the evaluated date (no look-ahead)."""

    def __init__(self, core_weights: dict[str, float], *,
                 signals_by_date: dict[str, dict] | None = None,
                 caps: InstitutionalCaps | None = None,
                 use_strategy_fit: bool = False, crowding_aware: bool = False,
                 contrarian: bool = False, single_manager: bool = False,
                 tactic_id: str = "institutional_consensus",
                 name: str | None = None):
        super().__init__(
            tactic_id=tactic_id, name=name or tactic_id.replace("_", " ").title(),
            source="institutional", target_weights=dict(core_weights),
            metadata={
                "feeds_decision_engine": False, "observe_only": True,
                "sandbox_only": True, "single_manager_diagnostic": single_manager,
                "materialization": {"rules": [
                    "bounded long-only institutional sleeve <= "
                    f"{(caps or InstitutionalCaps()).max_total_sleeve:.0%}",
                    "gated on min consensus confidence + min effective managers",
                    "options never contribute direction (see options_interpretation)"]}},
            approximate=False)
        self.core = dict(core_weights)
        self.signals_by_date = signals_by_date or {}
        self.caps = caps or InstitutionalCaps()
        self.use_strategy_fit = use_strategy_fit
        self.crowding_aware = crowding_aware
        self.contrarian = contrarian
        self.single_manager = single_manager

    def _signals_asof(self, date: str) -> dict[str, dict]:
        keys = sorted(k for k in self.signals_by_date if k <= date)  # no look-ahead
        return self.signals_by_date.get(keys[-1], {}) if keys else {}

    def target_weights_asof(self, date: str, ctx: dict | None = None) -> dict[str, float]:
        signals = self._signals_asof(date)
        if not signals:
            return _normalize(dict(self.core))   # no signal -> anchor unchanged
        return apply_institutional_tilt(
            self.core, signals, self.caps, use_strategy_fit=self.use_strategy_fit,
            crowding_aware=self.crowding_aware, contrarian=self.contrarian)


def institutional_variants(core_weights: dict[str, float],
                           signals_by_date: dict[str, dict],
                           *, caps: InstitutionalCaps | None = None) -> list[InstitutionalTactic]:
    """The institutional Strategy-Lab variants, all over identical inputs."""
    caps = caps or InstitutionalCaps()
    common = dict(signals_by_date=signals_by_date, caps=caps)
    return [
        InstitutionalTactic(core_weights, single_manager=True,
                            tactic_id="institutional_single_manager",
                            name="Institutional (single-manager diagnostic)", **common),
        InstitutionalTactic(core_weights, tactic_id="institutional_consensus",
                            name="Institutional Consensus", **common),
        InstitutionalTactic(core_weights, use_strategy_fit=True,
                            tactic_id="institutional_consensus_strategy_fit",
                            name="Institutional Consensus + Strategy Fit", **common),
        InstitutionalTactic(core_weights, crowding_aware=True,
                            tactic_id="institutional_consensus_crowding_aware",
                            name="Institutional Consensus (crowding-aware)", **common),
        InstitutionalTactic(core_weights, contrarian=True,
                            tactic_id="institutional_contrarian_crowding_diagnostic",
                            name="Institutional Contrarian (crowding diagnostic)", **common),
    ]
