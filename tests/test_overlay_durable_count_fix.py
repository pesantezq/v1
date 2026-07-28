"""Fix: `durably_live_count` must count only DURABLE-typed watchlist ops
(final-review fix wave, 2026-07-28).

`watchlist_ops` holds every op routed to the watchlist overlay by *workflow*,
including non-durable, state-derived types such as
`flock_watchlist_candidate_logic`. `watchlist_applied` legitimately counts all
of them ("ops in the watchlist overlay"), but `durably_live_count` — rendered
by the GUI as "N durable op(s) live in production" — must count only the
subset whose `proposal_type` satisfies `is_durable_proposal_type`. Before this
fix the two fields were identical, so a flock-only run claimed durability for
an op that refreshes away on the very next run.
"""
from __future__ import annotations

from pathlib import Path

from portfolio_automation.sim_governance import production_application as PAP
from portfolio_automation.sim_governance import schemas as S

_NOW = "2026-07-28T17:00:00+00:00"


def _outputs(tmp_path: Path) -> str:
    d = tmp_path / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _proposal(pid, cid, sym, ptype, **change) -> dict:
    return {"proposal_id": pid, "candidate_id": cid, "proposal_type": ptype,
            "proposed_production_change": {"symbol": sym, **change},
            "rollback_plan": "revoke it"}


def test_flock_only_run_reports_zero_durably_live(tmp_path):
    base = _outputs(tmp_path)
    today = _proposal("prop_flock", "cand_flock", "GOOGL",
                      S.PROPOSAL_FLOCK_WATCHLIST_LOGIC, confirmed=True)

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[today],
        approved_ids={"prop_flock"}, approved_candidate_ids={"cand_flock"},
        write_files=False,
    )

    assert res["watchlist_applied"] == 1, "the flock op IS in the watchlist overlay"
    assert res["durably_live_count"] == 0, "a flock op is NOT a durable membership decision"
    assert res["watchlist_applied_today"] == 0, "consistent with the durable-live label"


def test_mixed_run_durable_count_excludes_the_flock_op(tmp_path):
    base = _outputs(tmp_path)
    durable = _proposal("prop_add", "cand_add", "XOM", S.PROPOSAL_WATCHLIST_ADD)
    flock = _proposal("prop_flock", "cand_flock", "GOOGL",
                      S.PROPOSAL_FLOCK_WATCHLIST_LOGIC, confirmed=True)

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[durable, flock],
        approved_ids={"prop_add", "prop_flock"},
        approved_candidate_ids={"cand_add", "cand_flock"},
        write_files=False,
    )

    assert res["watchlist_applied"] == 2
    assert res["durably_live_count"] == 1
    assert res["watchlist_applied_today"] == 1


def test_early_return_fail_closed_path_filters_durable_only(tmp_path, monkeypatch):
    """The overlay-rebuild-skipped branch must not report the mixed count either."""
    base = _outputs(tmp_path)
    appr_dir = Path(base) / "promotion_approvals"
    appr_dir.mkdir(parents=True, exist_ok=True)
    (appr_dir / "approved_proposals.json").write_bytes(b"{not valid json")

    latest_dir = Path(base) / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    import json as _json
    (latest_dir / PAP.WATCHLIST_OVERLAY).write_text(_json.dumps({
        "ops": [
            {"proposal_id": "p1", "candidate_id": "c1",
             "proposal_type": S.PROPOSAL_WATCHLIST_ADD, "change": {"symbol": "XOM"}},
            {"proposal_id": "p2", "candidate_id": "c2",
             "proposal_type": S.PROPOSAL_FLOCK_WATCHLIST_LOGIC, "change": {"symbol": "GOOGL"}},
        ]
    }), encoding="utf-8")

    res = PAP.apply_approved_proposals(_NOW, base_dir=base, write_files=False)

    assert res["overlay_rebuild_skipped"] is True
    assert res["watchlist_applied"] == 2
    assert res["durably_live_count"] == 1
