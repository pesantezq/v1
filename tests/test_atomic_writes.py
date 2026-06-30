"""Phase 1 — atomic artifact writes.

safe_write_json/text must write via a temp file + atomic replace so an
interrupted write never leaves a valid-looking partial artifact, and never
clobbers a prior good artifact when serialization fails mid-write.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.data_governance import (
    OutputNamespace, safe_write_json, safe_write_text,
)


def test_safe_write_json_roundtrips(tmp_path):
    p = safe_write_json(OutputNamespace.LATEST, "phase1_probe.json",
                        {"k": "v"}, base_dir=tmp_path)
    assert json.loads(Path(p).read_text())["k"] == "v"


def test_no_tmp_file_left_behind_on_success(tmp_path):
    safe_write_text(OutputNamespace.LATEST, "phase1_probe.txt", "hello",
                    base_dir=tmp_path)
    leftovers = list(Path(tmp_path).rglob("*.tmp")) + list(Path(tmp_path).rglob("*.tmp-*"))
    assert leftovers == [], f"atomic temp files leaked: {leftovers}"


def test_interrupted_replace_leaves_original_intact(tmp_path, monkeypatch):
    """The keystone atomicity guarantee: if the final atomic replace fails
    (simulated crash at the rename point), the prior good artifact is intact
    and no partial temp file is left. This FAILS on a direct write_text impl
    (it would have already clobbered the destination)."""
    import os
    p = safe_write_json(OutputNamespace.LATEST, "phase1_atomic.json",
                        {"v": "original"}, base_dir=tmp_path)

    real_replace = os.replace
    def boom(src, dst, *a, **k):
        raise OSError("simulated crash at rename")
    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError):
        safe_write_json(OutputNamespace.LATEST, "phase1_atomic.json",
                        {"v": "new-but-interrupted"}, base_dir=tmp_path)

    monkeypatch.setattr(os, "replace", real_replace)
    # original survived the interrupted overwrite
    assert json.loads(Path(p).read_text()) == {"v": "original"}
    # temp cleaned up on failure
    assert list(Path(tmp_path).rglob("*.tmp*")) == []
