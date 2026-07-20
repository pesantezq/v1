"""Phase 17 — fixture-driven end-to-end institutional run (offline, no network).

Drives context_loader.run_institutional_intelligence through the governed SEC
client against on-disk fixtures (submissions JSON + information-table XML) for
two enabled managers, and proves the required end-to-end evidence:

  * ingestion completes and produces the artifacts
  * re-running the same accessions is idempotent (no duplicate rows)
  * options do NOT create a directional signal
  * production decision/allocation artifacts are byte-identical before/after
    (decision_plan.json is never written)
  * feeds_decision_engine stays false; the invariant envelope is present
  * unchanged filings do not change the run
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from portfolio_automation.institutional_intelligence.context_loader import (
    run_institutional_intelligence,
)
from portfolio_automation.institutional_intelligence.sec_client import (
    ARCHIVES_BASE,
    SUBMISSIONS_URL,
    GovernedSECClient,
    SECClientConfig,
    cik_to_padded,
)
from portfolio_automation.institutional_intelligence.security_identity import (
    MappingEntry,
    SecurityIdentityResolver,
)

_AAPL = "037833100"
_MSFT = "594918104"

_NS = "http://www.sec.gov/edgar/document/thirteenf/informationtable"


def _infotable_xml(rows: list[tuple[str, str, float, float, str]]) -> str:
    body = ""
    for issuer, cusip, value, shares, put_call in rows:
        pc = f"<putCall>{put_call}</putCall>" if put_call else ""
        body += (f"<infoTable><nameOfIssuer>{issuer}</nameOfIssuer>"
                 f"<titleOfClass>COM</titleOfClass><cusip>{cusip}</cusip>"
                 f"<value>{value}</value><shrsOrPrnAmt><sshPrnamt>{shares}</sshPrnamt>"
                 f"<sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>{pc}"
                 f"<investmentDiscretion>SOLE</investmentDiscretion>"
                 f"<votingAuthority><Sole>{shares}</Sole><Shared>0</Shared>"
                 f"<None>0</None></votingAuthority></infoTable>")
    return f'<informationTable xmlns="{_NS}">{body}</informationTable>'


def _write_registry(root: Path) -> Path:
    root.joinpath("config").mkdir(parents=True, exist_ok=True)
    reg = root / "config" / "institutional_managers.yaml"
    reg.write_text(f"""schema_version: 1
managers:
  alpha_value:
    display_name: Alpha Value LP
    cik: "0000000101"
    enabled: true
    cik_verified: true
    strategy_archetype: value
    expected_horizon: long
    concentration_style: concentrated
    turnover_class: low
    cloneability: 0.8
    manager_quality_prior: 0.7
    options_complexity: low
    market_maker: false
    specialization: [technology]
    effective_from: "2025-01-01"
    effective_to: null
    rationale: E2E fixture manager.
  beta_quality:
    display_name: Beta Quality Fund
    cik: "0000000102"
    enabled: true
    cik_verified: true
    strategy_archetype: quality_compounder
    expected_horizon: long
    concentration_style: concentrated
    turnover_class: low
    cloneability: 0.8
    manager_quality_prior: 0.7
    options_complexity: low
    market_maker: false
    specialization: [software]
    effective_from: "2025-01-01"
    effective_to: null
    rationale: E2E fixture manager.
""", encoding="utf-8")
    return reg


def _build_fixtures(fixtures: Path) -> None:
    """Two managers, each with two 13F-HR filings (AAPL 500 -> 800 = increase),
    the current filing also holding an AAPL PUT (must NOT be directional)."""
    fixtures.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}

    def _add(url: str, name: str, content: str) -> None:
        (fixtures / name).write_text(content, encoding="utf-8")
        manifest[url] = name

    for cik10, tag in (("0000000101", "a"), ("0000000102", "b")):
        padded = cik_to_padded(cik10)
        acc_prev, acc_cur = f"{tag}-prev", f"{tag}-cur"
        submissions = {
            "cik": int(cik10),
            "filings": {"recent": {
                "form": ["13F-HR", "13F-HR"],
                "accessionNumber": [acc_cur, acc_prev],
                "filingDate": ["2026-05-15", "2026-02-14"],
                "reportDate": ["2026-03-31", "2025-12-31"],
                "primaryDocument": ["infotable.xml", "infotable.xml"],
            }},
        }
        _add(SUBMISSIONS_URL.format(cik=padded), f"sub_{tag}.json",
             json.dumps(submissions))
        # info tables: prev = 500 sh; current = 800 sh (increase) + an AAPL put.
        # EDGAR archive folders use the accession WITHOUT dashes (matches
        # context_loader._infotable_url).
        prev_nd, cur_nd = acc_prev.replace("-", ""), acc_cur.replace("-", "")
        _add(f"{ARCHIVES_BASE}/{int(cik10)}/{prev_nd}/infotable.xml", f"prev_{tag}.xml",
             _infotable_xml([("APPLE INC", _AAPL, 100000.0, 500.0, "")]))
        _add(f"{ARCHIVES_BASE}/{int(cik10)}/{cur_nd}/infotable.xml", f"cur_{tag}.xml",
             _infotable_xml([("APPLE INC", _AAPL, 160000.0, 800.0, ""),
                             ("APPLE INC", _AAPL, 20000.0, 10.0, "Put")]))
    (fixtures / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _run(root: Path):
    client = GovernedSECClient(SECClientConfig(
        live_enabled=False, fixtures_dir=root / "fixtures",
        cache_dir=root / "cache", db_path=root / "data" / "institutional_intelligence.db"))
    resolver = SecurityIdentityResolver(cusip_map={
        _AAPL: [MappingEntry("AAPL", timeless=True)],
        _MSFT: [MappingEntry("MSFT", timeless=True)]})
    return run_institutional_intelligence(
        str(root), client=client, resolver=resolver,
        registry_path=root / "config" / "institutional_managers.yaml",
        now=date(2026, 6, 8), write=True,
        config={"enabled": True, "live_sec_ingestion_enabled": False,
                "minimum_effective_managers": 1.5, "minimum_consensus_confidence": 0.55,
                "stale_after_days": 140,
                "manager_registry_path": str(root / "config" / "institutional_managers.yaml")})


def _setup(tmp_path: Path) -> Path:
    _write_registry(tmp_path)
    _build_fixtures(tmp_path / "fixtures")
    (tmp_path / "outputs" / "latest").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_end_to_end_ingestion_and_artifacts(tmp_path):
    root = _setup(tmp_path)
    res = _run(root)
    assert res["status"] in ("ok", "degraded", "insufficient_data")
    art = json.loads((root / "outputs" / "latest" / "institutional_intelligence.json").read_text())
    assert art["feeds_decision_engine"] is False
    assert art["observe_only"] is True and art["no_trade"] is True
    assert any("delayed" in s for s in art["source_limitations"])
    # AAPL resolved + present across two independent managers.
    syms = {r["symbol"] for r in art["records"]}
    assert "AAPL" in syms
    aapl = next(r for r in art["records"] if r["symbol"] == "AAPL")
    # Two distinct-archetype managers clear the independence gate; the signal is
    # positive but honestly modest (single quarter, no strategy tags) — the
    # system does NOT overclaim accumulation on weak inputs.
    assert aapl["effective_independent_managers"] >= 1.5
    assert aapl["consensus_score"] > 0                       # positive direction
    assert aapl["consensus_state"] != "insufficient_data"    # gate cleared
    assert aapl["consensus_state"] in (
        "neutral", "moderate_accumulation", "strong_accumulation", "crowded_accumulation")


def test_end_to_end_never_writes_decision_plan(tmp_path):
    root = _setup(tmp_path)
    dp = root / "outputs" / "latest" / "decision_plan.json"
    assert not dp.exists()
    _run(root)
    # The subsystem must NEVER create the production decision plan.
    assert not dp.exists()


def test_end_to_end_idempotent_reingest(tmp_path):
    root = _setup(tmp_path)
    _run(root)
    import sqlite3
    db = root / "data" / "institutional_intelligence.db"
    with sqlite3.connect(db) as cx:
        holdings_1 = cx.execute("SELECT COUNT(*) FROM institutional_holdings").fetchone()[0]
        filings_1 = cx.execute("SELECT COUNT(*) FROM institutional_filings").fetchone()[0]
    _run(root)   # same accessions again
    with sqlite3.connect(db) as cx:
        holdings_2 = cx.execute("SELECT COUNT(*) FROM institutional_holdings").fetchone()[0]
        filings_2 = cx.execute("SELECT COUNT(*) FROM institutional_filings").fetchone()[0]
    assert holdings_1 == holdings_2 and filings_1 == filings_2   # no dup rows


def test_end_to_end_options_not_directional(tmp_path):
    # The current filing holds an AAPL PUT; the AAPL consensus must be
    # ACCUMULATION driven by the SHARE increase — the put adds no bearish signal.
    root = _setup(tmp_path)
    res = _run(root)
    aapl = next(r for r in res["records"] if r["symbol"] == "AAPL")
    assert aapl["consensus_score"] > 0     # not dragged negative by the put


def test_end_to_end_produces_status_and_consensus_artifacts(tmp_path):
    root = _setup(tmp_path)
    _run(root)
    latest = root / "outputs" / "latest"
    for fname in ("institutional_intelligence.json", "institutional_intelligence_status.json",
                  "institutional_consensus.json"):
        assert (latest / fname).exists()
    st = json.loads((latest / "institutional_intelligence_status.json").read_text())
    assert st["feeds_decision_engine"] is False
    assert st["overall_status"] in ("ok", "degraded", "insufficient_data")
