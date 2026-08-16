"""Two bounded sessions in one ledger must project as two sessions.

Session 2 reused Session 1's identifier, and the projection carried the id and
objective as module constants while reading the FIRST SessionStarted in the
file. The GUI therefore showed one session wearing Session 1's name, objective
and start time, with Session 2's task counts silently added in. No single field
was fabricated; two episodes were merged.

Each test builds a ledger containing two real episodes and asserts the specific
separation it names.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from tools import ns0c_session as ns

REPO = Path(__file__).resolve().parents[1]

S1 = "ns0c-evgw-foundation-001"
S2 = "ns0c-revision-supersession-002"
S1_START = "2026-01-01T00:00:00+00:00"
S2_START = "2026-02-01T00:00:00+00:00"


def _ledger(tmp_path) -> Path:
    """A ledger holding two episodes, the second written under the FIRST's id —
    the exact physical situation Session 2 produced."""
    events = [
        {"kind": "SessionStarted", "session_id": S1, "mission_id": "m",
         "session_objective": "EvidenceGateway Foundation",
         "session_started_at": S1_START, "starting_main_sha": "aaa"},
        {"kind": "TaskStage", "session_id": S1, "task_id": "t1",
         "stage": "IMPLEMENTATION", "title": "admissibility"},
        {"kind": "TaskOutcome", "session_id": S1, "task_id": "t1",
         "final_status": "VERIFIED"},
        {"kind": "TaskOutcome", "session_id": S1, "task_id": "t2",
         "final_status": "VERIFIED"},
        {"kind": "SessionState", "session_id": S1, "session_state": "COMPLETE"},

        {"kind": "SessionStarted", "session_id": S1,      # reused id, as recorded
         "mission_id": "m", "session_objective": "Revision / Supersession Safety "
         "Foundation", "session_started_at": S2_START, "starting_main_sha": "bbb"},
        {"kind": "TaskStage", "session_id": S1, "task_id": "s2t1",
         "stage": "IMPLEMENTATION", "title": "revision visibility"},
        {"kind": "TaskOutcome", "session_id": S1, "task_id": "s2t1",
         "final_status": "VERIFIED"},
        {"kind": "SessionState", "session_id": S1, "session_state": "BETWEEN_TASKS"},

        {"kind": "SessionIdentityCorrection",
         "original_recorded_session_id": S1, "corrected_logical_session_id": S2,
         "applies_to_session_started_at": S2_START,
         "reason": "bounded Session 2 erroneously reused Session 1 identity"},
    ]
    docs = tmp_path / "docs"
    docs.mkdir()
    path = docs / f"NORTHSTAR_0C_SESSION_{S1}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


# ── G1 — task counts must not merge ────────────────────────────────────────
def test_g1_two_sessions_in_one_ledger_do_not_merge_task_counts(tmp_path):
    _ledger(tmp_path)

    one = ns.session_projection(repo_root=tmp_path, session_id=S1)
    two = ns.session_projection(repo_root=tmp_path, session_id=S2)

    assert one["tasks_attempted"] == 2 and one["tasks_verified"] == 2
    assert two["tasks_attempted"] == 1 and two["tasks_verified"] == 1
    assert one["tasks_verified"] + two["tasks_verified"] == 3, (
        "three outcomes exist in the file; neither session may claim all three")


def test_g1_session2_does_not_inherit_session1_current_task(tmp_path):
    _ledger(tmp_path)
    assert ns.session_projection(repo_root=tmp_path, session_id=S1)["current_task_id"] == "t1"
    assert ns.session_projection(repo_root=tmp_path, session_id=S2)["current_task_id"] == "s2t1"


# ── G2 — selecting session 2 returns session 2 ─────────────────────────────
def test_g2_selecting_session_2_returns_its_own_objective_and_start(tmp_path):
    _ledger(tmp_path)
    two = ns.session_projection(repo_root=tmp_path, session_id=S2)

    assert two["session_objective"] == "Revision / Supersession Safety Foundation"
    assert two["session_started_at"] == S2_START
    assert two["starting_main_sha"] == "bbb"
    assert two["session_state"] == "BETWEEN_TASKS"


def test_g2_default_selection_is_the_latest_episode_not_the_first(tmp_path):
    """The original defect was `next(SessionStarted)` — always the first."""
    _ledger(tmp_path)
    default = ns.session_projection(repo_root=tmp_path)
    assert default["session_id"] == S2
    assert default["session_objective"] == "Revision / Supersession Safety Foundation"


def test_g2_objective_comes_from_records_not_a_module_constant(tmp_path):
    """No constant may supply an objective the evidence does not contain."""
    source = (REPO / "tools" / "ns0c_session.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    constants = {t.id for node in ast.walk(tree)
                 if isinstance(node, ast.Assign)
                 for t in node.targets if isinstance(t, ast.Name)}
    assert "SESSION_OBJECTIVE" not in constants

    _ledger(tmp_path)
    for sid, objective in ((S1, "EvidenceGateway Foundation"),
                           (S2, "Revision / Supersession Safety Foundation")):
        assert ns.session_projection(repo_root=tmp_path,
                                     session_id=sid)["session_objective"] == objective


# ── G3 — session 1 remains independently reconstructible ───────────────────
def test_g3_session_1_is_still_fully_reconstructible(tmp_path):
    _ledger(tmp_path)
    one = ns.session_projection(repo_root=tmp_path, session_id=S1)

    assert one["session_id"] == S1
    assert one["session_objective"] == "EvidenceGateway Foundation"
    assert one["session_started_at"] == S1_START
    assert one["starting_main_sha"] == "aaa"
    assert one["session_state"] == "COMPLETE"


def test_g3_the_correction_does_not_rewrite_the_recorded_identity(tmp_path):
    """Session 2's records still say what they said. The correction is a
    forward-linking claim, not an edit — and the projection reports both."""
    path = _ledger(tmp_path)
    raw = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    starts = [e for e in raw if e["kind"] == "SessionStarted"]
    assert all(e["session_id"] == S1 for e in starts), (
        "history must remain byte-identical; only an appended record may correct it")

    two = ns.session_projection(repo_root=tmp_path, session_id=S2)
    assert two["recorded_session_id"] == S1
    assert two["identity_corrected"] is True


def test_g3_an_uncorrected_ledger_keeps_its_recorded_identity(tmp_path):
    """Without a correction record, nothing is invented: the episode keeps the
    id it was written with, even if that means two episodes share one id."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / f"NORTHSTAR_0C_SESSION_{S1}.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"kind": "SessionStarted", "session_id": S1, "session_objective": "one",
         "session_started_at": S1_START},
        {"kind": "SessionStarted", "session_id": S1, "session_objective": "two",
         "session_started_at": S2_START},
    ]) + "\n", encoding="utf-8")

    sessions = ns.list_sessions(repo_root=tmp_path)
    assert [s["session_id"] for s in sessions] == [S1, S1]
    assert all(s["identity_corrected"] is False for s in sessions)


# ── G4 — unknown session fails closed ──────────────────────────────────────
def test_g4_unknown_session_id_fails_closed(tmp_path):
    _ledger(tmp_path)
    out = ns.session_projection(repo_root=tmp_path, session_id="ns0c-does-not-exist")

    assert out["session_state"] == ns.NO_SESSION
    assert out["session_objective"] == ns.NO_SESSION
    assert out["tasks_attempted"] == 0 and out["tasks_verified"] == 0
    assert out["current_task_id"] is None
    assert set(out["known_sessions"]) == {S1, S2}, (
        "an unknown id must not silently resolve to some other session")


def test_g4_empty_repository_projects_no_session(tmp_path):
    (tmp_path / "docs").mkdir()
    out = ns.session_projection(repo_root=tmp_path, session_id=S1)
    assert out["session_state"] == ns.NO_SESSION
    assert out["tasks_verified"] == 0


def test_g4_events_before_any_session_started_are_not_attributed(tmp_path):
    """An orphan record belongs to no episode and must not be adopted by one."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / f"NORTHSTAR_0C_SESSION_{S1}.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"kind": "TaskOutcome", "task_id": "orphan", "final_status": "VERIFIED"},
        {"kind": "SessionStarted", "session_id": S1, "session_objective": "one",
         "session_started_at": S1_START},
    ]) + "\n", encoding="utf-8")

    out = ns.session_projection(repo_root=tmp_path, session_id=S1)
    assert out["tasks_verified"] == 0, "the orphan outcome predates the episode"


# ── G5 — no cross-session inference of VERIFIED ────────────────────────────
def test_g5_gui_does_not_infer_verified_from_another_sessions_pass(tmp_path):
    """Session 2's episode carries no TaskOutcome at all; Session 1's is full of
    them. Session 2 must read as zero verified, not inherit its neighbour's."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / f"NORTHSTAR_0C_SESSION_{S1}.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"kind": "SessionStarted", "session_id": S1, "session_objective": "one",
         "session_started_at": S1_START},
        {"kind": "TaskOutcome", "session_id": S1, "task_id": "t1",
         "final_status": "VERIFIED"},
        {"kind": "GPTVerdict", "session_id": S1, "task_id": "t1",
         "gpt_verdict": "PASS", "verified": True},
        {"kind": "SessionStarted", "session_id": S1, "session_objective": "two",
         "session_started_at": S2_START},
        {"kind": "TaskStage", "session_id": S1, "task_id": "s2t1",
         "stage": "IMPLEMENTATION"},
        {"kind": "SessionIdentityCorrection", "original_recorded_session_id": S1,
         "corrected_logical_session_id": S2,
         "applies_to_session_started_at": S2_START, "reason": "reused identity"},
    ]) + "\n", encoding="utf-8")

    two = ns.session_projection(repo_root=tmp_path, session_id=S2)
    assert two["tasks_verified"] == 0
    assert two["tasks_attempted"] == 0
    assert ns.session_projection(repo_root=tmp_path, session_id=S1)["tasks_verified"] == 1


def test_g5_verified_requires_a_recorded_final_status(tmp_path):
    """A PASS verdict alone is not VERIFIED; only a recorded TaskOutcome is."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / f"NORTHSTAR_0C_SESSION_{S1}.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"kind": "SessionStarted", "session_id": S1, "session_objective": "one",
         "session_started_at": S1_START},
        {"kind": "GPTVerdict", "session_id": S1, "task_id": "t1",
         "gpt_verdict": "PASS", "verified": True},
        {"kind": "TaskOutcome", "session_id": S1, "task_id": "t1",
         "final_status": "REPAIR_REQUIRED"},
    ]) + "\n", encoding="utf-8")

    out = ns.session_projection(repo_root=tmp_path, session_id=S1)
    assert out["tasks_verified"] == 0
    assert out["tasks_attempted"] == 1


# ── G6 — the GUI path introduces no authoritative write ────────────────────
def test_g6_no_authoritative_write_path_in_the_projection():
    source = (REPO / "tools" / "ns0c_session.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    reader_names = {"session_projection", "load_episodes", "list_sessions",
                    "split_episodes", "read_events", "_corrections", "_no_session"}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in reader_names:
            dumped = ast.dump(node)
            for writer in ("open(", "write", "mkdir", "record("):
                assert writer not in dumped, f"{node.name} must not {writer}"


def test_g6_readmodel_session_surface_is_read_only():
    from portfolio_automation.engineer_worker import ew0a_readmodels
    source = Path(ew0a_readmodels.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_build_active_session":
            dumped = ast.dump(node)
            for writer in ("open(", "write", "record("):
                assert writer not in dumped


def test_g6_absent_ledger_is_pending_backend_not_a_fabrication(tmp_path):
    from portfolio_automation.engineer_worker.ew0a_readmodels import _build_active_session
    assert _build_active_session(tmp_path) == "PENDING_BACKEND"


# ── the real repository ────────────────────────────────────────────────────
def test_real_ledger_projects_both_bounded_sessions_distinctly():
    sessions = {s["session_id"]: s for s in ns.list_sessions(repo_root=REPO)}
    assert ns.SESSION1_ID in sessions and ns.SESSION2_ID in sessions
    assert sessions[ns.SESSION1_ID]["session_objective"] == "EvidenceGateway Foundation"
    assert (sessions[ns.SESSION2_ID]["session_objective"]
            == "Revision / Supersession Safety Foundation")
    assert (sessions[ns.SESSION1_ID]["session_started_at"]
            != sessions[ns.SESSION2_ID]["session_started_at"])
