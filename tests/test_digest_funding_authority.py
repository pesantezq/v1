"""The Finance Digest must not issue deploy-now capital instructions.

PROVENANCE (2026-08-08). A previous handoff reported "Finance Digest does not
exist — no module, no cron". That was WRONG: the search used the identifier
`finance_digest`, but the module is `email_digest.py`, built by
`digest_builder.py`. It is live — `main.py` sends it, and
`scripts/run_daily_safe.sh:168` runs `python main.py --run-mode daily` from the
daily cron, with `config.email.enabled = true`.

THE DEFECT. `digest_builder.build_top3_actions` emitted, verbatim:

    f"Deploy {format_currency(top.recommended_dollars)} → {top.symbol} "

from `ctx.contribution_plan` — the unconstrained monthly-contribution
allocation. Grepping `scoring.py`, `digest_builder.py` and `email_digest.py` for
`daily_capital_plan|funded_actions|pacing|deployable` returned ZERO matches: the
digest had no awareness of funding state whatsoever. So on a day when
`daily_capital_plan.json` said "No capital is funded for deployment today ($0
available after pacing)", the digest still emailed "Deploy $1,000 → VFH".

This is the contradiction the operator actually received.
"""
import pytest

import digest_builder as db


class _Alloc:
    def __init__(self, symbol, dollars, reason="target underweight"):
        self.symbol = symbol
        self.recommended_dollars = dollars
        self.reason = reason


def _ctx(plan, funding=None):
    ctx = db.DigestContext()
    ctx.contribution_plan = plan
    if funding is not None:
        ctx.funding_state = funding
    return ctx


def _joined(ctx):
    return " | ".join(db.build_top3_actions(ctx))


# ---------------------------------------------------------------------------
# The live defect
# ---------------------------------------------------------------------------
def test_unfunded_symbol_never_says_deploy():
    """$0 funded today must not produce 'Deploy $1,000 -> VFH'."""
    out = _joined(_ctx([_Alloc("VFH", 1000.0)],
                       funding={"available": True, "funded": {}}))
    assert "Deploy $" not in out
    assert "VFH" in out


def test_unfunded_symbol_uses_research_framing():
    """It may still surface VFH as the next priority — just not as an order."""
    out = _joined(_ctx([_Alloc("VFH", 1000.0)],
                       funding={"available": True, "funded": {}}))
    assert "next portfolio-priority" in out
    assert "not funded today" in out.lower()


def test_funded_symbol_may_say_deploy_with_the_funded_amount():
    """A genuinely funded action IS an instruction, and uses the FUNDED figure."""
    out = _joined(_ctx([_Alloc("VFH", 1000.0)],
                       funding={"available": True, "funded": {"VFH": 250.0}}))
    assert "Deploy $250" in out
    # never the unconstrained number
    assert "$1,000" not in out


def test_missing_funding_state_fails_closed_to_research_framing():
    """No funding evidence is NOT permission to assert an instruction."""
    out = _joined(_ctx([_Alloc("VFH", 1000.0)]))
    assert "Deploy $" not in out
    assert "VFH" in out


def test_unavailable_funding_authority_fails_closed():
    out = _joined(_ctx([_Alloc("VFH", 1000.0)], funding={"available": False}))
    assert "Deploy $" not in out


def test_partial_funding_uses_funded_amount_not_requested():
    out = _joined(_ctx([_Alloc("QQQ", 5000.0)],
                       funding={"available": True, "funded": {"QQQ": 151.98}}))
    assert "Deploy $152" in out or "Deploy $151" in out
    assert "$5,000" not in out


# ---------------------------------------------------------------------------
# Regressions in the surrounding behaviour
# ---------------------------------------------------------------------------
def test_zero_dollar_allocation_emits_no_money_language():
    """A $0 allocation falls through to the pre-existing 'no action' fallback —
    that branch is unchanged; what matters is that no imperative escapes."""
    out = _joined(_ctx([_Alloc("VFH", 0.0)], funding={"available": True, "funded": {}}))
    assert "Deploy $" not in out
    assert "No action needed" in out


def test_empty_contribution_plan_is_unchanged():
    ctx = db.DigestContext()
    assert isinstance(db.build_top3_actions(ctx), list)


def test_funding_state_defaults_exist_for_backward_compatibility():
    """Callers that never set funding_state must keep working."""
    assert db.DigestContext().funding_state is None


@pytest.mark.parametrize("phrase", ["Deploy $", "deploy $", "Buy $", "Invest $"])
def test_no_money_imperative_survives_when_nothing_is_funded(phrase):
    out = _joined(_ctx([_Alloc("VFH", 1000.0), _Alloc("QQQ", 800.0)],
                       funding={"available": True, "funded": {}}))
    assert phrase not in out


# ---------------------------------------------------------------------------
# The digest joins authority coverage (§2): its rendered actions are fed
# through the SAME leak detector that guards the memo, so the two products can
# never drift apart on what counts as a capital instruction.
# ---------------------------------------------------------------------------
def test_digest_output_passes_the_authority_leak_detector():
    from portfolio_automation import decision_authority as da

    actions = db.build_top3_actions(
        _ctx([_Alloc("VFH", 1000.0)], funding={"available": True, "funded": {}}))
    leaks = da.find_rendered_instructions(
        [{"name": "finance_digest", "text": "\n".join(actions)}])
    assert leaks == []


def test_the_pre_repair_string_would_have_been_caught():
    """Guards the guard: the exact sentence the operator received must trip it."""
    from portfolio_automation import decision_authority as da

    leaks = da.find_rendered_instructions([{
        "name": "finance_digest",
        "text": "Deploy $1,000.00 → VFH (monthly contribution — target underweight)"}])
    assert leaks and "VFH" in leaks[0]["context"]


# ---------------------------------------------------------------------------
# PART 2 AUDIT RESULT (2026-08-08). The previous handoff listed as a limitation:
# "Only the Top 3 Actions path is funding-gated; other digest sections may carry
# money language I haven't audited."
#
# Audited: ALL FOUR digest variants -- daily text (email_digest.py:142), daily
# HTML (:372), monthly text (:603), monthly HTML (:769) -- call the SAME
# build_top3_actions. The funding gate therefore reaches every digest surface.
# This test freezes that property: a future variant that hand-rolls its own
# action text would bypass the gate silently.
# ---------------------------------------------------------------------------
def test_every_digest_variant_routes_through_the_gated_builder():
    import re
    src = open("email_digest.py", encoding="utf-8").read()
    call_sites = len(re.findall(r"top3\s*=\s*build_top3_actions\(", src))
    assert call_sites >= 4, (
        f"expected >=4 digest variants routing through the gated builder, found "
        f"{call_sites} -- a variant may be hand-rolling action text and bypassing "
        f"the funding gate")


def test_digest_module_has_no_ungated_deploy_fstring():
    """The pre-repair pattern must not reappear anywhere in the digest layer."""
    import re
    for module in ("digest_builder.py", "email_digest.py"):
        src = open(module, encoding="utf-8").read()
        # 'Deploy {...}' is only legal when fed the funded amount
        for match in re.finditer(r'f"[^"]*Deploy \{([a-z_]+)', src):
            assert match.group(1) == "format_currency", (
                f"{module}: ungated Deploy f-string interpolating "
                f"{match.group(1)!r}")
        assert "recommended_dollars)} →" not in src, (
            f"{module}: renders unconstrained recommended_dollars as an instruction")
