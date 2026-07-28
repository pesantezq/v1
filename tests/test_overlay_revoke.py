"""Explicit revoke path (Task 4).

Operator decision: an applied watchlist op persists until a recorded human
decision reverses it. Data drift never restores a symbol. Revocation must itself
be human-gated — an AI marker cannot revoke, just as it cannot approve.
"""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.sim_governance import production_application as PAP
from portfolio_automation.sim_governance import promotion_approvals as PA
from portfolio_automation.sim_governance import schemas as S

_NOW = "2026-07-29T09:00:00+00:00"


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


def _applied_row(pid: str, cid: str, sym: str) -> dict:
    return {"ts": "2026-07-28T09:00:00+00:00", "event": "applied_to_production",
            "proposal_id": pid, "candidate_id": cid,
            "proposal_type": S.PROPOSAL_WATCHLIST_REMOVE,
            "change": {"op": "remove", "symbol": sym},
            "rollback_plan": "delete the op", "snapshots": {}}


def test_revoke_is_recorded(tmp_path):
    base = _outputs(tmp_path)
    res = PA.revoke_application("cand_riot", "pesantez", _NOW, base_dir=base)
    assert res["ok"] is True, res["reason"]
    assert PA.revoked_ids(base) == {"cand_riot"}


def test_ai_cannot_revoke(tmp_path):
    """Revocation is human-gated, exactly like approval."""
    base = _outputs(tmp_path)
    res = PA.revoke_application("cand_riot", "auto_approval", _NOW, base_dir=base)
    assert res["ok"] is False
    assert PA.revoked_ids(base) == set()


def test_revoked_op_is_dropped_from_the_overlay(tmp_path):
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_old", "cand_riot", "RIOT")])
    PA.revoke_application("cand_riot", "pesantez", _NOW, base_dir=base)

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_ids={"prop_old"},
        write_files=False,
    )
    assert res["watchlist_applied"] == 0, "a revoked op must not persist"


def test_revoke_by_proposal_id_also_works(tmp_path):
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_old", "cand_riot", "RIOT")])
    PA.revoke_application("prop_old", "pesantez", _NOW, base_dir=base)

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_ids={"prop_old"},
        write_files=False,
    )
    assert res["watchlist_applied"] == 0


def test_unrevoked_sibling_op_survives_a_revoke(tmp_path):
    """Revoking one symbol must not drop the others."""
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_a", "cand_riot", "RIOT"),
                  _applied_row("prop_b", "cand_tsla", "TSLA")])
    PA.revoke_application("cand_riot", "pesantez", _NOW, base_dir=base)

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[],
        approved_ids={"prop_a", "prop_b"}, write_files=False,
    )
    assert res["watchlist_applied"] == 1


def test_missing_revoke_log_degrades_to_empty(tmp_path):
    assert PA.revoked_ids(_outputs(tmp_path)) == set()
