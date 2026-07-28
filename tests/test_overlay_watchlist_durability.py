"""Durable watchlist ops (Task 3).

Watchlist ops are durable membership state and must persist across runs even when
their candidate stops being proposed — which is exactly what happens after a
removal is applied and the producer correctly self-suppresses. Advisory ops are
current-state annotations and must keep refreshing.

A rejected or revoked op must NEVER be resurrected by the rebuild.
"""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.sim_governance import production_application as PAP
from portfolio_automation.sim_governance import schemas as S

_NOW = "2026-07-28T17:00:00+00:00"


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


def _applied_row(pid: str, cid: str, sym: str, ptype: str = S.PROPOSAL_WATCHLIST_REMOVE) -> dict:
    return {"ts": "2026-07-27T15:00:00+00:00", "event": "applied_to_production",
            "proposal_id": pid, "candidate_id": cid, "proposal_type": ptype,
            "change": {"op": "remove", "symbol": sym},
            "rollback_plan": "delete the op", "snapshots": {}}


def test_prior_applied_removal_persists_with_no_pending_proposal(tmp_path):
    """The core case: the producer self-suppresses, so nothing is proposed today."""
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_yesterday", "cand_riot", "RIOT")])

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[],       # nothing proposed today
        approved_ids={"prop_yesterday"},
        write_files=True,
    )

    assert res["watchlist_applied"] == 1
    ov = json.loads((Path(base) / "latest" / "approved_watchlist_proposals.json").read_text())
    assert [o["change"]["symbol"] for o in ov["ops"]] == ["RIOT"]


def test_rejected_prior_op_is_not_resurrected(tmp_path):
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_old", "cand_riot", "RIOT")])

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[],
        approved_ids=set(), rejected_ids={"prop_old"},
        write_files=False,
    )
    assert res["watchlist_applied"] == 0


def test_candidate_rejected_prior_op_is_not_resurrected(tmp_path):
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_old", "cand_riot", "RIOT")])

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[],
        approved_ids={"prop_old"}, rejected_candidate_ids={"cand_riot"},
        write_files=False,
    )
    assert res["watchlist_applied"] == 0


def test_unapproved_prior_op_is_not_resurrected(tmp_path):
    """An audit row alone is not authority — approval must still be present."""
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_old", "cand_riot", "RIOT")])

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[],
        approved_ids=set(), approved_candidate_ids=set(),
        write_files=False,
    )
    assert res["watchlist_applied"] == 0


def test_today_and_prior_ops_are_deduped_by_candidate(tmp_path):
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_old", "cand_riot", "RIOT")])
    today = {"proposal_id": "prop_new", "candidate_id": "cand_riot",
             "proposal_type": S.PROPOSAL_WATCHLIST_REMOVE,
             "proposed_production_change": {"op": "remove", "symbol": "RIOT"},
             "rollback_plan": "delete the op"}

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[today],
        approved_candidate_ids={"cand_riot"},
        write_files=False,
    )
    assert res["watchlist_applied"] == 1, "the same candidate must not be applied twice"


def test_advisory_ops_are_not_made_durable(tmp_path):
    """Advisory annotations must still refresh — a stale label would mislead."""
    base = _outputs(tmp_path)
    _audit(base, [{
        "ts": "2026-07-27T15:00:00+00:00", "event": "applied_to_production",
        "proposal_id": "prop_adv", "candidate_id": "cand_adv",
        "proposal_type": S.PROPOSAL_FLOCK_ADVISORY_CONTEXT,
        "change": {"op": "flock_context", "symbol": "GOOGL", "label": "stale"},
        "rollback_plan": "", "snapshots": {},
    }])

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[],
        approved_ids={"prop_adv"}, write_files=False,
    )
    assert res["advisory_applied"] == 0, "advisory ops must not persist from the audit log"


def test_missing_audit_log_degrades_to_empty(tmp_path):
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=_outputs(tmp_path), proposals=[], write_files=False,
    )
    assert res["watchlist_applied"] == 0


def test_corrupt_audit_lines_are_skipped(tmp_path):
    base = _outputs(tmp_path)
    d = Path(base) / "promotion_approvals"
    d.mkdir(parents=True, exist_ok=True)
    (d / "production_application_audit.jsonl").write_text(
        "not json\n" + json.dumps(_applied_row("prop_ok", "cand_ok", "RIOT")) + "\n",
        encoding="utf-8")

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_ids={"prop_ok"}, write_files=False,
    )
    assert res["watchlist_applied"] == 1
