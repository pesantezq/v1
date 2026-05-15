"""Tests for tools/backup_portfolio_db.py."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools import backup_portfolio_db as tool


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "stockbot"
    repo.mkdir()
    (repo / "main.py").write_text("# marker\n", encoding="utf-8")
    (repo / "data").mkdir()
    return repo


def _seed_db(repo: Path) -> Path:
    db = repo / "data" / "portfolio.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE run_history(run_id TEXT, status TEXT)")
    conn.execute("INSERT INTO run_history VALUES ('rid', 'completed')")
    conn.commit()
    conn.close()
    return db


class TestDetectRepoRoot:
    def test_explicit_with_marker(self, fake_repo: Path):
        assert tool.detect_repo_root(fake_repo) == fake_repo.resolve()

    def test_missing_marker_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            tool.detect_repo_root(tmp_path)


class TestBackup:
    def test_missing_source_db_fails_cleanly(self, fake_repo: Path):
        result = tool.backup(repo_root=fake_repo)
        assert result.success is False
        assert "not found" in (result.error or "")

    def test_writes_backup_to_expected_path(self, fake_repo: Path):
        _seed_db(fake_repo)
        result = tool.backup(repo_root=fake_repo)
        assert result.success is True
        assert result.backup_bytes > 0
        # Path lives in outputs/policy/db_backups/portfolio.db.YYYY-MM-DD.sqlite
        bp = Path(result.backup_path)
        assert bp.exists()
        assert bp.parent.name == "db_backups"
        assert bp.name.startswith("portfolio.db.")
        assert bp.name.endswith(".sqlite")

    def test_backup_is_a_real_sqlite_db(self, fake_repo: Path):
        _seed_db(fake_repo)
        result = tool.backup(repo_root=fake_repo)
        conn = sqlite3.connect(result.backup_path)
        try:
            row = conn.execute("SELECT run_id, status FROM run_history").fetchone()
        finally:
            conn.close()
        assert row == ("rid", "completed")

    def test_same_day_rerun_overwrites(self, fake_repo: Path):
        _seed_db(fake_repo)
        r1 = tool.backup(repo_root=fake_repo)
        # Add a row, re-backup; the file should now be larger or at least
        # different (must contain the new row).
        db = fake_repo / "data" / "portfolio.db"
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO run_history VALUES ('rid2', 'completed')")
        conn.commit()
        conn.close()
        r2 = tool.backup(repo_root=fake_repo)
        assert r2.success is True
        assert r2.backup_path == r1.backup_path  # same date → same file
        conn = sqlite3.connect(r2.backup_path)
        try:
            rows = conn.execute("SELECT run_id FROM run_history").fetchall()
        finally:
            conn.close()
        assert {r[0] for r in rows} == {"rid", "rid2"}

    def test_no_partial_file_left_behind(self, fake_repo: Path):
        _seed_db(fake_repo)
        tool.backup(repo_root=fake_repo)
        backup_dir = fake_repo / "outputs" / "policy" / "db_backups"
        partials = [f for f in backup_dir.iterdir() if f.suffix == ".partial"]
        assert partials == []

    def test_appends_log(self, fake_repo: Path):
        _seed_db(fake_repo)
        tool.backup(repo_root=fake_repo)
        log = fake_repo / "outputs" / "policy" / "db_backups_log.jsonl"
        assert log.exists()
        records = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert records[-1]["tool"] == "backup_portfolio_db"
        assert records[-1]["success"] is True

    def test_dry_run_writes_nothing(self, fake_repo: Path):
        _seed_db(fake_repo)
        result = tool.backup(repo_root=fake_repo, dry_run=True)
        assert result.success is True
        assert result.dry_run is True
        backup_dir = fake_repo / "outputs" / "policy" / "db_backups"
        # Directory may exist from mkdir but no .sqlite files should be there
        files = [f for f in backup_dir.iterdir() if f.suffix == ".sqlite"] if backup_dir.exists() else []
        assert files == []


class TestRetention:
    def _make_old_backup(self, fake_repo: Path, date_str: str) -> Path:
        backup_dir = fake_repo / "outputs" / "policy" / "db_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        p = backup_dir / f"portfolio.db.{date_str}.sqlite"
        # Real but tiny SQLite file
        conn = sqlite3.connect(p)
        conn.execute("CREATE TABLE x(i INTEGER)")
        conn.commit()
        conn.close()
        return p

    def test_deletes_older_than_retention(self, fake_repo: Path):
        _seed_db(fake_repo)
        # Create 5 older daily backups
        for d in ("2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05"):
            self._make_old_backup(fake_repo, d)
        # Retain 3 → only 3 of the 6 (5 old + 1 new) survive
        result = tool.backup(repo_root=fake_repo, retain=3)
        assert result.success is True
        backup_dir = fake_repo / "outputs" / "policy" / "db_backups"
        survivors = sorted([f.name for f in backup_dir.iterdir() if f.name.endswith(".sqlite")])
        assert len(survivors) == 3
        # Today's backup must be among the survivors
        today = datetime.now(timezone.utc).date().isoformat()
        assert any(today in n for n in survivors)
        # The oldest must have been deleted
        assert not any("2026-05-01" in n for n in survivors)

    def test_retention_does_not_touch_unrelated_files(self, fake_repo: Path):
        _seed_db(fake_repo)
        backup_dir = fake_repo / "outputs" / "policy" / "db_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        # An unrelated file in the dir
        stray = backup_dir / "do-not-delete.txt"
        stray.write_text("hands off", encoding="utf-8")
        tool.backup(repo_root=fake_repo, retain=1)
        assert stray.exists()
        assert stray.read_text(encoding="utf-8") == "hands off"

    def test_retain_one_keeps_only_today(self, fake_repo: Path):
        _seed_db(fake_repo)
        # 3 old + run today → keep 1 = today only
        for d in ("2026-05-01", "2026-05-02", "2026-05-03"):
            self._make_old_backup(fake_repo, d)
        tool.backup(repo_root=fake_repo, retain=1)
        backup_dir = fake_repo / "outputs" / "policy" / "db_backups"
        survivors = [f for f in backup_dir.iterdir() if f.suffix == ".sqlite"]
        assert len(survivors) == 1
        today = datetime.now(timezone.utc).date().isoformat()
        assert today in survivors[0].name


class TestCLI:
    def test_default_run_exits_zero(self, fake_repo: Path, capsys: pytest.CaptureFixture):
        _seed_db(fake_repo)
        rc = tool.main(["--repo-root", str(fake_repo)])
        out = capsys.readouterr().out
        assert rc == 0
        parsed = json.loads(out)
        assert parsed["success"] is True

    def test_missing_db_exits_one(self, fake_repo: Path, capsys: pytest.CaptureFixture):
        # No seed_db call → no DB exists
        rc = tool.main(["--repo-root", str(fake_repo)])
        assert rc == 1

    def test_invalid_retain_exits_two(self, fake_repo: Path):
        rc = tool.main(["--repo-root", str(fake_repo), "--retain", "0"])
        assert rc == 2

    def test_dry_run_flag(self, fake_repo: Path, capsys: pytest.CaptureFixture):
        _seed_db(fake_repo)
        rc = tool.main(["--repo-root", str(fake_repo), "--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        parsed = json.loads(out)
        assert parsed["dry_run"] is True
        assert parsed["backup_bytes"] == 0
