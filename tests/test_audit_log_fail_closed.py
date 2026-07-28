"""Fix WS10 — fail-closed guard for production_application_audit.jsonl.

Defect fixed (confirmed by experiment, ``.superpowers/audit/ws-10-11-12-persistence.md``
WS10.4): ``production_application_audit.jsonl`` is the SOLE source
``_prior_durable_ops`` reconstructs durable watchlist membership from. Unlike
the approvals log and the revocation ledger (each guarded by a dedicated
``*_unreadable`` check before any overlay rebuild), the audit log had no such
guard: total corruption degraded silently to "no prior ops" (``_prior_durable_ops``
returns ``[]`` on any read exception), dropping ``durably_live_count`` from 1 to
0 while ``overlay_rebuild_skipped`` stayed False — no signal anywhere.

``audit_log_unreadable`` (production_application.py) mirrors
``revocations_log_unreadable``'s exact torn-tail-vs-total-corruption rule and is
routed through the SAME refusal path as the other two guards.
"""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.sim_governance import production_application as PAP
from portfolio_automation.sim_governance import schemas as S

_NOW = "2026-07-28T18:00:00+00:00"
_OLD = "2026-07-01T09:00:00+00:00"


def _outputs(tmp_path: Path) -> str:
    d = tmp_path / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _appr_dir(base_dir: str) -> Path:
    d = Path(base_dir) / "promotion_approvals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _audit_path(base_dir: str) -> Path:
    return _appr_dir(base_dir) / "production_application_audit.jsonl"


def _applied_row(pid: str, cid: str, sym: str, *, ts: str = _OLD) -> dict:
    return {"ts": ts, "event": "applied_to_production", "proposal_id": pid,
            "candidate_id": cid, "proposal_type": S.PROPOSAL_WATCHLIST_ADD,
            "change": {"op": "add", "symbol": sym},
            "rollback_plan": "revoke it", "snapshots": {}}


def _seed_live_overlay(base_dir: str, sym: str, pid: str, cid: str) -> dict:
    latest = Path(base_dir) / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    live = {"generated_at": _OLD, "schema": "approved_watchlist_proposals.v1",
            "feeds_production": True, "applied_proposal_ids": [pid],
            "ops": [{"proposal_id": pid, "candidate_id": cid,
                     "proposal_type": S.PROPOSAL_WATCHLIST_ADD,
                     "change": {"symbol": sym}, "rollback_plan": "",
                     "applied_from": "human_approved_promotion_proposal"}]}
    (latest / "approved_watchlist_proposals.json").write_text(json.dumps(live),
                                                              encoding="utf-8")
    return live


def _overlay(base_dir: str) -> dict:
    return json.loads((Path(base_dir) / "latest" /
                       "approved_watchlist_proposals.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# audit_log_unreadable — the classification rule itself
# ---------------------------------------------------------------------------

def test_absent_audit_log_is_not_unreadable(tmp_path):
    assert PAP.audit_log_unreadable(_outputs(tmp_path)) is None


def test_wholly_corrupt_audit_log_is_detected(tmp_path):
    base = _outputs(tmp_path)
    _audit_path(base).write_text("{not valid json at all\nstill not valid\n",
                                 encoding="utf-8")
    reason = PAP.audit_log_unreadable(base)
    assert reason and reason.startswith("wholly_corrupt")


def test_torn_final_line_only_is_tolerated(tmp_path):
    base = _outputs(tmp_path)
    _audit_path(base).write_text(json.dumps(_applied_row("prop_x", "cand_x", "XOM")) + "\n",
                                 encoding="utf-8")
    with _audit_path(base).open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-07-28T00:00:00", "event": "applied_to_pro')  # torn, no newline
    assert PAP.audit_log_unreadable(base) is None


def test_empty_audit_log_file_is_not_unreadable(tmp_path):
    base = _outputs(tmp_path)
    _audit_path(base).write_text("\n\n", encoding="utf-8")
    assert PAP.audit_log_unreadable(base) is None


# ---------------------------------------------------------------------------
# apply_approved_proposals — fail-closed reuse of the existing refusal path
# ---------------------------------------------------------------------------

def test_wholly_corrupt_audit_log_refuses_rebuild_and_does_not_drop_durable_ops(tmp_path):
    """The core reproduction: a durable op already live in the overlay must not
    be silently dropped just because the audit log (the only source
    _prior_durable_ops reconstructs from) got wholly corrupted.
    """
    base = _outputs(tmp_path)
    _audit_path(base).write_text(json.dumps(_applied_row("prop_xom", "cand_xom", "XOM")) + "\n",
                                 encoding="utf-8")
    live = _seed_live_overlay(base, "XOM", "prop_xom", "cand_xom")

    # BEFORE corruption: durable op resolves normally.
    before = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_ids={"prop_xom"},
        approved_candidate_ids={"cand_xom"}, write_files=False)
    assert before["overlay_rebuild_skipped"] is False
    assert before["durably_live_count"] == 1

    # Corrupt the audit log wholesale.
    _audit_path(base).write_text("###corrupt###\nnot json\n", encoding="utf-8")
    assert PAP.audit_log_unreadable(base), "audit log must be classified unreadable"

    after = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_ids={"prop_xom"},
        approved_candidate_ids={"cand_xom"}, write_files=True)

    assert after["overlay_rebuild_skipped"] is True
    assert after["audit_log_unreadable"]
    assert after["reason"]
    # durably_live_count must report what's STILL on disk, not zero.
    assert after["durably_live_count"] == 1
    assert _overlay(base) == live, "existing overlay must be left untouched"

    persisted = json.loads((_appr_dir(base) / "production_application_state.json")
                           .read_text(encoding="utf-8"))
    assert persisted["overlay_rebuild_skipped"] is True
    assert persisted["audit_log_unreadable"]
    assert persisted["reason"]


def test_torn_final_line_in_audit_log_does_not_refuse_rebuild(tmp_path):
    base = _outputs(tmp_path)
    _audit_path(base).write_text(json.dumps(_applied_row("prop_xom", "cand_xom", "XOM")) + "\n",
                                 encoding="utf-8")
    with _audit_path(base).open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-07-28T00:00:00", "event": "applied_to_pro')  # torn tail

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_ids={"prop_xom"},
        approved_candidate_ids={"cand_xom"}, write_files=False)

    assert res["overlay_rebuild_skipped"] is False
    assert res["durably_live_count"] == 1, "the still-valid durable op must take effect"


def test_absent_audit_log_does_not_refuse_rebuild(tmp_path):
    """Backward compatibility: no prior audit log at all → normal (empty prior-ops) operation."""
    base = _outputs(tmp_path)
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_ids=set(), write_files=False)
    assert res["overlay_rebuild_skipped"] is False
    assert res["durably_live_count"] == 0


def test_reason_surfaced_for_all_three_unreadable_conditions(tmp_path):
    # 1. approvals log unreadable
    base1 = _outputs(tmp_path / "case1")
    (_appr_dir(base1) / "approved_proposals.json").write_text(
        '{"approvals": [{"proposal_id"', encoding="utf-8")
    res1 = PAP.apply_approved_proposals(_NOW, base_dir=base1, write_files=False)
    assert res1["overlay_rebuild_skipped"] is True
    assert res1["reason"] and res1["approvals_log_unreadable"] == res1["reason"]
    assert res1["revocations_log_unreadable"] is None
    assert res1["audit_log_unreadable"] is None

    # 2. revocations ledger unreadable
    base2 = _outputs(tmp_path / "case2")
    (_appr_dir(base2) / "production_revocations.jsonl").write_text(
        "garbage\nmore garbage\n", encoding="utf-8")
    res2 = PAP.apply_approved_proposals(_NOW, base_dir=base2, write_files=False)
    assert res2["overlay_rebuild_skipped"] is True
    assert res2["reason"] and res2["revocations_log_unreadable"] == res2["reason"]
    assert res2["approvals_log_unreadable"] is None
    assert res2["audit_log_unreadable"] is None

    # 3. audit log unreadable
    base3 = _outputs(tmp_path / "case3")
    (_appr_dir(base3) / "production_application_audit.jsonl").write_text(
        "garbage\nmore garbage\n", encoding="utf-8")
    res3 = PAP.apply_approved_proposals(_NOW, base_dir=base3, write_files=False)
    assert res3["overlay_rebuild_skipped"] is True
    assert res3["reason"] and res3["audit_log_unreadable"] == res3["reason"]
    assert res3["approvals_log_unreadable"] is None
    assert res3["revocations_log_unreadable"] is None
