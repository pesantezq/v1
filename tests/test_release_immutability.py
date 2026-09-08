"""The production release-immutability contract.

Twelve runtime-generated artifacts were tracked in git while production wrote
them on every run, so ordinary operation left the deployed checkout permanently
dirty and `production_code_sha == approved_release_sha AND no tracked drift`
was unreachable. These tests lock the repair and the surrounding deployment
contract.

The sharp case was `outputs/performance/signal_outcomes.csv`: simultaneously
VS-001's content-addressed frozen evidence and a file production rewrote every
run. It was resolved by moving the WRITER, not the evidence — VS-001's
EVIDENCE_REL, evidence bytes, evidence SHA and freeze digest are all unchanged,
and the live producer now writes `outputs/runtime/performance/`.

Nothing here needs the VPS, network, root, systemd, cron, or secrets. The git
queries run against this repository; everything else is fixture-driven.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from portfolio_automation import signal_outcomes_paths as SOP
from portfolio_automation.backup import recovery_set as RS
from portfolio_automation.release import contracts as C
from portfolio_automation.release import pointer as PT
from portfolio_automation.release import preflight as PF
from portfolio_automation.release import scheduler as S

REPO = Path(__file__).resolve().parent.parent

#: VS-001 identity as registered on durable main. Hard-coded on purpose: a test
#: that recomputes both sides of an equality proves only self-consistency.
VS001_EVIDENCE_SHA256 = "960f7f425d0c74067210cde8c5d489c1e8f0a535568cdec05178185816f955b7"
VS001_FREEZE_DIGEST = "vsfreeze_f1c6ad413651834dff1dec548394a2d1"
VS001_EVIDENCE_BYTES = 154256


def _git(*args: str) -> str:
    return subprocess.run(("git", "-C", str(REPO)) + args,
                          capture_output=True, text=True, check=True).stdout


def _tracked_under(prefix: str) -> list[str]:
    return [ln for ln in _git("ls-files", "--", prefix).splitlines() if ln.strip()]


# ===========================================================================
# 1-3. VS-001 frozen experiment identity is bit-for-bit unchanged
# ===========================================================================

def test_1_vs001_frozen_evidence_sha_unchanged():
    """The evidence bytes are content-addressed into a completed experiment."""
    from portfolio_automation.vertical_slice import preregistration as PRE

    evidence = REPO / PRE.EVIDENCE_REL
    assert evidence.exists(), "VS-001 frozen evidence must remain in the repository"
    assert evidence.stat().st_size == VS001_EVIDENCE_BYTES
    assert PRE.evidence_digest(REPO) == VS001_EVIDENCE_SHA256, (
        "VS-001 frozen evidence bytes changed — a completed experiment's "
        "identity is no longer reproducible"
    )


def test_1b_evidence_rel_still_names_the_original_path():
    """Re-aiming the freeze pointer was explicitly not authorized."""
    from portfolio_automation.vertical_slice import feasibility as FEAS
    from portfolio_automation.vertical_slice import preregistration as PRE

    assert PRE.EVIDENCE_REL == "outputs/performance/signal_outcomes.csv"
    # VS-002 feasibility is historical evidence for why VS-002 was blocked, so
    # it reads the same frozen bytes to keep VS-002_blocked.json reproducible.
    assert FEAS.EVIDENCE_REL == PRE.EVIDENCE_REL


def test_2_vs001_freeze_digest_unchanged():
    from portfolio_automation.vertical_slice import preregistration as PRE

    assert PRE.preregistration_digest(REPO) == VS001_FREEZE_DIGEST


def test_3_vs001_reproduces_against_the_registered_artifact():
    from portfolio_automation.vertical_slice import preregistration as PRE

    registered = json.loads(
        (REPO / "evals/vertical_slice/VS-001_preregistration.json").read_text(
            encoding="utf-8"))
    assert registered["evidence_sha256"] == VS001_EVIDENCE_SHA256
    assert registered["freeze_digest"] == VS001_FREEZE_DIGEST
    ok, problems = PRE.verify_freeze(REPO, registered)
    assert ok, f"VS-001 freeze no longer verifies: {problems}"
    assert PRE.preregistration_content(REPO) == registered["preregistration"]


def test_3b_frozen_evidence_is_tracked_and_classified_as_evidence():
    rel = "outputs/performance/signal_outcomes.csv"
    assert _tracked_under(rel) == [rel], "frozen evidence must stay tracked"
    assert C.classify_path(rel) == C.IMMUTABLE_EXPERIMENT_EVIDENCE
    assert not C.is_runtime_mutable(rel), "production must never write the evidence"
    assert rel in C.IMMUTABLE_EVIDENCE_ARTIFACTS
    assert C.IMMUTABLE_EVIDENCE_ARTIFACTS[rel].strip(), "needs a recorded reason"


# ===========================================================================
# 4. The runtime producer cannot touch the frozen evidence
# ===========================================================================

def test_4_producer_writes_runtime_path_and_leaves_evidence_untouched(tmp_path):
    """Behavioural proof, not a path assertion.

    Runs the real producer against a scratch tree containing a sentinel at the
    frozen-evidence path, and requires the sentinel to survive byte-for-byte
    while the runtime path is created.
    """
    from watchlist_scanner.performance_feedback import (
        generate_signal_performance_reports,
    )

    perf = tmp_path / "outputs" / "performance"
    perf.mkdir(parents=True)
    frozen = perf / "signal_outcomes.csv"
    sentinel = "FROZEN-EVIDENCE-DO-NOT-WRITE\n"
    frozen.write_text(sentinel, encoding="utf-8")

    generate_signal_performance_reports(
        db_path=tmp_path / "portfolio.db", output_dir=perf)

    assert frozen.read_text(encoding="utf-8") == sentinel, (
        "the signal-outcomes producer overwrote VS-001 frozen evidence"
    )
    runtime = SOP.runtime_path(tmp_path)
    assert runtime.exists(), f"producer did not write the runtime path {runtime}"
    assert runtime.read_text(encoding="utf-8-sig").startswith("ticker,")


def test_4b_runtime_and_frozen_paths_are_distinct():
    assert SOP.RUNTIME_REL != SOP.FROZEN_EVIDENCE_REL
    assert SOP.FROZEN_EVIDENCE_REL == "outputs/performance/signal_outcomes.csv"
    assert SOP.RUNTIME_REL == "outputs/runtime/performance/signal_outcomes.csv"
    assert C.classify_path(SOP.RUNTIME_REL) == C.RUNTIME_MUTABLE


def test_4c_runtime_root_is_ignored():
    r = subprocess.run(("git", "-C", str(REPO), "check-ignore", "-q", SOP.RUNTIME_REL))
    assert r.returncode == 0, f"{SOP.RUNTIME_REL} must be git-ignored"


# ===========================================================================
# 5. Live consumers resolve the runtime path — and do NOT fall back
# ===========================================================================

def test_5_live_consumer_reads_the_runtime_path(tmp_path):
    from portfolio_automation import resolution_due_probe as RDP

    runtime = SOP.runtime_path(tmp_path)
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        "ticker,signal_time,outcome_return_1d\nNVDA,2026-01-02T09:00:00,1.5\n",
        encoding="utf-8-sig")
    payload = RDP.build_resolution_due(root=tmp_path)
    assert payload.get("reason") != "no_signal_outcomes_csv", (
        "live consumer did not read the runtime signal-outcomes path"
    )


def test_5b_live_consumer_does_not_fall_back_to_frozen_evidence(tmp_path):
    """No fallback, on purpose.

    A fallback would let production render 2026-06 frozen evidence as current
    data — a silent-staleness defect. Absent runtime data must report absent.
    """
    from portfolio_automation import resolution_due_probe as RDP

    frozen = tmp_path / SOP.FROZEN_EVIDENCE_REL
    frozen.parent.mkdir(parents=True)
    frozen.write_text(
        "ticker,signal_time,outcome_return_1d\nNVDA,2026-01-02T09:00:00,1.5\n",
        encoding="utf-8-sig")
    assert not SOP.runtime_path(tmp_path).exists()
    payload = RDP.build_resolution_due(root=tmp_path)
    assert payload["reason"] == "no_signal_outcomes_csv", (
        "consumer silently fell back to the frozen VS-001 evidence"
    )


@pytest.mark.parametrize("module_rel", [
    "gui/app.py",
    "portfolio_automation/flock_intelligence/data_sources.py",
    "portfolio_automation/flock_intelligence/producer.py",
    "portfolio_automation/pattern_learning.py",
    "portfolio_automation/semantic_liveness.py",
    "portfolio_automation/universe_sanitation.py",
    "portfolio_automation/resolution_due_probe.py",
    "portfolio_automation/retune_impact_tracker.py",
])
def test_5c_no_live_consumer_hardcodes_the_frozen_path(module_rel):
    """Every live consumer derives its path from the single source of truth.

    Checked structurally: the module must import signal_outcomes_paths and must
    not contain the frozen literal. Only vertical_slice/* may name the frozen
    path, and it is excluded from this list deliberately.
    """
    src = (REPO / module_rel).read_text(encoding="utf-8")
    assert "from portfolio_automation import signal_outcomes_paths" in src, (
        f"{module_rel} must derive the path from signal_outcomes_paths"
    )
    assert '"outputs", "performance", "signal_outcomes.csv"' not in src
    assert "outputs/performance/signal_outcomes.csv" not in src, (
        f"{module_rel} still names the frozen VS-001 evidence path"
    )


def test_5d_tuple_consumers_derive_the_runtime_tuple():
    from portfolio_automation import resolution_due_probe as RDP
    from portfolio_automation import retune_impact_tracker as RIT

    expected = tuple(SOP.RUNTIME_REL.split("/"))
    assert RDP._SIGNAL_OUTCOMES_REL == expected
    assert RIT._SIGNAL_OUTCOMES_REL == expected


# ===========================================================================
# 8-9. Zero tracked production-generated artifacts
# ===========================================================================

@pytest.mark.parametrize("rel", C.RUNTIME_GENERATED_ARTIFACTS)
def test_9_runtime_generated_artifact_is_not_tracked(rel):
    assert _tracked_under(rel) == [], (
        f"{rel} is tracked again. It is generated by production at runtime; "
        f"tracking it makes the release worktree dirty during normal operation."
    )


@pytest.mark.parametrize("rel", C.RUNTIME_GENERATED_ARTIFACTS)
def test_9b_runtime_generated_artifact_is_ignored(rel):
    r = subprocess.run(("git", "-C", str(REPO), "check-ignore", "-q", rel))
    assert r.returncode == 0, f"{rel} is not covered by .gitignore"


def test_8_production_snapshot_is_not_tracked():
    """Human decision 3: it is runtime state, not source."""
    rel = "outputs/portfolio/portfolio_snapshot.json"
    assert rel in C.RUNTIME_GENERATED_ARTIFACTS
    assert _tracked_under(rel) == []
    assert C.is_runtime_mutable(rel)


@pytest.mark.parametrize("root", ["data/", "logs/"])
def test_9c_clean_runtime_roots_hold_nothing_tracked(root):
    assert _tracked_under(root) == []


def test_9d_outputs_tracks_only_immutable_evidence():
    """The strongest truthful invariant.

    Not "nothing tracked under outputs/" — that would forbid legitimate frozen
    experiment evidence. The claim is: zero tracked RUNTIME_GENERATED
    artifacts, and the tracked set is exactly the immutable-evidence allowlist.
    """
    tracked = set(_tracked_under("outputs/"))
    assert tracked == set(C.IMMUTABLE_EVIDENCE_ARTIFACTS), (
        f"outputs/ tracked set drifted from the contract.\n"
        f"  unexpected: {sorted(tracked - set(C.IMMUTABLE_EVIDENCE_ARTIFACTS))}\n"
        f"  missing:    {sorted(set(C.IMMUTABLE_EVIDENCE_ARTIFACTS) - tracked)}"
    )
    assert not (tracked & set(C.RUNTIME_GENERATED_ARTIFACTS))


def test_9e_the_classifications_are_disjoint_and_account_for_all_twelve():
    generated = set(C.RUNTIME_GENERATED_ARTIFACTS)
    evidence = set(C.IMMUTABLE_EVIDENCE_ARTIFACTS)
    assert len(generated) == 11
    assert len(evidence) == 1
    assert generated.isdisjoint(evidence)
    assert len(generated | evidence) == 12, "the original twelve must be accounted for"


def test_9f_fixture_generation_does_not_mutate_tracked_state(tmp_path):
    before = _git("status", "--porcelain", "--untracked-files=no")
    for rel in C.RUNTIME_GENERATED_ARTIFACTS:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}" if target.suffix == ".json" else "x", encoding="utf-8")
    assert _git("status", "--porcelain", "--untracked-files=no") == before


# ===========================================================================
# 10-13. Doc audit: production must not author, and must not mutate
# ===========================================================================

def _doc_audit_src() -> str:
    return (REPO / "scripts" / "run_doc_audit.sh").read_text(encoding="utf-8")


def test_10_mutation_is_gated_before_it_occurs_not_reverted_after():
    """Ordering proof: every mutation site sits AFTER the non-authoring exit.

    This is the defect Codex flagged as P1 and it was right: the previous gate
    let the writes happen and then reset the index, which does not undo file
    writes. So the guard is an ordering assertion, not a text ban — a new
    ungated mutation added anywhere above the boundary fails this test.
    """
    src = _doc_audit_src()
    lines = src.splitlines()

    # The gate helper's own `git commit` is its whole purpose and is defined
    # near the top of the file; exclude its body so the ordering check measures
    # the execution flow rather than the definition site.
    helper_start = next(i for i, ln in enumerate(lines)
                        if ln.startswith("doc_audit_commit() {"))
    helper_end = next(i for i in range(helper_start, len(lines))
                      if lines[i] == "}")

    def _code(i: int) -> str:
        ln = lines[i]
        if ln.lstrip().startswith("#"):
            return ""
        if helper_start <= i <= helper_end:
            return ""
        return ln

    gate = [i for i, ln in enumerate(lines)
            if 'DOC_AUDIT_GIT_AUTHORSHIP" != "1"' in _code(i)
            and "NON-AUTHORING" in "\n".join(lines[i:i + 4])]
    assert len(gate) == 1, "expected exactly one non-authoring mutation boundary"
    boundary = gate[0]
    exits = [i for i in range(boundary, len(lines)) if _code(i).strip() == "exit 0"]
    assert exits, "the non-authoring branch must exit before mutating"
    exit_line = exits[0]

    mutators = ("git add", "git commit", "doc_audit_commit ", "save_state(")
    offenders = [
        (i + 1, lines[i].strip()[:70])
        for i in range(len(lines))
        if i < exit_line and any(m in _code(i) for m in mutators)
    ]
    assert not offenders, (
        f"mutation before the non-authoring exit (line {exit_line + 1}): {offenders}"
    )


def test_11_no_executable_reset_checkout_or_restore():
    """Unstaging is not proof the tree is unchanged, and cleanup can destroy
    pre-existing operator changes. Neither is permitted."""
    for i, ln in enumerate(_doc_audit_src().splitlines(), 1):
        if ln.lstrip().startswith("#"):
            continue
        for forbidden in ("git reset", "git checkout", "git restore"):
            assert forbidden not in ln, f"line {i} uses {forbidden}"


def test_12_authorship_defaults_to_disabled():
    src = _doc_audit_src()
    assert 'DOC_AUDIT_GIT_AUTHORSHIP="${STOCKBOT_DOC_AUDIT_GIT_AUTHORSHIP:-0}"' in src


def test_12b_commit_helper_fails_closed_if_ever_reached_unauthorised():
    src = _doc_audit_src()
    start = src.index("doc_audit_commit() {")
    body = src[start:src.index("\n}\n", start)]
    assert "exit 2" in body, "the helper must fail loudly, not commit"
    assert "git commit -m" in body


def test_10b_status_write_does_not_touch_tracked_state(tmp_path):
    """The actual mechanism of the P1 defect, tested directly.

    write_doc_audit_status persisted coverage-gap bookkeeping into the TRACKED
    .agent/doc_audit_state.yaml. That is why a non-authoring run still dirtied
    the worktree even though Step 4 was skipped.
    """
    from portfolio_automation import doc_audit

    state = tmp_path / ".agent" / "doc_audit_state.yaml"
    state.parent.mkdir(parents=True)
    original = "apply_enabled: true\ncoverage_gap_first_seen: {}\n"
    state.write_text(original, encoding="utf-8")

    result = {
        "generated_at": "2026-01-02T00:00:00+00:00",
        "overall_status": "coverage_gap",
        "findings": [],
        "auto_fix_candidates": [],
        "coverage_gaps": ["docs/x.md"],
        "open_coverage_gaps": ["docs/x.md"],
    }
    doc_audit.write_doc_audit_status(result, str(tmp_path), persist_state=False)
    assert state.read_text(encoding="utf-8") == original, (
        "persist_state=False still wrote the tracked state file"
    )
    assert (tmp_path / "outputs" / "latest" / "doc_audit_status.json").exists(), (
        "the untracked status artifact should still be produced"
    )


def test_13_authoring_mode_still_persists_state(tmp_path):
    """Engineering behaviour must be preserved, not quietly removed."""
    from portfolio_automation import doc_audit

    state = tmp_path / ".agent" / "doc_audit_state.yaml"
    state.parent.mkdir(parents=True)
    original = "apply_enabled: true\ncoverage_gap_first_seen: {}\n"
    state.write_text(original, encoding="utf-8")

    result = {
        "generated_at": "2026-01-02T00:00:00+00:00",
        "overall_status": "coverage_gap",
        "findings": [],
        "auto_fix_candidates": [],
        "coverage_gaps": ["docs/x.md"],
        "open_coverage_gaps": ["docs/x.md"],
    }
    doc_audit.write_doc_audit_status(result, str(tmp_path), persist_state=True)
    assert state.read_text(encoding="utf-8") != original, (
        "authoring mode must still advance coverage-gap bookkeeping"
    )


def test_13b_script_tells_engineers_how_to_enable_authoring():
    assert "STOCKBOT_DOC_AUDIT_GIT_AUTHORSHIP=1" in _doc_audit_src()


# ===========================================================================
# 14-15. Scheduler: WorkingDirectory and EnvironmentFile
# ===========================================================================

LEGACY_WORKDIR_UNIT = """\
[Service]
User=root
WorkingDirectory=/opt/stockbot
EnvironmentFile=/opt/stockbot/.env
ExecStart=/opt/stockbot/current/.venv/bin/uvicorn gui_v2.app:app --host 127.0.0.1 --port 8502
"""

ALIGNED_UNIT = """\
[Service]
WorkingDirectory=/opt/stockbot/current
EnvironmentFile=/opt/stockbot/current/.env
ExecStart=/opt/stockbot/current/.venv/bin/streamlit run gui/app.py \\
    --server.address 127.0.0.1 \\
    --server.port 8501
"""

CRONTAB = """\
SHELL=/bin/sh
PATH=/usr/sbin:/usr/bin:/sbin:/bin

# Daily pipeline
0 9 * * * /opt/stockbot/current/scripts/run_daily_safe.sh >> /opt/stockbot/logs/cron.log 2>&1
30 23 * * * /opt/stockbot/current/scripts/backup_state.sh
0 7 * * 1 /opt/stockbot/scripts/run_doc_audit.sh
*/5 * * * * root /usr/local/sbin/stockbot-evidence-publish
"""


def test_14_legacy_working_directory_is_not_certified():
    """The live dashboard shape: release ExecStart, legacy WorkingDirectory.

    `uvicorn gui_v2.app:app` names the module relatively, so Python resolves
    gui_v2 from the working directory — this unit runs the release interpreter
    against legacy application code. ExecStart-only parsing calls it aligned.
    """
    s = S.parse_systemd_unit(LEGACY_WORKDIR_UNIT, origin="systemd:dash")[0]
    assert s.working_directory == "/opt/stockbot"
    assert not s.resolves_to_release(release_root=S.CURRENT_POINTER)
    assert "/opt/stockbot" in s.legacy_paths(release_root=S.CURRENT_POINTER)
    report = S.certify_scheduler_identity([s], release_root=S.CURRENT_POINTER)
    assert report["status"] == "FAILED"


def test_14b_aligned_working_directory_certifies():
    s = S.parse_systemd_unit(ALIGNED_UNIT, origin="systemd:streamlit")[0]
    assert s.working_directory == "/opt/stockbot/current"
    assert s.resolves_to_release(release_root=S.CURRENT_POINTER)
    # Continuation lines must be joined or the bind address vanishes.
    assert "--server.address 127.0.0.1" in s.raw


def test_15_environment_file_is_surfaced_but_not_counted_as_code_drift():
    """A credential file is not release code, but the cutover must re-provision
    it, so it is reported rather than ignored."""
    s = S.parse_systemd_unit(LEGACY_WORKDIR_UNIT, origin="systemd:dash")[0]
    assert s.environment_files == ("/opt/stockbot/.env",)
    # Not code: it must not appear among the code-path failures.
    assert "/opt/stockbot/.env" not in s.legacy_paths(release_root=S.CURRENT_POINTER)
    assert s.legacy_secret_paths(release_root=S.CURRENT_POINTER) == ("/opt/stockbot/.env",)
    report = S.certify_scheduler_identity([s], release_root=S.CURRENT_POINTER)
    assert any(".env" in e for e in report["secret_paths_outside_release"])


def test_15b_a_secret_outside_the_release_alone_does_not_fail_certification():
    unit = """\
[Service]
WorkingDirectory=/opt/stockbot/current
EnvironmentFile=/opt/stockbot/.env
ExecStart=/opt/stockbot/current/.venv/bin/python -m portfolio_automation
"""
    s = S.parse_systemd_unit(unit, origin="systemd:secretonly")[0]
    report = S.certify_scheduler_identity([s], release_root=S.CURRENT_POINTER)
    assert report["status"] == "OK", "a credential path is not code drift"
    assert report["secret_paths_outside_release"], "but it must still be surfaced"


def test_14c_optional_environment_file_prefix_is_handled():
    unit = ("[Service]\nWorkingDirectory=/opt/stockbot/current\n"
            "EnvironmentFile=-/opt/stockbot/current/.env\n"
            "ExecStart=/opt/stockbot/current/.venv/bin/python -m x\n")
    s = S.parse_systemd_unit(unit, origin="systemd:opt")[0]
    assert s.environment_files == ("/opt/stockbot/current/.env",)


def test_14d_sibling_directory_is_not_mistaken_for_the_release():
    unit = ("[Service]\nWorkingDirectory=/opt/stockbot/current-old\n"
            "ExecStart=/opt/stockbot/current-old/.venv/bin/python -m x\n")
    s = S.parse_systemd_unit(unit, origin="systemd:sibling")[0]
    assert not s.resolves_to_release(release_root=S.CURRENT_POINTER)


def test_14e_interpreter_invocation_exposes_the_real_script():
    unit = "[Service]\nExecStart=/bin/bash /opt/stockbot/scripts/run_daily_sandbox_safe.sh\n"
    s = S.parse_systemd_unit(unit, origin="systemd:sandbox")[0]
    assert s.executable == "/opt/stockbot/scripts/run_daily_sandbox_safe.sh"
    assert not s.resolves_to_release(release_root=S.CURRENT_POINTER)


def test_14f_cron_parsing_and_certification():
    surfaces = S.parse_crontab(CRONTAB)
    assert len(surfaces) == 4
    daily = surfaces[0]
    assert daily.executable == "/opt/stockbot/current/scripts/run_daily_safe.sh"
    assert "cron.log" not in " ".join(daily.referenced_paths)
    publisher = surfaces[-1]
    assert publisher.executable == "/usr/local/sbin/stockbot-evidence-publish"
    assert publisher.is_system_transitional
    report = S.certify_scheduler_identity(surfaces, release_root=S.CURRENT_POINTER)
    assert report["status"] == "FAILED"
    assert len(report["unresolved"]) == 1
    assert "run_doc_audit.sh" in report["errors"][0]
    assert report["system_transitional"] == [publisher.origin]


def test_14g_certification_fails_closed_on_empty_input():
    report = S.certify_scheduler_identity([], release_root=S.CURRENT_POINTER)
    assert report["status"] == "FAILED"
    assert report["errors"]


@pytest.mark.parametrize("content,fn", [
    # A blank `ExecStart=` is a systemd list RESET, not malformed content —
    # asserting otherwise was the original defect this module fixes, so the
    # malformed case is a genuinely unparseable non-empty command instead.
    ('[Service]\nExecStart=/opt/stockbot/current/bin/x "unterminated\n', "unit"),
    ("not a schedule at all\n", "cron"),
])
def test_14h_unparseable_content_raises(content, fn):
    with pytest.raises(S.SchedulerParseError):
        if fn == "unit":
            S.parse_systemd_unit(content, origin="systemd:bad")
        else:
            S.parse_crontab(content)


def test_14i_scheduler_declares_its_own_scope_honestly():
    """Path alignment must not be mistaken for release identity."""
    surfaces = S.parse_systemd_unit(ALIGNED_UNIT, origin="systemd:streamlit")
    report = S.certify_scheduler_identity(surfaces, release_root=S.CURRENT_POINTER)
    assert report["scope"] == "path_alignment_only"


# ===========================================================================
# 16-18. Pointer certification, and the two together
# ===========================================================================

APPROVED = "ebcb03eba297a08580eeba362aa8471759fe3c74"
OTHER = "9650c6671ece88d9b6a61efff1a8af3a6cac91b4"


def _good(**over) -> PT.PointerEvidence:
    base = dict(
        pointer_path=S.CURRENT_POINTER, exists=True, is_symlink=True,
        link_target=f"releases/{APPROVED}",
        resolved_path=f"/opt/stockbot/releases/{APPROVED}",
        resolved_exists=True, target_sha=APPROVED, target_tracked_dirty=False,
    )
    base.update(over)
    return PT.PointerEvidence(**base)


def test_18_pointer_and_scheduler_together_establish_release_identity():
    surfaces = S.parse_systemd_unit(ALIGNED_UNIT, origin="systemd:streamlit")
    ptr = PT.certify_pointer(_good(), approved_sha=APPROVED)
    assert ptr["status"] == PT.POINTER_OK, ptr["errors"]
    combined = S.certify_release_identity(surfaces, pointer_result=ptr)
    assert combined["status"] == "OK", combined["errors"]


def test_18b_aligned_schedulers_with_a_stale_pointer_do_not_certify():
    """The exact hole Codex flagged: every surface OK, wrong release deployed."""
    surfaces = S.parse_systemd_unit(ALIGNED_UNIT, origin="systemd:streamlit")
    sched = S.certify_scheduler_identity(surfaces, release_root=S.CURRENT_POINTER)
    assert sched["status"] == "OK"          # path alignment passes...
    ptr = PT.certify_pointer(_good(target_sha=OTHER), approved_sha=APPROVED)
    assert ptr["status"] == PT.POINTER_FAILED   # ...but identity does not
    combined = S.certify_release_identity(surfaces, pointer_result=ptr)
    assert combined["status"] == "FAILED"
    assert any("!= approved" in e for e in combined["errors"])


def test_18c_correct_pointer_with_a_legacy_scheduler_does_not_certify():
    surfaces = S.parse_systemd_unit(LEGACY_WORKDIR_UNIT, origin="systemd:dash")
    ptr = PT.certify_pointer(_good(), approved_sha=APPROVED)
    assert ptr["status"] == PT.POINTER_OK
    combined = S.certify_release_identity(surfaces, pointer_result=ptr)
    assert combined["status"] == "FAILED"


def test_16_pointer_rejects_a_wrong_approved_sha():
    r = PT.certify_pointer(_good(), approved_sha=OTHER)
    assert r["status"] == PT.POINTER_FAILED
    assert any("!= approved" in e for e in r["errors"])


def test_16b_pointer_rejects_an_abbreviated_sha():
    """Comparing a prefix invites a false match and hides what is deployed."""
    assert PT.certify_pointer(_good(), approved_sha="ebcb03eb")["status"] == PT.POINTER_FAILED
    assert PT.certify_pointer(_good(target_sha="ebcb03eb"),
                              approved_sha=APPROVED)["status"] == PT.POINTER_FAILED


@pytest.mark.parametrize("over,needle", [
    ({"exists": False}, "pointer missing"),
    ({"is_symlink": False}, "not a symlink"),
    ({"resolved_path": None}, "did not resolve"),
    ({"resolved_exists": False}, "does not exist"),
    ({"resolved_path": "/opt/stockbot"}, "outside the releases root"),
    ({"resolved_path": "/tmp/elsewhere"}, "outside the releases root"),
    ({"target_sha": None}, "unavailable"),
    ({"target_tracked_dirty": None}, "unknown"),
    ({"target_tracked_dirty": True}, "uncommitted tracked modifications"),
])
def test_17_pointer_fails_closed_on_every_missing_link(over, needle):
    r = PT.certify_pointer(_good(**over), approved_sha=APPROVED)
    assert r["status"] == PT.POINTER_FAILED
    assert any(needle in e for e in r["errors"]), r["errors"]


def test_17b_pointer_never_raises_on_unusable_evidence():
    """An unusable record is a FAILED certification, not a traceback."""
    r = PT.certify_pointer(PT.PointerEvidence(pointer_path=""), approved_sha="")
    assert r["status"] == PT.POINTER_FAILED
    assert len(r["errors"]) >= 3


def test_17c_pointer_result_records_what_it_compared():
    r = PT.certify_pointer(_good(), approved_sha=APPROVED)
    assert r["resolved_path"] == f"/opt/stockbot/releases/{APPROVED}"
    assert r["target_sha"] == APPROVED
    assert r["approved_sha"] == APPROVED


def test_pointer_is_the_recommended_release_model():
    assert S.RECOMMENDED_MODEL == S.POINTER


# ===========================================================================
# Runtime/immutable classification and venv preflight
# ===========================================================================

@pytest.mark.parametrize("rel", [
    "portfolio_automation/release/contracts.py", "scripts/run_doc_audit.sh",
    "gui/app.py", "gui_v2/app.py", "config/ew0a_runtime.json",
    "requirements.txt", "main.py",
])
def test_release_source_is_immutable(rel):
    assert C.classify_path(rel) == C.RELEASE_IMMUTABLE


@pytest.mark.parametrize("rel", [
    "data/portfolio.db", "outputs/agent_export/latest.json",
    "outputs/backtest/historical/SPY_5y.json", "logs/cron.log",
])
def test_production_writable_paths_are_runtime_mutable(rel):
    assert C.is_runtime_mutable(rel)


@pytest.mark.parametrize("bad", ["/etc/passwd", "../../etc/passwd",
                                 "outputs/../../escape", ""])
def test_classification_refuses_paths_outside_the_release(bad):
    with pytest.raises(ValueError):
        C.classify_path(bad)


LEGACY = "/opt/stockbot"
RELEASE = "/opt/stockbot/current"


def _venv(tmp_path: Path) -> Path:
    (tmp_path / "venv" / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    return tmp_path / "venv"


def test_clean_environment_passes_preflight(tmp_path):
    venv = _venv(tmp_path)
    site = next(venv.glob("lib/python*/site-packages"))
    (site / "harmless.pth").write_text("/opt/stockbot/current/extra\n", encoding="utf-8")
    r = PF.scan_environment(venv, legacy_root=LEGACY, release_root=RELEASE)
    assert r.ok, r.findings


@pytest.mark.parametrize("name,content,needle", [
    ("stockbot.pth", "/opt/stockbot\n", "sys.path entry binds legacy tree"),
    ("__editable__.sb.pth",
     "import _f; _f.install('/opt/stockbot')\n", "executable .pth"),
])
def test_pth_bindings_to_legacy_fail(tmp_path, name, content, needle):
    venv = _venv(tmp_path)
    site = next(venv.glob("lib/python*/site-packages"))
    (site / name).write_text(content, encoding="utf-8")
    r = PF.scan_environment(venv, legacy_root=LEGACY, release_root=RELEASE)
    assert not r.ok
    assert any(needle in f for f in r.findings)


def test_egg_link_pointing_at_legacy_fails(tmp_path):
    venv = _venv(tmp_path)
    site = next(venv.glob("lib/python*/site-packages"))
    (site / "sb.egg-link").write_text("/opt/stockbot\n.\n", encoding="utf-8")
    r = PF.scan_environment(venv, legacy_root=LEGACY, release_root=RELEASE)
    assert not r.ok
    assert any("editable install points at" in f for f in r.findings)


@pytest.mark.parametrize("url,editable,ok", [
    ("file:///opt/stockbot", True, False),
    ("file:///opt/stockbot/current", False, True),
])
def test_direct_url_records_the_source_tree(tmp_path, url, editable, ok):
    venv = _venv(tmp_path)
    site = next(venv.glob("lib/python*/site-packages"))
    di = site / "sb-1.0.dist-info"
    di.mkdir()
    (di / "direct_url.json").write_text(
        json.dumps({"url": url, "dir_info": {"editable": editable}}), encoding="utf-8")
    r = PF.scan_environment(venv, legacy_root=LEGACY, release_root=RELEASE)
    assert r.ok is ok, r.findings


def test_uninspectable_environment_fails_closed(tmp_path):
    bare = tmp_path / "venv"
    bare.mkdir()
    assert not PF.scan_environment(bare, legacy_root=LEGACY, release_root=RELEASE).ok
    assert not PF.scan_environment(tmp_path / "nope", legacy_root=LEGACY,
                                   release_root=RELEASE).ok


def test_corrupt_direct_url_is_a_finding_not_a_crash(tmp_path):
    venv = _venv(tmp_path)
    site = next(venv.glob("lib/python*/site-packages"))
    di = site / "broken-1.0.dist-info"
    di.mkdir()
    (di / "direct_url.json").write_bytes(b"\xff\xfe not json")
    r = PF.scan_environment(venv, legacy_root=LEGACY, release_root=RELEASE)
    assert not r.ok
    assert any("unreadable" in f for f in r.findings)


def test_import_origins_decide_which_code_actually_ran():
    good = {m: f"{RELEASE}/{m}/__init__.py" for m in PF.PROJECT_MODULES}
    assert PF.verify_import_origin(good, release_root=RELEASE).ok
    bad = dict(good, portfolio_automation=f"{LEGACY}/portfolio_automation/__init__.py")
    r = PF.verify_import_origin(bad, release_root=RELEASE)
    assert not r.ok
    assert any("outside the approved release" in f for f in r.findings)
    empty = PF.verify_import_origin({}, release_root=RELEASE)
    assert not empty.ok
    assert len(empty.findings) == len(PF.PROJECT_MODULES)


# ===========================================================================
# Documentation and backup contract
# ===========================================================================

def test_contract_document_states_the_invariants():
    doc = (REPO / "docs" / "PRODUCTION_RELEASE_CONTRACT.md").read_text(encoding="utf-8")
    flat = " ".join(doc.replace("*", "").replace("`", "").split())
    assert "PRODUCTION_RUNTIME_DOES_NOT_CREATE_GIT_COMMITS" in flat
    assert "must not schedule" in flat
    for token in ("systemd", "cron", "timer", "WorkingDirectory",
                  "EnvironmentFile", "IMMUTABLE_EXPERIMENT_EVIDENCE"):
        assert token in flat, f"contract must cover {token}"


def test_absent_optional_databases_are_gaps_not_blockers():
    by_path = {s.path: s for s in RS.backup_databases() + RS.backup_state_files()}
    for optional in ("data/rd_control.db", "data/institutional_intelligence.db"):
        assert by_path[optional].required is False


def test_the_two_hard_required_surfaces_are_unchanged():
    by_path = {s.path: s for s in RS.backup_databases() + RS.backup_state_files()}
    assert sorted(p for p, s in by_path.items() if s.required) == [
        "data/finance_history.json", "data/portfolio.db"]


def test_untracking_created_no_backup_obligation():
    for rel in C.RUNTIME_GENERATED_ARTIFACTS:
        assert RS.classification_of(rel) != "BACKUP_REQUIRED"


def test_every_registered_outputs_surface_is_regenerable():
    registered = {s.path: s.classification
                  for s in RS.ALL_SURFACES if s.path.startswith("outputs/")}
    assert registered
    assert set(registered.values()) == {"REGENERABLE"}, registered


# ===========================================================================
# EXECUTION-PATH EXTRACTION AND RELEASE ALIGNMENT
#
# This module certifies which executable/application paths a scheduler
# configuration can invoke, and whether they resolve to the approved release.
# It does NOT decide whether systemd would accept a unit — that is
# `systemd-analyze verify`, which the cutover runbook runs directly.
#
# An earlier revision emulated systemd validity and drew seven consecutive
# review findings, two of them false rejections that would have blocked a
# correct cutover. Those rules were removed rather than extended; these tests
# cover the retained responsibility only.
# ===========================================================================

MISSION22_DASHBOARD_UNIT = """\
[Unit]
Description=StockBot Dashboard

[Service]
WorkingDirectory=/opt/stockbot
EnvironmentFile=/opt/stockbot/.env
ExecStart=/opt/stockbot/.venv/bin/uvicorn gui_v2.app:app --host 127.0.0.1 --port 8502

[Service]
WorkingDirectory=/opt/stockbot/current
ExecStart=
ExecStart=/opt/stockbot/current/.venv/bin/uvicorn gui_v2.app:app --host 127.0.0.1 --port 8502
"""

PRODUCTION_CRON = """\
SHELL=/bin/sh
PATH=/usr/sbin:/usr/bin:/sbin:/bin

0 9 * * * /opt/stockbot/current/scripts/run_daily_safe.sh >> /opt/stockbot/logs/cron.log 2>&1
15 9 * * 1-5 /opt/stockbot/current/scripts/daily_check.sh >> /opt/stockbot/logs/daily_check_cron.log 2>&1
30 23 * * * /opt/stockbot/current/scripts/backup_state.sh
*/5 * * * * root /usr/local/sbin/stockbot-evidence-publish
"""


# --- 1. inherited legacy ExecStart replaced by a reset ---------------------

def test_1_reset_replaces_inherited_legacy_execstart():
    unit = """\
[Service]
ExecStart=/opt/stockbot/scripts/legacy.sh
ExecStart=
ExecStart=/opt/stockbot/current/scripts/run_daily.sh
"""
    surfaces = S.parse_systemd_unit(unit, origin="systemd:reset")
    assert [x.executable for x in surfaces] == [
        "/opt/stockbot/current/scripts/run_daily.sh"], [x.raw for x in surfaces]
    assert surfaces[0].resolves_to_release(release_root=S.CURRENT_POINTER)


# --- 2/3. stop commands are executable release surfaces -------------------

@pytest.mark.parametrize("directive", ["ExecStop", "ExecStopPost"])
def test_2_3_legacy_stop_commands_fail_alignment(directive):
    """A stop command on the legacy checkout is legacy code running on the host."""
    unit = ("[Service]\nWorkingDirectory=/opt/stockbot/current\n"
            "ExecStart=/opt/stockbot/current/scripts/run_daily.sh\n"
            f"{directive}=/opt/stockbot/scripts/legacy-stop.sh\n")
    surfaces = S.parse_systemd_unit(unit, origin=f"systemd:{directive}")
    assert "/opt/stockbot/scripts/legacy-stop.sh" in [x.executable for x in surfaces]
    report = S.certify_scheduler_identity(surfaces, release_root=S.CURRENT_POINTER)
    assert report["status"] == "FAILED"
    assert any("legacy-stop.sh" in e for e in report["errors"])


def test_2b_release_bound_stop_commands_certify():
    unit = ("[Service]\nWorkingDirectory=/opt/stockbot/current\n"
            "ExecStart=/opt/stockbot/current/scripts/run_daily.sh\n"
            "ExecStop=/opt/stockbot/current/scripts/stop.sh\n"
            "ExecStopPost=/opt/stockbot/current/scripts/cleanup.sh\n")
    surfaces = S.parse_systemd_unit(unit, origin="systemd:stopok")
    assert len(surfaces) == 3
    assert S.certify_scheduler_identity(
        surfaces, release_root=S.CURRENT_POINTER)["status"] == "OK"


# --- 4. ExecStop reset removes the prior legacy path ----------------------

def test_4_execstop_reset_removes_the_legacy_path():
    unit = ("[Service]\nWorkingDirectory=/opt/stockbot/current\n"
            "ExecStart=/opt/stockbot/current/scripts/run_daily.sh\n"
            "ExecStop=/opt/stockbot/scripts/legacy-stop.sh\n"
            "ExecStop=\n"
            "ExecStop=/opt/stockbot/current/scripts/stop.sh\n")
    surfaces = S.parse_systemd_unit(unit, origin="systemd:stopreset")
    execs = [x.executable for x in surfaces]
    assert "/opt/stockbot/scripts/legacy-stop.sh" not in execs, execs
    assert S.certify_scheduler_identity(
        surfaces, release_root=S.CURRENT_POINTER)["status"] == "OK"


# --- 5. reset semantics for the remaining Exec* directives ---------------

@pytest.mark.parametrize("directive", ["ExecStartPre", "ExecStartPost", "ExecReload"])
def test_5_reset_semantics_for_every_exec_directive(directive):
    unit = ("[Service]\nWorkingDirectory=/opt/stockbot/current\n"
            f"{directive}=/opt/stockbot/scripts/old.sh\n"
            f"{directive}=\n"
            f"{directive}=/opt/stockbot/current/scripts/new.sh\n"
            "ExecStart=/opt/stockbot/current/scripts/run_daily.sh\n")
    execs = [x.executable for x in S.parse_systemd_unit(unit, origin="systemd:x")]
    assert "/opt/stockbot/scripts/old.sh" not in execs, execs
    assert "/opt/stockbot/current/scripts/new.sh" in execs


def test_5b_multiple_commands_after_a_reset_are_all_kept():
    unit = ("[Service]\nWorkingDirectory=/opt/stockbot/current\n"
            "ExecStartPre=/opt/stockbot/scripts/old.sh\n"
            "ExecStartPre=\n"
            "ExecStartPre=/opt/stockbot/current/scripts/a.sh\n"
            "ExecStartPre=/opt/stockbot/current/scripts/b.sh\n"
            "ExecStart=/opt/stockbot/current/scripts/run_daily.sh\n")
    execs = sorted(x.executable for x in S.parse_systemd_unit(unit, origin="systemd:multi"))
    assert execs == ["/opt/stockbot/current/scripts/a.sh",
                     "/opt/stockbot/current/scripts/b.sh",
                     "/opt/stockbot/current/scripts/run_daily.sh"], execs


# --- 6. directives outside [Service] are not execution surfaces ----------

def test_6_directives_outside_service_are_not_execution_surfaces():
    unit = ("[Unit]\n"
            "ExecStart=/opt/stockbot/scripts/legacy-in-unit.sh\n"
            "WorkingDirectory=/opt/stockbot\n"
            "EnvironmentFile=/opt/stockbot/.env\n"
            "[Service]\n"
            "WorkingDirectory=/opt/stockbot/current\n"
            "ExecStart=/opt/stockbot/current/scripts/run_daily.sh\n")
    surfaces = S.parse_systemd_unit(unit, origin="systemd:sections")
    assert [x.executable for x in surfaces] == [
        "/opt/stockbot/current/scripts/run_daily.sh"]
    assert surfaces[0].working_directory == "/opt/stockbot/current"
    assert surfaces[0].environment_files == ()


# --- 7/8/9. WorkingDirectory and RootDirectory ---------------------------

def test_7_working_directory_effective_semantics():
    """Last non-blank wins; blank reverts to unset."""
    last_wins = S.parse_systemd_unit(
        "[Service]\nWorkingDirectory=/opt/stockbot/current\n"
        "WorkingDirectory=/opt/stockbot\n"
        "ExecStart=/opt/stockbot/current/scripts/run_daily.sh\n",
        origin="systemd:wd")[0]
    assert last_wins.working_directory == "/opt/stockbot"
    assert not last_wins.resolves_to_release(release_root=S.CURRENT_POINTER), (
        "uvicorn/streamlit name modules relatively, so a legacy working "
        "directory means legacy application code"
    )
    reset = S.parse_systemd_unit(
        "[Service]\nWorkingDirectory=/opt/stockbot\nWorkingDirectory=\n"
        "ExecStart=/opt/stockbot/current/scripts/run_daily.sh\n",
        origin="systemd:wdreset")[0]
    assert reset.working_directory is None
    assert reset.resolves_to_release(release_root=S.CURRENT_POINTER)


def test_8_root_directory_is_independent_of_working_directory():
    """Resetting one must not discard the other."""
    s0 = S.parse_systemd_unit(
        "[Service]\nRootDirectory=/opt/stockbot\nWorkingDirectory=\n"
        "ExecStart=/scripts/run_daily.sh\n", origin="systemd:rootkept")[0]
    assert s0.working_directory is None
    assert s0.root_directory == "/opt/stockbot"
    s1 = S.parse_systemd_unit(
        "[Service]\nWorkingDirectory=/opt/stockbot\nRootDirectory=\n"
        "ExecStart=/opt/stockbot/current/scripts/run_daily.sh\n",
        origin="systemd:wdkept")[0]
    assert s1.root_directory is None
    assert s1.working_directory == "/opt/stockbot"


def test_9_legacy_root_directory_fails_even_with_a_release_executable():
    s0 = S.parse_systemd_unit(
        "[Service]\nRootDirectory=/opt/stockbot\n"
        "ExecStart=/current/scripts/run_daily.sh\n", origin="systemd:legacyroot")[0]
    assert not s0.resolves_to_release(release_root=S.CURRENT_POINTER)
    assert S.certify_scheduler_identity(
        [s0], release_root=S.CURRENT_POINTER)["status"] == "FAILED"


def test_9b_root_directory_is_a_chroot_so_paths_resolve_inside_it():
    """`RootDirectory=` is chroot(2) (systemd.exec(5)).

    A unit with RootDirectory=/srv/jail and a release-looking ExecStart really
    executes /srv/jail/opt/stockbot/current/... — nothing to do with the
    approved pointer. Comparing the lexical ExecStart would approve it.
    """
    s0 = S.parse_systemd_unit(
        "[Service]\nRootDirectory=/srv/jail\n"
        "ExecStart=/opt/stockbot/current/scripts/run_daily.sh\n",
        origin="systemd:jail")[0]
    assert s0._in_root(s0.executable) == \
        "/srv/jail/opt/stockbot/current/scripts/run_daily.sh"
    assert not s0.resolves_to_release(release_root=S.CURRENT_POINTER)
    assert S.certify_scheduler_identity(
        [s0], release_root=S.CURRENT_POINTER)["status"] == "FAILED"


def test_9c_a_release_rooted_chroot_with_in_root_paths_certifies():
    """The coherent chrooted shape: paths are relative to the root."""
    s0 = S.parse_systemd_unit(
        "[Service]\nRootDirectory=/opt/stockbot/current\n"
        "WorkingDirectory=/\n"
        "ExecStart=/scripts/run_daily.sh\n", origin="systemd:relroot")[0]
    assert s0._in_root(s0.executable) == "/opt/stockbot/current/scripts/run_daily.sh"
    assert s0.resolves_to_release(release_root=S.CURRENT_POINTER)


# --- 10. EnvironmentFile ---------------------------------------------------

def test_10_environment_file_classification_and_reset():
    s0 = S.parse_systemd_unit(
        "[Service]\nEnvironmentFile=/opt/stockbot/.env\n"
        "EnvironmentFile=-/etc/stockbot/stockbot.env\n"
        "WorkingDirectory=/opt/stockbot/current\n"
        "ExecStart=/opt/stockbot/current/scripts/run_daily.sh\n",
        origin="systemd:env")[0]
    assert s0.environment_files == ("/opt/stockbot/.env", "/etc/stockbot/stockbot.env")
    # A credential path is surfaced, never counted as code drift.
    assert s0.legacy_paths(release_root=S.CURRENT_POINTER) == ()
    assert s0.legacy_secret_paths(release_root=S.CURRENT_POINTER) == (
        "/opt/stockbot/.env",)
    report = S.certify_scheduler_identity([s0], release_root=S.CURRENT_POINTER)
    assert report["status"] == "OK"
    assert any(".env" in e for e in report["secret_paths_outside_release"])

    cleared = S.parse_systemd_unit(
        "[Service]\nEnvironmentFile=/opt/stockbot/.env\nEnvironmentFile=\n"
        "EnvironmentFile=/opt/stockbot/current/.env\n"
        "WorkingDirectory=/opt/stockbot/current\n"
        "ExecStart=/opt/stockbot/current/scripts/run_daily.sh\n",
        origin="systemd:envreset")[0]
    assert cleared.environment_files == ("/opt/stockbot/current/.env",)


# --- 11. malformed non-empty command fails closed ------------------------

def test_11_malformed_non_empty_command_fails_closed():
    with pytest.raises(S.SchedulerParseError):
        S.parse_systemd_unit(
            '[Service]\nExecStart=/opt/stockbot/current/bin/x "unterminated\n',
            origin="systemd:bad")
    with pytest.raises(S.SchedulerParseError):
        S.parse_crontab("not a schedule at all\n")


def test_11b_a_blank_exec_is_a_reset_not_a_malformed_command():
    """The bug that started this: a blank Exec* must not raise."""
    surfaces = S.parse_systemd_unit(
        "[Service]\nExecStart=/opt/stockbot/scripts/legacy.sh\nExecStart=\n"
        "ExecStart=/opt/stockbot/current/scripts/run_daily.sh\n",
        origin="systemd:blank")
    assert len(surfaces) == 1


# --- 12. production-shaped units ------------------------------------------

def test_12_mission22_production_unit_resolves_to_the_release():
    surfaces = S.parse_systemd_unit(MISSION22_DASHBOARD_UNIT, origin="systemd:dashboard")
    assert len(surfaces) == 1, [x.raw for x in surfaces]
    s0 = surfaces[0]
    assert s0.executable == "/opt/stockbot/current/.venv/bin/uvicorn"
    assert s0.working_directory == "/opt/stockbot/current"
    assert s0.legacy_paths(release_root=S.CURRENT_POINTER) == ()
    assert "--host 127.0.0.1" in s0.raw and "--port 8502" in s0.raw
    assert S.certify_scheduler_identity(
        [s0], release_root=S.CURRENT_POINTER)["status"] == "OK"


def test_12b_the_live_daily_unit_shape_resolves():
    daily = ("[Unit]\nDescription=StockBot Daily Portfolio Run\n"
             "[Service]\nType=oneshot\nWorkingDirectory=/opt/stockbot\n"
             "ExecStart=/opt/stockbot/scripts/run_daily.sh\n"
             "[Install]\nWantedBy=multi-user.target\n"
             "[Service]\nEnvironmentFile=/opt/stockbot/.env\n"
             "EnvironmentFile=-/etc/stockbot/stockbot.env\n"
             "[Service]\nWorkingDirectory=/opt/stockbot/current\nExecStart=\n"
             "ExecStart=/opt/stockbot/current/scripts/run_daily.sh\n")
    d = S.parse_systemd_unit(daily, origin="systemd:stockbot-daily")
    assert len(d) == 1, [x.raw for x in d]
    assert d[0].executable == "/opt/stockbot/current/scripts/run_daily.sh"
    assert d[0].working_directory == "/opt/stockbot/current"
    assert d[0].resolves_to_release(release_root=S.CURRENT_POINTER)


def test_12c_interpreter_invocation_exposes_the_real_script():
    s0 = S.parse_systemd_unit(
        "[Service]\nExecStart=/bin/bash /opt/stockbot/scripts/run_sandbox.sh\n",
        origin="systemd:sandbox")[0]
    assert s0.executable == "/opt/stockbot/scripts/run_sandbox.sh"
    assert not s0.resolves_to_release(release_root=S.CURRENT_POINTER)


def test_12d_sibling_directory_is_not_mistaken_for_the_release():
    s0 = S.parse_systemd_unit(
        "[Service]\nExecStart=/opt/stockbot/current-old/.venv/bin/python -m x\n",
        origin="systemd:sibling")[0]
    assert not s0.resolves_to_release(release_root=S.CURRENT_POINTER)


# --- 13. cron surfaces -----------------------------------------------------

def test_13_cron_produces_the_expected_release_surfaces():
    surfaces = S.parse_crontab(PRODUCTION_CRON, origin="cron")
    assert len(surfaces) == 4
    release = [x for x in surfaces if "/opt/stockbot/current/scripts/" in x.executable]
    assert len(release) == 3
    # A log redirect target is written, not executed.
    assert "cron.log" not in " ".join(surfaces[0].referenced_paths)
    # The /etc/cron.d user field is stripped, and the publisher is exempt.
    publisher = surfaces[-1]
    assert publisher.executable == "/usr/local/sbin/stockbot-evidence-publish"
    assert publisher.is_system_transitional
    report = S.certify_scheduler_identity(surfaces, release_root=S.CURRENT_POINTER)
    assert report["status"] == "OK", report["errors"]
    assert report["system_transitional"] == [publisher.origin]


def test_13b_a_legacy_cron_entry_fails():
    surfaces = S.parse_crontab(
        "0 7 * * 1 /opt/stockbot/scripts/run_doc_audit.sh\n", origin="cron")
    report = S.certify_scheduler_identity(surfaces, release_root=S.CURRENT_POINTER)
    assert report["status"] == "FAILED"
    assert "run_doc_audit.sh" in report["errors"][0]


# --- 14/15. release identity requires pointer proof too -------------------

def test_14_wrong_pointer_sha_fails_release_identity():
    surfaces = S.parse_systemd_unit(MISSION22_DASHBOARD_UNIT, origin="systemd:dash")
    good = PT.certify_pointer(_good(), approved_sha=APPROVED)
    assert S.certify_release_identity(surfaces, pointer_result=good)["status"] == "OK"
    stale = PT.certify_pointer(_good(target_sha=OTHER), approved_sha=APPROVED)
    combined = S.certify_release_identity(surfaces, pointer_result=stale)
    assert combined["status"] == "FAILED"
    assert any("!= approved" in e for e in combined["errors"])


def test_15_an_effective_legacy_command_fails_release_identity():
    """Aligned pointer, legacy command: still FAILED."""
    legacy = S.parse_systemd_unit(
        "[Service]\nWorkingDirectory=/opt/stockbot\n"
        "ExecStart=/opt/stockbot/.venv/bin/uvicorn gui_v2.app:app\n",
        origin="systemd:legacy")
    good = PT.certify_pointer(_good(), approved_sha=APPROVED)
    assert S.certify_release_identity(
        legacy, pointer_result=good)["status"] == "FAILED"


def test_15b_a_forgotten_reset_leaves_the_legacy_command_effective():
    no_reset = MISSION22_DASHBOARD_UNIT.replace("ExecStart=\n", "")
    surfaces = S.parse_systemd_unit(no_reset, origin="systemd:noreset")
    assert len(surfaces) == 2, [x.raw for x in surfaces]
    assert S.certify_scheduler_identity(
        surfaces, release_root=S.CURRENT_POINTER)["status"] == "FAILED"


# --- 16. an expected service contributing nothing cannot pass -------------

def test_16_expected_service_with_no_surface_cannot_pass_silently():
    """A parser may return nothing for a fragment; the CERTIFIER must not
    accept a known production service that contributes nothing."""
    fragment = ("[Service]\nEnvironmentFile=/opt/stockbot/.env\n"
                "EnvironmentFile=-/etc/stockbot/stockbot.env\n")
    assert S.parse_systemd_unit(fragment, origin="systemd:override.conf") == [], (
        "a bare drop-in legitimately declares no commands"
    )
    aligned = S.parse_crontab(PRODUCTION_CRON, origin="cron")
    aligned += S.parse_systemd_unit(MISSION22_DASHBOARD_UNIT,
                                    origin="systemd:stockbot-dashboard")
    expected = ("systemd:stockbot-dashboard", "systemd:stockbot-streamlit")
    report = S.certify_scheduler_identity(
        aligned, release_root=S.CURRENT_POINTER, expected_origins=expected)
    assert report["status"] == "FAILED", (
        "streamlit contributed no surface yet the set certified OK"
    )
    assert report["missing_expected"] == ["systemd:stockbot-streamlit"]
    assert any("no executable surface" in e for e in report["errors"])


def test_16b_all_expected_services_present_certifies():
    surfaces = S.parse_systemd_unit(MISSION22_DASHBOARD_UNIT,
                                    origin="systemd:stockbot-dashboard")
    surfaces += S.parse_systemd_unit(
        "[Service]\nWorkingDirectory=/opt/stockbot/current\n"
        "ExecStart=/opt/stockbot/current/.venv/bin/streamlit run gui/app.py "
        "--server.address 127.0.0.1 --server.port 8501\n",
        origin="systemd:stockbot-streamlit")
    report = S.certify_scheduler_identity(
        surfaces, release_root=S.CURRENT_POINTER,
        expected_origins=("systemd:stockbot-dashboard", "systemd:stockbot-streamlit"))
    assert report["status"] == "OK", report["errors"]
    assert report["missing_expected"] == []


def test_16c_empty_surface_list_still_fails_closed():
    report = S.certify_scheduler_identity([], release_root=S.CURRENT_POINTER)
    assert report["status"] == "FAILED"
    assert report["errors"]


# --- the responsibility split is recorded in the module ------------------

def test_the_module_declares_the_responsibility_split():
    src = (REPO / "portfolio_automation/release/scheduler.py").read_text(encoding="utf-8")
    flat = " ".join(src.split())
    assert "systemd-analyze verify" in flat
    assert "SYSTEMD_UNIT_VALIDITY" in flat and "SCHEDULER_ALIGNMENT" in flat
    # And the validity emulation is really gone, not merely unused.
    for gone in ("SERVICE_TYPES", "MULTI_EXECSTART_TYPES", "RemainAfterExit",
                 "SuccessAction", "DEFAULT_SERVICE_TYPE"):
        assert gone not in src, f"{gone} survived the scope cut"


def test_scheduler_still_declares_path_alignment_only_scope():
    surfaces = S.parse_systemd_unit(MISSION22_DASHBOARD_UNIT, origin="systemd:dash")
    assert S.certify_scheduler_identity(
        surfaces, release_root=S.CURRENT_POINTER)["scope"] == "path_alignment_only"


# ---------------------------------------------------------------------------
# Fresh-review findings against the reduced candidate. Both are path-identity
# defects, not validity emulation: each changes which HOST path the certifier
# believes production executes.
# ---------------------------------------------------------------------------

REL = "/opt/stockbot/current"


def test_root_directory_start_only_leaves_non_start_commands_on_the_host():
    """RootDirectoryStartOnly=yes chroots ExecStart ONLY (systemd.service(5)).

    ExecStartPre/Post, ExecReload, ExecStop and ExecStopPost keep resolving on
    the host, so applying the root to them computes a host path that
    production never executes.
    """
    surfaces = S.parse_systemd_unit(
        "[Service]\n"
        "RootDirectory=/srv/jail\n"
        "RootDirectoryStartOnly=yes\n"
        f"ExecStart={REL}/scripts/run.sh\n"
        f"ExecStop={REL}/scripts/stop.sh\n",
        origin="systemd:x.service",
    )
    by_raw = {surface.raw: surface for surface in surfaces}
    start = by_raw[f"{REL}/scripts/run.sh"]
    stop = by_raw[f"{REL}/scripts/stop.sh"]

    assert start.root_directory == "/srv/jail"
    assert stop.root_directory is None, "a non-start command is not chrooted"
    assert start._in_root(start.executable) == f"/srv/jail{REL}/scripts/run.sh"
    assert stop._in_root(stop.executable) == f"{REL}/scripts/stop.sh"
    # The stop command genuinely runs release code and must certify as such.
    assert stop.resolves_to_release(release_root=REL)


@pytest.mark.parametrize("value", ["no", "false", "0", "off"])
def test_root_directory_start_only_narrows_only_on_an_explicit_true(value):
    """A parseable false, or no assignment at all, leaves the root applied.

    The original rationale here claimed the two error directions were
    asymmetric — that wrongly applying a root was merely a false reject. That
    is wrong, and review caught it: ``_in_root`` rewrites a legacy host path
    into one under the release root, so wrongly applying a root LAUNDERS legacy
    code into release-looking code. Both directions can false-approve, which is
    why the effective value is now modelled from systemd's real semantics
    rather than approximated. See ``test_..._unparseable_boolean_...`` below.
    """
    surfaces = S.parse_systemd_unit(
        "[Service]\n"
        "RootDirectory=/srv/jail\n"
        f"RootDirectoryStartOnly={value}\n"
        f"ExecStop={REL}/scripts/stop.sh\n",
        origin="systemd:x.service",
    )
    stop = surfaces[0]
    assert stop.root_directory == "/srv/jail"
    assert not stop.resolves_to_release(release_root=REL)


def test_expected_origins_reaches_the_real_certification_entry_point():
    """A guard only direct callers can reach is a guard production never runs.

    ``certify_release_identity`` is the entry point certification uses; if it
    cannot forward ``expected_origins`` then a known production service that
    contributes no executable surface is silently absorbed into a PASS.
    """
    surfaces = S.parse_systemd_unit(
        f"[Service]\nExecStart={REL}/scripts/run.sh\n",
        origin="systemd:stockbot-daily.service",
    )
    pointer_ok = {"status": "OK", "errors": []}

    result = S.certify_release_identity(
        surfaces, pointer_result=pointer_ok, release_root=REL,
        expected_origins=("systemd:stockbot-daily.service",
                          "systemd:stockbot-dashboard.service"),
    )

    assert result["status"] == "FAILED", "silent absence must not certify"
    missing = result["scheduler"]["missing_expected"]
    assert "systemd:stockbot-dashboard.service" in missing
    assert any("no executable surface" in e for e in result["errors"])

    # And it still passes when every expected service is present.
    ok = S.certify_release_identity(
        surfaces, pointer_result=pointer_ok, release_root=REL,
        expected_origins=("systemd:stockbot-daily.service",),
    )
    assert ok["status"] == "OK"



def test_root_directory_start_only_unparseable_boolean_is_ignored_not_reset():
    """systemd IGNORES an unparseable boolean, keeping the prior value.

    Verified by asking the installed systemd 255 rather than reading about it::

        RootDirectoryStartOnly=yes + =garbage -> yes
        RootDirectoryStartOnly=yes + =        -> yes   (blank does NOT reset)
        RootDirectoryStartOnly=yes + =no      -> no

    Clobbering the effective value to False re-applies the chroot to a non-start
    command, and because ``_in_root`` rewrites a legacy host path into one under
    the release root, that command would then falsely certify as release-aligned.
    """
    legacy_stop = "/opt/stockbot/legacy-checkout/scripts/stop.sh"

    for override in ("garbage", "", "   "):
        surfaces = S.parse_systemd_unit(
            "[Service]\n"
            f"RootDirectory={REL}\n"
            "RootDirectoryStartOnly=yes\n"
            f"RootDirectoryStartOnly={override}\n"
            f"ExecStop={legacy_stop}\n",
            origin="systemd:x.service",
        )
        stop = surfaces[0]
        assert stop.root_directory is None, (
            f"override {override!r} must be ignored, leaving the start-only "
            f"narrowing in force"
        )
        # The stop command really runs on the host at a legacy path.
        assert stop._in_root(stop.executable) == legacy_stop
        assert not stop.resolves_to_release(release_root=REL), (
            "laundering a legacy host path under the release root would be a "
            "false approve"
        )


def test_root_directory_start_only_parseable_false_does_override():
    """A *valid* false must still take effect — ignoring is only for garbage."""
    surfaces = S.parse_systemd_unit(
        "[Service]\n"
        f"RootDirectory={REL}\n"
        "RootDirectoryStartOnly=yes\n"
        "RootDirectoryStartOnly=no\n"
        "ExecStop=/legacy/stop.sh\n",
        origin="systemd:x.service",
    )
    assert surfaces[0].root_directory == REL


def test_transitional_allowlist_does_not_launder_a_chrooted_binary():
    """The allowlist names HOST binaries and must be applied after chroot.

    Under ``RootDirectory=/opt/stockbot`` the executed binary is
    ``/opt/stockbot/usr/local/bin/cloudflared`` — inside the legacy checkout,
    not the approved host binary. Matching the lexical executable let the
    allowlist short-circuit the entire code-path check.
    """
    chrooted = S.parse_systemd_unit(
        "[Service]\n"
        "RootDirectory=/opt/stockbot\n"
        "ExecStart=/usr/local/bin/cloudflared run\n",
        origin="systemd:cloudflared.service",
    )[0]

    assert not chrooted.is_system_transitional, "chroot changes the binary"
    assert not chrooted.resolves_to_release(release_root=REL)
    assert chrooted.legacy_paths(release_root=REL), "legacy binding must surface"

    result = S.certify_scheduler_identity([chrooted], release_root=REL)
    assert result["status"] == "FAILED"


def test_transitional_allowlist_still_applies_without_a_root_directory():
    """No regression: an un-chrooted transitional binary stays allowlisted."""
    plain = S.parse_systemd_unit(
        "[Service]\nExecStart=/usr/local/bin/cloudflared run\n",
        origin="systemd:cloudflared.service",
    )[0]
    assert plain.is_system_transitional
    assert plain.resolves_to_release(release_root=REL)
