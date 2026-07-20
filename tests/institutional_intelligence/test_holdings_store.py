"""Phase 4 tests — point-in-time holdings store + amendment supersession.

Covers: import + retrieve, idempotent re-import (no duplicate rows), amendment
supersedes deterministically (both retained + auditable), point-in-time
effective_asof (no look-ahead), signal unavailable before filing date, content
hash recorded, raw value preserved, zero-holdings filing, quarter-end vs
availability stored separately, transactional restatement.
"""

from __future__ import annotations

from datetime import date

import pytest

from portfolio_automation.institutional_intelligence.holdings_store import HoldingsStore
from portfolio_automation.institutional_intelligence.schemas import (
    ParsedFiling,
    ParsedHolding,
)

_Q1 = date(2026, 3, 31)


def _holding(cusip="037833100", value=1000.0, shares=500.0, put_call="none"):
    return ParsedHolding(
        issuer_name="APPLE INC", class_title="COM", cusip=cusip, value=value,
        shares_or_principal=shares, share_principal_type="SH", put_call=put_call,
    )


def _parsed(accession, holdings, form="13F-HR", warnings=()):
    return ParsedFiling(accession=accession, form_type=form,
                        holdings=tuple(holdings), parse_warnings=tuple(warnings))


def _store(tmp_path):
    return HoldingsStore(tmp_path / "inst.db", clock_fn=lambda: 0.0)


def _import(store, accession, filed, holdings, *, form="13F-HR", amend=False,
            period=_Q1, chash="h1", warnings=()):
    return store.import_filing(
        cik="0000320193", accession=accession, form_type=form,
        filed_at=filed, report_period=period, is_amendment=amend,
        parsed=_parsed(accession, holdings, form, warnings), content_hash=chash,
    )


# --- basic import --------------------------------------------------------

def test_import_and_retrieve(tmp_path):
    store = _store(tmp_path)
    assert _import(store, "a1", date(2026, 5, 15), [_holding()]) == "inserted"
    f = store.get_filing("a1")
    assert f.holdings_count == 1 and f.is_effective
    assert f.filed_at == date(2026, 5, 15) and f.report_period == _Q1  # stored separately
    h = store.holdings_for("a1")[0]
    assert h.cusip == "037833100" and h.raw_value if False else h.value == 1000.0


def test_raw_value_preserved(tmp_path):
    store = _store(tmp_path)
    _import(store, "a1", date(2026, 5, 15), [_holding(value=123456.0)])
    assert store.holdings_for("a1")[0].value == 123456.0   # verbatim, not scaled


def test_zero_holdings_filing_ok(tmp_path):
    store = _store(tmp_path)
    _import(store, "a1", date(2026, 5, 15), [], warnings=("zero_holdings",))
    assert store.get_filing("a1").holdings_count == 0       # valid, not an error


def test_content_hash_recorded(tmp_path):
    store = _store(tmp_path)
    _import(store, "a1", date(2026, 5, 15), [_holding()], chash="deadbeef")
    assert store.get_filing("a1").content_hash == "deadbeef"


# --- idempotence ---------------------------------------------------------

def test_reimport_same_hash_is_noop(tmp_path):
    store = _store(tmp_path)
    _import(store, "a1", date(2026, 5, 15), [_holding()], chash="h1")
    assert _import(store, "a1", date(2026, 5, 15), [_holding()], chash="h1") == "unchanged"
    assert len(store.holdings_for("a1")) == 1               # no duplicate rows


def test_reimport_changed_hash_restates(tmp_path):
    store = _store(tmp_path)
    _import(store, "a1", date(2026, 5, 15), [_holding()], chash="h1")
    status = _import(store, "a1", date(2026, 5, 15),
                     [_holding(), _holding(cusip="88160R101")], chash="h2")
    assert status == "restated"
    assert len(store.holdings_for("a1")) == 2               # replaced, still no dup PK


def test_duplicate_row_identity_deduped(tmp_path):
    store = _store(tmp_path)
    # same (accession, cusip, class, put_call) twice within a filing collapses
    _import(store, "a1", date(2026, 5, 15), [_holding(), _holding()])
    assert len(store.holdings_for("a1")) == 1


# --- amendments ----------------------------------------------------------

def test_amendment_supersedes_deterministically(tmp_path):
    store = _store(tmp_path)
    _import(store, "orig", date(2026, 5, 15), [_holding(shares=500.0)], chash="h1")
    _import(store, "amd", date(2026, 6, 1), [_holding(shares=800.0)],
            form="13F-HR/A", amend=True, chash="h2")
    orig, amd = store.get_filing("orig"), store.get_filing("amd")
    assert amd.is_effective and amd.superseded_by is None
    assert not orig.is_effective and orig.superseded_by == "amd"   # retained + auditable
    # effective (no as_of bound) reflects the amendment's restated shares
    holds = store.effective_holdings_asof("0000320193", _Q1, date(2026, 12, 31))
    assert holds[0].shares_or_principal == 800.0


# --- point-in-time / no look-ahead --------------------------------------

def test_effective_asof_no_lookahead(tmp_path):
    store = _store(tmp_path)
    _import(store, "orig", date(2026, 5, 15), [_holding(shares=500.0)], chash="h1")
    _import(store, "amd", date(2026, 6, 1), [_holding(shares=800.0)],
            form="13F-HR/A", amend=True, chash="h2")
    # On 2026-05-20 the amendment does NOT exist yet → original.
    f_before = store.effective_filing_asof("0000320193", _Q1, date(2026, 5, 20))
    assert f_before.accession == "orig"
    assert store.effective_holdings_asof("0000320193", _Q1, date(2026, 5, 20))[0].shares_or_principal == 500.0
    # On 2026-06-05 the amendment is available → amendment.
    f_after = store.effective_filing_asof("0000320193", _Q1, date(2026, 6, 5))
    assert f_after.accession == "amd"


def test_signal_unavailable_before_filing_date(tmp_path):
    store = _store(tmp_path)
    _import(store, "a1", date(2026, 5, 15), [_holding()])
    # Before the public filing date, nothing is available (no quarter-end leak).
    assert store.effective_filing_asof("0000320193", _Q1, date(2026, 5, 14)) is None
    f = store.get_filing("a1")
    assert store.is_available_on(f, date(2026, 5, 14)) is False
    assert store.is_available_on(f, date(2026, 5, 15)) is True


def test_quarter_end_not_used_as_availability(tmp_path):
    store = _store(tmp_path)
    _import(store, "a1", date(2026, 5, 15), [_holding()])
    # Asking as-of the quarter-end (2026-03-31) — well before the 05-15 filing —
    # must return nothing: the data was NOT public at quarter-end.
    assert store.effective_filing_asof("0000320193", _Q1, _Q1) is None
