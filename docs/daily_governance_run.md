# Simulation Governance — Daily Governance Run

## Purpose

`portfolio_automation/sim_governance/daily_governance_run.py` is the daily
orchestrator for the full two-lane simulation-governance pipeline. It runs AFTER
the production baseline artifacts already exist and chains the lane end to end:

```
baseline snapshot
  -> Flock Intelligence producer (writes simulation artifacts)
  -> active simulation lane (may change simulation outputs)
  -> daily simulation bundle (evidence)
  -> consolidated AI/product review packet (advisory + watchlist together)
  -> ONE gated AI/product review (<= $0.50/day, else deferred)
  -> pending production proposals for READY candidates
  -> apply already-human-approved proposals to the production overlays
```

---

## Two-Lane Governance

Every stage is wrapped so a failure in one never sinks the pipeline (non-blocking
integration). The simulation lane is active (sandbox-scoped); the AI review can
only recommend; production application applies only human-approved proposals, and
the production overlays remain default-OFF (`production_overlay_live` reports
their flags). Reads its knobs from `config.json` `sim_governance`. Never raises.

---

## Artifacts Written (OutputNamespace.PROMOTION_REVIEW → `outputs/promotion_review/`)

| File | Contents |
|------|----------|
| `daily_governance_status.json` | Compact per-stage status (schema `daily_governance_status.v1`) for the GUI / daily-tool-analysis |

The status carries `enabled`, `simulation_lane_active`, a `stages` map
(`flock_intelligence`, `simulation_lane`, `bundle`, `packet`, `ai_review`,
`proposals`, `production_application`), and roll-up counts
(`approved_proposal_count`, `rejected_proposal_count`, `pending_proposal_count`,
`production_overlay_live`).

The downstream stages write their own artifacts (see
`docs/daily_simulation_bundle.md`, `docs/ai_review_packet.md`,
`docs/daily_ai_review.md`, `docs/promotion_proposals.md`,
`docs/production_application.md`).

---

## Key Functions

- `run_daily_governance(root, now=None, *, config=None, reviewer=None,
  write_files=True) -> dict` — the orchestrator; supports an injectable
  `reviewer` (LLM seam) and `config`.
- `load_sim_governance_config(root) -> dict` — reads the `sim_governance` block
  from `config.json`, shallow-merged over `_DEFAULTS` (ai_review cap default
  `$0.50`, production overlays default OFF).
- `_enrich_baseline(root, baseline) -> dict` — best-effort pull of discovery
  promotion candidates + crowd/watchlist context for the experiments.
- `run(root=".")` — convenience entrypoint for the pipeline stage.

---

## Tests

Covered under `tests/` with the sim-governance suite
(`python -m pytest -q tests -k sim_governance`).
