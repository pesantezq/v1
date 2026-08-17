"""Restart behaviour through the REAL runtime path, not a standalone driver.

The component-level restart tests drive the journal directly, so they prove the
machinery works without proving the operating loop uses it. These go through
``run_task``. PROCESS A dies with ``os._exit`` at a named lifecycle record;
PROCESS B receives only the directory and re-enters ``run_task``.

The supervisor writes a call log and fsyncs it BEFORE returning, because every
crash point after the call is downstream of it -- a buffered log would
under-count and every "never called" assertion would be unfalsifiable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from portfolio_automation.engineer_worker.review_journal import (
    LifecycleKind, RecoveryState, ReviewJournal, read_events_strict,
)
from portfolio_automation.engineer_worker.review_packet_store import PacketStore

REPO = Path(__file__).resolve().parents[1]

DRIVER = '''
import json, os, sys
sys.path.insert(0, {repo!r})
from pathlib import Path

root = Path(sys.argv[1]); die_at = sys.argv[2]; phase = sys.argv[3]

from portfolio_automation.engineer_worker import review_journal as RJ
from portfolio_automation.engineer_worker.durable_certification import ReviewContext
from portfolio_automation.engineer_worker.ew0a import (
    AttemptEvidence, EngineeringTaskV0, Executor, RiskClass)
from portfolio_automation.engineer_worker.ew0a_authority import (
    EngineerAuthorityLevel as Lvl)
from portfolio_automation.engineer_worker.ew0a_loop import RuntimePolicy, run_task
from portfolio_automation.engineer_worker.gpt_supervisor import (
    SupervisorDecision, SupervisorVerdict)
from portfolio_automation.engineer_worker.review_candidate import HeadResolution

HEAD = "a" * 40

class Repo:
    def head_sha(self):
        moved = (root / "head_moved").exists()
        return ("b" * 40) if moved else HEAD
    def file_at(self, sha, path):
        return None

class Binding:
    def __init__(self):
        self.head_at_binding = HEAD
        self.repo = Repo()
        self.refusals = ()
        self.checks = {{"HEAD_UNCHANGED_AT_DISPATCH": "PENDING"}}
    @property
    def ok(self):
        return not self.refusals
    def to_dict(self):
        return {{"candidate_bound": "YES", "git_head_at_binding": self.head_at_binding,
                 "checks": dict(self.checks), "refusals": []}}
    def resolve_head_terminal(self):
        now = self.repo.head_sha()
        if now == self.head_at_binding:
            self.checks["HEAD_UNCHANGED_AT_DISPATCH"] = "YES"
            return self, HeadResolution("YES", HEAD, now, "UNCHANGED")
        self.checks["HEAD_UNCHANGED_AT_DISPATCH"] = "NO"
        return self, HeadResolution("NO", HEAD, now, "MOVED")

# Crash points are named by the DURABLE RECORD they follow, so they cannot drift
# when the wiring is refactored. The real append runs first, so ordering is
# unchanged; os._exit skips atexit and buffered flushes, which sys.exit does not.
_real_append = RJ.ReviewJournal.append
def _hooked(self, kind, **kw):
    rec = _real_append(self, kind, **kw)
    if kind.value == die_at:
        os._exit(9)
    return rec
RJ.ReviewJournal.append = _hooked

def supervisor(packet):
    with open(root / "reviewer_calls.log", "a") as fh:
        fh.write(json.dumps({{"phase": phase}}) + chr(10))
        fh.flush(); os.fsync(fh.fileno())
    if die_at == "AFTER_SUPERVISOR_RETURN":
        os._exit(9)
    return SupervisorDecision(verdict=SupervisorVerdict.PASS, reasons=["ok"])

ctx = ReviewContext.open(root, mission_id="m1", session_id="s1",
                         reviewer_identity={{"model": "stub"}},
                         repo=Repo(), candidate_binding=Binding())

task = EngineeringTaskV0(task_id="t1", title="t", goal="g",
    risk_class=RiskClass.E1_ROUTINE, executor=Executor.ENGINEER, mission_id="m1",
    allowed_paths=["tests/"], allowed_tests=["tests/tx.py"],
    acceptance_criteria=["it holds"])

def engineer_fn(t, n):
    return AttemptEvidence(attempt_id="a1", executor=Executor.ENGINEER,
        worker_claim="done", changed_paths=["tests/tx.py"],
        tests_run=["tests/tx.py"], test_results={{"tests/tx.py": "PASS"}},
        py_compile_ok=True, canonical_repo_touched=False)

res = run_task(task, Lvl.A1_ASSISTED_ENGINEERING, RuntimePolicy(mission_id="m1"),
               engineer_fn, lambda t, v: engineer_fn(t, 9), supervisor,
               lambda: "2026-01-01T00:00:00+00:00", lambda: "v-" + phase,
               certification=ctx)
(root / ("result_" + phase + ".json")).write_text(json.dumps({{
    "final_status": res.final_status, "verdict": res.verdict}}))
os._exit(0)
'''


def _run(tmp_path: Path, die_at: str, phase: str) -> subprocess.CompletedProcess:
    driver = tmp_path / f"driver_{phase}.py"
    driver.write_text(DRIVER.format(repo=str(REPO)), encoding="utf-8")
    return subprocess.run([sys.executable, str(driver), str(tmp_path), die_at, phase],
                          capture_output=True, text=True)


def _calls(tmp_path: Path) -> int:
    log = tmp_path / "reviewer_calls.log"
    return 0 if not log.exists() else len(
        [ln for ln in log.read_text().splitlines() if ln.strip()])


def _result(tmp_path: Path, phase: str):
    p = tmp_path / f"result_{phase}.json"
    return json.loads(p.read_text()) if p.exists() else None


def _journal(tmp_path: Path):
    return ReviewJournal(tmp_path / "docs" / "EW0A_REVIEW_JOURNAL.jsonl")


def _rid(tmp_path: Path) -> str:
    events, _ = read_events_strict(_journal(tmp_path).path)
    return events[0]["review_invocation_id"]


# ── tripwire: the crash must actually happen ───────────────────────────────
@pytest.mark.parametrize("die_at", [
    LifecycleKind.PACKET_PERSISTED.value, LifecycleKind.CANDIDATE_BOUND.value,
    LifecycleKind.DISPATCH_ATTEMPTED.value, LifecycleKind.REVIEWER_CALLED.value,
    "AFTER_SUPERVISOR_RETURN", LifecycleKind.VERDICT_RETURNED.value,
])
def test_process_a_really_dies_at_each_runtime_crash_point(tmp_path, die_at):
    """Without this the whole module could pass vacuously."""
    proc = _run(tmp_path, die_at, "A")
    assert proc.returncode == 9, proc.stderr[-2000:]


# ── A. crash after persistence, before the call ────────────────────────────
def test_crash_after_persistence_leaves_the_reviewer_provably_uncalled(tmp_path):
    _run(tmp_path, LifecycleKind.PACKET_PERSISTED.value, "A")
    assert _calls(tmp_path) == 0

    events, intact = read_events_strict(_journal(tmp_path).path)
    assert intact
    kinds = [e["kind"] for e in events]
    assert LifecycleKind.PACKET_PERSISTED.value in kinds
    assert LifecycleKind.REVIEWER_CALLED.value not in kinds


def test_restart_recovers_the_exact_persisted_bytes(tmp_path):
    _run(tmp_path, LifecycleKind.PACKET_PERSISTED.value, "A")
    events, _ = read_events_strict(_journal(tmp_path).path)
    phash = next(e["packet_hash"] for e in events
                 if e["kind"] == LifecycleKind.PACKET_PERSISTED.value)

    verified = PacketStore(repo_root=tmp_path).verify(phash)
    assert verified.ok
    assert verified.refusals == ()


def test_restart_after_persistence_dispatches_exactly_once_in_total(tmp_path):
    _run(tmp_path, LifecycleKind.PACKET_PERSISTED.value, "A")
    proc = _run(tmp_path, "NONE", "B")

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _calls(tmp_path) == 1, "one call across BOTH processes"
    assert _result(tmp_path, "B")["final_status"] == "VERIFIED"


# ── B. the lethal window ───────────────────────────────────────────────────
def test_crash_after_write_ahead_before_the_call_is_indeterminate(tmp_path):
    """Evidence cannot distinguish this from the call having happened, so it
    fails closed even though this case was in fact safe."""
    _run(tmp_path, LifecycleKind.REVIEWER_CALLED.value, "A")
    assert _calls(tmp_path) == 0

    finding = _journal(tmp_path).recover(_rid(tmp_path),
                                         store=PacketStore(repo_root=tmp_path))
    assert finding.state is RecoveryState.RECOVERY_INDETERMINATE_FAIL_CLOSED
    assert finding.dispatch_permitted is False


def test_crash_after_the_call_never_produces_a_second_call(tmp_path):
    """An independent reviewer answered and the answer is lost. Calling again
    would give two judgements of one candidate with only the second recorded."""
    _run(tmp_path, "AFTER_SUPERVISOR_RETURN", "A")
    assert _calls(tmp_path) == 1

    proc = _run(tmp_path, "NONE", "B")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _calls(tmp_path) == 1, "the reviewer must not be re-billed"
    assert _result(tmp_path, "B")["final_status"] != "VERIFIED"


def test_the_repair_loop_cannot_multiply_reviewer_calls_after_indeterminacy(tmp_path):
    """The refusal is terminal inside certify_attempt, so the bounded repair
    loop cannot convert one indeterminate into several fresh calls."""
    _run(tmp_path, "AFTER_SUPERVISOR_RETURN", "A")
    _run(tmp_path, "NONE", "B")
    _run(tmp_path, "NONE", "C")
    assert _calls(tmp_path) == 1


# ── C. crash after the verdict ─────────────────────────────────────────────
def test_crash_after_verdict_returned_reconstructs_without_recalling(tmp_path):
    _run(tmp_path, LifecycleKind.VERDICT_RETURNED.value, "A")
    assert _calls(tmp_path) == 1

    proc = _run(tmp_path, "NONE", "B")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _calls(tmp_path) == 1
    assert _result(tmp_path, "B")["final_status"] == "VERIFIED"


# ── D. missing / corrupt payload ───────────────────────────────────────────
def test_a_deleted_packet_makes_verified_impossible(tmp_path):
    _run(tmp_path, LifecycleKind.VERDICT_RETURNED.value, "A")
    events, _ = read_events_strict(_journal(tmp_path).path)
    phash = next(e["packet_hash"] for e in events
                 if e["kind"] == LifecycleKind.PACKET_PERSISTED.value)
    blob = PacketStore(repo_root=tmp_path).path_for(phash)
    os.chmod(blob, 0o644)
    blob.unlink()

    _run(tmp_path, "NONE", "B")
    assert _result(tmp_path, "B")["final_status"] != "VERIFIED"


def test_a_corrupt_packet_makes_verified_impossible(tmp_path):
    _run(tmp_path, LifecycleKind.VERDICT_RETURNED.value, "A")
    events, _ = read_events_strict(_journal(tmp_path).path)
    phash = next(e["packet_hash"] for e in events
                 if e["kind"] == LifecycleKind.PACKET_PERSISTED.value)
    blob = PacketStore(repo_root=tmp_path).path_for(phash)
    os.chmod(blob, 0o644)
    original = blob.read_bytes()
    blob.write_bytes(original.replace(b"done", b"DONE"))

    _run(tmp_path, "NONE", "B")
    assert _result(tmp_path, "B")["final_status"] != "VERIFIED"
    assert blob.read_bytes() != original, "and it was not silently repaired"


def test_a_forged_verdict_with_no_packet_cannot_certify(tmp_path):
    """The journal is a plain text file. A verdict it names must be backed by a
    packet that still verifies, or it is not evidence."""
    _run(tmp_path, LifecycleKind.PACKET_PERSISTED.value, "A")
    j = _journal(tmp_path)
    j.append(LifecycleKind.VERDICT_PERSISTED,
             review_invocation_id=_rid(tmp_path),
             packet_hash="pkt_" + "0" * 32,
             verdict={"verdict": "PASS", "reasons": ["forged"]})

    finding = j.recover(_rid(tmp_path), store=PacketStore(repo_root=tmp_path))
    assert finding.state is RecoveryState.RECOVERY_INDETERMINATE_FAIL_CLOSED
    assert finding.verdict is None


# ── E. terminal HEAD = NO ──────────────────────────────────────────────────
def test_head_moved_before_dispatch_never_calls_the_supervisor(tmp_path):
    (tmp_path / "head_moved").write_text("1")
    proc = _run(tmp_path, "NONE", "A")

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _calls(tmp_path) == 0
    assert _result(tmp_path, "A")["final_status"] != "VERIFIED"

    events, _ = read_events_strict(_journal(tmp_path).path)
    kinds = [e["kind"] for e in events]
    assert LifecycleKind.REVIEWER_CALLED.value not in kinds
    assert LifecycleKind.DISPATCH_REFUSED.value in kinds


def test_no_record_naming_an_invocation_carries_a_pending_head(tmp_path):
    _run(tmp_path, "NONE", "A")
    events, _ = read_events_strict(_journal(tmp_path).path)
    for e in events:
        assert e.get("head_unchanged_at_dispatch") != "PENDING"
