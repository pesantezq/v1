"""Revocation ledger fail-closed guard (mirrors Fix 5 for the approvals log).

Defect fixed: ``promotion_approvals.revoked_ids(base_dir)`` degrades to
``set()`` on ANY read failure, including a present-but-unparseable
``production_revocations.jsonl``. ``apply_approved_proposals`` used that set
to subtract revoked ops from the durable watchlist overlay, so corrupting the
revocation ledger silently RESURRECTED a human-revoked op into the live
production overlay while ``overlay_rebuild_skipped`` stayed False.

``revocations_log_unreadable`` (promotion_approvals.py) now distinguishes:
  * absent ledger -> None (legitimately no revocations)
  * present, every non-blank line parses -> None (a torn TRAILING line from a
    crash mid-append is a known, accepted residual risk and is tolerated as
    long as at least one line still parses)
  * present but wholly corrupt (zero of N non-blank lines parse) -> a reason

``apply_approved_proposals`` now refuses to rebuild the overlay on either an
unreadable approvals log OR an unreadable revocation ledger, through the SAME
refusal path (one skip flag, one code branch), and surfaces a human-readable
``reason`` in the returned state for both conditions.
"""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.sim_governance import production_application as PAP
from portfolio_automation.sim_governance import promotion_approvals as PA
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


def _audit(base_dir: str, rows: list[dict]) -> None:
    d = _appr_dir(base_dir)
    with (d / "production_application_audit.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _applied_row(pid: str, cid: str, sym: str, *, ts: str = _OLD) -> dict:
    return {"ts": ts, "event": "applied_to_production", "proposal_id": pid,
            "candidate_id": cid, "proposal_type": S.PROPOSAL_WATCHLIST_REMOVE,
            "change": {"op": "remove", "symbol": sym},
            "rollback_plan": "revoke it", "snapshots": {}}


def _seed_live_overlay(base_dir: str, sym: str, pid: str, cid: str) -> dict:
    latest = Path(base_dir) / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    live = {"generated_at": _OLD, "schema": "approved_watchlist_proposals.v1",
            "feeds_production": True, "applied_proposal_ids": [pid],
            "ops": [{"proposal_id": pid, "candidate_id": cid,
                     "proposal_type": S.PROPOSAL_WATCHLIST_REMOVE,
                     "change": {"symbol": sym}, "rollback_plan": "",
                     "applied_from": "human_approved_promotion_proposal"}]}
    (latest / "approved_watchlist_proposals.json").write_text(json.dumps(live),
                                                              encoding="utf-8")
    return live


def _overlay(base_dir: str) -> dict:
    return json.loads((Path(base_dir) / "latest" /
                       "approved_watchlist_proposals.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# revocations_log_unreadable — the classification rule itself
# ---------------------------------------------------------------------------

def test_absent_revocation_ledger_is_not_unreadable(tmp_path):
    assert PA.revocations_log_unreadable(_outputs(tmp_path)) is None


def test_wholly_corrupt_revocation_ledger_is_detected(tmp_path):
    base = _outputs(tmp_path)
    d = _appr_dir(base)
    (d / "production_revocations.jsonl").write_text(
        "{not valid json at all\nstill not valid\n", encoding="utf-8")
    reason = PA.revocations_log_unreadable(base)
    assert reason and reason.startswith("wholly_corrupt")


def test_torn_final_line_only_is_tolerated(tmp_path):
    """A valid revocation followed by a crash-torn trailing line is NOT unreadable."""
    base = _outputs(tmp_path)
    PA.revoke_application("cand_riot", "pesantez", _NOW, base_dir=base)
    d = _appr_dir(base)
    with (d / "production_revocations.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"target_id": "cand_tsla", "approver": "pesan')  # torn, no newline
    assert PA.revocations_log_unreadable(base) is None
    # and the valid revocation above the torn line still takes effect
    assert PA.revoked_ids(base) == {"cand_riot"}


def test_empty_revocation_ledger_file_is_not_unreadable(tmp_path):
    """A zero-byte/whitespace-only file is vacuously all-parseable -> None."""
    base = _outputs(tmp_path)
    d = _appr_dir(base)
    (d / "production_revocations.jsonl").write_text("\n\n", encoding="utf-8")
    assert PA.revocations_log_unreadable(base) is None


# ---------------------------------------------------------------------------
# apply_approved_proposals — fail-closed reuse of the existing refusal path
# ---------------------------------------------------------------------------

def test_wholly_corrupt_revocation_ledger_refuses_overlay_rebuild_and_does_not_resurrect(tmp_path):
    """The core reproduction: durable revoked op must not come back from the dead."""
    base = _outputs(tmp_path)
    # A human revoked cand_riot; that op is NOT in the live overlay (already
    # un-applied) -- but a DIFFERENT durable op (cand_tsla) IS still live, and
    # the audit log still shows the revoked op as historically applied, so a
    # naive "revoked_ids() == set()" degrade would let it resurrect on rebuild.
    _audit(base, [
        _applied_row("prop_riot", "cand_riot", "RIOT"),
        _applied_row("prop_tsla", "cand_tsla", "TSLA"),
    ])
    live = _seed_live_overlay(base, "TSLA", "prop_tsla", "cand_tsla")

    # Record a real revocation, then corrupt the ledger wholesale (simulating
    # e.g. disk corruption / truncation after the fact).
    PA.revoke_application("cand_riot", "pesantez", _OLD, base_dir=base)
    d = _appr_dir(base)
    (d / "production_revocations.jsonl").write_text("###corrupt###\nnot json\n",
                                                     encoding="utf-8")
    assert PA.revocations_log_unreadable(base), "ledger must be classified unreadable"

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base,
        approved_ids={"prop_riot", "prop_tsla"}, write_files=True)

    assert res["overlay_rebuild_skipped"] is True
    assert res["revocations_log_unreadable"]
    assert res["reason"]
    assert _overlay(base) == live, "existing overlay must be left untouched"
    assert "RIOT" not in [o["change"]["symbol"] for o in _overlay(base)["ops"]], (
        "the human-revoked op must NOT be resurrected")

    persisted = json.loads((d / "production_application_state.json")
                           .read_text(encoding="utf-8"))
    assert persisted["overlay_rebuild_skipped"] is True
    assert persisted["reason"]


def test_torn_final_line_in_revocation_ledger_does_not_refuse_rebuild(tmp_path):
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_riot", "cand_riot", "RIOT")])
    PA.revoke_application("cand_riot", "pesantez", _OLD, base_dir=base)
    d = _appr_dir(base)
    with (d / "production_revocations.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"target_id": "cand_other", "approver": "pes')  # torn tail

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[],
        approved_ids={"prop_riot"}, write_files=False)

    assert res["overlay_rebuild_skipped"] is False
    assert res["watchlist_applied"] == 0, "the still-valid revocation must take effect"


def test_absent_revocation_ledger_does_not_refuse_rebuild(tmp_path):
    """Backward compatibility: production today has no revocation ledger at all."""
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_riot", "cand_riot", "RIOT")])

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[],
        approved_ids={"prop_riot"}, write_files=False)

    assert res["overlay_rebuild_skipped"] is False
    assert res["watchlist_applied"] == 1, "op carries forward normally"


def test_reason_is_present_for_unreadable_approvals_condition(tmp_path):
    base = _outputs(tmp_path)
    d = _appr_dir(base)
    (d / "approved_proposals.json").write_text('{"approvals": [{"proposal_id"',
                                               encoding="utf-8")
    res = PAP.apply_approved_proposals(_NOW, base_dir=base, write_files=False)
    assert res["overlay_rebuild_skipped"] is True
    assert res["reason"], "reason must not be None for the approvals-log condition"
    assert res["approvals_log_unreadable"] == res["reason"]


def test_reason_is_present_for_unreadable_revocations_condition(tmp_path):
    base = _outputs(tmp_path)
    d = _appr_dir(base)
    (d / "production_revocations.jsonl").write_text("garbage\nmore garbage\n",
                                                     encoding="utf-8")
    res = PAP.apply_approved_proposals(_NOW, base_dir=base, write_files=False)
    assert res["overlay_rebuild_skipped"] is True
    assert res["reason"], "reason must not be None for the revocations-ledger condition"
    assert res["revocations_log_unreadable"] == res["reason"]
    assert res["approvals_log_unreadable"] is None, (
        "must not falsely blame the approvals log for a revocation-ledger failure")
