"""The tail proof bridges a reviewed candidate to the PR head, or refuses.

Real git repositories throughout: the proof is a claim about git semantics, and
a fake ancestry view would only test the fake. Several of these cases exist
because an endpoint diff reports them as clean.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from portfolio_automation.engineer_worker.certification_tail import (
    GitAncestryView, TailPolicy, TailRefusal, classify_path,
    prove_certification_tail,
)

PKT = "docs/review_packets/61/pkt_aaa.json"
LEDGER = "docs/EW0A_REVIEW_JOURNAL.jsonl"


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(("git", *args), cwd=str(root), capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _write(root, "portfolio_automation/mod.py", "def f():\n    return 1\n")
    _write(root, "tests/test_a.py", "def test_a():\n    assert True\n")
    _write(root, "config/authority.yaml", "authority: A1\n")
    _write(root, LEDGER, '{"r": 1}\n')
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _commit(root: Path, files: dict[str, str], msg: str) -> str:
    for rel, text in files.items():
        _write(root, rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


def _prove(root: Path, cand: str, head: str):
    return prove_certification_tail(cand, head, GitAncestryView(root))


# ── TAIL 1 — a valid evidence append ───────────────────────────────────────
def test_tail1_valid_evidence_append_is_valid(tmp_path):
    root = _repo(tmp_path)
    cand = _git(root, "rev-parse", "HEAD")
    _commit(root, {PKT: '{"packet": 1}\n'}, "evidence: packet")
    head = _commit(root, {LEDGER: '{"r": 1}\n{"r": 2}\n'}, "evidence: journal append")

    proof = _prove(root, cand, head)
    assert proof.verdict == "YES", proof.details
    assert proof.refusals == ()
    assert proof.checks["CANDIDATE_IS_ANCESTOR_OF_HEAD"] == "YES"
    assert proof.checks["LEDGERS_APPEND_ONLY"] == "YES"
    assert len(proof.tail_commits) == 2


def test_empty_tail_where_candidate_equals_head_is_valid(tmp_path):
    """Reflexivity is intentional: nothing was appended, so nothing can be
    wrong. It must be a decision, not an accident."""
    root = _repo(tmp_path)
    cand = _git(root, "rev-parse", "HEAD")
    proof = _prove(root, cand, cand)
    assert proof.verdict == "YES"
    assert proof.tail_commits == ()


# ── TAIL 2/3/4 — code, tests, config may not move after review ─────────────
def test_tail2_implementation_change_in_tail_is_invalid(tmp_path):
    root = _repo(tmp_path)
    cand = _git(root, "rev-parse", "HEAD")
    _commit(root, {PKT: '{"packet": 1}\n'}, "evidence")
    head = _commit(root, {"portfolio_automation/mod.py": "def f():\n    return 2\n"},
                   "sneaky code change")

    proof = _prove(root, cand, head)
    assert proof.verdict == "NO"
    assert TailRefusal.FORBIDDEN_PATH_TOUCHED in proof.refusals


def test_tail3_test_change_in_tail_is_invalid(tmp_path):
    """Tests are part of proof semantics; they cannot change after the review
    that relied on them."""
    root = _repo(tmp_path)
    cand = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"tests/test_a.py": "def test_a():\n    assert 1\n"},
                   "test tweak")
    proof = _prove(root, cand, head)
    assert proof.verdict == "NO"
    assert TailRefusal.FORBIDDEN_PATH_TOUCHED in proof.refusals


@pytest.mark.parametrize("rel,text", [
    ("config/authority.yaml", "authority: A2\n"),
    (".agent/project_state.yaml", "phase: 0d\n"),
    (".github/workflows/ci.yml", "on: push\n"),
    (".gitattributes", "*.json diff=poison\n"),
])
def test_tail4_config_or_authority_change_in_tail_is_invalid(tmp_path, rel, text):
    root = _repo(tmp_path)
    cand = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {rel: text}, "policy change")
    proof = _prove(root, cand, head)
    assert proof.verdict == "NO"
    assert TailRefusal.FORBIDDEN_PATH_TOUCHED in proof.refusals


def test_an_unlisted_path_is_refused_even_though_it_is_not_forbidden(tmp_path):
    root = _repo(tmp_path)
    cand = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"docs/NOTES.md": "hello\n"}, "unlisted doc")
    proof = _prove(root, cand, head)
    assert proof.verdict == "NO"
    assert TailRefusal.PATH_OUTSIDE_EVIDENCE_ALLOWLIST in proof.refusals
    assert classify_path("docs/NOTES.md", TailPolicy()) == "UNKNOWN"


# ── TAIL 5 — rewritten history / non-descendant ────────────────────────────
def test_tail5_rewritten_history_is_not_a_descendant(tmp_path):
    root = _repo(tmp_path)
    cand = _git(root, "rev-parse", "HEAD")
    old_head = _commit(root, {PKT: '{"packet": 1}\n'}, "evidence")

    _git(root, "checkout", "-q", "-b", "rebased", cand)
    rewritten = _commit(root, {PKT: '{"packet": 1}\n'}, "evidence again")

    assert rewritten != old_head
    proof = _prove(root, old_head, rewritten)
    assert proof.verdict == "NO"
    assert TailRefusal.CANDIDATE_NOT_ANCESTOR in proof.refusals


def test_tail5_unknown_object_is_git_unavailable_not_a_clean_negative(tmp_path):
    """rc 128 must never be folded into rc 1. A broken repository is not proof
    that a candidate is unrelated."""
    root = _repo(tmp_path)
    head = _git(root, "rev-parse", "HEAD")
    proof = _prove(root, "d" * 40, head)
    assert proof.verdict == "NO"
    assert TailRefusal.CANDIDATE_UNRESOLVABLE in proof.refusals
    assert TailRefusal.CANDIDATE_NOT_ANCESTOR not in proof.refusals


def test_abbreviated_sha_is_refused_rather_than_resolved(tmp_path):
    """git resolves abbreviations happily, which would let a caller hand the
    proof an ambiguity it then treats as exact."""
    root = _repo(tmp_path)
    head = _git(root, "rev-parse", "HEAD")
    proof = _prove(root, head[:12], head)
    assert TailRefusal.CANDIDATE_UNRESOLVABLE in proof.refusals


def test_tail5_merge_commit_in_the_tail_is_refused(tmp_path):
    """A merge passes ancestry while its second parent smuggles in changes a
    first-parent walk never sees."""
    root = _repo(tmp_path)
    cand = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "-b", "side")
    _commit(root, {"portfolio_automation/mod.py": "def f():\n    return 3\n"}, "side")
    _git(root, "checkout", "-q", "-")
    _commit(root, {PKT: '{"packet": 1}\n'}, "evidence")
    _git(root, "merge", "-q", "--no-ff", "side", "-m", "merge")
    head = _git(root, "rev-parse", "HEAD")

    proof = _prove(root, cand, head)
    assert proof.verdict == "NO"
    assert TailRefusal.HISTORY_NOT_LINEAR in proof.refusals


# ── TAIL 6 — an immutable packet may not be rewritten ──────────────────────
def test_tail6_packet_added_then_tampered_is_refused(tmp_path):
    """The endpoint diff calls this a clean addition. Only the per-commit walk
    sees the tamper."""
    root = _repo(tmp_path)
    cand = _git(root, "rev-parse", "HEAD")
    _commit(root, {PKT: '{"packet": 1}\n'}, "evidence")
    head = _commit(root, {PKT: '{"packet": "TAMPERED"}\n'}, "rewrite the packet")

    endpoint = _git(root, "diff-tree", "-r", "--no-renames", "--name-status", cand, head)
    assert endpoint.split()[0] == "A", "endpoint diff alone reports a clean add"

    proof = _prove(root, cand, head)
    assert proof.verdict == "NO"
    assert TailRefusal.IMMUTABLE_ARTIFACT_MODIFIED in proof.refusals


def test_tail6_packet_existing_before_the_candidate_may_not_be_modified(tmp_path):
    root = _repo(tmp_path)
    _commit(root, {PKT: '{"packet": 1}\n'}, "pre-existing evidence")
    cand = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {PKT: '{"packet": 2}\n'}, "rewrite")

    proof = _prove(root, cand, head)
    assert proof.verdict == "NO"
    assert TailRefusal.IMMUTABLE_ARTIFACT_MODIFIED in proof.refusals


def test_deleted_evidence_is_refused(tmp_path):
    root = _repo(tmp_path)
    _commit(root, {PKT: '{"packet": 1}\n'}, "evidence")
    cand = _git(root, "rev-parse", "HEAD")
    _git(root, "rm", "-q", PKT)
    _git(root, "commit", "-q", "-m", "drop evidence")
    head = _git(root, "rev-parse", "HEAD")

    proof = _prove(root, cand, head)
    assert proof.verdict == "NO"
    assert TailRefusal.EVIDENCE_DELETED in proof.refusals


# ── TAIL 7 — ledgers grow, never change ────────────────────────────────────
def test_tail7_ledger_append_is_valid(tmp_path):
    root = _repo(tmp_path)
    cand = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {LEDGER: '{"r": 1}\n{"r": 2}\n'}, "append")
    assert _prove(root, cand, head).verdict == "YES"


def test_tail7_ledger_rewrite_is_refused(tmp_path):
    root = _repo(tmp_path)
    cand = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {LEDGER: '{"r": "REWRITTEN"}\n'}, "rewrite")
    proof = _prove(root, cand, head)
    assert proof.verdict == "NO"
    assert TailRefusal.LEDGER_NOT_APPEND_ONLY in proof.refusals


def test_tail7_ledger_truncation_is_refused(tmp_path):
    root = _repo(tmp_path)
    _commit(root, {LEDGER: '{"r": 1}\n{"r": 2}\n'}, "append")
    cand = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {LEDGER: '{"r": 1}\n'}, "truncate")
    proof = _prove(root, cand, head)
    assert proof.verdict == "NO"
    assert TailRefusal.LEDGER_NOT_APPEND_ONLY in proof.refusals


def test_tail7_rewrite_then_restore_is_still_refused(tmp_path):
    """Endpoint bytes are a clean append; the intermediate rewrite is only
    visible commit by commit."""
    root = _repo(tmp_path)
    cand = _git(root, "rev-parse", "HEAD")
    _commit(root, {LEDGER: '{"r": 1}\n{"r": 2}\n'}, "append")
    _commit(root, {LEDGER: '{"r": "REWRITTEN"}\n'}, "rewrite")
    head = _commit(root, {LEDGER: '{"r": 1}\n{"r": 2}\n'}, "restore")

    old = _git(root, "cat-file", "blob", f"{cand}:{LEDGER}")
    new = _git(root, "cat-file", "blob", f"{head}:{LEDGER}")
    assert new.startswith(old), "endpoint prefix check alone would pass"

    proof = _prove(root, cand, head)
    assert proof.verdict == "NO"
    assert TailRefusal.LEDGER_NOT_APPEND_ONLY in proof.refusals


def test_active_session_ledger_can_be_admitted_explicitly(tmp_path):
    """Only the ACTIVE ledger. A glob over every session file would let a tail
    append to a sealed session's history."""
    root = _repo(tmp_path)
    rel = "docs/EW0A_CRASH_RESILIENCE_SESSION_x.jsonl"
    cand = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {rel: '{"a": 1}\n'}, "session evidence")

    assert _prove(root, cand, head).verdict == "NO"
    proof = prove_certification_tail(cand, head, GitAncestryView(root),
                                     policy=TailPolicy().with_ledger(rel))
    assert proof.verdict == "YES"


def test_every_refusal_is_collected_not_just_the_first(tmp_path):
    root = _repo(tmp_path)
    cand = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"portfolio_automation/mod.py": "x = 2\n",
                          "docs/NOTES.md": "hi\n"}, "two problems")
    proof = _prove(root, cand, head)
    assert TailRefusal.FORBIDDEN_PATH_TOUCHED in proof.refusals
    assert TailRefusal.PATH_OUTSIDE_EVIDENCE_ALLOWLIST in proof.refusals
