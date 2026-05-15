"""
Tests for the smoke-test stage wired into ``scripts/preflight.sh``.

The script itself is POSIX bash, so these tests are content checks against
the script source plus runtime checks of the underlying ``tools.smoke_test``
CLI that the script invokes.  They mirror the style of the existing env-
registry tests and the smoke_test tool tests.

Goals:
- Lock in the contract that ``python -m tools.smoke_test --strict`` is
  invoked from preflight, runs *after* the env-registry strict check, and
  runs *before* the FMP compliance stage.
- Lock in the contract that any non-zero exit from the smoke test causes
  preflight to ``fail`` (which exits 1 via ``set -euo pipefail``).
- Verify the CLI propagates exit codes correctly so the shell wiring works.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import smoke_test as tool
from portfolio_automation.artifacts_registry import REGISTRY, artifact_path


# ---------------------------------------------------------------------------
# Repo paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT_SH = REPO_ROOT / "scripts" / "preflight.sh"


@pytest.fixture
def preflight_text() -> str:
    """Read the preflight script once per test."""
    assert PREFLIGHT_SH.exists(), f"preflight script missing: {PREFLIGHT_SH}"
    return PREFLIGHT_SH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Script-content contract
# ---------------------------------------------------------------------------

class TestPreflightScriptInvokesSmokeTest:
    def test_invokes_smoke_test_with_strict(self, preflight_text: str):
        # The strict flag is what makes preflight fail on missing/malformed
        # artifacts. Drop it and the gate is silently disabled — lock it in.
        assert "python -m tools.smoke_test --strict" in preflight_text

    def test_has_smoke_test_section_banner(self, preflight_text: str):
        # Section banner matches existing style ("section \"...\"").
        assert 'section "Artifact Shape Smoke Test"' in preflight_text

    def test_failure_path_calls_fail(self, preflight_text: str):
        # Non-zero exit from smoke_test must call fail, which prints FAIL: and
        # exits 1.  Match the exact 'fail "..."' invocation in the else branch.
        assert (
            'fail "One or more registered required artifacts are missing '
            'or malformed (see above)"'
        ) in preflight_text

    def test_success_path_calls_pass(self, preflight_text: str):
        assert (
            'pass "All registered required artifacts have the expected shape"'
        ) in preflight_text

    def test_runs_after_env_registry_stage(self, preflight_text: str):
        # Order matters: env check first (so a missing FMP_API_KEY surfaces
        # before we look at artifacts), then artifact shape.
        env_idx = preflight_text.index('section "Env Var Registry"')
        smoke_idx = preflight_text.index('section "Artifact Shape Smoke Test"')
        assert env_idx < smoke_idx, (
            "Smoke test stage must come AFTER the env registry check"
        )

    def test_runs_before_fmp_compliance_stage(self, preflight_text: str):
        # Artifact shape check is cheap and should run before the FMP
        # compliance probe so that obvious local data corruption surfaces
        # quickly without waiting on the FMP probe.
        smoke_idx = preflight_text.index('section "Artifact Shape Smoke Test"')
        fmp_idx = preflight_text.index('section "FMP Compliance"')
        assert smoke_idx < fmp_idx, (
            "Smoke test stage must come BEFORE the FMP compliance stage"
        )

    def test_does_not_add_include_optional(self, preflight_text: str):
        # Preflight intentionally only fails on REQUIRED artifacts. Promoting
        # missing optional artifacts to FAIL would break preflight on fresh
        # deploys where, say, memo_delivery_status.json hasn't been written yet.
        assert "--include-optional" not in preflight_text, (
            "Preflight must not use --include-optional; that would flag "
            "legitimately-absent optional artifacts as FAIL."
        )

    def test_smoke_test_command_is_strict_form(self, preflight_text: str):
        # No silent non-strict variant of the same command lurking.
        # The only smoke_test invocation should be the strict one.
        invocations = [
            line for line in preflight_text.splitlines()
            if "tools.smoke_test" in line and not line.lstrip().startswith("#")
        ]
        assert len(invocations) == 1, (
            f"Expected exactly one tools.smoke_test invocation, found "
            f"{len(invocations)}: {invocations}"
        )
        assert "--strict" in invocations[0]


# ---------------------------------------------------------------------------
# Runtime: the CLI the shell invokes must exit with the right code
# ---------------------------------------------------------------------------

def _write_minimal_artifact(repo: Path, name: str) -> Path:
    """Materialize a minimal valid artifact for the given registry entry."""
    art = next(a for a in REGISTRY if a.name == name)
    path = artifact_path(name, base_dir=repo / "outputs")
    path.parent.mkdir(parents=True, exist_ok=True)
    if art.format == "json":
        payload: dict = {"generated_at": "2026-05-15T00:00:00+00:00"}
        if art.observe_only_required:
            payload["observe_only"] = True
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif art.format == "jsonl":
        path.write_text(json.dumps({"k": "v"}) + "\n", encoding="utf-8")
    elif art.format == "md":
        path.write_text("# stub\n", encoding="utf-8")
    elif art.format == "txt":
        path.write_text("stub\n", encoding="utf-8")
    elif art.format == "csv":
        path.write_text("col\nval\n", encoding="utf-8")
    return path


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "stockbot"
    repo.mkdir()
    (repo / "main.py").write_text("# marker\n", encoding="utf-8")
    (repo / "outputs").mkdir()
    return repo


class TestSmokeTestExitContract:
    """
    The shell stage uses ``if python -m tools.smoke_test --strict; then``.
    These tests verify the exit codes the shell relies on.
    """

    def test_strict_missing_required_exits_one(self, fake_repo: Path):
        # Empty outputs/ tree: every required artifact missing → exit 1.
        rc = tool.main(["--repo-root", str(fake_repo), "--strict"])
        assert rc == 1, "Missing required artifacts must trip strict mode → preflight fail"

    def test_strict_all_required_present_exits_zero(self, fake_repo: Path):
        # Build the registry's required-and-not-append-only set.
        for art in REGISTRY:
            if art.optional or art.append_only:
                continue
            _write_minimal_artifact(fake_repo, art.name)
        rc = tool.main(["--repo-root", str(fake_repo), "--strict"])
        assert rc == 0, "Clean required artifacts must pass strict mode → preflight pass"

    def test_strict_with_corrupt_required_exits_one(self, fake_repo: Path):
        # Required set present, then we corrupt one JSON file.
        for art in REGISTRY:
            if art.optional or art.append_only:
                continue
            _write_minimal_artifact(fake_repo, art.name)
        target = artifact_path("pipeline_run_status", base_dir=fake_repo / "outputs")
        target.write_text("{not json", encoding="utf-8")
        rc = tool.main(["--repo-root", str(fake_repo), "--strict"])
        assert rc == 1, (
            "Malformed required artifact must trip strict mode → preflight fail"
        )

    def test_strict_missing_optional_does_not_fail(self, fake_repo: Path):
        # Required-and-not-append-only set present; every optional absent.
        for art in REGISTRY:
            if art.optional or art.append_only:
                continue
            _write_minimal_artifact(fake_repo, art.name)
        rc = tool.main(["--repo-root", str(fake_repo), "--strict"])
        assert rc == 0, (
            "Missing optional artifacts must NOT fail preflight — preflight "
            "must surface only required-artifact problems."
        )


# ---------------------------------------------------------------------------
# Sanity: registry has at least one required, non-append-only artifact so the
# preflight gate has something to guard.  If this ever becomes false, the
# preflight smoke stage degrades to a no-op and the gate should be revisited.
# ---------------------------------------------------------------------------

class TestRegistryGateIsMeaningful:
    def test_registry_has_required_non_append_only_artifact(self):
        required = [
            a for a in REGISTRY
            if not a.optional and not a.append_only
        ]
        assert required, (
            "Artifact registry has no required, non-append-only artifacts. "
            "The preflight smoke-test gate would become a no-op — revisit."
        )
