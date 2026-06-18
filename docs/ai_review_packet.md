# Simulation Governance — AI/Product Review Packet

## Purpose

`portfolio_automation/sim_governance/ai_review_packet.py` compresses the daily
simulation bundle into ONE consolidated review packet covering BOTH the advisory
and watchlist workflows together (the daily review must review them in a single
call). The packet is intentionally compact — one line of decision-relevant
evidence per candidate — so the single daily AI call stays well under the
$0.50/day cap.

---

## Two-Lane Governance

Build/aggregate-only; writes to the PROMOTION_REVIEW namespace. The packet's
instruction is explicit: the reviewer may classify and **recommend** readiness
but **cannot approve production** — human approval is the production gate. The
packet asserts `production_safe` and `decision_engine_untouched`.

---

## Artifacts Written (OutputNamespace.PROMOTION_REVIEW → `outputs/promotion_review/`)

| File | Contents |
|------|----------|
| `daily_ai_review_packet.json` | The consolidated packet (schema `daily_ai_review_packet.v1`) |
| `daily_ai_review_packet.md` | Human-readable rendering of the packet |

Packet fields: `instruction`, `covers_workflows`, `candidate_count`,
`advisory_candidates` / `watchlist_candidates` (one compact line each),
`risk_governance_checks`, `comparison_vs_production_baseline`,
`unified_crowd_summary`, `artifact_refs`, and `estimated_prompt_tokens`.

---

## Key Functions

- `build_review_packet(bundle, now) -> dict` — flattens advisory + watchlist
  experiment results into de-duplicated compact lines and assembles the packet.
- `estimate_packet_tokens(packet) -> int` — ~4 chars/token estimate used to gate
  the daily review's cost.
- `render_packet_md(packet) -> str` — Markdown table per workflow.
- `write_review_packet(packet, *, base_dir) -> dict` — writes JSON + MD; on
  failure returns the packet with a `write_error` key.

---

## Tests

Covered under `tests/` with the sim-governance suite
(`python -m pytest -q tests -k sim_governance`).
