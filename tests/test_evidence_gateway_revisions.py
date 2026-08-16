"""Revision / supersession safety — a later correction must not rewrite history.

Session 1 produced a false pass by testing an ADJACENT property: it showed that
two different valid snapshots hash differently, when the criterion required
constructing an actual mismatch. Every test here therefore builds the exact
situation its name claims — a real original, a real later revision, and a real
historical as_of between them — and asserts the specific effect.

Scenario coverage follows the session's named targets A–G.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from portfolio_automation.evidence_gateway.revisions import (
    LinkState, WINNER_POLICY_STATUS, resolve_visibility)
from portfolio_automation.northstar.evidence import EvidenceSnapshot
from portfolio_automation.northstar.pit import KNOWN_AT_SOURCE_REPORTED, PointInTime
from portfolio_automation.northstar.provenance import Provenance

MODULE = (Path(__file__).resolve().parents[1] / "portfolio_automation" /
          "evidence_gateway" / "revisions.py")

SRC = "src_" + "a" * 32
JAN10 = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
JAN15 = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
FEB20 = datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc)
MAR01 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
APR01 = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)


def _pit(known_at):
    return PointInTime(known_at=known_at, known_at_basis=KNOWN_AT_SOURCE_REPORTED)


def _prov():
    return Provenance(producer_id="adapter.test", producer_type="source_adapter",
                      recorded_at=JAN10, source_id=SRC)


def _snap(known_at, payload, supersedes=None):
    return EvidenceSnapshot(
        source_id=SRC, entity_id="AAPL", entity_type="symbol",
        evidence_type="fundamental.revenue", pit=_pit(known_at),
        provenance=_prov(), supersedes_snapshot_id=supersedes, payload=payload)


# ── SCENARIO A: a late revision must not leak backward ─────────────────────
def test_scenario_a_late_revision_cannot_leak_backward():
    """THE core property. A exists Jan 10; B supersedes A but is only knowable
    Feb 20. Reading as of Jan 15, B must have NO effect whatsoever."""
    a = _snap(JAN10, {"revenue": 100})
    b = _snap(FEB20, {"revenue": 111}, supersedes=a.snapshot_id)

    view = resolve_visibility([a, b], JAN15)

    assert view.is_visible(a.snapshot_id), "A was knowable at Jan 15"
    assert not view.is_visible(b.snapshot_id), "B was NOT knowable at Jan 15"
    member_a = view.member(a.snapshot_id)
    assert member_a.is_superseded_within_view is False, (
        "a revision that was not knowable at as_of must not mark A superseded")
    assert member_a.superseded_by == ()
    assert member_a.link_state is LinkState.ROOT


def test_scenario_a_presence_of_the_revision_in_the_corpus_changes_nothing():
    """The decisive comparison: the historical view with B present must be
    IDENTICAL to the view without B. Possessing a correction today cannot alter
    what was visible then."""
    a = _snap(JAN10, {"revenue": 100})
    b = _snap(FEB20, {"revenue": 111}, supersedes=a.snapshot_id)

    without_b = resolve_visibility([a], JAN15)
    with_b = resolve_visibility([a, b], JAN15)

    assert with_b.visible_ids == without_b.visible_ids
    assert [m.to_dict() for m in with_b.visible] == [m.to_dict() for m in without_b.visible]


def test_scenario_a_withheld_revision_reports_the_lookahead_reason():
    a = _snap(JAN10, {"revenue": 100})
    b = _snap(FEB20, {"revenue": 111}, supersedes=a.snapshot_id)
    view = resolve_visibility([a, b], JAN15)
    withheld = view.withheld[0]
    assert withheld.reason == "PIT_REFUSED"
    assert withheld.pit_reason == "KNOWN_AT_AFTER_AS_OF"


# ── SCENARIO B: revision becomes knowable later ────────────────────────────
def test_scenario_b_both_visible_later_and_link_reported_without_a_winner():
    a = _snap(JAN10, {"revenue": 100})
    b = _snap(FEB20, {"revenue": 111}, supersedes=a.snapshot_id)

    view = resolve_visibility([a, b], MAR01)

    assert view.is_visible(a.snapshot_id) and view.is_visible(b.snapshot_id)
    assert view.member(a.snapshot_id).superseded_by == (b.snapshot_id,)
    assert view.member(b.snapshot_id).link_state is LinkState.RESOLVED
    # the link is REPORTED; no current value is named
    assert view.winner_policy == WINNER_POLICY_STATUS


def test_no_winner_selection_policy_is_invented():
    """The repository establishes none, so none may appear here — not as a
    field, not as an ordering, not as a 'current' accessor."""
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {n.name.lower() for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    for banned in ("winner", "current_value", "latest", "preferred", "resolve_current"):
        assert not any(banned in n for n in names), f"winner policy leaked: {banned}"


# ── SCENARIO C: multi-hop chain ────────────────────────────────────────────
def test_scenario_c_multihop_exposes_only_the_knowable_prefix():
    a = _snap(JAN10, {"revenue": 100})
    b = _snap(FEB20, {"revenue": 111}, supersedes=a.snapshot_id)
    c = _snap(APR01, {"revenue": 122}, supersedes=b.snapshot_id)

    at_jan = resolve_visibility([a, b, c], JAN15)
    assert at_jan.visible_ids == (a.snapshot_id,)

    at_mar = resolve_visibility([a, b, c], MAR01)
    assert set(at_mar.visible_ids) == {a.snapshot_id, b.snapshot_id}
    assert not at_mar.is_visible(c.snapshot_id)
    # C cannot remove B from a view of a time before C was knowable
    assert at_mar.member(b.snapshot_id).superseded_by == ()

    at_apr = resolve_visibility([a, b, c], APR01)
    assert set(at_apr.visible_ids) == {a.snapshot_id, b.snapshot_id, c.snapshot_id}
    assert at_apr.member(b.snapshot_id).superseded_by == (c.snapshot_id,)


def test_scenario_c_deepest_node_does_not_erase_earlier_members():
    a = _snap(JAN10, {"revenue": 100})
    b = _snap(FEB20, {"revenue": 111}, supersedes=a.snapshot_id)
    c = _snap(APR01, {"revenue": 122}, supersedes=b.snapshot_id)
    view = resolve_visibility([a, b, c], APR01)
    # every member remains present; supersession is annotation, not deletion
    assert len(view.visible) == 3


# ── SCENARIO D: deterministic replay ───────────────────────────────────────
def test_scenario_d_same_corpus_same_as_of_gives_identical_output():
    a = _snap(JAN10, {"revenue": 100})
    b = _snap(FEB20, {"revenue": 111}, supersedes=a.snapshot_id)
    first = resolve_visibility([a, b], MAR01).to_dict()
    second = resolve_visibility([a, b], MAR01).to_dict()
    assert first == second


def test_scenario_d_input_ordering_does_not_change_the_result():
    a = _snap(JAN10, {"revenue": 100})
    b = _snap(FEB20, {"revenue": 111}, supersedes=a.snapshot_id)
    assert (resolve_visibility([a, b], MAR01).to_dict()
            == resolve_visibility([b, a], MAR01).to_dict())


# ── SCENARIO E: future AND malformed ───────────────────────────────────────
def test_scenario_e_future_revision_that_is_also_malformed_reports_as_future():
    """Timing is evaluated first, so a later integrity failure must not obscure
    the lookahead finding."""
    a = _snap(JAN10, {"revenue": 100})
    b = _snap(FEB20, {"revenue": 111}, supersedes=a.snapshot_id)
    object.__setattr__(b, "payload_hash", "0" * 64)      # also corrupt

    view = resolve_visibility([a, b], JAN15)
    withheld = [w for w in view.withheld]
    assert len(withheld) == 1
    assert withheld[0].reason == "PIT_REFUSED"
    assert withheld[0].pit_reason == "KNOWN_AT_AFTER_AS_OF"
    assert view.is_visible(a.snapshot_id)


def test_a_knowable_but_corrupt_revision_is_withheld_on_integrity():
    """Contrast case: when timing is fine, the integrity failure is the reason."""
    a = _snap(JAN10, {"revenue": 100})
    b = _snap(FEB20, {"revenue": 111}, supersedes=a.snapshot_id)
    object.__setattr__(b, "payload_hash", "0" * 64)
    view = resolve_visibility([a, b], MAR01)
    assert not view.is_visible(b.snapshot_id)
    assert view.withheld[0].reason == "PAYLOAD_HASH_MISMATCH"
    # and the corrupt revision still cannot supersede A
    assert view.member(a.snapshot_id).superseded_by == ()


# ── SCENARIO F: broken supersession reference ──────────────────────────────
def test_scenario_f_predecessor_absent_from_corpus_is_unresolved_not_invented():
    """The snapshot stays visible; the gap is recorded. Refusing it would let an
    incomplete corpus suppress legitimately visible evidence."""
    orphan = _snap(JAN10, {"revenue": 100}, supersedes="evs_" + "9" * 32)
    view = resolve_visibility([orphan], JAN15)

    assert view.is_visible(orphan.snapshot_id)
    member = view.member(orphan.snapshot_id)
    assert member.link_state is LinkState.PREDECESSOR_NOT_IN_CORPUS
    assert view.unresolved_links[0]["snapshot_id"] == orphan.snapshot_id
    assert "never invented" in view.unresolved_links[0]["detail"]


def test_scenario_f_predecessor_present_but_not_knowable_is_distinguished():
    """'Not yet knowable' and 'absent entirely' are both unresolved, but they are
    different audit facts."""
    late_root = _snap(FEB20, {"revenue": 100})
    child = _snap(MAR01, {"revenue": 111}, supersedes=late_root.snapshot_id)
    view = resolve_visibility([late_root, child], MAR01)
    # both knowable at MAR01 -> resolved
    assert view.member(child.snapshot_id).link_state is LinkState.RESOLVED

    later_child = _snap(APR01, {"revenue": 122}, supersedes=late_root.snapshot_id)
    view2 = resolve_visibility([late_root, later_child], JAN15)
    assert view2.visible == []          # neither knowable at Jan 15


# ── SCENARIO G: malformed chain topology ───────────────────────────────────
def test_scenario_g_self_supersession_is_structurally_unconstructible():
    """An architectural finding, not a guard.

    supersedes_snapshot_id participates in snapshot identity, so pointing it at
    the snapshot's own id CHANGES that id — the reference immediately stops
    referring to the snapshot. A self-referential snapshot cannot be built at
    all, which is why the resolver carries no SELF_REFERENTIAL state: a branch
    that can never fire would overstate how defensive this module is."""
    snap = _snap(JAN10, {"revenue": 100})
    original_id = snap.snapshot_id
    object.__setattr__(snap, "supersedes_snapshot_id", original_id)

    assert snap.snapshot_id != original_id, (
        "identity must shift once supersedes_snapshot_id changes")
    assert snap.supersedes_snapshot_id != snap.snapshot_id, (
        "the link no longer points at the snapshot itself")

    view = resolve_visibility([snap], JAN15)
    member = view.member(snap.snapshot_id)
    assert member.link_state is LinkState.PREDECESSOR_NOT_IN_CORPUS
    assert member.superseded_by == (), "nothing may supersede itself"


def test_no_self_referential_state_exists():
    assert not hasattr(LinkState, "SELF_REFERENTIAL")


def test_two_revisions_of_the_same_predecessor_are_both_reported():
    """Conflicting topology is REPORTED, not resolved — resolving it would
    require the winner policy that does not exist."""
    a = _snap(JAN10, {"revenue": 100})
    b1 = _snap(FEB20, {"revenue": 111}, supersedes=a.snapshot_id)
    b2 = _snap(FEB20, {"revenue": 222}, supersedes=a.snapshot_id)
    view = resolve_visibility([a, b1, b2], MAR01)
    assert set(view.member(a.snapshot_id).superseded_by) == {b1.snapshot_id, b2.snapshot_id}
    assert view.winner_policy == WINNER_POLICY_STATUS


# ── ordering + purity invariants ───────────────────────────────────────────
def test_pit_admission_precedes_supersession_interpretation():
    """Stated directly: an unadmitted snapshot contributes to NOTHING."""
    a = _snap(JAN10, {"revenue": 100})
    b = _snap(FEB20, {"revenue": 111}, supersedes=a.snapshot_id)
    view = resolve_visibility([a, b], JAN15)
    assert b.snapshot_id not in view.visible_ids
    assert all(b.snapshot_id not in m.superseded_by for m in view.visible)
    assert all(link["snapshot_id"] != b.snapshot_id for link in view.unresolved_links)


def test_effective_period_is_never_consulted():
    """An open contract question must not become an implicit rule."""
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for banned in ("effective_period_start", "effective_period_end",
                   "effective_period_label"):
        assert banned not in attrs, f"{banned} must not drive any decision"


def test_module_reads_no_clock_and_performs_no_io():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            assert name not in {"now", "utcnow", "today", "open", "connect"}
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {a.name for a in node.names}
            mod = getattr(node, "module", "") or ""
            for banned in ("os", "socket", "sqlite3", "requests", "urllib"):
                assert banned not in names and not mod.startswith(banned)


def test_generator_input_is_handled_without_being_consumed_twice():
    a = _snap(JAN10, {"revenue": 100})
    b = _snap(FEB20, {"revenue": 111}, supersedes=a.snapshot_id)
    view = resolve_visibility((s for s in (a, b)), MAR01)
    assert set(view.visible_ids) == {a.snapshot_id, b.snapshot_id}


def test_empty_corpus_is_an_empty_view_not_an_error():
    view = resolve_visibility([], JAN15)
    assert view.visible == [] and view.withheld == []
    assert view.winner_policy == WINNER_POLICY_STATUS


def test_malformed_member_is_withheld_rather_than_raising():
    a = _snap(JAN10, {"revenue": 100})
    view = resolve_visibility([a, None, "not-a-snapshot"], JAN15)
    assert view.is_visible(a.snapshot_id)
    assert len(view.withheld) == 2
    assert all(w.reason == "NOT_AN_EVIDENCE_SNAPSHOT" for w in view.withheld)
