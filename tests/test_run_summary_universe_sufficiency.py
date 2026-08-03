# tests/test_run_summary_universe_sufficiency.py
"""The scanner universe's sufficiency must reach an artifact a check can read.

Analysis+Health Coverage Requirement (CLAUDE.md): a new signal without a
consumer is debt. Before 2026-08-03 ``scraped_intel_run_summary.json`` already
carried ``scanner.symbol_count: 3`` every day and NOTHING escalated on it —
the number was published but never judged. These tests pin the verdict
alongside the count so the daily tier has something to triage.
"""
import json

from degraded_mode import MIN_TRUSTED_DATASET_SIZE
from scraped_intel.run_summary import build_run_summary


def _build(symbols, tmp_path, **kw):
    return build_run_summary(
        run_mode="daily",
        fmp_attempted=True,
        fmp_succeeded=True,
        fallback_used=False,
        watchlist_source="fmp",
        symbols_processed=list(symbols),
        output_dir=str(tmp_path),
        **kw,
    )


def test_thin_universe_is_judged_insufficient_on_the_healthy_path(tmp_path):
    """The live 2026-08-03 shape: 3 symbols, FMP healthy, nothing fell back."""
    summary = _build(["NVDA", "LLY", "AMD"], tmp_path)
    suff = summary["scanner"]["universe_sufficiency"]
    assert suff["sufficient"] is False
    assert suff["reasons"] == ["small_dataset"]
    assert suff["candidate_count"] == 3
    assert suff["trust_floor"] == MIN_TRUSTED_DATASET_SIZE
    # The surrounding degraded_mode verdict stays honest — nothing DID fall back.
    assert summary["degraded_mode"] is False


def test_healthy_universe_is_judged_sufficient(tmp_path):
    summary = _build([f"S{i}" for i in range(100)], tmp_path)
    suff = summary["scanner"]["universe_sufficiency"]
    assert suff["sufficient"] is True
    assert suff["reasons"] == []
    assert suff["candidate_count"] == 100


def test_empty_universe_is_distinguished_from_merely_thin(tmp_path):
    suff = _build([], tmp_path)["scanner"]["universe_sufficiency"]
    assert suff["reasons"] == ["empty_dataset"]


def test_verdict_is_persisted_to_the_json_artifact(tmp_path):
    """A consumer reads the file, not the return value."""
    _build(["NVDA", "LLY", "AMD"], tmp_path)
    written = json.loads((tmp_path / "scraped_intel_run_summary.json").read_text())
    assert written["scanner"]["universe_sufficiency"]["sufficient"] is False


def test_existing_scanner_fields_are_preserved(tmp_path):
    """Additive change — the block's existing contract must not shift."""
    scanner = _build(["NVDA", "LLY", "AMD"], tmp_path)["scanner"]
    for key in (
        "fmp_attempted", "fmp_succeeded", "fallback_used", "watchlist_source",
        "symbols_processed", "symbol_count", "data_fallback_triggered",
    ):
        assert key in scanner, f"{key} disappeared from the scanner block"
    assert scanner["symbol_count"] == 3
