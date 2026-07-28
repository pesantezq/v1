"""Durable-overlay hardening (final-review fix wave, 2026-07-28).

Covers the defects the whole-branch review found in the first durability pass:

  Fix 1 — durability is a property of the PROPOSAL TYPE, not the workflow, and the
          dedup identity falls back to the FACT (proposal_type, symbol), never to
          the clock-salted proposal_id.
  Fix 2 — durable ops SUPERSEDE rather than accumulate: for one asserted fact the
          most recent human decision wins regardless of list order, and today's op
          always beats a carried one.
  Fix 4 — a revocation applies to TODAY's pending proposals too, not only to the
          carry-forward path.
  Fix 5 — an unreadable approvals log must not silently reverse production.
  Fix 7 — a `rolled_back` audit event is honoured by the carry-forward.
"""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.sim_governance import production_application as PAP
from portfolio_automation.sim_governance import production_overlays as PO
from portfolio_automation.sim_governance import promotion_approvals as PA
from portfolio_automation.sim_governance import schemas as S

_NOW = "2026-07-28T17:00:00+00:00"
_OLD = "2026-07-01T09:00:00+00:00"


def _outputs(tmp_path: Path) -> str:
    d = tmp_path / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _audit(base_dir: str, rows: list[dict]) -> None:
    d = Path(base_dir) / "promotion_approvals"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "production_application_audit.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _applied_row(pid, cid, sym, ptype, *, ts=_OLD, **change) -> dict:
    row = {"ts": ts, "event": "applied_to_production", "proposal_id": pid,
           "proposal_type": ptype, "change": {"symbol": sym, **change},
           "rollback_plan": "revoke it", "snapshots": {}}
    if cid is not None:
        row["candidate_id"] = cid
    return row


def _proposal(pid, cid, sym, ptype, **change) -> dict:
    return {"proposal_id": pid, "candidate_id": cid, "proposal_type": ptype,
            "proposed_production_change": {"symbol": sym, **change},
            "rollback_plan": "revoke it"}


def _overlay(base_dir: str) -> dict:
    return json.loads((Path(base_dir) / "latest" /
                       "approved_watchlist_proposals.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Fix 1 — durable = membership decision, by proposal TYPE
# ---------------------------------------------------------------------------

def test_flock_watchlist_logic_is_not_durable(tmp_path):
    """A state-derived label must refresh even though it is a watchlist workflow.

    flock_watchlist_candidate_logic is salted by flock STATE, so persisting it
    would keep a stale label alive — exactly the hazard advisory refresh exists
    for. All 17 real pre-branch audit rows are of this shape.
    """
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_old", "cand_flock", "RIOT",
                               S.PROPOSAL_FLOCK_WATCHLIST_LOGIC)])
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_ids={"prop_old"},
        approved_candidate_ids={"cand_flock"}, write_files=False)
    assert res["watchlist_applied"] == 0


def test_membership_types_are_durable(tmp_path):
    for ptype in (S.PROPOSAL_WATCHLIST_ADD, S.PROPOSAL_WATCHLIST_REMOVE,
                  S.PROPOSAL_WATCHLIST_RANK, S.PROPOSAL_WATCHLIST_TAG):
        assert PAP.is_durable_proposal_type(ptype), ptype
    for ptype in (S.PROPOSAL_FLOCK_WATCHLIST_LOGIC, S.PROPOSAL_ADVISORY_CONTEXT,
                  S.PROPOSAL_FLOCK_ADVISORY_CONTEXT, S.PROPOSAL_CROWD_CONTEXT):
        assert not PAP.is_durable_proposal_type(ptype), ptype


def test_dedup_falls_back_to_the_fact_not_the_clock_salted_id(tmp_path):
    """Two applications of the SAME fact on different days collapse to one op.

    Legacy audit rows carry no candidate_id. Keying them on proposal_id would
    duplicate the op once per run, because make_proposal_id is clock-salted.
    """
    base = _outputs(tmp_path)
    _audit(base, [
        _applied_row("prop_day1", None, "RIOT", S.PROPOSAL_WATCHLIST_REMOVE, ts=_OLD),
        _applied_row("prop_day2", None, "RIOT", S.PROPOSAL_WATCHLIST_REMOVE,
                     ts="2026-07-02T09:00:00+00:00"),
    ])
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[],
        approved_ids={"prop_day1", "prop_day2"}, write_files=False)
    assert res["watchlist_applied"] == 1


# ---------------------------------------------------------------------------
# Fix 2 — supersede, don't accumulate
# ---------------------------------------------------------------------------

def test_todays_op_supersedes_a_carried_op_for_the_same_fact(tmp_path):
    """An approved rank 5 must not be silently overridden by a carried rank 12."""
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_old", "cand_old", "RIOT",
                               S.PROPOSAL_WATCHLIST_RANK, rank=12)])
    today = _proposal("prop_new", "cand_new", "RIOT", S.PROPOSAL_WATCHLIST_RANK, rank=5)

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[today],
        approved_ids={"prop_old", "prop_new"},
        approved_candidate_ids={"cand_old", "cand_new"}, write_files=True)

    assert res["watchlist_applied"] == 1
    folded = PO.apply_approved_watchlist(["RIOT"], _overlay(base))
    assert folded["ranks"]["RIOT"] == 5, "the fresh human approval must win"


def test_approved_removal_takes_effect_on_the_same_run_as_a_carried_add(tmp_path):
    """Day 1 of a removal must remove — a carried add cannot re-add the symbol."""
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_add", "cand_add", "XOM", S.PROPOSAL_WATCHLIST_ADD)])
    today = _proposal("prop_rm", "cand_rm", "XOM", S.PROPOSAL_WATCHLIST_REMOVE)

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[today],
        approved_ids={"prop_add", "prop_rm"},
        approved_candidate_ids={"cand_add", "cand_rm"}, write_files=True)

    folded = PO.apply_approved_watchlist(["XOM", "CVX"], _overlay(base))
    assert "XOM" not in folded["watchlist"]
    assert "CVX" in folded["watchlist"]
    assert res["watchlist_applied"] == 1, "the superseded add must be dropped, not kept"


def test_removal_stays_effective_once_it_is_itself_carried(tmp_path):
    """Determinism across runs: the effective result must not flip on day 2."""
    base = _outputs(tmp_path)
    _audit(base, [
        _applied_row("prop_add", "cand_add", "XOM", S.PROPOSAL_WATCHLIST_ADD, ts=_OLD),
        _applied_row("prop_rm", "cand_rm", "XOM", S.PROPOSAL_WATCHLIST_REMOVE,
                     ts="2026-07-27T09:00:00+00:00"),
    ])
    PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[],          # nothing proposed today
        approved_ids={"prop_add", "prop_rm"},
        approved_candidate_ids={"cand_add", "cand_rm"}, write_files=True)

    folded = PO.apply_approved_watchlist(["XOM"], _overlay(base))
    assert "XOM" not in folded["watchlist"]


def test_ops_are_emitted_oldest_first_so_the_fold_agrees_with_recency(tmp_path):
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_old", "cand_old", "RIOT",
                               S.PROPOSAL_WATCHLIST_TAG, tags=["stale"])])
    today = _proposal("prop_new", "cand_new", "AAPL", S.PROPOSAL_WATCHLIST_TAG,
                      tags=["fresh"])
    PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[today],
        approved_ids={"prop_old", "prop_new"},
        approved_candidate_ids={"cand_old", "cand_new"}, write_files=True)

    ops = _overlay(base)["ops"]
    assert [o["change"]["symbol"] for o in ops] == ["RIOT", "AAPL"]


# ---------------------------------------------------------------------------
# Fix 4 — revocation applies to today's proposals too
# ---------------------------------------------------------------------------

def test_revoked_target_still_pending_today_is_not_applied(tmp_path):
    base = _outputs(tmp_path)
    assert PA.revoke_application("cand_riot", "pesantez", _NOW, base_dir=base)["ok"]
    today = _proposal("prop_today", "cand_riot", "RIOT", S.PROPOSAL_WATCHLIST_REMOVE)

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[today],
        approved_ids={"prop_today"}, approved_candidate_ids={"cand_riot"},
        write_files=False)

    assert res["applied_count"] == 0
    assert res["watchlist_applied"] == 0
    assert [i["reason"] for i in res["ignored"]] == ["revoked"]


def test_revoked_proposal_id_still_pending_today_is_not_applied(tmp_path):
    base = _outputs(tmp_path)
    assert PA.revoke_application("prop_today", "pesantez", _NOW, base_dir=base)["ok"]
    today = _proposal("prop_today", "cand_riot", "RIOT", S.PROPOSAL_WATCHLIST_REMOVE)

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[today], approved_ids={"prop_today"},
        write_files=False)
    assert res["applied_count"] == 0


# ---------------------------------------------------------------------------
# Fix 5 — an unreadable approvals log must not reverse production
# ---------------------------------------------------------------------------

def _corrupt_approvals(base_dir: str) -> None:
    d = Path(base_dir) / "promotion_approvals"
    d.mkdir(parents=True, exist_ok=True)
    (d / "approved_proposals.json").write_text('{"approvals": [{"proposal_id"',
                                               encoding="utf-8")


def test_absent_approvals_log_is_not_treated_as_unreadable(tmp_path):
    assert PA.approvals_log_unreadable(_outputs(tmp_path)) is None


def test_corrupt_approvals_log_is_detected(tmp_path):
    base = _outputs(tmp_path)
    _corrupt_approvals(base)
    reason = PA.approvals_log_unreadable(base)
    assert reason and reason.startswith("unparseable_json")


def test_corrupt_approvals_log_refuses_to_rebuild_the_overlay(tmp_path):
    base = _outputs(tmp_path)
    # An established durable op is live in production.
    latest = Path(base) / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    live = {"generated_at": _OLD, "schema": "approved_watchlist_proposals.v1",
            "feeds_production": True, "applied_proposal_ids": ["prop_old"],
            "ops": [{"proposal_id": "prop_old", "candidate_id": "cand_old",
                     "proposal_type": S.PROPOSAL_WATCHLIST_REMOVE,
                     "change": {"symbol": "RIOT"}, "rollback_plan": "",
                     "applied_from": "human_approved_promotion_proposal"}]}
    (latest / "approved_watchlist_proposals.json").write_text(json.dumps(live),
                                                              encoding="utf-8")
    _corrupt_approvals(base)

    res = PAP.apply_approved_proposals(_NOW, base_dir=base, write_files=True)

    assert res["overlay_rebuild_skipped"] is True
    assert res["approvals_log_unreadable"]
    assert res["watchlist_applied"] == 1, "must report what is still live, not zero"
    assert _overlay(base) == live, "the existing overlay must be left untouched"
    # and the condition is persisted for the health check to pick up
    state = json.loads((Path(base) / "promotion_approvals" /
                        "production_application_state.json").read_text(encoding="utf-8"))
    assert state["overlay_rebuild_skipped"] is True


def test_non_dict_approvals_log_is_unreadable(tmp_path):
    base = _outputs(tmp_path)
    d = Path(base) / "promotion_approvals"
    d.mkdir(parents=True, exist_ok=True)
    (d / "approved_proposals.json").write_text("[]", encoding="utf-8")
    assert PA.approvals_log_unreadable(base) == "unexpected_top_level_type: list"


# ---------------------------------------------------------------------------
# Fix 7 — a rollback must not be undone by the next run
# ---------------------------------------------------------------------------

def test_rolled_back_op_is_not_carried_forward(tmp_path):
    base = _outputs(tmp_path)
    _audit(base, [
        _applied_row("prop_old", "cand_riot", "RIOT", S.PROPOSAL_WATCHLIST_REMOVE),
        {"ts": "2026-07-27T10:00:00+00:00", "event": "rolled_back",
         "overlay": PAP.WATCHLIST_OVERLAY, "restored_from": "snap",
         "rolled_back_ids": ["cand_riot"]},
    ])
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_ids={"prop_old"},
        approved_candidate_ids={"cand_riot"}, write_files=False)
    assert res["watchlist_applied"] == 0


def test_reapplication_after_a_rollback_survives(tmp_path):
    """A rollback only reverts what came before it."""
    base = _outputs(tmp_path)
    _audit(base, [
        _applied_row("prop_old", "cand_riot", "RIOT", S.PROPOSAL_WATCHLIST_REMOVE),
        {"ts": "2026-07-27T10:00:00+00:00", "event": "rolled_back",
         "overlay": PAP.WATCHLIST_OVERLAY, "restored_from": "snap",
         "rolled_back_ids": ["cand_riot"]},
        _applied_row("prop_new", "cand_riot", "RIOT", S.PROPOSAL_WATCHLIST_REMOVE,
                     ts="2026-07-27T11:00:00+00:00"),
    ])
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_candidate_ids={"cand_riot"},
        write_files=False)
    assert res["watchlist_applied"] == 1


def test_advisory_rollback_does_not_drop_durable_watchlist_ops(tmp_path):
    base = _outputs(tmp_path)
    _audit(base, [
        _applied_row("prop_old", "cand_riot", "RIOT", S.PROPOSAL_WATCHLIST_REMOVE),
        {"ts": "2026-07-27T10:00:00+00:00", "event": "rolled_back",
         "overlay": PAP.ADVISORY_OVERLAY, "restored_from": "snap",
         "rolled_back_ids": ["cand_riot"]},
    ])
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_candidate_ids={"cand_riot"},
        write_files=False)
    assert res["watchlist_applied"] == 1


def test_legacy_rollback_row_without_ids_drops_prior_ops(tmp_path):
    """No-resurrection direction: an unspecified rollback reverts what preceded it."""
    base = _outputs(tmp_path)
    _audit(base, [
        _applied_row("prop_old", "cand_riot", "RIOT", S.PROPOSAL_WATCHLIST_REMOVE),
        {"ts": "2026-07-27T10:00:00+00:00", "event": "rolled_back",
         "overlay": PAP.WATCHLIST_OVERLAY, "restored_from": "snap"},
    ])
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_candidate_ids={"cand_riot"},
        write_files=False)
    assert res["watchlist_applied"] == 0


def test_rollback_last_records_which_ops_it_reverted(tmp_path):
    base = _outputs(tmp_path)
    today = _proposal("prop_rm", "cand_rm", "XOM", S.PROPOSAL_WATCHLIST_REMOVE)
    # run 1: nothing live -> snapshot is absent, so run 2 snapshots the run-1 state
    PAP.apply_approved_proposals(_OLD, base_dir=base, proposals=[],
                                 approved_ids=set(), write_files=True)
    PAP.apply_approved_proposals(_NOW, base_dir=base, proposals=[today],
                                 approved_candidate_ids={"cand_rm"}, write_files=True)
    rb = PAP.rollback_last(PAP.WATCHLIST_OVERLAY, base_dir=base,
                           now="2026-07-28T18:00:00+00:00")
    assert rb["ok"]
    assert set(rb["rolled_back_ids"]) == {"prop_rm", "cand_rm"}

    # and the durable rebuild honours it
    res = PAP.apply_approved_proposals(
        "2026-07-29T09:00:00+00:00", base_dir=base, proposals=[],
        approved_candidate_ids={"cand_rm"}, write_files=False)
    assert res["watchlist_applied"] == 0
