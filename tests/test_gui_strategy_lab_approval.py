"""POST /dashboard/strategy-lab/decide — human approve/reject/defer a strategy.

The deliberate GUI click is the human approval. Approving writes
active_strategy_selection.json (POLICY) and logs to strategy_decisions.jsonl;
the recompute is guarded (degraded sandbox data here is non-fatal). REPO_ROOT is
pointed at tmp so nothing touches the real outputs tree.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import gui_v2.app as appmod

client = TestClient(appmod.app)

VALID = ["long_term_compounding", "tax_aware", "balanced_core_satellite"]


def _seed_queue(root: Path, ids):
    p = root / "outputs" / "latest"
    p.mkdir(parents=True, exist_ok=True)
    queue = {"queue": [{"strategy_id": i, "name": i.replace("_", " ").title()} for i in ids]}
    (p / "strategy_review_queue.json").write_text(json.dumps(queue), encoding="utf-8")


def _sel(root: Path) -> dict:
    p = root / "outputs" / "policy" / "active_strategy_selection.json"
    return json.loads(p.read_text()) if p.exists() else {}


def test_approve_writes_selection_and_redirects(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "operator")
    _seed_queue(tmp_path, VALID)
    r = client.post(
        "/dashboard/strategy-lab/decide",
        data={"strategy_id": "tax_aware", "decision": "approve"},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    sel = _sel(tmp_path)
    assert sel["active_strategy_id"] == "tax_aware"
    assert sel["approved_by"] == "operator"


def test_reject_redirects(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "operator")
    _seed_queue(tmp_path, VALID)
    r = client.post(
        "/dashboard/strategy-lab/decide",
        data={"strategy_id": "tax_aware", "decision": "reject"},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    log = (tmp_path / "outputs" / "policy" / "strategy_decisions.jsonl")
    assert log.exists()
    assert json.loads(log.read_text().splitlines()[-1])["decision"] == "reject"


def test_invalid_strategy_id_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "operator")
    _seed_queue(tmp_path, VALID)
    r = client.post(
        "/dashboard/strategy-lab/decide",
        data={"strategy_id": "ghost_strategy", "decision": "approve"},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert _sel(tmp_path).get("active_strategy_id") is None


def test_missing_decision_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "operator")
    _seed_queue(tmp_path, VALID)
    r = client.post(
        "/dashboard/strategy-lab/decide",
        data={"strategy_id": "tax_aware", "decision": "frobnicate"},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 400
