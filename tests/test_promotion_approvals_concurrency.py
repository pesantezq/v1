"""Fix WS11 — concurrency protection around ``record_approval``'s read-modify-write.

Defect fixed (confirmed by experiment, ``.superpowers/audit/ws-10-11-12-persistence.md``
WS11.2): ``record_approval`` did a whole-document read-modify-write with no lock
and no CAS. Two threads approving DIFFERENT proposals, forced to interleave with
a ``threading.Barrier`` so both complete their read of the existing file before
either writes, both returned ``ok: True`` — yet only the LAST writer's approval
survived on disk. No exception, no log signal, no error surfaced anywhere.

The fix wraps the unreadable-check + read-modify-write in an ``fcntl.flock``
advisory lock on a dedicated ``.lock`` sidecar file (``_approvals_write_lock``,
promotion_approvals.py), serializing concurrent writers so no ``ok: True``
result can ever be lost.

These tests exercise REAL concurrency (``threading.Thread`` + ``threading.Barrier``),
not a simulated/sequential stand-in for the race.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from portfolio_automation.sim_governance import promotion_approvals as PA

_NOW = "2026-07-28T18:30:00+00:00"


def _outputs(tmp_path: Path) -> str:
    d = tmp_path / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _appr_dir(base_dir: str) -> Path:
    d = Path(base_dir) / "promotion_approvals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log(base_dir: str) -> dict:
    return json.loads((Path(base_dir) / "promotion_approvals" / "approved_proposals.json")
                      .read_text(encoding="utf-8"))


def test_two_concurrent_approvals_for_different_proposals_both_survive(tmp_path):
    """The regression test for the confirmed WS11.2 lost-update defect.

    Two threads call record_approval for DIFFERENT proposal_ids against the
    same base_dir. This reproduces the auditor's own technique (a barrier
    placed INSIDE the read, between "load the existing document" and "append
    + write it back") rather than relying on incidental OS thread-scheduling
    luck: ``PA._load_raw`` is wrapped so both callers rendezvous at a 2-party
    barrier immediately after reading, guaranteeing neither has written yet
    when the other reads — the exact interleaving the audit's experiment
    forced by hand.

    Without the fix this deterministically loses one approval every run
    (verified by temporarily reverting the lock and re-running — see the
    implementation report for the before/after transcript: 12-way variant of
    this same race collapsed 12 approvals down to 1 survivor on the unpatched
    code). With the fix, ``record_approval`` acquires the write lock BEFORE
    calling ``_load_raw``, so the second caller cannot even enter ``_load_raw``
    until the first has finished its whole read-modify-write and released the
    lock — the barrier simply times out for whichever call arrives second,
    which is the correct, expected shape of "the lock serialized us" and is
    treated as success, not failure.
    """
    base = _outputs(tmp_path)
    read_barrier = threading.Barrier(2)
    orig_load_raw = PA._load_raw

    def _patched_load_raw(base_dir):
        data = orig_load_raw(base_dir)
        try:
            read_barrier.wait(timeout=1)
        except threading.BrokenBarrierError:
            # Expected once the fix serializes access: the second caller never
            # reaches this point concurrently with the first, so it cannot
            # rendezvous — that IS the lock working, not a test failure.
            pass
        return data

    results: dict[str, dict] = {}

    def _worker(pid: str):
        results[pid] = PA.record_approval(
            pid, "approve", "pesantez", _NOW, base_dir=base)

    PA._load_raw = _patched_load_raw
    try:
        t1 = threading.Thread(target=_worker, args=("prop_AAA",))
        t2 = threading.Thread(target=_worker, args=("prop_BBB",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
    finally:
        PA._load_raw = orig_load_raw

    assert results["prop_AAA"]["ok"] is True, results["prop_AAA"]
    assert results["prop_BBB"]["ok"] is True, results["prop_BBB"]

    data = _log(base)
    ids = {r["proposal_id"] for r in data["approvals"]}
    assert ids == {"prop_AAA", "prop_BBB"}, (
        f"lost-update: expected both approvals on disk, found {ids}")


def test_many_concurrent_approvals_all_survive(tmp_path):
    """Higher-contention variant: N threads, N distinct proposals, one barrier."""
    base = _outputs(tmp_path)
    n = 12
    pids = [f"prop_{i:02d}" for i in range(n)]
    barrier = threading.Barrier(n)
    results: dict[str, dict] = {}
    lock = threading.Lock()

    def _worker(pid: str):
        barrier.wait()
        res = PA.record_approval(pid, "approve", "pesantez", _NOW, base_dir=base)
        with lock:
            results[pid] = res

    threads = [threading.Thread(target=_worker, args=(pid,)) for pid in pids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    for pid in pids:
        assert results[pid]["ok"] is True, results[pid]

    data = _log(base)
    ids = {r["proposal_id"] for r in data["approvals"]}
    assert ids == set(pids), f"lost update(s): expected {len(pids)} survivors, found {len(ids)}: {ids}"
    assert len(data["approvals"]) == n, "no duplicate/extra records expected"


def test_approval_racing_a_revocation_both_persist(tmp_path):
    """An approval (approved_proposals.json) racing a revocation
    (production_revocations.jsonl) touches two independent files/mechanisms —
    both must succeed and both must be durably recorded, with no cross-file
    interference introduced by the new lock.
    """
    base = _outputs(tmp_path)
    barrier = threading.Barrier(2)
    results: dict[str, dict] = {}

    def _approve():
        barrier.wait()
        results["approve"] = PA.record_approval(
            "prop_CCC", "approve", "pesantez", _NOW, base_dir=base)

    def _revoke():
        barrier.wait()
        results["revoke"] = PA.revoke_application(
            "cand_other", "pesantez", _NOW, base_dir=base)

    t1 = threading.Thread(target=_approve)
    t2 = threading.Thread(target=_revoke)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert results["approve"]["ok"] is True, results["approve"]
    assert results["revoke"]["ok"] is True, results["revoke"]

    data = _log(base)
    assert {r["proposal_id"] for r in data["approvals"]} == {"prop_CCC"}
    assert PA.revoked_ids(base) == {"cand_other"}


def test_duplicate_submission_remains_idempotent_in_effect(tmp_path):
    """A double-click/replayed POST appends a second identical-decision record;
    the effective fold (last-wins) must still resolve to exactly one outcome —
    verified, not rebuilt, per the audit's 11.4 finding that this was already
    harmless via the existing fold.
    """
    base = _outputs(tmp_path)
    r1 = PA.record_approval("prop_dup", "approve", "pesantez", _NOW, base_dir=base)
    r2 = PA.record_approval("prop_dup", "approve", "pesantez", _NOW, base_dir=base)
    assert r1["ok"] is True and r2["ok"] is True

    data = _log(base)
    assert len(data["approvals"]) == 2, "duplicate submission still appends (benign)"
    assert PA.effective_approvals(base) == {"prop_dup": "approve"}
    assert PA.approved_proposal_ids(base) == {"prop_dup"}


def test_duplicate_concurrent_submissions_for_the_same_proposal_are_idempotent(tmp_path):
    """Two threads racing to approve the SAME proposal_id: no data loss (both
    records land, since duplicate submission was never meant to be deduped at
    the write layer) and the effective fold is still a single, unambiguous
    'approve' outcome.
    """
    base = _outputs(tmp_path)
    barrier = threading.Barrier(2)
    results: dict[str, dict] = {}
    lock = threading.Lock()

    def _worker(tag: str):
        barrier.wait()
        res = PA.record_approval("prop_race_dup", "approve", "pesantez", _NOW, base_dir=base)
        with lock:
            results[tag] = res

    t1 = threading.Thread(target=_worker, args=("a",))
    t2 = threading.Thread(target=_worker, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert results["a"]["ok"] is True and results["b"]["ok"] is True
    data = _log(base)
    assert len(data["approvals"]) == 2
    assert PA.effective_approvals(base) == {"prop_race_dup": "approve"}


def test_unreadable_log_refusal_still_fires_under_concurrency(tmp_path):
    """A corrupt approvals log must still refuse to write, even when a second
    concurrent caller is racing it — the lock serializes access, but the
    fail-closed guard (checked INSIDE the lock) must still fire for every
    caller, not just the first.
    """
    base = _outputs(tmp_path)
    d = _appr_dir(base)
    corrupt_bytes = b'{"approvals": [invalid json truncated'
    (d / "approved_proposals.json").write_bytes(corrupt_bytes)

    barrier = threading.Barrier(2)
    results: dict[str, dict] = {}
    lock = threading.Lock()

    def _worker(tag: str):
        barrier.wait()
        res = PA.record_approval("prop_x", "approve", "pesantez", _NOW, base_dir=base)
        with lock:
            results[tag] = res

    t1 = threading.Thread(target=_worker, args=("a",))
    t2 = threading.Thread(target=_worker, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    for tag in ("a", "b"):
        assert results[tag]["ok"] is False, results[tag]
        assert "unreadable" in results[tag]["reason"] or "unparseable" in results[tag]["reason"]
    # Corrupt file must be left byte-for-byte unchanged.
    assert (d / "approved_proposals.json").read_bytes() == corrupt_bytes
