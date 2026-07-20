"""
Deterministic security-identity resolution.

Maps a 13F holding (issuer / CUSIP / optional FIGI) to an application symbol via
a strict priority chain — and NEVER silently guesses a ticker:

  1. Exact FIGI, when present and mapped.
  2. Exact CUSIP, from a versioned local mapping table.
  3. Existing application symbol mappings.
  4. Conservative issuer/class matching (only an UNAMBIGUOUS exact normalized
     issuer name → single symbol; anything ambiguous stays unresolved).
  5. Unresolved — recorded with an explicit reason.

Point-in-time contract: a CUSIP/FIGI is a TIMELESS security identifier and may
be used regardless of date. A CUSIP/FIGI → TICKER mapping, however, can change
over time (ticker changes, re-listings), so those mappings carry effective
windows and ``resolve_asof`` will not use a mapping outside its window — unless
the entry is flagged ``timeless`` (identity that never changes). This prevents a
later ticker from being projected backward onto an earlier filing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# Resolution methods (most→least authoritative).
METHOD_FIGI = "figi_exact"
METHOD_CUSIP = "cusip_exact"
METHOD_APP_SYMBOL = "app_symbol_map"
METHOD_ISSUER = "issuer_exact_match"
METHOD_UNRESOLVED = "unresolved"

# Unresolved reasons.
REASON_NO_MAPPING = "no_mapping"
REASON_AMBIGUOUS_ISSUER = "ambiguous_issuer"
REASON_MAPPING_OUT_OF_WINDOW = "mapping_out_of_effective_window"
REASON_NO_CUSIP = "missing_cusip"


@dataclass(frozen=True)
class MappingEntry:
    symbol: str
    effective_from: date | None = None
    effective_to: date | None = None
    timeless: bool = False
    source: str = "local"

    def usable_on(self, as_of: date | None) -> bool:
        if self.timeless or as_of is None:
            return True
        if self.effective_from is not None and as_of < self.effective_from:
            return False
        if self.effective_to is not None and as_of > self.effective_to:
            return False
        return True


@dataclass(frozen=True)
class SecurityIdentity:
    cusip: str | None
    figi: str | None
    symbol: str | None
    method: str
    resolved: bool
    provenance: str | None = None
    mapping_effective_from: date | None = None
    reason: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


def normalize_issuer(name: str | None) -> str:
    """Normalize an issuer name for conservative exact matching."""
    if not name:
        return ""
    out = name.upper().strip()
    for suffix in (" INC", " INC.", " CORP", " CORP.", " CO", " CO.", " LTD",
                   " PLC", " LLC", " CLASS A", " CLASS B", " COM", " THE"):
        if out.endswith(suffix):
            out = out[: -len(suffix)].strip()
    return " ".join(out.split())


class SecurityIdentityResolver:
    def __init__(
        self,
        *,
        cusip_map: dict[str, list[MappingEntry]] | None = None,
        figi_map: dict[str, list[MappingEntry]] | None = None,
        app_symbol_map: dict[str, list[MappingEntry]] | None = None,
        issuer_index: dict[str, set[str]] | None = None,
    ) -> None:
        self._cusip = {k.upper(): v for k, v in (cusip_map or {}).items()}
        self._figi = {k.upper(): v for k, v in (figi_map or {}).items()}
        self._app = {k.upper(): v for k, v in (app_symbol_map or {}).items()}
        # issuer_index: normalized issuer name -> set of candidate symbols. A
        # single-candidate set is an unambiguous match; >1 is ambiguous.
        self._issuer = {normalize_issuer(k): set(v) for k, v in (issuer_index or {}).items()}

    def _pick(self, entries: list[MappingEntry] | None,
              as_of: date | None) -> MappingEntry | None:
        if not entries:
            return None
        usable = [e for e in entries if e.usable_on(as_of)]
        if not usable:
            return None
        # Deterministic: prefer timeless, then latest effective_from, then symbol.
        return sorted(
            usable,
            key=lambda e: (e.timeless, e.effective_from or date.min, e.symbol),
            reverse=True,
        )[0]

    def resolve(self, *, cusip: str | None, figi: str | None,
                issuer_name: str | None, as_of: date | None = None) -> SecurityIdentity:
        cu = cusip.upper() if cusip else None
        fg = figi.upper() if figi else None

        if cu is None:
            return SecurityIdentity(cu, fg, None, METHOD_UNRESOLVED, False,
                                    reason=REASON_NO_CUSIP)

        # 1) FIGI exact.
        if fg:
            entry = self._pick(self._figi.get(fg), as_of)
            if entry:
                return SecurityIdentity(cu, fg, entry.symbol, METHOD_FIGI, True,
                                        provenance=entry.source,
                                        mapping_effective_from=entry.effective_from)

        # 2) CUSIP exact.
        entry = self._pick(self._cusip.get(cu), as_of)
        if entry:
            return SecurityIdentity(cu, fg, entry.symbol, METHOD_CUSIP, True,
                                    provenance=entry.source,
                                    mapping_effective_from=entry.effective_from)

        # 3) App symbol map (keyed by CUSIP).
        entry = self._pick(self._app.get(cu), as_of)
        if entry:
            return SecurityIdentity(cu, fg, entry.symbol, METHOD_APP_SYMBOL, True,
                                    provenance=entry.source,
                                    mapping_effective_from=entry.effective_from)

        # If a mapping EXISTS for this CUSIP but is out of window, say so — do
        # not fall through to a fuzzy guess.
        if cu in self._cusip or cu in self._app or (fg and fg in self._figi):
            return SecurityIdentity(cu, fg, None, METHOD_UNRESOLVED, False,
                                    reason=REASON_MAPPING_OUT_OF_WINDOW)

        # 4) Conservative issuer match — UNAMBIGUOUS exact only.
        candidates = self._issuer.get(normalize_issuer(issuer_name))
        if candidates and len(candidates) == 1:
            symbol = next(iter(candidates))
            return SecurityIdentity(cu, fg, symbol, METHOD_ISSUER, True,
                                    provenance="issuer_index",
                                    warnings=("issuer_name_match_lower_confidence",))
        if candidates and len(candidates) > 1:
            return SecurityIdentity(cu, fg, None, METHOD_UNRESOLVED, False,
                                    reason=REASON_AMBIGUOUS_ISSUER)

        # 5) Unresolved — never guess.
        return SecurityIdentity(cu, fg, None, METHOD_UNRESOLVED, False,
                                reason=REASON_NO_MAPPING)
