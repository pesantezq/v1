"""Either-id approval matching (Task 2).

A proposal is applied when its proposal_id OR its candidate_id carries a valid
human approval, so an unchanged fact re-proposed under a fresh proposal_id does
not need re-approval. Reject always beats approve — the human gate must never
loosen.
"""
from __future__ import annotations

from pathlib import Path

from portfolio_automation.sim_governance import production_application as PAP
from portfolio_automation.sim_governance import schemas as S


def _outputs(tmp_path: Path) -> str:
    d = tmp_path / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _proposal(pid: str, cid: str, sym: str = "RIOT") -> dict:
    return {
        "proposal_id": pid,
        "candidate_id": cid,
        "proposal_type": S.PROPOSAL_WATCHLIST_REMOVE,
        "proposed_production_change": {"op": "remove", "symbol": sym},
        "rollback_plan": "delete the op and re-run the loader",
    }


_NOW = "2026-07-28T17:00:00+00:00"


def test_candidate_approval_applies_a_new_proposal_id(tmp_path):
    """The treadmill fix: yesterday's approval covers today's regenerated proposal."""
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=_outputs(tmp_path),
        proposals=[_proposal("prop_TODAY", "cand_stable")],
        approved_ids=set(),                       # yesterday's proposal id is gone
        approved_candidate_ids={"cand_stable"},   # but the candidate is approved
        write_files=False,
    )
    assert res["applied_count"] == 1
    assert res["watchlist_applied"] == 1


def test_proposal_id_approval_still_works(tmp_path):
    """Backward compatibility: the 43 historical records key on proposal_id only."""
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=_outputs(tmp_path),
        proposals=[_proposal("prop_X", "cand_X")],
        approved_ids={"prop_X"},
        approved_candidate_ids=set(),
        write_files=False,
    )
    assert res["applied_count"] == 1


def test_unapproved_candidate_is_not_applied(tmp_path):
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=_outputs(tmp_path),
        proposals=[_proposal("prop_X", "cand_X")],
        approved_ids=set(), approved_candidate_ids=set(),
        write_files=False,
    )
    assert res["applied_count"] == 0
    assert res["ignored"][0]["reason"] == "pending_or_unapproved"


def test_candidate_reject_beats_proposal_id_approve(tmp_path):
    """Reject always wins — the gate must never loosen."""
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=_outputs(tmp_path),
        proposals=[_proposal("prop_X", "cand_X")],
        approved_ids={"prop_X"},
        rejected_candidate_ids={"cand_X"},
        write_files=False,
    )
    assert res["applied_count"] == 0
    assert res["ignored"][0]["reason"] == "rejected"


def test_proposal_id_reject_beats_candidate_approve(tmp_path):
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=_outputs(tmp_path),
        proposals=[_proposal("prop_X", "cand_X")],
        rejected_ids={"prop_X"},
        approved_candidate_ids={"cand_X"},
        write_files=False,
    )
    assert res["applied_count"] == 0
    assert res["ignored"][0]["reason"] == "rejected"


def test_proposal_without_candidate_id_is_unaffected(tmp_path):
    p = _proposal("prop_X", "cand_X")
    del p["candidate_id"]
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=_outputs(tmp_path), proposals=[p],
        approved_ids={"prop_X"}, write_files=False,
    )
    assert res["applied_count"] == 1


def test_audit_row_carries_candidate_id(tmp_path):
    import json
    base = _outputs(tmp_path)
    PAP.apply_approved_proposals(
        _NOW, base_dir=base,
        proposals=[_proposal("prop_X", "cand_X")],
        approved_ids={"prop_X"}, write_files=True,
    )
    audit = Path(base) / "promotion_approvals" / "production_application_audit.jsonl"
    rows = [json.loads(l) for l in audit.read_text().splitlines() if l.strip()]
    applied = [r for r in rows if r.get("event") == "applied_to_production"]
    assert applied, "no applied_to_production row written"
    assert applied[-1]["candidate_id"] == "cand_X"
