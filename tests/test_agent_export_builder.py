"""Builder tests: determinism, fail-closed refusal, atomicity, immutability.

The governing property under test is: **a snapshot directory exists if and only
if it is complete and verified.** Every failure path is asserted twice — that it
raised, and that it left nothing behind that could later be mistaken for a valid
export.
"""
from __future__ import annotations

import json

import pytest

from portfolio_automation.agent_export import BUILD_PREFIX, MANIFEST_FILENAME
from portfolio_automation.agent_export.allowlist import (
    AllowlistEntry, SecretBoundaryViolation,
)
from portfolio_automation.agent_export.builder import (
    SnapshotBuildError, list_snapshots, read_latest_pointer, snapshots_root,
)
from portfolio_automation.agent_export.manifest import AMBER, GREEN
from portfolio_automation.agent_export.validator import validate_agent_snapshot
from tests.agent_export_fixtures import (
    OPTIONAL_NAME, REQUIRED_PATH, TEST_ALLOWLIST, add_commit, build, make_fake_repo,
)


def _build_dirs(root):
    snaps = snapshots_root(root)
    if not snaps.is_dir():
        return []
    return [p for p in snaps.iterdir() if p.name.startswith(BUILD_PREFIX)]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_build_produces_a_valid_snapshot(tmp_path):
    root = make_fake_repo(tmp_path)
    result = build(root)

    assert result.created is True
    assert result.snapshot_dir.is_dir()
    manifest = validate_agent_snapshot(result.snapshot_dir, required_entries=TEST_ALLOWLIST)
    assert manifest["snapshot_id"] == result.snapshot_id
    assert manifest["finalized"] is True
    assert manifest["counts"]["artifacts"] == len(TEST_ALLOWLIST)
    assert result.health_status == GREEN


def test_snapshot_id_encodes_run_and_commit(tmp_path):
    root = make_fake_repo(tmp_path)
    result = build(root)
    manifest = result.manifest
    sha = manifest["production"]["production_git_sha"]

    assert result.snapshot_id.startswith("2026-08-08_daily_official__")
    assert result.snapshot_id.endswith(sha[:12])
    assert len(sha) == 40, "production_git_sha must be the full resolved commit"
    assert manifest["production"]["production_git_sha_source"] == "run_manifest"


def test_artifact_bytes_are_copied_faithfully(tmp_path):
    root = make_fake_repo(tmp_path)
    result = build(root)
    for record in result.manifest["artifacts"]:
        source = (root / record["source_path"]).read_bytes()
        copied = (result.snapshot_dir / record["snapshot_path"]).read_bytes()
        assert copied == source, f"{record['name']} was not copied faithfully"


def test_governance_invariants_are_stamped(tmp_path):
    """The snapshot must assert, in its own manifest, that it grants no authority."""
    root = make_fake_repo(tmp_path)
    manifest = build(root).manifest
    assert manifest["observe_only"] is True
    assert manifest["feeds_decision_engine"] is False
    assert manifest["grants_production_authority"] is False


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_identical_inputs_produce_identical_fingerprint_across_clocks(tmp_path):
    """Only created_at may vary between two exports of the same frozen inputs."""
    root = make_fake_repo(tmp_path)
    first = build(root, created_at="2026-08-09T00:00:00+00:00", dry_run=True)
    second = build(root, created_at="2026-09-01T12:34:56+00:00", dry_run=True)

    assert first.manifest["created_at"] != second.manifest["created_at"]
    assert first.manifest["snapshot_sha256"] == second.manifest["snapshot_sha256"]
    assert first.manifest["content_sha256"] == second.manifest["content_sha256"]
    assert first.snapshot_id == second.snapshot_id


def test_changing_an_artifact_changes_the_fingerprint(tmp_path):
    """The determinism guarantee must not be vacuous."""
    root = make_fake_repo(tmp_path)
    before = build(root, dry_run=True).manifest["snapshot_sha256"]
    (root / REQUIRED_PATH).write_text('{"changed": true}', encoding="utf-8")
    after = build(root, dry_run=True).manifest["snapshot_sha256"]
    assert before != after


def test_reexport_is_idempotent_even_after_the_repo_moves_on(tmp_path):
    """Editing source after a run must not make its re-export look like tampering.

    The VPS doubles as a dev environment, so HEAD and the working tree routinely
    move between the 09:00 cron run and a later export. Content identity is a
    function of (run, code, artifacts) only, so the re-export stays idempotent
    while the changed observations are still recorded in export_context.
    """
    root = make_fake_repo(tmp_path)
    first = build(root)
    assert first.created is True

    # Simulate exactly what happens on the live box: new commits land and the
    # working tree gains uncommitted edits, all AFTER the run finished.
    add_commit(root, "feature.py", "# later work\n")
    (root / "README.md").write_text("edited after the run\n", encoding="utf-8")

    second = build(root, created_at="2026-08-20T00:00:00+00:00")

    assert second.created is False, "re-export raised a false integrity alarm"
    assert second.snapshot_id == first.snapshot_id
    assert first.manifest["content_sha256"] == second.manifest["content_sha256"]


def test_export_context_records_what_content_identity_ignores(tmp_path):
    """The volatile facts are still captured — excluded from identity, not dropped."""
    root = make_fake_repo(tmp_path)
    add_commit(root, "feature.py", "# later work\n")
    (root / "README.md").write_text("uncommitted edit\n", encoding="utf-8")

    manifest = build(root).manifest
    context = manifest["export_context"]

    assert context["code_moved_since_run"] is True
    assert "head_moved_since_run" in context["degradations"]
    assert "working_tree_code_modified" in context["degradations"]
    assert context["working_tree"]["code_clean"] is False
    assert "README.md" in context["working_tree"]["code_modified_paths"]

    # ...and none of it contaminates the RUN's health verdict.
    assert manifest["health"]["status"] == GREEN
    assert not any("provenance" in w for w in manifest["health"]["warnings"])


def test_working_tree_records_paths_never_contents(tmp_path):
    """A path list is safe to publish; a diff could contain anything."""
    root = make_fake_repo(tmp_path)
    (root / "README.md").write_text("SUPERSECRET_SENTINEL_VALUE\n", encoding="utf-8")
    manifest = build(root).manifest
    blob = json.dumps(manifest)
    assert "SUPERSECRET_SENTINEL_VALUE" not in blob
    assert "README.md" in manifest["export_context"]["working_tree"]["code_modified_paths"]


def test_artifact_churn_is_not_treated_as_code_contamination(tmp_path):
    """A run writing artifacts is the job, not a dirty tree."""
    root = make_fake_repo(tmp_path)
    import subprocess
    subprocess.run(["git", "add", "-A", "outputs"], cwd=root, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "track outputs"], cwd=root,
                   capture_output=True)
    (root / REQUIRED_PATH).write_text('{"rewritten": true}', encoding="utf-8")

    manifest = build(root).manifest
    tree = manifest["export_context"]["working_tree"]
    assert tree["artifact_churn_count"] >= 1
    assert tree["code_clean"] is True
    assert "working_tree_code_modified" not in manifest["export_context"]["degradations"]


def test_rebuild_of_unchanged_run_is_idempotent(tmp_path):
    """A second export of the same run must not rewrite the frozen directory."""
    root = make_fake_repo(tmp_path)
    first = build(root)
    manifest_path = first.snapshot_dir / MANIFEST_FILENAME
    original_bytes = manifest_path.read_bytes()
    original_mtime = manifest_path.stat().st_mtime_ns

    second = build(root, created_at="2026-12-25T00:00:00+00:00")

    assert second.created is False
    assert second.snapshot_id == first.snapshot_id
    assert manifest_path.read_bytes() == original_bytes
    assert manifest_path.stat().st_mtime_ns == original_mtime
    assert len(list_snapshots(root)) == 1


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


def test_missing_required_artifact_fails_closed(tmp_path):
    root = make_fake_repo(tmp_path, include_required=False)
    with pytest.raises(SnapshotBuildError, match="required artifact"):
        build(root)
    assert list_snapshots(root) == []
    assert _build_dirs(root) == []


def test_missing_optional_artifact_degrades_but_still_exports(tmp_path):
    root = make_fake_repo(tmp_path, include_optional=False)
    result = build(root)

    assert result.created is True
    assert OPTIONAL_NAME in result.manifest["missing_optional"]
    assert result.health_status == AMBER
    assert any("optional_artifact_missing" in w
               for w in result.manifest["health"]["warnings"])
    validate_agent_snapshot(result.snapshot_dir, required_entries=TEST_ALLOWLIST)


def test_incomplete_run_is_refused(tmp_path):
    """A run still at status=running must never be frozen as a finished run."""
    root = make_fake_repo(tmp_path, run_status="running")
    with pytest.raises(SnapshotBuildError, match="not 'complete'"):
        build(root)
    assert list_snapshots(root) == []


def test_failed_run_is_refused(tmp_path):
    root = make_fake_repo(tmp_path, run_status="failed")
    with pytest.raises(SnapshotBuildError, match="not 'complete'"):
        build(root)
    assert list_snapshots(root) == []


def test_absent_run_manifest_is_refused(tmp_path):
    root = make_fake_repo(tmp_path)
    (root / "outputs" / "policy" / "run_manifest.json").unlink()
    with pytest.raises(SnapshotBuildError, match="run_id"):
        build(root)
    assert list_snapshots(root) == []


def test_red_run_health_is_refused_and_leaves_no_residue(tmp_path):
    """A RED run must not be published, and its aborted build must vanish.

    This failure fires *after* artifacts have been copied into the temp dir, so
    it is the case that proves cleanup actually runs.
    """
    root = make_fake_repo(tmp_path, registry_status="red")
    with pytest.raises(SnapshotBuildError, match="RED"):
        build(root)
    assert list_snapshots(root) == []
    assert _build_dirs(root) == [], "aborted build directory was left behind"


def test_secret_in_the_allowlist_aborts_the_build(tmp_path):
    """Even a mis-authored allowlist cannot smuggle a credential into a snapshot."""
    root = make_fake_repo(tmp_path)
    (root / ".env").write_text("FMP_API_KEY=not-a-real-key\n", encoding="utf-8")
    poisoned = TEST_ALLOWLIST + (
        AllowlistEntry("leak", "outputs/latest/../../.env", "context", producer="x"),
    )
    with pytest.raises(SecretBoundaryViolation):
        build(root, allowlist=poisoned)
    assert list_snapshots(root) == []
    assert _build_dirs(root) == []


# ---------------------------------------------------------------------------
# Immutability of a finalised snapshot
# ---------------------------------------------------------------------------


def test_same_id_with_different_content_fails_closed_and_preserves_the_original(tmp_path):
    """A completed run's artifacts changing underneath us is an integrity anomaly."""
    root = make_fake_repo(tmp_path)
    first = build(root)
    frozen = (first.snapshot_dir / "artifacts" / "core_decision" / "decision_plan.json").read_bytes()

    # Same run_id, same commit -> same snapshot_id, but the artifact changed.
    (root / REQUIRED_PATH).write_text('{"tampered": true}', encoding="utf-8")

    with pytest.raises(SnapshotBuildError, match="DIFFERENT content"):
        build(root)

    still = (first.snapshot_dir / "artifacts" / "core_decision" / "decision_plan.json").read_bytes()
    assert still == frozen, "the frozen snapshot was modified"
    assert _build_dirs(root) == []
    validate_agent_snapshot(first.snapshot_dir, required_entries=TEST_ALLOWLIST)


def test_unreadable_existing_manifest_is_not_overwritten(tmp_path):
    root = make_fake_repo(tmp_path)
    first = build(root)
    (first.snapshot_dir / MANIFEST_FILENAME).write_text("{ not json", encoding="utf-8")
    with pytest.raises(SnapshotBuildError, match="refusing to overwrite"):
        build(root)


# ---------------------------------------------------------------------------
# Dry run + partial builds
# ---------------------------------------------------------------------------


def test_dry_run_promotes_nothing(tmp_path):
    root = make_fake_repo(tmp_path)
    result = build(root, dry_run=True)

    assert result.dry_run is True
    assert result.created is False
    assert list_snapshots(root) == []
    assert _build_dirs(root) == []
    assert read_latest_pointer(root) is None
    assert result.manifest["snapshot_sha256"]


def test_partial_build_directory_is_never_treated_as_a_snapshot(tmp_path):
    """A leftover build dir (killed process) must be invisible and unusable."""
    root = make_fake_repo(tmp_path)
    build(root)
    snaps = snapshots_root(root)
    partial = snaps / f"{BUILD_PREFIX}abandoned"
    (partial / "artifacts" / "core_decision").mkdir(parents=True)
    (partial / "artifacts" / "core_decision" / "decision_plan.json").write_text(
        "{}", encoding="utf-8")

    assert partial.name not in list_snapshots(root)
    from portfolio_automation.agent_export.validator import ValidationError
    with pytest.raises(ValidationError, match="missing"):
        validate_agent_snapshot(partial, required_entries=TEST_ALLOWLIST)


# ---------------------------------------------------------------------------
# latest pointer
# ---------------------------------------------------------------------------


def test_latest_is_a_pointer_not_a_copy(tmp_path):
    """`latest` must not duplicate artifacts — one copy, explicit indirection."""
    root = make_fake_repo(tmp_path)
    result = build(root)
    export_dir = root / "outputs" / "agent_export"

    pointer = read_latest_pointer(root)
    assert pointer is not None
    assert pointer["snapshot_id"] == result.snapshot_id
    assert pointer["snapshot_sha256"] == result.manifest["snapshot_sha256"]

    # Only latest.json and snapshots/ exist at the export root: no latest/ dir.
    assert sorted(p.name for p in export_dir.iterdir()) == ["latest.json", "snapshots"]
    assert not (export_dir / "latest").exists()


def test_pointer_tracks_the_newest_run(tmp_path):
    root = make_fake_repo(tmp_path)
    first = build(root)

    # A second, later run at the same commit: new run_id -> new snapshot_id.
    manifest_path = root / "outputs" / "policy" / "run_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["run_id"] = "2026-08-09_daily_official"
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    second = build(root, created_at="2026-08-10T00:00:00+00:00")

    assert second.snapshot_id != first.snapshot_id
    assert len(list_snapshots(root)) == 2
    assert read_latest_pointer(root)["snapshot_id"] == second.snapshot_id
    # The earlier snapshot is untouched and still valid.
    validate_agent_snapshot(first.snapshot_dir, required_entries=TEST_ALLOWLIST)
