"""
Tests for tools/smoke_test.py — registry-driven artifact shape smoke check.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import smoke_test as tool
from portfolio_automation.artifacts_registry import REGISTRY, artifact_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Minimal repo layout: just a main.py marker + outputs/ tree."""
    repo = tmp_path / "stockbot"
    repo.mkdir()
    (repo / "main.py").write_text("# marker\n", encoding="utf-8")
    (repo / "outputs").mkdir()
    return repo


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


# ---------------------------------------------------------------------------
# detect_repo_root
# ---------------------------------------------------------------------------

class TestDetectRepoRoot:
    def test_with_marker(self, fake_repo: Path):
        assert tool.detect_repo_root(fake_repo) == fake_repo.resolve()

    def test_missing_marker(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            tool.detect_repo_root(tmp_path)


# ---------------------------------------------------------------------------
# JSON validation
# ---------------------------------------------------------------------------

class TestValidateJson:
    def test_valid_with_observe_only(self, tmp_path: Path):
        p = tmp_path / "x.json"
        p.write_text(json.dumps({
            "generated_at": "x", "observe_only": True,
        }), encoding="utf-8")
        sev, msg, det = tool._validate_json(p, art=None, observe_only_required=True)
        assert sev == tool.SEV_OK

    def test_observe_only_required_but_missing(self, tmp_path: Path):
        p = tmp_path / "x.json"
        p.write_text(json.dumps({"generated_at": "x"}), encoding="utf-8")
        sev, msg, _ = tool._validate_json(p, art=None, observe_only_required=True)
        assert sev == tool.SEV_FAIL
        assert "observe_only" in msg

    def test_observe_only_not_required_missing_is_ok(self, tmp_path: Path):
        p = tmp_path / "x.json"
        p.write_text(json.dumps({"generated_at": "x"}), encoding="utf-8")
        sev, _, _ = tool._validate_json(p, art=None, observe_only_required=False)
        assert sev == tool.SEV_OK

    def test_missing_generated_at_is_warn(self, tmp_path: Path):
        p = tmp_path / "x.json"
        p.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
        sev, _, det = tool._validate_json(p, art=None, observe_only_required=False)
        assert sev == tool.SEV_WARN
        assert "missing_recommended_fields" in det

    def test_invalid_json_is_fail(self, tmp_path: Path):
        p = tmp_path / "x.json"
        p.write_text("{not json", encoding="utf-8")
        sev, msg, _ = tool._validate_json(p, art=None, observe_only_required=False)
        assert sev == tool.SEV_FAIL
        assert "invalid JSON" in msg

    def test_empty_file_is_fail(self, tmp_path: Path):
        p = tmp_path / "x.json"
        p.write_text("", encoding="utf-8")
        sev, msg, _ = tool._validate_json(p, art=None, observe_only_required=False)
        assert sev == tool.SEV_FAIL
        assert "empty" in msg.lower()

    def test_observe_only_required_with_array_root_is_fail(self, tmp_path: Path):
        p = tmp_path / "x.json"
        p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        sev, msg, _ = tool._validate_json(p, art=None, observe_only_required=True)
        assert sev == tool.SEV_FAIL
        assert "not an object" in msg


# ---------------------------------------------------------------------------
# JSONL validation
# ---------------------------------------------------------------------------

class TestValidateJsonl:
    def test_valid_rows(self, tmp_path: Path):
        p = tmp_path / "x.jsonl"
        p.write_text(
            json.dumps({"a": 1}) + "\n" + json.dumps({"b": 2}) + "\n",
            encoding="utf-8",
        )
        sev, msg, det = tool._validate_jsonl(p)
        assert sev == tool.SEV_OK
        assert det["row_count"] == 2

    def test_empty_jsonl_is_info(self, tmp_path: Path):
        p = tmp_path / "x.jsonl"
        p.write_text("", encoding="utf-8")
        sev, _, det = tool._validate_jsonl(p)
        assert sev == tool.SEV_INFO
        assert det["row_count"] == 0

    def test_blank_lines_ignored(self, tmp_path: Path):
        p = tmp_path / "x.jsonl"
        p.write_text(
            json.dumps({"a": 1}) + "\n\n\n" + json.dumps({"b": 2}) + "\n",
            encoding="utf-8",
        )
        sev, _, det = tool._validate_jsonl(p)
        assert sev == tool.SEV_OK
        assert det["row_count"] == 2

    def test_bad_row_is_fail(self, tmp_path: Path):
        p = tmp_path / "x.jsonl"
        p.write_text(
            json.dumps({"a": 1}) + "\n" + "broken row" + "\n",
            encoding="utf-8",
        )
        sev, msg, det = tool._validate_jsonl(p)
        assert sev == tool.SEV_FAIL
        assert det["bad_rows"] == [2]


# ---------------------------------------------------------------------------
# Text/markdown/csv validation
# ---------------------------------------------------------------------------

class TestValidateText:
    def test_non_empty_is_ok(self, tmp_path: Path):
        p = tmp_path / "x.md"
        p.write_text("# hello\n", encoding="utf-8")
        sev, _, _ = tool._validate_text(p, "markdown")
        assert sev == tool.SEV_OK

    def test_empty_is_warn(self, tmp_path: Path):
        p = tmp_path / "x.md"
        p.write_text("", encoding="utf-8")
        sev, _, _ = tool._validate_text(p, "markdown")
        assert sev == tool.SEV_WARN


# ---------------------------------------------------------------------------
# validate_registry — the integration walk
# ---------------------------------------------------------------------------

class TestValidateRegistry:
    def test_empty_repo_all_required_fail(self, fake_repo: Path):
        report = tool.validate_registry(fake_repo)
        # Every non-optional artifact missing → at least one FAIL
        assert report.severity_counts[tool.SEV_FAIL] > 0
        # Optional artifacts get INFO by default
        assert report.severity_counts[tool.SEV_INFO] > 0

    def test_all_required_artifacts_present_no_fails(self, fake_repo: Path):
        # Create every non-optional, non-append-only artifact
        for art in REGISTRY:
            if art.optional or art.append_only:
                continue
            _write_minimal_artifact(fake_repo, art.name)
        report = tool.validate_registry(fake_repo)
        assert report.severity_counts[tool.SEV_FAIL] == 0

    def test_include_optional_promotes_missing_to_fail(self, fake_repo: Path):
        # Create only required artifacts; optional are absent.
        for art in REGISTRY:
            if art.optional or art.append_only:
                continue
            _write_minimal_artifact(fake_repo, art.name)
        report = tool.validate_registry(fake_repo, include_optional=True)
        # Now optional missing should be FAIL, not INFO
        assert report.severity_counts[tool.SEV_FAIL] > 0
        fails = [r for r in report.results if r.severity == tool.SEV_FAIL]
        assert all(r.message == "artifact missing" for r in fails)

    def test_corrupt_artifact_is_fail(self, fake_repo: Path):
        # Build the registry's full set, then corrupt one JSON file.
        for art in REGISTRY:
            if art.optional or art.append_only:
                continue
            _write_minimal_artifact(fake_repo, art.name)

        # Corrupt the pipeline_run_status artifact
        target = artifact_path("pipeline_run_status", base_dir=fake_repo / "outputs")
        target.write_text("{not json", encoding="utf-8")

        report = tool.validate_registry(fake_repo)
        # That entry must now be FAIL with "invalid JSON" message
        entry = next(r for r in report.results if r.name == "pipeline_run_status")
        assert entry.severity == tool.SEV_FAIL
        assert "invalid JSON" in entry.message

    def test_observe_only_flag_missing_is_fail(self, fake_repo: Path):
        # Write pipeline_run_status without observe_only=True
        path = artifact_path("pipeline_run_status", base_dir=fake_repo / "outputs")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"generated_at": "x"}),  # no observe_only
            encoding="utf-8",
        )
        report = tool.validate_registry(fake_repo)
        entry = next(r for r in report.results if r.name == "pipeline_run_status")
        assert entry.severity == tool.SEV_FAIL
        assert "observe_only" in entry.message


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestRender:
    def test_text_includes_overall(self, fake_repo: Path):
        report = tool.validate_registry(fake_repo)
        out = tool.render_text(report)
        assert "Smoke Test" in out
        assert "Overall:" in out
        assert "Advisory only" in out

    def test_json_round_trip(self, fake_repo: Path):
        report = tool.validate_registry(fake_repo)
        parsed = json.loads(tool.render_json(report))
        assert parsed["advisory_only"] is True
        assert parsed["no_trade"] is True
        assert "results" in parsed
        assert "severity_counts" in parsed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_default_text_exits_zero(
        self, fake_repo: Path, capsys: pytest.CaptureFixture,
    ):
        rc = tool.main(["--repo-root", str(fake_repo)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Overall" in out

    def test_json_format(self, fake_repo: Path, capsys: pytest.CaptureFixture):
        rc = tool.main(["--repo-root", str(fake_repo), "--format", "json"])
        parsed = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert "results" in parsed

    def test_strict_exits_one_on_fail(self, fake_repo: Path):
        # Empty repo → required artifacts missing → FAIL → strict exit 1
        rc = tool.main(["--repo-root", str(fake_repo), "--strict"])
        assert rc == 1

    def test_strict_exits_zero_when_clean(self, fake_repo: Path):
        for art in REGISTRY:
            if art.optional or art.append_only:
                continue
            _write_minimal_artifact(fake_repo, art.name)
        rc = tool.main(["--repo-root", str(fake_repo), "--strict"])
        assert rc == 0

    def test_missing_marker_exit_two(self, tmp_path: Path):
        rc = tool.main(["--repo-root", str(tmp_path)])
        assert rc == 2

    def test_include_optional_propagates(self, fake_repo: Path):
        # Required-only setup. --include-optional should produce FAILs for the
        # missing optional artifacts, so --strict exits 1.
        for art in REGISTRY:
            if art.optional or art.append_only:
                continue
            _write_minimal_artifact(fake_repo, art.name)
        rc = tool.main([
            "--repo-root", str(fake_repo),
            "--include-optional", "--strict",
        ])
        assert rc == 1
