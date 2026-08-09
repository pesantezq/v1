"""Validator + shadow-consistency tests.

Validation is the consumer's trust gate, so these tests are written from the
attacker's side: for each recorded fact, mutate it and assert the snapshot stops
verifying. A validator that only proves good snapshots pass is not a gate.
"""
from __future__ import annotations

import json

import pytest

from portfolio_automation.agent_export import MANIFEST_FILENAME
from portfolio_automation.agent_export.consistency import (
    ShadowStatus, compare_against_snapshot, compare_shadow_to_production,
    git_ancestry_probe,
)
from portfolio_automation.agent_export.manifest import (
    compute_content_sha256, compute_snapshot_sha256,
)
from portfolio_automation.agent_export.validator import (
    ValidationError, is_valid_snapshot, validate_agent_snapshot,
)
from tests.agent_export_fixtures import (
    TEST_ALLOWLIST, add_commit, build, make_fake_repo,
)


@pytest.fixture
def snapshot(tmp_path):
    root = make_fake_repo(tmp_path)
    result = build(root)
    return root, result


def _validate(snapshot_dir):
    return validate_agent_snapshot(snapshot_dir, required_entries=TEST_ALLOWLIST)


def _reseal(manifest):
    """Recompute both fingerprints — what a thorough forger would have to do."""
    manifest["content_sha256"] = ""
    manifest["snapshot_sha256"] = ""
    manifest["content_sha256"] = compute_content_sha256(manifest)
    manifest["snapshot_sha256"] = compute_snapshot_sha256(manifest)


def _rewrite_manifest(snapshot_dir, mutate):
    path = snapshot_dir / MANIFEST_FILENAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def test_freshly_built_snapshot_validates(snapshot):
    _, result = snapshot
    manifest = _validate(result.snapshot_dir)
    assert manifest["snapshot_id"] == result.snapshot_id
    ok, error = is_valid_snapshot(result.snapshot_dir, required_entries=TEST_ALLOWLIST)
    assert ok and error is None


# ---------------------------------------------------------------------------
# Content tampering
# ---------------------------------------------------------------------------


def test_single_byte_artifact_tamper_is_detected(snapshot):
    """Alter one byte inside an exported artifact; the hash check must catch it."""
    _, result = snapshot
    target = result.snapshot_dir / "artifacts" / "core_decision" / "decision_plan.json"
    data = bytearray(target.read_bytes())
    data[0:1] = b" " if data[0:1] != b" " else b"\t"  # same length, different byte
    target.write_bytes(bytes(data))

    with pytest.raises(ValidationError, match="SHA-256 mismatch"):
        _validate(result.snapshot_dir)


def test_artifact_length_change_is_detected(snapshot):
    _, result = snapshot
    target = result.snapshot_dir / "artifacts" / "health" / "daily_run_status.json"
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValidationError, match="size mismatch"):
        _validate(result.snapshot_dir)


def test_deleted_artifact_is_detected(snapshot):
    _, result = snapshot
    (result.snapshot_dir / "artifacts" / "outcome_learning" /
     "confidence_calibration.json").unlink()
    with pytest.raises(ValidationError, match="missing from the snapshot"):
        _validate(result.snapshot_dir)


# ---------------------------------------------------------------------------
# Manifest tampering — every recorded fact is covered by the fingerprint
# ---------------------------------------------------------------------------


def test_forging_the_snapshot_id_is_detected(snapshot):
    _, result = snapshot
    _rewrite_manifest(result.snapshot_dir,
                      lambda m: m.__setitem__("snapshot_id", "forged-id"))
    with pytest.raises(ValidationError):
        _validate(result.snapshot_dir)


def test_forging_the_run_id_is_detected(snapshot):
    """Re-attributing a snapshot to a different run must invalidate it."""
    _, result = snapshot
    _rewrite_manifest(result.snapshot_dir,
                      lambda m: m["production"].__setitem__("run_id", "2099-01-01_fake"))
    with pytest.raises(ValidationError, match="snapshot_sha256 mismatch"):
        _validate(result.snapshot_dir)


def test_rewriting_an_artifact_hash_in_the_manifest_is_detected(snapshot):
    """Swapping a recorded hash breaks the fingerprint even before rehashing."""
    _, result = snapshot
    _rewrite_manifest(
        result.snapshot_dir,
        lambda m: m["artifacts"][0].__setitem__("sha256", "0" * 64),
    )
    with pytest.raises(ValidationError, match="snapshot_sha256 mismatch"):
        _validate(result.snapshot_dir)


def test_rewriting_provenance_is_detected(snapshot):
    """Forging which commit produced the run must invalidate the snapshot.

    A digest covering only file hashes would miss this — every file would still
    verify while the snapshot lied about its own origin.
    """
    _, result = snapshot
    _rewrite_manifest(
        result.snapshot_dir,
        lambda m: m["production"].__setitem__("production_git_sha", "f" * 40),
    )
    with pytest.raises(ValidationError, match="snapshot_sha256 mismatch"):
        _validate(result.snapshot_dir)


def test_rewriting_health_verdict_is_detected(tmp_path):
    """Upgrading a degraded snapshot's verdict to GREEN must invalidate it.

    Built from an AMBER snapshot (an optional artifact was absent) and forged to
    GREEN — the realistic attack is hiding a warning, not inventing one.
    """
    root = make_fake_repo(tmp_path, include_optional=False)
    result = build(root)
    assert result.manifest["health"]["status"] == "AMBER"

    _rewrite_manifest(
        result.snapshot_dir, lambda m: m["health"].__setitem__("status", "GREEN"))
    with pytest.raises(ValidationError, match="snapshot_sha256 mismatch"):
        _validate(result.snapshot_dir)


def test_recomputing_the_digest_after_tampering_still_fails_other_checks(snapshot):
    """A thorough forger who also refreshes the digest is caught elsewhere.

    The fingerprint is not the only defence: artifact hashes are re-derived from
    the files themselves, so consistent-but-false manifests still fail.
    """
    _, result = snapshot

    def forge(manifest):
        manifest["artifacts"][0]["sha256"] = "0" * 64
        _reseal(manifest)

    _rewrite_manifest(result.snapshot_dir, forge)
    with pytest.raises(ValidationError, match="SHA-256 mismatch"):
        _validate(result.snapshot_dir)


def test_refreshing_only_one_digest_is_detected(snapshot):
    """Both fingerprints must agree; refreshing one leaves the other inconsistent."""
    _, result = snapshot

    def half_forge(manifest):
        manifest["artifacts"][0]["sha256"] = "0" * 64
        manifest["snapshot_sha256"] = ""
        manifest["snapshot_sha256"] = compute_snapshot_sha256(manifest)
        # content_sha256 deliberately left stale

    _rewrite_manifest(result.snapshot_dir, half_forge)
    with pytest.raises(ValidationError, match="content_sha256 mismatch"):
        _validate(result.snapshot_dir)


def test_export_context_tamper_is_detected(snapshot):
    """export_context is outside the CONTENT digest but still sealed by the tamper digest."""
    _, result = snapshot
    _rewrite_manifest(
        result.snapshot_dir,
        lambda m: m["export_context"].__setitem__("head_git_sha_at_export", "9" * 40),
    )
    with pytest.raises(ValidationError, match="snapshot_sha256 mismatch"):
        _validate(result.snapshot_dir)


def test_unfinalized_manifest_is_rejected(snapshot):
    _, result = snapshot

    def unfinalize(manifest):
        manifest["finalized"] = False
        manifest["snapshot_sha256"] = ""
        manifest["snapshot_sha256"] = compute_snapshot_sha256(manifest)

    _rewrite_manifest(result.snapshot_dir, unfinalize)
    with pytest.raises(ValidationError, match="not marked finalized"):
        _validate(result.snapshot_dir)


@pytest.mark.parametrize("field", [
    "observe_only", "feeds_decision_engine", "grants_production_authority",
])
def test_flipped_governance_invariant_is_rejected(snapshot, field):
    """A snapshot claiming decision authority is refused outright."""
    _, result = snapshot

    def flip(manifest):
        manifest[field] = not manifest[field]
        manifest["snapshot_sha256"] = ""
        manifest["snapshot_sha256"] = compute_snapshot_sha256(manifest)

    _rewrite_manifest(result.snapshot_dir, flip)
    with pytest.raises(ValidationError, match="invariant broken"):
        _validate(result.snapshot_dir)


def test_unsupported_schema_version_is_rejected(snapshot):
    _, result = snapshot

    def bump(manifest):
        manifest["schema_version"] = "99.0"
        manifest["snapshot_sha256"] = ""
        manifest["snapshot_sha256"] = compute_snapshot_sha256(manifest)

    _rewrite_manifest(result.snapshot_dir, bump)
    with pytest.raises(ValidationError, match="unsupported schema_version"):
        _validate(result.snapshot_dir)


def test_corrupt_manifest_json_is_rejected(snapshot):
    _, result = snapshot
    (result.snapshot_dir / MANIFEST_FILENAME).write_text("{ nope", encoding="utf-8")
    with pytest.raises(ValidationError, match="not valid JSON"):
        _validate(result.snapshot_dir)


def test_missing_manifest_is_rejected(snapshot):
    _, result = snapshot
    (result.snapshot_dir / MANIFEST_FILENAME).unlink()
    with pytest.raises(ValidationError, match="missing"):
        _validate(result.snapshot_dir)


# ---------------------------------------------------------------------------
# Structural tampering
# ---------------------------------------------------------------------------


def test_unexpected_file_is_rejected(snapshot):
    """An undeclared file is exactly how something unreviewed crosses the boundary."""
    _, result = snapshot
    (result.snapshot_dir / "artifacts" / "core_decision" / "extra.json").write_text(
        "{}", encoding="utf-8")
    with pytest.raises(ValidationError, match="unexpected file"):
        _validate(result.snapshot_dir)


def test_smuggled_secret_file_is_rejected(snapshot):
    _, result = snapshot
    (result.snapshot_dir / "artifacts" / "credentials.json").write_text(
        "{}", encoding="utf-8")
    with pytest.raises(ValidationError, match="unexpected file|forbidden filename"):
        _validate(result.snapshot_dir)


def test_renamed_snapshot_directory_is_rejected(snapshot):
    """The directory name is part of the snapshot's identity."""
    _, result = snapshot
    moved = result.snapshot_dir.parent / "renamed-snapshot"
    result.snapshot_dir.rename(moved)
    with pytest.raises(ValidationError, match="does not match snapshot_id"):
        _validate(moved)


def test_expected_snapshot_id_mismatch_is_rejected(snapshot):
    _, result = snapshot
    with pytest.raises(ValidationError, match="!= expected"):
        validate_agent_snapshot(
            result.snapshot_dir, expect_snapshot_id="not-this-one",
            required_entries=TEST_ALLOWLIST,
        )


def test_required_artifact_absent_from_manifest_is_rejected(tmp_path):
    """Validation enforces required coverage against the allowlist, not just the manifest."""
    root = make_fake_repo(tmp_path)
    result = build(root)
    from portfolio_automation.agent_export.allowlist import AllowlistEntry
    stricter = TEST_ALLOWLIST + (
        AllowlistEntry("newly_required", "outputs/latest/never_made.json",
                       "health", required=True, producer="future"),
    )
    with pytest.raises(ValidationError, match="required artifact"):
        validate_agent_snapshot(result.snapshot_dir, required_entries=stricter)


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------


def test_validation_does_not_mutate_the_snapshot(snapshot):
    """A consumer verifying a snapshot must not be able to damage it."""
    _, result = snapshot

    def fingerprint():
        entries = {}
        for path in sorted(result.snapshot_dir.rglob("*")):
            if path.is_file():
                stat = path.stat()
                entries[str(path)] = (stat.st_size, stat.st_mtime_ns, path.read_bytes())
        return entries

    before = fingerprint()
    for _ in range(3):
        _validate(result.snapshot_dir)
    assert fingerprint() == before


# ---------------------------------------------------------------------------
# Shadow / production consistency (Phase 9)
# ---------------------------------------------------------------------------


def test_identical_shas_match():
    result = compare_shadow_to_production("a" * 40, "a" * 40)
    assert result["status"] == ShadowStatus.MATCH.value
    assert result["analysis_safe"] is True


def test_abbreviated_sha_still_matches_full_sha():
    """An 8-char run_manifest commit must not read as a divergence."""
    full = "55205f56b319a63ebaac1b09ba9ecdd7882d5047"
    result = compare_shadow_to_production(full[:8], full)
    assert result["status"] == ShadowStatus.MATCH.value


@pytest.mark.parametrize("shadow,production", [
    (None, "a" * 40), ("a" * 40, None), (None, None), ("unknown", "a" * 40),
])
def test_missing_sha_is_unknown_not_a_guess(shadow, production):
    result = compare_shadow_to_production(shadow, production)
    assert result["status"] == ShadowStatus.UNKNOWN.value
    assert result["analysis_safe"] is False


def test_without_an_ancestry_probe_difference_is_unknown():
    result = compare_shadow_to_production("a" * 40, "b" * 40)
    assert result["status"] == ShadowStatus.UNKNOWN.value
    assert result["reason"] == "no_ancestry_probe"


def test_shadow_behind_and_ahead_against_a_real_repo(tmp_path):
    root = make_fake_repo(tmp_path)
    import subprocess
    old = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                         capture_output=True, text=True).stdout.strip()
    new = add_commit(root, "later.txt", "later\n")

    behind = compare_shadow_to_production(old, new, repo_root=root)
    assert behind["status"] == ShadowStatus.SHADOW_BEHIND.value
    assert behind["analysis_safe"] is False

    ahead = compare_shadow_to_production(new, old, repo_root=root)
    assert ahead["status"] == ShadowStatus.SHADOW_AHEAD.value


def test_diverged_histories_report_unknown():
    """Neither commit is an ancestor of the other — not behind, not ahead."""
    result = compare_shadow_to_production(
        "a" * 40, "b" * 40, ancestry=lambda x, y: False)
    assert result["status"] == ShadowStatus.UNKNOWN.value
    assert result["reason"] == "histories_diverged"


def test_commits_absent_locally_report_unknown_not_diverged():
    """'I don't have that commit' must not be reported as a divergence."""
    result = compare_shadow_to_production(
        "a" * 40, "b" * 40, ancestry=lambda x, y: None)
    assert result["status"] == ShadowStatus.UNKNOWN.value
    assert result["reason"] == "commits_not_present_locally"


def test_ancestry_probe_returns_none_for_unknown_commits(tmp_path):
    root = make_fake_repo(tmp_path)
    probe = git_ancestry_probe(root)
    assert probe("a" * 40, "b" * 40) is None


def test_compare_against_snapshot_reads_the_manifest(snapshot):
    root, result = snapshot
    sha = result.manifest["production"]["production_git_sha"]
    assert compare_against_snapshot(result.manifest, sha)["status"] == ShadowStatus.MATCH.value
    assert compare_against_snapshot(result.manifest, None)["status"] == ShadowStatus.UNKNOWN.value
