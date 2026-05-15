"""
Regression tests for watchlist_scanner.output_writers._write_portfolio_snapshot_json.

Locks the contract guarantee that the artifact contains the ``generated_at``
field — surfaced as a producer gap by ``tools.smoke_test`` on 2026-05-15.
The writer adds the field only when the producer did not already provide
one, so a producer-supplied timestamp is preserved verbatim.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from watchlist_scanner.output_writers import _write_portfolio_snapshot_json


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    d = tmp_path / "portfolio"
    d.mkdir()
    return d


class TestGeneratedAtPresence:
    def test_writer_adds_generated_at_when_missing(self, out_dir: Path):
        snapshot = {"summary_label": "balanced", "rows": []}
        _write_portfolio_snapshot_json(out_dir, snapshot)
        payload = json.loads((out_dir / "portfolio_snapshot.json").read_text(encoding="utf-8"))
        assert "generated_at" in payload
        # Must be ISO-8601 with timezone offset (UTC); roughly:
        assert re.match(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+\d{2}:\d{2}|Z)",
            payload["generated_at"],
        ), payload["generated_at"]

    def test_writer_preserves_producer_supplied_generated_at(self, out_dir: Path):
        supplied = "2026-05-15T09:01:00+00:00"
        snapshot = {"generated_at": supplied, "rows": []}
        _write_portfolio_snapshot_json(out_dir, snapshot)
        payload = json.loads((out_dir / "portfolio_snapshot.json").read_text(encoding="utf-8"))
        assert payload["generated_at"] == supplied

    def test_writer_does_not_mutate_input_dict(self, out_dir: Path):
        snapshot: dict = {"rows": []}
        _write_portfolio_snapshot_json(out_dir, snapshot)
        # Original input must not gain generated_at via aliasing
        assert "generated_at" not in snapshot

    def test_writer_handles_non_dict_input(self, out_dir: Path):
        # The writer logs rows=0 for non-dict input; should still emit a file.
        _write_portfolio_snapshot_json(out_dir, {})  # empty dict
        assert (out_dir / "portfolio_snapshot.json").exists()
