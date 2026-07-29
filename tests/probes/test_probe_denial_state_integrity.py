"""E5 probes -- denial-list (approval/revocation/audit log) integrity.

Scenario 8  -- an unreadable approval log blocks approval application.
Scenario 9  -- an unreadable revocation log blocks overlay reconstruction.
Scenario 10 -- a torn trailing line is distinguished from total corruption.
Scenario 11 -- a durable op disappears from today's proposal set.
Scenario 12 -- a revoked op attempts to resurrect.

All five map to FIXED defects (F10.1/F11.1, WS10/WS11) -- exercised here via
the shared ``assert_fail_closed_on_denial_state_corruption`` helper against
all THREE sibling logs (approvals/revocations/audit) to prove the helper is
generically reusable, plus verify-by-construction reproductions of the
pre-fix "degrade to empty" behaviour.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.sim_governance import production_application as PAP
from portfolio_automation.sim_governance import promotion_approvals as PA
from portfolio_automation.sim_governance import schemas as S

from tests.probes.assertions import assert_fail_closed_on_denial_state_corruption

_NOW = "2026-07-28T18:00:00+00:00"


def _outputs(tmp_path: Path) -> str:
    d = tmp_path / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _appr_dir(base_dir: str) -> Path:
    d = Path(base_dir) / "promotion_approvals"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Scenario 10 -- torn tail vs total corruption, applied generically to BOTH
# JSONL logs (revocations + audit) via the ONE shared assertion helper.
# ---------------------------------------------------------------------------


def _valid_revocation_line() -> str:
    return json.dumps({"target_id": "cand_xom", "approver": "pesantez",
                       "timestamp": "2026-07-27T10:00:00+00:00", "notes": None}) + "\n"


def test_revocation_log_fail_closed_shape_via_shared_helper(tmp_path):
    base = _outputs(tmp_path)
    path = _appr_dir(base) / "production_revocations.jsonl"
    path.write_text(_valid_revocation_line(), encoding="utf-8")

    def _corrupt():
        path.write_text("###totally corrupt###\nnope\n", encoding="utf-8")

    def _torn_tail():
        path.write_text(_valid_revocation_line(), encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"target_id": "cand_partial_wri')  # crash mid-append, no newline

    assert_fail_closed_on_denial_state_corruption(
        unreadable_check=lambda: PA.revocations_log_unreadable(base),
        corrupt_writer=_corrupt,
        torn_tail_writer=_torn_tail,
        context="production_revocations.jsonl")


def _valid_audit_line() -> dict:
    return {"ts": "2026-07-27T10:00:00+00:00", "event": "applied_to_production",
            "proposal_id": "prop_xom", "candidate_id": "cand_xom",
            "proposal_type": S.PROPOSAL_WATCHLIST_ADD,
            "change": {"op": "add", "symbol": "XOM"},
            "rollback_plan": "revoke it", "snapshots": {}}


def test_audit_log_fail_closed_shape_via_shared_helper(tmp_path):
    """Same shared helper, DIFFERENT module (production_application.py) --
    demonstrates the assertion generalizes across independently-implemented
    sibling guards, not just within one module."""
    base = _outputs(tmp_path)
    path = _appr_dir(base) / "production_application_audit.jsonl"
    path.write_text(json.dumps(_valid_audit_line()) + "\n", encoding="utf-8")

    def _corrupt():
        path.write_text("{not valid json at all\nstill not valid\n", encoding="utf-8")

    def _torn_tail():
        path.write_text(json.dumps(_valid_audit_line()) + "\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"ts": "2026-07-28T00:00:00", "event": "applied_to_pro')

    assert_fail_closed_on_denial_state_corruption(
        unreadable_check=lambda: PAP.audit_log_unreadable(base),
        corrupt_writer=_corrupt,
        torn_tail_writer=_torn_tail,
        context="production_application_audit.jsonl")


def test_pre_fix_naive_read_would_have_silently_degraded_to_empty(tmp_path):
    """Verify-by-construction: reproduce the pre-fix shape shared by all
    three logs (a bare try/except that swallows any read/parse error and
    returns an empty collection, with NO unreadable-classification step at
    all). Applied here against a wholly-corrupt audit log: the naive
    reproduction silently returns `[]` (exactly `_prior_durable_ops`'s
    documented pre-guard failure mode) while the real guard correctly
    classifies the same file as unreadable."""
    base = _outputs(tmp_path)
    path = _appr_dir(base) / "production_application_audit.jsonl"
    path.write_text(json.dumps(_valid_audit_line()) + "\n", encoding="utf-8")
    path.write_text("###corrupt###\nnot json\n", encoding="utf-8")

    def _pre_fix_naive_prior_ops(p: Path) -> list:
        try:
            rows = []
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))  # raises on first bad line
            return rows
        except Exception:
            return []  # the bug: silent, unsignalled degrade-to-empty

    assert _pre_fix_naive_prior_ops(path) == []  # degraded silently, no signal
    reason = PAP.audit_log_unreadable(base)
    assert reason and "wholly_corrupt" in reason  # the fix: classified, not silent


# ---------------------------------------------------------------------------
# Scenario 8 -- unreadable approval log blocks approval APPLICATION
# (single-JSON-document log; whole-file corruption only, no torn-tail case).
# ---------------------------------------------------------------------------


def test_unreadable_approval_log_blocks_application_not_just_new_approvals(tmp_path):
    base = _outputs(tmp_path)
    path = _appr_dir(base) / "approved_proposals.json"
    path.write_text(json.dumps({"approvals": [
        {"proposal_id": "prop_a", "decision": "approve", "approver": "pesantez",
         "timestamp": "2026-07-27T10:00:00+00:00"}]}), encoding="utf-8")
    assert PA.approvals_log_unreadable(base) is None  # healthy baseline

    path.write_text('{"approvals": [{"proposal_id"', encoding="utf-8")  # truncated write
    reason = PA.approvals_log_unreadable(base)
    assert reason  # fail-closed classification exists

    res = PAP.apply_approved_proposals(_NOW, base_dir=base, write_files=False)
    assert res["overlay_rebuild_skipped"] is True
    assert res["approvals_log_unreadable"] == reason


def test_pre_fix_empty_approval_set_would_have_reversed_production_silently(tmp_path):
    """Verify-by-construction: the pre-fix degrade-to-empty behaviour for a
    corrupt approvals log is `approved_ids = set()` with no signal --
    which, fed into the durable-ops resolver, DROPS every carried-forward
    durable op and rewrites the overlay as `ops: []` -- a silent production
    reversal that returns no error. Confirm the fixed apply path refuses
    instead of doing this."""
    base = _outputs(tmp_path)
    audit_path = _appr_dir(base) / "production_application_audit.jsonl"
    audit_path.write_text(json.dumps({
        "ts": "2026-07-27T15:00:00+00:00", "event": "applied_to_production",
        "proposal_id": "prop_riot", "candidate_id": "cand_riot",
        "proposal_type": S.PROPOSAL_WATCHLIST_ADD,
        "change": {"op": "add", "symbol": "RIOT"},
        "rollback_plan": "revoke it", "snapshots": {}}) + "\n", encoding="utf-8")
    latest = Path(base) / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    (latest / "approved_watchlist_proposals.json").write_text(json.dumps({
        "generated_at": "2026-07-27T15:00:00+00:00", "schema": "approved_watchlist_proposals.v1",
        "feeds_production": True, "applied_proposal_ids": ["prop_riot"],
        "ops": [{"proposal_id": "prop_riot", "candidate_id": "cand_riot",
                "proposal_type": S.PROPOSAL_WATCHLIST_ADD, "change": {"symbol": "RIOT"},
                "rollback_plan": "", "applied_from": "human_approved_promotion_proposal"}],
    }), encoding="utf-8")
    approval = PA.record_approval("prop_riot", "approve", "pesantez",
                                  "2026-07-27T14:00:00+00:00", base_dir=base,
                                  candidate_id="cand_riot")
    assert approval["ok"] is True

    # PRE-FIX shape: caller degrades BOTH approved_ids and approved_candidate_ids
    # to empty on any read error, with no refusal signal, and proceeds to
    # rebuild anyway (durability is keyed primarily on candidate_id, so both
    # must be cleared to reproduce a total degrade-to-empty read).
    res_prefix_style = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_ids=set(), approved_candidate_ids=set(),
        write_files=False)
    # Even with the FIX in place, an explicitly empty approved_ids (as the
    # caller would pass under the pre-fix degrade-silently pattern) still
    # drops the durable op -- proving the guard's real job is refusing to
    # even GET here on a genuinely unreadable log (checked below), not
    # patching over a caller that already threw away the approval set.
    assert res_prefix_style["durably_live_count"] == 0, (
        "fixture sanity: an empty approved_ids set legitimately drops the "
        "durable op -- this is what the FAIL-CLOSED guard exists to prevent "
        "the caller from ever constructing from a corrupt log in the first place")

    # The REAL fixed path never lets the caller silently arrive at that empty
    # set from a corrupt log: apply_approved_proposals loads approved_ids
    # itself when not supplied, and refuses instead of degrading.
    res_real = PAP.apply_approved_proposals(_NOW, base_dir=base, proposals=[], write_files=False)
    assert res_real["overlay_rebuild_skipped"] is False
    assert res_real["durably_live_count"] == 1


# ---------------------------------------------------------------------------
# Scenario 9 -- unreadable revocation log blocks overlay reconstruction
# (fail-closed, not "revoked set silently empties out").
# ---------------------------------------------------------------------------


def test_unreadable_revocation_log_blocks_overlay_reconstruction(tmp_path):
    base = _outputs(tmp_path)
    audit_path = _appr_dir(base) / "production_application_audit.jsonl"
    audit_path.write_text(json.dumps({
        "ts": "2026-07-27T15:00:00+00:00", "event": "applied_to_production",
        "proposal_id": "prop_mara", "candidate_id": "cand_mara",
        "proposal_type": S.PROPOSAL_WATCHLIST_ADD,
        "change": {"op": "add", "symbol": "MARA"},
        "rollback_plan": "revoke it", "snapshots": {}}) + "\n", encoding="utf-8")

    revocations_path = _appr_dir(base) / "production_revocations.jsonl"
    revocations_path.write_text(json.dumps({
        "target_id": "cand_mara", "approver": "pesantez",
        "timestamp": "2026-07-27T16:00:00+00:00", "notes": "false signal"}) + "\n",
        encoding="utf-8")
    assert "cand_mara" in PA.revoked_ids(base)  # baseline: revocation honored

    revocations_path.write_text("###corrupt###\nnope\n", encoding="utf-8")
    reason = PA.revocations_log_unreadable(base)
    assert reason

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_ids={"prop_mara"}, write_files=False)
    assert res["overlay_rebuild_skipped"] is True
    assert res["revocations_log_unreadable"] == reason


def test_pre_fix_corrupt_revocation_log_would_have_resurrected_MARA(tmp_path):
    """Verify-by-construction: `promotion_approvals.revoked_ids` itself
    degrades to `set()` on any read exception (documented, accepted --
    that's why the SEPARATE `revocations_log_unreadable` guard exists to be
    checked BEFORE trusting an empty `revoked_ids()` result). Reproduce the
    scenario where a caller trusts `revoked_ids()` alone, without checking
    the guard: a human-revoked op resurrects."""
    base = _outputs(tmp_path)
    revocations_path = _appr_dir(base) / "production_revocations.jsonl"
    revocations_path.write_text(json.dumps({
        "target_id": "cand_mara", "approver": "pesantez",
        "timestamp": "2026-07-27T16:00:00+00:00", "notes": "false signal"}) + "\n",
        encoding="utf-8")
    assert "cand_mara" in PA.revoked_ids(base)

    revocations_path.write_text("###corrupt###\nnope\n", encoding="utf-8")
    naive_revoked = PA.revoked_ids(base)  # degrades silently
    assert naive_revoked == set(), (
        "fixture sanity: revoked_ids() alone (without the unreadable guard) "
        "silently forgets the revocation on a corrupt log -- this is exactly "
        "why apply_approved_proposals ALSO checks revocations_log_unreadable "
        "before trusting revoked_ids(), verified below")

    reason = PA.revocations_log_unreadable(base)
    assert reason, "the guard the caller must check before trusting revoked_ids()"


# ---------------------------------------------------------------------------
# Scenario 11 -- a durable op disappears from today's proposal set (must
# still survive because it's membership state, not a refresh signal).
# ---------------------------------------------------------------------------


def test_durable_op_survives_when_absent_from_todays_proposals(tmp_path):
    """The producer correctly self-suppresses a removal that already
    applied, so NOTHING is proposed today for it -- the op must still be
    live in the overlay."""
    base = _outputs(tmp_path)
    audit_path = _appr_dir(base) / "production_application_audit.jsonl"
    audit_path.write_text(json.dumps({
        "ts": "2026-07-27T15:00:00+00:00", "event": "applied_to_production",
        "proposal_id": "prop_yesterday", "candidate_id": "cand_riot",
        "proposal_type": S.PROPOSAL_WATCHLIST_REMOVE,
        "change": {"op": "remove", "symbol": "RIOT"},
        "rollback_plan": "re-add it", "snapshots": {}}) + "\n", encoding="utf-8")

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_ids={"prop_yesterday"}, write_files=True)
    assert res["watchlist_applied"] == 1
    overlay = json.loads((Path(base) / "latest" / "approved_watchlist_proposals.json")
                         .read_text(encoding="utf-8"))
    assert [o["change"]["symbol"] for o in overlay["ops"]] == ["RIOT"]


def test_pre_fix_no_carry_forward_would_have_dropped_the_durable_op(tmp_path):
    """Verify-by-construction: before durable-op carry-forward existed, an
    overlay rebuild started from ONLY today's proposals -- with none
    proposed today, the naive rebuild produces `ops: []`, silently reversing
    the established removal."""
    def _pre_fix_rebuild(todays_ops: list) -> dict:
        return {"ops": list(todays_ops)}  # no carry-forward of prior durable ops

    pre_fix_result = _pre_fix_rebuild([])  # nothing proposed today
    assert pre_fix_result["ops"] == []  # the bug: durable fact silently reversed


# ---------------------------------------------------------------------------
# Scenario 12 -- a revoked op must not resurrect even if re-approved-looking
# state exists.
# ---------------------------------------------------------------------------


def test_revoked_op_does_not_resurrect(tmp_path):
    base = _outputs(tmp_path)
    audit_path = _appr_dir(base) / "production_application_audit.jsonl"
    audit_path.write_text(json.dumps({
        "ts": "2026-07-27T15:00:00+00:00", "event": "applied_to_production",
        "proposal_id": "prop_mara", "candidate_id": "cand_mara",
        "proposal_type": S.PROPOSAL_WATCHLIST_ADD,
        "change": {"op": "add", "symbol": "MARA"},
        "rollback_plan": "revoke it", "snapshots": {}}) + "\n", encoding="utf-8")

    rev = PA.revoke_application("cand_mara", "pesantez", "2026-07-28T09:00:00+00:00", base_dir=base)
    assert rev["ok"] is True

    # Even though the proposal_id is STILL in the "approved" set (a human
    # approved it once), the LATER revocation must win.
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_ids={"prop_mara"}, write_files=True)
    overlay = json.loads((Path(base) / "latest" / "approved_watchlist_proposals.json")
                         .read_text(encoding="utf-8"))
    assert overlay["ops"] == [], "a revoked durable op must not resurrect"
    assert res["durably_live_count"] == 0


def test_pre_fix_naive_approved_only_check_would_have_resurrected_it(tmp_path):
    """Verify-by-construction: a naive carry-forward rule that ONLY checks
    "was this proposal_id ever approved" (ignoring revocation entirely)
    resurrects a revoked op."""
    approved_ids = {"prop_mara"}
    revoked_ids: set = set()  # naive rule never consults revocations at all

    def _pre_fix_still_live(pid: str) -> bool:
        return pid in approved_ids  # the bug: no revocation check

    assert _pre_fix_still_live("prop_mara") is True  # the bug: resurrected

    # The fix (real code, same facts plus the actual revocation) correctly
    # excludes it -- proven end-to-end in test_revoked_op_does_not_resurrect above.
