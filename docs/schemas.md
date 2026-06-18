# `schemas.py` — Disambiguation

Two distinct modules share the basename `schemas.py`. They are unrelated and have
separate, dedicated documentation. This page exists so the basename resolves to a
doc; follow the link for the module you mean.

| Module | Package | Dedicated doc |
|--------|---------|---------------|
| `portfolio_automation/crowd_intelligence/schemas.py` | Crowd Intelligence (Lane B — FMP crowd context) | [`docs/crowd_intelligence_schemas.md`](crowd_intelligence_schemas.md) |
| `portfolio_automation/sim_governance/schemas.py` | Simulation Governance (two-lane promotion workflow) | [`docs/sim_governance_schemas.md`](sim_governance_schemas.md) |

- `portfolio_automation/crowd_intelligence/schemas.py` defines the normalized
  crowd record shapes (`NormalizedEvent`, `CategoryResult`, `CrowdSignal`) shared
  by the crowd adapters and builder.
- `portfolio_automation/sim_governance/schemas.py` defines the shared vocabulary,
  data model (`SimulationCandidate`, `ReviewVerdict`, `PromotionProposal`), and
  the structural validators (notably `is_human_approver`) that enforce
  "AI cannot self-approve production".

Both modules are pure (no I/O), observe-only in the crowd case and
governance-contract-only in the sim-governance case. Neither touches
`decision_engine.py` or any score semantics.
