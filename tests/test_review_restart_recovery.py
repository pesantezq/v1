"""Restart reconstruction across a REAL process death.

PROCESS A runs the review lifecycle in a subprocess and dies with ``os._exit``
at a named point -- no exception, no atexit, no flush, which is what a machine
crash actually looks like. ``sys.exit`` would run cleanup and let the test lie.

PROCESS B receives ONLY the directory. It never sees the packet object, the
binding, the SHA or the criteria, so anything it reconstructs came from durable
evidence. The reviewer records its calls to a file, because a call count is the
only cross-process proof of whether an independent reviewer was consulted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from portfolio_automation.engineer_worker.review_journal import (
    JournalError, LifecycleKind, RecoveryState, ReviewJournal, WAL_CONTRACT,
    criterion_set_digest, read_events_strict, review_invocation_id,
)

REPO = Path(__file__).resolve().parents[1]

DRIVER = '''
import json, os, sys
sys.path.insert(0, {repo!r})
from pathlib import Path
from portfolio_automation.engineer_worker.review_journal import (
    LifecycleKind, ReviewJournal, criterion_set_digest, review_invocation_id)

root = Path(sys.argv[1])
die_at = sys.argv[2]
journal = ReviewJournal(root / "journal.jsonl")

RID = review_invocation_id(
    candidate_sha="a" * 40, packet_hash="pkt_" + "1" * 32, mission_id="m1",
    task_id="t1", criterion_digest=criterion_set_digest(["C1", "C2"]),
    reviewer_identity={{"provider": "p", "model": "m", "protocol": "one-shot"}})
(root / "invocation_id.txt").write_text(RID)

def call_reviewer():
    with open(root / "reviewer_calls.log", "a") as fh:
        fh.write(RID + "\\n"); fh.flush(); os.fsync(fh.fileno())
    return {{"verdict": "PASS"}}

journal.append(LifecycleKind.PACKET_BUILT, review_invocation_id=RID,
               packet_hash="pkt_" + "1" * 32)
if die_at == "after_packet_built": os._exit(9)

journal.append(LifecycleKind.PACKET_PERSISTED, review_invocation_id=RID,
               packet_hash="pkt_" + "1" * 32, packet_blob_rel="docs/x.json")
if die_at == "after_persist": os._exit(9)

journal.append(LifecycleKind.CANDIDATE_BOUND, review_invocation_id=RID,
               candidate_bound="YES")
if die_at == "after_bound": os._exit(9)

journal.append(LifecycleKind.DISPATCH_ATTEMPTED, review_invocation_id=RID,
               head_unchanged_at_dispatch="YES")
if die_at == "after_dispatch_attempted": os._exit(9)

journal.append(LifecycleKind.REVIEWER_CALLED, review_invocation_id=RID,
               note="WRITE-AHEAD: fsynced before the request left this process")
if die_at == "after_reviewer_called_before_call": os._exit(9)

decision = call_reviewer()
if die_at == "after_call_before_returned": os._exit(9)

journal.append(LifecycleKind.VERDICT_RETURNED, review_invocation_id=RID,
               verdict=decision)
if die_at == "after_returned": os._exit(9)

journal.append(LifecycleKind.VERDICT_PERSISTED, review_invocation_id=RID,
               verdict=decision)
os._exit(0)
'''


def _process_a(tmp_path: Path, die_at: str) -> subprocess.CompletedProcess:
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER.format(repo=str(REPO)), encoding="utf-8")
    return subprocess.run([sys.executable, str(driver), str(tmp_path), die_at],
                          capture_output=True, text=True)


def _reviewer_calls(tmp_path: Path) -> int:
    log = tmp_path / "reviewer_calls.log"
    if not log.exists():
        return 0
    return len([ln for ln in log.read_text().splitlines() if ln.strip()])


def _recover(tmp_path: Path):
    """PROCESS B: durable evidence only."""
    rid = (tmp_path / "invocation_id.txt").read_text().strip()
    return ReviewJournal(tmp_path / "journal.jsonl").recover(rid)


# ── the harness must actually die ──────────────────────────────────────────
@pytest.mark.parametrize("die_at", [
    "after_packet_built", "after_persist", "after_bound",
    "after_dispatch_attempted", "after_reviewer_called_before_call",
    "after_call_before_returned", "after_returned"])
def test_process_a_really_dies_at_each_crash_point(tmp_path, die_at):
    """Tripwire. If the crash point never fires the whole suite would pass
    vacuously, proving nothing about recovery."""
    proc = _process_a(tmp_path, die_at)
    assert proc.returncode == 9, proc.stderr


def test_recovery_reads_nothing_but_the_directory(tmp_path):
    _process_a(tmp_path, "after_persist")
    (tmp_path / "journal.jsonl").unlink()
    rid = (tmp_path / "invocation_id.txt").read_text().strip()
    finding = ReviewJournal(tmp_path / "journal.jsonl").recover(rid)
    assert finding.state is RecoveryState.NOT_DISPATCHED
    assert finding.observed_kinds == ()


# ── the crash-point matrix ─────────────────────────────────────────────────
def test_crash_after_packet_persistence_permits_dispatch(tmp_path):
    _process_a(tmp_path, "after_persist")
    f = _recover(tmp_path)
    assert f.state is RecoveryState.NOT_DISPATCHED
    assert f.dispatch_permitted is True
    assert LifecycleKind.PACKET_PERSISTED.value in f.observed_kinds
    assert _reviewer_calls(tmp_path) == 0


def test_crash_after_binding_before_dispatch_permits_dispatch(tmp_path):
    _process_a(tmp_path, "after_bound")
    f = _recover(tmp_path)
    assert f.state is RecoveryState.NOT_DISPATCHED
    assert f.dispatch_permitted is True
    assert _reviewer_calls(tmp_path) == 0


def test_crash_after_dispatch_attempted_proves_reviewer_never_called(tmp_path):
    """Sound ONLY because REVIEWER_CALLED is write-ahead."""
    _process_a(tmp_path, "after_dispatch_attempted")
    f = _recover(tmp_path)
    assert f.state is RecoveryState.DISPATCH_ALREADY_OCCURRED
    assert f.reviewer_may_have_been_billed is False
    assert f.dispatch_permitted is True
    assert _reviewer_calls(tmp_path) == 0


def test_crash_between_called_and_call_is_indeterminate(tmp_path):
    """The write-ahead record exists but the call had not happened. Evidence
    cannot distinguish this from the call having happened, so it fails closed
    even though this particular case was in fact safe -- which is what makes
    the next test's guarantee possible."""
    _process_a(tmp_path, "after_reviewer_called_before_call")
    f = _recover(tmp_path)
    assert f.state is RecoveryState.RECOVERY_INDETERMINATE_FAIL_CLOSED
    assert f.reviewer_may_have_been_billed is True
    assert f.dispatch_permitted is False


def test_crash_after_reviewer_call_before_verdict_never_recalls(tmp_path):
    """The lethal window: an independent reviewer answered and the answer is
    lost. Re-calling would produce two judgements of one candidate with only
    the second recorded."""
    _process_a(tmp_path, "after_call_before_returned")
    assert _reviewer_calls(tmp_path) == 1

    f = _recover(tmp_path)
    assert f.state is RecoveryState.RECOVERY_INDETERMINATE_FAIL_CLOSED
    assert f.reviewer_may_have_been_billed is True
    assert f.dispatch_permitted is False
    assert f.verdict is None
    assert _reviewer_calls(tmp_path) == 1, "recovery must not call the reviewer"


def test_crash_after_verdict_returned_recovers_without_calling(tmp_path):
    _process_a(tmp_path, "after_returned")
    f = _recover(tmp_path)
    assert f.state is RecoveryState.VERDICT_ALREADY_RECORDED
    assert f.verdict == {"verdict": "PASS"}
    assert f.dispatch_permitted is False
    assert _reviewer_calls(tmp_path) == 1


def test_completed_review_refuses_a_duplicate_dispatch(tmp_path):
    proc = _process_a(tmp_path, "none")
    assert proc.returncode == 0
    assert _reviewer_calls(tmp_path) == 1

    f = _recover(tmp_path)
    assert f.state is RecoveryState.VERDICT_ALREADY_RECORDED
    assert f.dispatch_permitted is False
    assert _reviewer_calls(tmp_path) == 1


def test_restarting_the_same_lifecycle_does_not_rebill_the_reviewer(tmp_path):
    """Identity is deterministic, so a fresh process recomputes the SAME id and
    finds the existing verdict instead of concluding it never dispatched."""
    _process_a(tmp_path, "none")
    first = (tmp_path / "invocation_id.txt").read_text().strip()

    recomputed = review_invocation_id(
        candidate_sha="a" * 40, packet_hash="pkt_" + "1" * 32, mission_id="m1",
        task_id="t1", criterion_digest=criterion_set_digest(["C2", "C1"]),
        reviewer_identity={"provider": "p", "model": "m", "protocol": "one-shot"})
    assert recomputed == first, "identity must survive restart byte-identically"
    assert ReviewJournal(tmp_path / "journal.jsonl").recover(recomputed).state \
        is RecoveryState.VERDICT_ALREADY_RECORDED


# ── torn tails and corruption ──────────────────────────────────────────────
def test_torn_final_line_is_indeterminate_not_absence(tmp_path):
    """The most likely crash artifact. Skipping it would report the state
    BEFORE the last thing that happened."""
    _process_a(tmp_path, "after_dispatch_attempted")
    p = tmp_path / "journal.jsonl"
    p.write_text(p.read_text() + '{"kind": "Reviewer', encoding="utf-8")

    f = _recover(tmp_path)
    assert f.state is RecoveryState.RECOVERY_INDETERMINATE_FAIL_CLOSED
    assert "torn" in f.reason


def test_corruption_in_the_middle_raises_rather_than_skipping(tmp_path):
    _process_a(tmp_path, "after_returned")
    p = tmp_path / "journal.jsonl"
    lines = p.read_text().splitlines()
    lines[1] = "{not json"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(JournalError):
        read_events_strict(p)


def test_mid_journal_corruption_recovers_as_indeterminate(tmp_path):
    _process_a(tmp_path, "after_returned")
    p = tmp_path / "journal.jsonl"
    lines = p.read_text().splitlines()
    lines[1] = "{not json"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    f = _recover(tmp_path)
    assert f.state is RecoveryState.RECOVERY_INDETERMINATE_FAIL_CLOSED


# ── identity properties ────────────────────────────────────────────────────
def test_identity_contains_no_timestamp_and_is_order_free():
    kw = dict(candidate_sha="a" * 40, packet_hash="pkt_" + "1" * 32,
              mission_id="m1", task_id="t1",
              reviewer_identity={"provider": "p", "model": "m", "protocol": "x"})
    assert review_invocation_id(criterion_digest=criterion_set_digest(["A", "B"]), **kw) \
        == review_invocation_id(criterion_digest=criterion_set_digest(["B", "A"]), **kw)


@pytest.mark.parametrize("field,value", [
    ("candidate_sha", "b" * 40), ("packet_hash", "pkt_" + "2" * 32),
    ("mission_id", "m2"), ("task_id", "t2")])
def test_identity_changes_with_every_bound_fact(field, value):
    kw = dict(candidate_sha="a" * 40, packet_hash="pkt_" + "1" * 32,
              mission_id="m1", task_id="t1",
              criterion_digest=criterion_set_digest(["A"]),
              reviewer_identity={"provider": "p", "model": "m", "protocol": "x"})
    assert review_invocation_id(**kw) != review_invocation_id(**{**kw, field: value})


def test_dispatch_epoch_changes_identity_so_a_retry_is_never_silent():
    kw = dict(candidate_sha="a" * 40, packet_hash="pkt_" + "1" * 32,
              mission_id="m1", task_id="t1",
              criterion_digest=criterion_set_digest(["A"]),
              reviewer_identity={"provider": "p", "model": "m", "protocol": "x"})
    assert review_invocation_id(**kw) != review_invocation_id(**kw, dispatch_epoch=2)


# ── records without the write-ahead promise ────────────────────────────────
def test_records_lacking_the_wal_contract_recover_as_indeterminate(tmp_path):
    """Historical records -- including every one in the crashed 0C ledger --
    were written by a process that made no ordering promise, so absence of a
    REVIEWER_CALLED entry carries no information."""
    p = tmp_path / "journal.jsonl"
    p.write_text(json.dumps({
        "kind": LifecycleKind.DISPATCH_ATTEMPTED.value,
        "review_invocation_id": "rvi_legacy"}) + "\n", encoding="utf-8")

    f = ReviewJournal(p).recover("rvi_legacy")
    assert f.state is RecoveryState.RECOVERY_INDETERMINATE_FAIL_CLOSED
    assert f.reviewer_may_have_been_billed is True


def test_every_state_is_written_as_its_own_record(tmp_path):
    """Seven independently observable facts, never one summary record."""
    _process_a(tmp_path, "none")
    events, intact = read_events_strict(tmp_path / "journal.jsonl")
    assert intact
    kinds = [e["kind"] for e in events]
    assert kinds == [
        LifecycleKind.PACKET_BUILT.value, LifecycleKind.PACKET_PERSISTED.value,
        LifecycleKind.CANDIDATE_BOUND.value, LifecycleKind.DISPATCH_ATTEMPTED.value,
        LifecycleKind.REVIEWER_CALLED.value, LifecycleKind.VERDICT_RETURNED.value,
        LifecycleKind.VERDICT_PERSISTED.value]
    assert all(e["wal_contract"] == WAL_CONTRACT for e in events)
