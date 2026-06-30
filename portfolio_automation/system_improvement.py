"""Daily system-improvement producer (Phase 3, spec §14).

Answers "what should we improve in the Portfolio Automation System today?" — an
operational/product/engineering question, **never** a market opportunity or a
buy/sell/hold recommendation.

Design (resolved decision §23.8): a **deterministic** producer generates and
ranks ideas from existing operational telemetry (works offline, no cost, fully
testable). An **optional** OpenAI summary may rewrite the human brief when a
provider is injected and budget allows — the deterministic ideas are unaffected.

Outputs (all ``observe_only: true``):
* ``outputs/latest/system_improvement_ideas.json``      (ranked ideas)
* ``outputs/latest/system_improvement_brief.md``        (operator brief)
* ``outputs/latest/system_improvement_scorecard.json``  (counts)
* ``outputs/policy/system_improvement_history.jsonl``   (append-only; dedup source)

Safety: emits no market action verbs (sanitizer-asserted); writes only to the
LATEST + POLICY namespaces it owns; degrades to an empty (but valid) artifact on
any failure; never mutates code, config, holdings, or the decision plan.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import yaml  # PyYAML is a project dependency (used by artifact_registry)
except Exception:  # pragma: no cover - defensive
    yaml = None  # type: ignore[assignment]

from portfolio_automation.data_governance import OutputNamespace, safe_write_json, safe_write_text
from portfolio_automation.next_stage.contracts import (
    SystemImprovementIdea, SystemImprovementCategory as Cat,
    SystemImprovementStatus as St, observe_only_envelope,
)

_OBSERVE_ONLY = True
_MAX_IDEAS = 12
_COOLDOWN_DAYS = 14

# Market action verbs that must NEVER appear in a system-improvement idea.
_MARKET_VERBS = re.compile(r"\b(buy|sell|hold|trade|long|short|allocate to)\b", re.IGNORECASE)

# Constraints every implementation prompt inherits (spec §16).
_SAFETY_CONSTRAINTS = [
    "advisory-only; no auto-trading, order placement, broker writes, or money movement",
    "no automatic portfolio allocation changes",
    "do not change protected scoring/decision logic without explicit approval",
    "no unrelated refactors",
    "observe-only: new artifacts carry observe_only=true",
]
_BLOCKED_ACTIONS = ["place_trade", "submit_order", "broker_write_action",
                    "auto_rebalance", "modify_real_holdings", "money_movement"]


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------


def _load_json_safe(path: Path) -> Any:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_history(root: Path) -> list[dict[str, Any]]:
    path = root / "outputs" / "policy" / "system_improvement_history.jsonl"
    out: list[dict[str, Any]] = []
    try:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue  # tolerate tampered lines
    except Exception:
        pass
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def idea_key(category: str, title: str) -> str:
    """Stable dedup key for an idea (category + title slug)."""
    return f"{category}:{_slug(title)}"


# ---------------------------------------------------------------------------
# Deterministic detectors — each returns 0+ (title, category, summary, evidence,
# proposed_change, affected, impact, urgency, effort) tuples as dicts.
# ---------------------------------------------------------------------------


def _mk(title, category, summary, evidence, proposed_change, affected_modules=None,
        affected_artifacts=None, impact=0.5, urgency=0.5, effort=0.4,
        acceptance=None, tests=None) -> dict[str, Any]:
    return {
        "title": title, "category": category, "summary": summary,
        "evidence": list(evidence), "proposed_change": proposed_change,
        "affected_modules": list(affected_modules or []),
        "affected_artifacts": list(affected_artifacts or []),
        "impact": impact, "urgency": urgency, "effort": effort,
        "acceptance_criteria": list(acceptance or []),
        "suggested_tests": list(tests or []),
    }


def detect_from_artifact_registry(root: Path) -> list[dict[str, Any]]:
    st = _load_json_safe(root / "outputs" / "latest" / "artifact_registry_status.json")
    if not isinstance(st, dict):
        return []
    out = []
    missing = [m for m in (st.get("missing") or []) if m]
    invalid = st.get("invalid_json") or []
    if invalid:
        out.append(_mk(
            f"Fix {len(invalid)} invalid-JSON artifact(s)", Cat.ARTIFACT_CONTRACT.value,
            f"artifact_registry_status reports invalid JSON: {', '.join(invalid[:5])}.",
            ["artifact_registry_status.json: invalid_json=" + json.dumps(invalid[:5])],
            "Inspect each producer; ensure atomic JSON writes; add a contract test.",
            affected_artifacts=invalid[:5], impact=0.7, urgency=0.7, effort=0.4,
            acceptance=["artifact_registry_status.invalid_json is empty"],
            tests=["tests/test_artifact_registry.py"]))
    # missing_required is the escalating subset
    missing_required = st.get("counts", {}).get("missing_required", 0) if isinstance(st.get("counts"), dict) else 0
    if missing_required:
        out.append(_mk(
            f"Restore {missing_required} missing required artifact(s)", Cat.RELIABILITY.value,
            "Required artifacts are missing from the latest run.",
            ["artifact_registry_status.json: missing_required=" + str(missing_required)],
            "Trace the producer chain for each missing required artifact.",
            impact=0.85, urgency=0.8, effort=0.5,
            acceptance=["no required artifacts missing"]))
    # unjustified registry debt
    debt = st.get("unjustified_debt")
    if isinstance(debt, int) and debt > 0:
        out.append(_mk(
            f"Classify {debt} unattributed registry artifact(s)", Cat.ARTIFACT_CONTRACT.value,
            "Registry rows lack a justified consumer_status.",
            ["artifact_registry_status.json: unjustified_debt=" + str(debt)],
            "Assign consumer_status (consumed/diagnostic_only/...) per artifact; "
            "proof-wire a consumer where appropriate.", impact=0.5, urgency=0.4, effort=0.5))
    return out


def _count(value: Any) -> int:
    """Coerce an int-or-list field into a count (real artifacts use both shapes)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return len(value)
    return 0


def detect_from_data_quality(root: Path) -> list[dict[str, Any]]:
    dq = _load_json_safe(root / "outputs" / "latest" / "data_quality_report.json")
    if not isinstance(dq, dict):
        return []
    out = []
    # Real schema: critical_symbols / warning_symbols are lists; also accept *_count.
    crit = _count(dq.get("critical_symbols", dq.get("critical_count", dq.get("critical"))))
    warn = _count(dq.get("warning_symbols", dq.get("warning_count", dq.get("warnings"))))
    if crit:
        out.append(_mk(
            f"Resolve {crit} critical data-quality issue(s)", Cat.DATA_QUALITY.value,
            "Data-quality monitor reports critical symbols/feeds.",
            ["data_quality_report.json: critical=" + str(crit)],
            "Investigate the failing symbols/feeds; add degraded-mode handling.",
            affected_artifacts=["data_quality_report.json"], impact=0.8, urgency=0.75, effort=0.5))
    elif warn:
        out.append(_mk(
            f"Reduce {warn} data-quality warning(s)", Cat.DATA_QUALITY.value,
            "Data-quality monitor reports warnings.",
            ["data_quality_report.json: warnings=" + str(warn)],
            "Triage warning sources; suppress benign repeats; document expected gaps.",
            impact=0.4, urgency=0.4, effort=0.4))
    return out


def detect_from_run_status(root: Path) -> list[dict[str, Any]]:
    drs = _load_json_safe(root / "outputs" / "latest" / "daily_run_status.json")
    if not isinstance(drs, dict):
        return []
    out = []
    ss = drs.get("stage_summary") or {}
    failed = ss.get("failed", 0) if isinstance(ss, dict) else 0
    warned = ss.get("warn", 0) if isinstance(ss, dict) else 0
    if failed:
        out.append(_mk(
            f"Stabilize {failed} failed pipeline stage(s)", Cat.RELIABILITY.value,
            "daily_run_status reports failed stages.",
            ["daily_run_status.json: stage_summary.failed=" + str(failed)],
            "Inspect failed stages; add retries/guards; ensure non-fatal isolation.",
            affected_modules=["main.py"], impact=0.9, urgency=0.85, effort=0.55,
            acceptance=["no failed stages on a clean run"]))
    # content liveness "looks-fresh-but-empty" warnings
    warns = drs.get("content_warn_count")
    if isinstance(warns, int) and warns > 0:
        out.append(_mk(
            f"Add/repair stale-content probe ({warns} liveness warning(s))",
            Cat.OBSERVABILITY.value,
            "content_liveness flags artifacts that look fresh but are empty.",
            ["daily_run_status.json: content_warn_count=" + str(warns)],
            "Add a content_liveness check for the affected producer(s).",
            impact=0.55, urgency=0.5, effort=0.4))
    # required artifacts missing (daily_run_status carries this directly)
    req_missing = drs.get("required_missing_count")
    if isinstance(req_missing, int) and req_missing > 0:
        out.append(_mk(
            f"Restore {req_missing} missing required artifact(s)", Cat.RELIABILITY.value,
            "daily_run_status reports required artifacts missing from the run.",
            ["daily_run_status.json: required_missing_count=" + str(req_missing)],
            "Trace the producer chain for each missing required artifact.",
            impact=0.85, urgency=0.8, effort=0.5,
            acceptance=["required_missing_count == 0"]))
    return out


def detect_from_ai_budget(root: Path) -> list[dict[str, Any]]:
    ab = _load_json_safe(root / "outputs" / "latest" / "ai_budget_summary.json")
    if not isinstance(ab, dict):
        return []
    # Real schema: blocked/warning flags + monthly_cost_total_usd vs limit.
    if ab.get("blocked"):
        return [_mk(
            "AI budget BLOCKED — review spend", Cat.COST_BUDGET.value,
            "ai_budget_summary reports blocked=true (monthly cap reached).",
            ["ai_budget_summary.json: blocked=true"],
            "Audit AI call sites; prefer keyword/deterministic fallbacks; consider cap.",
            impact=0.6, urgency=0.8, effort=0.3)]
    pct = None
    try:
        total = float(ab.get("monthly_cost_total_usd"))
        limit = float(ab.get("monthly_cost_limit_usd"))
        if limit > 0:
            pct = 100.0 * total / limit
    except (TypeError, ValueError):
        pct = None
    if (pct is not None and pct >= 80.0) or ab.get("warning"):
        label = f"{pct:.0f}% of cap" if pct is not None else "warning flag set"
        return [_mk(
            "Review AI budget burn (nearing cap)", Cat.COST_BUDGET.value,
            f"AI spend is high ({label}).",
            ["ai_budget_summary.json: monthly_cost_total_usd/limit, warning flag"],
            "Audit AI call sites; prefer keyword/deterministic fallbacks where adequate.",
            impact=0.5, urgency=0.55, effort=0.3)]
    return []


def detect_from_calibration(root: Path) -> list[dict[str, Any]]:
    cal = _load_json_safe(root / "outputs" / "latest" / "confidence_calibration.json")
    if not isinstance(cal, dict):
        return []
    if cal.get("insufficient_data") or cal.get("available") is False:
        return []  # not enough resolved decisions to judge calibration yet
    # Real schema: overall_calibration_gap = avg_confidence - hit_rate.
    flag = cal.get("overall_flag") or cal.get("calibration_flag")
    if not flag:
        gap = cal.get("overall_calibration_gap")
        try:
            gap = float(gap)
            flag = "overconfident" if gap > 0.15 else "underconfident" if gap < -0.15 else ""
        except (TypeError, ValueError):
            flag = ""
    if isinstance(flag, str) and flag.lower() in ("overconfident", "underconfident"):
        return [_mk(
            f"Improve confidence-calibration reporting ({flag})",
            Cat.CONFIDENCE_CALIBRATION.value,
            f"Calibration is {flag}; the dashboard could surface this more clearly.",
            ["confidence_calibration.json: overall_calibration_gap"],
            "Add a calibration trend card; annotate over/under-confident buckets.",
            impact=0.45, urgency=0.35, effort=0.4)]
    return []


def detect_roadmap_alignment(root: Path) -> list[dict[str, Any]]:
    p = root / ".agent" / "project_state.yaml"
    step = ""
    try:
        if p.exists() and yaml is not None:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            nos = data.get("next_official_step")
            if isinstance(nos, str):
                step = nos
            elif isinstance(nos, dict):
                # spec shape: {primary: <step>, secondary: [...], note: ...}
                step = str(nos.get("primary") or "").strip()
    except Exception:
        step = ""
    if not step:
        return []
    return [_mk(
        f"Advance the roadmap step: {step}", Cat.ROADMAP_ALIGNMENT.value,
        f"The authoritative next_official_step is '{step}'.",
        [".agent/project_state.yaml: next_official_step=" + step],
        f"Scope the smallest increment toward '{step}'.",
        impact=0.6, urgency=0.4, effort=0.6)]


def detect_from_sim_governance(root: Path) -> list[dict[str, Any]]:
    """Sim-governance lane health → improvement ideas (observe-only).

    Two conditions, both surfaced from the lane's own artifacts:

    1. The daily consolidated AI/product review runs the deterministic
       heuristic fallback rather than the LLM reviewer. The ``reviewer=`` seam
       threads cleanly from ``run_daily_governance`` to ``run_daily_ai_review``,
       but the production entrypoint (``run_daily_safe.sh`` Stage 10e) calls
       ``run_daily_governance('.')`` with no reviewer, so promotion verdicts
       carry no model judgment.
    2. Advisory ``crowd_context_change`` proposals accumulate as pending — a
       fast-refreshing daily signal (crowd_state flips day to day) routed
       through a slow, permanent human-approval gate.
    """
    out: list[dict[str, Any]] = []

    review = _load_json_safe(root / "outputs" / "promotion_review" / "daily_ai_review_result.json")
    if isinstance(review, dict) and review.get("status") == "reviewed":
        method = str(review.get("review_method") or "")
        reviewed = (int(review.get("advisory_candidates_reviewed") or 0)
                    + int(review.get("watchlist_candidates_reviewed") or 0))
        if method.startswith("heuristic_fallback") and reviewed > 0:
            est = review.get("estimated_cost_usd")
            cap = review.get("daily_cost_cap_usd")
            out.append(_mk(
                "Wire the LLM reviewer into the daily sim-governance review",
                Cat.OBSERVABILITY.value,
                "The daily consolidated promotion review runs the deterministic "
                "heuristic fallback, not the model — the reviewer= seam is never "
                "injected at the production entrypoint, so ready_for_production_review "
                "verdicts carry no LLM judgment.",
                [f"daily_ai_review_result.json: review_method={method}",
                 f"estimated_cost_usd={est} vs daily_cost_cap_usd={cap} (well under cap)",
                 "run_daily_safe.sh Stage 10e calls run_daily_governance('.') with no reviewer="],
                "Construct an OpenAI-backed reviewer Callable and pass it into "
                "run_daily_governance(reviewer=...); estimated cost is far under the daily cap.",
                affected_modules=["portfolio_automation/sim_governance/daily_governance_run.py",
                                  "portfolio_automation/sim_governance/daily_ai_review.py",
                                  "scripts/run_daily_safe.sh"],
                affected_artifacts=["daily_ai_review_result.json"],
                impact=0.6, urgency=0.45, effort=0.4,
                acceptance=["daily_ai_review_result.review_method == 'llm'"],
                tests=["tests/test_daily_ai_review.py"]))

    pending = _load_json_safe(root / "outputs" / "promotion_review" / "pending_proposals.json")
    if isinstance(pending, dict):
        props = pending.get("proposals") or []
        adv_ctx = [p for p in props if isinstance(p, dict)
                   and p.get("proposal_type") == "crowd_context_change"
                   and p.get("workflow") == "advisory"]
        if len(adv_ctx) >= 5:
            syms = sorted({str(p.get("proposed_production_change", {}).get("symbol"))
                           for p in adv_ctx
                           if isinstance(p.get("proposed_production_change"), dict)})
            out.append(_mk(
                "Auto-refresh advisory crowd-context instead of per-day approval",
                Cat.SANDBOX_QUALITY.value,
                f"{len(adv_ctx)} advisory crowd_context_change proposals are pending "
                "human approval. crowd_state is a fast-refreshing daily signal (it flips "
                "confirmed/divergent/insufficient day to day), so routing it through a "
                "permanent approval gate makes today's read stale tomorrow and "
                "accumulates a recurring backlog.",
                [f"pending_proposals.json: {len(adv_ctx)} crowd_context_change advisory pending",
                 "symbols: " + ", ".join(syms[:9]),
                 "evidence_refs cite outputs/sandbox/crowd_radar (absent); real signal is "
                 "outputs/latest/unified_crowd_intelligence.json"],
                "Have the advisory overlay read unified_crowd_intelligence crowd_state live "
                "each run (self-refreshing context annotation) rather than minting one "
                "pending proposal per symbol per day; repair the evidence_ref provenance.",
                affected_modules=["portfolio_automation/sim_governance/production_overlays.py",
                                  "portfolio_automation/sim_governance/promotion_proposals.py"],
                affected_artifacts=["pending_proposals.json", "unified_crowd_intelligence.json"],
                impact=0.55, urgency=0.5, effort=0.5,
                acceptance=["advisory crowd_context proposals no longer accumulate as pending",
                            "advisory overlay crowd_context matches the daily unified bus"],
                tests=["tests/test_sim_governance.py"]))
    return out


_DETECTORS: tuple[Callable[[Path], list[dict[str, Any]]], ...] = (
    detect_from_artifact_registry,
    detect_from_data_quality,
    detect_from_run_status,
    detect_from_ai_budget,
    detect_from_calibration,
    detect_roadmap_alignment,
    detect_from_sim_governance,
)


# ---------------------------------------------------------------------------
# Scoring + dedup/cooldown
# ---------------------------------------------------------------------------


def _final_rank(impact: float, urgency: float, effort: float,
                risk: float, confidence: float, roadmap: float) -> float:
    # Higher impact/urgency/confidence/roadmap lift rank; higher effort/risk lower it.
    raw = (0.30 * impact + 0.25 * urgency + 0.15 * confidence
           + 0.10 * roadmap - 0.12 * effort - 0.08 * risk)
    return round(max(0.0, min(1.0, raw + 0.3)), 4)  # shift into a friendly 0..1 band


def _priority(score: float) -> str:
    return "high" if score >= 0.65 else "medium" if score >= 0.45 else "low"


def _decision_suppressed_ids(root: Path, today: date) -> set[str]:
    """Item-ids suppressed by recorded operator decisions in
    ``system_improvement_decisions.jsonl``.

    Reuses ``approval_queue._suppressed`` — the SAME suppress/cooldown semantics the
    operator action queue already applies — so a single ``record_decision(...)`` call
    closes an idea at BOTH the action-queue layer and this producer/brief layer. Keyed
    on ``item_id``, which equals the idea ``id`` the build computes. Non-fatal: any
    failure (missing file, malformed JSON, legacy ``id``-only records) yields whatever
    the shared helper returns, defaulting to an empty set on hard error.
    """
    try:
        from portfolio_automation import approval_queue as _aq
        path = root / "outputs" / "policy" / "system_improvement_decisions.jsonl"
        return _aq._suppressed(_aq._read_jsonl(path), today)
    except Exception:
        return set()


def _cooldown_state(history: list[dict[str, Any]], today: date) -> dict[str, str]:
    """Map idea_key → suppression reason for keys still in cooldown."""
    suppressed: dict[str, str] = {}
    for rec in history:
        key = rec.get("idea_key") or idea_key(rec.get("category", ""), rec.get("title", ""))
        status = (rec.get("owner_decision") or rec.get("status") or "").lower()
        cu = rec.get("cooldown_until")
        if status in ("rejected", "deferred"):
            try:
                if cu and date.fromisoformat(cu[:10]) >= today:
                    suppressed[key] = status
            except Exception:
                pass
        elif status == "completed":
            # completed ideas don't repeat (no regression signal available here)
            suppressed[key] = "completed"
    return suppressed


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_system_improvement(root: Path, now: datetime | None = None) -> dict[str, Any]:
    """Pure build: returns the ideas-artifact payload dict. Never raises."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    today = now.date()
    cooldown_until = (today + timedelta(days=_COOLDOWN_DAYS)).isoformat()

    try:
        history = _read_history(root)
        suppressed = _cooldown_state(history, today)
        decision_suppressed = _decision_suppressed_ids(root, today)
        open_keys: dict[str, str] = {}  # key -> id of an already-open idea this run

        raw: list[dict[str, Any]] = []
        for det in _DETECTORS:
            try:
                raw.extend(det(root) or [])
            except Exception:
                continue  # one detector failing never sinks the rest

        ideas: list[SystemImprovementIdea] = []
        for r in raw:
            key = idea_key(r["category"], r["title"])
            iid = "si-" + _slug(key)
            # cooldown / completed → don't re-surface. Two unified sources:
            #  - legacy history owner_decision line (idea_key)
            #  - recorded operator decision (decisions.jsonl, keyed on item_id == iid)
            if key in suppressed or iid in decision_suppressed:
                continue
            # sanitize: never emit market verbs
            blob = " ".join([r["title"], r["summary"], r["proposed_change"]])
            if _MARKET_VERBS.search(blob):
                continue
            risk = 0.2  # ideas are advisory; implementation risk captured separately
            confidence = 0.6
            roadmap = 0.9 if r["category"] == Cat.ROADMAP_ALIGNMENT.value else 0.3
            rank = _final_rank(r["impact"], r["urgency"], r["effort"], risk, confidence, roadmap)
            duplicate_of = open_keys.get(key)
            idea = SystemImprovementIdea(
                id=iid, title=r["title"], category=r["category"], source="deterministic",
                created_at=now_iso, updated_at=now_iso,
                status=(St.DUPLICATE.value if duplicate_of else St.PROPOSED.value),
                priority=_priority(rank),
                impact_score=r["impact"], urgency_score=r["urgency"], effort_score=r["effort"],
                risk_score=risk, confidence_score=confidence, roadmap_alignment_score=roadmap,
                final_rank_score=rank, summary=r["summary"], evidence=r["evidence"],
                affected_modules=r["affected_modules"], affected_artifacts=r["affected_artifacts"],
                proposed_change=r["proposed_change"],
                acceptance_criteria=r["acceptance_criteria"], suggested_tests=r["suggested_tests"],
                safety_constraints=list(_SAFETY_CONSTRAINTS), blocked_actions=list(_BLOCKED_ACTIONS),
                duplicate_of=duplicate_of, cooldown_until=None,
            )
            if not duplicate_of:
                open_keys[key] = iid
            ideas.append(idea)

        ideas.sort(key=lambda i: i.final_rank_score, reverse=True)
        ideas = ideas[:_MAX_IDEAS]

        payload = observe_only_envelope(now_iso, source="system_improvement",
                                        cooldown_days=_COOLDOWN_DAYS,
                                        default_cooldown_until=cooldown_until)
        payload["ideas"] = [i.to_dict() for i in ideas]
        payload["idea_count"] = len(ideas)
        return payload
    except Exception as exc:  # never break the pipeline
        deg = observe_only_envelope(now_iso, source="system_improvement",
                                    degraded_mode=True, degraded_reason=str(exc))
        deg["ideas"] = []
        deg["idea_count"] = 0
        return deg


def render_brief_md(payload: dict[str, Any]) -> str:
    ideas = payload.get("ideas", [])
    lines = ["# System Improvement Brief",
             "", f"_Generated {payload.get('generated_at', '')} · observe-only · "
             "engineering/ops ideas, not market advice_", ""]
    if payload.get("degraded_mode"):
        lines += ["> Degraded: " + str(payload.get("degraded_reason", "")), ""]
    if not ideas:
        lines += ["No improvement ideas surfaced today — the system looks healthy."]
        return "\n".join(lines) + "\n"
    for i, idea in enumerate(ideas, 1):
        lines.append(f"## {i}. {idea['title']}  ·  {idea['priority'].upper()}  "
                     f"({idea['category']}, rank {idea['final_rank_score']})")
        lines.append(idea.get("summary", ""))
        if idea.get("proposed_change"):
            lines.append(f"- **Proposed:** {idea['proposed_change']}")
        if idea.get("evidence"):
            lines.append(f"- **Evidence:** {'; '.join(idea['evidence'][:3])}")
        if idea.get("duplicate_of"):
            lines.append(f"- _duplicate of {idea['duplicate_of']}_")
        lines.append("")
    return "\n".join(lines) + "\n"


def _scorecard(payload: dict[str, Any]) -> dict[str, Any]:
    ideas = payload.get("ideas", [])
    by_cat: dict[str, int] = {}
    by_pri: dict[str, int] = {}
    for i in ideas:
        by_cat[i["category"]] = by_cat.get(i["category"], 0) + 1
        by_pri[i["priority"]] = by_pri.get(i["priority"], 0) + 1
    sc = observe_only_envelope(payload.get("generated_at", ""), source="system_improvement")
    sc["counts"] = {"total": len(ideas), "by_category": by_cat, "by_priority": by_pri}
    return sc


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def write_system_improvement_artifacts(
    root: Path, now: datetime | None = None,
    summarizer: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Build + write all system-improvement artifacts. Non-fatal; returns paths.

    ``summarizer`` is an optional OpenAI hook (§23.8): if supplied it may rewrite
    the brief; failures fall back to the deterministic brief. Never required.
    """
    now = now or datetime.now(timezone.utc)
    base = root / "outputs"
    payload = build_system_improvement(root, now)
    brief = render_brief_md(payload)
    if summarizer is not None:
        try:
            polished = summarizer(brief)
            if isinstance(polished, str) and polished.strip():
                brief = polished
        except Exception:
            pass  # deterministic brief stands
    scorecard = _scorecard(payload)

    json_path = safe_write_json(OutputNamespace.LATEST, "system_improvement_ideas.json",
                                payload, base_dir=base)
    safe_write_text(OutputNamespace.LATEST, "system_improvement_brief.md", brief, base_dir=base)
    safe_write_json(OutputNamespace.LATEST, "system_improvement_scorecard.json",
                    scorecard, base_dir=base)
    # append-only history (one line per idea this run) — best-effort
    try:
        hist_path = base / "policy" / "system_improvement_history.jsonl"
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        with hist_path.open("a", encoding="utf-8") as f:
            for idea in payload.get("ideas", []):
                f.write(json.dumps({
                    "idea_key": idea_key(idea["category"], idea["title"]),
                    "id": idea["id"], "title": idea["title"], "category": idea["category"],
                    "status": idea["status"], "generated_at": payload.get("generated_at"),
                    "final_rank_score": idea["final_rank_score"], "observe_only": True,
                }, default=str) + "\n")
    except Exception:
        pass

    return {"ideas_path": str(json_path), "idea_count": payload.get("idea_count", 0),
            "degraded": bool(payload.get("degraded_mode"))}
