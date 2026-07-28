"""Guard: promotion_approvals.record_approval must refuse to write while the
approvals log is unreadable (present but unparseable).

``_load_raw`` degrades any read failure to ``{"approvals": []}``. That is safe
for callers that treat an absent log as "no approvals yet", but ``record_approval``
does a whole-document read-modify-write: if the log is present-but-corrupt, the
degraded read silently drops every one of the existing (e.g. 43) approval
records and rewrites the file with just the one new record. The file then
parses again, so the corruption self-heals into data loss instead of surfacing
— a silent reversal of established human production membership reached through
the front door.

``approvals_log_unreadable`` (promotion_approvals.py:64) already distinguishes
"absent" (None — legitimate, no approvals yet) from "present but unparseable"
(a reason string). ``record_approval`` must consult it and refuse to write in
the latter case, leaving the corrupt file untouched for an operator to recover.
"""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.sim_governance import promotion_approvals as PA

NOW = "2026-07-28T15:00:00+00:00"


def _outputs(tmp_path: Path) -> Path:
    d = tmp_path / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_approval_refused_when_approvals_log_is_corrupt(tmp_path):
    outputs = _outputs(tmp_path)
    appr_dir = outputs / "promotion_approvals"
    appr_dir.mkdir(parents=True, exist_ok=True)
    log_path = appr_dir / "approved_proposals.json"
    corrupt_bytes = b'{"approvals": [invalid json truncated'
    log_path.write_bytes(corrupt_bytes)

    res = PA.record_approval("prop_x", "approve", "pesantez", NOW, base_dir=str(outputs))

    assert res["ok"] is False
    assert res["record"] is None
    assert "unreadable" in res["reason"] or "unparseable" in res["reason"]
    # The corrupt file must be left byte-for-byte unchanged — no read-modify-write
    # through the degraded {"approvals": []} view.
    assert log_path.read_bytes() == corrupt_bytes


def test_approval_refused_when_approvals_field_is_not_a_list(tmp_path):
    outputs = _outputs(tmp_path)
    appr_dir = outputs / "promotion_approvals"
    appr_dir.mkdir(parents=True, exist_ok=True)
    log_path = appr_dir / "approved_proposals.json"
    bad_payload = json.dumps({"approvals": "not-a-list"})
    log_path.write_text(bad_payload, encoding="utf-8")

    res = PA.record_approval("prop_x", "approve", "pesantez", NOW, base_dir=str(outputs))

    assert res["ok"] is False
    assert res["record"] is None
    assert log_path.read_text(encoding="utf-8") == bad_payload


def test_approval_still_succeeds_when_log_is_absent(tmp_path):
    outputs = _outputs(tmp_path)
    res = PA.record_approval("prop_y", "approve", "pesantez", NOW, base_dir=str(outputs))
    assert res["ok"] is True
    written = outputs / "promotion_approvals" / "approved_proposals.json"
    assert written.exists()
    data = json.loads(written.read_text(encoding="utf-8"))
    assert data["approvals"][-1]["proposal_id"] == "prop_y"


def test_approval_still_succeeds_when_log_is_valid_and_preserves_prior_records(tmp_path):
    outputs = _outputs(tmp_path)
    appr_dir = outputs / "promotion_approvals"
    appr_dir.mkdir(parents=True, exist_ok=True)
    log_path = appr_dir / "approved_proposals.json"
    existing = {
        "generated_at": "2026-07-01T00:00:00+00:00",
        "schema": "approved_proposals.v1",
        "approvals": [
            {"proposal_id": f"prop_{i}", "decision": "approve", "approver": "pesantez",
             "timestamp": "2026-07-01T00:00:00+00:00"}
            for i in range(43)
        ],
    }
    log_path.write_text(json.dumps(existing), encoding="utf-8")

    res = PA.record_approval("prop_new", "approve", "pesantez", NOW, base_dir=str(outputs))

    assert res["ok"] is True
    data = json.loads(log_path.read_text(encoding="utf-8"))
    assert len(data["approvals"]) == 44, "the 43 prior records must survive"
    assert data["approvals"][-1]["proposal_id"] == "prop_new"
