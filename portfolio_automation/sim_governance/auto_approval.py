"""
Bounded GPT auto-approval channel — SIMULATION ONLY.
(🔒 SANCTIONED MUTATING PATH | gated | reversible | audited | default-INERT)

The third sanctioned mutating path (alongside ``backtesting/auto_apply.py`` and the
human-gated ``sim_governance`` promotion workflow). It may automatically apply bounded
changes ONLY to authorized simulation / advisory state, accelerating simulation-lane
experimentation. It can NEVER authorize production promotion, feed the production
decision engine, or impersonate human approval — see ``docs/SIM_GOVERNANCE.md`` and the
CLAUDE.md sanctioned exception (operator-approved 2026-07-14).

Non-negotiable authority invariant (enforced here, in tests, health, and docs):

    target_lane           == "simulation"
    production_mutation   == False
    feeds_decision_engine == False
    is_human_approved     == False

The safety IS the gates, not a human. Fail-closed at every step; never raises from the
orchestrator. Ships INERT (config ``enabled=false``, all sub-flags false,
``strategy_daily_cap=0``) and hard-disabled by a kill-switch file or env var regardless
of config.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from portfolio_automation.sim_governance import schemas as S

# Event kinds recorded to the append-only ledger.
EVENT_ATTEMPT = "attempt"
EVENT_APPLIED = "applied"
EVENT_GPT_VETO = "gpt_veto"
EVENT_DETERMINISTIC_REJECT = "deterministic_reject"
EVENT_HUMAN_VETO = "human_veto"
EVENT_ROLLBACK = "rollback"
EVENT_ROLLBACK_CONFLICT = "rollback_conflict"
EVENT_FAILURE = "failure"

# Rollback RESULT statuses (distinct from ledger event kinds; used by health + digest).
ROLLBACK_OK = "rolled_back"
ROLLBACK_FAILED = "rollback_failed"

# GPT verdict values.
GPT_APPROVE = "approve_in_bounds"
GPT_VETO = "veto"
GPT_INVALID = "invalid_or_unavailable"

_DEFAULT_MODEL = "gpt-4o-mini"
PROMPT_VERSION = "auto_approval.v1"
POLICY_VERSION = "auto_approval_policy.v1"

# Kill-switch identifiers.
KILL_SWITCH_FILE = "config/auto_approval.DISABLED"
KILL_SWITCH_ENV = "STOCKBOT_AUTO_APPROVAL_DISABLED"

# Proposal types the SIMULATION watchlist auto-apply may act on.
_WATCHLIST_ELIGIBLE_TYPES = frozenset({
    S.PROPOSAL_WATCHLIST_ADD,
    S.PROPOSAL_DISCOVERY_PROMOTION,
})

# A conservative ticker shape: 1-10 chars, upper-alnum plus '.'/'-'.
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

# The approval-channel marker. schemas.is_human_approver already rejects the token
# "auto" (in AI_REVIEWER_MARKERS), so this marker can never impersonate a human
# approver — a structural guarantee, re-checked by a regression test.
AUTO_APPROVAL_CHANNEL = "auto_approval"

# Candidate types this channel understands.
CANDIDATE_WATCHLIST = "watchlist"
CANDIDATE_STRATEGY = "strategy"
SUPPORTED_CANDIDATE_TYPES = frozenset({CANDIDATE_WATCHLIST, CANDIDATE_STRATEGY})


# ---------------------------------------------------------------------------
# Structured gate trace
# ---------------------------------------------------------------------------


@dataclass
class GateResult:
    """One deterministic gate's outcome, with the evidence that decided it."""
    gate_name: str
    passed: bool
    reason: str
    observed_value: Any = None
    required_value: Any = None

    def to_dict(self) -> dict:
        return {
            "gate_name": self.gate_name,
            "passed": bool(self.passed),
            "reason": self.reason,
            "observed_value": self.observed_value,
            "required_value": self.required_value,
        }


def all_passed(results: list[GateResult]) -> bool:
    """True only when every gate in *results* passed (empty list is not a pass)."""
    return bool(results) and all(r.passed for r in results)


def _bool_gate(name: str, observed: Any, required: bool, ok_reason: str,
               fail_reason: str) -> GateResult:
    """A gate that requires ``observed is <required>`` — fail-closed on anything else.

    Note the identity check: a missing field (``None``) or a truthy-but-wrong value
    never satisfies the gate, so omitting an authority field fails closed.
    """
    passed = observed is required
    return GateResult(
        gate_name=name,
        passed=passed,
        reason=ok_reason if passed else fail_reason,
        observed_value=observed,
        required_value=required,
    )


# ---------------------------------------------------------------------------
# Authority hard-gates — the safety spine
# ---------------------------------------------------------------------------


def run_authority_gates(candidate: dict) -> list[GateResult]:
    """The four non-negotiable authority gates. Every one must pass or no mutation.

    Fail-closed: a candidate that omits an authority field (so the value is ``None``)
    fails the corresponding gate — auto-approval trusts nothing it did not prove.
    """
    c = candidate or {}
    return [
        GateResult(
            gate_name="target_lane_is_simulation",
            passed=(c.get("target_lane") == "simulation"),
            reason=("target_lane is simulation" if c.get("target_lane") == "simulation"
                    else f"target_lane must be 'simulation', got {c.get('target_lane')!r}"),
            observed_value=c.get("target_lane"),
            required_value="simulation",
        ),
        _bool_gate(
            "no_production_mutation", c.get("production_mutation"), False,
            "does not mutate production",
            "production_mutation must be False (candidate would mutate production)",
        ),
        _bool_gate(
            "does_not_feed_decision_engine", c.get("feeds_decision_engine"), False,
            "does not feed the decision engine",
            "feeds_decision_engine must be False (candidate would feed the decision engine)",
        ),
        _bool_gate(
            "not_human_approved", c.get("is_human_approved"), False,
            "not marked human-approved (auto-approval never impersonates a human)",
            "is_human_approved must be False (auto-approval must not claim human approval)",
        ),
    ]


# ---------------------------------------------------------------------------
# Enablement + kill-switch precedence (fail-closed)
# ---------------------------------------------------------------------------


def _env_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def auto_approval_disabled_reason(
    config: dict,
    *,
    component: str,
    env: dict,
    kill_file_exists: bool,
) -> str | None:
    """Return a machine reason string if auto-approval is disabled, else ``None``.

    Precedence (any one disables; checked in this order):
      1. environment global kill switch  -> "env_kill_switch"
      2. file kill switch                 -> "file_kill_switch"
      3. global config ``enabled``        -> "global_disabled"
      4. component config flag            -> "component_disabled"
      5. component environment kill switch-> "component_kill_switch"

    Fail-closed: a non-dict config, or an ``enabled``/component flag that is not a
    real bool, returns "invalid_config" (treated as disabled).
    """
    env = env or {}

    # 1. env global kill switch — wins over everything.
    if _env_truthy(env.get(KILL_SWITCH_ENV)):
        return "env_kill_switch"
    # 2. file kill switch.
    if kill_file_exists:
        return "file_kill_switch"

    # Config must be well-formed from here on (fail-closed).
    if not isinstance(config, dict):
        return "invalid_config"
    enabled = config.get("enabled", False)
    flag_key = f"{component}_enabled"
    component_flag = config.get(flag_key, False)
    if not isinstance(enabled, bool) or not isinstance(component_flag, bool):
        return "invalid_config"

    # 3. global config enabled.
    if not enabled:
        return "global_disabled"
    # 4. component config flag.
    if not component_flag:
        return "component_disabled"
    # 5. component environment kill switch.
    if _env_truthy(env.get(f"STOCKBOT_AUTO_APPROVAL_{component.upper()}_DISABLED")):
        return "component_kill_switch"
    return None


# ---------------------------------------------------------------------------
# Deterministic gates — watchlist (simulation lane)
# ---------------------------------------------------------------------------


def run_watchlist_gates(candidate: dict, config: dict, ctx: dict) -> list[GateResult]:
    """Bounded watchlist gates. ``ctx`` carries the observed live state:

    active_count, max_symbols, applied_today, active_awaiting_veto,
    prohibited (set), static (set), conflicting_symbols (set).
    """
    c = candidate or {}
    symbol = str(c.get("symbol") or "")
    conf = float(c.get("confidence") or 0.0)
    min_conf = float(config.get("min_confidence", 0.85))
    daily_cap = int(config.get("watchlist_daily_cap", 0))
    max_active_veto = int(config.get("max_active_awaiting_veto", 0))

    active = int(ctx.get("active_count", 0))
    max_symbols = int(ctx.get("max_symbols", 0))
    applied_today = int(ctx.get("applied_today", 0))
    awaiting = int(ctx.get("active_awaiting_veto", 0))
    prohibited = ctx.get("prohibited") or set()
    static = ctx.get("static") or set()
    conflicting = ctx.get("conflicting_symbols") or set()

    return [
        GateResult("symbol_format", bool(_SYMBOL_RE.match(symbol)),
                   "symbol is a well-formed ticker" if _SYMBOL_RE.match(symbol)
                   else "symbol is not a well-formed ticker",
                   observed_value=symbol, required_value="^[A-Z][A-Z0-9.-]{0,9}$"),
        GateResult("not_prohibited_or_static",
                   symbol not in prohibited and symbol not in static,
                   "symbol is neither prohibited nor static-only"
                   if (symbol not in prohibited and symbol not in static)
                   else "symbol is prohibited or already static-only",
                   observed_value=symbol),
        GateResult("capacity_below_max", active < max_symbols,
                   "capacity headroom available" if active < max_symbols
                   else "simulation watchlist at capacity",
                   observed_value=active, required_value=f"< {max_symbols}"),
        GateResult("watchlist_daily_cap", applied_today < daily_cap,
                   "under daily cap" if applied_today < daily_cap else "daily cap reached",
                   observed_value=applied_today, required_value=f"< {daily_cap}"),
        GateResult("min_confidence", conf >= min_conf,
                   "confidence meets threshold" if conf >= min_conf
                   else "confidence below threshold",
                   observed_value=round(conf, 4), required_value=f">= {min_conf}"),
        GateResult("source_proposal_eligible",
                   c.get("proposal_type") in _WATCHLIST_ELIGIBLE_TYPES,
                   "proposal type is eligible for simulation auto-application"
                   if c.get("proposal_type") in _WATCHLIST_ELIGIBLE_TYPES
                   else "proposal type is not eligible for simulation auto-application",
                   observed_value=c.get("proposal_type"),
                   required_value=sorted(_WATCHLIST_ELIGIBLE_TYPES)),
        GateResult("no_conflicting_active_proposal", symbol not in conflicting,
                   "no conflicting active proposal for this symbol"
                   if symbol not in conflicting else "a conflicting active proposal exists",
                   observed_value=symbol),
        _bool_gate("feeds_decision_engine_false", c.get("feeds_decision_engine"), False,
                   "does not feed the decision engine",
                   "feeds_decision_engine must be False"),
        GateResult("max_active_awaiting_veto", awaiting < max_active_veto,
                   "under max active awaiting-veto" if awaiting < max_active_veto
                   else "too many items awaiting veto",
                   observed_value=awaiting, required_value=f"< {max_active_veto}"),
    ]


# ---------------------------------------------------------------------------
# Deterministic gates — strategy (simulation lane; ships disabled, cap 0)
# ---------------------------------------------------------------------------


def run_strategy_gates(candidate: dict, config: dict, ctx: dict) -> list[GateResult]:
    """Bounded strategy gates. ``ctx`` carries: applied_today, active_awaiting_veto,
    active_strategy_count, valid_strategy_ids (set), prior_active_capturable (bool)."""
    c = candidate or {}
    sid = c.get("strategy_id")
    daily_cap = int(config.get("strategy_daily_cap", 0))
    max_active_veto = int(config.get("max_active_awaiting_veto", 0))
    applied_today = int(ctx.get("applied_today", 0))
    awaiting = int(ctx.get("active_awaiting_veto", 0))
    active_count = int(ctx.get("active_strategy_count", 0))
    valid_ids = ctx.get("valid_strategy_ids") or set()

    return [
        GateResult("sandbox_only_assertion", c.get("target_lane") == "simulation",
                   "target lane is the sandbox/simulation lane"
                   if c.get("target_lane") == "simulation"
                   else "strategy auto-apply is sandbox/simulation-only",
                   observed_value=c.get("target_lane"), required_value="simulation"),
        GateResult("strategy_daily_cap", applied_today < daily_cap,
                   "under strategy daily cap" if applied_today < daily_cap
                   else "strategy daily cap reached (0 = disabled by default)",
                   observed_value=applied_today, required_value=f"< {daily_cap}"),
        GateResult("one_active_strategy_invariant", active_count <= 1,
                   "at most one active simulation strategy" if active_count <= 1
                   else "more than one active simulation strategy — invariant breached",
                   observed_value=active_count, required_value="<= 1"),
        GateResult("candidate_strategy_valid", sid in valid_ids,
                   "candidate strategy exists and is valid" if sid in valid_ids
                   else "candidate strategy is unknown/invalid",
                   observed_value=sid),
        _bool_gate("prior_active_capturable", ctx.get("prior_active_capturable"), True,
                   "prior active strategy can be captured for rollback",
                   "prior active strategy could not be captured for rollback"),
        _bool_gate("no_production_strategy_mutation", c.get("production_mutation"), False,
                   "does not mutate production strategy/config",
                   "production_mutation must be False"),
        _bool_gate("feeds_decision_engine_false", c.get("feeds_decision_engine"), False,
                   "does not feed the decision engine",
                   "feeds_decision_engine must be False"),
        GateResult("max_active_awaiting_veto", awaiting < max_active_veto,
                   "under max active awaiting-veto" if awaiting < max_active_veto
                   else "too many items awaiting veto",
                   observed_value=awaiting, required_value=f"< {max_active_veto}"),
    ]


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------


def idempotency_key(*, source_verdict_id: str, candidate_type: str, target_id: str,
                    source_artifact_hash: str, policy_version: str) -> str:
    """Stable key over immutable candidate inputs. A changed source artifact hash
    (re-review) yields a new key, so a genuinely new decision is never suppressed."""
    raw = f"{source_verdict_id}|{candidate_type}|{target_id}|{source_artifact_hash}|{policy_version}"
    return "idk_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# GPT approver — approve-in-bounds / veto / invalid; fail-closed; never widens
# ---------------------------------------------------------------------------


def _build_approver_prompt(candidate: dict) -> str:
    return (
        "You are a risk gate for the SIMULATION lane of an advisory portfolio system. "
        "A simulation-lane change is proposed within already-fixed deterministic bounds "
        "(the caps were checked before you). You may ONLY approve the exact pre-bounded "
        "change or veto. You may NOT widen any bound, change the target, or promote to "
        "production. This never affects production or the decision engine. Veto if the "
        "evidence is thin or anything looks off.\n\n"
        f"candidate_type: {candidate.get('candidate_type')}\n"
        f"target: {candidate.get('symbol') or candidate.get('strategy_id')}\n"
        f"proposal_type: {candidate.get('proposal_type')}\n"
        f"confidence: {candidate.get('confidence')}\n"
        f"why: {candidate.get('why_changed') or candidate.get('reason')}\n\n"
        'Return ONLY a JSON object: {"decision":"approve"|"veto",'
        '"within_bounds":true|false,"reason":"<one line>"}'
    )


def _parse_approver_reply(raw: str) -> dict:
    """Parse the approver reply. Fail-closed: anything that is not an unambiguous
    in-bounds approval or an explicit veto becomes ``invalid_or_unavailable``."""
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        doc = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError, TypeError, AttributeError):
        return {"verdict": GPT_INVALID, "within_bounds": False, "reason": "unparseable_verdict"}
    decision = str(doc.get("decision", "")).strip().lower()
    within = doc.get("within_bounds") is True
    reason = str(doc.get("reason", ""))[:300]
    if decision == "approve" and within:
        return {"verdict": GPT_APPROVE, "within_bounds": True, "reason": reason}
    if decision in ("approve", "veto"):
        # An "approve" that is not within bounds is an attempt to widen -> veto.
        return {"verdict": GPT_VETO, "within_bounds": within,
                "reason": reason or ("out_of_bounds_approval" if decision == "approve"
                                     else "vetoed")}
    return {"verdict": GPT_INVALID, "within_bounds": within, "reason": reason or "unknown_decision"}


def gpt_approve_candidate(candidate: dict, *, provider: str | None = None,
                          model: str | None = None,
                          approver: Callable[[str], str] | None = None) -> dict:
    """Return a GPT verdict dict {verdict, within_bounds, reason, model}. Fail-closed:
    empty reply, exception, or malformed output all yield ``invalid_or_unavailable``.
    ``approver`` is injectable so tests never call a real LLM."""
    prompt = _build_approver_prompt(candidate or {})
    used_model = model or _DEFAULT_MODEL
    try:
        if approver is not None:
            raw = approver(prompt)
        else:
            from agent.llm_adapters import call_provider
            raw = call_provider(provider=provider or "openai", model=used_model,
                                prompt=prompt, max_tokens=200, timeout=60)
        if not raw or not str(raw).strip():
            return {"verdict": GPT_INVALID, "within_bounds": False,
                    "reason": "empty_reply", "model": used_model}
        v = _parse_approver_reply(str(raw))
        v["model"] = used_model
        return v
    except Exception as exc:  # fail-closed
        return {"verdict": GPT_INVALID, "within_bounds": False,
                "reason": f"approver_error:{exc}", "model": used_model}


def is_gpt_approval(verdict: dict) -> bool:
    """True only for an unambiguous in-bounds approval."""
    return bool(verdict) and verdict.get("verdict") == GPT_APPROVE


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def circuit_breaker_reason(state: dict) -> str | None:
    cb = (state or {}).get("circuit_breaker") or {}
    return cb.get("reason") if cb.get("engaged") else None


def engage_circuit_breaker(state: dict, reason: str, *, now: str) -> dict:
    state = dict(state or {})
    state["circuit_breaker"] = {"engaged": True, "reason": reason, "since": now}
    return state


# ---------------------------------------------------------------------------
# Audit event builder
# ---------------------------------------------------------------------------


def _event_id(kind: str, key: str, stamp: str) -> str:
    raw = f"{kind}|{key}|{stamp}"
    return "evt_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def make_applied_event(*, now: str, idempotency_key: str, candidate: dict,
                       gpt_verdict: dict, gate_trace: list, before_state: Any,
                       after_state: Any, source_verdict_id: str,
                       source_artifact_path: str, source_artifact_hash: str,
                       source_verdict_timestamp: str, model_id: str,
                       prompt_version: str = PROMPT_VERSION,
                       policy_version: str = POLICY_VERSION,
                       config_version: str = "") -> dict:
    """Build an ``applied`` ledger event carrying every required field and, critically,
    the authority-channel invariants (never human, never production, never DE-feeding)."""
    c = candidate or {}
    target_id = c.get("symbol") or c.get("strategy_id")
    return {
        "kind": EVENT_APPLIED,
        "event_id": _event_id(EVENT_APPLIED, idempotency_key, now),
        "decision_id": _event_id("decision", idempotency_key, now),
        "idempotency_key": idempotency_key,
        "candidate_type": c.get("candidate_type"),
        "target_id": target_id,
        "symbol": c.get("symbol"),
        "strategy_id": c.get("strategy_id"),
        "source_verdict_id": source_verdict_id,
        "source_artifact_path": source_artifact_path,
        "source_artifact_hash": source_artifact_hash,
        "source_verdict_timestamp": source_verdict_timestamp,
        "confidence": c.get("confidence"),
        "gate_trace": [g.to_dict() if isinstance(g, GateResult) else g for g in (gate_trace or [])],
        "gpt_verdict": (gpt_verdict or {}).get("verdict"),
        "gpt_reasoning": (gpt_verdict or {}).get("reason"),
        "model_id": model_id,
        "prompt_version": prompt_version,
        "policy_version": policy_version,
        "config_version": config_version,
        "before_state": before_state,
        "after_state": after_state,
        "application_timestamp": now,
        "ts": now,
        # Authority-channel invariants — structurally non-human, non-production.
        "approval_channel": AUTO_APPROVAL_CHANNEL,
        "is_human_approved": False,
        "target_lane": "simulation",
        "production_mutation": False,
        "feeds_decision_engine": False,
        "application_status": "applied",
    }


# ---------------------------------------------------------------------------
# Append-only ledger + derived summary  (outputs/policy/)
# ---------------------------------------------------------------------------

_EVENTS_FILE = "auto_approval_events.jsonl"
_SUMMARY_FILE = "auto_approval_audit.json"

# Events that terminate an applied item's "active / awaiting-veto" lifetime.
_TERMINAL_KINDS = frozenset({EVENT_HUMAN_VETO, EVENT_ROLLBACK})


def _policy_path(filename: str, base_dir: str):
    from portfolio_automation.data_governance import OutputNamespace, get_output_path
    return get_output_path(OutputNamespace.POLICY, filename, base_dir=base_dir)


def append_event(event: dict, *, base_dir: str) -> None:
    """Append one event to the authoritative append-only ledger. Raises on I/O failure
    (the caller in ``record_and_apply`` relies on this to gate the mutation)."""
    from pathlib import Path

    from portfolio_automation.data_governance import OutputNamespace, ensure_output_dir
    ensure_output_dir(OutputNamespace.POLICY, _EVENTS_FILE, base_dir=base_dir)
    path = _policy_path(_EVENTS_FILE, base_dir)
    with Path(path).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, default=str) + "\n")


def load_events(*, base_dir: str) -> list[dict]:
    """Load all ledger events (malformed lines skipped, not fatal)."""
    from pathlib import Path
    path = _policy_path(_EVENTS_FILE, base_dir)
    out: list[dict] = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            except (json.JSONDecodeError, ValueError):
                continue
    except (OSError, FileNotFoundError):
        return []
    return out


def applied_key_exists(key: str, *, base_dir: str) -> bool:
    """True if a successful application event already exists for this idempotency key."""
    return any(e.get("kind") == EVENT_APPLIED and e.get("idempotency_key") == key
               for e in load_events(base_dir=base_dir))


def build_summary(*, base_dir: str, now: str, state: dict | None = None) -> dict:
    """Derive the current-state summary from the append-only ledger.

    ``active_items`` are applied items with no subsequent human-veto / successful
    rollback for the same idempotency key (rollback_conflict does NOT clear them).
    """
    events = load_events(base_dir=base_dir)
    counters: dict[str, int] = {}
    latest_applied: dict[str, dict] = {}
    terminated: set[str] = set()
    for e in events:
        kind = e.get("kind", "unknown")
        counters[kind] = counters.get(kind, 0) + 1
        key = e.get("idempotency_key")
        if kind == EVENT_APPLIED and key:
            latest_applied[key] = e
            terminated.discard(key)
        elif kind in _TERMINAL_KINDS and key:
            terminated.add(key)

    active_items = [
        {"idempotency_key": k, "event_id": e.get("event_id"),
         "target_id": e.get("target_id"), "candidate_type": e.get("candidate_type"),
         "symbol": e.get("symbol"), "strategy_id": e.get("strategy_id"),
         "applied_at": e.get("application_timestamp") or e.get("ts"),
         "confidence": e.get("confidence")}
        for k, e in latest_applied.items() if k not in terminated
    ]

    cb = (state or {}).get("circuit_breaker") or {"engaged": False, "reason": None}
    return {
        "generated_at": now,
        "schema": "auto_approval_audit.v1",
        "observe_only": False,
        "generated_by": "portfolio_automation.sim_governance.auto_approval",
        "counters": counters,
        "event_count": len(events),
        "active_items": active_items,
        "active_item_count": len(active_items),
        "circuit_breaker": {"engaged": bool(cb.get("engaged")), "reason": cb.get("reason")},
        "policy_version": POLICY_VERSION,
    }


def write_summary(summary: dict, *, base_dir: str) -> None:
    from portfolio_automation.data_governance import OutputNamespace, safe_write_json
    safe_write_json(OutputNamespace.POLICY, _SUMMARY_FILE, summary, base_dir=base_dir)


def record_and_apply(event: dict, mutate: Callable[[], Any], *, base_dir: str) -> dict:
    """Audit-before-mutate: write the durable event FIRST; only if that succeeds run the
    mutation. If the audit write fails, the mutation never runs — an applied mutation can
    never exist without a matching durable audit record."""
    try:
        append_event(event, base_dir=base_dir)
    except Exception as exc:
        return {"ok": False, "reason": f"audit_write_failed:{exc}", "result": None}
    try:
        result = mutate()
    except Exception as exc:
        # Mutation failed AFTER the audit record — record a failure event for oversight.
        try:
            append_event({**event, "kind": EVENT_FAILURE,
                          "reason": f"mutation_failed:{exc}"}, base_dir=base_dir)
        except Exception:
            pass
        return {"ok": False, "reason": f"mutation_failed:{exc}", "result": None}
    return {"ok": True, "reason": "ok", "result": result}


# ---------------------------------------------------------------------------
# Apply + event-aware compare-and-swap rollback — WATCHLIST (simulation)
# ---------------------------------------------------------------------------

# Fields that meaningfully identify a watchlist row's state for CAS comparison.
# Volatile counters (scan_count, mention_count, timestamps, MAX'd confidence) are
# intentionally excluded so normal bookkeeping never manufactures a false conflict,
# while a human veto (is_active/drop_reason change) is always detected.
_WATCHLIST_STATE_FIELDS = ("is_active", "drop_reason", "theme_name")


def _watchlist_conflict_fields(current: dict | None, applied_after: dict | None) -> list[str]:
    """Return the fields where the CURRENT row differs from the state this event applied.

    Empty list == unchanged since the auto-apply (safe to roll back). A row that was
    deleted (current is None) while the event expected a row is a conflict on all fields.
    """
    if current is None and applied_after is None:
        return []
    if current is None or applied_after is None:
        return list(_WATCHLIST_STATE_FIELDS)
    return [f for f in _WATCHLIST_STATE_FIELDS if current.get(f) != applied_after.get(f)]


def apply_watchlist_candidate(candidate: dict, watchlist, *, now: str) -> dict:
    """Apply one watchlist candidate to the SIMULATION watchlist, capturing exact
    before/after state for later compare-and-swap rollback."""
    sym = str(candidate.get("symbol") or "").upper()
    before = watchlist.get_symbol(sym)
    res = watchlist.promote_auto_approved(sym, confidence=float(candidate.get("confidence") or 0.9))
    after = watchlist.get_symbol(sym)
    status = "applied" if res.get("status") == "promoted" else res.get("status")
    return {
        "status": status,
        "before_state": before,
        "after_state": after,
        "promote_result": res,
        "applied_at": now,
    }


def rollback_watchlist_event(event: dict, watchlist) -> dict:
    """Compare-and-swap rollback: restore the event's before_state ONLY if the current
    state still equals the state the event applied. Otherwise record a conflict and
    preserve the current (newer) state — never overwrite work done since."""
    sym = str(event.get("symbol") or event.get("target_id") or "").upper()
    current = watchlist.get_symbol(sym)
    conflict = _watchlist_conflict_fields(current, event.get("after_state"))
    if conflict:
        return {"status": EVENT_ROLLBACK_CONFLICT, "conflicting_fields": conflict,
                "symbol": sym, "preserved_state": current}
    watchlist.restore_state(sym, event.get("before_state"))
    return {"status": ROLLBACK_OK, "symbol": sym,
            "restored_state": event.get("before_state")}


# ---------------------------------------------------------------------------
# Apply + event-aware compare-and-swap rollback — STRATEGY (simulation)
# ---------------------------------------------------------------------------


def apply_strategy_candidate(candidate: dict, *, now: str, base_dir: str,
                             valid_strategy_ids) -> dict:
    """Anchor the active SIMULATION strategy for one candidate, capturing before/after
    for compare-and-swap rollback. Never touches production or the decision engine."""
    from portfolio_automation.strategy import strategy_selection as SS
    sid = candidate.get("strategy_id")
    res = SS.record_auto_strategy_anchor(
        sid, valid_strategy_ids=valid_strategy_ids, now=now, base_dir=base_dir)
    if not res.get("ok"):
        return {"status": "skipped", "reason": res.get("reason"),
                "before_state": None, "after_state": None}
    return {"status": "applied", "before_state": res["before_state"],
            "after_state": res["after_state"]}


def rollback_strategy_event(event: dict, *, base_dir: str) -> dict:
    """Compare-and-swap rollback of an auto-anchored strategy: re-anchor the prior
    selection ONLY if the current active strategy still equals the one this event applied.
    Otherwise record a conflict and preserve the current (newer) selection."""
    from portfolio_automation.strategy import strategy_selection as SS
    current = SS.load_active_selection(base_dir).get("active_strategy_id")
    applied = (event.get("after_state") or {}).get("active_strategy_id")
    if current != applied:
        return {"status": EVENT_ROLLBACK_CONFLICT,
                "conflicting_fields": ["active_strategy_id"],
                "observed_active": current, "expected_active": applied}
    SS.restore_active_selection(event.get("before_state"), base_dir=base_dir, now=None)
    return {"status": ROLLBACK_OK, "restored_active":
            (event.get("before_state") or {}).get("active_strategy_id")}


# ---------------------------------------------------------------------------
# Orchestrator + human veto API
# ---------------------------------------------------------------------------


def _global_disabled_reason(config: dict, env: dict, kill_file_exists: bool) -> str | None:
    """Run-level (component-agnostic) enablement gate. Fail-closed."""
    if _env_truthy(env.get(KILL_SWITCH_ENV)):
        return "env_kill_switch"
    if kill_file_exists:
        return "file_kill_switch"
    if not isinstance(config, dict) or not isinstance(config.get("enabled"), bool):
        return "invalid_config"
    if not config.get("enabled"):
        return "global_disabled"
    return None


def _applied_today(base_dir: str, now: str, component: str) -> int:
    today = (now or "")[:10]
    return sum(1 for e in load_events(base_dir=base_dir)
               if e.get("kind") == EVENT_APPLIED
               and e.get("candidate_type") == component
               and str(e.get("application_timestamp") or e.get("ts") or "")[:10] == today)


def _predicted_watchlist_after(candidate: dict) -> dict:
    return {"symbol": str(candidate.get("symbol") or "").upper(), "is_active": 1,
            "drop_reason": None, "theme_name": "auto_approval_sim"}


def run_auto_approval(
    *,
    candidates: list[dict],
    now: str,
    base_dir: str,
    config: dict,
    source_artifact_path: str,
    source_artifact_hash: str,
    env: dict | None = None,
    kill_file_exists: bool = False,
    watchlist=None,
    valid_strategy_ids=None,
    approver: Callable[[str], str] | None = None,
    provider: str | None = None,
    model: str | None = None,
    static_symbols=None,
    prohibited_symbols=None,
    conflicting_symbols=None,
    state: dict | None = None,
    config_version: str = "",
    write_files: bool = True,
) -> dict:
    """Consume eligible verdicts, gate them, GPT-approve in-bounds, and apply bounded
    SIMULATION changes — auditing before every mutation. Never raises; fail-closed.

    Candidates that fail any gate or are vetoed are NOT applied and remain pending-human
    proposals (the existing production path). This channel never touches production.
    """
    env = env or {}
    valid_strategy_ids = set(valid_strategy_ids or [])
    static_symbols = {s.upper() for s in (static_symbols or set())}
    prohibited_symbols = {s.upper() for s in (prohibited_symbols or set())}
    conflicting_symbols = {s.upper() for s in (conflicting_symbols or set())}
    state = state or {}

    result = {
        "generated_at": now, "observe_only": False,
        "enabled": True, "disabled_reason": None,
        "applied_count": 0, "gpt_vetoed_count": 0, "invalid_count": 0,
        "rejected_count": 0, "already_applied_count": 0, "pending_fallback_count": 0,
        "results": [],
    }

    # Circuit breaker wins over everything.
    cb = circuit_breaker_reason(state)
    if cb:
        result.update(enabled=False, disabled_reason="circuit_breaker", circuit_breaker_reason=cb)
        return result

    # Run-level enablement (inert + side-effect-free when disabled).
    gdr = _global_disabled_reason(config, env, kill_file_exists)
    if gdr:
        result.update(enabled=False, disabled_reason=gdr)
        return result

    for cand in candidates or []:
        component = cand.get("candidate_type")
        target_id = cand.get("symbol") or cand.get("strategy_id")
        entry = {"candidate_id": cand.get("candidate_id"), "candidate_type": component,
                 "target_id": target_id}

        # Component enablement (e.g. strategy ships disabled).
        cdr = auto_approval_disabled_reason(config, component=component, env=env,
                                            kill_file_exists=kill_file_exists)
        if cdr:
            entry["status"] = cdr
            result["results"].append(entry)
            continue

        if component not in SUPPORTED_CANDIDATE_TYPES:
            entry["status"] = "unsupported_candidate_type"
            result["rejected_count"] += 1
            result["results"].append(entry)
            continue

        key = idempotency_key(
            source_verdict_id=str(cand.get("source_verdict_id") or ""),
            candidate_type=component, target_id=str(target_id or ""),
            source_artifact_hash=source_artifact_hash, policy_version=POLICY_VERSION)
        entry["idempotency_key"] = key

        if applied_key_exists(key, base_dir=base_dir):
            entry["status"] = "already_applied"
            result["already_applied_count"] += 1
            result["results"].append(entry)
            continue

        # Authority hard-gates.
        auth = run_authority_gates(cand)
        if not all_passed(auth):
            _record_reject(cand, key, auth, now, base_dir, source_artifact_path,
                           source_artifact_hash, write_files, reason="authority_gate_failed")
            entry["status"] = "rejected_authority"
            result["rejected_count"] += 1
            result["pending_fallback_count"] += 1
            result["results"].append(entry)
            continue

        # Component deterministic gates.
        if component == CANDIDATE_WATCHLIST:
            ctx = {
                "active_count": len(watchlist.get_active_symbols()) if watchlist else 0,
                "max_symbols": getattr(watchlist, "max_symbols", 0),
                "applied_today": _applied_today(base_dir, now, component),
                "active_awaiting_veto": build_summary(base_dir=base_dir, now=now)["active_item_count"],
                "prohibited": prohibited_symbols, "static": static_symbols,
                "conflicting_symbols": conflicting_symbols,
            }
            gates = run_watchlist_gates(cand, config, ctx)
        else:  # strategy
            from portfolio_automation.strategy.strategy_selection import load_active_selection
            active_id = load_active_selection(base_dir).get("active_strategy_id")
            ctx = {
                "applied_today": _applied_today(base_dir, now, component),
                "active_awaiting_veto": build_summary(base_dir=base_dir, now=now)["active_item_count"],
                "active_strategy_count": 1 if active_id else 0,
                "valid_strategy_ids": valid_strategy_ids, "prior_active_capturable": True,
            }
            gates = run_strategy_gates(cand, config, ctx)

        trace = auth + gates
        if not all_passed(gates):
            _record_reject(cand, key, trace, now, base_dir, source_artifact_path,
                           source_artifact_hash, write_files, reason="deterministic_gate_failed")
            entry["status"] = "rejected_deterministic"
            result["rejected_count"] += 1
            result["pending_fallback_count"] += 1
            result["results"].append(entry)
            continue

        # GPT approver — only after every deterministic gate passed (cost posture).
        verdict = gpt_approve_candidate(cand, provider=provider, model=model, approver=approver)
        if not is_gpt_approval(verdict):
            if write_files:
                append_event({"kind": EVENT_GPT_VETO, "event_id": _event_id(EVENT_GPT_VETO, key, now),
                              "idempotency_key": key, "candidate_type": component,
                              "target_id": target_id, "symbol": cand.get("symbol"),
                              "strategy_id": cand.get("strategy_id"), "ts": now,
                              "gpt_verdict": verdict.get("verdict"),
                              "gpt_reasoning": verdict.get("reason")}, base_dir=base_dir)
            if verdict.get("verdict") == GPT_INVALID:
                result["invalid_count"] += 1
                entry["status"] = "gpt_invalid"
            else:
                result["gpt_vetoed_count"] += 1
                entry["status"] = "gpt_vetoed"
            result["pending_fallback_count"] += 1
            result["results"].append(entry)
            continue

        # Apply — audit-before-mutate.
        if component == CANDIDATE_WATCHLIST:
            sym = str(cand.get("symbol")).upper()
            before = watchlist.get_symbol(sym)
            after = _predicted_watchlist_after(cand)
            mutate = lambda s=sym, c=cand: watchlist.promote_auto_approved(
                s, confidence=float(c.get("confidence") or 0.9))
        else:
            from portfolio_automation.strategy.strategy_selection import (
                load_active_selection, record_auto_strategy_anchor)
            sid = cand.get("strategy_id")
            prev = load_active_selection(base_dir) or None
            before = prev
            after = {"active_strategy_id": sid}
            mutate = lambda i=sid: record_auto_strategy_anchor(
                i, valid_strategy_ids=valid_strategy_ids, now=now, base_dir=base_dir)

        event = make_applied_event(
            now=now, idempotency_key=key, candidate=cand, gpt_verdict=verdict,
            gate_trace=trace, before_state=before, after_state=after,
            source_verdict_id=str(cand.get("source_verdict_id") or ""),
            source_artifact_path=source_artifact_path,
            source_artifact_hash=source_artifact_hash,
            source_verdict_timestamp=str(cand.get("source_verdict_timestamp") or now),
            model_id=verdict.get("model", model or _DEFAULT_MODEL),
            config_version=config_version)

        rr = record_and_apply(event, mutate, base_dir=base_dir) if write_files else {"ok": True}
        if rr.get("ok"):
            result["applied_count"] += 1
            entry["status"] = "applied"
            entry["event_id"] = event["event_id"]
        else:
            result["rejected_count"] += 1
            entry["status"] = "apply_failed"
            entry["reason"] = rr.get("reason")
        result["results"].append(entry)

    if write_files:
        try:
            write_summary(build_summary(base_dir=base_dir, now=now, state=state), base_dir=base_dir)
        except Exception:
            pass
    return result


def _record_reject(candidate, key, trace, now, base_dir, source_artifact_path,
                   source_artifact_hash, write_files, *, reason):
    if not write_files:
        return
    append_event({
        "kind": EVENT_DETERMINISTIC_REJECT,
        "event_id": _event_id(EVENT_DETERMINISTIC_REJECT, key, now),
        "idempotency_key": key, "candidate_type": candidate.get("candidate_type"),
        "target_id": candidate.get("symbol") or candidate.get("strategy_id"),
        "symbol": candidate.get("symbol"), "strategy_id": candidate.get("strategy_id"),
        "ts": now, "reason": reason,
        "gate_trace": [g.to_dict() if isinstance(g, GateResult) else g for g in trace],
        "routed_to": "pending_human_proposal",
    }, base_dir=base_dir)


def record_veto(event_id: str, *, operator_identity: str, base_dir: str,
                watchlist=None, now: str, reason: str | None = None,
                valid_strategy_ids=None) -> dict:
    """Human veto of a specific auto-applied event, with event-aware CAS rollback.

    Idempotent: a second veto of the same event is a no-op. Returns a status dict:
    rolled_back | rollback_conflict | rollback_failed | already_vetoed | unknown_event.
    """
    events = load_events(base_dir=base_dir)
    applied = next((e for e in events
                    if e.get("kind") == EVENT_APPLIED and e.get("event_id") == event_id), None)
    if applied is None:
        return {"status": "unknown_event", "event_id": event_id}

    # Idempotency: already vetoed?
    if any(e.get("kind") == EVENT_HUMAN_VETO and e.get("target_event_id") == event_id
           for e in events):
        return {"status": "already_vetoed", "event_id": event_id}

    key = applied.get("idempotency_key")
    # Record the human veto intent FIRST (durable), then perform the rollback.
    append_event({"kind": EVENT_HUMAN_VETO, "event_id": _event_id(EVENT_HUMAN_VETO, key or event_id, now),
                  "target_event_id": event_id, "idempotency_key": key,
                  "candidate_type": applied.get("candidate_type"),
                  "target_id": applied.get("target_id"), "symbol": applied.get("symbol"),
                  "strategy_id": applied.get("strategy_id"), "operator": operator_identity,
                  "reason": reason, "ts": now}, base_dir=base_dir)

    try:
        if applied.get("candidate_type") == CANDIDATE_WATCHLIST:
            rb = rollback_watchlist_event(applied, watchlist)
        else:
            rb = rollback_strategy_event(applied, base_dir=base_dir)
    except Exception as exc:  # rollback failed — RED condition, engage breaker
        append_event({"kind": EVENT_FAILURE, "target_event_id": event_id,
                      "idempotency_key": key, "ts": now,
                      "reason": f"rollback_failed:{exc}", "rollback_status": ROLLBACK_FAILED},
                     base_dir=base_dir)
        state = engage_circuit_breaker({}, "rollback_failed", now=now)
        try:
            write_summary(build_summary(base_dir=base_dir, now=now, state=state), base_dir=base_dir)
        except Exception:
            pass
        return {"status": ROLLBACK_FAILED, "event_id": event_id, "error": str(exc)}

    status = rb.get("status")
    kind = EVENT_ROLLBACK if status == ROLLBACK_OK else EVENT_ROLLBACK_CONFLICT
    append_event({"kind": kind, "event_id": _event_id(kind, key or event_id, now),
                  "target_event_id": event_id, "idempotency_key": key,
                  "candidate_type": applied.get("candidate_type"),
                  "target_id": applied.get("target_id"), "symbol": applied.get("symbol"),
                  "strategy_id": applied.get("strategy_id"), "operator": operator_identity,
                  "ts": now, "rollback": rb}, base_dir=base_dir)
    try:
        write_summary(build_summary(base_dir=base_dir, now=now), base_dir=base_dir)
    except Exception:
        pass
    return {"status": status, "event_id": event_id, "rollback": rb}


# ---------------------------------------------------------------------------
# Pipeline glue — map daily-AI-review verdicts to auto-approval candidates
# ---------------------------------------------------------------------------


def collect_auto_approval_candidates(review_result: dict, candidates_by_id: dict) -> list[dict]:
    """Build simulation auto-approval candidates from READY watchlist verdicts.

    Only ``ready_for_production_review`` verdicts for watchlist-eligible proposal types
    become candidates. Each is stamped with the authority-lane fields set to their
    simulation-safe values so the authority gates can verify them. This NEVER promotes to
    production — it only feeds the simulation auto-approval channel.
    """
    out: list[dict] = []
    for v in (review_result or {}).get("verdicts", []) or []:
        if v.get("decision") != S.DECISION_READY:
            continue
        cand = candidates_by_id.get(v.get("candidate_id"))
        if not cand:
            continue
        if cand.get("proposal_type") not in _WATCHLIST_ELIGIBLE_TYPES:
            continue
        if not cand.get("symbol"):
            continue
        out.append({
            "candidate_id": cand.get("candidate_id"),
            "candidate_type": CANDIDATE_WATCHLIST,
            "workflow": S.WORKFLOW_WATCHLIST,
            "proposal_type": cand.get("proposal_type"),
            "symbol": cand.get("symbol"),
            "confidence": float(cand.get("confidence") or 0.0),
            "why_changed": cand.get("why_changed") or v.get("reason"),
            "source_verdict_id": cand.get("candidate_id"),
            "source_verdict_timestamp": review_result.get("generated_at"),
            # Authority-lane fields — simulation-safe by construction.
            "target_lane": "simulation",
            "production_mutation": False,
            "feeds_decision_engine": False,
            "is_human_approved": False,
        })
    return out


def run_stage(*, root: str, now: str, sim_gov_config: dict, review_result: dict,
              candidates_by_id: dict, base_dir: str, env: dict | None = None,
              approver: Callable[[str], str] | None = None,
              provider: str | None = None, model: str | None = None,
              static_symbols=None, write_files: bool = True) -> dict:
    """Daily-pipeline stage entry point. Inert + side-effect-free when disabled; never
    raises. Builds candidates from the day's review, resolves the SEPARATE simulation
    watchlist DB, and runs the bounded auto-approval channel."""
    import os
    from pathlib import Path
    try:
        aa_cfg = (sim_gov_config or {}).get("auto_approval") or {}
        # Fast inert path — do not even build the DB when globally disabled.
        env = env if env is not None else dict(os.environ)
        kill_file = Path(root) / KILL_SWITCH_FILE
        gdr = _global_disabled_reason(aa_cfg, env, kill_file.exists())
        if gdr:
            return {"ok": True, "enabled": False, "disabled_reason": gdr,
                    "applied_count": 0, "gpt_vetoed_count": 0, "rejected_count": 0}

        candidates = collect_auto_approval_candidates(review_result or {}, candidates_by_id or {})
        src_hash = hashlib.sha256(
            json.dumps(review_result or {}, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]

        from watchlist_scanner.extended_watchlist import ExtendedWatchlist
        sim_db = Path(root) / aa_cfg.get("sim_watchlist_db_path", "data/sim_governance_watchlist.db")
        watchlist = ExtendedWatchlist(db_path=str(sim_db),
                                      max_symbols=int(aa_cfg.get("sim_max_symbols", 5)))

        res = run_auto_approval(
            candidates=candidates, now=now, base_dir=base_dir, config=aa_cfg,
            source_artifact_path="outputs/promotion_review/daily_ai_review_result.json",
            source_artifact_hash=src_hash, env=env, kill_file_exists=kill_file.exists(),
            watchlist=watchlist, valid_strategy_ids=set(),
            approver=approver, provider=provider, model=model,
            static_symbols=static_symbols, write_files=write_files)
        return {"ok": True, **res}
    except Exception as exc:  # non-blocking: never sink the pipeline
        return {"ok": False, "error": str(exc), "applied_count": 0}


def veto_from_gui(root: str, event_id: str, *, operator: str, reason: str | None,
                  now: str) -> dict:
    """GUI convenience: build the SIMULATION watchlist from config under *root* and veto
    a specific applied event. Never raises; returns record_veto's status dict."""
    from pathlib import Path
    from watchlist_scanner.extended_watchlist import ExtendedWatchlist
    try:
        cfg = json.loads((Path(root) / "config.json").read_text(encoding="utf-8"))
        aa_cfg = ((cfg.get("sim_governance") or {}).get("auto_approval") or {})
    except Exception:
        aa_cfg = {}
    base_dir = str(Path(root) / "outputs")
    sim_db = Path(root) / aa_cfg.get("sim_watchlist_db_path", "data/sim_governance_watchlist.db")
    watchlist = ExtendedWatchlist(db_path=str(sim_db),
                                  max_symbols=int(aa_cfg.get("sim_max_symbols", 5)))
    return record_veto(event_id, operator_identity=operator, base_dir=base_dir,
                       watchlist=watchlist, now=now, reason=reason)


# ---------------------------------------------------------------------------
# Deterministic health assessment — the signal the health skills read/verify
# ---------------------------------------------------------------------------

# Authority-channel fields every applied event MUST carry (breach => RED).
_APPLIED_AUTHORITY_INVARIANTS = {
    "is_human_approved": False,
    "target_lane": "simulation",
    "production_mutation": False,
    "feeds_decision_engine": False,
    "approval_channel": AUTO_APPROVAL_CHANNEL,
}
_RED_BREAKER_REASONS = frozenset({
    "rollback_failed", "ledger_corrupt", "unaudited_mutation",
    "state_ledger_inconsistent", "duplicate_application", "production_boundary",
})


def assess_health(*, base_dir: str, now: str, state: dict | None = None) -> dict:
    """Deterministic GREEN/AMBER/RED assessment over the ledger + summary. This is the
    signal the daily-tool-analysis + portfolio-learning-loop-health skills verify. A
    successful human veto + rollback is AMBER (control worked), never RED."""
    events = load_events(base_dir=base_dir)
    summary = build_summary(base_dir=base_dir, now=now, state=state)
    today = (now or "")[:10]
    reds: list[str] = []
    ambers: list[str] = []

    applied_keys: dict[str, int] = {}
    for e in events:
        kind = e.get("kind")
        if kind == EVENT_APPLIED:
            k = e.get("idempotency_key")
            applied_keys[k] = applied_keys.get(k, 0) + 1
            for field_name, required in _APPLIED_AUTHORITY_INVARIANTS.items():
                if e.get(field_name) != required:
                    reds.append(f"authority_breach:{e.get('event_id')}:{field_name}")
        if kind == EVENT_FAILURE and e.get("rollback_status") == ROLLBACK_FAILED:
            reds.append(f"rollback_failed:{e.get('idempotency_key')}")
    for k, n in applied_keys.items():
        if n > 1:
            reds.append(f"duplicate_application:{k}")

    cb = summary.get("circuit_breaker") or {}
    if cb.get("engaged"):
        reason = cb.get("reason")
        (reds if reason in _RED_BREAKER_REASONS else ambers).append(f"circuit_breaker:{reason}")

    if summary.get("active_item_count", 0) > 0:
        ambers.append(f"active_awaiting_veto:{summary['active_item_count']}")
    for e in events:
        if str(e.get("ts", ""))[:10] != today:
            continue
        if e.get("kind") in (EVENT_HUMAN_VETO, EVENT_ROLLBACK):
            ambers.append(f"{e.get('kind')}_today")
        if e.get("kind") == EVENT_ROLLBACK_CONFLICT:
            ambers.append("rollback_conflict_awaiting_operator")

    status = "RED" if reds else ("AMBER" if ambers else "GREEN")
    return {"status": status, "reds": reds, "ambers": sorted(set(ambers)),
            "counters": summary.get("counters", {}),
            "active_item_count": summary.get("active_item_count", 0),
            "circuit_breaker": cb}
