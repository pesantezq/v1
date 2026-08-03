# tests/test_scanner_dataset_sufficiency.py
"""Scanner dataset sufficiency must be assessed independently of degraded_mode.

The defect (found 2026-08-03): ``main.py`` already contained the right check —
``len(scanner_candidates) < MIN_TRUSTED_DATASET_SIZE`` — but it was nested inside
``if _scanner_data_health.get("degraded_mode"):``. On the healthy-FMP path
(``fmp_succeeded=True``, ``fallback_used=False``) ``degraded_mode`` is False, so
the branch was unreachable. That single nesting level is why a 3-symbol scanner
universe passed ``scraped_intel.degraded_mode``, ``artifact_registry_status`` and
``pipeline_wiring_status`` for two months.

The guard asked "did we fall back?" when it needed to ask "is this enough?".
"""
import pytest

from degraded_mode import MIN_TRUSTED_DATASET_SIZE, assess_scanner_dataset_sufficiency


def test_empty_dataset_is_flagged():
    assert assess_scanner_dataset_sufficiency(0) == ["empty_dataset"]


def test_small_dataset_is_flagged_on_the_healthy_path():
    """The live 2026-08-03 condition: 3 candidates, FMP healthy, nothing fell
    back. This must now produce a reason where previously it produced none."""
    assert assess_scanner_dataset_sufficiency(3) == ["small_dataset"]


def test_dataset_at_the_floor_is_accepted():
    assert assess_scanner_dataset_sufficiency(MIN_TRUSTED_DATASET_SIZE) == []


def test_healthy_dataset_produces_no_reasons():
    assert assess_scanner_dataset_sufficiency(120) == []


def test_reason_does_not_depend_on_degraded_mode():
    """Sufficiency is a property of the dataset, not of how it was obtained.
    Same count must yield the same verdict either way."""
    assert (
        assess_scanner_dataset_sufficiency(3)
        == assess_scanner_dataset_sufficiency(3, min_size=MIN_TRUSTED_DATASET_SIZE)
    )


def test_min_size_is_overridable():
    assert assess_scanner_dataset_sufficiency(30, min_size=50) == ["small_dataset"]
    assert assess_scanner_dataset_sufficiency(30, min_size=10) == []


def test_negative_count_is_treated_as_empty():
    assert assess_scanner_dataset_sufficiency(-1) == ["empty_dataset"]


@pytest.mark.parametrize("bad", [None, "3", 2.5])
def test_non_integer_counts_fail_closed_as_empty(bad):
    """A missing/garbled count must not read as sufficient."""
    assert assess_scanner_dataset_sufficiency(bad) == ["empty_dataset"]
