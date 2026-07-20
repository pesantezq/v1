"""
Options interpretation taxonomy.

13F options disclosures are AMBIGUOUS: the filing exposes neither strike,
expiration, premium, written-vs-purchased direction, offsetting shorts, nor the
full hedge structure. Therefore:

  * A PUT is NEVER auto-mapped to bearish; a CALL is NEVER auto-mapped to bullish.
  * An option's directional contribution DEFAULTS TO ZERO and stays zero unless
    a future, explicitly-documented rule justifies otherwise (none does today).
  * Options may only lower an interpretability score / raise a hedge-complexity
    penalty — never add directional conviction.
  * "Capital invested" is never computed from option notional, and share and
    option notionals are never summed into one conviction total.

Only deterministic, documented portfolio-context logic may label a *possible*
hedge structure, and even then it is flagged an inference with retained
uncertainty.
"""

from __future__ import annotations

from dataclasses import dataclass

from .schemas import PUT_CALL_CALL, PUT_CALL_NONE, PUT_CALL_PUT

# Taxonomy.
OPT_COMMON_EQUITY_LONG = "common_equity_long"
OPT_CALL_EXPOSURE = "call_exposure"
OPT_PUT_EXPOSURE = "put_exposure"
OPT_PROTECTED_LONG_POSSIBLE = "protected_long_possible"
OPT_SECTOR_BETA_HEDGE_POSSIBLE = "sector_beta_hedge_possible"
OPT_RELATIVE_VALUE_POSSIBLE = "relative_value_structure_possible"
OPT_COMPLEX_OR_UNKNOWN = "complex_or_unknown"
OPT_INSUFFICIENT_CONTEXT = "insufficient_context"

# Interpretability penalty magnitudes (0 = fully interpretable, 1 = opaque).
# Rationale: ordinary equity is clean (0). A plain call/put is partially opaque
# (0.35). A possible hedge structure is more opaque (0.55) because net exposure
# cannot be reconstructed. High-options-complexity managers get the max (0.75).
_PENALTY_EQUITY = 0.0
_PENALTY_PLAIN_OPTION = 0.35
_PENALTY_POSSIBLE_HEDGE = 0.55
_PENALTY_COMPLEX = 0.75


@dataclass(frozen=True)
class OptionInterpretation:
    taxonomy: str
    directional_contribution: float   # ALWAYS 0.0 for options (invariant)
    interpretability_penalty: float   # [0,1]
    is_inference: bool
    note: str


def classify_option_context(
    put_call: str,
    *,
    manager_options_complexity: str = "low",
    has_concentrated_longs: bool = False,
    underlier_is_broad_market: bool = False,
) -> OptionInterpretation:
    """Classify one holding's option context. Deterministic; no LLM.

    ``has_concentrated_longs`` + ``underlier_is_broad_market`` gate the ONLY
    inference we permit: a concentrated-longs manager holding puts on a broad
    market/mega-cap underlier *may* be running a sector-beta hedge — surfaced as
    ``sector_beta_hedge_possible`` (inference), never as a confirmed short.
    """
    if put_call == PUT_CALL_NONE:
        return OptionInterpretation(OPT_COMMON_EQUITY_LONG, 0.0, _PENALTY_EQUITY,
                                    False, "Ordinary shares — directional long input.")

    complex_mgr = manager_options_complexity == "high"

    if put_call == PUT_CALL_PUT:
        if has_concentrated_longs and underlier_is_broad_market:
            return OptionInterpretation(
                OPT_SECTOR_BETA_HEDGE_POSSIBLE, 0.0, _PENALTY_POSSIBLE_HEDGE, True,
                "Possible sector/beta hedge around concentrated longs — INFERENCE, "
                "not a confirmed short; net exposure cannot be reconstructed.")
        tax = OPT_COMPLEX_OR_UNKNOWN if complex_mgr else OPT_PUT_EXPOSURE
        pen = _PENALTY_COMPLEX if complex_mgr else _PENALTY_PLAIN_OPTION
        return OptionInterpretation(tax, 0.0, pen, False,
                                    "Put exposure — NOT interpreted as bearish; "
                                    "directional contribution is zero.")

    if put_call == PUT_CALL_CALL:
        tax = OPT_COMPLEX_OR_UNKNOWN if complex_mgr else OPT_CALL_EXPOSURE
        pen = _PENALTY_COMPLEX if complex_mgr else _PENALTY_PLAIN_OPTION
        return OptionInterpretation(tax, 0.0, pen, False,
                                    "Call exposure — NOT interpreted as bullish; "
                                    "directional contribution is zero.")

    return OptionInterpretation(OPT_INSUFFICIENT_CONTEXT, 0.0, _PENALTY_PLAIN_OPTION,
                                False, "Unrecognized option marker; no directional read.")
