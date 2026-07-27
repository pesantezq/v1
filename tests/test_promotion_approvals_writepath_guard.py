"""Guard: promotion_approvals.record_approval must refuse to write when
``base_dir`` points at the project root instead of its ``outputs`` dir.

The output-namespace convention is ``base_dir=<root>/outputs``. Passing
``<root>`` (e.g. ".") silently resolves to ``<root>/promotion_approvals/``,
a location no production loader reads — so a human approval would be recorded
"successfully" yet never applied. This regression was hit live on 2026-07-27.
The guard fails closed (ok=False, nothing written) on that misuse while leaving
the legitimate ``<root>/outputs`` and tmp-dir call sites untouched.
"""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.sim_governance import promotion_approvals as PA

NOW = "2026-07-27T15:00:00+00:00"


def _make_repo_root(p: Path) -> None:
    (p / "config.json").write_text("{}")
    (p / "CLAUDE.md").write_text("# repo root marker")


def test_write_rejected_when_base_dir_is_repo_root(tmp_path):
    _make_repo_root(tmp_path)
    res = PA.record_approval("prop_x", "approve", "pesantez", NOW, base_dir=str(tmp_path))
    assert res["ok"] is False
    assert "repo_root" in res["reason"]
    # Nothing was written to the misdirected location.
    assert not (tmp_path / "promotion_approvals" / "approved_proposals.json").exists()


def test_write_allowed_for_outputs_style_base_dir(tmp_path):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    res = PA.record_approval("prop_x", "approve", "pesantez", NOW, base_dir=str(outputs))
    assert res["ok"] is True
    written = outputs / "promotion_approvals" / "approved_proposals.json"
    assert written.exists()
    data = json.loads(written.read_text())
    assert data["approvals"][-1]["proposal_id"] == "prop_x"


def test_write_allowed_for_plain_tmp_base_dir(tmp_path):
    # Existing tests pass a bare tmp_path (no repo-root markers) — must stay OK.
    res = PA.record_approval("prop_y", "approve", "operator: Enrique", NOW, base_dir=str(tmp_path))
    assert res["ok"] is True
