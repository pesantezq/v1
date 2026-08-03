# tests/test_governance_digest_two_tier.py
"""Governance digest must summarize BOTH authority tiers, in both body formats.

Phase 5 of the memo-suite redesign (docs/MEMO_SUITE_REDESIGN_PHASE0_AUDIT.md §5).
The Phase 0 audit found three defects here, all confirmed against the shipped code:

  1. ``run_evening_digest`` never passed ``pending_proposals``, so
     ``pending_human_proposals`` was ALWAYS [] in production no matter how many
     production promotions awaited a human. ``approval_packet.py`` already
     consolidates both tiers and was never consulted.
  2. ``pending_human_proposals`` was rendered in ``_render_html`` but NOWHERE in
     ``_render_text`` — an HTML/plain parity violation, masked only because
     defect 1 kept the list empty.
  3. ``_render_text`` asserted "No auto-approval activity in this period." from a
     predicate that ignored pending production reviews entirely.

Authority invariant under test throughout: the digest REPORTS governance state.
It never approves, mutates, or creates an email-based production mutation path.
"""
from __future__ import annotations

from portfolio_automation.sim_governance import auto_approval as AA
from portfolio_automation.sim_governance import governance_digest as GD

NOW = "2026-08-03T22:00:00+00:00"


def _pending(pid="prop_1", created="2026-08-01T09:00:00+00:00", symbol="VFH"):
    return {
        "proposal_id": pid,
        "workflow": "watchlist_promotion",
        "proposal_type": "add_symbol",
        "symbol": symbol,
        "approval_status": "pending",
        "created_at": created,
        "status": "pending human review",
    }


def _applied(symbol="NVDA", eid="evt_a", applied_at="2026-08-03T12:00:00+00:00"):
    return {"kind": AA.EVENT_APPLIED, "event_id": eid, "target_id": symbol,
            "candidate_type": "watchlist", "confidence": 0.92,
            "gpt_reasoning": "clean evidence", "gate_trace": [],
            "application_timestamp": applied_at, "ts": applied_at}


def _build(**kw):
    kw.setdefault("summary", {"active_items": [], "circuit_breaker": {"engaged": False}})
    kw.setdefault("events", [])
    kw.setdefault("now", NOW)
    return GD.build_governance_digest(**kw)


# --------------------------------------------------------------------------
# Scenario 10 — pending production queue with zero simulation activity
# --------------------------------------------------------------------------

def test_pending_production_appears_in_text_body():
    """The parity defect: text never rendered this key at all."""
    d = _build(pending_proposals=[_pending("p1"), _pending("p2")])
    assert "2" in d["text"]
    assert "pending" in d["text"].lower()
    assert "production" in d["text"].lower()


def test_pending_production_appears_in_html_body():
    d = _build(pending_proposals=[_pending("p1"), _pending("p2")])
    assert "production" in d["html"].lower()
    assert "2" in d["html"]


def test_digest_never_claims_nothing_to_do_while_production_is_pending():
    """Scenario 10: no auto-approval activity, but 6 production reviews wait."""
    d = _build(pending_proposals=[_pending(f"p{i}") for i in range(6)])
    for fmt in ("text", "html"):
        body = d[fmt]
        assert "No auto-approval activity in this period." not in body, (
            f"{fmt} still asserts nothing happened while 6 production reviews are pending"
        )
        assert "6" in body


def test_pending_count_is_exposed_in_the_payload():
    d = _build(pending_proposals=[_pending("p1"), _pending("p2")])
    assert d["json"]["counts_two_tier"]["production_pending"] == 2


# --------------------------------------------------------------------------
# GREEN / AMBER / RED rollup
# --------------------------------------------------------------------------

def test_status_is_green_in_true_steady_state():
    d = _build()
    assert d["json"]["governance_status"] == "GREEN"
    assert d["json"]["status_reasons"] == []


def test_steady_state_renders_one_concise_green_line():
    """Scenario 11: concise GREEN, no noisy empty sections."""
    d = _build()
    for fmt in ("text", "html"):
        body = d[fmt]
        assert "GREEN" in body
        assert "0 production approvals pending" in body
        # No empty section headers for things that did not happen.
        assert "Rollback conflicts" not in body
        assert "Human vetoes" not in body


def test_pending_production_is_amber():
    d = _build(pending_proposals=[_pending()])
    assert d["json"]["governance_status"] == "AMBER"
    assert any("production_pending" in r for r in d["json"]["status_reasons"])


def test_awaiting_veto_is_amber():
    d = _build(events=[_applied()])
    assert d["json"]["governance_status"] == "AMBER"


def test_failed_application_is_red():
    d = _build(events=[{"kind": AA.EVENT_FAILURE, "event_id": "e", "ts": NOW}])
    assert d["json"]["governance_status"] == "RED"


def test_authority_rejection_is_red():
    """An authority-gate breach is the one thing that must never read GREEN."""
    d = _build(events=[{"kind": AA.EVENT_DETERMINISTIC_REJECT, "event_id": "e",
                        "reason": "authority_gate_failed", "ts": NOW}])
    assert d["json"]["governance_status"] == "RED"


def test_circuit_breaker_is_red():
    d = _build(summary={"active_items": [],
                        "circuit_breaker": {"engaged": True, "reason": "corrupt_ledger"}})
    assert d["json"]["governance_status"] == "RED"


def test_red_outranks_amber():
    d = _build(events=[{"kind": AA.EVENT_FAILURE, "event_id": "e", "ts": NOW}],
               pending_proposals=[_pending()])
    assert d["json"]["governance_status"] == "RED"


def test_status_leads_both_bodies():
    d = _build(pending_proposals=[_pending()])
    assert d["text"].lstrip().startswith("GOVERNANCE — AMBER")
    assert "GOVERNANCE — AMBER" in d["html"]


# --------------------------------------------------------------------------
# Operator decision-queue aging
# --------------------------------------------------------------------------

def test_oldest_pending_age_is_reported():
    d = _build(pending_proposals=[
        _pending("old", created="2026-07-28T09:00:00+00:00"),
        _pending("new", created="2026-08-03T09:00:00+00:00"),
    ])
    assert d["json"]["counts_two_tier"]["oldest_pending_age_days"] == 6


def test_missing_created_at_does_not_become_zero_age():
    """A missing timestamp must not silently read as 'brand new'."""
    p = _pending()
    p.pop("created_at")
    d = _build(pending_proposals=[p])
    assert d["json"]["counts_two_tier"]["oldest_pending_age_days"] is None


def test_stale_pending_count_from_packet_health():
    health = {"status": "AMBER", "reasons": ["stale_pending:prop_1:5d"], "counts": {}}
    d = _build(pending_proposals=[_pending()], packet_health=health)
    assert d["json"]["counts_two_tier"]["stale_pending"] == 1


def test_packet_health_red_escalates_the_rollup():
    """gate drift detected by assess_packet_health must not be downgraded."""
    health = {"status": "RED", "reasons": ["packet_gate_drift:prop_9"], "counts": {}}
    d = _build(packet_health=health)
    assert d["json"]["governance_status"] == "RED"
    assert any("packet_gate_drift" in r for r in d["json"]["status_reasons"])


# --------------------------------------------------------------------------
# Scenario 12 — HTML / plain-text parity on critical facts
# --------------------------------------------------------------------------

def test_html_and_text_agree_on_every_critical_fact():
    d = _build(
        events=[_applied(), {"kind": AA.EVENT_ROLLBACK_CONFLICT, "event_id": "c", "ts": NOW}],
        pending_proposals=[_pending("p1"), _pending("p2"), _pending("p3")],
        summary={"active_items": [], "circuit_breaker": {"engaged": True, "reason": "anomaly"}},
    )
    text, html = d["text"], d["html"]
    for fact in ("RED", "3", "Circuit breaker", "human-gated"):
        assert fact.lower() in text.lower(), f"text missing critical fact: {fact}"
        assert fact.lower() in html.lower(), f"html missing critical fact: {fact}"


def test_production_human_gated_statement_always_present():
    for d in (_build(), _build(pending_proposals=[_pending()])):
        assert "human-gated" in d["text"].lower()
        assert "human-gated" in d["html"].lower()


# --------------------------------------------------------------------------
# Authority invariant — the digest reports, it never approves
# --------------------------------------------------------------------------

def test_digest_exposes_no_approval_or_mutation_affordance():
    d = _build(pending_proposals=[_pending()])
    body = (d["text"] + d["html"]).lower()
    for forbidden in ("approve?proposal", "/approve?", "auto-approved for production",
                      "production approved"):
        assert forbidden not in body, f"digest exposes a production mutation affordance: {forbidden}"


def test_builder_does_not_mutate_its_inputs():
    events = [_applied()]
    pending = [_pending()]
    summary = {"active_items": [], "circuit_breaker": {"engaged": False}}
    before = (repr(events), repr(pending), repr(summary))
    GD.build_governance_digest(summary=summary, events=events, now=NOW,
                               pending_proposals=pending)
    assert (repr(events), repr(pending), repr(summary)) == before


# --------------------------------------------------------------------------
# Determinism (scenario 14)
# --------------------------------------------------------------------------

def test_same_inputs_produce_identical_bodies():
    a = _build(events=[_applied()], pending_proposals=[_pending()])
    b = _build(events=[_applied()], pending_proposals=[_pending()])
    assert a["text"] == b["text"]
    assert a["html"] == b["html"]
    assert a["json"]["governance_status"] == b["json"]["governance_status"]


# --------------------------------------------------------------------------
# Wiring guard — the audit's defect 1 must not silently regress
# --------------------------------------------------------------------------

def test_run_evening_digest_consults_the_operator_approval_packet(monkeypatch, tmp_path):
    """``pending_proposals`` was an accepted-but-never-passed parameter for months.

    This asserts the production entry point actually sources tier-b from
    ``approval_packet``. It fails ON PURPOSE if someone drops the wiring — the
    parameter defaulting to None means a regression here is otherwise invisible.
    """
    import json as _json

    from portfolio_automation.sim_governance import approval_packet as AP

    (tmp_path / "config.json").write_text(_json.dumps({
        "sim_governance": {
            "auto_approval": {"evening_digest": {"enabled": True}, "veto_window_hours": 48},
            "approval_packet": {"deep_link_base": "https://example.invalid"},
        }
    }))

    seen: dict = {}

    def _fake_build(**kw):
        seen.update(kw)
        return {"json": {}, "text": "t", "html": "h", "subject_date": NOW[:10],
                "approval_page_url": "", "governance_status": "AMBER"}

    monkeypatch.setattr(GD, "build_governance_digest", _fake_build)
    monkeypatch.setattr(AP, "build_operator_packet",
                        lambda *a, **k: {"tier_production": [_pending("wired_1")]})
    monkeypatch.setattr(AP, "assess_packet_health",
                        lambda *a, **k: {"status": "AMBER", "reasons": ["stale_pending:x:9d"]})
    monkeypatch.setattr(GD, "send_governance_digest", lambda d, **k: {"status": "skipped"})

    GD.run_evening_digest(root=str(tmp_path), now=NOW, env={}, write_files=False)

    assert "pending_proposals" in seen, "run_evening_digest no longer passes pending_proposals"
    assert [p["proposal_id"] for p in seen["pending_proposals"]] == ["wired_1"]
    assert seen.get("packet_health", {}).get("status") == "AMBER"


def test_unreadable_packet_degrades_to_amber_not_an_implied_empty_queue(monkeypatch, tmp_path):
    """If the production queue cannot be read we must not imply it is empty."""
    import json as _json

    from portfolio_automation.sim_governance import approval_packet as AP

    (tmp_path / "config.json").write_text(_json.dumps({
        "sim_governance": {"auto_approval": {"evening_digest": {"enabled": True}}}
    }))

    seen: dict = {}

    def _fake_build(**kw):
        seen.update(kw)
        return {"json": {}, "text": "t", "html": "h", "subject_date": NOW[:10],
                "approval_page_url": "", "governance_status": "AMBER"}

    def _boom(*a, **k):
        raise RuntimeError("packet unreadable")

    monkeypatch.setattr(GD, "build_governance_digest", _fake_build)
    monkeypatch.setattr(AP, "build_operator_packet", _boom)
    monkeypatch.setattr(GD, "send_governance_digest", lambda d, **k: {"status": "skipped"})

    GD.run_evening_digest(root=str(tmp_path), now=NOW, env={}, write_files=False)

    assert seen["packet_health"]["status"] == "AMBER"
    assert any("approval_packet_unavailable" in r for r in seen["packet_health"]["reasons"])


# --------------------------------------------------------------------------
# Subject line — human action state, not a bare date
# --------------------------------------------------------------------------

def test_subject_conveys_review_count_and_health():
    d = _build(pending_proposals=[_pending(f"p{i}") for i in range(6)])
    subj = GD.build_subject(d)
    assert "6 production review" in subj.lower()
    assert "AMBER" in subj
    assert d["subject_date"] in subj


def test_subject_is_concise_in_steady_state():
    subj = GD.build_subject(_build())
    assert "GREEN" in subj
    assert "0 production reviews" in subj.lower()


def test_subject_flags_red_health():
    d = _build(events=[{"kind": AA.EVENT_FAILURE, "event_id": "e", "ts": NOW}])
    assert "RED" in GD.build_subject(d)
