# Crowd Source Dev-Doc Audit

## Purpose

`portfolio_automation/social_sources/dev_doc_audit.py` is the validated,
research-backed catalog of crowd / social data sources. Each entry was verified
against **official developer documentation** (blogs and scraper marketplaces were
rejected as non-authoritative). It is the single source of truth for which sources
are `active`, their endpoints, auth model, response fields, pagination, rate
limits, cost/entitlement status, and recommended connector behavior. The audit
also produces the human-readable catalog artifact + Markdown.

---

## Two-Lane Governance

This module is part of the **simulation-active / production-gated / sandbox-only**
crowd / social-sentiment lane. The audit payload is research intelligence only —
it never feeds `outputs/latest/decision_plan.json` and never touches any score
semantics. The payload hardcodes `observe_only=true`, `sandbox_only=true`,
`no_trade=true`, and `cost_policy="no_extra_cost"`.

---

## No-Extra-Cost Policy

A source is classified `active` only if it is reachable with **no paid plan and no
approval**. Updated 2026-06-21 to remove paid/inert probes and add the free text
sources.

- **Active:** `apewisdom` (attention), `bluesky`, `mastodon`, `lemmy` (text).
- **Removed (paid/inert):** `fmp_social_sentiment` (paid Starter+ plan,
  never entitled), `finnhub_social` (premium-gated, 403 on free key),
  `stocktwits` (partner-gated, developer program suspended),
  `quiver_wsb` (paid ~$75/mo).

Each `SOURCE_AUDIT` record carries: `source_name`, `official_docs_url_or_source`,
`endpoint_candidates`, `auth_required`, `known_params`, `response_fields`,
`pagination_model`, `rate_limit_notes_if_available`, `cost_or_entitlement_status`,
`allowed_under_no_extra_cost`, `implementation_status`, `source_type`,
`risk_notes`, `recommended_connector_behavior`.

---

## Key API

- `SOURCE_AUDIT: list[dict]` — the catalog (consumed by `source_health` for the
  active / probe-only / blocked classification).
- `build_dev_doc_audit(*, run_id, run_mode, created_at) -> dict` — builds the
  `crowd_source_dev_doc_audit.json` payload (`schema_version="2"`,
  active/probe/blocked/removed source lists, governance flags, the full records).
- `render_dev_doc_audit_md(payload) -> str` — renders the human-readable
  `docs/CROWD_SOURCE_DEV_DOC_AUDIT.md` from the payload.

Constant: `SCHEMA_VERSION = "2"`.

---

## Related Modules

`source_health` (`_AUDIT_STATUS` is derived from `SOURCE_AUDIT`) · the connectors
`apewisdom_connector`, `bluesky_connector`, `mastodon_connector`,
`lemmy_connector`. The rendered catalog lives at
`docs/CROWD_SOURCE_DEV_DOC_AUDIT.md`.

---

## Tests

Covered under `tests/` with the social-sources suite
(`python -m pytest -q tests -k "audit or dev_doc"`).
