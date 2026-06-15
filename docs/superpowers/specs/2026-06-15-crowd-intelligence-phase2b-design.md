# Crowd Intelligence — Phase 2B: GUI Context + Advisory Enrichment + Daily Wiring (Design Spec)

- **Date:** 2026-06-15
- **Status:** Approved; implementing
- **Scope:** Surface Phase 2A artifacts as **context-only** on the Portfolio advisory
  picks + enrich explanations + non-blocking daily wiring. **No change** to
  recommendation generation, scoring, allocations, risk caps, BUY/SELL/HOLD, or trade
  execution. Artifact-only consumption — no FMP, no HTTP, no governor calls.
- **Builds on:** Phase 2A (`crowd_intelligence.json` / `_status.json`).

## 1. Files

```
portfolio_automation/crowd_intelligence/
  context_loader.py            # read artifacts -> per-symbol context + status (missing/stale safe)
  advisory_context_enricher.py # pure: context label + enrichment lines + forbidden-phrase guard
gui_v2/data/dash_crowd_context.py   # GUI loader: per-pick context dicts for the template
gui_v2/templates/components/decision_card.html   # MODIFY: render d.crowd_context block
gui_v2/templates/dashboard/portfolio.html        # MODIFY: crowd-context status banner + empty/stale
gui_v2/data/dash_portfolio.py                     # MODIFY: enrich decisions + ctx status
scripts/run_daily_safe.sh                          # MODIFY: non-blocking crowd-intel stage
tests/test_crowd_intelligence_phase2b.py
```

## 2. `context_loader.py` (artifact-only)

`load_crowd_context(root, *, max_age_hours=30, now=None) -> dict`:
- Reads `outputs/latest/crowd_intelligence.json` + `crowd_intelligence_status.json`.
- Returns `{available: bool, stale: bool, generated_at, by_symbol: {SYM: signal},
  social_disabled: bool, disabled_categories, missing_reason}`.
- Missing file → `available=False, missing_reason="not_generated"`. Parse error →
  `available=False, missing_reason="unreadable"`. Stale (age > max_age_hours) →
  `available=True, stale=True`. **No FMP/HTTP** — pure file reads.

## 3. `advisory_context_enricher.py` (pure)

- `context_label(signal | None) -> str` ∈ {`Supportive`, `Neutral`, `Caution`,
  `High Attention`, `Insufficient Data`}. Rules (in order):
  - `None`/no records/`confidence < 0.2` → **Insufficient Data**
  - `abs(category_scores.attention) >= 0.5` → **High Attention**
  - `composite >= 0.15` → **Supportive**; `composite <= -0.15` → **Caution**
  - else → **Neutral**
- `label_severity(label) -> str` for badge color: Supportive→green, Caution→yellow,
  High Attention→blue, Neutral→gray, Insufficient Data→gray. (No green/red BUY/SELL feel.)
- `enrich(signal, label, *, social_disabled) -> list[str]` context sentences, e.g.
  "Crowd context is neutral; advisory remains driven by portfolio drift/risk rules.",
  "Analyst context is supportive, but direct FMP social sentiment is unavailable on the
  current plan.", "Market attention is elevated; treat as context, not a trade signal."
- **Forbidden-phrase guard:** `FORBIDDEN` substrings (lowercased) — "buy because",
  "sell because", "confirms trade", "crowd signal confirms", "social sentiment is
  positive", "buy signal", "sell signal", "strong buy", "strong sell", "bullish",
  "bearish", "privileged", "insider knowledge". `assert_safe(text)` raises if any
  appears; `enrich` runs every line through it. Tests assert all generated text is safe
  across many synthetic signals.

## 4. GUI loader `dash_crowd_context.py`

`crowd_context_for(root, symbols) -> dict`: uses `context_loader` + enricher; returns
`{status: {available, stale, generated_at, social_disabled, banner}, by_symbol: {SYM:
{label, severity, composite, confidence, enabled_sources, disabled_sources,
data_freshness, top_reasons (≤3), warnings, lines, present}}}`. Symbol absent →
`{present: False, label: "Insufficient Data", lines: ["No crowd context available for
this symbol."]}`.

## 5. Portfolio tab integration

`dash_portfolio.collect_portfolio_view`: after `decisions = _top_decisions(dp)`, call
`crowd_context_for(root, [d["ticker"] for d in decisions])`; attach
`d["crowd_context"] = by_symbol.get(d["ticker"])` to each row and add
`ctx["crowd_context_status"]`. `decision_card.html` renders the context block
(label badge + composite/confidence + sources + ≤3 reasons + warnings + enrichment
lines) when `d.crowd_context.present`. `portfolio.html` shows the status banner:
- missing → "Crowd context unavailable — artifact not generated yet."
- stale → "Crowd context may be stale — last generated at {generated_at}."
- social disabled → "Direct FMP social sentiment is unavailable on the current Starter plan."

## 6. Labels

Context-oriented only (Supportive/Neutral/Caution/High Attention/Insufficient Data).
NEVER Bullish/Bearish/Buy Signal/Sell Signal/Strong Buy/Strong Sell (in the FORBIDDEN
guard + a template/test check).

## 7. Daily cadence wiring

Add a `run_aux_stage "Crowd intelligence"` to `run_daily_safe.sh` AFTER the decision
pipeline (so holdings/decisions exist) calling
`portfolio_automation.crowd_intelligence.artifact_writer.run('.')`. `run_aux_stage` is
already non-fatal (logs WARN, never aborts). `run()` already swallows all exceptions and
returns a status dict, so a crowd failure produces a warning, never a failed portfolio
run. Observe-only; does not mutate `decision_plan` or affect advisory selection.

## 8. Guardrails (hard invariants)

Context-only. MUST NOT alter recommendation generation, scoring, allocations, risk
caps, BUY/SELL/HOLD, or execution. Phase 2B makes **no FMP/HTTP/governor calls**
(artifact reads only — test-asserted via a no-network check + asserting no
`governed_client`/`FMPClient` import in the 2B modules). `decision_plan.json` untouched.

## 9. Tests

context_loader: missing → safe empty; unreadable → safe; stale flagged; fresh ok;
symbol-absent handled. enricher: label thresholds; every generated line passes the
forbidden guard; social-disabled line present when disabled; labels never contain
trade words. dash_portfolio: decisions get `crowd_context`; enrichment does not change
any decision `action`/`ticker` (assert decisions identical pre/post enrich except the
added `crowd_context` key); `decision_plan.json` byte-unchanged by a portfolio render.
No-FMP: a stub that fails if any FMP/governor symbol is invoked from 2B. Daily wiring:
`run()` returns a status dict (not raises) when artifacts/inputs are degraded. Existing
crowd/compliance/governor tests still pass.

## 10. Definition of done

Portfolio tab shows context-only crowd cards per advisory pick; missing/stale degrade
gracefully; direct social shown as unavailable; no decision/allocation/recommendation/
execution change; tests pass.
