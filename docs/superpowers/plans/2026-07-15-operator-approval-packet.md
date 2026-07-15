# One-Shot Operator Approval Packet — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a single consolidated per-cycle "approval packet" that shows the operator both governance tiers at once — auto-applied *simulation* items (vetoable) and pending *production* candidates (approvable per-item or in bulk) — delivered by email deep link and actioned in the authenticated GUI.

**Architecture:** A read-only builder assembles the two-tier packet from existing artifacts (`auto_approval.build_summary()["active_items"]` + `promotion_proposals.load_pending_proposals()`) into one artifact both the email and GUI read. Tier-b approval reuses the existing human-gated `promotion_approvals.record_approval`; no new production-mutation path is introduced. Ships gated behind `sim_governance.approval_packet.enabled=false`.

**Tech Stack:** Python 3.12, pytest, FastAPI (gui_v2), sqlite/json artifacts, `portfolio_automation.data_governance.OutputNamespace` for all writes.

## Global Constraints

- Run tests with the repo venv: `./.venv/bin/python -m pytest -q <path>`.
- All file writes go through `OutputNamespace`. Packet artifact → `OutputNamespace.PROMOTION_REVIEW` (`outputs/promotion_review/`).
- Do NOT modify `decision_engine.py` or any score semantics (`signal_score`, `confidence_score`, `effective_score`, `conviction_score`, `final_rank_score`, `recommendation_score`).
- Do NOT add a new production-mutation path. Tier-b approval calls only the existing `promotion_approvals.record_approval`.
- Human decision values are exactly `"approve"` / `"reject"` (`schemas.HUMAN_APPROVE` / `HUMAN_REJECT`).
- The builder is read-only: it MUST hardcode `observe_only: true` and write only its own artifact.
- Every new feature ships gated OFF and must be backward compatible (disabled ⇒ no behavior change).
- Stage git paths explicitly; never `git commit -am` (the working tree carries unrelated modified runtime files).
- Branch: `feat/operator-approval-packet` (already created off `main`; the design spec commit `5e88cfb0` is its first commit).

---

### Task 1: Packet builder module

**Files:**
- Create: `portfolio_automation/sim_governance/approval_packet.py`
- Modify: `portfolio_automation/sim_governance/__init__.py` (add `"approval_packet"` to the module export list, mirroring the existing `"ai_review_packet"` entry)
- Test: `tests/test_approval_packet.py`

**Interfaces:**
- Consumes:
  - `auto_approval.build_summary(*, base_dir, now)` → dict with `active_items: list[dict]` (each has `event_id, candidate_type, symbol, strategy_id, applied_at, confidence`).
  - `promotion_proposals.load_pending_proposals(base_dir)` → `list[dict]` (each has `proposal_id, workflow, proposal_type, candidate_id, proposed_production_change, risk_summary, rollback_plan, approval_status, evidence_refs, created_at`).
  - `data_governance.OutputNamespace`, `safe_write_json`, `safe_write_text`.
- Produces:
  - `build_operator_packet(base_dir: str, now: str, *, deep_link_base: str = "", veto_window_hours: int = 48) -> dict`
  - `write_operator_packet(packet: dict, *, base_dir: str) -> dict`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_approval_packet.py
import json
from pathlib import Path

from portfolio_automation.sim_governance import approval_packet as ap


def _seed_pending(base_dir, proposals):
    d = Path(base_dir) / "promotion_review"
    d.mkdir(parents=True, exist_ok=True)
    (d / "pending_proposals.json").write_text(
        json.dumps({"schema": "pending_proposals.v1", "proposals": proposals}),
        encoding="utf-8",
    )


def test_build_packet_two_tiers(tmp_path, monkeypatch):
    base = str(tmp_path / "outputs")
    # tier-a: stub the sim summary
    monkeypatch.setattr(
        ap.auto_approval, "build_summary",
        lambda *, base_dir, now: {
            "active_items": [
                {"event_id": "ev1", "candidate_type": "watchlist", "symbol": "XOM",
                 "strategy_id": None, "applied_at": "2026-07-15T00:00:00+00:00",
                 "confidence": 0.9},
            ],
            "active_item_count": 1,
        },
    )
    # tier-b: pending proposals on disk (one pending, one already approved -> excluded)
    _seed_pending(base, [
        {"proposal_id": "p1", "workflow": "watchlist", "proposal_type": "watchlist_add",
         "candidate_id": "c1", "proposed_production_change": {"symbol": "CVX"},
         "risk_summary": "low", "rollback_plan": "remove", "approval_status": "pending",
         "evidence_refs": ["e1"], "created_at": "2026-07-14T00:00:00+00:00"},
        {"proposal_id": "p2", "approval_status": "approved"},
    ])
    packet = ap.build_operator_packet(base, "2026-07-15T12:00:00+00:00",
                                      deep_link_base="https://x", veto_window_hours=48)
    assert packet["schema"] == "operator_approval_packet.v1"
    assert packet["observe_only"] is True
    assert packet["approval_page_url"] == "https://x/dashboard/governance"
    assert packet["counts"] == {"tier_sim_within_veto": 1, "tier_production_pending": 1}
    assert packet["tier_sim"][0]["event_id"] == "ev1"
    assert packet["tier_sim"][0]["veto_deadline"] == "2026-07-17T00:00:00+00:00"
    assert packet["tier_sim"][0]["status"] == "auto-applied in simulation · veto available"
    assert packet["tier_production"][0]["proposal_id"] == "p1"
    assert packet["tier_production"][0]["symbol"] == "CVX"
    assert packet["tier_production"][0]["status"] == "pending human review"


def test_build_packet_degraded_on_failure(tmp_path, monkeypatch):
    base = str(tmp_path / "outputs")

    def _boom(*, base_dir, now):
        raise RuntimeError("ledger corrupt")

    monkeypatch.setattr(ap.auto_approval, "build_summary", _boom)
    packet = ap.build_operator_packet(base, "2026-07-15T12:00:00+00:00")
    assert packet["observe_only"] is True
    assert packet["tier_sim"] == []
    assert packet["tier_production"] == []
    assert "error" in packet


def test_write_packet_creates_artifacts(tmp_path):
    base = str(tmp_path / "outputs")
    packet = {"schema": "operator_approval_packet.v1", "observe_only": True,
              "generated_at": "n", "tier_sim": [], "tier_production": [],
              "counts": {"tier_sim_within_veto": 0, "tier_production_pending": 0}}
    ap.write_operator_packet(packet, base_dir=base)
    assert (Path(base) / "promotion_review" / "operator_approval_packet.json").exists()
    assert (Path(base) / "promotion_review" / "operator_approval_packet.md").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest -q tests/test_approval_packet.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'portfolio_automation.sim_governance.approval_packet'`.

- [ ] **Step 3: Write the module**

```python
# portfolio_automation/sim_governance/approval_packet.py
"""
One-shot operator approval packet (design 2026-07-15).

Read-only builder that consolidates BOTH governance tiers into ONE artifact both
the evening email and the GUI approval page read:

  * tier-a: simulation items the GPT auto-approval channel auto-applied and that
    are still awaiting veto (source: auto_approval.build_summary active_items).
  * tier-b: production-promotion candidates still pending human approval
    (source: promotion_proposals.load_pending_proposals, approval_status=pending).

This module NEVER mutates governance state. Production approval happens only via
the existing human-gated promotion_approvals.record_approval, invoked from the GUI.

Writes:
  * outputs/promotion_review/operator_approval_packet.json
  * outputs/promotion_review/operator_approval_packet.md
"""
from __future__ import annotations

import datetime as _dt
import logging

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)
from portfolio_automation.sim_governance import auto_approval, promotion_proposals

logger = logging.getLogger("stockbot.sim_governance.approval_packet")

_PACKET_JSON = "operator_approval_packet.json"
_PACKET_MD = "operator_approval_packet.md"
_SCHEMA = "operator_approval_packet.v1"


def _veto_deadline(applied_at: str, veto_window_hours: int) -> str | None:
    try:
        t = _dt.datetime.fromisoformat(applied_at)
        return (t + _dt.timedelta(hours=veto_window_hours)).isoformat()
    except Exception:
        return None


def _sim_item(item: dict, veto_window_hours: int) -> dict:
    applied_at = item.get("applied_at")
    return {
        "event_id": item.get("event_id"),
        "candidate_type": item.get("candidate_type"),
        "symbol_or_strategy": item.get("symbol") or item.get("strategy_id"),
        "applied_at": applied_at,
        "veto_deadline": _veto_deadline(applied_at, veto_window_hours) if applied_at else None,
        "confidence": item.get("confidence"),
        "target_lane": "simulation",
        "feeds_decision_engine": False,
        "status": "auto-applied in simulation · veto available",
    }


def _prod_item(p: dict) -> dict:
    change = p.get("proposed_production_change") or {}
    return {
        "proposal_id": p.get("proposal_id"),
        "workflow": p.get("workflow"),
        "proposal_type": p.get("proposal_type"),
        "candidate_id": p.get("candidate_id"),
        "symbol": change.get("symbol"),
        "change": change,
        "risk_summary": p.get("risk_summary"),
        "rollback_plan": p.get("rollback_plan"),
        "evidence": p.get("evidence_refs", []),
        "approval_status": p.get("approval_status"),
        "created_at": p.get("created_at"),
        "status": "pending human review",
    }


def build_operator_packet(base_dir: str, now: str, *, deep_link_base: str = "",
                          veto_window_hours: int = 48) -> dict:
    """Assemble the two-tier packet. Read-only; never raises."""
    packet = {
        "schema": _SCHEMA,
        "observe_only": True,
        "generated_at": now,
        "generated_by": "portfolio_automation.sim_governance.approval_packet",
        "approval_page_url": (f"{deep_link_base.rstrip('/')}/dashboard/governance"
                              if deep_link_base else "/dashboard/governance"),
        "tier_sim": [],
        "tier_production": [],
        "counts": {"tier_sim_within_veto": 0, "tier_production_pending": 0},
    }
    try:
        summary = auto_approval.build_summary(base_dir=base_dir, now=now)
        packet["tier_sim"] = [_sim_item(i, veto_window_hours)
                              for i in (summary.get("active_items") or [])]
        pending = promotion_proposals.load_pending_proposals(base_dir)
        packet["tier_production"] = [_prod_item(p) for p in pending
                                     if (p.get("approval_status") == "pending")]
        packet["counts"] = {
            "tier_sim_within_veto": len(packet["tier_sim"]),
            "tier_production_pending": len(packet["tier_production"]),
        }
    except Exception as exc:  # degraded, never raise into the pipeline
        logger.warning("approval_packet: build failed: %s", exc)
        packet["error"] = str(exc)
    return packet


def _render_md(packet: dict) -> str:
    c = packet.get("counts", {})
    lines = [
        "# Operator Approval Packet",
        "",
        f"Generated: {packet.get('generated_at')}",
        f"Review & approve: {packet.get('approval_page_url')}",
        "",
        f"## Simulation items awaiting veto ({c.get('tier_sim_within_veto', 0)})",
    ]
    for i in packet.get("tier_sim", []):
        lines.append(f"- [{i.get('candidate_type')}] {i.get('symbol_or_strategy')} "
                     f"(event {i.get('event_id')}) — {i.get('status')}")
    lines += ["", f"## Production candidates pending approval "
                  f"({c.get('tier_production_pending', 0)})"]
    for p in packet.get("tier_production", []):
        lines.append(f"- [{p.get('workflow')}] {p.get('symbol')} "
                     f"(proposal {p.get('proposal_id')}) — {p.get('status')}")
    return "\n".join(lines) + "\n"


def write_operator_packet(packet: dict, *, base_dir: str) -> dict:
    """Write JSON + MD artifacts. Best-effort; logs on failure."""
    try:
        safe_write_json(OutputNamespace.PROMOTION_REVIEW, _PACKET_JSON, packet,
                        base_dir=base_dir)
        safe_write_text(OutputNamespace.PROMOTION_REVIEW, _PACKET_MD, _render_md(packet),
                        base_dir=base_dir)
    except Exception as exc:
        logger.warning("approval_packet: write failed: %s", exc)
    return packet
```

Then add the export in `portfolio_automation/sim_governance/__init__.py` — find the list containing `"ai_review_packet",` and add `"approval_packet",` next to it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/bin/python -m pytest -q tests/test_approval_packet.py`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/sim_governance/approval_packet.py \
        portfolio_automation/sim_governance/__init__.py \
        tests/test_approval_packet.py
git commit -m "feat(sim-gov): read-only two-tier operator approval packet builder

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Pipeline integration (Step 8, gated + non-blocking)

**Files:**
- Modify: `portfolio_automation/sim_governance/daily_governance_run.py` (insert after the Step 7 `production_application` block, before the roll-up block)
- Modify: `portfolio_automation/sim_governance/daily_governance_run.py` (the module import list near line 30 — add `approval_packet`)
- Modify: `portfolio_automation/sim_governance/daily_governance_run.py` (`_KNOWN_STAGES`-style init near line 58/73 — add `"approval_packet"` so the stage always appears)
- Test: `tests/test_approval_packet_pipeline.py`

**Interfaces:**
- Consumes: `approval_packet.build_operator_packet(base_dir, now, *, deep_link_base, veto_window_hours)`, `approval_packet.write_operator_packet(packet, *, base_dir)`. Config is the `sim_governance` subtree `cfg`; the block lives at `cfg["approval_packet"]` and the window at `cfg["auto_approval"]["veto_window_hours"]`.
- Produces: `status["stages"]["approval_packet"]` — `{ok, status|counts}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_approval_packet_pipeline.py
from pathlib import Path

from portfolio_automation.sim_governance import daily_governance_run as dgr


def test_step8_disabled_by_default(tmp_path, monkeypatch):
    # Minimal config: sim_governance enabled but approval_packet OFF.
    cfg = {"enabled": True, "simulation_lane": {"enabled": True},
           "ai_review": {"enabled": False}, "approval_packet": {"enabled": False}}
    status = dgr.run_daily_governance(tmp_path, "2026-07-15T00:00:00+00:00",
                                      config=cfg, write_files=False)
    assert status["stages"]["approval_packet"]["status"] == "disabled"


def test_step8_builds_when_enabled(tmp_path, monkeypatch):
    called = {}

    def _fake_build(base_dir, now, *, deep_link_base="", veto_window_hours=48):
        called["deep_link_base"] = deep_link_base
        called["veto_window_hours"] = veto_window_hours
        return {"counts": {"tier_sim_within_veto": 2, "tier_production_pending": 3}}

    monkeypatch.setattr(dgr.approval_packet, "build_operator_packet", _fake_build)
    monkeypatch.setattr(dgr.approval_packet, "write_operator_packet",
                        lambda packet, *, base_dir: packet)
    cfg = {"enabled": True, "simulation_lane": {"enabled": True},
           "ai_review": {"enabled": False},
           "auto_approval": {"veto_window_hours": 24},
           "approval_packet": {"enabled": True, "deep_link_base": "https://x"}}
    status = dgr.run_daily_governance(tmp_path, "2026-07-15T00:00:00+00:00",
                                      config=cfg, write_files=False)
    assert status["stages"]["approval_packet"]["ok"] is True
    assert status["stages"]["approval_packet"]["counts"]["tier_production_pending"] == 3
    assert called == {"deep_link_base": "https://x", "veto_window_hours": 24}


def test_step8_never_sinks_run_on_error(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(dgr.approval_packet, "build_operator_packet", _boom)
    cfg = {"enabled": True, "simulation_lane": {"enabled": True},
           "ai_review": {"enabled": False},
           "approval_packet": {"enabled": True}}
    status = dgr.run_daily_governance(tmp_path, "2026-07-15T00:00:00+00:00",
                                      config=cfg, write_files=False)
    assert status["stages"]["approval_packet"]["ok"] is False
    assert "kaboom" in status["stages"]["approval_packet"]["error"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./.venv/bin/python -m pytest -q tests/test_approval_packet_pipeline.py`
Expected: FAIL — `AttributeError: module ... has no attribute 'approval_packet'` (import not added yet) or `KeyError: 'approval_packet'`.

- [ ] **Step 3: Add the import and the Step 8 block**

In the module import group near line 30 (the one listing `ai_review_packet,`), add `approval_packet,`:

```python
    ai_review_packet,
    approval_packet,
```

In the stage-init block (near line 58/73, the dict/loop that pre-seeds known stages), add `"approval_packet"` to the known-stage names so it always renders.

Insert this block immediately **after** the Step 7 `production_application` `try/except` and **before** the `# ── roll-up counts` block:

```python
    # ── Step 8: one-shot operator approval packet (gated; non-blocking) ─────
    # Read-only consolidation of tier-a (sim veto) + tier-b (pending production)
    # into ONE artifact the email + GUI read. Introduces NO new mutation path.
    try:
        ap_cfg = cfg.get("approval_packet", {}) or {}
        if ap_cfg.get("enabled"):
            aa_cfg = cfg.get("auto_approval", {}) or {}
            packet = approval_packet.build_operator_packet(
                base_dir, now,
                deep_link_base=ap_cfg.get("deep_link_base", ""),
                veto_window_hours=int(aa_cfg.get("veto_window_hours", 48)))
            if write_files:
                approval_packet.write_operator_packet(packet, base_dir=base_dir)
            status["stages"]["approval_packet"] = {"ok": True,
                                                   "counts": packet.get("counts", {})}
        else:
            status["stages"]["approval_packet"] = {"ok": True, "status": "disabled"}
    except Exception as exc:
        logger.warning("daily_governance: approval_packet stage failed: %s", exc)
        status["stages"]["approval_packet"] = {"ok": False, "error": str(exc)}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./.venv/bin/python -m pytest -q tests/test_approval_packet_pipeline.py`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/sim_governance/daily_governance_run.py \
        tests/test_approval_packet_pipeline.py
git commit -m "feat(sim-gov): wire gated Step 8 approval-packet build into daily governance run

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Email deep link in the evening digest

**Files:**
- Modify: `portfolio_automation/sim_governance/governance_digest.py` (`build_governance_digest` signature + `_render_text` + `_render_html`)
- Modify: `portfolio_automation/sim_governance/governance_digest.py` (`run_evening_digest` — pass `approval_page_url` from the auto-approval/approval_packet config)
- Test: `tests/test_governance_digest.py` (extend)

**Interfaces:**
- Consumes: the existing `build_governance_digest(*, summary, events, now, pending_proposals=None, ...)`.
- Produces: digest dict gains `approval_page_url`; text + HTML render a "Review & approve today's packet →" line when the URL is set.

- [ ] **Step 1: Write the failing test (append to `tests/test_governance_digest.py`)**

```python
def test_digest_includes_approval_deep_link():
    from portfolio_automation.sim_governance import governance_digest as gd
    digest = gd.build_governance_digest(
        summary={"active_items": [], "active_item_count": 0, "counters": {}},
        events=[], now="2026-07-15T00:00:00+00:00",
        pending_proposals=[],
        approval_page_url="https://dash.example/dashboard/governance",
    )
    assert digest["approval_page_url"] == "https://dash.example/dashboard/governance"
    text = gd._render_text(digest)
    assert "https://dash.example/dashboard/governance" in text
    html = gd._render_html(digest)
    assert "https://dash.example/dashboard/governance" in html


def test_digest_omits_link_when_unset():
    from portfolio_automation.sim_governance import governance_digest as gd
    digest = gd.build_governance_digest(
        summary={"active_items": [], "active_item_count": 0, "counters": {}},
        events=[], now="2026-07-15T00:00:00+00:00", pending_proposals=[])
    assert digest.get("approval_page_url") in (None, "")
    assert "Review & approve" not in gd._render_text(digest)
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/python -m pytest -q tests/test_governance_digest.py -k approval_deep_link`
Expected: FAIL — `TypeError: build_governance_digest() got an unexpected keyword argument 'approval_page_url'`.

- [ ] **Step 3: Implement**

In `build_governance_digest`, add the keyword-only param and include it in the returned dict:

```python
def build_governance_digest(*, summary: dict, events: list[dict], now: str,
                            veto_window_hours: int = 48,
                            pending_proposals: list[dict] | None = None,
                            approval_page_url: str | None = None) -> dict:
```

In the returned dict (where `"pending_human_proposals": pending_proposals or [],` is set) add:

```python
        "approval_page_url": approval_page_url or "",
```

In `_render_text(p)`, add near the top of the body (after the header line):

```python
    if p.get("approval_page_url"):
        lines.append(f"Review & approve today's packet → {p['approval_page_url']}")
```

(Use the file's existing `lines` list variable; match its append style.)

In `_render_html(p)`, add near the top of the HTML body:

```python
    if p.get("approval_page_url"):
        url = _esc(p["approval_page_url"])
        parts.append(f'<p><a href="{url}">Review &amp; approve today\'s packet →</a></p>')
```

(Use the file's existing HTML accumulator variable — match the `parts`/`html` name used in `_render_html`.)

In `run_evening_digest`, when building the digest, source the URL from config and pass it through:

```python
    approval_url = ""
    try:
        ap_cfg = (_load_sim_governance_config_block(root) or {}).get("approval_packet", {})
        base = ap_cfg.get("deep_link_base", "")
        if base:
            approval_url = f"{base.rstrip('/')}/dashboard/governance"
    except Exception:
        approval_url = ""
```

Then pass `approval_page_url=approval_url` into the `build_governance_digest(...)` call inside `run_evening_digest`. If `run_evening_digest` already loads a config block (see `_load_auto_approval_config`), reuse that loader to read `approval_packet.deep_link_base` instead of adding a new one — do NOT introduce a duplicate config reader.

- [ ] **Step 4: Run to verify pass**

Run: `./.venv/bin/python -m pytest -q tests/test_governance_digest.py`
Expected: PASS (all existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/sim_governance/governance_digest.py tests/test_governance_digest.py
git commit -m "feat(sim-gov): evening digest links to the operator approval packet page

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: GUI reader + wire packet context into the governance page

**Files:**
- Create: `gui_v2/data/dash_approval_packet.py`
- Modify: `gui_v2/app.py` (the `@app.get("/dashboard/governance"...)` handler — add packet context)
- Test: `tests/test_dash_approval_packet.py`

**Interfaces:**
- Consumes: `outputs/promotion_review/operator_approval_packet.json`.
- Produces: `load_packet_context(outputs_dir) -> dict` — always returns a dict with `available: bool`, `tier_sim: list`, `tier_production: list`, `counts: dict` (degraded-safe).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dash_approval_packet.py
import json
from pathlib import Path

from gui_v2.data.dash_approval_packet import load_packet_context


def test_load_packet_present(tmp_path):
    d = tmp_path / "promotion_review"
    d.mkdir(parents=True)
    (d / "operator_approval_packet.json").write_text(json.dumps({
        "schema": "operator_approval_packet.v1", "observe_only": True,
        "tier_sim": [{"event_id": "ev1"}],
        "tier_production": [{"proposal_id": "p1"}],
        "counts": {"tier_sim_within_veto": 1, "tier_production_pending": 1},
    }), encoding="utf-8")
    ctx = load_packet_context(str(tmp_path))
    assert ctx["available"] is True
    assert ctx["counts"]["tier_production_pending"] == 1
    assert ctx["tier_production"][0]["proposal_id"] == "p1"


def test_load_packet_absent(tmp_path):
    ctx = load_packet_context(str(tmp_path))
    assert ctx["available"] is False
    assert ctx["tier_sim"] == []
    assert ctx["tier_production"] == []
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/python -m pytest -q tests/test_dash_approval_packet.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'gui_v2.data.dash_approval_packet'`.

- [ ] **Step 3: Implement the reader**

```python
# gui_v2/data/dash_approval_packet.py
"""Read-only GUI reader for the one-shot operator approval packet."""
from __future__ import annotations

import json
from pathlib import Path

_EMPTY = {"available": False, "observe_only": True, "tier_sim": [],
          "tier_production": [], "counts": {"tier_sim_within_veto": 0,
                                            "tier_production_pending": 0},
          "approval_page_url": "/dashboard/governance"}


def load_packet_context(outputs_dir: str) -> dict:
    """Load the packet artifact for the governance page. Never raises."""
    path = Path(outputs_dir) / "promotion_review" / "operator_approval_packet.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(_EMPTY)
    return {
        "available": True,
        "observe_only": True,
        "tier_sim": data.get("tier_sim", []) or [],
        "tier_production": data.get("tier_production", []) or [],
        "counts": data.get("counts", {}) or _EMPTY["counts"],
        "approval_page_url": data.get("approval_page_url", "/dashboard/governance"),
        "generated_at": data.get("generated_at"),
    }
```

Then, in `gui_v2/app.py`, locate the governance GET handler (search: `@app.get("/dashboard/governance"`). In its render context, add the packet. Follow the existing try/except-with-fallback pattern used by the other dashboard GET handlers (e.g. `page_dash_strategy_tax`):

```python
    try:
        from gui_v2.data.dash_approval_packet import load_packet_context
        approval_packet_ctx = load_packet_context(str(REPO_ROOT / "outputs"))
    except Exception:
        approval_packet_ctx = {"available": False, "tier_sim": [],
                               "tier_production": [], "counts": {}}
```

and pass `approval_packet=approval_packet_ctx` into the `_render(request, "dashboard/governance.html", ...)` call for that handler.

- [ ] **Step 4: Run to verify pass**

Run: `./.venv/bin/python -m pytest -q tests/test_dash_approval_packet.py`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add gui_v2/data/dash_approval_packet.py gui_v2/app.py tests/test_dash_approval_packet.py
git commit -m "feat(gui): read approval packet into the governance page context

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: GUI approve route (per-item + bulk), reusing the human production gate

**Files:**
- Modify: `gui_v2/app.py` (new `@app.post("/dashboard/governance/approve")` route, placed right after the existing `page_governance_veto` route)
- Test: `tests/test_gui_governance_approve.py`

**Interfaces:**
- Consumes: `promotion_approvals.record_approval(proposal_id, decision, approver, now, *, base_dir)` → `{ok, reason, record}`; `promotion_approvals.load_pending_proposals`, `approved_proposal_ids`, `rejected_proposal_ids`; helpers `_operator_edit_enabled()`, `_same_origin(request)`, `_require_auth_dep`, `audit_log.record_event`. `decision` ∈ `{"approve","reject"}`.
- Produces: the route; a `_apply_approval_action(...)` helper for testability.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gui_governance_approve.py
import importlib

import pytest


@pytest.fixture
def appmod(monkeypatch):
    monkeypatch.setenv("GUI_V2_AUTH_USER", "op")
    monkeypatch.setenv("GUI_V2_AUTH_PASS", "pw")
    monkeypatch.setenv("GUI_V2_OPERATOR_EDIT", "1")
    import gui_v2.app as app
    importlib.reload(app)
    return app


def test_apply_per_item_approve(appmod, monkeypatch):
    calls = []
    monkeypatch.setattr(appmod, "_promotion_approvals_record",
                        lambda **kw: calls.append(kw) or {"ok": True, "reason": "ok"})
    monkeypatch.setattr(appmod, "_pending_ids", lambda base_dir: {"p1", "p2"})
    monkeypatch.setattr(appmod, "_decided_ids", lambda base_dir: set())
    res = appmod._apply_approval_action(
        action="approve", proposal_id="p1", excluded_ids=set(),
        actor="op", now="n", base_dir="b")
    assert res["applied"] == ["p1"]
    assert calls[0]["decision"] == "approve"
    assert calls[0]["approver"] == "op"


def test_apply_bulk_approve_with_exclusion(appmod, monkeypatch):
    calls = []
    monkeypatch.setattr(appmod, "_promotion_approvals_record",
                        lambda **kw: calls.append(kw["proposal_id"]) or {"ok": True, "reason": "ok"})
    monkeypatch.setattr(appmod, "_pending_ids", lambda base_dir: {"p1", "p2", "p3"})
    monkeypatch.setattr(appmod, "_decided_ids", lambda base_dir: set())
    res = appmod._apply_approval_action(
        action="approve_all", proposal_id=None, excluded_ids={"p2"},
        actor="op", now="n", base_dir="b")
    assert set(res["applied"]) == {"p1", "p3"}
    assert "p2" not in calls


def test_apply_skips_already_decided(appmod, monkeypatch):
    monkeypatch.setattr(appmod, "_promotion_approvals_record",
                        lambda **kw: {"ok": True, "reason": "ok"})
    monkeypatch.setattr(appmod, "_pending_ids", lambda base_dir: {"p1"})
    monkeypatch.setattr(appmod, "_decided_ids", lambda base_dir: {"p1"})
    res = appmod._apply_approval_action(
        action="approve", proposal_id="p1", excluded_ids=set(),
        actor="op", now="n", base_dir="b")
    assert res["applied"] == []
    assert res["skipped"] == ["p1"]
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/python -m pytest -q tests/test_gui_governance_approve.py`
Expected: FAIL — `AttributeError: module 'gui_v2.app' has no attribute '_apply_approval_action'`.

- [ ] **Step 3: Implement the helper + route**

Add these thin indirection helpers near the other module-level helpers in `gui_v2/app.py` (they exist so tests can monkeypatch without hitting disk):

```python
def _promotion_approvals_record(**kw):
    from portfolio_automation.sim_governance import promotion_approvals
    return promotion_approvals.record_approval(
        kw["proposal_id"], kw["decision"], kw["approver"], kw["now"],
        base_dir=kw["base_dir"])


def _pending_ids(base_dir):
    from portfolio_automation.sim_governance import promotion_proposals
    return {p.get("proposal_id") for p in promotion_proposals.load_pending_proposals(base_dir)
            if p.get("approval_status") == "pending"}


def _decided_ids(base_dir):
    from portfolio_automation.sim_governance import promotion_approvals
    return (promotion_approvals.approved_proposal_ids(base_dir)
            | promotion_approvals.rejected_proposal_ids(base_dir))


def _apply_approval_action(*, action, proposal_id, excluded_ids, actor, now, base_dir):
    """Apply a per-item or bulk approve/reject. Each item goes through the
    human-gated promotion_approvals.record_approval. Returns a summary dict."""
    decision = "approve" if action in ("approve", "approve_all") else "reject"
    if action in ("approve", "reject"):
        targets = {proposal_id} if proposal_id else set()
    else:  # approve_all / reject_all
        targets = _pending_ids(base_dir) - (excluded_ids or set())
    decided = _decided_ids(base_dir)
    applied, skipped, failed = [], [], []
    for pid in sorted(t for t in targets if t):
        if pid in decided:
            skipped.append(pid)
            continue
        r = _promotion_approvals_record(proposal_id=pid, decision=decision,
                                        approver=actor, now=now, base_dir=base_dir)
        (applied if r.get("ok") else failed).append(pid)
    return {"decision": decision, "applied": applied, "skipped": skipped, "failed": failed}
```

Add the route immediately after `page_governance_veto`:

```python
@app.post("/dashboard/governance/approve")
async def page_governance_approve(
    request: Request, _a: str | None = Depends(_require_auth_dep)
):
    """
    POST /dashboard/governance/approve — human approve/reject of pending PRODUCTION
    proposals, per-item or in bulk. Every item flows through the existing
    human-gated promotion_approvals.record_approval; this route adds NO new
    production-mutation path. Strict operator pattern: actor from auth (never the
    form), operator-edit gate, same-origin CSRF guard, audited on every branch.
    """
    import datetime
    from operator_control import audit_log

    actor: str = _a if _a else "dashboard-manual"
    actor_source: str = "dashboard_auth" if _a else "dashboard_open_mode"

    form = await request.form()
    action = str(form.get("action", "")).strip()
    proposal_id = (str(form.get("proposal_id", "")).strip() or None)
    excluded_ids = {x.strip() for x in form.getlist("excluded_ids") if x.strip()}

    def _redirect(msg: str, level: str) -> RedirectResponse:
        return RedirectResponse(
            url=f"/dashboard/governance?msg={quote(msg)}&level={level}", status_code=303)

    if action not in ("approve", "reject", "approve_all", "reject_all"):
        raise HTTPException(status_code=400, detail="invalid action")

    if not _operator_edit_enabled():
        audit_log.record_event(
            REPO_ROOT, event_type="governance_approve_rejected", actor=actor,
            details={"why": "edit_disabled", "action": action, "actor_source": actor_source})
        return _redirect("Approval disabled (set GUI_V2_OPERATOR_EDIT=1).", "error")

    if not _same_origin(request):
        audit_log.record_event(
            REPO_ROOT, event_type="governance_approve_rejected", actor=actor,
            details={"why": "cross-origin rejected", "action": action,
                     "actor_source": actor_source},
            safety_result="rejected: cross-origin")
        return _redirect("Rejected: cross-origin request.", "error")

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    base_dir = str(REPO_ROOT / "outputs")
    try:
        res = _apply_approval_action(
            action=action, proposal_id=proposal_id, excluded_ids=excluded_ids,
            actor=actor, now=now, base_dir=base_dir)
    except Exception as exc:
        audit_log.record_event(
            REPO_ROOT, event_type="governance_approve_error", actor=actor,
            details={"action": action, "error": str(exc)})
        return _redirect("Approval failed (see logs).", "error")

    audit_log.record_event(
        REPO_ROOT, event_type="governance_approve", actor=actor,
        details={"action": action, "decision": res["decision"],
                 "applied": res["applied"], "skipped": res["skipped"],
                 "failed": res["failed"], "actor_source": actor_source})
    level = "success" if not res["failed"] else "error"
    return _redirect(
        f"{res['decision']}: {len(res['applied'])} applied, "
        f"{len(res['skipped'])} skipped, {len(res['failed'])} failed.", level)
```

- [ ] **Step 4: Run to verify pass**

Run: `./.venv/bin/python -m pytest -q tests/test_gui_governance_approve.py`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add gui_v2/app.py tests/test_gui_governance_approve.py
git commit -m "feat(gui): per-item + bulk production approval route via human-gated record_approval

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Governance page "Approval Packet" panel

**Files:**
- Modify: `gui_v2/templates/dashboard/governance.html`
- Test: `tests/test_gui_governance_approve.py` (add a render-smoke test)

**Interfaces:**
- Consumes: the `approval_packet` context added in Task 4 (`available`, `tier_sim`, `tier_production`, `counts`).
- Produces: a panel with per-item approve/reject forms + a bulk approve-all/reject-all form with per-item exclusion checkboxes. All forms POST to `/dashboard/governance/approve`; sim veto uses the existing `/dashboard/governance/veto` form.

- [ ] **Step 1: Write the failing render-smoke test (append to `tests/test_gui_governance_approve.py`)**

```python
def test_governance_page_renders_packet_panel(appmod, monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    # Point the reader at a seeded packet.
    import gui_v2.data.dash_approval_packet as dap
    monkeypatch.setattr(dap, "load_packet_context", lambda outputs_dir: {
        "available": True, "tier_sim": [], "counts": {"tier_production_pending": 1},
        "tier_production": [{"proposal_id": "p1", "workflow": "watchlist",
                             "symbol": "CVX", "status": "pending human review"}],
        "approval_page_url": "/dashboard/governance"})
    client = TestClient(appmod.app)
    r = client.get("/dashboard/governance", auth=("op", "pw"))
    assert r.status_code == 200
    assert "Approval Packet" in r.text
    assert "/dashboard/governance/approve" in r.text
    assert "p1" in r.text
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/python -m pytest -q tests/test_gui_governance_approve.py::test_governance_page_renders_packet_panel`
Expected: FAIL — assertion error (`"Approval Packet" not in r.text`).

- [ ] **Step 3: Add the panel to `governance.html`**

Insert this block where the page lists governance content (near the existing auto-applied / veto cards; match the template's existing card markup + CSS classes). Use the template engine's existing conditional/loop syntax (Jinja2):

```html
{% if approval_packet and approval_packet.available %}
<section class="card" id="approval-packet">
  <h2>Approval Packet</h2>
  <p class="muted">One consolidated packet: veto simulation items and/or approve production candidates.</p>

  <h3>Production candidates pending approval
      ({{ approval_packet.counts.tier_production_pending or 0 }})</h3>
  {% if approval_packet.tier_production %}
  <form method="post" action="/dashboard/governance/approve"
        onsubmit="return confirm('Approve/reject the selected production candidates?');">
    <table>
      <thead><tr><th>Exclude</th><th>Proposal</th><th>Workflow</th><th>Symbol</th>
                 <th>Status</th><th>Per-item</th></tr></thead>
      <tbody>
      {% for p in approval_packet.tier_production %}
        <tr>
          <td><input type="checkbox" name="excluded_ids" value="{{ p.proposal_id }}"></td>
          <td>{{ p.proposal_id }}</td>
          <td>{{ p.workflow }}</td>
          <td>{{ p.symbol }}</td>
          <td>{{ p.status }}</td>
          <td>
            <button name="action" value="approve"
                    formaction="/dashboard/governance/approve"
                    onclick="this.form.proposal_id.value='{{ p.proposal_id }}';">Approve</button>
            <button name="action" value="reject"
                    onclick="this.form.proposal_id.value='{{ p.proposal_id }}';">Reject</button>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    <input type="hidden" name="proposal_id" value="">
    <div class="bulk-actions">
      <button name="action" value="approve_all">Approve all (except excluded)</button>
      <button name="action" value="reject_all">Reject all (except excluded)</button>
    </div>
  </form>
  {% else %}
  <p class="muted">No production candidates pending.</p>
  {% endif %}
</section>
{% endif %}
```

Note: the existing simulation-veto cards already POST to `/dashboard/governance/veto`; leave them as-is. If the template variable name for the context differs from `approval_packet`, use whatever name Task 4 passed into `_render(...)`.

- [ ] **Step 4: Run to verify pass**

Run: `./.venv/bin/python -m pytest -q tests/test_gui_governance_approve.py`
Expected: PASS (4 passed).

- [ ] **Step 5: Rebuild CSS if the panel uses new classes (only if needed)**

If you introduced new CSS classes, run: `bash scripts/build_dashboard_css.sh` (per repo convention). If you reused existing classes, skip.

- [ ] **Step 6: Commit**

```bash
git add gui_v2/templates/dashboard/governance.html tests/test_gui_governance_approve.py
git commit -m "feat(gui): approval-packet panel with per-item + bulk production controls

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Config block, health coverage, and docs

**Files:**
- Modify: `config.json` (add `sim_governance.approval_packet` block)
- Modify: `portfolio_automation/sim_governance/approval_packet.py` (add `assess_packet_health`)
- Modify: `.claude/commands/daily-tool-analysis.md` (artifacts-read + dispatch + body-grammar + content_liveness)
- Modify: `.claude/agents/portfolio-learning-loop-health.md` (mirror the packet check as a new Layer)
- Modify: `docs/SIM_GOVERNANCE.md` (document the packet + activation runbook)
- Test: `tests/test_approval_packet.py` (append health tests)

**Interfaces:**
- Consumes: the packet artifact + `promotion_approvals.approved_proposal_ids` / `rejected_proposal_ids`.
- Produces: `assess_packet_health(base_dir: str, now: str, *, stale_pending_days: int = 3) -> dict` → `{"status": "GREEN"|"AMBER"|"RED", "reasons": list[str], "counts": dict}`.

- [ ] **Step 1: Write the failing health tests (append to `tests/test_approval_packet.py`)**

```python
def test_assess_health_green_when_empty(tmp_path, monkeypatch):
    base = str(tmp_path / "outputs")
    monkeypatch.setattr(ap.auto_approval, "build_summary",
                        lambda *, base_dir, now: {"active_items": []})
    ap.write_operator_packet(
        ap.build_operator_packet(base, "2026-07-15T00:00:00+00:00"), base_dir=base)
    h = ap.assess_packet_health(base, "2026-07-15T00:00:00+00:00")
    assert h["status"] == "GREEN"


def test_assess_health_amber_on_stale_pending(tmp_path, monkeypatch):
    base = str(tmp_path / "outputs")
    monkeypatch.setattr(ap.auto_approval, "build_summary",
                        lambda *, base_dir, now: {"active_items": []})
    _seed_pending(base, [
        {"proposal_id": "p1", "approval_status": "pending",
         "created_at": "2026-07-01T00:00:00+00:00",
         "proposed_production_change": {"symbol": "CVX"}, "workflow": "watchlist"},
    ])
    ap.write_operator_packet(
        ap.build_operator_packet(base, "2026-07-15T00:00:00+00:00"), base_dir=base)
    h = ap.assess_packet_health(base, "2026-07-15T00:00:00+00:00", stale_pending_days=3)
    assert h["status"] == "AMBER"
    assert any("stale_pending" in r for r in h["reasons"])
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/python -m pytest -q tests/test_approval_packet.py -k assess_health`
Expected: FAIL — `AttributeError: module ... has no attribute 'assess_packet_health'`.

- [ ] **Step 3: Implement `assess_packet_health` in `approval_packet.py`**

```python
def assess_packet_health(base_dir: str, now: str, *, stale_pending_days: int = 3) -> dict:
    """GREEN/AMBER/RED for the daily health tier. Never raises.

    AMBER: a tier-b candidate has been pending longer than stale_pending_days
           (operator decision-queue aging), or content-liveness looks off.
    RED:   the packet marks an item decided but no valid approval record exists.
    """
    import datetime as _dt
    import json as _json
    from pathlib import Path as _Path

    reasons: list[str] = []
    status = "GREEN"
    path = _Path(base_dir) / "promotion_review" / "operator_approval_packet.json"
    try:
        packet = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "AMBER", "reasons": ["packet_missing_or_unreadable"], "counts": {}}

    counts = packet.get("counts", {})
    now_dt = None
    try:
        now_dt = _dt.datetime.fromisoformat(now)
    except Exception:
        pass

    for p in packet.get("tier_production", []):
        created = p.get("created_at")
        if not (now_dt and created):
            continue
        try:
            age_days = (now_dt - _dt.datetime.fromisoformat(created)).days
        except Exception:
            continue
        if age_days > stale_pending_days:
            status = "AMBER"
            reasons.append(f"stale_pending:{p.get('proposal_id')}:{age_days}d")

    # RED integrity: packet claims a decided item with no valid approval record.
    try:
        from portfolio_automation.sim_governance import promotion_approvals as _pa
        decided = _pa.approved_proposal_ids(base_dir) | _pa.rejected_proposal_ids(base_dir)
        for p in packet.get("tier_production", []):
            st = (p.get("status") or "").lower()
            if ("approved" in st or "rejected" in st) and p.get("proposal_id") not in decided:
                status = "RED"
                reasons.append(f"packet_gate_drift:{p.get('proposal_id')}")
    except Exception:
        pass

    return {"status": status, "reasons": reasons, "counts": counts}
```

- [ ] **Step 4: Run to verify pass**

Run: `./.venv/bin/python -m pytest -q tests/test_approval_packet.py`
Expected: PASS (all Task 1 tests + 2 health tests).

- [ ] **Step 5: Add the config block to `config.json`**

Inside the existing `"sim_governance"` object, add a sibling key next to `"auto_approval"`:

```json
    "approval_packet": {
      "enabled": false,
      "deep_link_base": "https://dashboard.portfolio-ops-center.com",
      "stale_pending_days": 3,
      "note": "One-shot operator approval packet. Read-only builder + GUI approve route reusing the human-gated promotion_approvals path. Ships GATED. Disabled => no Step 8, approve route responds disabled, email unchanged. No new production-mutation path."
    }
```

Validate JSON: `./.venv/bin/python -c "import json; json.load(open('config.json'))"` → no error.

- [ ] **Step 6: Extend the health skill + agent + docs**

In `.claude/commands/daily-tool-analysis.md`:
- Step 1 artifacts-read: add `outputs/promotion_review/operator_approval_packet.json`.
- Step 3 dispatch: dispatch `portfolio-learning-loop-health` when `assess_packet_health` returns AMBER/RED.
- Step 4 body grammar: add a one-liner reporting `tier_production_pending` count + any `stale_pending` / `packet_gate_drift` reasons.
- content_liveness: flag packet file fresh-but-both-tiers-empty when upstream `pending_proposals.json` is non-empty.

In `.claude/agents/portfolio-learning-loop-health.md`: add a new Layer "Operator approval queue" that reads the packet + `assess_packet_health` semantics and VERIFIES decisions against `promotion_approvals` records (never reverts legitimate approvals/vetoes).

In `docs/SIM_GOVERNANCE.md`: add an "Operator Approval Packet" section describing the two tiers, the email-notifies/act-in-GUI flow, the reuse of `record_approval`, the gated config, and the activation runbook (flip `approval_packet.enabled=true`; set `deep_link_base`; the email link additionally needs `evening_digest.enabled` + `GOVERNANCE_DIGEST_ENABLED=1`).

- [ ] **Step 7: Commit**

```bash
git add config.json portfolio_automation/sim_governance/approval_packet.py \
        tests/test_approval_packet.py .claude/commands/daily-tool-analysis.md \
        .claude/agents/portfolio-learning-loop-health.md docs/SIM_GOVERNANCE.md
git commit -m "feat(sim-gov): approval-packet config, health assessor, and oversight coverage

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Full targeted suite + PR

**Files:** none (verification + PR)

- [ ] **Step 1: Run the full targeted suite for this feature + the merged auto-approval suite (regression)**

Run:
```bash
./.venv/bin/python -m pytest -q \
  tests/test_approval_packet.py \
  tests/test_approval_packet_pipeline.py \
  tests/test_dash_approval_packet.py \
  tests/test_gui_governance_approve.py \
  tests/test_governance_digest.py \
  tests/test_gui_governance_veto.py \
  tests/test_auto_approval.py \
  tests/test_auto_approval_stage.py
```
Expected: all PASS.

- [ ] **Step 2: Compile touched Python files**

Run:
```bash
./.venv/bin/python -m py_compile \
  portfolio_automation/sim_governance/approval_packet.py \
  portfolio_automation/sim_governance/daily_governance_run.py \
  portfolio_automation/sim_governance/governance_digest.py \
  gui_v2/app.py gui_v2/data/dash_approval_packet.py
```
Expected: no output (success).

- [ ] **Step 3: Confirm the protected registry weight is untouched**

Run: `grep -m1 "0.4947" config/signal_registry.yaml && echo "registry intact"`
Expected: prints the line + "registry intact". (If the full suite was ever run, restore `default_weight` per the isolation note before committing.)

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin feat/operator-approval-packet
gh pr create --title "feat(sim-gov): one-shot operator approval packet (INERT)" \
  --body "$(cat <<'EOF'
Phase B of the operator approval work (Phase A = PR #5, merged 77b6a89e).

Consolidates BOTH governance tiers into one per-cycle packet: (a) auto-applied
simulation items (vetoable) and (b) pending production candidates (approvable
per-item or in bulk). Email notifies with a deep link; all mutating actions
happen in the authenticated GUI.

## No new production-mutation path
Tier-b approval flows exclusively through the existing human-gated
promotion_approvals.record_approval (is_human_approver enforced). The builder is
read-only (observe_only=true). Ships GATED (sim_governance.approval_packet.enabled=false).

## Tests
Builder, pipeline (gated + non-blocking), digest deep link, GUI reader, per-item
+ bulk approve route (session actor, CSRF, operator-gate, idempotency, audit),
render smoke, health assessor (GREEN/AMBER/RED). All pass.

Spec: docs/superpowers/specs/2026-07-15-operator-approval-packet-design.md
Plan: docs/superpowers/plans/2026-07-15-operator-approval-packet.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Do NOT merge.** Report the PR URL and await operator review (production stays human-gated; activation is a later human step).

---

## Self-Review

**Spec coverage:**
- §1/§3 two-tier packet + single source of truth → Task 1 (builder) + Task 4 (reader).
- §2 invariants (read-only builder, no new prod path, session actor, gated) → Task 1 (`observe_only`), Task 5 (`record_approval` reuse + session actor), Task 2 + Task 7 (gating).
- §4 components → Tasks 1–7 cover every listed file.
- §5 packet schema → Task 1 build output + tests.
- §6 GUI action semantics (per-item + bulk, veto reuse, audit every branch, idempotent, 303) → Task 5 route + Task 6 panel.
- §7 health coverage (daily cadence, process+dev lenses, AMBER stale-pending, RED integrity, learning-loop mirror) → Task 7 (`assess_packet_health` + skill/agent/docs).
- §8 config → Task 7 Step 5.
- §9 test plan → Tasks 1–7 tests + Task 8 suite.
- §10 delivery (gated, PR not merged) → Task 8.

**Placeholder scan:** No TBD/TODO; every code step shows full code; commands have expected output. The only "match the existing name/markup" notes (digest `parts`/`html` accumulator, governance GET context var, template CSS classes) are grounded pointers to existing code the implementer will see, not deferred design.

**Type consistency:** `build_operator_packet` / `write_operator_packet` / `assess_packet_health` signatures match across Tasks 1, 2, 7. `load_packet_context` matches across Tasks 4 and 6. `_apply_approval_action` / `_promotion_approvals_record` / `_pending_ids` / `_decided_ids` match across Task 5 tests and implementation. `record_approval(proposal_id, decision, approver, now, *, base_dir)` and decision values `"approve"`/`"reject"` match the verified source.
