"""
Tests for tools/cleanup_orphan_outputs.py

Safety properties under test:

- Dry run is the default and is non-destructive.
- ``--confirm`` actually deletes.
- The tool refuses to delete a path inside the repo.
- The tool refuses to delete the repo's real outputs/.
- Audit log records every confirmed deletion.
- Missing orphan tree → clean exit, nothing deleted.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import cleanup_orphan_outputs as tool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """
    Build a minimal repo layout under tmp_path:

        tmp_path/
          stockbot/         <- pretend repo root
            main.py
            outputs/        <- the REAL outputs (must not be deleted)
              latest/
                keep_me.txt
          outputs/          <- the ORPHAN outputs (target of cleanup)
            latest/
              orphan.txt
            policy/
              orphan_log.jsonl
    """
    repo = tmp_path / "stockbot"
    repo.mkdir()
    (repo / "main.py").write_text("# fake repo marker\n", encoding="utf-8")

    real_outputs = repo / "outputs" / "latest"
    real_outputs.mkdir(parents=True)
    (real_outputs / "keep_me.txt").write_text("REAL", encoding="utf-8")

    orphan_root = tmp_path / "outputs"
    (orphan_root / "latest").mkdir(parents=True)
    (orphan_root / "latest" / "orphan.txt").write_text("ORPHAN", encoding="utf-8")
    (orphan_root / "policy").mkdir()
    (orphan_root / "policy" / "orphan_log.jsonl").write_text(
        '{"k":"v"}\n', encoding="utf-8",
    )
    return repo


# ---------------------------------------------------------------------------
# detect_repo_root
# ---------------------------------------------------------------------------

class TestDetectRepoRoot:
    def test_explicit_root_with_marker(self, fake_repo: Path):
        root = tool.detect_repo_root(fake_repo)
        assert root == fake_repo.resolve()

    def test_explicit_root_without_marker_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            tool.detect_repo_root(tmp_path)

    def test_default_root_uses_repo_marker(self):
        """
        Calling with no override picks ``parents[1]`` of this script — the
        real repo root. Must contain main.py.
        """
        root = tool.detect_repo_root()
        assert (root / "main.py").exists()


# ---------------------------------------------------------------------------
# orphan_root_for
# ---------------------------------------------------------------------------

class TestOrphanRootFor:
    def test_sibling_of_repo(self, fake_repo: Path):
        orphan = tool.orphan_root_for(fake_repo)
        assert orphan == fake_repo.parent / "outputs"

    def test_distinct_from_real_outputs(self, fake_repo: Path):
        orphan = tool.orphan_root_for(fake_repo)
        real = (fake_repo / "outputs").resolve()
        assert orphan.resolve() != real


# ---------------------------------------------------------------------------
# find_orphan_items
# ---------------------------------------------------------------------------

class TestFindOrphanItems:
    def test_lists_top_level_entries(self, fake_repo: Path):
        items = tool.find_orphan_items(fake_repo.parent / "outputs")
        names = {p.name for p in items}
        assert names == {"latest", "policy"}

    def test_missing_orphan_returns_empty(self, tmp_path: Path):
        items = tool.find_orphan_items(tmp_path / "no_such_dir")
        assert items == []


# ---------------------------------------------------------------------------
# cleanup() dry run
# ---------------------------------------------------------------------------

class TestCleanupDryRun:
    def test_dry_run_is_default(self, fake_repo: Path):
        result = tool.cleanup(repo_root=fake_repo)
        assert result.dry_run is True
        assert result.deleted == []

    def test_dry_run_does_not_touch_orphan_tree(self, fake_repo: Path):
        tool.cleanup(repo_root=fake_repo)
        orphan_file = fake_repo.parent / "outputs" / "latest" / "orphan.txt"
        assert orphan_file.exists()
        assert orphan_file.read_text(encoding="utf-8") == "ORPHAN"

    def test_dry_run_does_not_touch_real_outputs(self, fake_repo: Path):
        tool.cleanup(repo_root=fake_repo)
        real_file = fake_repo / "outputs" / "latest" / "keep_me.txt"
        assert real_file.exists()
        assert real_file.read_text(encoding="utf-8") == "REAL"

    def test_dry_run_reports_items(self, fake_repo: Path):
        result = tool.cleanup(repo_root=fake_repo)
        names = {p.name for p in result.items}
        assert names == {"latest", "policy"}
        assert result.orphan_exists is True
        assert result.refused_reason is None

    def test_no_orphan_dir_is_clean_exit(self, tmp_path: Path):
        # Build a fake repo with NO orphan tree alongside it.
        repo = tmp_path / "stockbot"
        repo.mkdir()
        (repo / "main.py").write_text("", encoding="utf-8")
        result = tool.cleanup(repo_root=repo)
        assert result.orphan_exists is False
        assert result.items == []
        assert result.refused_reason is None


# ---------------------------------------------------------------------------
# cleanup() with --confirm
# ---------------------------------------------------------------------------

class TestCleanupConfirm:
    def test_confirm_removes_orphan_tree(self, fake_repo: Path):
        result = tool.cleanup(repo_root=fake_repo, confirm=True)
        assert result.dry_run is False
        assert len(result.deleted) == 2
        # Orphan tree is gone:
        orphan_file = fake_repo.parent / "outputs" / "latest" / "orphan.txt"
        assert not orphan_file.exists()

    def test_confirm_does_not_touch_real_outputs(self, fake_repo: Path):
        tool.cleanup(repo_root=fake_repo, confirm=True)
        real_file = fake_repo / "outputs" / "latest" / "keep_me.txt"
        assert real_file.exists()
        assert real_file.read_text(encoding="utf-8") == "REAL"

    def test_confirm_writes_audit_log(self, fake_repo: Path):
        tool.cleanup(repo_root=fake_repo, confirm=True)
        log_path = fake_repo / "outputs" / "policy" / "cleanup_orphan_outputs.jsonl"
        assert log_path.exists()
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["tool"] == "cleanup_orphan_outputs"
        assert record["dry_run"] is False
        assert len(record["items_deleted"]) == 2

    def test_confirm_removes_empty_orphan_root(self, fake_repo: Path):
        tool.cleanup(repo_root=fake_repo, confirm=True)
        assert not (fake_repo.parent / "outputs").exists()


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------

class TestSafety:
    def test_refuses_when_orphan_is_inside_repo(self, tmp_path: Path):
        """
        Construct a degenerate case where the orphan_root_for() helper would
        produce a path inside the repo: build a fake repo as
        ``tmp_path/<repo>/`` with the repo's parent set to ``tmp_path/<repo>``
        itself by making the repo root a child of a dir that exists under
        the repo.  Since orphan_root = repo_root.parent / "outputs", this is
        hard to engineer without filesystem trickery — instead, monkey-patch
        orphan_root_for to return a known-inside-repo path and verify cleanup
        refuses.
        """
        repo = tmp_path / "stockbot"
        repo.mkdir()
        (repo / "main.py").write_text("", encoding="utf-8")
        # Create a directory inside the repo and point orphan_root_for at it.
        inside = repo / "subdir"
        inside.mkdir()
        (inside / "should_not_delete.txt").write_text("KEEP", encoding="utf-8")

        original = tool.orphan_root_for
        try:
            tool.orphan_root_for = lambda root: inside  # type: ignore
            result = tool.cleanup(repo_root=repo, confirm=True)
        finally:
            tool.orphan_root_for = original  # type: ignore

        assert result.refused_reason is not None
        assert "inside repo root" in result.refused_reason
        assert (inside / "should_not_delete.txt").exists()

    def test_refuses_when_orphan_equals_real_outputs(self, tmp_path: Path):
        repo = tmp_path / "stockbot"
        repo.mkdir()
        (repo / "main.py").write_text("", encoding="utf-8")
        real_outputs = repo / "outputs"
        real_outputs.mkdir()
        (real_outputs / "important.txt").write_text("REAL", encoding="utf-8")

        original = tool.orphan_root_for
        try:
            tool.orphan_root_for = lambda root: real_outputs  # type: ignore
            result = tool.cleanup(repo_root=repo, confirm=True)
        finally:
            tool.orphan_root_for = original  # type: ignore

        # outputs/ is inside repo too, so "inside repo root" guard fires first
        # — either refusal reason is acceptable.
        assert result.refused_reason is not None
        assert (real_outputs / "important.txt").exists()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_dry_run_exit_zero(self, fake_repo: Path, capsys: pytest.CaptureFixture):
        rc = tool.main(["--repo-root", str(fake_repo)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Dry run" in out
        assert "Items found" in out

    def test_confirm_exit_zero_and_deletes(self, fake_repo: Path, capsys: pytest.CaptureFixture):
        rc = tool.main(["--repo-root", str(fake_repo), "--confirm"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Deleted" in out
        assert not (fake_repo.parent / "outputs").exists()

    def test_missing_marker_exit_two(self, tmp_path: Path, capsys: pytest.CaptureFixture):
        rc = tool.main(["--repo-root", str(tmp_path)])
        err = capsys.readouterr().err
        assert rc == 2
        assert "Repo root marker" in err
