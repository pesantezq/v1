"""Regression guard for pipeline producer wiring (stale-producer audit, 2026-06-11).

Context: a daily-tool-analysis run found six artifacts flagged stale by the
artifact-registry validator. Tracing each to root cause revealed the recurring
"ships code + tests but leaves the producer unwired" pattern plus one registry
cadence mislabel. This module pins every fix so the producers cannot silently
fall out of their cron driver again, and so the consumers always read fresh data.

Each producer's own behavior is covered by its dedicated suite
(test_market_narratives.py, test_news_evidence_layer.py, test_quant_watch_probes.py,
test_scraped_intel_comparison.py). This module is a wiring/ordering/contract test.
"""

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_DAILY = _ROOT / "scripts" / "run_daily_safe.sh"
_WEEKLY = _ROOT / "scripts" / "run_weekly_safe.sh"
_MAIN = _ROOT / "main.py"
_REGISTRY = _ROOT / "portfolio_automation" / "artifact_registry.yaml"


@pytest.fixture(scope="module")
def daily() -> str:
    return _DAILY.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def weekly() -> str:
    return _WEEKLY.read_text(encoding="utf-8")


# ── Today's primary fix: daily narratives + evidence layer ───────────────────

def test_market_narratives_is_wired(daily: str) -> None:
    assert "run_market_narratives" in daily, (
        "run_daily_safe.sh must invoke run_market_narratives — otherwise "
        "market_narrative_*.json go stale and downstream consumers read old data."
    )


def test_news_evidence_layer_is_wired(daily: str) -> None:
    assert "run_news_evidence_layer" in daily, (
        "run_daily_safe.sh must invoke run_news_evidence_layer — otherwise "
        "news_evidence_layer.json goes stale and memo_enrichment reads old data."
    )


def test_producers_run_before_their_consumers(daily: str) -> None:
    """Ordering: narratives -> evidence layer -> promotion governance / memo."""
    idx_narratives = daily.index("run_market_narratives")
    idx_evidence = daily.index("run_news_evidence_layer")
    idx_promotion = daily.index("run_automatic_promotion_governance")
    idx_memo = daily.index("watchlist_scanner.daily_memo")

    assert idx_narratives < idx_evidence, (
        "market_narratives must run before news_evidence_layer (the evidence "
        "layer consumes market_narrative_*.json)."
    )
    assert idx_evidence < idx_promotion, (
        "news_evidence_layer must run before automatic_promotion_governance."
    )
    assert idx_evidence < idx_memo, (
        "news_evidence_layer must run before the daily memo (memo_enrichment "
        "consumes news_evidence_layer.json)."
    )


def test_narrative_evidence_use_nonblocking_aux_stage(daily: str) -> None:
    assert 'run_aux_stage "Market narratives"' in daily
    assert 'run_aux_stage "News evidence layer"' in daily


# ── #2: quant-watch ledger wired deterministically into the daily cron ───────

def test_quant_watch_is_wired_daily(daily: str) -> None:
    assert "run_quant_watch" in daily, (
        "run_daily_safe.sh must invoke run_quant_watch so quant_watch_status.json "
        "refreshes every cron run rather than depending on the /quant-watch-analysis "
        "LLM skill being invoked."
    )
    assert 'run_aux_stage "Quant-watch probe ledger"' in daily


def test_quant_watch_runs_after_retune_impact(daily: str) -> None:
    """quant-watch consumes retune_impact.json, so it must run after that stage."""
    assert daily.index("run_retune_impact_tracker") < daily.index("run_quant_watch")


# ── #4: weekly + monthly narratives wired into the weekly cron ───────────────

def test_weekly_monthly_narratives_wired(weekly: str) -> None:
    assert "run_market_narratives" in weekly, (
        "run_weekly_safe.sh must invoke run_market_narratives for the weekly + "
        "monthly periods (the daily cron only refreshes the 'daily' period)."
    )
    assert "periods=['weekly']" in weekly
    assert "periods=['monthly']" in weekly


# ── #3: scraped_intel comparison reachable from the daily pipeline ───────────

def test_config_exposes_scraped_intel() -> None:
    """Config must expose scraped_intel so main.py can pass it to the scanner.

    Asserts both the dataclass default (absent key -> inert {"enabled": False})
    and that the real config.json value is surfaced on the loaded Config.
    """
    import dataclasses

    from utils import Config, load_config

    # The dataclass declares scraped_intel as a field (so getattr works and
    # main.py's getattr(config, 'scraped_intel', None) returns the real value).
    field_names = {f.name for f in dataclasses.fields(Config)}
    assert "scraped_intel" in field_names, "Config must declare a scraped_intel field"

    # Real config.json round-trips through Config.from_dict onto the attribute.
    cfg = load_config(str(_ROOT / "config.json"), record_history=False)
    si = getattr(cfg, "scraped_intel", None)
    assert isinstance(si, dict), "config.scraped_intel must be exposed as a dict"


def test_main_passes_scraped_intel_config_to_scanner() -> None:
    """main.py must pass scraped_intel_config to the watchlist scanner call.

    Omitting it left scraped_intel_config=None, silently skipping the scraped-intel
    layer (and its comparison report) in the daily pipeline.
    """
    text = _MAIN.read_text(encoding="utf-8")
    assert "scraped_intel_config=getattr(config, 'scraped_intel'" in text, (
        "main.py's _run_watchlist_scanner call must pass scraped_intel_config; "
        "without it scraped_intel_comparison.json never regenerates in the daily run."
    )


# ── #1: registry cadence corrected to weekly for gate_retune_suggestions ─────

def test_gate_retune_cadence_is_weekly() -> None:
    """gate_retune_suggestions is produced weekly (run_weekly_safe.sh), not daily."""
    import yaml

    reg = yaml.safe_load(_REGISTRY.read_text(encoding="utf-8"))
    # artifacts may live under a top-level key; find the entry robustly
    entry = None
    for container in (reg, *(v for v in reg.values() if isinstance(v, dict))):
        if isinstance(container, dict) and "gate_retune_suggestions.json" in container:
            entry = container["gate_retune_suggestions.json"]
            break
    assert entry is not None, "gate_retune_suggestions.json missing from registry"
    assert entry.get("cadence") == "weekly", (
        "gate_retune_suggestions cadence must be weekly (produced by "
        "run_weekly_safe.sh Monday cron); daily produced false-stale signals."
    )


# ── Schwab read-only sync activation (2026-06-12) ────────────────────────────

def _registry_entry(name: str):
    import yaml
    reg = yaml.safe_load(_REGISTRY.read_text(encoding="utf-8"))
    for container in (reg, *(v for v in reg.values() if isinstance(v, dict))):
        if isinstance(container, dict) and name in container:
            return container[name]
    return None


def test_schwab_sync_is_wired_daily(daily: str) -> None:
    assert "schwab_sync" in daily, (
        "run_daily_safe.sh must invoke schwab_sync so broker_sync_status.json "
        "refreshes every cron run (daily read-only reconciliation)."
    )
    assert 'run_aux_stage "Schwab broker sync"' in daily, (
        "Schwab sync must run through the non-blocking run_aux_stage wrapper so a "
        "Schwab API / token failure degrades broker_sync_status and never aborts "
        "the pipeline."
    )


def test_schwab_sync_runs_before_daily_run_status(daily: str) -> None:
    """broker_sync_status must be fresh before Stages 11/12/13 count it."""
    assert daily.index("schwab_sync") < daily.index("run_daily_run_status"), (
        "schwab_sync must run before run_daily_run_status so daily_run_status, "
        "the registry validator, and the wiring probe see fresh broker data."
    )


def test_broker_sync_status_cadence_is_daily() -> None:
    entry = _registry_entry("broker_sync_status.json")
    assert entry is not None, "broker_sync_status.json missing from registry"
    assert entry.get("cadence") == "daily", (
        "broker_sync_status is always-producible and now refreshed by the daily "
        "cron stage, so its cadence must be daily."
    )


def test_schwab_advisor_artifacts_stay_on_demand() -> None:
    """The 4 advisor artifacts only populate post-auth; daily cadence would
    manufacture false-stale flags while unconfigured or on a failed sync."""
    for name in ("schwab_portfolio_snapshot.json", "schwab_positions.json",
                 "portfolio_reconciliation.json", "portfolio_config_update_proposal.json"):
        entry = _registry_entry(name)
        assert entry is not None, f"{name} missing from registry"
        assert entry.get("cadence") == "on_demand", (
            f"{name} must stay on_demand (only exists after a live authenticated sync)."
        )
