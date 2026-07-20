"""Phase 1 tests — manager registry schema + strict validation.

Covers: valid registry, duplicate CIK, invalid score, overlapping effective
periods, unknown archetype, market-maker cloneability handling, the shipped
seed registry, enabled-requires-verified-CIK, and point-in-time queries.

All synthetic dicts — no network, no on-disk dependency except the one test
that validates the committed seed registry.
"""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path

import pytest

from portfolio_automation.institutional_intelligence import manager_registry as mr
from portfolio_automation.institutional_intelligence.schemas import (
    MARKET_MAKER_MAX_CLONEABILITY,
    RegistryValidationError,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_PATH = _REPO_ROOT / "config" / "institutional_managers.yaml"


def _manager(**overrides) -> dict:
    base = {
        "display_name": "Test Manager LP",
        "cik": "0000000010",
        "enabled": False,
        "cik_verified": False,
        "strategy_archetype": "value",
        "expected_horizon": "long",
        "concentration_style": "moderate",
        "turnover_class": "low",
        "cloneability": 0.7,
        "manager_quality_prior": 0.6,
        "options_complexity": "low",
        "market_maker": False,
        "specialization": ["financials"],
        "effective_from": "2025-01-01",
        "effective_to": None,
        "rationale": "Test archetype exemplar.",
    }
    base.update(overrides)
    return base


def _registry(managers: dict) -> dict:
    return {"schema_version": 1, "managers": managers}


# --- valid ---------------------------------------------------------------

def test_valid_registry():
    reg = mr.validate_registry_dict(_registry({"a": _manager()}))
    assert reg.schema_version == 1
    assert len(reg.all_records()) == 1
    assert reg.by_cik("0000000010").internal_id == "a"


def test_seed_registry_is_valid_and_inert():
    reg = mr.load_registry(_SEED_PATH)
    records = reg.all_records()
    assert len(records) >= 5
    # The shipped registry MUST be inert: nothing enabled, nothing verified.
    assert reg.enabled_records() == []
    assert all(not r.cik_verified for r in records)
    # Archetype diversity is present.
    archetypes = {r.strategy_archetype for r in records}
    assert {"value", "activist", "macro_multistrategy", "sector_specialist"} <= archetypes
    # The low-cloneability macro control exists.
    macro = next(r for r in records if r.strategy_archetype == "macro_multistrategy")
    assert macro.cloneability <= 0.30


# --- duplicate CIK -------------------------------------------------------

def test_duplicate_cik_rejected():
    data = _registry({
        "a": _manager(cik="0000000010"),
        "b": _manager(cik="0000000010", display_name="Other LP"),
    })
    with pytest.raises(RegistryValidationError, match="duplicate CIK"):
        mr.validate_registry_dict(data)


# --- invalid score -------------------------------------------------------

@pytest.mark.parametrize("bad", [1.5, -0.1, "high", None])
def test_invalid_cloneability_rejected(bad):
    with pytest.raises(RegistryValidationError):
        mr.validate_registry_dict(_registry({"a": _manager(cloneability=bad)}))


def test_invalid_manager_quality_prior_rejected():
    with pytest.raises(RegistryValidationError):
        mr.validate_registry_dict(_registry({"a": _manager(manager_quality_prior=2.0)}))


# --- overlapping effective periods --------------------------------------

def test_overlapping_effective_periods_rejected():
    data = _registry({
        # same internal_id via re-versioning requires the SAME key; simulate by
        # two managers sharing internal_id is impossible in a dict, so model the
        # re-version as a list under one key is not supported — instead the
        # registry keys are unique. Overlap is checked per internal_id, so build
        # two records with the same internal_id by using the same CIK + key.
        "mgr": _manager(cik="0000000011", effective_from="2025-01-01", effective_to="2025-06-30"),
    })
    # Single window is fine.
    mr.validate_registry_dict(data)
    # Now craft an explicit overlap by validating two records for one id.
    from portfolio_automation.institutional_intelligence.schemas import validate_manager
    r1 = validate_manager(_manager(cik="0000000011", effective_from="2025-01-01",
                                   effective_to="2025-06-30"), "mgr")
    r2 = validate_manager(_manager(cik="0000000011", effective_from="2025-05-01",
                                   effective_to=None), "mgr")
    with pytest.raises(RegistryValidationError, match="overlapping"):
        mr._check_overlapping_windows([r1, r2])


def test_non_overlapping_reversioned_windows_ok():
    from portfolio_automation.institutional_intelligence.schemas import validate_manager
    r1 = validate_manager(_manager(cik="0000000011", effective_from="2025-01-01",
                                   effective_to="2025-06-30"), "mgr")
    r2 = validate_manager(_manager(cik="0000000011", effective_from="2025-07-01",
                                   effective_to=None), "mgr")
    mr._check_overlapping_windows([r1, r2])  # no raise
    mr._check_duplicate_ciks([r1, r2])       # same id reusing CIK is allowed


# --- unknown archetype ---------------------------------------------------

def test_unknown_archetype_rejected():
    with pytest.raises(RegistryValidationError, match="strategy_archetype"):
        mr.validate_registry_dict(_registry({"a": _manager(strategy_archetype="crypto_degen")}))


@pytest.mark.parametrize("field,bad", [
    ("expected_horizon", "forever"),
    ("concentration_style", "ultra"),
    ("turnover_class", "extreme"),
    ("options_complexity", "quantum"),
])
def test_unknown_enum_values_rejected(field, bad):
    with pytest.raises(RegistryValidationError):
        mr.validate_registry_dict(_registry({"a": _manager(**{field: bad})}))


# --- market-maker cloneability handling ---------------------------------

def test_market_maker_high_cloneability_rejected():
    data = _registry({"a": _manager(market_maker=True, cloneability=0.8)})
    with pytest.raises(RegistryValidationError, match="market_maker"):
        mr.validate_registry_dict(data)


def test_market_maker_low_cloneability_ok():
    reg = mr.validate_registry_dict(_registry({
        "a": _manager(market_maker=True, cloneability=MARKET_MAKER_MAX_CLONEABILITY),
    }))
    assert reg.all_records()[0].market_maker is True


# --- enabled requires verified CIK --------------------------------------

def test_enabled_without_verified_cik_rejected():
    data = _registry({"a": _manager(enabled=True, cik_verified=False)})
    with pytest.raises(RegistryValidationError, match="cik_verified"):
        mr.validate_registry_dict(data)


def test_enabled_with_verified_cik_ok():
    reg = mr.validate_registry_dict(_registry({
        "a": _manager(enabled=True, cik_verified=True),
    }))
    assert reg.enabled_records()[0].enabled is True


def test_bad_cik_format_rejected():
    with pytest.raises(RegistryValidationError, match="cik"):
        mr.validate_registry_dict(_registry({"a": _manager(cik="12345")}))


def test_missing_effective_from_rejected():
    m = _manager()
    del m["effective_from"]
    with pytest.raises(RegistryValidationError, match="effective_from"):
        mr.validate_registry_dict(_registry({"a": m}))


def test_missing_rationale_rejected():
    m = _manager()
    del m["rationale"]
    with pytest.raises(RegistryValidationError, match="rationale"):
        mr.validate_registry_dict(_registry({"a": m}))


# --- point-in-time queries ----------------------------------------------

def test_effective_on_respects_window():
    from portfolio_automation.institutional_intelligence.schemas import validate_manager
    rec = validate_manager(_manager(enabled=True, cik_verified=True,
                                    effective_from="2025-01-01",
                                    effective_to="2025-12-31"), "a")
    reg = mr.ManagerRegistry([rec], schema_version=1)
    assert reg.effective_on(date(2025, 6, 1)) == [rec]
    assert reg.effective_on(date(2024, 12, 31)) == []   # before window
    assert reg.effective_on(date(2026, 1, 1)) == []      # after window


def test_effective_on_enabled_only_filter():
    from portfolio_automation.institutional_intelligence.schemas import validate_manager
    disabled = validate_manager(_manager(cik="0000000021", enabled=False), "a")
    reg = mr.ManagerRegistry([disabled], schema_version=1)
    assert reg.effective_on(date(2025, 6, 1), enabled_only=True) == []
    assert reg.effective_on(date(2025, 6, 1), enabled_only=False) == [disabled]


def test_unsupported_schema_version_rejected():
    with pytest.raises(RegistryValidationError, match="schema_version"):
        mr.validate_registry_dict({"schema_version": 99, "managers": {}})
