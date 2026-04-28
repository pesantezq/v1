"""
decision_engine.py

Unifies outputs from the watchlist scanner, scoring system, conviction layer,
allocation engine, and portfolio state into a single, consistent decision record
per symbol.

No external dependencies. Deterministic. All inputs are plain Python types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Decision strings — exhaustive closed set.
DECISION_BUY = "BUY"
DECISION_SCALE = "SCALE"
DECISION_HOLD = "HOLD"
DECISION_WAIT = "WAIT"
DECISION_AVOID = "AVOID"

# Conviction band literals (must match conviction.py).
BAND_HIGH_CONVICTION = "high_conviction"
BAND_NORMAL = "normal"
BAND_STARTER = "starter"
BAND_OBSERVE = "observe"
BAND_DEFER = "defer"

# Ordered from strongest to weakest — used for capping logic.
_BAND_RANK: dict[str, int] = {
    BAND_HIGH_CONVICTION: 4,
    BAND_NORMAL: 3,
    BAND_STARTER: 2,
    BAND_OBSERVE: 1,
    BAND_DEFER: 0,
}

# Minimum signal_score required to allow a BUY decision.
_BUY_SIGNAL_FLOOR = 0.55

# Confidence threshold below which decisions are capped at HOLD.
_LOW_CONFIDENCE_THRESHOLD = 0.60

# Priority weight distribution (must sum to 1.0).
_PRIORITY_WEIGHTS = {
    "conviction_score": 0.45,
    "signal_score": 0.35,
    "confidence_score": 0.20,
}

# Maximum allocation cap applied inside the engine as a hard safety rail.
_ABSOLUTE_MAX_ALLOCATION_PCT = 0.08  # mirrors allocation_engine max_position_cap

# ---------------------------------------------------------------------------
# Input / Output data classes
# ---------------------------------------------------------------------------


@dataclass
class DecisionInput:
    """
    All inputs required to produce one DecisionRecord.

    Scores are floats in [0, 1] unless noted. Percentages are decimals (0.05 = 5%).
    """

    # Identity
    symbol: str
    strategy_type: str  # "compounder" | "momentum"
    data_mode: str       # "live" | "fallback"

    # Signal layer (from watchlist scanner)
    signal_score: float           # 0–1
    confidence_score: float       # 0–1
    effective_score: float        # 0–1  (post-reliability adjustment)

    # Conviction layer
    conviction_score: float       # 0–1
    conviction_band: str          # one of the BAND_* constants
    sizing_multiplier: float      # 0–1  (from conviction.py)

    # Allocation engine
    suggested_allocation_pct: float  # decimal, e.g. 0.03 = 3%

    # State flags
    degraded_mode: bool = False
    cooldown_active: bool = False

    # Portfolio context
    current_position_pct: float = 0.0   # current holding as % of portfolio
    sector_exposure_pct: float = 0.0    # total sector weight
    available_cash_pct: float = 1.0     # cash as % of portfolio
    sector_cap: float = 0.20            # hard sector cap


@dataclass
class DecisionRecord:
    """
    Final unified decision for one symbol.
    """

    symbol: str
    decision: str                         # BUY | SCALE | HOLD | WAIT | AVOID
    priority: float                       # 0–1 composite priority score
    recommended_allocation_pct: float     # decimal (0.03 = 3%)
    capital_action: str                   # plain-English capital instruction
    decision_reason: str                  # structured explanation
    risk_flags: list[str] = field(default_factory=list)
    confidence: float = 0.0              # output confidence (may differ from input)


# ---------------------------------------------------------------------------
# Step 1 — Risk flag evaluation
# ---------------------------------------------------------------------------


def evaluate_risk_flags(inp: DecisionInput) -> list[str]:
    """
    Return a list of active risk flag strings based on input state.
    Order is deterministic (append order matches the spec).
    """
    flags: list[str] = []

    if inp.degraded_mode or inp.data_mode == "fallback":
        flags.append("degraded_data")

    if inp.cooldown_active:
        flags.append("cooldown_active")

    if inp.signal_score < _BUY_SIGNAL_FLOOR:
        flags.append("weak_signal")

    if inp.sector_exposure_pct >= inp.sector_cap:
        flags.append("sector_overexposed")

    if inp.confidence_score < _LOW_CONFIDENCE_THRESHOLD:
        flags.append("low_confidence")

    return flags


# ---------------------------------------------------------------------------
# Step 2 — Base decision from conviction band + signal
# ---------------------------------------------------------------------------


def _base_decision(inp: DecisionInput) -> str:
    """
    Derive the raw (pre-override) decision from conviction band and signal.

    Mapping:
      high_conviction + strong signal + existing position → SCALE
      high_conviction + strong signal                     → BUY
      high_conviction + weak signal                       → HOLD
      normal + strong signal                              → HOLD (starter buy territory)
      normal + weak signal                                → HOLD
      starter                                             → WAIT
      observe | defer                                     → AVOID
    """
    band = inp.conviction_band
    has_strong_signal = inp.signal_score >= _BUY_SIGNAL_FLOOR
    has_position = inp.current_position_pct > 0.0

    if band == BAND_HIGH_CONVICTION:
        if has_strong_signal:
            return DECISION_SCALE if has_position else DECISION_BUY
        return DECISION_HOLD

    if band == BAND_NORMAL:
        # Normal band earns a HOLD at minimum; a strong signal can justify a
        # cautious starter buy, represented here as HOLD (allocation will be
        # sized small via the conviction multiplier).
        return DECISION_HOLD

    if band == BAND_STARTER:
        return DECISION_WAIT

    # observe or defer
    return DECISION_AVOID


# ---------------------------------------------------------------------------
# Step 3 — Override rules
# ---------------------------------------------------------------------------


# Decision strength ordering — used to cap / downgrade.
_DECISION_RANK: dict[str, int] = {
    DECISION_AVOID: 0,
    DECISION_WAIT: 1,
    DECISION_HOLD: 2,
    DECISION_SCALE: 3,
    DECISION_BUY: 4,
}
_RANK_TO_DECISION: dict[int, str] = {v: k for k, v in _DECISION_RANK.items()}


def _cap_decision(current: str, maximum: str) -> str:
    """Return the weaker of *current* and *maximum*."""
    return _RANK_TO_DECISION[min(_DECISION_RANK[current], _DECISION_RANK[maximum])]


def apply_overrides(decision: str, inp: DecisionInput, flags: list[str]) -> str:
    """
    Apply override rules in priority order.  Each rule can only weaken the
    decision, never strengthen it.

    Rules (highest priority first):
      1. cooldown_active           → cap at WAIT
      2. degraded_mode / fallback  → cap at HOLD
      3. low_confidence            → cap at HOLD
      4. weak_signal + not AVOID   → cap at HOLD
      5. observe/defer conviction  → cap at AVOID (already handled in base,
                                     but guarded here too for safety)
    """
    # Rule 1: cooldown blocks any buy action
    if inp.cooldown_active:
        decision = _cap_decision(decision, DECISION_WAIT)

    # Rule 2: degraded data — reduce confidence in action
    if inp.degraded_mode or inp.data_mode == "fallback":
        decision = _cap_decision(decision, DECISION_HOLD)

    # Rule 3: low confidence caps at HOLD
    if inp.confidence_score < _LOW_CONFIDENCE_THRESHOLD:
        decision = _cap_decision(decision, DECISION_HOLD)

    # Rule 4: weak signal — if decision would be aggressive, pull back
    if "weak_signal" in flags and decision in (DECISION_BUY, DECISION_SCALE):
        decision = _cap_decision(decision, DECISION_HOLD)

    # Rule 5: band-level safety net (observe/defer can never be actionable)
    if _BAND_RANK.get(inp.conviction_band, 0) <= _BAND_RANK[BAND_OBSERVE]:
        decision = _cap_decision(decision, DECISION_AVOID)

    return decision


# ---------------------------------------------------------------------------
# Step 4 — Allocation integration
# ---------------------------------------------------------------------------


def compute_allocation(decision: str, inp: DecisionInput, flags: list[str]) -> float:
    """
    Derive the recommended allocation percentage.

    Logic:
      - Start from suggested_allocation_pct (from allocation_engine).
      - Apply sizing_multiplier (from conviction layer).
      - Apply degraded penalty if needed.
      - Clip to [0, _ABSOLUTE_MAX_ALLOCATION_PCT].
      - Zero-out for AVOID / WAIT decisions.
    """
    if decision in (DECISION_AVOID, DECISION_WAIT):
        return 0.0

    alloc = inp.suggested_allocation_pct * inp.sizing_multiplier

    # Degraded penalty: allocation_engine uses 0.65 — mirror that here.
    if "degraded_data" in flags:
        alloc *= 0.65

    # Sector overexposure: allow only incremental adds, not full sizing.
    if "sector_overexposed" in flags:
        alloc = min(alloc, 0.01)

    # Cash guard: never recommend more than available deployable cash.
    cash_floor = max(0.0, inp.available_cash_pct - 0.05)  # preserve 5% reserve
    alloc = min(alloc, cash_floor)

    # Hard cap
    alloc = min(alloc, _ABSOLUTE_MAX_ALLOCATION_PCT)

    return round(max(alloc, 0.0), 4)


# ---------------------------------------------------------------------------
# Step 5 — Priority score
# ---------------------------------------------------------------------------


def compute_priority(inp: DecisionInput) -> float:
    """
    Weighted composite of conviction_score, signal_score, and confidence_score.
    Returns a float in [0, 1].
    """
    raw = (
        inp.conviction_score * _PRIORITY_WEIGHTS["conviction_score"]
        + inp.signal_score * _PRIORITY_WEIGHTS["signal_score"]
        + inp.confidence_score * _PRIORITY_WEIGHTS["confidence_score"]
    )
    return round(min(max(raw, 0.0), 1.0), 4)


# ---------------------------------------------------------------------------
# Step 6 — Capital action string
# ---------------------------------------------------------------------------


_CAPITAL_ACTION_MAP = {
    DECISION_BUY: "Open new position — deploy {alloc:.1%} of portfolio.",
    DECISION_SCALE: "Scale existing position — add {alloc:.1%} of portfolio.",
    DECISION_HOLD: "Hold current position — no capital movement.",
    DECISION_WAIT: "Stand by — do not deploy capital until conditions improve.",
    DECISION_AVOID: "Pass — no capital action warranted.",
}


def build_capital_action(decision: str, alloc: float) -> str:
    template = _CAPITAL_ACTION_MAP.get(decision, "No action.")
    return template.format(alloc=alloc)


# ---------------------------------------------------------------------------
# Step 7 — Decision reason narrative
# ---------------------------------------------------------------------------


def build_decision_reason(
    decision: str,
    inp: DecisionInput,
    flags: list[str],
    alloc: float,
) -> str:
    """
    Produce a structured explanation string covering:
      - Why this decision was reached.
      - Which factors drove it.
      - What would change the decision.
    """
    parts: list[str] = []

    # --- Why this decision ---
    band_label = inp.conviction_band.replace("_", " ").title()
    parts.append(
        f"Decision={decision} | Band={band_label} | Strategy={inp.strategy_type.title()}"
    )

    # --- Key driving factors ---
    drivers: list[str] = []
    drivers.append(f"conviction={inp.conviction_score:.2f}")
    drivers.append(f"signal={inp.signal_score:.2f}")
    drivers.append(f"confidence={inp.confidence_score:.2f}")
    drivers.append(f"effective={inp.effective_score:.2f}")
    if alloc > 0:
        drivers.append(f"alloc={alloc:.1%}")
    parts.append("Drivers: " + ", ".join(drivers))

    # --- Active overrides that shaped this outcome ---
    if flags:
        parts.append("Overrides active: " + ", ".join(flags))

    # --- What would change the decision ---
    change_hints: list[str] = []

    if "cooldown_active" in flags:
        change_hints.append("cooldown expiry would remove WAIT cap")

    if "degraded_data" in flags:
        change_hints.append("live data feed would lift degraded cap")

    if "low_confidence" in flags:
        change_hints.append(
            f"confidence >={_LOW_CONFIDENCE_THRESHOLD:.0%} would remove HOLD cap"
        )

    if "weak_signal" in flags:
        change_hints.append(
            f"signal >={_BUY_SIGNAL_FLOOR:.0%} would enable BUY/SCALE"
        )

    if "sector_overexposed" in flags:
        change_hints.append(
            f"sector exposure <{inp.sector_cap:.0%} would restore full sizing"
        )

    if decision in (DECISION_AVOID, DECISION_WAIT) and inp.conviction_band in (
        BAND_STARTER,
        BAND_OBSERVE,
        BAND_DEFER,
    ):
        change_hints.append(
            f"conviction band upgrade to '{BAND_NORMAL}' would enable action"
        )

    if change_hints:
        parts.append("To change: " + "; ".join(change_hints) + ".")
    else:
        parts.append("No single factor change would immediately alter this decision.")

    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def make_decision(inp: DecisionInput) -> DecisionRecord:
    """
    Produce a DecisionRecord from a DecisionInput.

    Pipeline:
      1. Evaluate risk flags
      2. Derive base decision from conviction band + signal
      3. Apply override rules
      4. Compute allocation
      5. Compute priority score
      6. Build capital action string
      7. Build decision reason narrative
    """
    flags = evaluate_risk_flags(inp)
    base = _base_decision(inp)
    decision = apply_overrides(base, inp, flags)
    alloc = compute_allocation(decision, inp, flags)
    priority = compute_priority(inp)
    capital_action = build_capital_action(decision, alloc)
    reason = build_decision_reason(decision, inp, flags, alloc)

    # Output confidence: take the lower of input confidence and conviction score
    # to avoid over-stating certainty when either dimension is weak.
    out_confidence = round(min(inp.confidence_score, inp.conviction_score), 4)

    return DecisionRecord(
        symbol=inp.symbol,
        decision=decision,
        priority=priority,
        recommended_allocation_pct=alloc,
        capital_action=capital_action,
        decision_reason=reason,
        risk_flags=flags,
        confidence=out_confidence,
    )


# ---------------------------------------------------------------------------
# Batch helper
# ---------------------------------------------------------------------------


def rank_decisions(inputs: list[DecisionInput]) -> list[DecisionRecord]:
    """
    Process a list of DecisionInputs and return DecisionRecords sorted by
    priority descending.  AVOID decisions are placed last regardless of score.
    """
    records = [make_decision(inp) for inp in inputs]
    records.sort(
        key=lambda r: (0 if r.decision == DECISION_AVOID else 1, r.priority),
        reverse=True,
    )
    return records


# ---------------------------------------------------------------------------
# Example usage (run as __main__ for a quick sanity check)
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    # --- Example 1: High-conviction live signal, no position yet ---
    ex1 = DecisionInput(
        symbol="NVDA",
        strategy_type="compounder",
        data_mode="live",
        signal_score=0.82,
        confidence_score=0.88,
        effective_score=0.79,
        conviction_score=0.84,
        conviction_band="high_conviction",
        sizing_multiplier=1.00,
        suggested_allocation_pct=0.05,
        degraded_mode=False,
        cooldown_active=False,
        current_position_pct=0.00,
        sector_exposure_pct=0.12,
        available_cash_pct=0.25,
        sector_cap=0.20,
    )

    # --- Example 2: Degraded fallback data, cooldown active ---
    ex2 = DecisionInput(
        symbol="PLTR",
        strategy_type="momentum",
        data_mode="fallback",
        signal_score=0.71,
        confidence_score=0.55,
        effective_score=0.60,
        conviction_score=0.72,
        conviction_band="normal",
        sizing_multiplier=0.50,
        suggested_allocation_pct=0.03,
        degraded_mode=True,
        cooldown_active=True,
        current_position_pct=0.02,
        sector_exposure_pct=0.18,
        available_cash_pct=0.10,
        sector_cap=0.20,
    )

    # --- Example 3: Starter band, weak signal ---
    ex3 = DecisionInput(
        symbol="IONQ",
        strategy_type="momentum",
        data_mode="live",
        signal_score=0.40,
        confidence_score=0.65,
        effective_score=0.45,
        conviction_score=0.48,
        conviction_band="starter",
        sizing_multiplier=0.25,
        suggested_allocation_pct=0.01,
        degraded_mode=False,
        cooldown_active=False,
        current_position_pct=0.00,
        sector_exposure_pct=0.05,
        available_cash_pct=0.20,
        sector_cap=0.20,
    )

    # --- Example 4: Existing position, scale opportunity ---
    ex4 = DecisionInput(
        symbol="MSFT",
        strategy_type="compounder",
        data_mode="live",
        signal_score=0.75,
        confidence_score=0.91,
        effective_score=0.80,
        conviction_score=0.87,
        conviction_band="high_conviction",
        sizing_multiplier=1.00,
        suggested_allocation_pct=0.04,
        degraded_mode=False,
        cooldown_active=False,
        current_position_pct=0.06,
        sector_exposure_pct=0.15,
        available_cash_pct=0.18,
        sector_cap=0.20,
    )

    examples = [ex1, ex2, ex3, ex4]
    ranked = rank_decisions(examples)

    print("=" * 72)
    print("DECISION ENGINE — EXAMPLE OUTPUT")
    print("=" * 72)
    for rec in ranked:
        print(f"\nSymbol   : {rec.symbol}")
        print(f"Decision : {rec.decision}")
        print(f"Priority : {rec.priority:.4f}")
        print(f"Alloc    : {rec.recommended_allocation_pct:.2%}")
        print(f"Confidence: {rec.confidence:.4f}")
        print(f"Action   : {rec.capital_action}")
        print(f"Flags    : {rec.risk_flags or '—'}")
        print(f"Reason   : {rec.decision_reason}")
        print("-" * 72)
