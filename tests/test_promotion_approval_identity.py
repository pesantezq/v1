"""Candidate-keyed approval identity (Task 1).

make_candidate_id is stable-when-unchanged by design (schemas.py:245); the flock
producer salts it with the flock STATE. make_proposal_id hashes candidate_id|now,
so the proposal id churns every run even when the fact is identical. Approvals key
on proposal_id, so an unchanged fact needs daily re-approval. Recording
candidate_id lets an approval outlive the proposal id it was filed against.

Backward compatibility is load-bearing: 43 real records carry no candidate_id.
"""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.sim_governance import promotion_approvals as PA
from portfolio_automation.sim_governance import schemas as S

_NOW = "2026-07-28T17:00:00+00:00"


def _outputs(tmp_path: Path) -> str:
    d = tmp_path / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _write_log(base_dir: str, approvals: list[dict]) -> None:
    d = Path(base_dir) / "promotion_approvals"
    d.mkdir(parents=True, exist_ok=True)
    (d / "approved_proposals.json").write_text(
        json.dumps({"generated_at": _NOW, "schema": "approved_proposals.v1",
                    "approvals": approvals}), encoding="utf-8")


def _rec(pid: str, decision: str = "approve", *, cid: str | None = None) -> dict:
    r = {"proposal_id": pid, "decision": decision, "approver": "pesantez",
         "timestamp": _NOW, "notes": None, "review_date": None}
    if cid is not None:
        r["candidate_id"] = cid
    return r


def test_record_approval_persists_candidate_id(tmp_path):
    base = _outputs(tmp_path)
    res = PA.record_approval("prop_a", "approve", "pesantez", _NOW,
                             base_dir=base, candidate_id="cand_x")
    assert res["ok"] is True, res["reason"]
    assert res["record"]["candidate_id"] == "cand_x"


def test_legacy_record_without_candidate_id_is_still_valid(tmp_path):
    """The 43 historical records carry no candidate_id and must keep working."""
    ok, reason = S.is_valid_approval_record(_rec("prop_legacy"))
    assert ok is True, reason


def test_fold_by_candidate_ignores_records_without_candidate_id(tmp_path):
    base = _outputs(tmp_path)
    _write_log(base, [_rec("prop_legacy"), _rec("prop_b", cid="cand_b")])

    by_cand = PA.effective_approvals_by_candidate(base)

    assert by_cand == {"cand_b": "approve"}
    # proposal-id folding is untouched and still sees both
    assert PA.approved_proposal_ids(base) == {"prop_legacy", "prop_b"}


def test_last_record_wins_per_candidate(tmp_path):
    """A later reject supersedes an earlier approve for the same candidate."""
    base = _outputs(tmp_path)
    _write_log(base, [
        _rec("prop_1", "approve", cid="cand_x"),
        _rec("prop_2", "reject", cid="cand_x"),
    ])

    assert PA.effective_approvals_by_candidate(base) == {"cand_x": "reject"}
    assert PA.approved_candidate_ids(base) == set()
    assert PA.rejected_candidate_ids(base) == {"cand_x"}


def test_approve_under_a_new_proposal_id_keeps_the_candidate_approved(tmp_path):
    """The treadmill case: same fact, new proposal id each run."""
    base = _outputs(tmp_path)
    _write_log(base, [_rec("prop_day1", "approve", cid="cand_same")])

    assert PA.approved_candidate_ids(base) == {"cand_same"}


def test_missing_log_degrades_to_empty(tmp_path):
    base = _outputs(tmp_path)
    assert PA.effective_approvals_by_candidate(base) == {}
    assert PA.approved_candidate_ids(base) == set()
    assert PA.rejected_candidate_ids(base) == set()


def test_ai_approver_still_rejected_with_candidate_id(tmp_path):
    """candidate_id must not become a bypass for the human gate."""
    base = _outputs(tmp_path)
    res = PA.record_approval("prop_ai", "approve", "auto_approval", _NOW,
                             base_dir=base, candidate_id="cand_x")
    assert res["ok"] is False
    assert "human" in res["reason"].lower() or "approver" in res["reason"].lower()
