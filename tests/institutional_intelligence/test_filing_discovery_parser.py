"""Phase 3 tests — filing discovery + defensive information-table parsing.

Discovery: 13F form filtering, notice-vs-holdings, amendments, point-in-time
filing date, ragged/missing columns, disabled client. Parser: ordinary shares,
put, call, FIGI present/absent, amendments, 13F-NT, malformed XML, missing
optional fields, multiple other managers, duplicate rows, zero holdings,
value/share units, and XXE hardening. All offline.
"""

from __future__ import annotations

from datetime import date

from portfolio_automation.institutional_intelligence import filing_discovery as fd
from portfolio_automation.institutional_intelligence import filing_parser as fp
from portfolio_automation.institutional_intelligence.schemas import (
    PUT_CALL_CALL,
    PUT_CALL_NONE,
    PUT_CALL_PUT,
)


# --- discovery -----------------------------------------------------------

def _submissions(forms, accns, filed, reports=None, docs=None):
    recent = {"form": forms, "accessionNumber": accns, "filingDate": filed}
    if reports is not None:
        recent["reportDate"] = reports
    if docs is not None:
        recent["primaryDocument"] = docs
    return {"cik": 320193, "filings": {"recent": recent}}


def test_discovery_filters_and_classifies():
    data = _submissions(
        forms=["13F-HR", "10-K", "13F-NT", "13F-HR/A", "8-K"],
        accns=["a1", "x", "a2", "a3", "y"],
        filed=["2026-05-15", "2026-01-01", "2026-05-15", "2026-06-01", "2026-02-01"],
        reports=["2026-03-31", "", "2026-03-31", "2026-03-31", ""],
    )
    refs = fd.parse_submissions(data)
    assert [r.form_type for r in refs] == ["13F-HR", "13F-NT", "13F-HR/A"]
    hr, nt, amd = refs
    assert hr.is_holdings and not hr.is_notice
    assert nt.is_notice and not nt.is_holdings           # notice != holdings
    assert amd.is_amendment and amd.is_holdings
    assert hr.filed_at == date(2026, 5, 15)               # point-in-time = filingDate
    assert hr.report_period == date(2026, 3, 31)          # quarter-end kept separate
    assert hr.cik == "0000320193"


def test_discovery_skips_rows_without_filing_date():
    data = _submissions(forms=["13F-HR"], accns=["a1"], filed=[""])
    assert fd.parse_submissions(data) == []               # no PIT anchor → skip


def test_discovery_ragged_columns_tolerated():
    data = _submissions(forms=["13F-HR", "13F-HR"], accns=["a1"], filed=["2026-05-15"])
    refs = fd.parse_submissions(data)
    assert len(refs) == 1                                  # min() over columns


def test_latest_holdings_excludes_notice():
    data = _submissions(
        forms=["13F-HR", "13F-NT"], accns=["a1", "a2"],
        filed=["2026-05-15", "2026-08-15"],
    )
    refs = fd.parse_submissions(data)
    latest = fd.latest_holdings_filing(refs)
    assert latest.accession == "a1"                        # the notice is ignored


def test_discovery_disabled_client_returns_empty(tmp_path):
    from portfolio_automation.institutional_intelligence.sec_client import (
        GovernedSECClient, SECClientConfig,
    )
    (tmp_path / "fx").mkdir()
    client = GovernedSECClient(
        SECClientConfig(live_enabled=False, cache_dir=tmp_path / "c",
                        db_path=tmp_path / "d.db", fixtures_dir=tmp_path / "fx"),
        user_agent="ua", urlopen=lambda *a, **k: (_ for _ in ()).throw(AssertionError()),
    )
    assert fd.discover_filings(client, "320193") == []


# --- parser --------------------------------------------------------------

_NS = "http://www.sec.gov/edgar/document/thirteenf/informationtable"


def _info_table(rows_xml: str, *, ns: bool = True) -> str:
    ns_attr = f' xmlns="{_NS}"' if ns else ""
    return f'<informationTable{ns_attr}>{rows_xml}</informationTable>'


def _row(issuer="APPLE INC", cusip="037833100", value="1000", shares="500",
         stype="SH", put_call=None, figi=None, others=None):
    pc = f"<putCall>{put_call}</putCall>" if put_call else ""
    fg = f"<figi>{figi}</figi>" if figi else ""
    om = "".join(f"<otherManager>{o}</otherManager>" for o in (others or []))
    return (
        f"<infoTable><nameOfIssuer>{issuer}</nameOfIssuer>"
        f"<titleOfClass>COM</titleOfClass><cusip>{cusip}</cusip>{fg}"
        f"<value>{value}</value>"
        f"<shrsOrPrnAmt><sshPrnamt>{shares}</sshPrnamt>"
        f"<sshPrnamtType>{stype}</sshPrnamtType></shrsOrPrnAmt>{pc}"
        f"<investmentDiscretion>SOLE</investmentDiscretion>{om}"
        f"<votingAuthority><Sole>{shares}</Sole><Shared>0</Shared><None>0</None>"
        f"</votingAuthority></infoTable>"
    )


def test_parse_ordinary_shares():
    xml = _info_table(_row())
    pf = fp.parse_information_table(xml, accession="a1", form_type="13F-HR")
    assert pf.holdings_count == 1
    h = pf.holdings[0]
    assert h.issuer_name == "APPLE INC" and h.cusip == "037833100"
    assert h.value == 1000.0 and h.shares_or_principal == 500.0
    assert h.share_principal_type == "SH" and h.put_call == PUT_CALL_NONE
    assert h.voting_sole == 500.0
    assert "value_units_ambiguous" in pf.parse_warnings   # units flagged, never assumed


def test_parse_put_and_call_kept_raw():
    put = fp.parse_information_table(_info_table(_row(put_call="Put")),
                                    accession="a", form_type="13F-HR").holdings[0]
    call = fp.parse_information_table(_info_table(_row(put_call="Call")),
                                     accession="a", form_type="13F-HR").holdings[0]
    assert put.put_call == PUT_CALL_PUT
    assert call.put_call == PUT_CALL_CALL   # raw marker only; NOT interpreted here


def test_parse_figi_present_and_absent():
    with_figi = fp.parse_information_table(_info_table(_row(figi="BBG000B9XRY4")),
                                          accession="a", form_type="13F-HR").holdings[0]
    without = fp.parse_information_table(_info_table(_row()),
                                        accession="a", form_type="13F-HR").holdings[0]
    assert with_figi.figi == "BBG000B9XRY4"
    assert without.figi is None


def test_parse_without_namespace():
    pf = fp.parse_information_table(_info_table(_row(), ns=False),
                                    accession="a", form_type="13F-HR")
    assert pf.holdings_count == 1   # namespace-agnostic


def test_parse_multiple_other_managers():
    h = fp.parse_information_table(
        _info_table(_row(others=["01", "02", "03"])),
        accession="a", form_type="13F-HR").holdings[0]
    assert h.other_managers == ("01", "02", "03")


def test_parse_duplicate_rows_both_kept():
    xml = _info_table(_row() + _row())   # dedup is the store's job, not parse's
    pf = fp.parse_information_table(xml, accession="a", form_type="13F-HR")
    assert pf.holdings_count == 2


def test_parse_missing_optional_fields():
    xml = _info_table("<infoTable><nameOfIssuer>X CO</nameOfIssuer>"
                      "<titleOfClass>COM</titleOfClass><cusip>123456789</cusip>"
                      "<value>50</value></infoTable>")
    h = fp.parse_information_table(xml, accession="a", form_type="13F-HR").holdings[0]
    assert h.shares_or_principal is None and h.share_principal_type is None
    assert h.put_call == PUT_CALL_NONE and h.figi is None


def test_parse_unusable_row_skipped():
    # missing cusip → skipped, warned, not crashed
    xml = _info_table("<infoTable><nameOfIssuer>NO CUSIP CO</nameOfIssuer>"
                      "<value>1</value></infoTable>")
    pf = fp.parse_information_table(xml, accession="a", form_type="13F-HR")
    assert pf.holdings_count == 0
    assert any(w.startswith("skipped_unusable_rows") for w in pf.parse_warnings)


def test_parse_zero_holdings():
    pf = fp.parse_information_table(_info_table(""), accession="a", form_type="13F-HR")
    assert pf.holdings_count == 0 and "zero_holdings" in pf.parse_warnings


def test_parse_notice_has_no_information_table():
    pf = fp.parse_information_table("", accession="a", form_type="13F-NT", is_notice=True)
    assert pf.is_notice and pf.holdings_count == 0
    assert "notice_no_information_table" in pf.parse_warnings


def test_parse_malformed_xml_degrades():
    pf = fp.parse_information_table("<not valid xml <<", accession="a", form_type="13F-HR")
    assert pf.holdings_count == 0 and "malformed_xml" in pf.parse_warnings


def test_parse_principal_amount_units():
    xml = _info_table(_row(stype="PRN", shares="100000"))
    h = fp.parse_information_table(xml, accession="a", form_type="13F-HR").holdings[0]
    assert h.share_principal_type == "PRN" and h.shares_or_principal == 100000.0


def test_parse_rejects_xxe_billion_laughs():
    # defusedxml must refuse entity-expansion / external entities → malformed_xml,
    # never expand. (Proves the security hardening is active.)
    xxe = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">]>'
        f'<informationTable xmlns="{_NS}"><infoTable>'
        '<nameOfIssuer>&lol2;</nameOfIssuer><cusip>037833100</cusip>'
        '<value>1</value></infoTable></informationTable>'
    )
    pf = fp.parse_information_table(xxe, accession="a", form_type="13F-HR")
    assert "malformed_xml" in pf.parse_warnings   # entity expansion refused
    assert pf.holdings_count == 0
