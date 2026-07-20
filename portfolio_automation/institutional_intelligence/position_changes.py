"""
Position-change engine.

Compares a manager's current effective filing against its previous effective
filing and emits per-security change events. Ordinary shares, calls, and puts
are compared on SEPARATE keys — a share change and an option change for the same
issuer are distinct events and never conflated.

Safety:
  * No division by zero; a NEW position is never reported as an "infinite"
    increase (its share % change is ``None``).
  * A split-like change (shares move by a clean ratio while reported value is
    ~unchanged) is flagged ``possible_split`` and treated as ``unchanged`` — it
    is not a real accumulation/distribution signal.
  * Unresolved identities emit ``identity_unresolved`` (never a fabricated one).
  * With no previous filing, every position is ``comparison_unavailable``
    (first observation), not a flood of "new_position" signals.

Portfolio weight uses the ORDINARY-SHARE value total as the denominator
(options are excluded from the conviction total, per the options rules); option
positions carry raw value but no conviction weight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .schemas import PUT_CALL_CALL, PUT_CALL_NONE, PUT_CALL_PUT, ParsedHolding
from .security_identity import SecurityIdentity

# --- event vocabulary ----------------------------------------------------
EV_NEW = "new_position"
EV_INCREASED = "increased"
EV_UNCHANGED = "unchanged"
EV_REDUCED = "reduced"
EV_EXITED = "exited"
EV_NEW_CALL = "new_call"
EV_INCREASED_CALL = "increased_call"
EV_REDUCED_CALL = "reduced_call"
EV_EXITED_CALL = "exited_call"
EV_NEW_PUT = "new_put"
EV_INCREASED_PUT = "increased_put"
EV_REDUCED_PUT = "reduced_put"
EV_EXITED_PUT = "exited_put"
EV_IDENTITY_UNRESOLVED = "identity_unresolved"
EV_COMPARISON_UNAVAILABLE = "comparison_unavailable"

# A share change within +/- this relative band is "unchanged".
UNCHANGED_SHARE_TOLERANCE = 0.01
# A split-like ratio must be within this fraction of a clean small integer ratio
# AND the reported value must be within this band to be treated as a split.
_SPLIT_RATIOS = (2.0, 3.0, 4.0, 0.5, 1.0 / 3.0, 0.25, 1.5, 2.0 / 3.0)
_SPLIT_RATIO_TOL = 0.03
_SPLIT_VALUE_TOL = 0.10

_OPTION_EVENTS = {
    PUT_CALL_CALL: (EV_NEW_CALL, EV_INCREASED_CALL, EV_REDUCED_CALL, EV_EXITED_CALL),
    PUT_CALL_PUT: (EV_NEW_PUT, EV_INCREASED_PUT, EV_REDUCED_PUT, EV_EXITED_PUT),
}


@dataclass(frozen=True)
class PositionChange:
    symbol: str | None
    cusip: str
    put_call: str
    event: str
    shares_delta: float | None
    shares_pct_change: float | None    # None for new/exit (never infinite)
    value_delta: float | None
    prev_weight: float | None
    curr_weight: float | None
    weight_delta: float | None
    curr_rank: int | None
    prev_rank: int | None
    top10_entry: bool
    filing_age_days: int | None
    identity_resolved: bool
    possible_split: bool = False
    # Price fields are filled by the backtest layer (needs a price panel).
    price_change_qend_to_filing: float | None = None
    price_change_since_filing: float | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PositionChangeSet:
    changes: tuple[PositionChange, ...]
    comparison_available: bool
    portfolio_turnover: float | None
    current_ordinary_total: float


def _key(identity: SecurityIdentity, holding: ParsedHolding) -> tuple[str, str]:
    # Compare on (symbol-or-cusip, put_call) so options stay separate from shares.
    ident = identity.symbol or f"CUSIP:{holding.cusip}"
    return (ident, holding.put_call)


def _ordinary_total(items: list[tuple[ParsedHolding, SecurityIdentity]]) -> float:
    return sum((h.value or 0.0) for h, _ in items if h.put_call == PUT_CALL_NONE)


def _rank_map(items: list[tuple[ParsedHolding, SecurityIdentity]]) -> dict[tuple[str, str], int]:
    # Rank ordinary-share positions by value desc (1 = largest).
    ordinary = [(h, i) for h, i in items if h.put_call == PUT_CALL_NONE]
    ordinary.sort(key=lambda pair: (pair[0].value or 0.0), reverse=True)
    return {_key(i, h): rank for rank, (h, i) in enumerate(ordinary, 1)}


def _is_split_like(prev_shares: float, curr_shares: float,
                   prev_val: float | None, curr_val: float | None) -> bool:
    if prev_shares <= 0 or curr_shares <= 0:
        return False
    ratio = curr_shares / prev_shares
    near_clean = any(abs(ratio - r) / r <= _SPLIT_RATIO_TOL for r in _SPLIT_RATIOS if r != 1.0)
    if not near_clean:
        return False
    if not prev_val or not curr_val or prev_val <= 0:
        return False
    return abs(curr_val - prev_val) / prev_val <= _SPLIT_VALUE_TOL


def _event_for(put_call: str, kind: str) -> str:
    """kind in {new, inc, red, exit}."""
    if put_call in _OPTION_EVENTS:
        new_e, inc_e, red_e, exit_e = _OPTION_EVENTS[put_call]
        return {"new": new_e, "inc": inc_e, "red": red_e, "exit": exit_e}[kind]
    return {"new": EV_NEW, "inc": EV_INCREASED, "red": EV_REDUCED, "exit": EV_EXITED}[kind]


def compute_position_changes(
    current: list[tuple[ParsedHolding, SecurityIdentity]],
    previous: list[tuple[ParsedHolding, SecurityIdentity]] | None,
    *,
    as_of: date | None = None,
    current_filed_at: date | None = None,
) -> PositionChangeSet:
    """Compare current vs previous holdings into change events."""
    curr_total = _ordinary_total(current)
    prev_total = _ordinary_total(previous) if previous else 0.0
    curr_ranks = _rank_map(current)
    prev_ranks = _rank_map(previous) if previous else {}
    filing_age = ((as_of - current_filed_at).days
                  if (as_of and current_filed_at) else None)

    prev_by_key: dict[tuple[str, str], tuple[ParsedHolding, SecurityIdentity]] = {}
    if previous:
        for h, i in previous:
            prev_by_key[_key(i, h)] = (h, i)

    changes: list[PositionChange] = []
    seen_keys: set[tuple[str, str]] = set()

    for h, ident in current:
        key = _key(ident, h)
        seen_keys.add(key)
        curr_w = ((h.value or 0.0) / curr_total) if (h.put_call == PUT_CALL_NONE and curr_total > 0) else None
        curr_rank = curr_ranks.get(key)
        top10 = curr_rank is not None and curr_rank <= 10

        if not ident.resolved:
            changes.append(PositionChange(
                None, h.cusip, h.put_call, EV_IDENTITY_UNRESOLVED, None, None, None,
                None, curr_w, None, curr_rank, None, top10, filing_age, False,
                warnings=(f"identity:{ident.reason}",)))
            continue

        if previous is None:
            changes.append(PositionChange(
                ident.symbol, h.cusip, h.put_call, EV_COMPARISON_UNAVAILABLE, None,
                None, None, None, curr_w, None, curr_rank, None, top10, filing_age, True))
            continue

        prev = prev_by_key.get(key)
        if prev is None:
            changes.append(PositionChange(
                ident.symbol, h.cusip, h.put_call, _event_for(h.put_call, "new"),
                h.shares_or_principal, None, h.value, None, curr_w, None,
                curr_rank, None, top10, filing_age, True))
            continue

        ph, _ = prev
        prev_shares = ph.shares_or_principal or 0.0
        curr_shares = h.shares_or_principal or 0.0
        shares_delta = curr_shares - prev_shares
        value_delta = (h.value or 0.0) - (ph.value or 0.0)
        prev_w = ((ph.value or 0.0) / prev_total) if (h.put_call == PUT_CALL_NONE and prev_total > 0) else None
        weight_delta = (curr_w - prev_w) if (curr_w is not None and prev_w is not None) else None
        prev_rank = prev_ranks.get(key)
        top10_entry = top10 and (prev_rank is None or prev_rank > 10)

        possible_split = _is_split_like(prev_shares, curr_shares, ph.value, h.value)
        if possible_split:
            # A clean share ratio with ~unchanged value is a split, not a trade.
            event = EV_UNCHANGED
            pct = None
            warnings = ("possible_split",)
        else:
            pct = (shares_delta / prev_shares) if prev_shares > 0 else None
            warnings = ()
            if pct is None:
                event = _event_for(h.put_call, "new")
            elif pct > UNCHANGED_SHARE_TOLERANCE:
                event = _event_for(h.put_call, "inc")
            elif pct < -UNCHANGED_SHARE_TOLERANCE:
                event = _event_for(h.put_call, "red")
            else:
                event = EV_UNCHANGED   # shared event for shares + options

        changes.append(PositionChange(
            ident.symbol, h.cusip, h.put_call, event, shares_delta, pct, value_delta,
            prev_w, curr_w, weight_delta, curr_rank, prev_rank, top10_entry,
            filing_age, True, possible_split=possible_split, warnings=warnings))

    # Exits: keys present in previous but not current.
    if previous:
        for key, (ph, pi) in prev_by_key.items():
            if key in seen_keys or not pi.resolved:
                continue
            prev_w = ((ph.value or 0.0) / prev_total) if (ph.put_call == PUT_CALL_NONE and prev_total > 0) else None
            changes.append(PositionChange(
                pi.symbol, ph.cusip, ph.put_call, _event_for(ph.put_call, "exit"),
                -(ph.shares_or_principal or 0.0), None, -(ph.value or 0.0),
                prev_w, None, (None if prev_w is None else -prev_w),
                None, prev_ranks.get(key), False, filing_age, True))

    turnover = None
    if previous and prev_total > 0:
        gross = sum(abs(c.value_delta or 0.0) for c in changes
                    if c.put_call == PUT_CALL_NONE and c.value_delta is not None)
        turnover = min(gross / prev_total, 1.0)

    return PositionChangeSet(
        changes=tuple(changes),
        comparison_available=previous is not None,
        portfolio_turnover=turnover,
        current_ordinary_total=curr_total,
    )
