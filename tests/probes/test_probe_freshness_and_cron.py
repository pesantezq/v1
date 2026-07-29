"""E5 probes -- session-aware freshness and cron-exit-zero-but-invalid.

Scenario 7  -- a stale weekly artifact is consumed by a daily gate.
Scenario 20 -- a cron exits zero after producing stale or semantically
               invalid output.

Both probes document STILL-OPEN gaps (F8.2 / F8.1 respectively -- neither
appears in the reliability-program's implemented-changes table). Per the
task's instruction for open items, each asserts the current honest state and
names the open item explicitly, rather than fabricating a passing "fix".
"""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from portfolio_automation import artifact_registry as AR
from portfolio_automation import run_manifest as RM

from tests.probes.assertions import assert_artifact_fresh_for_session

NOW = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)  # a Tuesday


# ---------------------------------------------------------------------------
# Scenario 7 -- F8.2: no reader-specific ("needs data as-of-session") concept
# exists alongside the producer-cadence-only staleness window.
# ---------------------------------------------------------------------------


def test_registry_cadence_window_correctly_tolerates_a_healthy_weekly_artifact():
    """Positive control: the REAL registry row for `gate_retune_suggestions.json`
    (weekly cadence, produced Monday 08:00, consumed by daily-tool-analysis
    per the registry's own `consumers` list) is 6 days old here -- well within
    its own 192h/8-day cadence window. `is_stale` must say False; this is
    correct behaviour, not the gap."""
    registry = AR.load_registry()
    row = registry["artifacts"]["gate_retune_suggestions.json"]
    assert "daily-tool-analysis" in row["consumers"]
    assert row["cadence"] == "weekly"

    age_hours = 6 * 24.0  # produced last Monday, it's now Sunday-equivalent age
    assert AR.is_stale(row, age_hours) is False


def test_session_aware_reader_freshness_gap_STILL_OPEN():
    """STILL OPEN (F8.2): nothing in the repo distinguishes "fresh enough for
    the artifact's OWN weekly cadence" from "fresh enough for a DAILY reader
    that needs this-session's data." The registry's cadence window (192h)
    happily tolerates an artifact that is, from a same-session-daily-reader's
    point of view, several sessions stale. Demonstrated here against the
    session-aware helper (this probe suite's own generic tool, not a
    production dependency -- there is nothing in production to call): an
    artifact generated the previous Monday, read by a Tuesday daily gate one
    week later, is > 1 session stale under a session-aware check even though
    the registry's own cadence window would still call it 'not stale'."""
    registry = AR.load_registry()
    row = registry["artifacts"]["gate_retune_suggestions.json"]

    generated_at = (NOW - timedelta(days=8)).isoformat()  # last-but-one Monday
    age_hours = 8 * 24.0
    # Registry (producer-cadence-only) says fine:
    assert AR.is_stale(row, age_hours) is False

    # Session-aware reader check (what a genuinely daily-cadence consumer of
    # this artifact would need) correctly flags it as stale for THAT purpose:
    with pytest.raises(AssertionError, match="completed weekday session"):
        assert_artifact_fresh_for_session(
            generated_at, now=NOW, max_sessions_stale=1,
            context="gate_retune_suggestions.json (as read by daily-tool-analysis)")

    # Confirm this reader-specific concept genuinely does not exist in
    # production today: artifact_registry.py has no per-consumer/per-reader
    # cadence override, only a single row-level `cadence`/`staleness_hours_override`.
    src = inspect.getsource(AR)
    assert "reader_cadence" not in src and "per_consumer_freshness" not in src, (
        "if a reader-specific freshness concept has been added to "
        "artifact_registry.py, update this probe to assert it is applied "
        "instead of asserting its absence")


# ---------------------------------------------------------------------------
# Scenario 20 -- F8.1: the one safeguard built for "cron exits zero after
# mixing today's run with a stale artifact" is dead code.
# ---------------------------------------------------------------------------


def test_coherent_run_ids_correctly_detects_a_mixed_run():
    """The safeguard function itself is correct (this is not the bug)."""
    rid = "2026-07-28_daily_official"
    fresh = [{"run_id": rid, "name": "a"}, {"run_id": rid, "name": "b"}]
    mixed = [{"run_id": rid, "name": "a"}, {"run_id": "2026-07-21_daily_official", "name": "stale_b"}]

    assert RM.coherent_run_ids(rid, fresh) is True
    assert RM.coherent_run_ids(rid, mixed) is False


def _grep_repo_for_calls(root: Path, symbol: str, *, exclude: tuple[str, ...]) -> list[str]:
    """Minimal, dependency-free call-site scan: every .py file under *root*
    (excluding paths containing any of *exclude*) whose text contains
    `symbol(` outside of its own definition line. General enough to reuse
    for any 'is this safeguard actually wired up' probe."""
    hits: list[str] = []
    for p in root.rglob("*.py"):
        rel = str(p.relative_to(root))
        if any(ex in rel for ex in exclude):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if f"{symbol}(" in text and f"def {symbol}(" not in text:
            hits.append(rel)
    return hits


def test_coherent_run_ids_is_wired_into_a_production_consumer():
    """CLOSED by Phase D1 (`3287b37d`). F8.1 was that `coherent_run_ids()` --
    built and tested precisely to catch a cron exiting zero after silently
    combining today's run with a stale artifact -- had no production caller.
    A caller now exists; this half of the probe keeps the WIRING from silently
    disappearing again. The half that matters more is below: that the wiring
    actually FLAGS a mismatch."""
    root = Path(__file__).resolve().parents[2]  # repo root
    callers = _grep_repo_for_calls(
        root, "coherent_run_ids",
        exclude=("run_manifest.py", "tests/", ".venv", "node_modules"))
    assert callers, (
        "coherent_run_ids() has no production caller -- F8.1 has re-opened: the "
        "safeguard for stale-artifact mixing exists but nothing invokes it")


def test_run_coherence_flags_a_decision_plan_built_under_a_different_run(tmp_path):
    """The behavioural half the old probe's failure message asked for: a
    mismatched run_id must actually be reported, not merely passed to a
    function. A cron that regenerates decision_plan.json out of band -- the
    exact F8.1 scenario -- must come back `coherent: False` and name the
    offending artifact."""
    from portfolio_automation.daily_run_status import check_run_coherence

    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    (tmp_path / "outputs" / "sandbox").mkdir(parents=True)
    (tmp_path / "outputs" / "latest" / "decision_plan.json").write_text(
        json.dumps({"run_id": "run-STALE-from-a-previous-run"}), encoding="utf-8")
    (tmp_path / "outputs" / "sandbox" / "daily_input_snapshot.json").write_text(
        json.dumps({"run_id": "run-TODAY"}), encoding="utf-8")

    result = check_run_coherence(tmp_path, {"run_id": "run-TODAY"})
    assert result["coherent"] is False, (
        "a decision_plan carrying a foreign run_id must be reported incoherent")
    assert result["mismatched"] == ["decision_plan"]


def test_run_coherence_reports_unknown_rather_than_incoherent_when_nothing_is_present(tmp_path):
    """Fail-closed direction check. Absence of artifacts must read as UNKNOWN
    (`None`), never as a confident `False` -- inventing an incoherence verdict
    from missing data is the same defect class the B4 correction closed in
    regime_coverage."""
    from portfolio_automation.daily_run_status import check_run_coherence

    result = check_run_coherence(tmp_path, {"run_id": "run-TODAY"})
    assert result["coherent"] is None
    assert result["mismatched"] == []


def test_cron_exit_zero_with_stale_input_is_not_caught_by_pipeline_wiring_probe():
    """Cross-check against `pipeline_wiring_probe.py` (the module that DOES
    exist to catch unwired producers, per MEMORY 'Stale-producer audit') --
    confirm it does not itself close this specific gap (it audits producer
    wiring, not run-id coherence), so this probe is not duplicating existing
    coverage."""
    from portfolio_automation import pipeline_wiring_probe as PWP
    src = inspect.getsource(PWP)
    assert "coherent_run_ids" not in src, (
        "pipeline_wiring_probe now references coherent_run_ids -- if wired "
        "in, update this probe (and the one above) to reflect the closed gap")
