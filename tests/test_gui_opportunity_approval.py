"""POST /dashboard/strategy-lab/opportunity-decide — market-opportunity decisions.

Records the routing decision to user_decisions.jsonl; approve_to_watchlist_review
additionally runs the guarded operator-promotion. REPO_ROOT points at tmp so
nothing touches the real outputs tree or DB.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import gui_v2.app as appmod

client = TestClient(appmod.app)

QUEUE = [
    {"id": "mo-panw", "candidate": "PANW",
     "allowed_actions": ["approve_to_watchlist_review", "reject", "keep_watching"],
     "blocked_actions": ["place_trade"]},
    {"id": "mo-xom", "candidate": "XOM",
     "allowed_actions": ["reject", "keep_watching"], "blocked_actions": ["place_trade"]},
]


def _seed(root: Path):
    p = root / "outputs" / "latest"
    p.mkdir(parents=True, exist_ok=True)
    (p / "operator_action_queue.json").write_text(json.dumps({"queue": QUEUE}), encoding="utf-8")


def _decisions(root: Path):
    p = root / "outputs" / "policy" / "user_decisions.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def test_reject_logs_and_redirects(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "operator")
    _seed(tmp_path)
    r = client.post("/dashboard/strategy-lab/opportunity-decide",
                    data={"opportunity_id": "mo-xom", "action": "reject"},
                    headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303
    assert _decisions(tmp_path)[-1]["action"] == "reject"


def test_approve_logs_with_promote_result(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "operator")
    _seed(tmp_path)
    r = client.post("/dashboard/strategy-lab/opportunity-decide",
                    data={"opportunity_id": "mo-panw", "action": "approve_to_watchlist_review"},
                    headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303
    row = _decisions(tmp_path)[-1]
    assert row["action"] == "approve_to_watchlist_review"
    assert row["candidate"] == "PANW"
    # promotion was attempted (result present, success or guarded skip/error)
    assert "promote_result" in row


def test_action_not_allowed_for_item_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "operator")
    _seed(tmp_path)
    # mo-xom does not allow approve_to_watchlist_review
    r = client.post("/dashboard/strategy-lab/opportunity-decide",
                    data={"opportunity_id": "mo-xom", "action": "approve_to_watchlist_review"},
                    headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 400


def test_blocked_trade_verb_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "operator")
    _seed(tmp_path)
    r = client.post("/dashboard/strategy-lab/opportunity-decide",
                    data={"opportunity_id": "mo-panw", "action": "place_trade"},
                    headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 400
    assert _decisions(tmp_path) == []
