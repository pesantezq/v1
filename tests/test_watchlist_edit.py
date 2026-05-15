"""Tests for tools/watchlist_edit.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import watchlist_edit as tool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "stockbot"
    repo.mkdir()
    (repo / "main.py").write_text("# marker\n", encoding="utf-8")
    (repo / "data").mkdir()
    # Seed config with an empty watchlist
    (repo / "config.json").write_text(
        json.dumps({"watchlist_scanner": {"watchlist": []}}),
        encoding="utf-8",
    )
    return repo


def _read_wl(repo: Path) -> list[str]:
    cfg = json.loads((repo / "config.json").read_text(encoding="utf-8"))
    return cfg.get("watchlist_scanner", {}).get("watchlist") or []


def _read_tags(repo: Path) -> dict:
    p = repo / "data" / "watchlist_tags.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Symbol parsing + validation
# ---------------------------------------------------------------------------

class TestParseSymbols:
    @pytest.mark.parametrize("raw,expected", [
        ("AAPL", ["AAPL"]),
        ("aapl", ["AAPL"]),
        ("NVDA,AAPL", ["NVDA", "AAPL"]),
        ("nvda, aapl,  msft", ["NVDA", "AAPL", "MSFT"]),
        ("AAPL,AAPL,AAPL", ["AAPL"]),     # dedupe
        ("BRK.B", ["BRK.B"]),              # dots allowed
        ("BF-B", ["BF-B"]),                # hyphens allowed
    ])
    def test_normalisation(self, raw: str, expected: list[str]):
        assert tool.parse_symbols(raw) == expected

    def test_empty_returns_empty(self):
        assert tool.parse_symbols("") == []
        assert tool.parse_symbols(",,,") == []

    @pytest.mark.parametrize("bad", [
        "1AAPL",        # starts with digit
        "AAPL$",        # invalid char
        "TOOLONGSYMBOL",  # over 10 chars
        "A B",          # space in symbol
    ])
    def test_invalid_raises(self, bad: str):
        with pytest.raises(ValueError):
            tool.parse_symbols(bad)


# ---------------------------------------------------------------------------
# list_watchlist
# ---------------------------------------------------------------------------

class TestList:
    def test_empty_watchlist(self, fake_repo: Path):
        r = tool.list_watchlist(fake_repo)
        assert r["count"] == 0
        assert r["symbols"] == []
        assert r["rows"] == []
        assert r["advisory_only"] is True

    def test_lists_with_tags(self, fake_repo: Path):
        tool.add_symbols(fake_repo, "NVDA,AAPL")
        tool.set_tags(fake_repo, "NVDA", "AI,Semis")
        tool.set_enabled(fake_repo, "AAPL", False)
        r = tool.list_watchlist(fake_repo)
        rows = {x["symbol"]: x for x in r["rows"]}
        assert rows["NVDA"]["tags"] == ["AI", "Semis"]
        assert rows["NVDA"]["enabled"] is True
        assert rows["AAPL"]["enabled"] is False


# ---------------------------------------------------------------------------
# add / remove
# ---------------------------------------------------------------------------

class TestAdd:
    def test_add_new_symbols(self, fake_repo: Path):
        r = tool.add_symbols(fake_repo, "NVDA,AAPL")
        assert r.success is True
        assert r.added == ["NVDA", "AAPL"]
        assert _read_wl(fake_repo) == ["NVDA", "AAPL"]

    def test_add_skips_existing(self, fake_repo: Path):
        tool.add_symbols(fake_repo, "NVDA")
        r = tool.add_symbols(fake_repo, "NVDA,AAPL")
        assert r.added == ["AAPL"]
        assert _read_wl(fake_repo) == ["NVDA", "AAPL"]

    def test_add_all_existing_reports_error(self, fake_repo: Path):
        tool.add_symbols(fake_repo, "NVDA,AAPL")
        r = tool.add_symbols(fake_repo, "NVDA,AAPL")
        assert r.added == []
        assert "already" in (r.error or "")

    def test_add_dry_run_does_not_write(self, fake_repo: Path):
        r = tool.add_symbols(fake_repo, "NVDA", dry_run=True)
        assert r.dry_run is True
        assert r.added == ["NVDA"]
        assert _read_wl(fake_repo) == []

    def test_add_invalid_symbol_raises(self, fake_repo: Path):
        with pytest.raises(ValueError):
            tool.add_symbols(fake_repo, "1BAD")


class TestRemove:
    def test_remove_existing(self, fake_repo: Path):
        tool.add_symbols(fake_repo, "NVDA,AAPL,MSFT")
        r = tool.remove_symbols(fake_repo, "AAPL")
        assert r.removed == ["AAPL"]
        assert _read_wl(fake_repo) == ["NVDA", "MSFT"]

    def test_remove_unknown_reports_error(self, fake_repo: Path):
        tool.add_symbols(fake_repo, "NVDA")
        r = tool.remove_symbols(fake_repo, "AAPL")
        assert r.removed == []
        assert "no requested symbols" in (r.error or "")

    def test_remove_cleans_up_tags(self, fake_repo: Path):
        tool.add_symbols(fake_repo, "NVDA,AAPL")
        tool.set_tags(fake_repo, "AAPL", "Mega")
        assert "AAPL" in _read_tags(fake_repo)
        tool.remove_symbols(fake_repo, "AAPL")
        assert "AAPL" not in _read_tags(fake_repo)
        # Other symbols' tags preserved
        tool.set_tags(fake_repo, "NVDA", "AI")
        assert "NVDA" in _read_tags(fake_repo)

    def test_remove_dry_run_does_not_write(self, fake_repo: Path):
        tool.add_symbols(fake_repo, "NVDA,AAPL")
        r = tool.remove_symbols(fake_repo, "AAPL", dry_run=True)
        assert r.removed == ["AAPL"]
        assert _read_wl(fake_repo) == ["NVDA", "AAPL"]


# ---------------------------------------------------------------------------
# bulk_replace
# ---------------------------------------------------------------------------

class TestBulkReplace:
    def test_replaces_entire_list(self, fake_repo: Path):
        tool.add_symbols(fake_repo, "NVDA,AAPL")
        r = tool.bulk_replace(fake_repo, "QQQ,SPY,GLD")
        assert r.added == ["QQQ", "SPY", "GLD"]
        assert r.removed == ["NVDA", "AAPL"]
        assert _read_wl(fake_repo) == ["QQQ", "SPY", "GLD"]

    def test_replace_strips_orphaned_tags(self, fake_repo: Path):
        tool.add_symbols(fake_repo, "NVDA,AAPL")
        tool.set_tags(fake_repo, "AAPL", "Mega")
        tool.bulk_replace(fake_repo, "QQQ")
        tags = _read_tags(fake_repo)
        assert "AAPL" not in tags

    def test_replace_dry_run(self, fake_repo: Path):
        tool.add_symbols(fake_repo, "NVDA")
        r = tool.bulk_replace(fake_repo, "QQQ", dry_run=True)
        assert r.dry_run is True
        assert _read_wl(fake_repo) == ["NVDA"]


# ---------------------------------------------------------------------------
# Per-symbol metadata
# ---------------------------------------------------------------------------

class TestPerSymbolMetadata:
    def test_set_tags(self, fake_repo: Path):
        tool.add_symbols(fake_repo, "NVDA")
        r = tool.set_tags(fake_repo, "NVDA", "AI,Semis")
        assert r.success is True
        assert _read_tags(fake_repo)["NVDA"]["tags"] == ["AI", "Semis"]

    def test_set_tags_replaces_not_appends(self, fake_repo: Path):
        tool.add_symbols(fake_repo, "NVDA")
        tool.set_tags(fake_repo, "NVDA", "AI")
        tool.set_tags(fake_repo, "NVDA", "Mega,Growth")
        assert _read_tags(fake_repo)["NVDA"]["tags"] == ["Mega", "Growth"]

    def test_set_enabled(self, fake_repo: Path):
        tool.add_symbols(fake_repo, "NVDA")
        tool.set_enabled(fake_repo, "NVDA", False)
        assert _read_tags(fake_repo)["NVDA"]["enabled"] is False
        tool.set_enabled(fake_repo, "NVDA", True)
        assert _read_tags(fake_repo)["NVDA"]["enabled"] is True

    def test_set_note(self, fake_repo: Path):
        tool.add_symbols(fake_repo, "NVDA")
        tool.set_note(fake_repo, "NVDA", "AI bellwether")
        assert _read_tags(fake_repo)["NVDA"]["note"] == "AI bellwether"

    def test_set_tag_on_unknown_symbol_fails(self, fake_repo: Path):
        r = tool.set_tags(fake_repo, "GHOST", "Phantom")
        assert r.success is False
        assert "not in the watchlist" in (r.error or "")


# ---------------------------------------------------------------------------
# Import / export
# ---------------------------------------------------------------------------

class TestExportImport:
    def test_export_round_trip(self, fake_repo: Path, tmp_path: Path):
        tool.add_symbols(fake_repo, "NVDA,AAPL")
        tool.set_tags(fake_repo, "NVDA", "AI,Semis")
        out = tmp_path / "exported.json"
        tool.export_state(fake_repo, out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["advisory_only"] is True
        assert payload["watchlist"] == ["NVDA", "AAPL"]
        assert payload["tags"]["NVDA"]["tags"] == ["AI", "Semis"]

    def test_import_replaces_state(self, fake_repo: Path, tmp_path: Path):
        # Start with one state
        tool.add_symbols(fake_repo, "NVDA,AAPL,MSFT")
        tool.set_tags(fake_repo, "AAPL", "Mega")
        # Craft an import payload with a different state
        src = tmp_path / "incoming.json"
        src.write_text(json.dumps({
            "watchlist": ["QQQ", "GLD"],
            "tags": {"QQQ": {"enabled": True, "tags": ["Core"], "note": "core etf"}},
        }), encoding="utf-8")
        r = tool.import_state(fake_repo, src)
        assert r.success is True
        assert _read_wl(fake_repo) == ["QQQ", "GLD"]
        # Old tags wiped
        assert "AAPL" not in _read_tags(fake_repo)
        # New tags applied
        assert _read_tags(fake_repo)["QQQ"]["note"] == "core etf"

    def test_import_dry_run_does_not_write(self, fake_repo: Path, tmp_path: Path):
        tool.add_symbols(fake_repo, "NVDA")
        src = tmp_path / "incoming.json"
        src.write_text(json.dumps({"watchlist": ["QQQ"]}), encoding="utf-8")
        r = tool.import_state(fake_repo, src, dry_run=True)
        assert r.dry_run is True
        assert _read_wl(fake_repo) == ["NVDA"]

    def test_import_invalid_file_fails(self, fake_repo: Path, tmp_path: Path):
        src = tmp_path / "bad.json"
        src.write_text("{not json", encoding="utf-8")
        r = tool.import_state(fake_repo, src)
        assert r.success is False
        assert "invalid JSON" in (r.error or "")

    def test_import_missing_watchlist_key_fails(self, fake_repo: Path, tmp_path: Path):
        src = tmp_path / "shape.json"
        src.write_text(json.dumps({"tags": {}}), encoding="utf-8")
        r = tool.import_state(fake_repo, src)
        assert r.success is False
        assert "watchlist" in (r.error or "")


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_each_write_appends_one_row(self, fake_repo: Path):
        tool.add_symbols(fake_repo, "NVDA")
        tool.add_symbols(fake_repo, "AAPL")
        tool.remove_symbols(fake_repo, "AAPL")
        log = fake_repo / "outputs" / "policy" / "watchlist_edits.jsonl"
        rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(rows) == 3
        assert rows[0]["op"] == "add"
        assert rows[2]["op"] == "remove"

    def test_dry_run_does_not_log(self, fake_repo: Path):
        tool.add_symbols(fake_repo, "NVDA", dry_run=True)
        log = fake_repo / "outputs" / "policy" / "watchlist_edits.jsonl"
        assert not log.exists()


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------

class TestAtomicity:
    def test_no_partial_files_remain(self, fake_repo: Path):
        tool.add_symbols(fake_repo, "NVDA,AAPL")
        tool.set_tags(fake_repo, "NVDA", "AI")
        # Look for any leftover .partial files anywhere under the repo
        partials = list(fake_repo.rglob("*.partial"))
        assert partials == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_list_default_exits_zero(self, fake_repo: Path, capsys: pytest.CaptureFixture):
        rc = tool.main(["--repo-root", str(fake_repo), "--list"])
        out = capsys.readouterr().out
        assert rc == 0
        parsed = json.loads(out)
        assert parsed["count"] == 0

    def test_add_via_cli(self, fake_repo: Path, capsys: pytest.CaptureFixture):
        rc = tool.main(["--repo-root", str(fake_repo), "--add", "NVDA,AAPL"])
        assert rc == 0
        assert _read_wl(fake_repo) == ["NVDA", "AAPL"]

    def test_invalid_symbol_exits_one(self, fake_repo: Path, capsys: pytest.CaptureFixture):
        rc = tool.main(["--repo-root", str(fake_repo), "--add", "1BAD"])
        assert rc == 1

    def test_missing_marker_exits_two(self, tmp_path: Path, capsys: pytest.CaptureFixture):
        rc = tool.main(["--repo-root", str(tmp_path), "--list"])
        assert rc == 2

    def test_dry_run_flag(self, fake_repo: Path, capsys: pytest.CaptureFixture):
        rc = tool.main(["--repo-root", str(fake_repo), "--dry-run", "--add", "NVDA"])
        assert rc == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["dry_run"] is True
        assert _read_wl(fake_repo) == []
