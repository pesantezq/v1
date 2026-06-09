"""Claude Code prompt generators (Phase 4, spec §16).

Generates implementation/research prompts for the three prompt types. Health
remediation (Type A) already lives in ``operator_control/worker_prompts.py``;
this module adds Type C (system improvement) and Type B (market-opportunity
research). Every generated prompt embeds the mandatory safety/forbidden block so
a launched Claude Code session inherits the advisory-only invariants.

These functions return prompt TEXT only — they launch nothing and change no code.
"""
from __future__ import annotations

from typing import Any

# Forbidden in EVERY generated prompt (spec §16).
FORBIDDEN_BLOCK = """## Forbidden (hard constraints)
- No auto-trading, order placement, broker write actions, or money movement.
- No automatic portfolio allocation changes.
- Do NOT change protected scoring/decision logic (`decision_engine.py`,
  `scoring.py`, `signal_registry.yaml`, the six protected scores) without explicit
  owner approval.
- No unrelated refactors. Keep the change minimal and additive.
- New artifacts must carry `observe_only: true`.
"""

_FINAL_REPORT = """## Final report (required)
Files created / modified · Behavior implemented · Artifacts written (paths+namespace) ·
Tests added (file+count) · Test commands + results · Assumptions · Risks ·
Safety confirmation (no trading/broker-write/decision-plan changes) ·
Recommended next step.
"""


def generate_system_improvement_prompt(idea: dict[str, Any]) -> str:
    """Type C — turn a system-improvement idea into a Claude Code implementation prompt."""
    ev = "\n".join(f"  - {e}" for e in (idea.get("evidence") or [])) or "  - (see artifact)"
    mods = ", ".join(idea.get("affected_modules") or []) or "(to be determined during inspection)"
    arts = ", ".join(idea.get("affected_artifacts") or []) or "(none)"
    acc = "\n".join(f"  - {a}" for a in (idea.get("acceptance_criteria") or [])) or "  - (define during scoping)"
    tests = "\n".join(f"  - {t}" for t in (idea.get("suggested_tests") or [])) or "  - add targeted tests"
    return f"""# Claude Code — System Improvement Implementation (Type C)

## Repo context
Portfolio Automation System (advisory-only, observe-only). Read CLAUDE.md,
docs/ARCHITECTURE_MAP.md, and docs/NEXT_STAGE_PORTFOLIO_INTELLIGENCE_SPEC.md first.

## Problem
{idea.get('title', '')} — category: {idea.get('category', '')}
{idea.get('summary', '')}

## Evidence
{ev}

## Proposed change
{idea.get('proposed_change', '')}

## Scope
Smallest additive change that satisfies the acceptance criteria. Affected modules:
{mods}. Affected artifacts: {arts}.

## Files to inspect
The affected modules above + their tests + artifact_registry.yaml if an artifact changes.

## Acceptance criteria
{acc}

## Tests to run
{tests}

## Docs to update
Any doc describing the touched module/artifact; CHANGELOG if applicable.

{FORBIDDEN_BLOCK}
{_FINAL_REPORT}"""


def generate_market_opportunity_research_prompt(card: dict[str, Any]) -> str:
    """Type B — turn a review card into a (research-only) prompt for integration."""
    private = card.get("final_status") == "PRIVATE_WATCH_ONLY"
    access = ("Evaluate ACCESS ROUTES only (IPO watch / public suppliers / ETFs / proxies) — "
              "this candidate is private and NOT directly tradeable.\n" if private else "")
    return f"""# Claude Code — Market Opportunity Research (Type B, research-only)

## Repo context
Advisory-only portfolio intelligence. Research output is EVIDENCE ONLY — it never
becomes an official recommendation and never produces a buy/sell order.

## Candidate
{card.get('candidate', '')} — theme: {card.get('theme', '')} — status: {card.get('final_status', '')}
opportunity_score={card.get('opportunity_score')} boom_score={card.get('boom_score')} \
risk_score={card.get('risk_score')} investability={card.get('investability_score')}

## Research questions
{card.get('summary', '')}
{access}
## Output
A research review note (catalyst durability, fundamentals, valuation, crowding/hype,
liquidity, portfolio-fit). Recommend at most: send to sandbox / watchlist review.

{FORBIDDEN_BLOCK}
"""
