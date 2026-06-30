# Phase 7 — Memo / Investor-Decision Coherence: STATUS = complete-by-merge

The Phase 7 contract is fully satisfied by `portfolio_automation/memo_coherence.py`
(`run_memo_coherence`/`build_memo_coherence`), which was implemented + tested
separately and **merged into `main`** (commits 66f38982 / 3fc25dbb) before this
program branch was cut — so the SQG branch already carries it. See
`docs/MEMO_COHERENCE.md` for the full design.

Verified on the SQG branch: `tests/test_memo_coherence.py` → **46 passed**.

## Coverage vs the Phase 7 spec

| Phase 7 requirement | Where |
|---|---|
| Funding bridge (gross/capped/eligible/cash/incoming/deployable/funded/unfunded, each with universe) | `_funding_bridge` (reuses cash_deployment 5% reserve + monthly envelope) |
| Verdict reconciliation (action mix / capital / risk / regime / correlation / funding) | `build_memo_coherence` verdict block |
| Priority tie labeling (no fake precision; 0.55 default-fallback + transparent tie-break) | priority breakdown + default-fallback detection |
| Entry-context states (normal/starter/extended-prefer-pullback/blocked-by-cash/…) | `derive_presentation_state` (STARTER, ADD_ON_PULLBACK, BLOCKED_BY_CASH, …) |
| Correlation risk (effective bets / clusters / leveraged overlap / post-action concentration) | overlap clusters via correlation_risk_advisor |
| Crowd semantics separation (attention vs sentiment vs coverage vs sufficiency vs freshness) | `crowd.definitions` map (confirmed-attention ≠ classified state) |
| Investor-core vs operator-appendix split; research labeled as research | memo investor-core block + operator/system appendix |
| ±1% return neutral band (memo layer only; stored win-rate preserved) | neutral-band re-evaluation |

DoD met — the memo unambiguously answers: (1) what production recommends,
(2) what can be funded, (3) what risks prevent treating every BUY equally.

## Optional future polish (not required for DoD)
Bind the memo-coherence artifact to the Phase 1 `run_id` + Phase 2
`snapshot_hash` for end-to-end lineage. Deferred — `memo_coherence` is a
merged, consumed producer; an additive lineage stamp can land later without
risk to the shipped behavior.
