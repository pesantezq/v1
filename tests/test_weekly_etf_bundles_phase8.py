"""
Phase 8 tests — analysis+health coverage wiring: artifact-registry rows, env-var
registration, and the /weekly-etf-analysis skill membership in /run-all-weekly.
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]

_WEEKLY_ARTIFACTS = [
    "weekly_etf_bundles/latest.json",
    "weekly_etf_bundles/health.json",
    "weekly_etf_bundles/scorecard.json",
    "weekly_etf_bundles/calibration.json",
    "weekly_etf_bundles/attribution.json",
    "weekly_etf_bundles/strat_lab_comparison.json",
    "weekly_etf_bundles/challenger_registry.json",
    "weekly_etf_bundles/email_receipt.json",
]


def test_registry_rows_present_with_consumers():
    reg = yaml.safe_load((REPO / "portfolio_automation" / "artifact_registry.yaml").read_text())
    arts = reg["artifacts"]
    for key in _WEEKLY_ARTIFACTS:
        assert key in arts, f"missing registry row: {key}"
        row = arts[key]
        assert row["cadence"] == "weekly"
        assert row["producer"] == "weekly_etf_bundles"
        assert row["consumers"], f"{key} has no consumers (unjustified debt)"
        assert "weekly-etf-analysis" in row["consumers"]
        assert row["consumer_status"] == "consumed"
        assert row["required"] is False   # absent until first run → info, not RED


def test_every_weekly_artifact_has_a_consumer():
    # The corollary: every artifact the subsystem writes is consumed by >=1 check.
    reg = yaml.safe_load((REPO / "portfolio_automation" / "artifact_registry.yaml").read_text())
    weekly_rows = {k: v for k, v in reg["artifacts"].items() if k.startswith("weekly_etf_bundles/")}
    assert len(weekly_rows) == len(_WEEKLY_ARTIFACTS)
    for k, v in weekly_rows.items():
        assert v["consumers"], f"producer without consumer: {k}"


def test_env_vars_registered():
    from portfolio_automation import env
    names = set()
    for attr in ("ENV_VARS", "REGISTRY", "_ENV_VARS", "ENV_REGISTRY"):
        reg = getattr(env, attr, None)
        if reg:
            for item in reg:
                names.add(getattr(item, "name", None) or (item[0] if isinstance(item, tuple) else None))
            break
    # Fallback: scan source if the registry attr name differs.
    if not names:
        src = (REPO / "portfolio_automation" / "env.py").read_text()
        for n in ("WEEKLY_ETF_BUNDLES_ENABLED", "WEEKLY_ETF_BUNDLES_EMAIL_ENABLED",
                  "WEEKLY_ETF_BUNDLES_EMAIL_DRY_RUN", "WEEKLY_ETF_BUNDLES_EMAIL_TO",
                  "WEEKLY_ETF_BUNDLES_EMAIL_FORCE"):
            assert n in src
        return
    for n in ("WEEKLY_ETF_BUNDLES_ENABLED", "WEEKLY_ETF_BUNDLES_EMAIL_ENABLED",
              "WEEKLY_ETF_BUNDLES_EMAIL_DRY_RUN", "WEEKLY_ETF_BUNDLES_EMAIL_TO",
              "WEEKLY_ETF_BUNDLES_EMAIL_FORCE"):
        assert n in names, f"{n} not registered in env.py"


def test_skill_exists_and_wired_into_weekly_suite():
    assert (REPO / ".claude" / "commands" / "weekly-etf-analysis.md").exists()
    suite = (REPO / ".claude" / "commands" / "run-all-weekly.md").read_text()
    assert "weekly-etf-analysis" in suite


def test_docs_present():
    assert (REPO / "docs" / "WEEKLY_ETF_BUNDLES.md").exists()
    contracts = (REPO / "docs" / "OUTPUT_ARTIFACT_CONTRACTS.md").read_text()
    assert "Weekly ETF Bundle Watchlist Artifacts" in contracts
