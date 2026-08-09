"""Secret-boundary tests: prove disallowed data cannot enter an export.

These are the tests that matter most in this subsystem. The snapshot is designed
to eventually leave the production host, so a leak here is not a bug in a report
— it is a credential on another machine. Each test therefore asserts a *refusal*,
not merely an absence.

No test in this file reads a real secret. The boundary is structural (name +
resolved location), so proving it works never requires handling the thing it
protects.
"""
from __future__ import annotations

import pytest

from portfolio_automation.agent_export import allowlist as al
from portfolio_automation.agent_export.allowlist import (
    ARTIFACT_ALLOWLIST, DECLARED_EXCLUSIONS, ExclusionReason, SecretBoundaryViolation,
    forbidden_reason, resolve_source_path,
)

# Filenames the contract names explicitly, plus the usual suspects.
SECRET_FILENAMES = [
    ".env", ".env.template", ".env.bak-20260729T142849Z", ".envrc",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    "auth.json", "credentials.json", "secrets.json", "token.json",
    "client_secret.json", "service-account.json",
    "server.pem", "private.key", "cert.pfx", "keystore.jks",
    ".netrc", ".git-credentials", ".npmrc", ".pypirc", "kubeconfig",
    "cookies.txt", "portfolio.db", "sim_governance_watchlist.db",
    "config.json", "aws_secret_access_key.txt", "my_api_key.json",
]


@pytest.mark.parametrize("name", SECRET_FILENAMES)
def test_secret_filenames_are_refused(name):
    """Every known-sensitive basename is rejected by the name rule alone."""
    assert forbidden_reason(name) is not None, f"{name!r} was NOT refused"


@pytest.mark.parametrize("name", [
    "decision_plan.json", "daily_memo.md", "portfolio_snapshot.json",
    "run_manifest.json", "artifact_registry_status.json", "manifest.json",
])
def test_legitimate_artifact_names_are_allowed(name):
    """The rules must not be so broad that real artifacts are caught."""
    assert forbidden_reason(name) is None


@pytest.mark.parametrize("component", [".", "..", "", ".git", ".ssh", ".aws", "logs", "data"])
def test_traversal_and_sensitive_directories_are_refused(component):
    assert forbidden_reason(component) is not None


# ---------------------------------------------------------------------------
# Path resolution: traversal, absolute paths, symlink escape
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "outputs" / "latest").mkdir(parents=True)
    (root / "outputs" / "latest" / "decision_plan.json").write_text("{}", encoding="utf-8")
    # A credential file that genuinely exists at the repo root, so the tests
    # prove refusal of a REACHABLE target rather than of a missing file.
    (root / ".env").write_text("FMP_API_KEY=not-a-real-key\n", encoding="utf-8")
    (root / ".ssh").mkdir()
    (root / ".ssh" / "id_ed25519").write_text("not-a-real-key\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("candidate", [
    "../../etc/passwd",
    "outputs/latest/../../.env",
    "outputs/../.env",
    "outputs/latest/../../../root/.ssh/id_ed25519",
])
def test_path_traversal_is_refused(repo, candidate):
    with pytest.raises(SecretBoundaryViolation):
        resolve_source_path(repo, candidate)


@pytest.mark.parametrize("candidate", [
    "/etc/passwd", "/opt/stockbot/.env", "C:/Windows/System32/config",
])
def test_absolute_paths_are_refused(repo, candidate):
    with pytest.raises(SecretBoundaryViolation):
        resolve_source_path(repo, candidate)


@pytest.mark.parametrize("candidate", [
    ".env", "config.json", "data/portfolio.db", "logs/run.log",
    "outputs/latest/.env", "scripts/run_daily_safe.sh",
])
def test_paths_outside_permitted_roots_are_refused(repo, candidate):
    with pytest.raises(SecretBoundaryViolation):
        resolve_source_path(repo, candidate)


def test_symlink_escaping_to_repo_secret_is_refused(repo):
    """An innocently-named symlink pointing at .env must not be followed out."""
    link = repo / "outputs" / "latest" / "harmless.json"
    link.symlink_to(repo / ".env")
    with pytest.raises(SecretBoundaryViolation) as exc:
        resolve_source_path(repo, "outputs/latest/harmless.json")
    assert "escapes" in str(exc.value) or "outside" in str(exc.value)


def test_symlink_escaping_outside_repo_is_refused(repo, tmp_path):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("sensitive\n", encoding="utf-8")
    link = repo / "outputs" / "latest" / "innocuous.json"
    link.symlink_to(outside)
    with pytest.raises(SecretBoundaryViolation):
        resolve_source_path(repo, "outputs/latest/innocuous.json")


def test_symlink_to_private_key_is_refused(repo):
    link = repo / "outputs" / "latest" / "plan_backup.json"
    link.symlink_to(repo / ".ssh" / "id_ed25519")
    with pytest.raises(SecretBoundaryViolation):
        resolve_source_path(repo, "outputs/latest/plan_backup.json")


def test_directory_target_is_refused(repo):
    (repo / "outputs" / "latest" / "subdir").mkdir()
    with pytest.raises(SecretBoundaryViolation):
        resolve_source_path(repo, "outputs/latest/subdir")


def test_absent_artifact_is_missing_not_hostile(repo):
    """A simply-not-produced artifact is FileNotFoundError, not a breach.

    The distinction matters: missing-optional degrades to AMBER, whereas a
    boundary violation must abort the build. Collapsing them would let a real
    breach be silently reported as "artifact unavailable".
    """
    with pytest.raises(FileNotFoundError):
        resolve_source_path(repo, "outputs/latest/never_produced.json")


def test_valid_artifact_resolves(repo):
    resolved = resolve_source_path(repo, "outputs/latest/decision_plan.json")
    assert resolved.is_file()
    assert resolved.name == "decision_plan.json"


# ---------------------------------------------------------------------------
# The allowlist itself must obey its own rules
# ---------------------------------------------------------------------------


def test_allowlist_entries_all_pass_the_name_rules():
    """No production allowlist entry may trip the forbidden-name rules.

    If someone adds an artifact called e.g. ``api_key_status.json`` this fails
    at test time rather than at export time.
    """
    for entry in ARTIFACT_ALLOWLIST:
        al.assert_name_allowed(entry.source_path)


def test_allowlist_entries_live_under_permitted_roots():
    for entry in ARTIFACT_ALLOWLIST:
        assert any(
            entry.source_path.startswith(root + "/") for root in al.PERMITTED_SOURCE_ROOTS
        ), f"{entry.source_path} is not under a permitted root"


def test_allowlist_logical_names_are_unique():
    names = [e.logical_name for e in ARTIFACT_ALLOWLIST]
    assert len(names) == len(set(names)), "duplicate logical_name in allowlist"


def test_allowlist_snapshot_paths_do_not_collide():
    """Two entries must not map onto the same file inside a snapshot."""
    from pathlib import Path
    seen = {}
    for entry in ARTIFACT_ALLOWLIST:
        key = f"{entry.category}/{Path(entry.source_path).name}"
        assert key not in seen, f"{entry.logical_name} collides with {seen.get(key)} at {key}"
        seen[key] = entry.logical_name


def test_allowlist_categories_are_declared():
    for entry in ARTIFACT_ALLOWLIST:
        assert entry.category in al.CATEGORIES, f"undocumented category {entry.category!r}"


def test_no_jsonl_ledger_is_exported():
    """Writable append-only ledgers stay behind; only derived summaries cross."""
    for entry in ARTIFACT_ALLOWLIST:
        assert not entry.source_path.endswith(".jsonl"), (
            f"{entry.logical_name} exports a mutable ledger; export its derived "
            "summary instead"
        )


def test_broker_account_artifacts_are_not_exported():
    """Raw broker state carries account identifiers and must stay behind."""
    for entry in ARTIFACT_ALLOWLIST:
        assert "schwab" not in entry.source_path.lower(), (
            f"{entry.logical_name} would export raw broker account data"
        )


def test_declared_exclusions_cover_the_named_sensitive_classes():
    reasons = {x.reason for x in DECLARED_EXCLUSIONS}
    assert ExclusionReason.SECRET in reasons
    assert ExclusionReason.CREDENTIAL in reasons
    assert ExclusionReason.PII in reasons
    assert ExclusionReason.MUTABLE_INTERNAL_STATE in reasons
    assert ExclusionReason.NOT_AGENT_RELEVANT in reasons


def test_exclusions_record_patterns_not_values():
    """Recording that a secret exists must never disclose it.

    Each exclusion carries a path pattern and a prose reason — never a value —
    so the manifest can state 'credentials were withheld' without the exporter
    ever having read one.
    """
    for exclusion in DECLARED_EXCLUSIONS:
        blob = f"{exclusion.source_pattern} {exclusion.detail}"
        assert "=" not in exclusion.source_pattern, "pattern looks like a key=value pair"
        assert "BEGIN " not in blob, "exclusion detail embeds key material"


def test_allowlist_producers_are_attributable():
    """Every entry must be attributable: registry-governed, or explicitly declared.

    Provenance is the point of the export. An artifact whose producer nobody can
    name is not analysable evidence.
    """
    try:
        from portfolio_automation.artifact_registry import load_registry
        registry_paths = {
            (row or {}).get("path")
            for row in (load_registry().get("artifacts") or {}).values()
        }
    except Exception:  # pragma: no cover - registry unavailable
        pytest.skip("artifact registry unavailable")

    for entry in ARTIFACT_ALLOWLIST:
        attributable = entry.source_path in registry_paths or bool(entry.producer)
        assert attributable, (
            f"{entry.logical_name} is neither in artifact_registry.yaml nor declares "
            "an explicit producer"
        )
