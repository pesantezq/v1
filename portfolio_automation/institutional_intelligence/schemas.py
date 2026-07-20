"""
Schemas + strict validation for the Institutional Intelligence subsystem.

Phase 1 covers the **manager registry** contract. Later phases extend this
module with filing/holdings/scoring/consensus schemas.

The manager registry is version-controlled configuration: a curated, bounded
set of institutional managers with documented, point-in-time-versionable
metadata. Manager metadata is a mix of FACTS (CIK — must be operator-verified)
and documented subjective PRIORS (cloneability, manager_quality_prior) — the
priors are inputs, never claimed as fact, and are surfaced with rationale.

Validation is strict and fail-closed: duplicate CIKs, out-of-range scores,
missing effective dates, unsupported enum values, contradictory flags, or an
enabled manager without a verified valid CIK all raise
:class:`RegistryValidationError`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

# ---------------------------------------------------------------------------
# Filing + holding vocabularies (Phase 3)
# ---------------------------------------------------------------------------

# 13F filing form types. HR = holdings report (has an information table);
# NT = notice (holdings reported by another manager — NO information table).
FORM_13F_HR = "13F-HR"
FORM_13F_HR_A = "13F-HR/A"
FORM_13F_NT = "13F-NT"
FORM_13F_NT_A = "13F-NT/A"

HOLDINGS_FORMS: frozenset[str] = frozenset({FORM_13F_HR, FORM_13F_HR_A})
NOTICE_FORMS: frozenset[str] = frozenset({FORM_13F_NT, FORM_13F_NT_A})
ALL_13F_FORMS: frozenset[str] = HOLDINGS_FORMS | NOTICE_FORMS
AMENDMENT_FORMS: frozenset[str] = frozenset({FORM_13F_HR_A, FORM_13F_NT_A})

# Put/call marker taxonomy as reported in the information table (raw, not
# interpreted as directional — interpretation lives in Phase 6).
PUT_CALL_NONE = "none"      # ordinary shares / principal
PUT_CALL_PUT = "put"
PUT_CALL_CALL = "call"
PUT_CALL_VALUES: frozenset[str] = frozenset({PUT_CALL_NONE, PUT_CALL_PUT, PUT_CALL_CALL})

# ---------------------------------------------------------------------------
# Controlled vocabularies (extend deliberately; validation rejects unknowns)
# ---------------------------------------------------------------------------

STRATEGY_ARCHETYPES: frozenset[str] = frozenset({
    "thematic_ai_infrastructure",
    "thematic_technology",
    "value",
    "quality_compounder",
    "activist",
    "macro_multistrategy",
    "sector_specialist",
})

EXPECTED_HORIZONS: frozenset[str] = frozenset({
    "short", "medium", "medium_long", "long",
})

CONCENTRATION_STYLES: frozenset[str] = frozenset({
    "concentrated", "moderate", "diversified",
})

TURNOVER_CLASSES: frozenset[str] = frozenset({
    "low", "moderate", "high",
})

OPTIONS_COMPLEXITY_LEVELS: frozenset[str] = frozenset({
    "low", "medium", "high",
})

# EDGAR CIK: 10-digit zero-padded string.
_CIK_RE = re.compile(r"^\d{10}$")

# A unit-interval score must lie in [0, 1].
_SCORE_MIN = 0.0
_SCORE_MAX = 1.0

# A market maker's 13F is dealer inventory, not conviction: its cloneability
# MUST be capped low. Enforced so a "market_maker: true, cloneability: 0.8"
# contradiction cannot slip through.
MARKET_MAKER_MAX_CLONEABILITY = 0.30

_SCORE_FIELDS = ("cloneability", "manager_quality_prior")


class RegistryValidationError(ValueError):
    """Raised when the manager registry violates a strict invariant."""


@dataclass(frozen=True)
class ManagerRecord:
    """One institutional manager's point-in-time-versionable metadata."""

    internal_id: str
    display_name: str
    cik: str
    enabled: bool
    cik_verified: bool
    strategy_archetype: str
    expected_horizon: str
    concentration_style: str
    turnover_class: str
    cloneability: float
    manager_quality_prior: float
    options_complexity: str
    market_maker: bool
    specialization: tuple[str, ...]
    effective_from: date
    effective_to: date | None
    rationale: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def is_effective_on(self, as_of: date) -> bool:
        """True when this record's effective window covers ``as_of``.

        Point-in-time contract: a backtest at ``as_of`` may only consult a
        record whose ``[effective_from, effective_to]`` window contains it.
        """
        if as_of < self.effective_from:
            return False
        if self.effective_to is not None and as_of > self.effective_to:
            return False
        return True


@dataclass(frozen=True)
class FilingRef:
    """A discovered 13F filing reference (pre-parse).

    ``filed_at`` (public availability) is the point-in-time signal timestamp —
    NEVER ``report_period`` (quarter-end). ``accession`` is the stable identity.
    """

    cik: str
    accession: str
    form_type: str
    filed_at: date
    report_period: date | None
    primary_doc: str | None = None
    is_amendment: bool = False
    amendment_number: int | None = None

    @property
    def is_holdings(self) -> bool:
        """True only for holdings reports (13F-HR/A) — NOT notices (13F-NT)."""
        return self.form_type in HOLDINGS_FORMS

    @property
    def is_notice(self) -> bool:
        return self.form_type in NOTICE_FORMS


@dataclass(frozen=True)
class ParsedHolding:
    """One parsed information-table row. Raw reported fields only — no
    interpretation, no derived conviction, no ticker guessing."""

    issuer_name: str
    class_title: str
    cusip: str
    value: float | None            # reported USD value (as filed; units handled by parser)
    shares_or_principal: float | None
    share_principal_type: str | None   # "SH" | "PRN"
    put_call: str = PUT_CALL_NONE
    figi: str | None = None
    investment_discretion: str | None = None
    voting_sole: float | None = None
    voting_shared: float | None = None
    voting_none: float | None = None
    other_managers: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ParsedFiling:
    """The result of parsing one filing's information table."""

    accession: str
    form_type: str
    holdings: tuple[ParsedHolding, ...]
    parse_warnings: tuple[str, ...] = field(default_factory=tuple)
    is_notice: bool = False

    @property
    def holdings_count(self) -> int:
        return len(self.holdings)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise RegistryValidationError(msg)


def _parse_date(value: object, field_name: str, ctx: str) -> date:
    if isinstance(value, date):
        return value
    _require(
        isinstance(value, str) and bool(value.strip()),
        f"{ctx}: {field_name} must be an ISO date string (YYYY-MM-DD)",
    )
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:  # noqa: BLE001 - re-raised as domain error
        raise RegistryValidationError(
            f"{ctx}: {field_name} is not a valid ISO date: {value!r}"
        ) from exc


def validate_manager(raw: dict, internal_id: str) -> ManagerRecord:
    """Validate a single raw manager dict into a :class:`ManagerRecord`.

    Raises :class:`RegistryValidationError` on any violation.
    """
    ctx = f"manager '{internal_id}'"
    _require(isinstance(raw, dict), f"{ctx}: entry must be a mapping")

    display_name = raw.get("display_name")
    _require(isinstance(display_name, str) and bool(display_name.strip()),
             f"{ctx}: display_name is required")

    cik = raw.get("cik")
    _require(isinstance(cik, str) and _CIK_RE.match(cik or ""),
             f"{ctx}: cik must be a 10-digit zero-padded string (got {cik!r})")

    enabled = raw.get("enabled", False)
    _require(isinstance(enabled, bool), f"{ctx}: enabled must be a boolean")
    cik_verified = raw.get("cik_verified", False)
    _require(isinstance(cik_verified, bool),
             f"{ctx}: cik_verified must be a boolean")
    # A manager may only be ENABLED once its CIK is operator-verified. This
    # keeps the registry honest: we never ingest against an unverified CIK.
    _require(not (enabled and not cik_verified),
             f"{ctx}: enabled=true requires cik_verified=true "
             f"(verify the CIK against EDGAR before enabling)")

    archetype = raw.get("strategy_archetype")
    _require(archetype in STRATEGY_ARCHETYPES,
             f"{ctx}: strategy_archetype {archetype!r} not in {sorted(STRATEGY_ARCHETYPES)}")

    horizon = raw.get("expected_horizon")
    _require(horizon in EXPECTED_HORIZONS,
             f"{ctx}: expected_horizon {horizon!r} not in {sorted(EXPECTED_HORIZONS)}")

    conc = raw.get("concentration_style")
    _require(conc in CONCENTRATION_STYLES,
             f"{ctx}: concentration_style {conc!r} not in {sorted(CONCENTRATION_STYLES)}")

    turnover = raw.get("turnover_class")
    _require(turnover in TURNOVER_CLASSES,
             f"{ctx}: turnover_class {turnover!r} not in {sorted(TURNOVER_CLASSES)}")

    options_complexity = raw.get("options_complexity")
    _require(options_complexity in OPTIONS_COMPLEXITY_LEVELS,
             f"{ctx}: options_complexity {options_complexity!r} not in "
             f"{sorted(OPTIONS_COMPLEXITY_LEVELS)}")

    scores: dict[str, float] = {}
    for sf in _SCORE_FIELDS:
        val = raw.get(sf)
        _require(isinstance(val, (int, float)) and not isinstance(val, bool),
                 f"{ctx}: {sf} must be a number in [0,1]")
        fval = float(val)
        _require(_SCORE_MIN <= fval <= _SCORE_MAX,
                 f"{ctx}: {sf} {fval} out of range [0,1]")
        scores[sf] = fval

    market_maker = raw.get("market_maker", False)
    _require(isinstance(market_maker, bool),
             f"{ctx}: market_maker must be a boolean")
    # Contradiction guard: a market maker cannot be highly cloneable.
    _require(not (market_maker and scores["cloneability"] > MARKET_MAKER_MAX_CLONEABILITY),
             f"{ctx}: market_maker=true requires cloneability <= "
             f"{MARKET_MAKER_MAX_CLONEABILITY} (dealer inventory is not conviction)")

    specialization = raw.get("specialization") or []
    _require(isinstance(specialization, list)
             and all(isinstance(s, str) for s in specialization),
             f"{ctx}: specialization must be a list of strings")

    rationale = raw.get("rationale") or raw.get("notes")
    # Rationale is REQUIRED (documented reasoning for inclusion + priors).
    if isinstance(rationale, list):
        rationale = " ".join(str(x) for x in rationale)
    _require(isinstance(rationale, str) and bool(rationale.strip()),
             f"{ctx}: a non-empty rationale is required")

    eff_from = _parse_date(raw.get("effective_from"), "effective_from", ctx)
    eff_to_raw = raw.get("effective_to")
    eff_to = None if eff_to_raw in (None, "", "null") else _parse_date(
        eff_to_raw, "effective_to", ctx)
    _require(eff_to is None or eff_to >= eff_from,
             f"{ctx}: effective_to must be >= effective_from")

    notes = raw.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]
    _require(isinstance(notes, list),
             f"{ctx}: notes must be a list or string")

    return ManagerRecord(
        internal_id=internal_id,
        display_name=display_name.strip(),
        cik=cik,
        enabled=enabled,
        cik_verified=cik_verified,
        strategy_archetype=archetype,
        expected_horizon=horizon,
        concentration_style=conc,
        turnover_class=turnover,
        cloneability=scores["cloneability"],
        manager_quality_prior=scores["manager_quality_prior"],
        options_complexity=options_complexity,
        market_maker=market_maker,
        specialization=tuple(specialization),
        effective_from=eff_from,
        effective_to=eff_to,
        rationale=rationale.strip(),
        notes=tuple(str(n) for n in notes),
    )
